import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.tools.preflight import _busy, preflight


def _state(is_playing: bool, is_compiling: bool = True, external_dirty: bool = True) -> SimpleNamespace:
    return SimpleNamespace(model_dump=lambda: {
        "success": True,
        "data": {
            "play_mode": {"is_playing": is_playing},
            "compilation": {"is_compiling": is_compiling},
            "assets": {"external_changes_dirty": external_dirty},
        },
    })


class PreflightBusyResponseTests(unittest.TestCase):
    def test_preflight_busy_response_is_machine_readable(self):
        response = _busy("compiling", 500).model_dump()

        self.assertFalse(response["success"])
        self.assertEqual(response["code"], "unity_busy_compiling")
        self.assertEqual(response["category"], "busy")
        self.assertEqual(response["severity"], "warning")
        self.assertTrue(response["retryable"])
        self.assertEqual(response["retry_after_ms"], 500)
        self.assertEqual(response["hint"], "retry")


class PreflightPlayModeTests(unittest.IsolatedAsyncioTestCase):
    """`preflight` no-ops under pytest by design, so each case disables that guard."""

    def setUp(self):
        patcher = patch("services.tools.preflight._in_pytest", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_play_mode_does_not_refresh_and_reports_deferred_compile(self):
        refresh = AsyncMock()
        with (
            patch("services.resources.editor_state.get_editor_state_for_instance",
                  new=AsyncMock(return_value=_state(is_playing=True))),
            patch("services.tools.refresh_unity.refresh_unity", new=refresh),
        ):
            gate = await preflight(
                SimpleNamespace(), unity_instance="FortuneDao@h",
                wait_for_no_compile=True, refresh_if_dirty=True,
            )

        refresh.assert_not_awaited()
        self.assertIsNotNone(gate)
        payload = gate.model_dump()
        self.assertEqual(payload["code"], "unity_busy_compile_deferred_until_play_exit")
        self.assertGreaterEqual(payload["retry_after_ms"], 5000)

    async def test_edit_mode_still_refreshes_when_dirty(self):
        refresh = AsyncMock()
        with (
            patch("services.resources.editor_state.get_editor_state_for_instance",
                  new=AsyncMock(return_value=_state(is_playing=False))),
            patch("services.tools.refresh_unity.refresh_unity", new=refresh),
        ):
            gate = await preflight(
                SimpleNamespace(), unity_instance="FortuneDao@h",
                wait_for_no_compile=True, refresh_if_dirty=True,
            )

        refresh.assert_awaited_once()
        self.assertEqual(gate.model_dump()["code"], "unity_busy_compiling")

    async def test_play_mode_without_pending_compile_proceeds(self):
        with patch("services.resources.editor_state.get_editor_state_for_instance",
                   new=AsyncMock(return_value=_state(is_playing=True, is_compiling=False))):
            gate = await preflight(
                SimpleNamespace(), unity_instance="FortuneDao@h",
                wait_for_no_compile=True, refresh_if_dirty=True,
            )

        self.assertIsNone(gate)


if __name__ == "__main__":
    unittest.main()
