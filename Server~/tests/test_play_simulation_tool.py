import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.tools.play_simulation import play_simulation

INSTANCE = "FortuneDao@test-hash"


class FakeContext(SimpleNamespace):
    def __init__(self):
        super().__init__(unity_instance=INSTANCE)

    def get_state(self, key):
        return getattr(self, key, None)


def editor_state(playing=False):
    return {"success": True, "data": {"editor": {"play_mode": {
        "is_playing": playing, "is_changing": False,
    }}}}


class PlaySimulationToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_orchestrates_play_entry_capture_sample_and_confirmed_stop(self):
        manage = AsyncMock(side_effect=[
            {"success": True, "data": {"isPlaying": True, "alreadyInTargetState": False}},
            {"success": True, "data": {"isPlaying": False}},
        ])
        capture = AsyncMock(return_value={"success": True, "data": {"captures": []}})
        sample = AsyncMock(return_value={"success": True, "data": {"metrics": []}})
        ready = AsyncMock(return_value={"success": True})
        with (
            patch("services.tools.play_simulation.preflight", new=AsyncMock(return_value=None)),
            patch("services.tools.play_simulation.get_editor_state_for_instance", new=AsyncMock(return_value=editor_state())),
            patch("services.tools.play_simulation.manage_editor", new=manage),
            patch("services.tools.play_simulation._invoke_entry", new=AsyncMock(return_value={"success": True})) as entry,
            patch("services.tools.play_simulation._wait_for_target", new=ready),
            patch("services.tools.play_simulation.capture_view", new=capture),
            patch("services.tools.play_simulation.sample_runtime", new=sample),
            patch("services.tools.play_simulation.asyncio.sleep", new=AsyncMock()),
        ):
            result = await play_simulation(
                FakeContext(), entry="Example.Simulation::Run", duration=2,
                capture={"angles": ["front"]}, sample=True, target="Head",
            )

        self.assertTrue(result["success"], result)
        self.assertEqual([call.kwargs["action"] for call in manage.await_args_list], ["play", "stop"])
        self.assertTrue(result["data"]["stopConfirmed"])
        self.assertEqual(result["data"]["playResult"]["data"]["isPlaying"], True)
        entry.assert_awaited_once_with(INSTANCE, "Example.Simulation::Run")
        ready.assert_awaited_once()
        self.assertEqual(capture.await_args.kwargs["unity_instance"], INSTANCE)
        self.assertEqual(sample.await_args.kwargs["unity_instance"], INSTANCE)
        self.assertEqual(sample.await_args.kwargs["duration"], 2)
        self.assertEqual(sample.await_args.kwargs["target"], "Head")

    async def test_target_readiness_failure_does_not_capture_and_still_stops(self):
        manage = AsyncMock(side_effect=[
            {"success": True, "data": {}},
            {"success": True, "data": {"isPlaying": False}},
        ])
        with (
            patch("services.tools.play_simulation.preflight", new=AsyncMock(return_value=None)),
            patch("services.tools.play_simulation.get_editor_state_for_instance", new=AsyncMock(return_value=editor_state())),
            patch("services.tools.play_simulation.manage_editor", new=manage),
            patch("services.tools.play_simulation._invoke_entry", new=AsyncMock(return_value={"success": True})),
            patch("services.tools.play_simulation._wait_for_target", new=AsyncMock(return_value={
                "success": False, "code": "simulation_target_not_ready",
            })),
            patch("services.tools.play_simulation.capture_view", new=AsyncMock()) as capture,
            patch("services.tools.play_simulation.asyncio.sleep", new=AsyncMock()),
        ):
            result = await play_simulation(
                FakeContext(), entry="Example.Simulation::Run", capture=True, target="LateTarget",
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "simulation_target_not_ready")
        self.assertTrue(result["data"]["stopConfirmed"])
        capture.assert_not_awaited()

    async def test_existing_play_session_is_not_taken_over_or_stopped(self):
        with (
            patch("services.tools.play_simulation.preflight", new=AsyncMock(return_value=None)),
            patch("services.tools.play_simulation.get_editor_state_for_instance", new=AsyncMock(return_value=editor_state(True))),
            patch("services.tools.play_simulation.manage_editor", new=AsyncMock()) as manage,
        ):
            result = await play_simulation(FakeContext(), entry="Example.Simulation::Run")

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "play_session_already_active")
        manage.assert_not_awaited()

    async def test_entry_failure_still_stops_and_returns_failure(self):
        manage = AsyncMock(side_effect=[
            {"success": True, "data": {}},
            {"success": True, "data": {"isPlaying": False}},
        ])
        with (
            patch("services.tools.play_simulation.preflight", new=AsyncMock(return_value=None)),
            patch("services.tools.play_simulation.get_editor_state_for_instance", new=AsyncMock(return_value=editor_state())),
            patch("services.tools.play_simulation.manage_editor", new=manage),
            patch("services.tools.play_simulation._invoke_entry", new=AsyncMock(return_value={"success": False, "error": "entry failed"})),
            patch("services.tools.play_simulation.asyncio.sleep", new=AsyncMock()),
        ):
            result = await play_simulation(FakeContext(), entry="Example.Simulation::Run", duration=0.1)

        self.assertFalse(result["success"])
        self.assertTrue(result["data"]["stopConfirmed"])
        self.assertEqual(manage.await_args_list[-1].kwargs["action"], "stop")

    async def test_cancellation_waits_for_stop_before_propagating(self):
        entered = asyncio.Event()

        async def invoke(*_args):
            entered.set()
            await asyncio.Event().wait()

        manage = AsyncMock(side_effect=[
            {"success": True, "data": {}},
            {"success": True, "data": {"isPlaying": False}},
        ])
        with (
            patch("services.tools.play_simulation.preflight", new=AsyncMock(return_value=None)),
            patch("services.tools.play_simulation.get_editor_state_for_instance", new=AsyncMock(return_value=editor_state())),
            patch("services.tools.play_simulation.manage_editor", new=manage),
            patch("services.tools.play_simulation._invoke_entry", side_effect=invoke),
        ):
            task = asyncio.create_task(play_simulation(FakeContext(), entry="Example.Simulation::Run"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual([call.kwargs["action"] for call in manage.await_args_list], ["play", "stop"])

    async def test_unconfirmed_stop_and_cross_instance_nested_options_fail(self):
        with (
            patch("services.tools.play_simulation.preflight", new=AsyncMock(return_value=None)),
            patch("services.tools.play_simulation.get_editor_state_for_instance", new=AsyncMock(return_value=editor_state())),
            patch("services.tools.play_simulation.manage_editor", new=AsyncMock(side_effect=[
                {"success": True, "data": {}}, {"success": False, "error": "unknown"},
            ])),
            patch("services.tools.play_simulation._invoke_entry", new=AsyncMock(return_value={"success": True})),
            patch("services.tools.play_simulation._confirm_stopped", new=AsyncMock(return_value=False)),
            patch("services.tools.play_simulation.asyncio.sleep", new=AsyncMock()),
        ):
            stopped = await play_simulation(FakeContext(), entry="Example.Simulation::Run")
        rejected = await play_simulation(
            FakeContext(), entry="Example.Simulation::Run", capture={"unity_instance": "Other@hash"},
        )

        self.assertFalse(stopped["success"])
        self.assertFalse(stopped["data"]["stopConfirmed"])
        self.assertFalse(rejected["success"])
        self.assertIn("not allowed", rejected["message"])


if __name__ == "__main__":
    unittest.main()
