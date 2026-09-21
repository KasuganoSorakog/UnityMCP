"""
Privacy-focused, anonymous telemetry system for MCP for Unity
Inspired by Onyx's telemetry implementation with Unity-specific adaptations

Fire-and-forget telemetry sender with a single background worker.
- No context/thread-local propagation to avoid re-entrancy into tool resolution.
- Small network timeouts to prevent stalls.

Fork note (Sora Unity MCP): all outbound telemetry has been physically removed.
The upstream endpoint belonged to CoplayDev; this fork never phones home.
The public API (record_*, is_telemetry_enabled, get_package_version) is kept
as no-op shims for import compatibility; `enabled` is hard-coded to False.
"""

import contextlib
from dataclasses import dataclass
from enum import Enum
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any
import uuid

import tomli

logger = logging.getLogger("unity-mcp-telemetry")
PACKAGE_NAME = "mcpforunityserver"


def _version_from_local_pyproject() -> str:
    """Locate the nearest pyproject.toml that matches our package name."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "pyproject.toml"
        if not candidate.exists():
            continue
        try:
            with candidate.open("rb") as f:
                data = tomli.load(f)
        except (OSError, tomli.TOMLDecodeError):
            continue

        project_table = data.get("project") or {}
        poetry_table = data.get("tool", {}).get("poetry", {})

        project_name = project_table.get("name") or poetry_table.get("name")
        if project_name and project_name.lower() != PACKAGE_NAME.lower():
            continue

        version = project_table.get("version") or poetry_table.get("version")
        if version:
            return version
    raise FileNotFoundError("pyproject.toml not found for mcpforunityserver")


def get_package_version() -> str:
    """
    Get package version in different ways:
    1. First we try the installed metadata - this is because uvx is used on the asset store
    2. If that fails, we try to read from pyproject.toml - this is available for users who download via Git
    Default is "unknown", but that should never happen
    """
    try:
        return metadata.version(PACKAGE_NAME)
    except Exception:
        # Fallback for development: read from pyproject.toml
        try:
            return _version_from_local_pyproject()
        except Exception:
            return "unknown"


class RecordType(str, Enum):
    """Types of telemetry records we collect"""
    VERSION = "version"
    STARTUP = "startup"
    USAGE = "usage"
    LATENCY = "latency"
    FAILURE = "failure"
    RESOURCE_RETRIEVAL = "resource_retrieval"
    TOOL_EXECUTION = "tool_execution"
    UNITY_CONNECTION = "unity_connection"
    CLIENT_CONNECTION = "client_connection"


class MilestoneType(str, Enum):
    """Major user journey milestones"""
    FIRST_STARTUP = "first_startup"
    FIRST_TOOL_USAGE = "first_tool_usage"
    FIRST_SCRIPT_CREATION = "first_script_creation"
    FIRST_SCENE_MODIFICATION = "first_scene_modification"
    MULTIPLE_SESSIONS = "multiple_sessions"
    DAILY_ACTIVE_USER = "daily_active_user"
    WEEKLY_ACTIVE_USER = "weekly_active_user"


@dataclass
class TelemetryRecord:
    """Structure for telemetry data"""
    record_type: RecordType
    timestamp: float
    customer_uuid: str
    session_id: str
    data: dict[str, Any]
    milestone: MilestoneType | None = None


class TelemetryConfig:
    """Telemetry configuration.

    Fork note (Sora Unity MCP): outbound telemetry was removed entirely, so
    `enabled` is hard-coded to False and there is no endpoint configuration.
    The local-only fields below are kept because record_*() and the milestone
    bookkeeping reference them.
    """

    def __init__(self):
        self.enabled = False
        self.endpoint = None
        self.default_endpoint = None
        self.timeout = 0.0

        # Local storage for UUID and milestones
        self.data_dir = self._get_data_directory()
        self.uuid_file = self.data_dir / "customer_uuid.txt"
        self.milestones_file = self.data_dir / "milestones.json"

        # Session tracking
        self.session_id = str(uuid.uuid4())

    def _get_data_directory(self) -> Path:
        """Get directory for storing telemetry data"""
        if os.name == 'nt':  # Windows
            base_dir = Path(os.environ.get(
                'APPDATA', Path.home() / 'AppData' / 'Roaming'))
        elif os.name == 'posix':  # macOS/Linux
            if 'darwin' in os.uname().sysname.lower():  # macOS
                base_dir = Path.home() / 'Library' / 'Application Support'
            else:  # Linux
                base_dir = Path(os.environ.get('XDG_DATA_HOME',
                                Path.home() / '.local' / 'share'))
        else:
            base_dir = Path.home() / '.unity-mcp'

        data_dir = base_dir / 'UnityMCP'
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir


class TelemetryCollector:
    """Main telemetry collection class"""

    def __init__(self):
        self.config = TelemetryConfig()
        self._customer_uuid: str | None = None
        self._milestones: dict[str, dict[str, Any]] = {}
        self._lock: threading.Lock = threading.Lock()
        # Bounded queue with single background worker (records only; no context propagation)
        self._queue: "queue.Queue[TelemetryRecord]" = queue.Queue(maxsize=1000)
        # Load persistent data before starting worker so first events have UUID
        self._load_persistent_data()
        self._worker: threading.Thread = threading.Thread(
            target=self._worker_loop, daemon=True)
        self._worker.start()

    def _load_persistent_data(self):
        """Load UUID and milestones from disk"""
        # Load customer UUID
        try:
            if self.config.uuid_file.exists():
                self._customer_uuid = self.config.uuid_file.read_text(
                    encoding="utf-8").strip() or str(uuid.uuid4())
            else:
                self._customer_uuid = str(uuid.uuid4())
                try:
                    self.config.uuid_file.write_text(
                        self._customer_uuid, encoding="utf-8")
                    if os.name == "posix":
                        os.chmod(self.config.uuid_file, 0o600)
                except OSError as e:
                    logger.debug(
                        f"Failed to persist customer UUID: {e}", exc_info=True)
        except OSError as e:
            logger.debug(f"Failed to load customer UUID: {e}", exc_info=True)
            self._customer_uuid = str(uuid.uuid4())

        # Load milestones (failure here must not affect UUID)
        try:
            if self.config.milestones_file.exists():
                content = self.config.milestones_file.read_text(
                    encoding="utf-8")
                self._milestones = json.loads(content) or {}
                if not isinstance(self._milestones, dict):
                    self._milestones = {}
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.debug(f"Failed to load milestones: {e}", exc_info=True)
            self._milestones = {}

    def _save_milestones(self):
        """Save milestones to disk. Caller must hold self._lock."""
        try:
            self.config.milestones_file.write_text(
                json.dumps(self._milestones, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"Failed to save milestones: {e}", exc_info=True)

    def record_milestone(self, milestone: MilestoneType, data: dict[str, Any] | None = None) -> bool:
        """Record a milestone event, returns True if this is the first occurrence"""
        if not self.config.enabled:
            return False
        milestone_key = milestone.value
        with self._lock:
            if milestone_key in self._milestones:
                return False  # Already recorded
            milestone_data = {
                "timestamp": time.time(),
                "data": data or {},
            }
            self._milestones[milestone_key] = milestone_data
            self._save_milestones()

        # Also send as telemetry record
        self.record(
            record_type=RecordType.USAGE,
            data={"milestone": milestone_key, **(data or {})},
            milestone=milestone
        )

        return True

    def record(self,
               record_type: RecordType,
               data: dict[str, Any],
               milestone: MilestoneType | None = None):
        """Record a telemetry event (async, non-blocking)"""
        if not self.config.enabled:
            return

        record = TelemetryRecord(
            record_type=record_type,
            timestamp=time.time(),
            customer_uuid=self._customer_uuid or "unknown",
            session_id=self.config.session_id,
            data=data,
            milestone=milestone
        )
        # Enqueue for background worker (non-blocking). Drop on backpressure.
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            logger.debug(
                f"Telemetry queue full; dropping {record.record_type}")

    def _worker_loop(self):
        """Background worker that serializes telemetry sends."""
        while True:
            rec = self._queue.get()
            try:
                # Run sender directly; do not reuse caller context/thread-locals
                self._send_telemetry(rec)
            except Exception:
                logger.debug("Telemetry worker send failed", exc_info=True)
            finally:
                with contextlib.suppress(Exception):
                    self._queue.task_done()

    def _send_telemetry(self, record: TelemetryRecord):
        """No-op: outbound telemetry has been physically removed in this fork
        (the upstream endpoint belonged to CoplayDev). Kept so the background
        worker loop remains structurally intact.
        """
        return


# Global telemetry instance
_telemetry_collector: TelemetryCollector | None = None


def get_telemetry() -> TelemetryCollector:
    """Get the global telemetry collector instance"""
    global _telemetry_collector
    if _telemetry_collector is None:
        _telemetry_collector = TelemetryCollector()
    return _telemetry_collector


def record_telemetry(record_type: RecordType,
                     data: dict[str, Any],
                     milestone: MilestoneType | None = None):
    """Convenience function to record telemetry"""
    get_telemetry().record(record_type, data, milestone)


def record_milestone(milestone: MilestoneType, data: dict[str, Any] | None = None) -> bool:
    """Convenience function to record a milestone"""
    return get_telemetry().record_milestone(milestone, data)


def record_tool_usage(tool_name: str, success: bool, duration_ms: float, error: str | None = None, sub_action: str | None = None):
    """Record tool usage telemetry

    Args:
        tool_name: Name of the tool invoked (e.g., 'manage_scene').
        success: Whether the tool completed successfully.
        duration_ms: Execution duration in milliseconds.
        error: Optional error message (truncated if present).
        sub_action: Optional sub-action/operation within the tool (e.g., 'get_hierarchy').
    """
    data = {
        "tool_name": tool_name,
        "success": success,
        "duration_ms": round(duration_ms, 2)
    }

    if sub_action is not None:
        try:
            data["sub_action"] = str(sub_action)
        except Exception:
            # Ensure telemetry is never disruptive
            data["sub_action"] = "unknown"

    if error:
        data["error"] = str(error)[:200]  # Limit error message length

    record_telemetry(RecordType.TOOL_EXECUTION, data)


def record_resource_usage(resource_name: str, success: bool, duration_ms: float, error: str | None = None):
    """Record resource usage telemetry

    Args:
        resource_name: Name of the resource invoked (e.g., 'get_tests').
        success: Whether the resource completed successfully.
        duration_ms: Execution duration in milliseconds.
        error: Optional error message (truncated if present).
    """
    data = {
        "resource_name": resource_name,
        "success": success,
        "duration_ms": round(duration_ms, 2)
    }

    if error:
        data["error"] = str(error)[:200]  # Limit error message length

    record_telemetry(RecordType.RESOURCE_RETRIEVAL, data)


def record_latency(operation: str, duration_ms: float, metadata: dict[str, Any] | None = None):
    """Record latency telemetry"""
    data = {
        "operation": operation,
        "duration_ms": round(duration_ms, 2)
    }

    if metadata:
        data.update(metadata)

    record_telemetry(RecordType.LATENCY, data)


def record_failure(component: str, error: str, metadata: dict[str, Any] | None = None):
    """Record failure telemetry"""
    data = {
        "component": component,
        "error": str(error)[:500]  # Limit error message length
    }

    if metadata:
        data.update(metadata)

    record_telemetry(RecordType.FAILURE, data)


def is_telemetry_enabled() -> bool:
    """Check if telemetry is enabled"""
    return get_telemetry().config.enabled
