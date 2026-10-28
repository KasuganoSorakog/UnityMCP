import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.tools.refresh_unity import _wait_for_refresh_completion, refresh_unity


class RefreshUnityToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_execution_after_compile_recovers_without_resending(self):
        ctx = SimpleNamespace(unity_instance="FortuneDao@test-hash")
        send = AsyncMock(return_value={
            "success": False,
            "code": "unity_execution_state_unknown",
            "category": "timeout",
            "error": "The compile request interrupted the old Unity session.",
        })
        recovery = AsyncMock(return_value={
            "completed": True,
            "domain_reload_observed": True,
            "busy_observed": True,
        })
        with (
            patch("services.tools.refresh_unity._read_editor_state", new=AsyncMock(return_value=None)),
            patch("services.tools.refresh_unity.unity_transport.send_with_unity_instance", send),
            patch("services.tools.refresh_unity._wait_for_refresh_completion", recovery),
        ):
            result = await refresh_unity(ctx, compile="request", unity_instance="FortuneDao@test-hash")

        self.assertTrue(result.success, result)
        self.assertEqual(result.hint, "Unity refresh completed; editor is ready.")
        self.assertTrue(result.data["domain_reload_observed"])
        self.assertFalse(result.data["safe_to_retry_refresh"])
        send.assert_awaited_once()
        recovery.assert_awaited_once_with(
            ctx, "FortuneDao@test-hash", None, require_reload_evidence=True,
        )

    async def test_unknown_execution_does_not_claim_success_without_reload(self):
        recovery = AsyncMock(return_value={
            "completed": False,
            "domain_reload_observed": False,
            "busy_observed": False,
        })
        with (
            patch("services.tools.refresh_unity._read_editor_state", new=AsyncMock(return_value=None)),
            patch("services.tools.refresh_unity.unity_transport.send_with_unity_instance", new=AsyncMock(return_value={
                "success": False, "code": "unity_execution_state_unknown",
            })),
            patch("services.tools.refresh_unity._wait_for_refresh_completion", recovery),
        ):
            result = await refresh_unity(SimpleNamespace(unity_instance="FortuneDao@test-hash"), compile="request")

        self.assertFalse(result.success)
        self.assertEqual(result.code, "refresh_recovery_timeout")
        self.assertFalse(result.data["safe_to_retry_refresh"])

    async def test_successful_compile_returns_final_ready_state(self):
        recovery = AsyncMock(return_value={
            "completed": True,
            "domain_reload_observed": True,
            "busy_observed": True,
        })
        with (
            patch("services.tools.refresh_unity._read_editor_state", new=AsyncMock(return_value=None)),
            patch("services.tools.refresh_unity.unity_transport.send_with_unity_instance", new=AsyncMock(return_value={
                "success": True, "message": "Refresh requested.",
                "data": {
                    "resulting_state": "compiling", "compile_requested": True,
                    "hint": "If Unity enters compilation/domain reload, poll editor_state until ready_for_tools is true.",
                },
            })),
            patch("services.tools.refresh_unity._wait_for_refresh_completion", recovery),
        ):
            result = await refresh_unity(SimpleNamespace(unity_instance="FortuneDao@test-hash"), compile="request")

        self.assertTrue(result.success, result)
        self.assertEqual(result.hint, "Unity refresh completed; editor is ready.")
        self.assertNotIn("hint", result.data)
        self.assertEqual(result.data["resulting_state"], "idle")
        self.assertTrue(result.data["domain_reload_observed"])
        self.assertTrue(result.data["recovered_after_session_disconnect"])

    async def test_busy_ready_state_does_not_replace_reload_evidence(self):
        state = {
            "advice": {"ready_for_tools": True},
            "compilation": {"is_compiling": True, "last_domain_reload_after_unix_ms": 10},
        }
        with (
            patch("services.tools.refresh_unity._read_editor_state", new=AsyncMock(return_value=state)),
            patch("services.tools.refresh_unity.RELOAD_RECOVERY_TIMEOUT_SECONDS", 0),
        ):
            result = await _wait_for_refresh_completion(None, None, 10, require_reload_evidence=True)

        self.assertFalse(result["completed"])
        self.assertTrue(result["busy_observed"])
        self.assertFalse(result["domain_reload_observed"])


if __name__ == "__main__":
    unittest.main()
