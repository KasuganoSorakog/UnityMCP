from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any, Literal

from fastmcp import Context
from mcp.types import ToolAnnotations

from models import MCPResponse
from services.registry import mcp_for_unity_tool
from services.tools import choose_unity_instance
import transport.unity_transport as unity_transport
from transport.legacy.unity_connection import async_send_command_with_retry
from services.state.external_changes_scanner import external_changes_scanner
import services.resources.editor_state as editor_state


RELOAD_RECOVERY_TIMEOUT_SECONDS = 60.0
RELOAD_RECOVERY_POLL_SECONDS = 0.5


def _as_payload(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        payload = response.model_dump()
        return payload if isinstance(payload, dict) else {}
    return {}


def _state_data(response: Any) -> dict[str, Any] | None:
    payload = _as_payload(response)
    if not payload.get("success", False):
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _domain_reload_after_ms(data: dict[str, Any] | None) -> int | None:
    compilation = data.get("compilation") if isinstance(data, dict) else None
    if not isinstance(compilation, dict):
        return None
    value = compilation.get("last_domain_reload_after_unix_ms")
    return value if isinstance(value, int) else None


def _is_ready(data: dict[str, Any] | None) -> bool:
    advice = data.get("advice") if isinstance(data, dict) else None
    return isinstance(advice, dict) and advice.get("ready_for_tools") is True


def _is_session_disconnect(response: Any) -> bool:
    payload = _as_payload(response)
    data = payload.get("data")
    reason = data.get("reason") if isinstance(data, dict) else None
    code = payload.get("code")
    return code == "unity_session_disconnected" or reason == "unity_session_disconnected"


async def _read_editor_state(ctx: Context, unity_instance: str | None) -> dict[str, Any] | None:
    try:
        response = await editor_state.get_editor_state_for_instance(ctx, unity_instance)
    except Exception:
        return None
    return _state_data(response)


async def _wait_for_refresh_completion(
    ctx: Context,
    unity_instance: str | None,
    baseline_reload_after_ms: int | None,
    *,
    require_reload_evidence: bool,
) -> dict[str, Any]:
    deadline = time.monotonic() + RELOAD_RECOVERY_TIMEOUT_SECONDS
    reload_observed = False
    busy_observed = False

    while True:
        data = await _read_editor_state(ctx, unity_instance)
        if data is not None:
            compilation = data.get("compilation")
            if isinstance(compilation, dict):
                busy_observed = busy_observed or bool(
                    compilation.get("is_compiling")
                    or compilation.get("is_domain_reload_pending")
                )

            current_reload_after_ms = _domain_reload_after_ms(data)
            reload_observed = reload_observed or (
                current_reload_after_ms is not None
                and current_reload_after_ms != baseline_reload_after_ms
            )

            evidence_satisfied = (
                not require_reload_evidence
                or reload_observed
                or busy_observed
            )
            if _is_ready(data) and evidence_satisfied:
                return {
                    "completed": True,
                    "domain_reload_observed": reload_observed,
                    "busy_observed": busy_observed,
                    "current_domain_reload_after_unix_ms": current_reload_after_ms,
                }

        if time.monotonic() >= deadline:
            return {
                "completed": False,
                "domain_reload_observed": reload_observed,
                "busy_observed": busy_observed,
                "current_domain_reload_after_unix_ms": None,
            }

        await asyncio.sleep(RELOAD_RECOVERY_POLL_SECONDS)


def _recovery_timeout_response(
    *,
    compile_requested: bool,
    recovery: dict[str, Any],
) -> MCPResponse:
    return MCPResponse(
        success=False,
        error="Unity refresh was dispatched, but the new session did not become ready before the recovery timeout",
        code="refresh_recovery_timeout",
        category="session",
        severity="warning",
        retryable=False,
        hint="check_editor_state",
        data={
            "reason": "refresh_recovery_timeout",
            "compile_requested": compile_requested,
            "refresh_command_dispatched": True,
            "domain_reload_observed": recovery.get("domain_reload_observed", False),
            "safe_to_retry_refresh": False,
            "recommended_next_action": "poll_editor_state",
        },
    )


@mcp_for_unity_tool(
    description="Request a Unity asset database refresh and optionally a script compilation. Can optionally wait for readiness.",
    annotations=ToolAnnotations(
        title="Refresh Unity",
        destructiveHint=True,
    ),
)
async def refresh_unity(
    ctx: Context,
    mode: Annotated[Literal["if_dirty", "force"], "Refresh mode"] = "if_dirty",
    scope: Annotated[Literal["assets", "scripts", "all"],
                     "Refresh scope"] = "all",
    compile: Annotated[Literal["none", "request"],
                       "Whether to request compilation"] = "none",
    wait_for_ready: Annotated[bool,
                              "If true, wait until editor_state.advice.ready_for_tools is true"] = True,
    unity_instance: Annotated[str,
                              "Optional target Unity instance Name@hash. Overrides the session active instance for this call."] | None = None,
) -> MCPResponse | dict[str, Any]:
    unity_instance = choose_unity_instance(ctx, unity_instance)
    compile_requested = compile == "request"

    # 记录刷新前的 Domain Reload 时间。旧 session 在编译中断开后，使用该值
    # 判断新 session 是否确实完成了本次重载，避免重复发送有副作用的刷新命令。
    baseline_state = await _read_editor_state(ctx, unity_instance)
    baseline_reload_after_ms = _domain_reload_after_ms(baseline_state)

    params: dict[str, Any] = {
        "mode": mode,
        "scope": scope,
        "compile": compile,
        "wait_for_ready": bool(wait_for_ready),
    }

    response = await unity_transport.send_with_unity_instance(
        async_send_command_with_retry,
        unity_instance,
        "refresh_unity",
        params,
    )

    response_payload = _as_payload(response)
    response_succeeded = response_payload.get("success", True)

    if not response_succeeded:
        if not (compile_requested and _is_session_disconnect(response)):
            return MCPResponse(**response_payload) if response_payload else response

        # RequestScriptCompilation 会销毁承载本次 command_result 的旧 WebSocket。
        # 此处只等待新 session，绝不重发 refresh_unity，否则会形成无限重载循环。
        recovery = await _wait_for_refresh_completion(
            ctx,
            unity_instance,
            baseline_reload_after_ms,
            require_reload_evidence=True,
        )
        if not recovery["completed"]:
            return _recovery_timeout_response(
                compile_requested=True,
                recovery=recovery,
            )

        response = MCPResponse(
            success=True,
            message="Unity refresh completed after the editor session reconnected.",
            data={
                "refresh_triggered": scope != "scripts",
                "compile_requested": True,
                "resulting_state": "idle",
                "domain_reload_observed": recovery["domain_reload_observed"],
                "recovered_after_session_disconnect": True,
                "safe_to_retry_refresh": False,
            },
        )

    elif wait_for_ready:
        recovery = await _wait_for_refresh_completion(
            ctx,
            unity_instance,
            baseline_reload_after_ms,
            require_reload_evidence=compile_requested,
        )
        if not recovery["completed"]:
            return _recovery_timeout_response(
                compile_requested=compile_requested,
                recovery=recovery,
            )

    # After readiness is restored, clear any external-dirty flag for this instance so future tools can proceed cleanly.
    try:
        inst = unity_instance or await editor_state.infer_single_instance_id(ctx)
        if inst:
            external_changes_scanner.clear_dirty(inst)
    except Exception:
        pass

    return MCPResponse(**response) if isinstance(response, dict) else response
