"""Tests for the round-3 registration hardening in PluginHub._handle_register:

- failed/timed-out RegisteredMessage send rolls back the new session AND
  restores the previously superseded one (project keeps a usable session);
- a re-register on the SAME websocket cleans up the old session entry so the
  orphan sweep cannot close the live socket.
"""
import asyncio
import unittest

from transport.models import RegisterMessage
from transport.plugin_hub import PluginHub
from transport.plugin_registry import PluginRegistry


class FakeWebSocket:
    def __init__(self, fail_send=False, hang_send=False):
        self.sent: list[dict] = []
        self.closed_codes: list[int] = []
        self.fail_send = fail_send
        self.hang_send = hang_send

    async def send_json(self, payload):
        if self.hang_send:
            await asyncio.sleep(3600)
        if self.fail_send:
            raise ConnectionError("socket gone")
        self.sent.append(payload)

    async def close(self, code=1000):
        self.closed_codes.append(code)


class RegisterHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = PluginRegistry()
        PluginHub.configure(self.registry, asyncio.get_running_loop())
        PluginHub._connections = {}
        PluginHub._pending = {}
        # Bypass WebSocketEndpoint.__init__ (needs a real ASGI scope).
        self.hub = PluginHub.__new__(PluginHub)

    async def asyncTearDown(self):
        PluginHub._connections = {}
        PluginHub._pending = {}

    async def test_failed_registered_send_rolls_back_and_restores_superseded(self):
        old_ws = FakeWebSocket()
        await self.hub._handle_register(
            old_ws, RegisterMessage(project_name="P", project_hash="hash-r"))
        old_sid = await self.registry.get_session_id_by_hash("hash-r")
        self.assertIsNotNone(old_sid)

        new_ws = FakeWebSocket(fail_send=True)
        with self.assertRaises(ConnectionError):
            await self.hub._handle_register(
                new_ws, RegisterMessage(project_name="P", project_hash="hash-r"))

        # New session fully rolled back; old session restored with its socket,
        # never notified, never closed.
        sessions = await self.registry.list_sessions()
        self.assertEqual(set(sessions.keys()), {old_sid})
        self.assertIs(PluginHub._connections.get(old_sid), old_ws)
        self.assertEqual(old_ws.closed_codes, [])
        superseded = [
            m for m in old_ws.sent if m.get("type") == "session_superseded"]
        self.assertEqual(superseded, [])
        self.assertEqual(new_ws.closed_codes, [1011])

    async def test_registered_send_timeout_rolls_back(self):
        old_timeout = PluginHub.REGISTER_ACK_SEND_TIMEOUT_SECONDS
        PluginHub.REGISTER_ACK_SEND_TIMEOUT_SECONDS = 0.05
        try:
            ws = FakeWebSocket(hang_send=True)
            with self.assertRaises(asyncio.TimeoutError):
                await self.hub._handle_register(
                    ws, RegisterMessage(project_name="P", project_hash="hash-t"))
        finally:
            PluginHub.REGISTER_ACK_SEND_TIMEOUT_SECONDS = old_timeout

        sessions = await self.registry.list_sessions()
        self.assertEqual(sessions, {})
        self.assertEqual(PluginHub._connections, {})
        self.assertEqual(ws.closed_codes, [1011])

    async def test_same_websocket_reregister_survives_orphan_sweep(self):
        ws = FakeWebSocket()
        await self.hub._handle_register(
            ws, RegisterMessage(project_name="P", project_hash="hash-s"))
        first_sid = await self.registry.get_session_id_by_hash("hash-s")

        # Same connection re-registers the same hash (client retry).
        await self.hub._handle_register(
            ws, RegisterMessage(project_name="P", project_hash="hash-s"))
        second_sid = await self.registry.get_session_id_by_hash("hash-s")
        self.assertNotEqual(first_sid, second_sid)

        sessions = await self.registry.list_sessions()
        self.assertEqual(set(sessions.keys()), {second_sid})
        self.assertEqual(dict(PluginHub._connections), {second_sid: ws})

        # The orphan sweep must not close the live socket.
        await PluginHub.sweep_stale_sessions_once()
        self.assertEqual(ws.closed_codes, [])
        self.assertIs(PluginHub._connections.get(second_sid), ws)


if __name__ == "__main__":
    unittest.main()
