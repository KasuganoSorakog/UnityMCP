from __future__ import annotations

import json
import logging
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from core.logging_config import get_log_directory

logger = logging.getLogger("mcp-for-unity-server")


class ConnectionJournal:
    def __init__(
        self,
        file_path: Path | None = None,
        *,
        max_bytes: int = 1024 * 1024,
        backup_count: int = 4,
    ) -> None:
        self._path = file_path or get_log_directory() / "unity_connection_events.jsonl"
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._handler: RotatingFileHandler | None = None
        self._last_write_succeeded = False
        self._last_error: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        handler = self._handler
        self._handler = None
        if handler is not None:
            handler.acquire()
            try:
                handler.flush()
                handler.close()
            finally:
                handler.release()

    def record_connected(self, session: Any) -> None:
        disconnected_at = session.last_disconnected_at
        event = {
            **self._identity(session),
            "event": "reconnected" if disconnected_at else "connected",
            "timestamp": session.connected_at.isoformat(),
            "previous_disconnected_at": disconnected_at.isoformat() if disconnected_at else None,
            "downtime_ms": round((session.connected_at - disconnected_at).total_seconds() * 1000, 3) if disconnected_at else None,
        }
        self._write(event)

    def record_disconnected(self, session: Any) -> None:
        self._write({
            **self._identity(session),
            "event": "disconnected",
            "timestamp": session.last_disconnected_at.isoformat(),
            "reason": session.last_disconnect_reason,
        })

    def recent(self, project_hash: str, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        events: deque[dict[str, Any]] = deque(maxlen=limit)
        handler = self._handler
        if handler is not None:
            handler.acquire()
        try:
            paths = [Path(f"{self._path}.{index}") for index in range(self._backup_count, 0, -1)]
            paths.append(self._path)
            for path in paths:
                if not path.is_file():
                    continue
                try:
                    with path.open("r", encoding="utf-8") as stream:
                        for line in stream:
                            try:
                                event = json.loads(line)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                continue
                            if isinstance(event, dict) and event.get("project_hash") == project_hash:
                                events.append(event)
                except OSError as exc:
                    return self._status(list(events), str(exc))
        finally:
            if handler is not None:
                handler.release()
        return self._status(list(events))

    def _status(self, events: list[dict[str, Any]], error: str | None = None) -> dict[str, Any]:
        return {
            "persistent": self._last_write_succeeded,
            "path": str(self._path),
            "events": events,
            "error": error or self._last_error,
        }

    @staticmethod
    def _identity(session: Any) -> dict[str, Any]:
        return {
            "project_name": session.project_name,
            "project_hash": session.project_hash,
            "session_id": session.session_id,
            "unity_version": session.unity_version,
        }

    def _write(self, event: dict[str, Any]) -> None:
        try:
            if self._handler is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._handler = RotatingFileHandler(
                    self._path,
                    maxBytes=self._max_bytes,
                    backupCount=self._backup_count,
                    encoding="utf-8",
                )
                self._handler.setFormatter(logging.Formatter("%(message)s"))
            record = logging.LogRecord(
                "unity-connection-journal", logging.INFO, __file__, 0,
                json.dumps(event, ensure_ascii=False, separators=(",", ":")), (), None,
            )
            self._handler.emit(record)
            self._last_write_succeeded = True
            self._last_error = None
        except (OSError, ValueError) as exc:
            self._last_write_succeeded = False
            self._last_error = str(exc)
            logger.warning("Could not persist Unity connection event: %s", exc)
