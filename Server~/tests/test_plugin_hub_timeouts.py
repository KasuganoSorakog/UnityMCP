import asyncio
import sys
import unittest
from pathlib import Path
from types import ModuleType


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

core_config = ModuleType("core.config")
core_config.config = type("Config", (), {"reload_retry_ms": 250})()
sys.modules["core.config"] = core_config

models_models = ModuleType("models.models")
from pydantic import BaseModel


class MCPResponse(BaseModel):
    success: bool = True
    error: str | None = None
    hint: str | None = None
    data: dict | None = None
    code: str | None = None
    category: str | None = None
    severity: str | None = None
    retryable: bool | None = None
    retry_after_ms: int | None = None


class ToolDefinitionModel(BaseModel):
    name: str


models_models.MCPResponse = MCPResponse
models_models.ToolDefinitionModel = ToolDefinitionModel
sys.modules["models.models"] = models_models

from transport.plugin_hub import PluginDisconnectedError, PluginHub


class PluginHubTimeoutTests(unittest.TestCase):
    def test_read_console_is_not_a_two_second_fast_fail_command(self):
        self.assertNotIn("read_console", PluginHub._FAST_FAIL_COMMANDS)

    def test_read_console_has_a_dedicated_timeout_budget(self):
        self.assertGreaterEqual(PluginHub.READ_CONSOLE_TIMEOUT, 10.0)


class PluginHubDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_session_is_retryable_during_reconnect_window(self):
        class EmptyRegistry:
            async def get_session_id_by_hash(self, _project_hash):
                return None

            async def find_hashes_by_prefix(self, _prefix):
                return []

            async def list_sessions(self):
                return {}

        previous_registry = PluginHub._registry
        PluginHub._registry = EmptyRegistry()
        try:
            response = await PluginHub.send_command_for_instance(
                "WZRY_VR@project-hash",
                "get_editor_state",
                {},
            )
        finally:
            PluginHub._registry = previous_registry

        self.assertFalse(response["success"])
        self.assertEqual(response["code"], "no_unity_session")
        self.assertTrue(response["retryable"])
        self.assertEqual(response["retry_after_ms"], 2000)
        self.assertEqual(response["hint"], "retry")

    async def test_inflight_disconnect_is_retryable(self):
        class FakeWebSocket:
            async def send_json(self, _payload):
                return None

        PluginHub._lock = asyncio.Lock()
        PluginHub._pending = {}
        PluginHub._connections = {"session-a": FakeWebSocket()}

        task = asyncio.create_task(
            PluginHub.send_command("session-a", "manage_scene", {})
        )
        await asyncio.sleep(0)
        pending = next(iter(PluginHub._pending.values()))
        pending["future"].set_exception(PluginDisconnectedError("session disconnected"))

        response = await task

        self.assertFalse(response["success"])
        self.assertEqual(response["code"], "unity_session_disconnected")
        self.assertEqual(response["category"], "session")
        self.assertTrue(response["retryable"])
        self.assertEqual(response["retry_after_ms"], 2000)
        self.assertEqual(response["hint"], "retry")


if __name__ == "__main__":
    unittest.main()
