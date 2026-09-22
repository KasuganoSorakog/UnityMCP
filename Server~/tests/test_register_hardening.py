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
    def __init__(self, fail_send=False, hang_send=False, send_gate=None, send_started=None):
        self.sent: list[dict] = []
        self.closed_codes: list[int] = []
        self.fail_send = fail_send
        self.hang_send = hang_send
        # Optional asyncio.Event gate: send_json blocks until the gate is set
        # (deterministic "stuck peer" for concurrency tests).
        self.send_gate = send_gate
        self.send_started = send_started

    async def send_json(self, payload):
        if self.send_started is not None:
            self.send_started.set()
        if self.send_gate is not None:
            await self.send_gate.wait()
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

    async def test_register_locks_sharded_by_hash_stuck_peer_does_not_block_others(self):
        # hash-a's RegisteredMessage send is stuck; hash-b's registration must
        # still complete (sharded locks), and hash-a finishes once unblocked.
        gate_a = asyncio.Event()
        send_started_a = asyncio.Event()
        ws_a = FakeWebSocket(send_gate=gate_a, send_started=send_started_a)
        task_a = asyncio.create_task(self.hub._handle_register(
            ws_a, RegisterMessage(project_name="A", project_hash="hash-a")))
        await asyncio.wait_for(send_started_a.wait(), 1.0)

        ws_b = FakeWebSocket()
        await asyncio.wait_for(
            self.hub._handle_register(
                ws_b, RegisterMessage(project_name="B", project_hash="hash-b")),
            1.0,
        )
        self.assertIsNotNone(
            await self.registry.get_session_id_by_hash("hash-b"))
        # hash-a is still stuck inside its gated send.
        self.assertFalse(task_a.done())

        gate_a.set()
        await asyncio.wait_for(task_a, 1.0)
        self.assertIsNotNone(
            await self.registry.get_session_id_by_hash("hash-a"))

    async def test_register_lock_same_hash_stays_serialized(self):
        # Two concurrent registrations for the SAME hash: the second must wait
        # for the first's full register+ack sequence before superseding it.
        gate_first = asyncio.Event()
        send_started_first = asyncio.Event()
        ws1 = FakeWebSocket(send_gate=gate_first,
                            send_started=send_started_first)
        task1 = asyncio.create_task(self.hub._handle_register(
            ws1, RegisterMessage(project_name="P", project_hash="hash-x")))
        await asyncio.wait_for(send_started_first.wait(), 1.0)

        ws2 = FakeWebSocket()
        task2 = asyncio.create_task(self.hub._handle_register(
            ws2, RegisterMessage(project_name="P", project_hash="hash-x")))
        # Give task2 every chance to run: it must remain blocked on the
        # per-hash lock while task1 is stuck.
        await asyncio.sleep(0.1)
        self.assertFalse(task2.done())

        gate_first.set()
        await asyncio.wait_for(task1, 1.0)
        await asyncio.wait_for(task2, 1.0)
        # Serialized outcome: exactly one surviving session (task2's), and
        # task1's socket was superseded afterwards, never 4408-ed.
        sessions = await self.registry.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(ws1.closed_codes,
                         [PluginHub.CLOSE_CODE_SESSION_SUPERSEDED])

    async def test_orphan_sweep_skips_session_marked_eviction_in_progress(self):
        ws = FakeWebSocket()
        await self.hub._handle_register(
            ws, RegisterMessage(project_name="P", project_hash="hash-o"))
        sid = await self.registry.get_session_id_by_hash("hash-o")

        # Simulate the supersede race window: the replacement's register() has
        # already dropped the old record, but the eviction (notification +
        # close) has not finished yet, so the session carries the mark.
        await self.registry.unregister(sid)
        PluginHub._eviction_in_progress.add(sid)
        try:
            await PluginHub.sweep_stale_sessions_once()
            # Not closed with 4408 and still connected: the old client must get
            # its session_superseded notification from the eviction instead.
            self.assertEqual(ws.closed_codes, [])
            self.assertIs(PluginHub._connections.get(sid), ws)
        finally:
            PluginHub._eviction_in_progress.discard(sid)

        # Once the eviction is over (mark gone), the orphan pass cleans up.
        await PluginHub.sweep_stale_sessions_once()
        self.assertEqual(ws.closed_codes, [PluginHub.CLOSE_CODE_STALE_SESSION])
        self.assertNotIn(sid, PluginHub._connections)

    async def test_supersede_eviction_clears_mark_and_notifies_old_client(self):
        old_ws = FakeWebSocket()
        await self.hub._handle_register(
            old_ws, RegisterMessage(project_name="P", project_hash="hash-e"))
        old_sid = await self.registry.get_session_id_by_hash("hash-e")

        new_ws = FakeWebSocket()
        await self.hub._handle_register(
            new_ws, RegisterMessage(project_name="P", project_hash="hash-e"))

        # After registration completes, the eviction ran to completion: mark
        # removed, old client notified and closed with 4409 (not 4408).
        self.assertNotIn(old_sid, PluginHub._eviction_in_progress)
        self.assertEqual(old_ws.closed_codes,
                         [PluginHub.CLOSE_CODE_SESSION_SUPERSEDED])
        superseded = [
            m for m in old_ws.sent if m.get("type") == "session_superseded"]
        self.assertEqual(len(superseded), 1)

    async def test_register_locks_pruned_for_inactive_hashes(self):
        ws = FakeWebSocket()
        await self.hub._handle_register(
            ws, RegisterMessage(project_name="P", project_hash="hash-p"))
        self.assertIn("hash-p", PluginHub._register_locks)

        sid = await self.registry.get_session_id_by_hash("hash-p")
        await PluginHub._cleanup_session_locked(sid)
        await PluginHub.sweep_stale_sessions_once()
        # No live session for the hash and the lock is free: pruned.
        self.assertNotIn("hash-p", PluginHub._register_locks)


if __name__ == "__main__":
    unittest.main()
