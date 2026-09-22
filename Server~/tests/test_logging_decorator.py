"""Tests for the budget-based log payload formatter: MB-sized hierarchy /
screenshot payloads must never be str()-ed in full."""
import asyncio
import logging
import unittest

from core.logging_decorator import (
    _MAX_LOG_PAYLOAD_CHARS,
    _format_payload,
    log_execution,
)


class FormatPayloadTests(unittest.TestCase):
    def test_huge_string_is_sliced(self):
        big = "x" * (5 * 1024 * 1024)
        out = _format_payload(big)
        self.assertLessEqual(len(out), _MAX_LOG_PAYLOAD_CHARS + 64)
        self.assertIn("<+", out)

    def test_dict_with_huge_value_is_bounded(self):
        payload = {"data": {"png_base64": "y" * (10 * 1024 * 1024), "width": 8192}}
        out = _format_payload(payload)
        self.assertLessEqual(len(out), _MAX_LOG_PAYLOAD_CHARS + 256)
        self.assertIn("...", out)

    def test_kwargs_shape_small_payload_readable(self):
        out = _format_payload({"action": "get_hierarchy", "page": 2})
        self.assertIn("action", out)
        self.assertIn("get_hierarchy", out)

    def test_bytes_summarized_by_length(self):
        out = _format_payload(b"\x00" * (8 * 1024 * 1024))
        self.assertIn("8388608", out)
        self.assertLess(len(out), 64)

    def test_pydantic_model_goes_through_model_dump(self):
        from pydantic import BaseModel

        class MCPResponseLike(BaseModel):
            success: bool
            data: dict

        model = MCPResponseLike(
            success=True, data={"blob": "z" * (5 * 1024 * 1024)})
        out = _format_payload(model)
        self.assertLessEqual(len(out), _MAX_LOG_PAYLOAD_CHARS + 256)

    def test_unrepresentable_object_does_not_raise(self):
        class Bad:
            def __str__(self):
                raise RuntimeError("boom")

        self.assertEqual(_format_payload(Bad()), "<unrepresentable>")


class FailurePathLoggingTests(unittest.TestCase):
    """Failure paths must log at ERROR with a traceback (not bare INFO text),
    while keeping the payload truncation intact."""

    def test_sync_failure_logs_error_with_exc_info(self):
        @log_execution("sync_boom", "tool")
        def sync_boom():
            raise ValueError("sync failure")

        with self.assertLogs("mcp-for-unity-server", level="ERROR") as captured:
            with self.assertRaises(ValueError):
                sync_boom()

        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertIsNotNone(record.exc_info)
        self.assertIs(record.exc_info[0], ValueError)
        self.assertIn("sync failure", record.getMessage())

    def test_async_failure_logs_error_with_exc_info(self):
        @log_execution("async_boom", "tool")
        async def async_boom():
            raise RuntimeError("async failure")

        async def run():
            with self.assertLogs("mcp-for-unity-server", level="ERROR") as captured:
                with self.assertRaises(RuntimeError):
                    await async_boom()
            return captured

        captured = asyncio.run(run())
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertIsNotNone(record.exc_info)
        self.assertIs(record.exc_info[0], RuntimeError)
        self.assertIn("async failure", record.getMessage())

    def test_failure_message_stays_truncated(self):
        @log_execution("big_boom", "tool")
        def big_boom():
            raise ValueError("x" * (5 * 1024 * 1024))

        with self.assertLogs("mcp-for-unity-server", level="ERROR") as captured:
            with self.assertRaises(ValueError):
                big_boom()

        message = captured.records[0].getMessage()
        self.assertLessEqual(
            len(message), _MAX_LOG_PAYLOAD_CHARS + 128)
        self.assertIn("<+", message)

    def test_success_path_still_logs_at_info(self):
        @log_execution("ok_call", "tool")
        def ok_call():
            return {"success": True}

        with self.assertLogs("mcp-for-unity-server", level="INFO") as captured:
            self.assertEqual(ok_call(), {"success": True})

        levels = {record.levelno for record in captured.records}
        self.assertEqual(levels, {logging.INFO})


if __name__ == "__main__":
    unittest.main()
