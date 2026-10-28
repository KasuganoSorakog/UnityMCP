"""In-memory registry for connected Unity plugin sessions."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import asyncio

from models.models import ToolDefinitionModel
from services.state.connection_journal import ConnectionJournal


@dataclass(slots=True)
class PluginSession:
    """Represents a single Unity plugin connection."""

    session_id: str
    project_name: str
    project_hash: str
    unity_version: str
    registered_at: datetime
    connected_at: datetime
    # Liveness timestamp: refreshed on heartbeats and any inbound message.
    # ``connected_at`` stays fixed at connect time for UI display purposes.
    last_seen: datetime
    tools: dict[str, ToolDefinitionModel] = field(default_factory=dict)
    project_id: str | None = None
    project_path: str | None = None
    package_version: str | None = None
    current_scene: str | None = None
    capabilities_version: str | None = None
    last_disconnected_at: datetime | None = None
    last_disconnect_reason: str | None = None


class PluginRegistry:
    """Track active connections and bounded, per-project disconnect history."""

    def __init__(self, journal: ConnectionJournal | None = None) -> None:
        self._sessions: dict[str, PluginSession] = {}
        self._hash_to_session: dict[str, str] = {}
        self._disconnected: dict[str, PluginSession] = {}
        self._lock = asyncio.Lock()
        self._journal = journal

    async def register(
        self,
        session_id: str,
        project_name: str,
        project_hash: str,
        unity_version: str,
        project_path: str | None = None,
        package_version: str | None = None,
        current_scene: str | None = None,
        capabilities_version: str | None = None,
    ) -> PluginSession:
        async with self._lock:
            now = datetime.now(timezone.utc)
            previous = self._disconnected.get(project_hash)
            previous_session_id = self._hash_to_session.get(project_hash)
            replaced = self._sessions.pop(previous_session_id, None) if previous_session_id else None
            if replaced is not None:
                previous = replace(
                    replaced,
                    tools=dict(replaced.tools),
                    last_disconnected_at=now,
                    last_disconnect_reason="session_replaced",
                )
                self._disconnected[project_hash] = previous
                if self._journal is not None:
                    self._journal.record_disconnected(previous)
            session = PluginSession(
                session_id=session_id,
                project_name=project_name,
                project_hash=project_hash,
                unity_version=unity_version,
                registered_at=now,
                connected_at=now,
                last_seen=now,
                project_path=project_path,
                package_version=package_version,
                current_scene=current_scene,
                capabilities_version=capabilities_version,
                tools=dict(previous.tools) if previous else {},
                last_disconnected_at=previous.last_disconnected_at if previous else None,
                last_disconnect_reason=previous.last_disconnect_reason if previous else None,
            )
            self._sessions[session_id] = session
            self._hash_to_session[project_hash] = session_id
            self._disconnected.pop(project_hash, None)
            while len(self._disconnected) > 128:
                oldest = min(self._disconnected.values(), key=lambda item: item.last_disconnected_at)
                del self._disconnected[oldest.project_hash]
            return session

    @property
    def has_connection_journal(self) -> bool:
        return self._journal is not None

    def record_connected(self, session: PluginSession) -> None:
        if self._journal is not None:
            self._journal.record_connected(session)

    def close_journal(self) -> None:
        if self._journal is not None:
            self._journal.close()

    async def connection_events(self, project_hash: str, limit: int = 20) -> dict[str, object]:
        if self._journal is None:
            return {"persistent": False, "path": None, "events": [], "error": None}
        async with self._lock:
            return self._journal.recent(project_hash, limit)

    async def touch(self, session_id: str) -> None:
        """Refresh the ``last_seen`` liveness timestamp for a session.

        ``connected_at`` is intentionally left untouched: it records when the
        connection was established and is surfaced in UIs as such.
        """

        async with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.last_seen = datetime.now(timezone.utc)

    async def unregister(self, session_id: str, reason: str | None = None) -> None:
        """Disconnect a session without letting an old socket overwrite a newer registration."""
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None or self._hash_to_session.get(session.project_hash) != session_id:
                return
            del self._hash_to_session[session.project_hash]
            snapshot = replace(
                session,
                tools=dict(session.tools),
                last_disconnected_at=datetime.now(timezone.utc),
                last_disconnect_reason=reason or "websocket_closed",
            )
            self._disconnected[session.project_hash] = snapshot
            if self._journal is not None:
                self._journal.record_disconnected(snapshot)
            while len(self._disconnected) > 128:
                oldest = min(self._disconnected.values(), key=lambda item: item.last_disconnected_at)
                del self._disconnected[oldest.project_hash]

    async def register_tools_for_session(self, session_id: str, tools: list[ToolDefinitionModel]) -> None:
        """Register tools for a specific session."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session:
                # Replace existing tools or merge? Usually replace for "set state".
                # We will replace the dict but keep the field.
                session.tools.clear()
                for tool in tools:
                    session.tools[tool.name] = tool

    async def get_session(self, session_id: str) -> PluginSession | None:
        """Fetch a session by its ``session_id``."""

        async with self._lock:
            return self._sessions.get(session_id)

    async def get_session_id_by_hash(self, project_hash: str) -> str | None:
        """Resolve a ``project_hash`` (Unity instance id) to a session id."""

        async with self._lock:
            return self._hash_to_session.get(project_hash)

    async def find_hashes_by_prefix(self, prefix: str) -> list[str]:
        """Return all registered project hashes that start with ``prefix``."""

        async with self._lock:
            return sorted(
                project_hash
                for project_hash in self._hash_to_session
                if project_hash.startswith(prefix)
            )

    async def list_stale_sessions(self, threshold: timedelta) -> list[PluginSession]:
        """Return sessions whose ``last_seen`` is older than ``threshold``."""

        now = datetime.now(timezone.utc)
        async with self._lock:
            return [
                session
                for session in self._sessions.values()
                if now - session.last_seen > threshold
            ]

    async def list_sessions(self) -> dict[str, PluginSession]:
        """Return a shallow copy of all known sessions."""

        async with self._lock:
            return dict(self._sessions)

    async def list_disconnected(self) -> dict[str, PluginSession]:
        """Return the latest disconnected snapshot for each bounded project history entry."""
        async with self._lock:
            return {
                project_hash: replace(session, tools=dict(session.tools))
                for project_hash, session in self._disconnected.items()
            }


__all__ = ["PluginRegistry", "PluginSession"]
