"""Tests for the budget-based log payload formatter: MB-sized hierarchy /
screenshot payloads must never be str()-ed in full."""
import unittest

from core.logging_decorator import _MAX_LOG_PAYLOAD_CHARS, _format_payload


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


if __name__ == "__main__":
    unittest.main()
