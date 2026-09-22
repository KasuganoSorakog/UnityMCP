"""Contract test for the REAL PluginDisconnectedError.

test_unity_instance_middleware.py stubs transport.plugin_hub (to avoid heavy
imports), so its PluginDisconnectedError is a local mirror. This file guards
against stub/real drift: the real class must accept and store the same
machine-readable classification fields used by classified_error_response.
"""

import unittest

from transport.plugin_hub import PluginDisconnectedError


class PluginDisconnectedErrorContractTests(unittest.TestCase):
    def test_carries_classification_fields(self):
        exc = PluginDisconnectedError(
            "gone",
            code="unity_instance_unreachable",
            category="session",
            retryable=True,
            retry_after_ms=2000,
            hint="retry",
        )
        self.assertEqual(str(exc), "gone")
        self.assertEqual(exc.code, "unity_instance_unreachable")
        self.assertEqual(exc.category, "session")
        self.assertIs(exc.retryable, True)
        self.assertEqual(exc.retry_after_ms, 2000)
        self.assertEqual(exc.hint, "retry")

    def test_classification_fields_default_to_none(self):
        # Plain raise sites (message only) must keep working.
        exc = PluginDisconnectedError("gone")
        self.assertEqual(str(exc), "gone")
        self.assertIsNone(exc.code)
        self.assertIsNone(exc.category)
        self.assertIsNone(exc.retryable)
        self.assertIsNone(exc.retry_after_ms)
        self.assertIsNone(exc.hint)


if __name__ == "__main__":
    unittest.main()
