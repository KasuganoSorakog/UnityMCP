import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.state.instance_locks import InstanceBusyError, InstanceWriteLocks, is_mutating_tool
from transport.unity_instance_middleware import UnityInstanceMiddleware
from transport.plugin_registry import PluginRegistry


class InstanceWriteLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_session_conflicts_but_same_owner_can_renew_and_release(self):
        locks = InstanceWriteLocks()
        claimed = await locks.claim("hash-a", "Project@hash-a", "session-a", "task-a", 30)
        self.assertTrue(claimed["success"])
        conflict = await locks.claim("hash-a", "Project@hash-a", "session-b", "task-b", 30)
        self.assertEqual(conflict["code"], "unity_instance_busy")
        self.assertTrue(conflict["retryable"])
        self.assertEqual(conflict["data"]["lock"]["owner"], "task-a")
        self.assertNotIn("owner_key", conflict["data"]["lock"])
        renewed = await locks.claim("hash-a", "Project@hash-a", "session-a", "task-a", 60)
        self.assertTrue(renewed["success"])
        denied = await locks.release("hash-a", "session-b")
        self.assertFalse(denied["success"])
        self.assertTrue((await locks.release("hash-a", "session-a"))["data"]["released"])

    async def test_active_call_never_expires_and_different_projects_are_independent(self):
        locks = InstanceWriteLocks()
        with patch("services.state.instance_locks.time.monotonic") as clock:
            clock.return_value = 0
            await locks.claim("hash-a", "Project@hash-a", "session-a", "task-a", 10)
            clock.return_value = 5
            with self.assertRaises(InstanceBusyError):
                async with locks.hold("hash-a", "Project@hash-a", "session-b"):
                    pass
            async with locks.hold("hash-a", "Project@hash-a", "session-a"):
                clock.return_value = 9999
                status = await locks.status("hash-a")
                self.assertEqual(status["hash-a"]["active_calls"], 1)
                async with locks.hold("hash-b", "Project@hash-b", "session-b"):
                    pass
            clock.return_value = 10008
            self.assertEqual((await locks.status("hash-a"))["hash-a"]["active_calls"], 0)

    async def test_idle_expiry_and_nested_batch_classification(self):
        locks = InstanceWriteLocks()
        with patch("services.state.instance_locks.time.monotonic", return_value=0):
            await locks.claim("hash-a", "Project@hash-a", "session-a", "task-a", 10)
        with patch("services.state.instance_locks.time.monotonic", return_value=11):
            claimed = await locks.claim("hash-a", "Project@hash-a", "session-b", "task-b", 10)
            self.assertTrue(claimed["success"])
        self.assertTrue(is_mutating_tool("manage_components", {"action": "add"}))
        self.assertTrue(is_mutating_tool("read_console", {"action": "clear"}))
        self.assertFalse(is_mutating_tool("read_console", {"action": "get"}))
        self.assertFalse(is_mutating_tool("sample_runtime", {"action": "result"}))
        self.assertTrue(is_mutating_tool("sample_runtime", {"action": "sample"}))
        self.assertFalse(is_mutating_tool("manage_vfx", {"action": "vfx_list_templates"}))
        self.assertTrue(is_mutating_tool("manage_vfx", {"action": "particle_play"}))
        self.assertTrue(is_mutating_tool("batch_execute", {"commands": [
            {"tool": "read_console", "params": {"action": "get"}},
            {"tool": "manage_gameobject", "params": {"action": "create"}},
        ]}))
        self.assertFalse(is_mutating_tool("batch_execute", {"commands": [
            {"tool": "read_console", "params": {"action": "get"}},
        ]}))

    async def test_middleware_rejects_other_session_and_executes_read_tools(self):
        middleware = UnityInstanceMiddleware()
        contexts = []
        for client_id, name, arguments in (
            ("client-a", "manage_scene", {"action": "save"}),
            ("client-b", "manage_scene", {"action": "save"}),
            ("client-b", "read_console", {"action": "get"}),
            ("client-b", "manage_scene", {"action": "save", "unity_instance": "Other@hash-b"}),
        ):
            context = SimpleNamespace(
                fastmcp_context=SimpleNamespace(
                    client_id=client_id, session_id=None, request_context=None,
                    get_state=lambda _: "Project@hash-a",
                ),
                message=SimpleNamespace(name=name, arguments=arguments),
            )
            contexts.append(context)
            middleware._active_by_key[f"client:{client_id}"] = "Project@hash-a"

        executed = []
        async def inject(context):
            context.fastmcp_context.unity_instance = context.message.arguments.get(
                "unity_instance", "Project@hash-a")
        async def call_next(context):
            executed.append(context)
            return {"success": True}

        middleware._inject_unity_instance = inject
        async def resolve(instance):
            return "hash-b" if instance == "Other@hash-b" else "hash-a", instance or "Project@hash-a"
        with patch("services.state.instance_locks.resolve_lock_instance", side_effect=resolve), patch(
            "services.state.instance_locks.get_instance_write_locks", return_value=InstanceWriteLocks(),
        ):
            self.assertEqual((await middleware.on_call_tool(contexts[0], call_next))["success"], True)
            conflict = await middleware.on_call_tool(contexts[1], call_next)
            self.assertEqual(conflict.structured_content["code"], "unity_instance_busy")
            self.assertEqual(await middleware.on_call_tool(contexts[2], call_next), {"success": True})
            self.assertEqual(await middleware.on_call_tool(contexts[3], call_next), {"success": True})
        self.assertEqual(executed, [contexts[0], contexts[2], contexts[3]])


class PluginRegistryHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_disconnect_does_not_replace_new_session_history(self):
        registry = PluginRegistry()
        await registry.register("s1", "P", "hash-a", "2022.3")
        await registry.register("s2", "P", "hash-a", "2022.3")
        await registry.unregister("s1", "websocket_close_4409")
        self.assertEqual(await registry.get_session_id_by_hash("hash-a"), "s2")
        self.assertEqual(await registry.list_disconnected(), {})
        current = await registry.get_session("s2")
        self.assertEqual(current.last_disconnect_reason, "session_replaced")
        await registry.unregister("s2", "heartbeat_timeout")
        history = await registry.list_disconnected()
        self.assertEqual(history["hash-a"].last_disconnect_reason, "heartbeat_timeout")
        self.assertEqual(await registry.list_sessions(), {})

    async def test_reconnect_retains_tools_and_disconnect_reason(self):
        registry = PluginRegistry()
        first = await registry.register("s1", "P", "hash-a", "2022.3")
        first.tools["capture_view"] = SimpleNamespace(name="capture_view")
        await registry.unregister("s1", "domain_reload")
        second = await registry.register("s2", "P", "hash-a", "2022.3")
        self.assertIn("capture_view", second.tools)
        self.assertEqual(second.last_disconnect_reason, "domain_reload")
        self.assertEqual(await registry.list_disconnected(), {})
        second.tools["capture_view"] = None
        await registry.unregister("s2", "closed")
        snapshot = (await registry.list_disconnected())["hash-a"]
        self.assertIsNot(snapshot, second)


if __name__ == "__main__":
    unittest.main()
