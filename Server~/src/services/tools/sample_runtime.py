from __future__ import annotations

import asyncio
import math
import time
import uuid
from typing import Annotated, Any, Literal

from fastmcp import Context
from mcp.types import ToolAnnotations

from services.registry import mcp_for_unity_tool
from services.tools import choose_unity_instance
from services.tools.preflight import preflight
from services.tools.utils import coerce_bool, coerce_float, coerce_int, parse_json_payload
from transport.legacy.unity_connection import async_send_command_with_retry
from transport.unity_transport import send_with_unity_instance


@mcp_for_unity_tool(
    description=(
        "Read-only per-frame sampling during an existing Play session; does not enter Play or modify business code. "
        "Properties: transform.position/localPosition/rotation/localRotation/eulerAngles/localEulerAngles/localScale/lossyScale, "
        "animator (layer 0 state events and normalizedTime), animator.layerN[.state|.normalized_time], or ComponentType.field[.nestedField]. "
        "Fields must be public or [SerializeField], with numeric/Vector/Quaternion/Color/bool/string/enum leaves. "
        "Returns bounded statistics, spikes, and Animator events. Raw samples are opt-in and downsampled. "
        "If Play exits or Unity reloads, partial results can be retrieved with action=result and the returned sample_id."
    ),
    annotations=ToolAnnotations(title="Sample Runtime", readOnlyHint=True),
)
async def sample_runtime(
    ctx: Context,
    action: Annotated[Literal["sample", "result", "cancel"], "Start/await a sample, retrieve a saved result, or cancel the active sample."] = "sample",
    target: Annotated[str, "Target hierarchy path, unique GameObject/bone name, or instance ID."] | None = None,
    properties: Annotated[list[str] | str, "Dot-separated property paths; defaults to transform.position, transform.rotation, animator."] | None = None,
    fields: Annotated[list[str] | str, "Alias for properties. Do not supply both."] | None = None,
    duration: Annotated[float | str, "Sampling duration in seconds, 0.1-60; default 10."] = 10,
    include_samples: Annotated[bool | str, "Include evenly downsampled raw samples; default false returns statistics/events only."] = False,
    max_samples: Annotated[int | str, "Maximum raw samples per metric, 2-600; default 120."] = 120,
    max_spikes: Annotated[int | str, "Maximum spikes per metric, 1-100; default 20."] = 20,
    spike_threshold: Annotated[float | str | dict[str, float], "Frame-delta threshold: a number for all metrics, 'auto', or a JSON object keyed by property path. Quaternion deltas are degrees."] | None = None,
    include_inactive: Annotated[bool | str, "Resolve inactive targets; default false."] = False,
    sample_id: Annotated[str, "32-hex-character sample ID used with result/cancel. Normally generated automatically."] | None = None,
    unity_instance: Annotated[str, "Optional target Unity instance Name@hash. Overrides the session active instance for this call."] | None = None,
) -> dict[str, Any]:
    unity_instance = choose_unity_instance(ctx, unity_instance)
    if properties is not None and fields is not None:
        return {"success": False, "message": "Provide either properties or fields, not both."}
    properties = parse_json_payload(properties if properties is not None else fields)
    if properties is not None and not isinstance(properties, (list, str)):
        return {"success": False, "message": "properties must be an array of strings or a comma-separated string."}
    spike_threshold = parse_json_payload(spike_threshold)
    if isinstance(spike_threshold, str) and spike_threshold.strip().lower() == "auto":
        spike_threshold = None
    if spike_threshold is not None and not isinstance(spike_threshold, (int, float, dict)):
        return {"success": False, "message": "spike_threshold must be a number, 'auto', or a property-to-number object."}
    numeric_values = {
        "duration": coerce_float(duration, None),
        "maxSamples": coerce_int(max_samples, None),
        "maxSpikes": coerce_int(max_spikes, None),
    }
    for name, value in numeric_values.items():
        if value is None or (name == "duration" and not math.isfinite(value)):
            return {"success": False, "message": f"{name} must be a valid finite number."}
    if isinstance(spike_threshold, (int, float)) and not math.isfinite(spike_threshold):
        return {"success": False, "message": "spike_threshold must be finite."}
    bool_values = {
        "includeSamples": coerce_bool(include_samples, None),
        "includeInactive": coerce_bool(include_inactive, None),
    }
    for name, value in bool_values.items():
        if value is None:
            return {"success": False, "message": f"{name} must be a boolean."}

    if action in {"result", "cancel"} and not sample_id:
        return {"success": False, "message": f"sample_id is required for action={action}."}
    resolved_id = sample_id or uuid.uuid4().hex
    if not isinstance(resolved_id, str) or len(resolved_id) != 32:
        return {"success": False, "message": "sample_id must contain exactly 32 hexadecimal characters."}
    try:
        uuid.UUID(resolved_id)
    except (ValueError, AttributeError, TypeError):
        return {"success": False, "message": "sample_id must contain exactly 32 hexadecimal characters."}

    if action in {"result", "cancel"}:
        params = {"action": action, "sampleId": resolved_id, "timeoutSeconds": 15}
    else:
        params = {
            "action": action,
            "sampleId": resolved_id,
            "target": target,
            "duration": numeric_values["duration"],
            "maxSamples": numeric_values["maxSamples"],
            "maxSpikes": numeric_values["maxSpikes"],
            **bool_values,
            "timeoutSeconds": min(120, max(30, math.ceil(numeric_values["duration"] + 15))),
        }
        if properties is not None:
            params["properties"] = properties
        if spike_threshold is not None:
            params["spikeThresholds"] = spike_threshold
    gate = await preflight(ctx, unity_instance=unity_instance, wait_for_no_compile=True, refresh_if_dirty=False)
    if gate is not None:
        return gate.model_dump()
    response = _with_sample_id(await _send(unity_instance, params), resolved_id)
    if action != "sample":
        return response
    if not response.get("success") and not _is_recoverable(response):
        return response
    return await _await_sample_result(ctx, unity_instance, resolved_id, response, numeric_values["duration"])


def _with_sample_id(response: dict[str, Any], sample_id: str) -> dict[str, Any]:
    data = response.get("data")
    return {**response, "data": {**(data if isinstance(data, dict) else {}), "sampleId": sample_id}}


async def _await_sample_result(
    ctx: Context,
    instance: str | None,
    sample_id: str,
    response: dict[str, Any],
    duration: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + min(120, max(30, math.ceil(duration + 15)))
    recovered = False
    polling_result = False
    while True:
        if response.get("success") and response.get("_mcp_status") != "pending":
            if recovered and isinstance(response.get("data"), dict):
                response["data"]["recoveredAfterSessionDisconnect"] = True
            return response
        busy_during_result_poll = polling_result and response.get("code") == "unity_instance_busy"
        if not response.get("success") and not _is_recoverable(response) and not busy_during_result_poll:
            return response
        if time.monotonic() >= deadline:
            return {
                "success": False,
                "code": "sample_result_timeout",
                "error": "The runtime sample result is not available yet; its state is unknown, not successful.",
                "data": {
                    "sampleId": sample_id,
                    "resultAvailable": False,
                    "nextAction": "Call sample_runtime action=result with this sample_id after Unity reconnects.",
                },
            }
        interval = response.get("_mcp_poll_interval", 0.5) if response.get("success") else 0.5
        if not isinstance(interval, (int, float)) or not math.isfinite(interval):
            interval = 0.5
        await asyncio.sleep(min(5, max(0.1, interval)))
        if not response.get("success"):
            recovery = await _wait_for_refresh_completion(
                ctx, instance, baseline_reload_after_ms=None, require_reload_evidence=False, deadline=deadline,
            )
            if not recovery.get("completed"):
                return {
                    "success": False,
                    "code": "sample_recovery_timeout",
                    "error": "Unity did not reconnect in time; retrieve the saved partial result later.",
                    "data": {"sampleId": sample_id, "resultAvailable": False},
                    "recovery": recovery,
                }
            recovered = True
        response = _with_sample_id(await _send(instance, {
            "action": "result", "sampleId": sample_id, "timeoutSeconds": 15,
        }), sample_id)
        polling_result = True


async def _wait_for_refresh_completion(
    ctx: Context,
    unity_instance: str | None,
    baseline_reload_after_ms: int | None = None,
    *,
    require_reload_evidence: bool = False,
    deadline: float | None = None,
) -> dict[str, Any]:
    from services.resources.editor_state import get_editor_state_for_instance

    deadline = deadline if deadline is not None else time.monotonic() + 60
    while True:
        try:
            response = await get_editor_state_for_instance(ctx, unity_instance)
            payload = response.model_dump() if hasattr(response, "model_dump") else response
            data = payload.get("data") if payload.get("success") else None
            if isinstance(data, dict) and data.get("advice", {}).get("ready_for_tools") is True:
                return {"completed": True, "domain_reload_observed": False, "busy_observed": False}
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return {"completed": False, "domain_reload_observed": False, "busy_observed": False}
        await asyncio.sleep(0.5)


async def _send(unity_instance: str | None, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await send_with_unity_instance(
            async_send_command_with_retry, unity_instance, "sample_runtime", params,
        )
        return response if isinstance(response, dict) else {"success": False, "message": str(response)}
    except Exception as exc:
        return {"success": False, "message": f"Python error sampling runtime: {exc}"}


def _is_recoverable(response: dict[str, Any]) -> bool:
    return response.get("code") in {
        "no_unity_session", "unity_execution_state_unknown", "unity_session_disconnected",
        "unity_command_timeout", "unity_instance_unreachable",
    }
