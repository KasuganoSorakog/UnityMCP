import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from models.models import MCPResponse
from services.tools.manage_editor import _send, manage_editor


class FakeContext(SimpleNamespace):
    def __init__(self, unity_instance="FortuneDao@test-hash"):
        super().__init__(unity_instance=unity_instance)

    def get_state(self, key):
        return getattr(self, key, None)


class ManageEditorToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_no_session_and_adopts_the_active_operation_id(self):
        requested_id = "19b184d2160040e387a2f319dd055445"
        active_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        sent = []

        async def fake_send(instance, params):
            sent.append(params)
            if len(sent) == 1:
                return {"success": False, "code": "no_unity_session"}
            if len(sent) == 2:
                return {
                    "success": True, "_mcp_status": "pending",
                    "data": {"operationId": active_id},
                }
            return {
                "success": True,
                "data": {"operationId": active_id, "isPlaying": True, "completed": True},
            }

        with (
            patch("services.tools.manage_editor._send", side_effect=fake_send),
            patch("services.tools.manage_editor.asyncio.sleep", new=AsyncMock()),
        ):
            result = await manage_editor(
                FakeContext(), action="play", operation_id=requested_id,
            )

        self.assertTrue(result["success"], result)
        self.assertEqual(sent[1]["operationId"], requested_id)
        self.assertEqual(sent[2]["operationId"], active_id)
        self.assertEqual(result["data"]["operationId"], active_id)

    async def test_operation_status_can_retrieve_the_current_operation_after_reconnect(self):
        with patch(
            "services.tools.manage_editor._send",
            new=AsyncMock(return_value={
                "success": True, "_mcp_status": "pending",
                "data": {"operationId": "19b184d2160040e387a2f319dd055445"},
            }),
        ) as send:
            result = await manage_editor(FakeContext(), action="operation_status")

        self.assertTrue(result["success"])
        self.assertEqual(send.await_args.args[1]["action"], "operation_status")
        self.assertNotIn("operationId", send.await_args.args[1])

    async def test_preserves_operation_id_on_terminal_failure(self):
        operation_id = "19b184d2160040e387a2f319dd055445"
        with patch(
            "services.tools.manage_editor._send",
            new=AsyncMock(return_value={"success": False, "code": "unity_command_failed"}),
        ):
            result = await manage_editor(FakeContext(), action="stop", operation_id=operation_id)

        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["operationId"], operation_id)

    async def test_send_unwraps_pydantic_response(self):
        with patch(
            "services.tools.manage_editor.send_with_unity_instance",
            new=AsyncMock(return_value=MCPResponse(success=True, data={"isPlaying": True})),
        ):
            result = await _send("FortuneDao@test-hash", {"action": "play"})

        self.assertEqual(result["data"], {"isPlaying": True})
        self.assertNotIn("message", result)


if __name__ == "__main__":
    unittest.main()
