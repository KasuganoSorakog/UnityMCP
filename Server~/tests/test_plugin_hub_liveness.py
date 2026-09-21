import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone

from transport.models import RegisterMessage
from transport.plugin_hub import (
    NoUnitySessionError,
    PluginDisconnectedError,
    PluginHub,
)
from transport.plugin_registry import PluginRegistry


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed_codes: list[int] = []
        self.close_hangs = False

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000):
        if self.close_hangs:
            # Simulate a half-open TCP connection where close never completes.
            await asyncio.sleep(3600)
            return
        self.closed_codes.append(code)


class PluginHubLivenessTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = PluginRegistry()
        PluginHub.configure(self.registry, asyncio.get_running_loop())
        PluginHub._connections = {}
        PluginHub._pending = {}
        # Bypass WebSocketEndpoint.__init__ (requires a real ASGI scope);
        # the handlers under test only rely on class-level state.
        self.hub = PluginHub.__new__(PluginHub)

    async def asyncTearDown(self):
        await PluginHub.stop_session_sweeper()
        PluginHub._connections = {}
        PluginHub._pending = {}

    async def _register(self, session_id, project_hash, project_name="Project"):
        return await self.registry.register(
            session_id,
            project_name,
            project_hash,
            "2022.3",
        )


class LivenessTouchTests(PluginHubLivenessTestBase):
    async def test_pong_with_session_id_refreshes_last_seen(self):
        session = await self._register("s1", "hash-aaa")
        websocket = FakeWebSocket()
        PluginHub._connections["s1"] = websocket
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        session.last_seen = old

        await self.hub.on_receive(websocket, {"type": "pong", "session_id": "s1"})

        self.assertGreater(session.last_seen, old)

    async def test_pong_without_session_id_touches_via_websocket_fallback(self):
        session = await self._register("s1", "hash-aaa")
        websocket = FakeWebSocket()
        PluginHub._connections["s1"] = websocket
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        session.last_seen = old

        # Legacy clients send pong without a session_id.
        await self.hub.on_receive(websocket, {"type": "pong"})

        self.assertGreater(session.last_seen, old)

    async def test_any_inbound_message_touches_session(self):
        session = await self._register("s1", "hash-aaa")
        websocket = FakeWebSocket()
        PluginHub._connections["s1"] = websocket
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        session.last_seen = old

        await self.hub.on_receive(websocket, {"type": "unknown_future_message"})

        self.assertGreater(session.last_seen, old)

    async def test_touch_does_not_change_connected_at(self):
        session = await self._register("s1", "hash-aaa")
        websocket = FakeWebSocket()
        PluginHub._connections["s1"] = websocket
        original_connected_at = session.connected_at

        await self.hub.on_receive(websocket, {"type": "pong"})

        self.assertEqual(session.connected_at, original_connected_at)


class SupersedeTests(PluginHubLivenessTestBase):
    async def test_duplicate_hash_registration_supersedes_old_connection(self):
        old_websocket = FakeWebSocket()
        await self.hub._handle_register(
            old_websocket,
            RegisterMessage(project_name="Project", project_hash="hash-x"),
        )
        old_session_id = await self.registry.get_session_id_by_hash("hash-x")
        self.assertIsNotNone(old_session_id)
        self.assertIn(old_session_id, PluginHub._connections)

        new_websocket = FakeWebSocket()
        await self.hub._handle_register(
            new_websocket,
            RegisterMessage(project_name="Project", project_hash="hash-x"),
        )

        superseded = [
            message for message in old_websocket.sent
            if message.get("type") == "session_superseded"
        ]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["reason"], "duplicate_project_hash")
        self.assertEqual(superseded[0]["project_hash"], "hash-x")
        self.assertEqual(
            old_websocket.closed_codes, [PluginHub.CLOSE_CODE_SESSION_SUPERSEDED])

        new_session_id = await self.registry.get_session_id_by_hash("hash-x")
        self.assertIsNotNone(new_session_id)
        self.assertNotEqual(new_session_id, old_session_id)

        sessions = await self.registry.list_sessions()
        self.assertNotIn(old_session_id, sessions)
        self.assertIn(new_session_id, sessions)

        self.assertNotIn(old_session_id, PluginHub._connections)
        self.assertIs(PluginHub._connections[new_session_id], new_websocket)

    async def test_reregister_without_live_connection_does_not_close_anything(self):
        # Stale registry record without a websocket: register() replaces it
        # silently, no superseded notification is sent anywhere.
        await self._register("s-ghost", "hash-ghost")

        websocket = FakeWebSocket()
        await self.hub._handle_register(
            websocket,
            RegisterMessage(project_name="Project", project_hash="hash-ghost"),
        )

        superseded = [
            message for message in websocket.sent
            if message.get("type") == "session_superseded"
        ]
        self.assertEqual(superseded, [])
        self.assertEqual(websocket.closed_codes, [])
        new_session_id = await self.registry.get_session_id_by_hash("hash-ghost")
        sessions = await self.registry.list_sessions()
        self.assertEqual(set(sessions.keys()), {new_session_id})


class SweeperTests(PluginHubLivenessTestBase):
    async def test_sweeper_evicts_stale_session_and_fails_inflight_commands(self):
        session = await self._register("s-stale", "hash-stale", "StaleProject")
        websocket = FakeWebSocket()
        PluginHub._connections["s-stale"] = websocket
        session.last_seen = datetime.now(timezone.utc) - timedelta(seconds=3600)

        future = asyncio.get_running_loop().create_future()
        PluginHub._pending["cmd-1"] = {"future": future, "session_id": "s-stale"}

        evicted = await PluginHub.sweep_stale_sessions_once()

        self.assertEqual(evicted, 1)
        self.assertEqual(await self.registry.list_sessions(), {})
        self.assertNotIn("s-stale", PluginHub._connections)
        self.assertIn(PluginHub.CLOSE_CODE_STALE_SESSION, websocket.closed_codes)
        self.assertTrue(future.done())
        with self.assertRaises(PluginDisconnectedError):
            future.result()

    async def test_sweeper_keeps_fresh_sessions(self):
        await self._register("s-fresh", "hash-fresh")
        websocket = FakeWebSocket()
        PluginHub._connections["s-fresh"] = websocket

        evicted = await PluginHub.sweep_stale_sessions_once()

        self.assertEqual(evicted, 0)
        self.assertIn("s-fresh", await self.registry.list_sessions())
        self.assertIn("s-fresh", PluginHub._connections)
        self.assertEqual(websocket.closed_codes, [])

    async def test_sweeper_skips_session_refreshed_after_snapshot(self):
        session = await self._register("s-race", "hash-race")
        PluginHub._connections["s-race"] = FakeWebSocket()
        session.last_seen = datetime.now(timezone.utc) - timedelta(seconds=3600)

        original_list = self.registry.list_stale_sessions

        async def list_then_refresh(threshold):
            snapshot = await original_list(threshold)
            # Simulate a pong arriving after the stale snapshot was taken.
            await self.registry.touch("s-race")
            return snapshot

        self.registry.list_stale_sessions = list_then_refresh
        try:
            evicted = await PluginHub.sweep_stale_sessions_once()
        finally:
            self.registry.list_stale_sessions = original_list

        self.assertEqual(evicted, 0)
        self.assertIn("s-race", await self.registry.list_sessions())
        self.assertIn("s-race", PluginHub._connections)

    async def test_sweeper_tolerates_hanging_close(self):
        session = await self._register("s-hang", "hash-hang")
        websocket = FakeWebSocket()
        websocket.close_hangs = True
        PluginHub._connections["s-hang"] = websocket
        session.last_seen = datetime.now(timezone.utc) - timedelta(seconds=3600)

        evicted = await PluginHub.sweep_stale_sessions_once()

        self.assertEqual(evicted, 1)
        self.assertEqual(await self.registry.list_sessions(), {})
        self.assertNotIn("s-hang", PluginHub._connections)

    async def test_sweeper_start_and_stop_lifecycle(self):
        PluginHub.start_session_sweeper()
        task = PluginHub._sweeper_task
        self.assertIsNotNone(task)
        self.assertFalse(task.done())

        await PluginHub.stop_session_sweeper()

        self.assertIsNone(PluginHub._sweeper_task)
        self.assertTrue(task.done())


class HashPrefixResolutionTests(PluginHubLivenessTestBase):
    async def test_exact_hash_still_resolves(self):
        await self._register("s1", "abcdef0123456789", "ProjectA")

        session_id = await PluginHub._resolve_session_id("abcdef0123456789")

        self.assertEqual(session_id, "s1")

    async def test_unique_hash_prefix_resolves(self):
        await self._register("s1", "abcdef0123456789", "ProjectA")

        session_id = await PluginHub._resolve_session_id("abcdef01")
        self.assertEqual(session_id, "s1")

        session_id = await PluginHub._resolve_session_id("SomeName@abcdef01")
        self.assertEqual(session_id, "s1")

    async def test_ambiguous_hash_prefix_raises_with_candidates(self):
        await self._register("s1", "abcdef0123456789", "ProjectA")
        await self._register("s2", "abcdef9876543210", "ProjectB")

        with self.assertRaises(RuntimeError) as ctx:
            await PluginHub._resolve_session_id("abcdef")

        message = str(ctx.exception)
        self.assertIn("ambiguous", message)
        self.assertIn("ProjectA@abcdef0123456789", message)
        self.assertIn("ProjectB@abcdef9876543210", message)

    async def test_unknown_hash_prefix_raises_with_requested_hash(self):
        await self._register("s1", "abcdef0123456789", "ProjectA")

        with self.assertRaises(NoUnitySessionError) as ctx:
            await PluginHub._resolve_session_id("deadbeef")

        self.assertIn("deadbeef", str(ctx.exception))

    async def test_full_length_unknown_hash_raises_without_prefix_search(self):
        await self._register("s1", "abcdef0123456789", "ProjectA")

        with self.assertRaises(NoUnitySessionError) as ctx:
            await PluginHub._resolve_session_id("0000000000000000")

        self.assertIn("0000000000000000", str(ctx.exception))


class CommandTimeoutCancelTests(PluginHubLivenessTestBase):
    async def _send_command_with_late_outcome(
        self,
        *,
        session_id="s1",
        project_hash="hash-cmd",
        command_type="manage_scene",
        grace_env="0.1",
        late_result=None,
        disconnect_during_grace=False,
    ):
        """Drive send_command past its first timeout, then resolve the grace
        period in the requested way, and return the response."""
        await self._register(session_id, project_hash)
        websocket = FakeWebSocket()
        PluginHub._connections[session_id] = websocket

        original_timeout = PluginHub.COMMAND_TIMEOUT
        PluginHub.COMMAND_TIMEOUT = 0.1
        os.environ["UNITY_MCP_CANCEL_ACK_GRACE_SECONDS"] = grace_env
        try:
            task = asyncio.create_task(
                PluginHub.send_command(session_id, command_type, {}))
            # Wait until the first timeout fired and the cancel was sent.
            cancel_message = await self._wait_for_sent(websocket, "cancel")

            if disconnect_during_grace:
                await self.hub.on_disconnect(websocket, 4408)
            elif late_result is not None:
                # Late acknowledgement must come from the owning connection.
                await self.hub.on_receive(websocket, {
                    "type": "command_result",
                    "id": cancel_message["id"],
                    "result": late_result,
                })
            return await task
        finally:
            PluginHub.COMMAND_TIMEOUT = original_timeout
            os.environ.pop("UNITY_MCP_CANCEL_ACK_GRACE_SECONDS", None)

    async def _wait_for_sent(self, websocket, message_type, timeout=2.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for message in websocket.sent:
                if message.get("type") == message_type:
                    return message
            await asyncio.sleep(0.01)
        self.fail(f"message of type {message_type!r} was not sent")

    async def test_regular_command_timeout_sends_cancel_and_reports_unknown_state(self):
        response = await self._send_command_with_late_outcome(
            late_result=None, grace_env="0.1")

        self.assertFalse(response["success"])
        self.assertEqual(response["code"], "unity_execution_state_unknown")
        self.assertEqual(response["category"], "timeout")
        self.assertTrue(response["retryable"])
        self.assertEqual(response["hint"], "verify_state_before_retry")

    async def test_grace_period_cancelled_before_execution_is_safe_retry(self):
        response = await self._send_command_with_late_outcome(
            grace_env="2.0",
            late_result={
                "status": "error",
                "code": "cancelled_before_execution",
                "error": "命令未执行（排队中被取消/超时），可安全重试",
            },
        )

        self.assertFalse(response["success"])
        self.assertEqual(response["code"], "unity_command_timeout")
        self.assertEqual(response["category"], "timeout")
        self.assertTrue(response["retryable"])
        self.assertEqual(response["retry_after_ms"], 1000)
        self.assertEqual(response["hint"], "retry")
        self.assertIn("可安全重试", response["error"])

    async def test_grace_period_execution_state_unknown_is_not_safe_retry(self):
        response = await self._send_command_with_late_outcome(
            grace_env="2.0",
            late_result={
                "status": "error",
                "code": "execution_state_unknown",
                "error": "命令已在 Unity 主线程开始执行后超时/被取消",
            },
        )

        self.assertFalse(response["success"])
        self.assertEqual(response["code"], "unity_execution_state_unknown")
        self.assertEqual(response["category"], "timeout")
        self.assertTrue(response["retryable"])
        self.assertEqual(response["hint"], "verify_state_before_retry")

    async def test_grace_period_disconnect_reports_unknown_state(self):
        response = await self._send_command_with_late_outcome(
            grace_env="2.0",
            disconnect_during_grace=True,
        )

        self.assertFalse(response["success"])
        self.assertEqual(response["code"], "unity_execution_state_unknown")
        self.assertEqual(response["hint"], "verify_state_before_retry")

    async def test_grace_period_genuine_late_result_is_returned(self):
        late_payload = {"status": "success", "result": {"ok": True}}
        response = await self._send_command_with_late_outcome(
            grace_env="2.0",
            late_result=late_payload,
        )

        self.assertEqual(response, late_payload)

    async def test_fast_command_timeout_skips_grace_period(self):
        await self._register("s1", "hash-fast")
        websocket = FakeWebSocket()
        PluginHub._connections["s1"] = websocket

        original_timeout = PluginHub.FAST_FAIL_TIMEOUT
        PluginHub.FAST_FAIL_TIMEOUT = 0.05
        os.environ["UNITY_MCP_CANCEL_ACK_GRACE_SECONDS"] = "5.0"
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            response = await PluginHub.send_command("s1", "ping", {})
        finally:
            PluginHub.FAST_FAIL_TIMEOUT = original_timeout
            os.environ.pop("UNITY_MCP_CANCEL_ACK_GRACE_SECONDS", None)
        elapsed = loop.time() - started

        self.assertFalse(response["success"])
        self.assertEqual(response["code"], "unity_fast_command_timeout")
        # Read-only fast commands must not pay the grace-period wait.
        self.assertLess(elapsed, 2.0)
        cancel_messages = [
            message for message in websocket.sent
            if message.get("type") == "cancel"
        ]
        self.assertEqual(len(cancel_messages), 1)

    async def test_read_console_timeout_sends_cancel_and_returns_classified(self):
        await self._register("s1", "hash-console")
        websocket = FakeWebSocket()
        PluginHub._connections["s1"] = websocket

        original_timeout = PluginHub.READ_CONSOLE_TIMEOUT
        PluginHub.READ_CONSOLE_TIMEOUT = 0.05
        try:
            response = await PluginHub.send_command("s1", "read_console", {})
        finally:
            PluginHub.READ_CONSOLE_TIMEOUT = original_timeout

        self.assertFalse(response["success"])
        self.assertEqual(response["code"], "unity_console_timeout")
        cancel_messages = [
            message for message in websocket.sent
            if message.get("type") == "cancel"
        ]
        self.assertEqual(len(cancel_messages), 1)


class SourceValidationTests(PluginHubLivenessTestBase):
    async def test_command_result_from_wrong_connection_is_discarded(self):
        await self._register("s-owner", "hash-owner")
        await self._register("s-forger", "hash-forger")
        owner_ws = FakeWebSocket()
        forger_ws = FakeWebSocket()
        PluginHub._connections["s-owner"] = owner_ws
        PluginHub._connections["s-forger"] = forger_ws

        future = asyncio.get_running_loop().create_future()
        PluginHub._pending["cmd-1"] = {"future": future, "session_id": "s-owner"}

        # A different connection must not complete the owner's command.
        await self.hub.on_receive(forger_ws, {
            "type": "command_result",
            "id": "cmd-1",
            "result": {"status": "success", "forged": True},
        })
        self.assertFalse(future.done())

        # The owning connection completes it normally.
        await self.hub.on_receive(owner_ws, {
            "type": "command_result",
            "id": "cmd-1",
            "result": {"status": "success"},
        })
        self.assertTrue(future.done())
        self.assertEqual(future.result(), {"status": "success"})

    async def test_command_result_from_unregistered_connection_is_discarded(self):
        await self._register("s-owner", "hash-owner")
        PluginHub._connections["s-owner"] = FakeWebSocket()
        stranger_ws = FakeWebSocket()  # never registered

        future = asyncio.get_running_loop().create_future()
        PluginHub._pending["cmd-1"] = {"future": future, "session_id": "s-owner"}

        await self.hub.on_receive(stranger_ws, {
            "type": "command_result",
            "id": "cmd-1",
            "result": {"status": "success"},
        })

        self.assertFalse(future.done())

    async def test_pong_with_foreign_session_id_only_touches_own_session(self):
        own = await self._register("s-own", "hash-own")
        other = await self._register("s-other", "hash-other")
        own_ws = FakeWebSocket()
        PluginHub._connections["s-own"] = own_ws
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        own.last_seen = old
        other.last_seen = old

        # The payload claims another session's id; only the connection's own
        # session may be refreshed.
        await self.hub.on_receive(
            own_ws, {"type": "pong", "session_id": "s-other"})

        self.assertGreater(own.last_seen, old)
        self.assertEqual(other.last_seen, old)

    async def test_pong_from_unregistered_connection_is_ignored(self):
        stranger_ws = FakeWebSocket()
        session = await self._register("s1", "hash-aaa")
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        session.last_seen = old

        await self.hub.on_receive(
            stranger_ws, {"type": "pong", "session_id": "s1"})

        self.assertEqual(session.last_seen, old)


class InstanceBusyResponseTests(PluginHubLivenessTestBase):
    async def test_instance_busy_response_is_retryable(self):
        await self._register("s-busy", "hash-busy")
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_send_command(cls, session_id, command_type, params):
            started.set()
            await release.wait()
            return {"status": "success"}

        original_send_command = PluginHub.send_command
        PluginHub.send_command = classmethod(fake_send_command)
        os.environ["UNITY_MCP_FAST_QUEUE_TIMEOUT_SECONDS"] = "0.05"
        first_task = None
        try:
            first_task = asyncio.create_task(
                PluginHub.send_command_for_instance("hash-busy", "ping", {}))
            await started.wait()
            response = await PluginHub.send_command_for_instance(
                "hash-busy", "ping", {})
        finally:
            PluginHub.send_command = original_send_command
            os.environ.pop("UNITY_MCP_FAST_QUEUE_TIMEOUT_SECONDS", None)
            release.set()
            if first_task is not None:
                await first_task

        self.assertFalse(response["success"])
        self.assertEqual(response["code"], "unity_instance_busy")
        self.assertTrue(response["retryable"])
        self.assertEqual(response["retry_after_ms"], 5000)


if __name__ == "__main__":
    unittest.main()
