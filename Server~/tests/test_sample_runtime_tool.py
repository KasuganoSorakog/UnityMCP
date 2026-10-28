import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.tools.sample_runtime import sample_runtime


class FakeContext(SimpleNamespace):
    def __init__(self, unity_instance="FortuneDao@test-hash"):
        super().__init__(unity_instance=unity_instance)

    def get_state(self, key):
        return getattr(self, key, None)


class SampleRuntimeToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_sample_is_bounded_and_does_not_enter_play(self):
        captured = []

        async def fake_send(send_fn, unity_instance, command_type, params, **kwargs):
            captured.append((unity_instance, command_type, params))
            return {"success": True, "data": {"sampleId": params["sampleId"], "metrics": []}}

        with patch("services.tools.sample_runtime.send_with_unity_instance", fake_send):
            result = await sample_runtime(FakeContext(), target="Head")

        self.assertTrue(result["success"])
        unity_instance, command_type, params = captured[0]
        self.assertEqual(unity_instance, "FortuneDao@test-hash")
        self.assertEqual(command_type, "sample_runtime")
        self.assertEqual(params["action"], "sample")
        self.assertEqual(params["target"], "Head")
        self.assertEqual(params["duration"], 10.0)
        self.assertEqual(params["timeoutSeconds"], 30)
        self.assertFalse(params["includeSamples"])
        self.assertFalse(params["includeInactive"])
        self.assertEqual(params["maxSamples"], 120)
        self.assertEqual(params["maxSpikes"], 20)
        self.assertNotIn("properties", params)
        self.assertNotIn("manage_editor", params)
        self.assertNotIn("unity_instance", params)
        self.assertEqual(len(params["sampleId"]), 32)

    async def test_forwards_fields_thresholds_and_explicit_raw_sample_limit(self):
        captured = {}

        async def fake_send(send_fn, unity_instance, command_type, params, **kwargs):
            captured.update(params)
            return {"success": True, "data": {}}

        with patch("services.tools.sample_runtime.send_with_unity_instance", fake_send):
            result = await sample_runtime(
                FakeContext(),
                target="DaoRen",
                fields='["transform.localRotation", "MyScript.speed"]',
                duration="2.5",
                include_samples="true",
                max_samples=30,
                spike_threshold='{"MyScript.speed": 0.2}',
            )

        self.assertTrue(result["success"], result)
        self.assertIn("properties", captured, f"result={result}; captured={captured}")
        self.assertEqual(captured["properties"], ["transform.localRotation", "MyScript.speed"])
        self.assertEqual(captured["duration"], 2.5)
        self.assertEqual(captured["timeoutSeconds"], 30)
        self.assertTrue(captured["includeSamples"])
        self.assertEqual(captured["maxSamples"], 30)
        self.assertEqual(captured["spikeThresholds"], {"MyScript.speed": 0.2})
        self.assertNotIn("fields", captured)

    async def test_pending_start_polls_for_result_with_the_same_sample_id(self):
        sent = []

        async def fake_send(send_fn, unity_instance, command_type, params, **kwargs):
            sent.append(params)
            if params["action"] == "sample":
                return {"success": True, "_mcp_status": "pending", "_mcp_poll_interval": 0.5}
            return {"success": True, "data": {"sampledFrames": 12, "complete": True}}

        with (
            patch("services.tools.sample_runtime.send_with_unity_instance", fake_send),
            patch("services.tools.sample_runtime.asyncio.sleep", new=AsyncMock()),
        ):
            result = await sample_runtime(FakeContext(), target="Head", duration=2)

        self.assertTrue(result["success"], result)
        self.assertNotIn("_mcp_status", result)
        self.assertEqual(result["data"]["sampledFrames"], 12)
        self.assertEqual([params["action"] for params in sent], ["sample", "result"])
        self.assertEqual(sent[1]["sampleId"], sent[0]["sampleId"])

    async def test_result_timeout_returns_the_original_sample_id(self):
        pending = {"success": True, "_mcp_status": "pending"}
        with (
            patch("services.tools.sample_runtime.send_with_unity_instance", new=AsyncMock(return_value=pending)),
            patch("services.tools.sample_runtime.time.monotonic", side_effect=[0, 100]),
        ):
            result = await sample_runtime(FakeContext(), target="Head", duration=0.1)

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "sample_result_timeout")
        self.assertIn("sampleId", result["data"])

    async def test_recovers_saved_partial_result_without_restarting_sample(self):
        sent = []

        async def fake_send(send_fn, unity_instance, command_type, params, **kwargs):
            sent.append(params)
            if len(sent) == 1:
                return {"success": False, "code": "unity_execution_state_unknown"}
            return {"success": True, "data": {"sampleId": params["sampleId"], "partial": True, "stopReason": "domain_reload"}}

        wait = AsyncMock(return_value={"completed": True})
        with (
            patch("services.tools.sample_runtime.send_with_unity_instance", fake_send),
            patch("services.tools.sample_runtime._wait_for_refresh_completion", wait),
        ):
            result = await sample_runtime(FakeContext(), target="Head", duration=5)

        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["partial"])
        self.assertTrue(result["data"]["recoveredAfterSessionDisconnect"])
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["action"], "sample")
        self.assertEqual(sent[1]["action"], "result")
        self.assertEqual(sent[1]["sampleId"], sent[0]["sampleId"])
        wait.assert_awaited_once()

    async def test_initial_busy_does_not_poll_for_a_nonexistent_sample(self):
        send = AsyncMock(return_value={"success": False, "code": "unity_instance_busy"})
        with (
            patch("services.tools.sample_runtime.send_with_unity_instance", send),
            patch("services.tools.sample_runtime._wait_for_refresh_completion", new=AsyncMock()) as wait,
        ):
            result = await sample_runtime(FakeContext(), target="Head")

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "unity_instance_busy")
        send.assert_awaited_once()
        wait.assert_not_awaited()

    async def test_busy_result_poll_waits_then_recovers_saved_partial_result(self):
        sent = []

        async def fake_send(send_fn, unity_instance, command_type, params, **kwargs):
            sent.append(params)
            if params["action"] == "sample":
                return {"success": True, "_mcp_status": "pending"}
            if len(sent) == 2:
                return {"success": False, "code": "unity_instance_busy"}
            return {"success": True, "data": {"partial": True, "stopReason": "play_mode_exit"}}

        with (
            patch("services.tools.sample_runtime.send_with_unity_instance", fake_send),
            patch("services.tools.sample_runtime._wait_for_refresh_completion", new=AsyncMock(return_value={"completed": True})),
            patch("services.tools.sample_runtime.asyncio.sleep", new=AsyncMock()),
        ):
            result = await sample_runtime(FakeContext(), target="Head", duration=5)

        self.assertTrue(result["success"], result)
        self.assertTrue(result["data"]["partial"])
        self.assertEqual([params["action"] for params in sent], ["sample", "result", "result"])
        self.assertEqual(sent[0]["sampleId"], sent[-1]["sampleId"])

    async def test_missing_result_id_and_conflicting_field_inputs_fail_before_transport(self):
        with patch("services.tools.sample_runtime.send_with_unity_instance") as send:
            missing = await sample_runtime(FakeContext(), action="result")
            conflict = await sample_runtime(
                FakeContext(), target="Head", properties=["transform.position"], fields=["transform.rotation"],
            )

        self.assertFalse(missing["success"])
        self.assertFalse(conflict["success"])
        send.assert_not_called()

    async def test_rejects_nonfinite_duration_and_threshold(self):
        with patch("services.tools.sample_runtime.send_with_unity_instance") as send:
            duration = await sample_runtime(FakeContext(), target="Head", duration="nan")
            threshold = await sample_runtime(FakeContext(), target="Head", spike_threshold=float("inf"))

        self.assertFalse(duration["success"])
        self.assertFalse(threshold["success"])
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
