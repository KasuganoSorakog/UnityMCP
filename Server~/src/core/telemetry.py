"""
Telemetry shims for MCP for Unity (Sora Unity MCP fork).

All telemetry has been physically removed in this fork: no network sender, no
persistent install ID, no background worker, no queue. This module keeps only
the public API surface as zero-cost no-op shims so existing imports keep
working. `get_package_version` is a real, local-only helper (version display).
"""

from dataclasses import dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import Any

import tomli

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
    """Kept for import compatibility; nothing is recorded anymore."""

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
    """Kept for import compatibility; nothing is recorded anymore."""

    FIRST_STARTUP = "first_startup"
    FIRST_TOOL_USAGE = "first_tool_usage"
    FIRST_SCRIPT_CREATION = "first_script_creation"
    FIRST_SCENE_MODIFICATION = "first_scene_modification"
    MULTIPLE_SESSIONS = "multiple_sessions"
    DAILY_ACTIVE_USER = "daily_active_user"
    WEEKLY_ACTIVE_USER = "weekly_active_user"


@dataclass
class TelemetryRecord:
    """Kept for import compatibility; instances are never created."""

    record_type: RecordType
    timestamp: float
    customer_uuid: str
    session_id: str
    data: dict[str, Any]
    milestone: MilestoneType | None = None


class TelemetryConfig:
    """Inert configuration: telemetry is permanently disabled in this fork.

    No data directory is created, no UUID is persisted, no endpoint exists.
    """

    def __init__(self):
        self.enabled = False
        self.endpoint = None
        self.default_endpoint = None
        self.timeout = 0.0


class TelemetryCollector:
    """Zero-cost no-op collector (no thread, no queue, no disk I/O)."""

    def __init__(self):
        self.config = TelemetryConfig()

    def record_milestone(self, milestone: MilestoneType, data: dict[str, Any] | None = None) -> bool:
        return False

    def record(self,
               record_type: RecordType,
               data: dict[str, Any],
               milestone: MilestoneType | None = None):
        return None


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
    """No-op: telemetry removed in this fork."""
    return None


def record_milestone(milestone: MilestoneType, data: dict[str, Any] | None = None) -> bool:
    """No-op: telemetry removed in this fork. Always returns False."""
    return False


def record_tool_usage(tool_name: str, success: bool, duration_ms: float, error: str | None = None, sub_action: str | None = None):
    """No-op: telemetry removed in this fork."""
    return None


def record_resource_usage(resource_name: str, success: bool, duration_ms: float, error: str | None = None):
    """No-op: telemetry removed in this fork."""
    return None


def record_latency(operation: str, duration_ms: float, metadata: dict[str, Any] | None = None):
    """No-op: telemetry removed in this fork."""
    return None


def record_failure(component: str, error: str, metadata: dict[str, Any] | None = None):
    """No-op: telemetry removed in this fork."""
    return None


def is_telemetry_enabled() -> bool:
    """Telemetry is permanently disabled in this fork."""
    return False
