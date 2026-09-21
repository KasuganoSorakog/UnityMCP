import asyncio
import os
import sys
import unittest

# Other test files install stub modules under these names; drop them so the
# real transport modules are imported here.
for module_name in (
    "models.models",
    "transport.plugin_hub",
    "transport.unity_transport",
):
    sys.modules.pop(module_name, None)

from transport.plugin_hub import PluginHub
from transport.unity_transport import send_with_unity_instance


class UnityTransportTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_transport = os.environ.get("UNITY_MCP_TRANSPORT")
        os.environ["UNITY_MCP_TRANSPORT"] = "http"
        self._original_send = PluginHub.send_command_for_instance

    async def asyncTearDown(self):
        PluginHub.send_command_for_instance = self._original_send
        if self._old_transport is None:
            os.environ.pop("UNITY_MCP_TRANSPORT", None)
        else:
            os.environ["UNITY_MCP_TRANSPORT"] = self._old_transport

    async def test_command_timeout_is_retryable(self):
        async def fake_send(cls, unity_instance, command_type, params):
            raise asyncio.TimeoutError()

        PluginHub.send_command_for_instance = classmethod(fake_send)

        response = await send_with_unity_instance(None, None, "manage_scene", {})

        self.assertFalse(response["success"])
        self.assertEqual(response["code"], "unity_command_timeout")
        self.assertEqual(response["category"], "timeout")
        self.assertEqual(response["hint"], "retry")
        self.assertTrue(response["retryable"])
        # The fallback text must stay cautious: no blanket "safe to retry".
        self.assertIn("验证场景状态", response["error"])
        self.assertNotIn("safe to retry", response["error"])

    async def test_generic_error_still_requires_manual_connect(self):
        async def fake_send(cls, unity_instance, command_type, params):
            raise RuntimeError("boom")

        PluginHub.send_command_for_instance = classmethod(fake_send)

        response = await send_with_unity_instance(None, None, "manage_scene", {})

        self.assertFalse(response["success"])
        self.assertEqual(response["hint"], "manual_connect")
        self.assertFalse(response["retryable"])
        self.assertIn("boom", response["error"])


if __name__ == "__main__":
    unittest.main()
