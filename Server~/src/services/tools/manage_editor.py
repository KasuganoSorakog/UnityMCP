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
from transport.unity_transport import send_with_unity_instance
from transport.legacy.unity_connection import async_send_command_with_retry
from services.tools.utils import coerce_bool, coerce_float


@mcp_for_unity_tool(
    description=(
        "Controls the Unity editor. play/stop are idempotent and wait for readiness by default; "
        "operation_status retrieves an operation after a domain reload without repeating the transition. "
        "Read-only actions: telemetry_status, telemetry_ping, operation_status. "
        "Modifying actions: play, pause, stop, set_active_tool, add_tag, remove_tag, add_layer, remove_layer."
    ),
    annotations=ToolAnnotations(title="Manage Editor"),
)
async def manage_editor(
    ctx: Context,
    action: Annotated[Literal["telemetry_status", "telemetry_ping", "play", "pause", "stop", "operation_status", "set_active_tool", "add_tag", "remove_tag", "add_layer", "remove_layer"], "Get and update the Unity Editor state."],
    wait_for_completion: Annotated[bool | str, "Play/Stop: wait for the first frame or Stop and reconnection; default true."] | None = None,
    operation_id: Annotated[str, "GUID identifying a Play/Stop operation; generated on first request and reused when polling."] | None = None,
    timeout_seconds: Annotated[float | int | str, "Play/Stop deadline in seconds, 5-120; default 90."] = 90,
    tool_name: Annotated[str, "Tool name when setting active tool"] | None = None,
    tag_name: Annotated[str, "Tag name when adding and removing tags"] | None = None,
    layer_name: Annotated[str, "Layer name when adding and removing layers"] | None = None,
    unity_instance: Annotated[str, "Optional target Unity instance Name@hash. Overrides the session active instance for this call."] | None = None,
) -> dict[str, Any]:
    unity_instance = choose_unity_instance(ctx, unity_instance)
    wait = coerce_bool(wait_for_completion)
    timeout = coerce_float(timeout_seconds, None)
    if timeout is None or not math.isfinite(timeout) or not 5 <= timeout <= 120:
        return {"success": False, "message": "timeout_seconds must be a finite number between 5 and 120."}
    if operation_id is not None:
        try:
            uuid.UUID(operation_id)
        except (ValueError, AttributeError, TypeError):
            return {"success": False, "message": "operation_id must be a valid GUID."}
    elif action in {"play", "stop"}:
        operation_id = uuid.uuid4().hex

    try:
        if action == "telemetry_status":
            return {"success": True, "telemetry_enabled": False,
                    "message": "Telemetry has been removed in this fork; nothing is collected or sent."}
        if action == "telemetry_ping":
            return {"success": True,
                    "message": "Telemetry has been removed in this fork; nothing was queued or sent."}
        params = {
            "action": action,
            "waitForCompletion": wait,
            "operationId": operation_id,
            "timeoutSeconds": timeout,
            "toolName": tool_name,
            "tagName": tag_name,
            "layerName": layer_name,
        }
        params = {key: value for key, value in params.items() if value is not None}
        response = await _send(unity_instance, params)
        if action in {"play", "stop"}:
            if wait is not False:
                return await _await_transition(ctx, unity_instance, action, operation_id, response, timeout)
            return _with_operation(_shape(response), operation_id, action)
        return _shape(response)
    except Exception as exc:
        return _with_operation(
            {"success": False, "message": f"Python error managing editor: {exc}"},
            operation_id, action,
        )


async def _send(unity_instance: str | None, params: dict[str, Any]) -> dict[str, Any]:
    response = await send_with_unity_instance(
        async_send_command_with_retry, unity_instance, "manage_editor", params,
    )
    if hasattr(response, "model_dump"):
        response = response.model_dump(exclude_none=True)
    return response if isinstance(response, dict) else {"success": False, "message": str(response)}


def _shape(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("success"):
        return {**response, "message": response.get("message", "Editor operation successful.")}
    return response


def _with_operation(response: dict[str, Any], operation_id: str | None, action: str) -> dict[str, Any]:
    if not operation_id or action not in {"play", "stop", "operation_status"}:
        return response
    data = response.get("data")
    return {**response, "data": {**(data if isinstance(data, dict) else {}), "operationId": operation_id}}


async def _await_transition(
    ctx: Context,
    instance: str | None,
    action: str,
    operation_id: str,
    response: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout + 15
    recoverable = {
        "no_unity_session", "unity_session_disconnected", "unity_execution_state_unknown",
        "unity_command_timeout", "unity_instance_unreachable",
    }
    while True:
        data = response.get("data")
        if isinstance(data, dict) and data.get("operationId"):
            operation_id = str(data["operationId"])
        if response.get("success") and response.get("_mcp_status") != "pending":
            return _with_operation(_shape(response), operation_id, action)
        if not response.get("success") and response.get("code") not in recoverable:
            return _with_operation(_shape(response), operation_id, action)
        if time.monotonic() >= deadline:
            return {
                "success": False, "code": "play_mode_recovery_timeout",
                "error": "Play/Stop result is still unknown; query operation_status before any other transition.",
                "data": {"operationId": operation_id, "action": action},
            }
        await asyncio.sleep(0.5)
        response = await _send(instance, {
            "action": "operation_status", "operationId": operation_id, "timeoutSeconds": 15,
        })
        error = str(response.get("error") or response.get("message") or "")
        if not response.get("success") and "Unknown action" in error:
            return await _recover_legacy_transition(ctx, instance, action, operation_id, deadline)


async def _recover_legacy_transition(
    ctx: Context, instance: str | None, action: str, operation_id: str, deadline: float,
) -> dict[str, Any]:
    from services.resources.editor_state import get_editor_state_for_instance

    while time.monotonic() < deadline:
        try:
            raw = await get_editor_state_for_instance(ctx, instance)
            payload = raw.model_dump() if hasattr(raw, "model_dump") else raw
            data = payload.get("data") if payload.get("success") else None
            if isinstance(data, dict):
                play = data.get("editor", {}).get("play_mode", {})
                if (play.get("is_playing") is (action == "play")
                        and data.get("advice", {}).get("ready_for_tools") is True):
                    return {
                        "success": True, "message": "Play/Stop recovered after a session reconnect.",
                        "data": {**play, "operationId": operation_id, "recoveredAfterSessionDisconnect": True},
                    }
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return {
        "success": False, "code": "play_mode_recovery_timeout",
        "error": "The editor did not reach the requested stable Play state.",
        "data": {"operationId": operation_id},
    }
