from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def get_log_directory() -> Path:
    override = os.environ.get("UNITY_MCP_LOG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "UnityMCP" / "Logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "UnityMCP" / "Logs"
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "UnityMCP" / "Logs"


def add_rotating_file_handler(
    logger: logging.Logger,
    filename: str,
    *,
    level: int,
    log_format: str,
    max_bytes: int = 512 * 1024,
    backup_count: int = 2,
) -> Path:
    log_directory = get_log_directory()
    log_directory.mkdir(parents=True, exist_ok=True)
    path = log_directory / filename
    if os.environ.get("PYTEST_CURRENT_TEST"):
        # Unit tests exercise the same logger; without this guard a single `pytest` run appends
        # fake instances to the real rotating log and destroys the evidence `health` reports on.
        return path
    resolved_path = str(path.resolve())
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == resolved_path:
            return path
    handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(log_format))
    handler.setLevel(level)
    logger.addHandler(handler)
    return path


def configure_server_logging(logger: logging.Logger, log_format: str, level: int) -> Path:
    path = add_rotating_file_handler(
        logging.getLogger(), "unity_mcp_server.log", level=level, log_format=log_format,
    )
    logger.propagate = True
    logging.getLogger("unity-mcp-telemetry").propagate = True
    return path
