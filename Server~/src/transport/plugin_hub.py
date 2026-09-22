"""WebSocket hub for Unity plugin communication."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.endpoints import WebSocketEndpoint
from starlette.websockets import WebSocket

from models.models import MCPResponse
from transport.plugin_registry import PluginRegistry
from transport.instance_scheduler import (
    InstanceCommandScheduler,
    InstanceQueueTimeoutError,
)
from transport.models import (
    WelcomeMessage,
    RegisteredMessage,
    ExecuteCommandMessage,
    CancelCommandMessage,
    RegisterMessage,
    RegisterToolsMessage,
    PongMessage,
    CommandResultMessage,
    SessionList,
    SessionDetails,
)

logger = logging.getLogger("mcp-for-unity-server")


def classified_error_response(
    *,
    code: str,
    category: str,
    error: str,
    severity: str = "warning",
    retryable: bool = False,
    retry_after_ms: int | None = None,
    hint: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a backward-compatible MCP error response with machine-readable diagnostics."""

    payload = dict(data or {})
    payload.setdefault("reason", code)
    if retry_after_ms is not None:
        payload.setdefault("retry_after_ms", retry_after_ms)

    return MCPResponse(
        success=False,
        error=error,
        hint=hint,
        code=code,
        category=category,
        severity=severity,
        retryable=retryable,
        retry_after_ms=retry_after_ms,
        data=payload or None,
    ).model_dump()


class PluginDisconnectedError(RuntimeError):
    """Raised when a plugin WebSocket disconnects while commands are in flight.

    Carries the same machine-readable classification contract as
    ``classified_error_response`` so that raise-style paths (e.g. the instance
    middleware guard, where returning a response dict is not possible) expose
    the same fields as response-style paths (``send_command``).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        category: str | None = None,
        retryable: bool | None = None,
        retry_after_ms: int | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.hint = hint


class NoUnitySessionError(RuntimeError):
    """Raised when no Unity plugins are available."""


class PluginHub(WebSocketEndpoint):
    """Manages persistent WebSocket connections to Unity plugins."""

    encoding = "json"
    KEEP_ALIVE_INTERVAL = 15
    SERVER_TIMEOUT = 30
    COMMAND_TIMEOUT = 30
    # Timeout (seconds) for fast-fail commands like ping/get_editor_state.
    # Keep short so MCP clients aren't blocked during Unity compilation/reload/unfocused throttling.
    FAST_FAIL_TIMEOUT = 2.0
    # Console reads must run on the Unity main thread and can be slow when the
    # Editor console contains many long entries. Keep a bounded but realistic
    # default instead of the 2s fast-fail budget used by pings/editor-state.
    READ_CONSOLE_TIMEOUT = 15.0
    # Maximum time a command waits to enter a busy Unity instance queue.
    # Cross-project calls use different queues and are not affected by this.
    INSTANCE_QUEUE_TIMEOUT = 25.0
    FAST_QUEUE_TIMEOUT = 2.0
    READ_CONSOLE_QUEUE_TIMEOUT = 5.0
    # Sessions with no inbound traffic for this long are evicted by the sweeper.
    # Default = 3x the client keepalive interval; the client sends pongs from a
    # background thread, so only a frozen/killed/disconnected editor goes stale.
    # Overridable via UNITY_MCP_SESSION_STALE_SECONDS (clamped to >= 20s).
    SESSION_STALE_SECONDS = 45.0
    SESSION_SWEEP_INTERVAL_SECONDS = 10.0
    # Close codes used when the server terminates a plugin connection.
    CLOSE_CODE_STALE_SESSION = 4408
    CLOSE_CODE_SESSION_SUPERSEDED = 4409
    # Grace period to wait for the plugin's late acknowledgement after a
    # timeout cancel, distinguishing "never started" (safe to retry) from
    # "already running" (execution state unknown). Overridable via
    # UNITY_MCP_CANCEL_ACK_GRACE_SECONDS.
    CANCEL_ACK_GRACE_SECONDS = 2.0
    # Upper bound for sending the RegisteredMessage while holding the per-hash
    # register lock: a stuck/half-open socket must not stall re-registration of
    # the same project for long. Must stay well below the Unity client's 10s
    # RegistrationTimeout so that (a) the client still has round-trip headroom
    # to receive the ack before giving up, and (b) a stuck first registration
    # cannot push a queued re-registration of the same hash past the client
    # timeout.
    REGISTER_ACK_SEND_TIMEOUT_SECONDS = 5.0
    # Fast-path commands should never block the client for long; return a retry hint instead.
    # This helps avoid the Cursor-side ~30s tool-call timeout when Unity is compiling/reloading
    # or is throttled while unfocused.
    _FAST_FAIL_COMMANDS: set[str] = {
        "get_editor_state", "ping"}

    _registry: PluginRegistry | None = None
    _connections: dict[str, WebSocket] = {}
    # command_id -> {"future": Future, "session_id": str}
    _pending: dict[str, dict[str, Any]] = {}
    _lock: asyncio.Lock | None = None
    # Per-project-hash locks serializing _handle_register: concurrent
    # re-registrations of the same hash cannot interleave register/
    # connection-insert and both believe they own the hash. Sharded by hash so
    # a stuck registration for one project never blocks other projects'
    # editors from registering.
    _register_locks: dict[str, asyncio.Lock] = {}
    # Guards get/create/prune of _register_locks so one hash never ends up
    # with two live lock objects.
    _register_locks_guard: asyncio.Lock | None = None
    # Session ids whose supersede eviction is in flight: the replacement
    # session already owns the project hash, but the old socket has not been
    # notified/closed yet. The orphan sweep must skip these so the old client
    # receives its session_superseded notification instead of a bare 4408
    # close — without the notification it would auto-reconnect and the two
    # editors would keep superseding each other (ping-pong).
    _eviction_in_progress: set[str] = set()
    _loop: asyncio.AbstractEventLoop | None = None
    _scheduler: InstanceCommandScheduler | None = None
    _sweeper_task: asyncio.Task | None = None

    @classmethod
    def configure(
        cls,
        registry: PluginRegistry,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        cls._registry = registry
        cls._loop = loop or asyncio.get_running_loop()
        # Ensure coordination primitives are bound to the configured loop.
        # Reconfiguration swaps the registry as well, so in-flight work cannot
        # rely on old locks/marks surviving; recreating them here (and only
        # here) keeps mutual exclusion intact.
        cls._lock = asyncio.Lock()
        cls._register_locks = {}
        cls._register_locks_guard = asyncio.Lock()
        cls._eviction_in_progress = set()
        cls._scheduler = InstanceCommandScheduler()

    @classmethod
    def is_configured(cls) -> bool:
        return (
            cls._registry is not None
            and cls._lock is not None
            and cls._register_locks_guard is not None
            and cls._scheduler is not None
        )

    @classmethod
    async def _register_lock_for(cls, project_hash: str) -> asyncio.Lock:
        """Return the per-hash registration lock, creating it under the guard.

        Callers must enter the returned lock without any intervening await so
        the sweeper's lock pruning cannot drop it while it is about to be
        acquired.
        """
        guard = cls._register_locks_guard
        if guard is None:
            raise RuntimeError("PluginHub not configured")
        async with guard:
            lock = cls._register_locks.get(project_hash)
            if lock is None:
                lock = asyncio.Lock()
                cls._register_locks[project_hash] = lock
            return lock

    async def on_connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        msg = WelcomeMessage(
            serverTimeout=self.SERVER_TIMEOUT,
            keepAliveInterval=self.KEEP_ALIVE_INTERVAL,
        )
        await websocket.send_json(msg.model_dump())

    async def on_receive(self, websocket: WebSocket, data: Any) -> None:
        if not isinstance(data, dict):
            logger.warning(f"Received non-object payload from plugin: {data}")
            return

        message_type = data.get("type")
        try:
            if message_type == "register":
                await self._handle_register(websocket, RegisterMessage(**data))
            elif message_type == "register_tools":
                await self._handle_register_tools(websocket, RegisterToolsMessage(**data))
            elif message_type == "pong":
                await self._handle_pong(websocket, PongMessage(**data))
            elif message_type == "command_result":
                await self._handle_command_result(websocket, CommandResultMessage(**data))
            else:
                logger.debug(f"Ignoring plugin message: {data}")
            # Liveness fallback: any inbound message proves the plugin is alive,
            # even from older clients whose pong carries no session_id.
            await self._touch_session_for_websocket(websocket)
        except Exception as e:
            logger.error(f"Error handling message type {message_type}: {e}")

    @classmethod
    async def _session_id_for_websocket(cls, websocket: WebSocket) -> str | None:
        """Reverse-lookup the session id owning a websocket connection."""
        lock = cls._lock
        if lock is None:
            return None
        async with lock:
            return next(
                (sid for sid, ws in cls._connections.items() if ws is websocket), None)

    async def _touch_session_for_websocket(self, websocket: WebSocket) -> None:
        cls = type(self)
        registry = cls._registry
        if registry is None:
            return
        session_id = await cls._session_id_for_websocket(websocket)
        if session_id:
            await registry.touch(session_id)

    async def on_disconnect(self, websocket: WebSocket, close_code: int) -> None:
        cls = type(self)
        lock = cls._lock
        if lock is None:
            return
        async with lock:
            session_id = next(
                (sid for sid, ws in cls._connections.items() if ws is websocket), None)
        if session_id:
            await cls._cleanup_session_locked(session_id)
            logger.info(
                f"Plugin session {session_id} disconnected ({close_code})")

    @classmethod
    async def _cleanup_session_locked(cls, session_id: str) -> None:
        """Idempotently tear down a session's connection, in-flight commands,
        registry entry and scheduler state.

        Acquires ``cls._lock`` internally; callers must NOT hold the lock.
        Safe to invoke multiple times for the same session.
        """
        lock = cls._lock
        if lock is None:
            return
        async with lock:
            cls._connections.pop(session_id, None)
            # Fail-fast any in-flight commands for this session to avoid waiting
            # for COMMAND_TIMEOUT. Entries are popped here (not just failed) so a
            # send_command whose finally-block is cancelled twice cannot leak them.
            pending_ids = [
                command_id
                for command_id, entry in cls._pending.items()
                if entry.get("session_id") == session_id
            ]
            for command_id in pending_ids:
                entry = cls._pending.pop(command_id, None)
                future = entry.get("future") if isinstance(
                    entry, dict) else None
                if future and not future.done():
                    future.set_exception(
                        PluginDisconnectedError(
                            f"Unity plugin session {session_id} disconnected while awaiting command_result"
                        )
                    )
        if cls._registry:
            await cls._registry.unregister(session_id)
        if cls._scheduler:
            await cls._scheduler.forget_session(session_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @classmethod
    async def send_command(cls, session_id: str, command_type: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            websocket = await cls._get_connection(session_id)
        except PluginDisconnectedError as exc:
            return classified_error_response(
                code="unity_session_disconnected",
                category="session",
                error=f"{exc}; Unity session is reconnecting",
                retryable=True,
                retry_after_ms=2000,
                hint="retry",
            )
        command_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        # Compute a per-command timeout:
        # - fast-path commands: short timeout (encourage retry)
        # - long-running commands: allow caller to request a longer timeout via params
        unity_timeout_s = float(cls.COMMAND_TIMEOUT)
        server_wait_s = float(cls.COMMAND_TIMEOUT)
        requested = None
        try:
            if isinstance(params, dict):
                requested = params.get("timeout_seconds", None)
                if requested is None:
                    requested = params.get("timeoutSeconds", None)
        except Exception:
            requested = None

        requested_s: float | None = None
        if requested is not None:
            try:
                requested_s = float(requested)
                # Clamp to a sane upper bound to avoid accidental infinite hangs.
                requested_s = max(1.0, min(requested_s, 60.0 * 60.0))
            except Exception:
                requested_s = None

        if command_type in cls._FAST_FAIL_COMMANDS:
            fast_timeout = float(cls.FAST_FAIL_TIMEOUT)
            unity_timeout_s = fast_timeout
            server_wait_s = fast_timeout
        elif command_type == "read_console":
            console_timeout = requested_s or float(cls.READ_CONSOLE_TIMEOUT)
            unity_timeout_s = console_timeout
            server_wait_s = console_timeout + 5.0
        else:
            if requested_s is not None:
                unity_timeout_s = max(unity_timeout_s, requested_s)
                # Give the server a small cushion beyond the Unity-side timeout to account for transport overhead.
                server_wait_s = max(server_wait_s, requested_s + 5.0)

        lock = cls._lock
        if lock is None:
            raise RuntimeError("PluginHub not configured")

        async with lock:
            if command_id in cls._pending:
                raise RuntimeError(
                    f"Duplicate command id generated: {command_id}")
            cls._pending[command_id] = {
                "future": future, "session_id": session_id}

        try:
            msg = ExecuteCommandMessage(
                id=command_id,
                name=command_type,
                params=params,
                timeout=unity_timeout_s,
            )
            try:
                await websocket.send_json(msg.model_dump())
            except Exception as exc:
                # If send fails (socket already closing), fail the future so callers don't hang.
                if not future.done():
                    future.set_exception(exc)
                return classified_error_response(
                    code="unity_session_disconnected",
                    category="session",
                    error=(
                        f"Failed to send '{command_type}' to Unity ({exc}); "
                        "session may be reconnecting"
                    ),
                    retryable=True,
                    retry_after_ms=2000,
                    hint="retry",
                )
            try:
                # Shield the future so that after the first timeout it stays
                # alive and can still receive the plugin's late acknowledgement
                # (cancel result) during the grace period below.
                result = await asyncio.wait_for(
                    asyncio.shield(future), timeout=server_wait_s)
                return result
            except PluginDisconnectedError as exc:
                return classified_error_response(
                    code="unity_session_disconnected",
                    category="session",
                    error=f"{exc}; Unity session is reconnecting",
                    retryable=True,
                    retry_after_ms=2000,
                    hint="retry",
                )
            except asyncio.TimeoutError:
                # The server gives up waiting, but Unity may still execute the
                # command; ask the plugin to cancel it so AI retries don't
                # duplicate effects.
                await cls._send_cancel_best_effort(websocket, command_id)
                if command_type in cls._FAST_FAIL_COMMANDS:
                    return classified_error_response(
                        code="unity_fast_command_timeout",
                        category="timeout",
                        error=f"Unity did not respond to '{command_type}' within {server_wait_s:.1f}s",
                        data={"command": command_type, "timeout_seconds": server_wait_s},
                    )
                if command_type == "read_console":
                    return classified_error_response(
                        code="unity_console_timeout",
                        category="timeout",
                        error=(
                            f"Unity did not respond to 'read_console' within {server_wait_s:.1f}s; "
                            "the Console may be busy. Use a smaller count/page_size or clear noisy logs."
                        ),
                        data={"command": command_type, "timeout_seconds": server_wait_s},
                    )
                # Generic commands: wait a short grace period for the plugin's
                # late acknowledgement to distinguish "cancelled before
                # execution" (safe to retry) from "already running".
                grace_s = cls._read_timeout_env(
                    "UNITY_MCP_CANCEL_ACK_GRACE_SECONDS",
                    cls.CANCEL_ACK_GRACE_SECONDS,
                )
                late: Any = None
                if grace_s:
                    try:
                        late = await asyncio.wait_for(future, timeout=grace_s)
                    except Exception:
                        # Grace timeout, disconnect, or any late failure all
                        # leave the execution state unknown.
                        late = None
                if isinstance(late, dict):
                    late_code = late.get("code")
                    if late_code == "cancelled_before_execution":
                        # The plugin confirmed the command never started.
                        return classified_error_response(
                            code="unity_command_timeout",
                            category="timeout",
                            error=(
                                f"Unity 未在 {server_wait_s:.1f}s 内响应 '{command_type}'；"
                                "命令未开始执行，可安全重试"
                            ),
                            retryable=True,
                            retry_after_ms=1000,
                            hint="retry",
                            data={"command": command_type, "timeout_seconds": server_wait_s},
                        )
                    if late_code != "execution_state_unknown":
                        # A genuine late result arrived during the grace
                        # period; return it as-is instead of retrying blindly.
                        return late
                return classified_error_response(
                    code="unity_execution_state_unknown",
                    category="timeout",
                    error=(
                        f"Unity 未在 {server_wait_s:.1f}s 内响应 '{command_type}'；"
                        "命令可能已在 Unity 主线程开始执行（或编辑器无应答），"
                        "可能已部分生效，重试前请先验证场景状态"
                    ),
                    retryable=True,
                    hint="verify_state_before_retry",
                    data={"command": command_type, "timeout_seconds": server_wait_s},
                )
        finally:
            async with lock:
                cls._pending.pop(command_id, None)

    @classmethod
    async def get_sessions(cls) -> SessionList:
        if cls._registry is None:
            return SessionList(sessions={})
        sessions = await cls._registry.list_sessions()
        return SessionList(
            sessions={
                session_id: SessionDetails(
                    project=session.project_name,
                    hash=session.project_hash,
                    unity_version=session.unity_version,
                    connected_at=session.connected_at.isoformat(),
                    project_path=session.project_path,
                    package_version=session.package_version,
                    current_scene=session.current_scene,
                    capabilities_version=session.capabilities_version,
                )
                for session_id, session in sessions.items()
            }
        )

    @classmethod
    async def get_diagnostics(cls, server_version: str | None = None) -> dict[str, Any]:
        """Return central-server diagnostics for editor UI and MCP clients."""

        sessions_data = await cls.get_sessions()
        sessions = sessions_data.sessions
        package_versions = sorted({
            session.package_version or "unknown"
            for session in sessions.values()
        })
        capabilities_versions = sorted({
            session.capabilities_version or "unknown"
            for session in sessions.values()
        })

        warnings: list[dict[str, Any]] = []
        if len(package_versions) > 1:
            warnings.append({
                "code": "package_version_mismatch",
                "severity": "warning",
                "message": "已连接 Unity 项目的 MCP 包版本不一致，建议统一后再进行多项目操作。",
                "versions": package_versions,
            })
        if len(capabilities_versions) > 1:
            warnings.append({
                "code": "capabilities_version_mismatch",
                "severity": "warning",
                "message": "已连接 Unity 项目的能力协议版本不一致，部分工具可能行为不同。",
                "versions": capabilities_versions,
            })

        if server_version:
            server_base_version = cls._base_version(server_version)
            mismatched_sessions = [
                f"{session.project}@{session.hash}"
                for session in sessions.values()
                if session.package_version
                and cls._base_version(session.package_version) != server_base_version
            ]
            if mismatched_sessions:
                warnings.append({
                    "code": "server_package_version_mismatch",
                    "severity": "warning",
                    "message": "中心 Server 版本与部分 Unity 项目的 MCP 包版本不一致，建议统一后重启中心 Server。",
                    "server_version": server_version,
                    "instances": mismatched_sessions,
                })

        session_payload: dict[str, Any] = {}
        scheduler_snapshot = await cls.get_scheduler_snapshot()
        for session_id, session in sessions.items():
            tools = await cls.get_tools_for_project(session.hash)
            session_payload[session_id] = {
                **session.model_dump(),
                "instance": f"{session.project}@{session.hash}",
                "tool_count": len(tools),
                "scheduler": scheduler_snapshot.get(session.hash),
            }

        busy_instances = [
            instance_key
            for instance_key, state in scheduler_snapshot.items()
            if state.get("running") or state.get("queue_depth", 0) > 0
        ]
        active_hashes = {session.hash for session in sessions.values()}
        summary = cls._build_diagnostics_summary(
            session_count=len(sessions),
            warnings=warnings,
            scheduler_snapshot=scheduler_snapshot,
            busy_instance_count=len(busy_instances),
            active_hashes=active_hashes,
        )

        return {
            "status": "ok" if not warnings else "warning",
            "summary": summary,
            "server": {
                "transport": "http",
                "version": server_version or "unknown",
                "session_count": len(sessions),
                "package_versions": package_versions,
                "capabilities_versions": capabilities_versions,
            },
            "scheduler": {
                "policy": "parallel_across_unity_instances_serial_per_instance",
                "busy_instance_count": len(busy_instances),
                "queued_command_count": sum(
                    int(state.get("queue_depth") or 0)
                    for state in scheduler_snapshot.values()
                ),
                "instances": {
                    instance_key: {
                        **state,
                        "active": instance_key in active_hashes,
                    }
                    for instance_key, state in scheduler_snapshot.items()
                },
            },
            "sessions": session_payload,
            "warnings": warnings,
        }

    @classmethod
    def _build_diagnostics_summary(
        cls,
        *,
        session_count: int,
        warnings: list[dict[str, Any]],
        scheduler_snapshot: dict[str, dict[str, Any]],
        busy_instance_count: int,
        active_hashes: set[str] | None = None,
    ) -> dict[str, Any]:
        active_hashes = active_hashes or set()
        action_items: list[str] = []
        if session_count == 0:
            action_items.append("没有 Unity 项目连接到中心 Server。请确认目标项目已打开，并在 MCP For Unity 面板启动/连接 Session。")
        if warnings:
            action_items.append("存在版本或协议不一致，建议统一各项目的 MCP 包和 Server 后重启中心 Server。")
        if busy_instance_count > 0:
            action_items.append("有 Unity 实例正在执行或排队，请稍后重试，或查看 scheduler.instances 中的 current_command。")

        stale_instances = [
            state.get("instance") or instance_key
            for instance_key, state in scheduler_snapshot.items()
            if instance_key not in active_hashes
        ]
        if stale_instances:
            action_items.append("存在历史调度状态但对应 Unity 已断开；通常可忽略，重连后会自动更新。")

        if not action_items:
            action_items.append("中心 Server 正常；不同 Unity 项目可并行处理，同一项目会自动排队保护 Unity 主线程。")

        return {
            "zh_status": "正常" if not warnings and session_count > 0 else ("未连接" if session_count == 0 else "需要注意"),
            "session_count": session_count,
            "busy_instance_count": busy_instance_count,
            "queued_command_count": sum(
                int(state.get("queue_depth") or 0)
                for state in scheduler_snapshot.values()
            ),
            "parallel_model": "跨 Unity 项目并行；同一 Unity 项目串行排队。",
            "action_items": action_items,
        }

    @classmethod
    async def get_scheduler_snapshot(cls) -> dict[str, dict[str, Any]]:
        if cls._scheduler is None:
            return {}
        return await cls._scheduler.snapshot()

    @staticmethod
    def _base_version(version: str) -> str:
        return (version or "unknown").split("+", 1)[0].strip().lower()

    @classmethod
    async def get_tools_for_project(cls, project_hash: str) -> list[Any]:
        """Retrieve tools registered for a active project hash."""
        if cls._registry is None:
            return []

        session_id = await cls._registry.get_session_id_by_hash(project_hash)
        if not session_id:
            return []

        session = await cls._registry.get_session(session_id)
        if not session:
            return []

        return list(session.tools.values())

    @classmethod
    async def get_tool_definition(cls, project_hash: str, tool_name: str) -> Any | None:
        """Retrieve a specific tool definition for an active project hash."""
        if cls._registry is None:
            return None

        session_id = await cls._registry.get_session_id_by_hash(project_hash)
        if not session_id:
            return None

        session = await cls._registry.get_session(session_id)
        if not session:
            return None

        return session.tools.get(tool_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _handle_register(self, websocket: WebSocket, payload: RegisterMessage) -> None:
        cls = type(self)
        registry = cls._registry
        lock = cls._lock
        if registry is None or lock is None or cls._register_locks_guard is None:
            await websocket.close(code=1011)
            raise RuntimeError("PluginHub not configured")

        project_name = payload.project_name
        project_hash = payload.project_hash
        unity_version = payload.unity_version

        if not project_hash:
            await websocket.close(code=4400)
            raise ValueError(
                "Plugin registration missing project_hash")

        session_id = str(uuid.uuid4())

        # Serialize the whole lookup/register/insert/notify sequence per
        # project hash: two editors reconnecting the same hash concurrently
        # must not interleave, or both ends can believe they own the hash and
        # keep superseding each other. Different hashes take different locks,
        # so a stuck registration for one project cannot block the others.
        # (No await between _register_lock_for and `async with`: the sweeper's
        # lock pruning must not see this lock as unheld.)
        previous_session_id: str | None = None
        previous_websocket: WebSocket | None = None
        previous_session = None
        register_lock = await cls._register_lock_for(project_hash)
        async with register_lock:
            candidate_id = await registry.get_session_id_by_hash(project_hash)
            if candidate_id:
                async with lock:
                    candidate_ws = cls._connections.get(candidate_id)
                if candidate_ws is not None and candidate_ws is not websocket:
                    previous_session_id = candidate_id
                    previous_websocket = candidate_ws
                    # Mark before registry.register() below drops the old
                    # record: until _evict_superseded_session finishes, the
                    # orphan sweep must not treat this socket as a stray and
                    # close it with 4408 — the old client needs its
                    # session_superseded notification (else it auto-reconnects
                    # and the two editors ping-pong).
                    cls._eviction_in_progress.add(candidate_id)
                    # Captured for rollback: register() below atomically drops
                    # the old record, so restoring it needs the original data.
                    try:
                        previous_session = await registry.get_session(candidate_id)
                    except Exception:
                        # 标记泄漏兜底：此处抛出会让 _evict_superseded_session
                        # 永不执行，标记不摘会让该 session 永久免疫孤儿清扫。
                        cls._eviction_in_progress.discard(candidate_id)
                        previous_session_id = None
                        previous_websocket = None
                        raise
                elif candidate_ws is websocket:
                    # Same connection re-registering (e.g. a client retry):
                    # drop the old session's state so reverse websocket lookups
                    # and the orphan sweep cannot misattribute this socket or
                    # close it out from under the new session.
                    await cls._cleanup_session_locked(candidate_id)

            session = await registry.register(
                session_id,
                project_name,
                project_hash,
                unity_version,
                project_path=payload.project_path,
                package_version=payload.package_version,
                current_scene=payload.current_scene,
                capabilities_version=payload.capabilities_version,
            )
            async with lock:
                cls._connections[session.session_id] = websocket

            # Inform the plugin of its assigned session ID. Bounded wait: a
            # stuck socket must not hold the per-hash register lock past the
            # client's own registration timeout.
            # If the socket is already dying, roll back the new session and
            # restore the superseded one so the project keeps a usable session.
            response = RegisteredMessage(session_id=session_id)
            try:
                await asyncio.wait_for(
                    websocket.send_json(response.model_dump()),
                    timeout=cls.REGISTER_ACK_SEND_TIMEOUT_SECONDS,
                )
            except Exception:
                await cls._cleanup_session_locked(session_id)
                # The eviction below never runs on this path, so the old socket
                # is not being superseded after all; drop the mark with the
                # rollback (the restored session owns the socket again).
                if previous_session_id is not None:
                    cls._eviction_in_progress.discard(previous_session_id)
                if previous_session is not None:
                    restored = await registry.register(
                        previous_session.session_id,
                        previous_session.project_name,
                        previous_session.project_hash,
                        previous_session.unity_version,
                        project_path=previous_session.project_path,
                        package_version=previous_session.package_version,
                        current_scene=previous_session.current_scene,
                        capabilities_version=previous_session.capabilities_version,
                    )
                    async with lock:
                        cls._connections[restored.session_id] = previous_websocket
                    logger.info(
                        "Restored superseded session %s after registration rollback",
                        restored.session_id,
                    )
                try:
                    await websocket.close(code=1011)
                except Exception:
                    pass
                raise

        # Evict the previous connection only after the new session is fully
        # registered, so the project never has zero usable sessions.
        if previous_session_id is not None and previous_websocket is not None:
            await cls._evict_superseded_session(
                project_hash=project_hash,
                project_name=project_name,
                previous_session_id=previous_session_id,
                previous_websocket=previous_websocket,
            )
        logger.info(f"Plugin registered: {project_name} ({project_hash})")

    async def _handle_register_tools(self, websocket: WebSocket, payload: RegisterToolsMessage) -> None:
        cls = type(self)
        registry = cls._registry
        lock = cls._lock
        if registry is None or lock is None:
            return

        # Find session_id for this websocket
        async with lock:
            session_id = next(
                (sid for sid, ws in cls._connections.items() if ws is websocket), None)

        if not session_id:
            logger.warning("Received register_tools from unknown connection")
            return

        await registry.register_tools_for_session(session_id, payload.tools)
        logger.info(
            f"Registered {len(payload.tools)} tools for session {session_id}")

    async def _handle_command_result(self, websocket: WebSocket, payload: CommandResultMessage) -> None:
        cls = type(self)
        lock = cls._lock
        if lock is None:
            return
        command_id = payload.id
        result = payload.result

        if not command_id:
            logger.warning(f"Command result missing id: {payload}")
            return

        # Only accept results from the connection that owns the pending command:
        # a forged connection must not be able to complete another session's
        # commands.
        source_session_id = await cls._session_id_for_websocket(websocket)
        if source_session_id is None:
            logger.warning(
                "Discarding command result %s from an unregistered connection",
                command_id,
            )
            return

        async with lock:
            entry = cls._pending.get(command_id)
        expected_session_id = entry.get(
            "session_id") if isinstance(entry, dict) else None
        if expected_session_id is not None and expected_session_id != source_session_id:
            logger.warning(
                "Discarding command result %s from session %s; command belongs to session %s",
                command_id,
                source_session_id,
                expected_session_id,
            )
            return
        future = entry.get("future") if isinstance(entry, dict) else None
        if future and not future.done():
            future.set_result(result)

    async def _handle_pong(self, websocket: WebSocket, payload: PongMessage) -> None:
        cls = type(self)
        registry = cls._registry
        if registry is None:
            return
        # Trust the connection, not the payload: a pong may only refresh the
        # session that owns the websocket it arrived on.
        source_session_id = await cls._session_id_for_websocket(websocket)
        if source_session_id is None:
            return
        claimed_session_id = payload.session_id
        if claimed_session_id and claimed_session_id != source_session_id:
            logger.debug(
                "Pong payload session_id %s does not match connection session %s; using the connection",
                claimed_session_id,
                source_session_id,
            )
        await registry.touch(source_session_id)

    @classmethod
    async def _evict_superseded_session(
        cls,
        *,
        project_hash: str,
        project_name: str,
        previous_session_id: str,
        previous_websocket: WebSocket,
    ) -> None:
        """Notify and tear down a still-connected previous session that has
        been superseded by a new registration for the same project hash.

        Runs after the new session is fully registered; ``registry.register``
        has already dropped the old session record, so the cleanup below only
        clears the connection, in-flight commands and scheduler state.
        """
        # Re-verify socket ownership before closing: the old client may have
        # re-registered on the same socket while we were outside the register
        # lock, and closing it now would kill that live session. (In that path
        # the old session was already cleaned up during re-registration.)
        try:
            lock = cls._lock
            if lock is not None:
                async with lock:
                    socket_reused = any(
                        sid != previous_session_id and ws is previous_websocket
                        for sid, ws in cls._connections.items()
                    )
                if socket_reused:
                    logger.info(
                        "Skipping eviction close for session %s: its socket was re-registered by a newer session",
                        previous_session_id,
                    )
                    return

            logger.info(
                "Project hash %s re-registered by a new connection; superseding session %s",
                project_hash,
                previous_session_id,
            )
            try:
                await previous_websocket.send_json({
                    "type": "session_superseded",
                    "reason": "duplicate_project_hash",
                    "project_hash": project_hash,
                    "project_name": project_name,
                })
            except Exception:
                logger.debug(
                    "Failed to notify superseded session %s",
                    previous_session_id,
                    exc_info=True,
                )
            try:
                # Bounded wait: the old editor may be frozen and never answer.
                await asyncio.wait_for(
                    previous_websocket.close(code=cls.CLOSE_CODE_SESSION_SUPERSEDED),
                    timeout=3.0,
                )
            except Exception:
                logger.debug(
                    "Failed to close superseded websocket for session %s",
                    previous_session_id,
                    exc_info=True,
                )
            await cls._cleanup_session_locked(previous_session_id)
        finally:
            # Win or lose, the supersede window is over: the orphan sweep may
            # treat this socket as a stray again from here on.
            cls._eviction_in_progress.discard(previous_session_id)

    # ------------------------------------------------------------------
    # Stale session sweeper
    # ------------------------------------------------------------------
    @classmethod
    def _stale_threshold_seconds(cls) -> float:
        raw = os.environ.get("UNITY_MCP_SESSION_STALE_SECONDS")
        if raw is None:
            return cls.SESSION_STALE_SECONDS
        try:
            value = float(raw)
        except ValueError:
            logger.warning(
                "Invalid UNITY_MCP_SESSION_STALE_SECONDS=%r, using default %.1f",
                raw,
                cls.SESSION_STALE_SECONDS,
            )
            return cls.SESSION_STALE_SECONDS
        return max(20.0, value)

    @classmethod
    def start_session_sweeper(cls) -> None:
        """Start the background task evicting stale plugin sessions."""
        existing = cls._sweeper_task
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No running event loop; session sweeper not started")
            return
        cls._sweeper_task = loop.create_task(
            cls._session_sweeper_loop(),
            name="mcp-for-unity-session-sweeper",
        )

    @classmethod
    async def stop_session_sweeper(cls) -> None:
        task = cls._sweeper_task
        cls._sweeper_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Session sweeper task ended with an error", exc_info=True)

    @classmethod
    async def _session_sweeper_loop(cls) -> None:
        while True:
            await asyncio.sleep(cls.SESSION_SWEEP_INTERVAL_SECONDS)
            try:
                await cls.sweep_stale_sessions_once()
            except Exception:
                logger.exception("Stale session sweep failed")

    @classmethod
    async def sweep_stale_sessions_once(cls) -> int:
        """Evict every session idle past the stale threshold. Returns count."""
        registry = cls._registry
        if registry is None:
            return 0
        threshold = timedelta(seconds=cls._stale_threshold_seconds())
        stale_sessions = await registry.list_stale_sessions(threshold)
        evicted = 0
        for session in stale_sessions:
            session_id = session.session_id
            # Re-check before touching the connection: the
            # list_stale_sessions() snapshot takes time, and a pong arriving
            # in that window must save the session from eviction.
            if not await cls._session_still_stale(session_id, threshold):
                continue
            lock = cls._lock
            websocket = None
            if lock is not None:
                async with lock:
                    websocket = cls._connections.get(session_id)
            if websocket is not None:
                try:
                    # Best-effort: on a half-open TCP connection close may hang,
                    # so bound the wait and clean up regardless.
                    await asyncio.wait_for(
                        websocket.close(code=cls.CLOSE_CODE_STALE_SESSION),
                        timeout=3.0,
                    )
                except Exception:
                    pass
            # Re-check once more before destroying the registry record: the
            # bounded close() also takes time, and a pong arriving during the
            # close must still save the session.
            if not await cls._session_still_stale(session_id, threshold):
                continue
            logger.info(
                "Evicting stale Unity plugin session %s (%s@%s)",
                session_id,
                session.project_name,
                session.project_hash,
            )
            await cls._cleanup_session_locked(session_id)
            evicted += 1

        # Defense in depth: connections whose session is no longer in the
        # registry (e.g. a supersede interrupted mid-flight) can never serve
        # commands; close and drop them.
        lock = cls._lock
        if lock is not None:
            async with lock:
                connected = dict(cls._connections)
            for session_id, websocket in connected.items():
                if session_id in cls._eviction_in_progress:
                    # A supersede eviction is in flight for this socket (old
                    # registry record already dropped by the replacement's
                    # register()). Leave it alone so the old client receives
                    # its session_superseded notification and 4409 close
                    # instead of a bare 4408 — otherwise it would
                    # auto-reconnect and supersede back (ping-pong).
                    continue
                if await registry.get_session(session_id) is not None:
                    continue
                logger.warning(
                    "Closing orphaned plugin connection %s (no registry entry)",
                    session_id,
                )
                try:
                    await asyncio.wait_for(
                        websocket.close(code=cls.CLOSE_CODE_STALE_SESSION),
                        timeout=3.0,
                    )
                except Exception:
                    pass
                await cls._cleanup_session_locked(session_id)

        # Prune per-hash register locks whose project has no live session, so
        # a long-running server does not accumulate locks for closed projects.
        # Only locks not currently held are removed, and only under the guard,
        # so a registration in flight always keeps its lock.
        guard = cls._register_locks_guard
        if guard is not None:
            active_hashes = {
                session.project_hash
                for session in (await registry.list_sessions()).values()
            }
            async with guard:
                for hash_value, register_lock in list(cls._register_locks.items()):
                    if hash_value not in active_hashes and not register_lock.locked():
                        cls._register_locks.pop(hash_value, None)
        return evicted

    @classmethod
    async def _session_still_stale(cls, session_id: str, threshold: timedelta) -> bool:
        """Confirm a session is still present and stale before eviction."""
        registry = cls._registry
        if registry is None:
            return False
        session = await registry.get_session(session_id)
        if session is None:
            return False
        now = datetime.now(timezone.utc)
        if now - session.last_seen <= threshold:
            logger.debug(
                "Session %s was refreshed while sweeping; skipping eviction",
                session_id,
            )
            return False
        return True

    @classmethod
    async def _send_cancel_best_effort(cls, websocket: WebSocket, command_id: str) -> None:
        """Notify the plugin that a timed-out command should be cancelled."""
        try:
            msg = CancelCommandMessage(id=command_id)
            await websocket.send_json(msg.model_dump())
        except Exception:
            logger.debug(
                "Failed to send cancel for command %s", command_id, exc_info=True)

    @classmethod
    async def _get_connection(cls, session_id: str) -> WebSocket:
        lock = cls._lock
        if lock is None:
            raise RuntimeError("PluginHub not configured")
        async with lock:
            websocket = cls._connections.get(session_id)
        if websocket is None:
            raise PluginDisconnectedError(
                f"Unity plugin session {session_id} is not connected "
                "(disconnected or still reconnecting)"
            )
        return websocket

    # ------------------------------------------------------------------
    # Session resolution helpers
    # ------------------------------------------------------------------
    @classmethod
    async def _resolve_session_id(cls, unity_instance: str | None) -> str:
        """Resolve a project hash (Unity instance id) to an active plugin session.

        Resolve once and fail fast when no session is available. The caller receives
        a retryable response because Unity may be compiling or reconnecting.
        """
        if cls._registry is None:
            raise RuntimeError("Plugin registry not configured")

        # Allow callers to provide either just the hash or Name@hash
        target_hash: str | None = None
        if unity_instance:
            if "@" in unity_instance:
                _, _, suffix = unity_instance.rpartition("@")
                target_hash = suffix or None
            else:
                target_hash = unity_instance

        async def _try_once() -> tuple[str | None, int]:
            # Prefer a specific Unity instance if one was requested
            if target_hash:
                session_id = await cls._registry.get_session_id_by_hash(target_hash)
                sessions = await cls._registry.list_sessions()
                if session_id is None and len(target_hash) < 16:
                    # Fall back to unambiguous hash-prefix matching, mirroring
                    # set_active_instance semantics.
                    matches = await cls._registry.find_hashes_by_prefix(
                        target_hash.lower())
                    if len(matches) == 1:
                        session_id = await cls._registry.get_session_id_by_hash(
                            matches[0])
                    elif len(matches) > 1:
                        candidates: list[str] = []
                        for hash_value in matches:
                            matched_session_id = await cls._registry.get_session_id_by_hash(
                                hash_value)
                            matched = (
                                await cls._registry.get_session(matched_session_id)
                                if matched_session_id
                                else None
                            )
                            candidates.append(
                                f"{matched.project_name}@{hash_value}" if matched else hash_value)
                        raise RuntimeError(
                            f"Unity instance hash prefix '{target_hash}' is ambiguous "
                            f"({', '.join(candidates)}). "
                            "Use the full Name@hash from mcpforunity://instances."
                        )
                return session_id, len(sessions)

            # No target provided: determine if we can auto-select
            sessions = await cls._registry.list_sessions()
            count = len(sessions)
            if count == 0:
                return None, count
            if count == 1:
                return next(iter(sessions.keys())), count
            # Multiple sessions but no explicit target is ambiguous
            return None, count

        session_id, session_count = await _try_once()
        if session_id is None and not target_hash and session_count > 1:
            raise RuntimeError(
                "Multiple Unity instances are connected. "
                "Call set_active_instance with Name@hash from mcpforunity://instances."
            )

        if session_id is None:
            logger.warning(
                "No Unity plugin session available (instance=%s); reconnect may be in progress",
                unity_instance or "default",
            )
            if target_hash:
                raise NoUnitySessionError(
                    f"No Unity plugins are currently connected matching '{target_hash}'")
            raise NoUnitySessionError(
                "No Unity plugins are currently connected")

        return session_id

    @classmethod
    async def send_command_for_instance(
        cls,
        unity_instance: str | None,
        command_type: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            session_id = await cls._resolve_session_id(unity_instance)
        except NoUnitySessionError as exc:
            logger.debug(
                "Unity session unavailable; automatic reconnect may be in progress: command=%s instance=%s",
                command_type,
                unity_instance or "default",
            )
            return classified_error_response(
                code="no_unity_session",
                category="session",
                # _resolve_session_id includes the requested hash when one was
                # given; fall back to the generic message otherwise.
                error=str(exc) or "Unity session temporarily unavailable; automatic reconnect may be in progress",
                retryable=True,
                retry_after_ms=2000,
                hint="retry",
            )

        scheduler = cls._scheduler
        if scheduler is None:
            return await cls._send_command_for_resolved_session(
                session_id,
                command_type,
                params,
            )

        instance_key, instance_label = await cls._describe_instance(session_id, unity_instance)
        try:
            return await scheduler.run(
                instance_key,
                instance_label,
                command_type,
                lambda: cls._send_command_for_resolved_session(
                    session_id,
                    command_type,
                    params,
                ),
                session_id=session_id,
                queue_timeout_s=cls._queue_timeout_for_command(command_type),
            )
        except InstanceQueueTimeoutError as exc:
            return classified_error_response(
                code="unity_instance_busy",
                category="busy",
                error=str(exc),
                # Queue contention is transient; encourage the client to retry.
                retryable=True,
                retry_after_ms=5000,
                data={
                    "command": command_type,
                    "unity_instance": instance_label,
                    "session_id": session_id,
                    "queue_timeout_seconds": exc.timeout_seconds,
                },
            )

    @classmethod
    async def _send_command_for_resolved_session(
        cls,
        session_id: str,
        command_type: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        return await cls.send_command(session_id, command_type, params)

    @classmethod
    async def _describe_instance(
        cls,
        session_id: str,
        unity_instance: str | None,
    ) -> tuple[str, str]:
        if cls._registry is None:
            return session_id, unity_instance or session_id
        session = await cls._registry.get_session(session_id)
        if session:
            return session.project_hash, f"{session.project_name}@{session.project_hash}"
        if unity_instance:
            if "@" in unity_instance:
                _, _, suffix = unity_instance.rpartition("@")
                return suffix or unity_instance, unity_instance
            return unity_instance, unity_instance
        return session_id, session_id

    @classmethod
    def _queue_timeout_for_command(cls, command_type: str) -> float | None:
        if command_type in cls._FAST_FAIL_COMMANDS:
            return cls._read_timeout_env(
                "UNITY_MCP_FAST_QUEUE_TIMEOUT_SECONDS",
                cls.FAST_QUEUE_TIMEOUT,
            )
        if command_type == "read_console":
            return cls._read_timeout_env(
                "UNITY_MCP_CONSOLE_QUEUE_TIMEOUT_SECONDS",
                cls.READ_CONSOLE_QUEUE_TIMEOUT,
            )
        return cls._read_timeout_env(
            "UNITY_MCP_INSTANCE_QUEUE_TIMEOUT_SECONDS",
            cls.INSTANCE_QUEUE_TIMEOUT,
        )

    @staticmethod
    def _read_timeout_env(name: str, default: float) -> float | None:
        raw = os.environ.get(name)
        if raw is None:
            value = default
        else:
            try:
                value = float(raw)
            except ValueError:
                logger.warning("Invalid %s=%r, using default %.1f", name, raw, default)
                value = default
        if value <= 0:
            return None
        return max(0.05, min(value, 60.0 * 60.0))

    # ------------------------------------------------------------------
    # Blocking helpers for synchronous tool code
    # ------------------------------------------------------------------
    @classmethod
    def _run_coroutine_sync(cls, coro: "asyncio.Future[Any]") -> Any:
        if cls._loop is None:
            raise RuntimeError("PluginHub event loop not configured")
        loop = cls._loop
        if loop.is_running():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            else:
                if running_loop is loop:
                    raise RuntimeError(
                        "Cannot wait synchronously for PluginHub coroutine from within the event loop"
                    )
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    @classmethod
    def send_command_blocking(
        cls,
        unity_instance: str | None,
        command_type: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        return cls._run_coroutine_sync(
            cls.send_command_for_instance(unity_instance, command_type, params)
        )

    @classmethod
    def list_sessions_sync(cls) -> SessionList:
        return cls._run_coroutine_sync(cls.get_sessions())


def send_command_to_plugin(
    *,
    unity_instance: str | None,
    command_type: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    return PluginHub.send_command_blocking(unity_instance, command_type, params)
