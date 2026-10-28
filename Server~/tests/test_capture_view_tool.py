import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.tools.capture_view import capture_view


class FakeContext(SimpleNamespace):
    def __init__(self, unity_instance="FortuneDao@test-hash"):
        super().__init__(unity_instance=unity_instance)

    def get_state(self, key):
        return getattr(self, key, None)


class CaptureViewToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_forwards_sequence_overlay_and_timeout(self):
        captured = {}

        async def fake_send(send_fn, unity_instance, command_type, params, **kwargs):
            captured.update(
                unity_instance=unity_instance,
                command_type=command_type,
                params=params,
            )
            return {"success": True, "message": "ok", "data": {"fullPath": "E:/FortuneDao/Library/MCPScreenshots/view.png"}}

        with patch("services.tools.capture_view.send_with_unity_instance", fake_send):
            result = await capture_view(
                FakeContext(),
                target="Head",
                angles=["front", "45"],
                frames=2,
                interval="0.2",
                grid="true",
                include_inactive="true",
            )

        self.assertTrue(result["success"])
        self.assertEqual(captured["unity_instance"], "FortuneDao@test-hash")
        self.assertEqual(captured["command_type"], "capture_view")
        self.assertEqual(captured["params"]["angles"], ["front", "45"])
        self.assertEqual(captured["params"]["frames"], 2)
        self.assertEqual(captured["params"]["interval"], 0.2)
        self.assertEqual(captured["params"]["timeoutSeconds"], 31)
        self.assertTrue(captured["params"]["grid"])
        self.assertTrue(captured["params"]["includeOverlay"])
        self.assertTrue(captured["params"]["includeInactive"])
        self.assertNotIn("unity_instance", captured["params"])
        self.assertTrue(result["data"]["fullPath"].endswith("view.png"))

    async def test_preset_list_does_not_send_capture_fields(self):
        captured = {}

        async def fake_send(send_fn, unity_instance, command_type, params, **kwargs):
            captured.update(command_type=command_type, params=params)
            return {"success": True, "message": "ok", "data": {"presets": []}}

        with patch("services.tools.capture_view.send_with_unity_instance", fake_send):
            result = await capture_view(FakeContext(), action="list_presets")

        self.assertTrue(result["success"])
        self.assertEqual(captured["command_type"], "capture_view")
        self.assertEqual(captured["params"]["action"], "list_presets")
        self.assertNotIn("target", captured["params"])
        self.assertNotIn("angles", captured["params"])

    async def test_rejects_conflicting_angle_inputs_before_transport(self):
        with patch("services.tools.capture_view.send_with_unity_instance") as send:
            result = await capture_view(FakeContext(), target="Head", angle="front", angles=["back"])

        self.assertFalse(result["success"])
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
