from __future__ import annotations

import asyncio
import math
import re
import time
import uuid
from typing import Annotated, Any

from fastmcp import Context
from mcp.types import ToolAnnotations

from services.registry import mcp_for_unity_tool
from services.resources.editor_state import get_editor_state_for_instance
from services.tools import choose_unity_instance
from services.tools.capture_view import capture_view
from services.tools.manage_editor import manage_editor
from services.tools.preflight import preflight
from services.tools.sample_runtime import sample_runtime
from services.tools.utils import coerce_bool, coerce_float, parse_json_payload
from transport.legacy.unity_connection import async_send_command_with_retry
from transport.unity_transport import send_with_unity_instance

_ENTRY_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_+]*(?:\.[A-Za-z_][A-Za-z0-9_+]*)+::[A-Za-z_][A-Za-z0-9_]*$"
)


@mcp_for_unity_tool(
    description=(
        "Run a bounded Unity simulation: enter Play, invoke a trusted public static Namespace.Type::Method "
        "without parameters, optionally capture/sample, then always request Stop and confirm Edit mode. "
        "Refuses to take over an existing Play/transition session. capture/sample accept true or an options "
        "object matching capture_view/sample_runtime; nested unity_instance is not allowed. sample requires target."
    ),
    annotations=ToolAnnotations(title="Play Simulation", destructiveHint=True),
)
async def play_simulation(
    ctx: Context,
    entry: Annotated[str, "Loaded public static parameterless method, in Namespace.Type::Method form."],
    duration: Annotated[float | int | str, "Observation time after entry and target readiness, 0.1-60 seconds; default 10."] = 10,
    capture: Annotated[bool | str | dict[str, Any], "False, true, or capture_view options such as angles/grid/file_name."] = False,
    sample: Annotated[bool | str | dict[str, Any], "False, true, or sample_runtime options such as properties/include_samples."] = False,
    target: Annotated[str, "Default target for capture/sample; required for sample=true."] | None = None,
    timeout_seconds: Annotated[float | int | str, "Deadline for each Play and Stop transition, 5-120; default 90."] = 90,
    ready_timeout_seconds: Annotated[float | int | str, "Deadline for each capture/sample target after entry, 0-60; default 15."] = 15,
    unity_instance: Annotated[str, "Target Name@hash; also enforced for nested tools."] | None = None,
) -> dict[str, Any]:
    instance = choose_unity_instance(ctx, unity_instance)
    duration_value = coerce_float(duration, None)
    timeout_value = coerce_float(timeout_seconds, None)
    ready_timeout_value = coerce_float(ready_timeout_seconds, None)
    if duration_value is None or not math.isfinite(duration_value) or not 0.1 <= duration_value <= 60:
        return {"success": False, "message": "duration must be a finite number between 0.1 and 60."}
    if timeout_value is None or not math.isfinite(timeout_value) or not 5 <= timeout_value <= 120:
        return {"success": False, "message": "timeout_seconds must be a finite number between 5 and 120."}
    if ready_timeout_value is None or not math.isfinite(ready_timeout_value) or not 0 <= ready_timeout_value <= 60:
        return {"success": False, "message": "ready_timeout_seconds must be a finite number between 0 and 60."}
    if not isinstance(entry, str) or not _ENTRY_PATTERN.fullmatch(entry.strip()):
        return {"success": False, "message": "entry must use Namespace.Type::Method without assembly or argument lists."}
    entry = entry.strip()
    try:
        capture_options = _nested_options(capture, "capture")
        sample_options = _nested_options(sample, "sample")
    except ValueError as exc:
        return {"success": False, "message": str(exc)}
    for options in (capture_options, sample_options):
        if options is not None and target is not None:
            options.setdefault("target", target)
    if sample_options is not None and not sample_options.get("target"):
        return {"success": False, "message": "sample requires target, either at the top level or in the sample options."}

    result: dict[str, Any] = {
        "success": False,
        "data": {
            "entry": entry,
            "playOperationId": uuid.uuid4().hex,
            "stopOperationId": uuid.uuid4().hex,
            "entryResult": None,
            "targetReadyResults": [],
            "captureResult": None,
            "sampleResult": None,
            "stopResult": None,
            "stopConfirmed": False,
        },
    }
    gate = await preflight(ctx, unity_instance=instance, wait_for_no_compile=True, refresh_if_dirty=False)
    if gate is not None:
        return gate.model_dump()
    initial_state = await _play_mode(ctx, instance)
    if initial_state is None:
        return {**result, "code": "editor_state_unavailable", "error": "Could not verify the initial Play state; no transition was requested."}
    if initial_state.get("is_playing") or initial_state.get("is_changing"):
        return {
            **result, "code": "play_session_already_active",
            "error": "Play or a Play transition is already active; this tool will not stop an existing user session.",
            "data": {**result["data"], "initialPlayMode": initial_state},
        }

    play_requested = False
    try:
        play_requested = True
        play = await manage_editor(
            ctx, action="play", operation_id=result["data"]["playOperationId"],
            wait_for_completion=True, timeout_seconds=timeout_value, unity_instance=instance,
        )
        result["data"]["playResult"] = play
        if not play.get("success"):
            return result
        result["data"]["entryResult"] = await _invoke_entry(instance, entry)
        if not result["data"]["entryResult"].get("success"):
            return result
        targets = {}
        for options in (capture_options, sample_options):
            if options is not None and options.get("target"):
                targets[options["target"]] = options.get("include_inactive", False)
        for target_name, include_inactive in targets.items():
            ready = await _wait_for_target(
                ctx, instance, target_name, include_inactive, ready_timeout_value,
            )
            result["data"]["targetReadyResults"].append(ready)
            if not ready.get("success"):
                result["code"] = ready.get("code", "simulation_target_not_ready")
                result["error"] = ready.get("error") or ready.get("message")
                return result

        tasks: list[asyncio.Task] = []
        task_names: list[str] = []
        if capture_options is not None:
            task_names.append("captureResult")
            tasks.append(asyncio.create_task(_call_safely(
                capture_view, ctx=ctx, unity_instance=instance, **capture_options,
            )))
        if sample_options is not None:
            sample_options.setdefault("duration", duration_value)
            sample_options.setdefault("sample_id", uuid.uuid4().hex)
            result["data"]["sampleId"] = sample_options["sample_id"]
            task_names.append("sampleResult")
            tasks.append(asyncio.create_task(_call_safely(
                sample_runtime, ctx=ctx, unity_instance=instance, **sample_options,
            )))
        try:
            nested = await asyncio.wait_for(
                asyncio.gather(*tasks, asyncio.sleep(duration_value)),
                timeout=duration_value + 120,
            )
        except asyncio.TimeoutError:
            result.update({
                "code": "simulation_observation_timeout",
                "error": "Capture/sample observation exceeded its deadline; Stop was still requested.",
            })
            return result
        for name, value in zip(task_names, nested):
            result["data"][name] = value
        result["success"] = all(
            result["data"][name] is None or result["data"][name].get("success")
            for name in ("captureResult", "sampleResult")
        )
        return result
    finally:
        if play_requested:
            stop_task = asyncio.create_task(manage_editor(
                ctx, action="stop", operation_id=result["data"]["stopOperationId"],
                wait_for_completion=True, timeout_seconds=timeout_value, unity_instance=instance,
            ))
            result["data"]["stopResult"] = await _await_cleanup(stop_task)
            stopped = await _await_cleanup(asyncio.create_task(_confirm_stopped(ctx, instance)))
            result["data"]["stopConfirmed"] = stopped is True
            result["success"] = bool(
                result["success"] and result["data"]["stopResult"].get("success")
                and result["data"]["stopConfirmed"]
            )


async def _await_cleanup(task: asyncio.Task) -> Any:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    if task.cancelled():
        return {"success": False, "error": "The cleanup task was cancelled before completion."}
    try:
        return task.result()
    except asyncio.CancelledError:
        return {"success": False, "error": "The cleanup task was cancelled before completion."}
    except Exception as exc:
        return {"success": False, "error": f"Simulation cleanup failed: {exc}"}


async def _wait_for_target(
    ctx: Context,
    instance: str | None,
    target: str,
    include_inactive: bool | str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_response: dict[str, Any] = {}
    while True:
        mode = await _play_mode(ctx, instance)
        if mode is not None and mode.get("is_playing") is not True:
            return {
                "success": False, "code": "play_session_ended",
                "error": "Play ended before the observation target became ready.",
                "data": {"target": target},
            }
        try:
            response = await send_with_unity_instance(
                async_send_command_with_retry, instance, "resolve_runtime_target",
                {"target": target, "includeInactive": include_inactive, "timeoutSeconds": 15},
            )
            if hasattr(response, "model_dump"):
                response = response.model_dump(exclude_none=True)
            last_response = response if isinstance(response, dict) else {"success": False, "error": str(response)}
        except Exception as exc:
            last_response = {"success": False, "error": str(exc)}
        if last_response.get("success"):
            return last_response
        error = str(last_response.get("error") or last_response.get("message") or "")
        if "not found" not in error.lower():
            return {
                "success": False, "code": "runtime_target_unavailable",
                "error": error or "Could not resolve the observation target.", "data": {"target": target},
            }
        if time.monotonic() >= deadline:
            return {
                "success": False, "code": "simulation_target_not_ready",
                "error": f"Observation target '{target}' did not become ready within {timeout} seconds.",
                "data": {"target": target, "lastResponse": last_response},
            }
        await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))


async def _invoke_entry(instance: str | None, entry: str) -> dict[str, Any]:
    try:
        response = await send_with_unity_instance(
            async_send_command_with_retry, instance, "invoke_simulation_entry",
            {"entry": entry, "timeoutSeconds": 30},
        )
        if hasattr(response, "model_dump"):
            response = response.model_dump(exclude_none=True)
        return response if isinstance(response, dict) else {"success": False, "error": str(response)}
    except Exception as exc:
        return {"success": False, "error": f"Could not invoke simulation entry: {exc}"}


def _nested_options(value: bool | str | dict[str, Any], name: str) -> dict[str, Any] | None:
    value = parse_json_payload(value)
    if isinstance(value, dict):
        options = dict(value)
    else:
        enabled = coerce_bool(value, None)
        if enabled is None:
            raise ValueError(f"{name} must be a boolean or an options object.")
        if not enabled:
            return None
        options = {}
    if "unity_instance" in options:
        raise ValueError(f"Nested {name}.unity_instance is not allowed; use the outer unity_instance.")
    return options


async def _call_safely(func, **kwargs) -> dict[str, Any]:
    try:
        response = await func(**kwargs)
        if hasattr(response, "model_dump"):
            response = response.model_dump(exclude_none=True)
        return response if isinstance(response, dict) else {"success": False, "error": str(response)}
    except Exception as exc:
        return {"success": False, "error": f"{func.__name__} failed: {exc}"}


async def _play_mode(ctx: Context, instance: str | None) -> dict[str, Any] | None:
    try:
        response = await get_editor_state_for_instance(ctx, instance)
        payload = response.model_dump() if hasattr(response, "model_dump") else response
        data = payload.get("data") if payload.get("success") else None
        mode = data.get("editor", {}).get("play_mode") if isinstance(data, dict) else None
        return mode if isinstance(mode, dict) else None
    except Exception:
        return None


async def _confirm_stopped(ctx: Context, instance: str | None) -> bool:
    deadline = time.monotonic() + 30
    while True:
        mode = await _play_mode(ctx, instance)
        if mode is not None and mode.get("is_playing") is False and mode.get("is_changing") is not True:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.5)
