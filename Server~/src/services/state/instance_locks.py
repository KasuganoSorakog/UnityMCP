from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator


@dataclass
class _Lease:
    instance: str
    owner_key: str
    owner: str
    ttl_seconds: float
    expires_monotonic: float
    active_calls: int = 0
    updated_at: str = ""


class InstanceBusyError(RuntimeError):
    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload["error"])
        self.payload = payload


class InstanceWriteLocks:
    """Coordinate edits between MCP callers, separately from the command queue."""

    def __init__(self) -> None:
        self._leases: dict[str, _Lease] = {}
        self._guard = asyncio.Lock()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _expiry(lease: _Lease) -> str:
        return datetime.fromtimestamp(
            time.time() + max(0.0, lease.expires_monotonic - time.monotonic()),
            timezone.utc,
        ).isoformat()

    def _public(self, project_hash: str, lease: _Lease) -> dict[str, Any]:
        return {
            "project_hash": project_hash,
            "instance": lease.instance,
            "owner": lease.owner,
            "expires_at": self._expiry(lease),
            "ttl_seconds": lease.ttl_seconds,
            "active_calls": lease.active_calls,
            "updated_at": lease.updated_at,
        }

    def _expired(self, lease: _Lease) -> bool:
        return lease.active_calls == 0 and lease.expires_monotonic <= time.monotonic()

    def _conflict(self, project_hash: str, lease: _Lease) -> dict[str, Any]:
        return {
            "success": False,
            "code": "unity_instance_busy",
            "category": "busy",
            "error": f"Unity instance '{lease.instance}' is being edited by '{lease.owner}'.",
            "message": "Another MCP session holds this project's write lease. Read-only tools remain available.",
            "retryable": True,
            "retry_after_ms": 2000,
            "hint": "retry",
            "data": {
                "reason": "unity_instance_busy",
                "lock": self._public(project_hash, lease),
                "retryable": True,
            },
        }

    async def claim(self, project_hash: str, instance: str, owner_key: str, owner: str, ttl_seconds: float) -> dict[str, Any]:
        async with self._guard:
            lease = self._leases.get(project_hash)
            if lease is not None and self._expired(lease):
                del self._leases[project_hash]
                lease = None
            if lease is not None and lease.owner_key != owner_key:
                return self._conflict(project_hash, lease)
            now = time.monotonic()
            if lease is None:
                lease = _Lease(instance, owner_key, owner, ttl_seconds, now + ttl_seconds)
                self._leases[project_hash] = lease
            else:
                lease.instance = instance
                lease.owner = owner
                lease.ttl_seconds = ttl_seconds
                lease.expires_monotonic = max(lease.expires_monotonic, now + ttl_seconds)
            lease.updated_at = self._now()
            return {"success": True, "data": {"lock": self._public(project_hash, lease)}}

    async def release(self, project_hash: str, owner_key: str) -> dict[str, Any]:
        async with self._guard:
            lease = self._leases.get(project_hash)
            if lease is None or self._expired(lease):
                self._leases.pop(project_hash, None)
                return {"success": True, "data": {"released": False}}
            if lease.owner_key != owner_key:
                return self._conflict(project_hash, lease)
            if lease.active_calls:
                return {
                    "success": False, "code": "unity_instance_busy",
                    "error": "A write operation is still running; release it after the operation finishes.",
                    "retryable": True, "data": {"lock": self._public(project_hash, lease)},
                }
            del self._leases[project_hash]
            return {"success": True, "data": {"released": True}}

    @asynccontextmanager
    async def hold(self, project_hash: str, instance: str, owner_key: str) -> AsyncIterator[None]:
        lease: _Lease | None = None
        conflict: dict[str, Any] | None = None
        async with self._guard:
            candidate = self._leases.get(project_hash)
            if candidate is not None and self._expired(candidate):
                del self._leases[project_hash]
                candidate = None
            if candidate is not None and candidate.owner_key != owner_key:
                conflict = self._conflict(project_hash, candidate)
            else:
                lease = candidate
                if lease is None:
                    lease = _Lease(instance, owner_key, owner_key, 300.0, time.monotonic() + 300.0)
                    self._leases[project_hash] = lease
                lease.active_calls += 1
                lease.expires_monotonic = max(lease.expires_monotonic, time.monotonic() + lease.ttl_seconds)
        if conflict is not None:
            raise InstanceBusyError(conflict)
        try:
            yield
        finally:
            async with self._guard:
                lease.active_calls = max(0, lease.active_calls - 1)
                lease.expires_monotonic = time.monotonic() + lease.ttl_seconds

    async def status(self, project_hash: str | None = None) -> dict[str, Any]:
        async with self._guard:
            for key, lease in list(self._leases.items()):
                if self._expired(lease):
                    del self._leases[key]
            leases = self._leases if project_hash is None else {
                key: value for key, value in self._leases.items() if key == project_hash
            }
            return {key: self._public(key, value) for key, value in leases.items()}


_locks = InstanceWriteLocks()


def get_instance_write_locks() -> InstanceWriteLocks:
    return _locks


def is_mutating_tool(name: str, arguments: Any) -> bool:
    if name in {
        "health", "claim_instance", "instances", "set_active_instance", "debug_request_context",
        "find_gameobjects", "find_in_file", "validate_script", "get_sha",
        "manage_script_capabilities", "get_test_job", "get_registered_tools",
    }:
        return False
    if not isinstance(arguments, dict):
        return True
    action = str(arguments.get("action") or "").lower()
    if name == "read_console":
        return action == "clear"
    if name == "manage_script":
        return action != "read"
    if name == "manage_editor":
        return action not in {"telemetry_status", "telemetry_ping", "operation_status"}
    if name == "manage_scene":
        return action not in {"get_active", "get_hierarchy", "get_build_settings", "screenshot"}
    if name == "manage_asset":
        return action not in {"search", "get_info", "get_components"}
    if name == "manage_material":
        return action not in {"ping", "get_material_info"}
    if name == "manage_vfx":
        return not (action.endswith("_get_info") or action.startswith("vfx_list_"))
    if name == "capture_view":
        return action not in {"", "capture", "list_presets"}
    if name == "sample_runtime":
        return action in {"", "sample", "cancel"}
    if name == "batch_execute":
        commands = arguments.get("commands")
        return not isinstance(commands, list) or any(
            not isinstance(command, dict)
            or is_mutating_tool(str(command.get("tool") or ""), command.get("params") or {})
            for command in commands
        )
    return True


async def resolve_lock_instance(instance: str | None) -> tuple[str, str]:
    from transport.plugin_hub import PluginHub

    session_id = await PluginHub._resolve_session_id(instance)
    project_hash, label = await PluginHub._describe_instance(session_id, instance)
    if not project_hash or not label:
        raise ValueError("A connected Unity project_hash is required for a session write lease.")
    return project_hash, label


__all__ = [
    "InstanceBusyError", "InstanceWriteLocks", "get_instance_write_locks",
    "is_mutating_tool", "resolve_lock_instance",
]
