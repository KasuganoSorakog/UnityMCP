from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Annotated, Any

from fastmcp import Context
from mcp.types import ToolAnnotations

from core.telemetry import get_package_version
from services.registry import get_registered_tools, mcp_for_unity_tool
from services.state.instance_locks import get_instance_write_locks
from transport.legacy.unity_connection import async_send_command_with_retry
from transport.plugin_hub import PluginHub
from transport.unity_transport import send_with_unity_instance


async def _editor_state(instance: str) -> dict[str, Any]:
    response = await send_with_unity_instance(
        async_send_command_with_retry, instance, "get_editor_state", {"timeout_seconds": 3},
    )
    if not isinstance(response, dict) or not response.get("success"):
        return {"available": False, "error_code": response.get("code") if isinstance(response, dict) else "invalid_response"}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    compilation = data.get("compilation") or {}
    editor = data.get("editor") or {}
    return {
        "available": True,
        "observed_at_unix_ms": data.get("observed_at_unix_ms"),
        "compilation": compilation,
        "play_mode": editor.get("play_mode"),
        "active_scene": editor.get("active_scene"),
    }


@mcp_for_unity_tool(
    description=(
        "Read-only Unity/server health: versions, connected and recently disconnected projects, "
        "capabilities, registered tools, editor/Play state, command queues, write leases and disconnect history. "
        "Does not change the selected instance and does not require Unity to be reachable."
    ),
    annotations=ToolAnnotations(title="Unity MCP Health", readOnlyHint=True),
)
async def health(
    ctx: Context,
    include_editor_state: Annotated[bool, "Also query each connected editor with a short bounded timeout; default true."] = True,
) -> dict[str, Any]:
    server_version = get_package_version()
    diagnostics = await PluginHub.get_diagnostics(server_version=server_version)
    registry = PluginHub._registry
    online = await registry.list_sessions() if registry is not None else {}
    history = await registry.list_disconnected() if registry is not None else {}
    locks = await get_instance_write_locks().status()
    hashes = sorted({session.project_hash for session in online.values()} | set(history) | set(locks))
    states: dict[str, dict[str, Any]] = {}
    if include_editor_state:
        async def read(hash_value: str) -> tuple[str, dict[str, Any]]:
            session = next((s for s in online.values() if s.project_hash == hash_value), None)
            if session is None:
                return hash_value, {"available": False, "error_code": "unity_session_disconnected"}
            return hash_value, await _editor_state(f"{session.project_name}@{session.project_hash}")
        results = await asyncio.gather(*(read(value) for value in hashes))
        states = dict(results)
    instances = []
    now = datetime.now(timezone.utc)
    for hash_value in hashes:
        session = next((s for s in online.values() if s.project_hash == hash_value), None)
        saved = history.get(hash_value)
        current = session if session is not None else saved
        if current is None:
            instances.append({"project_hash": hash_value, "status": "disconnected", "write_lock": locks.get(hash_value)})
            continue
        age = (now - current.last_seen).total_seconds() if session is not None else None
        status = "disconnected" if session is None else "stale" if age is not None and age > PluginHub._stale_threshold_seconds() else "connected"
        package_version = current.package_version
        tools = sorted(current.tools)
        instances.append({
            "instance": f"{current.project_name}@{hash_value}",
            "project_hash": hash_value,
            "project_path": current.project_path,
            "unity_version": current.unity_version,
            "package_version": package_version,
            "server_version": server_version,
            "version_match": package_version == server_version if package_version else None,
            "status": status,
            "last_seen": current.last_seen.isoformat(),
            "last_seen_age_seconds": round(age, 3) if age is not None else None,
            "connected_at": current.connected_at.isoformat(),
            "last_session_id": current.session_id,
            "last_disconnected_at": current.last_disconnected_at.isoformat() if current.last_disconnected_at else None,
            "last_disconnect_reason": current.last_disconnect_reason,
            "tools_available": tools,
            "capabilities_version": current.capabilities_version,
            "editor": states.get(hash_value),
            "write_lock": locks.get(hash_value),
            "connection_history": await registry.connection_events(hash_value) if registry is not None and registry.has_connection_journal else {
                "persistent": False, "path": None, "events": [], "error": None,
            },
        })
    tools = sorted(tool["name"] for tool in get_registered_tools())
    return {
        "success": True,
        "status": diagnostics.get("status", "unknown"),
        "data": {
            "server": {**diagnostics.get("server", {}), "version": server_version},
            "instances": instances,
            "tools": tools,
            "capabilities": {
                "capture_view": {"angles": True, "sequence": True, "grid": True, "overlay": True, "presets": True},
                "sample_runtime": {"per_frame": True, "fields": True, "partial_results": True},
                "play_simulation": True,
                "session_write_leases": True,
                "disconnect_history": True,
                "persistent_connection_journal": registry.has_connection_journal if registry is not None else False,
            },
            "write_locks": locks,
            "scheduler": diagnostics.get("scheduler", {}),
            "warnings": diagnostics.get("warnings", []),
        },
    }
