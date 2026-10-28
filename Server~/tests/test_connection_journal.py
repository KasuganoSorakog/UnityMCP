from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from core.logging_config import get_log_directory
from services.state.connection_journal import ConnectionJournal
from transport.plugin_registry import PluginRegistry


def test_windows_log_directory_uses_local_app_data(monkeypatch, tmp_path):
    monkeypatch.setattr("core.logging_config.os.name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("UNITY_MCP_LOG_DIR", raising=False)
    assert get_log_directory() == tmp_path / "UnityMCP" / "Logs"


def test_log_directory_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITY_MCP_LOG_DIR", str(tmp_path / "custom"))
    assert get_log_directory() == tmp_path / "custom"


def test_connection_journal_records_disconnect_and_reconnect(tmp_path):
    path = tmp_path / "events.jsonl"
    journal = ConnectionJournal(path)
    registry = PluginRegistry(journal=journal)

    async def exercise():
        first = await registry.register("session-1", "Project", "hash-a", "2022.3")
        registry.record_connected(first)
        await registry.unregister("session-1", "domain_reload")
        second = await registry.register("session-2", "Project", "hash-a", "2022.3")
        registry.record_connected(second)
        await registry.unregister("session-2", "heartbeat_timeout")

    asyncio.run(exercise())
    events = journal.recent("hash-a")["events"]
    assert [event["event"] for event in events] == [
        "connected", "disconnected", "reconnected", "disconnected",
    ]
    assert events[1]["reason"] == "domain_reload"
    assert events[2]["previous_disconnected_at"] == events[1]["timestamp"]
    assert events[2]["downtime_ms"] >= 0
    assert all("project_path" not in event for event in events)
    journal.close()


def test_connection_journal_reads_across_rotation(tmp_path):
    path = tmp_path / "events.jsonl"
    journal = ConnectionJournal(path, max_bytes=240, backup_count=1)
    registry = PluginRegistry(journal=journal)

    async def exercise():
        for index in range(12):
            session = await registry.register(f"session-{index}", "Project", "hash-a", "2022.3")
            registry.record_connected(session)
            await registry.unregister(f"session-{index}", f"reason-{index}")

    asyncio.run(exercise())
    result = journal.recent("hash-a", limit=4)
    assert result["persistent"] is True
    assert result["events"][-1]["reason"] == "reason-11"
    backup_events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl.1").read_text(encoding="utf-8").splitlines()
    ]
    assert backup_events
    assert result["events"][0] == backup_events[0]
    for file in path.parent.glob("events.jsonl*"):
        for line in file.read_text(encoding="utf-8").splitlines():
            assert isinstance(json.loads(line), dict)
    journal.close()


class ConnectionJournalHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tempfile import TemporaryDirectory
        from transport.plugin_hub import PluginHub

        directory = self.enterContext(TemporaryDirectory())
        self.registry = PluginRegistry(journal=ConnectionJournal(Path(directory) / "events.jsonl"))
        PluginHub.configure(self.registry, asyncio.get_running_loop())
        PluginHub._connections = {}
        PluginHub._pending = {}
        self.hub = PluginHub.__new__(PluginHub)

    async def asyncTearDown(self):
        from transport.plugin_hub import PluginHub

        PluginHub._connections = {}
        PluginHub._pending = {}
        self.registry.close_journal()

    async def test_connect_is_recorded_only_after_registered_ack(self):
        from test_register_hardening import FakeWebSocket
        from transport.models import RegisterMessage

        await self.hub._handle_register(FakeWebSocket(), RegisterMessage(project_name="P", project_hash="hash-a"))
        with self.assertRaises(ConnectionError):
            await self.hub._handle_register(FakeWebSocket(fail_send=True), RegisterMessage(project_name="P", project_hash="hash-b"))

        connected = await self.registry.connection_events("hash-a")
        failed = await self.registry.connection_events("hash-b")
        assert [event["event"] for event in connected["events"]] == ["connected"]
        assert [event["event"] for event in failed["events"]] == ["disconnected"]
        assert failed["events"][0]["reason"] == "registration_ack_failed"


def test_pytest_runs_never_attach_the_real_file_handler(tmp_path, monkeypatch):
    """A test run must not append fake instances to the operator's rotating log."""
    import logging

    from core.logging_config import add_rotating_file_handler

    monkeypatch.setenv("UNITY_MCP_LOG_DIR", str(tmp_path / "logs"))
    logger = logging.getLogger("unity-mcp-log-guard-probe")
    logger.handlers.clear()
    try:
        path = add_rotating_file_handler(
            logger, "server.log", level=logging.INFO, log_format="%(message)s"
        )
        assert path == tmp_path / "logs" / "server.log"
        assert logger.handlers == []
    finally:
        logger.handlers.clear()
