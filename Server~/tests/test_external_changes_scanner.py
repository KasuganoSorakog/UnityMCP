"""Functional tests for ExternalChangesScanner.async_update_and_get: the
expensive os.walk runs in a worker thread while state mutations stay on the
event loop."""
import os
import unittest
from unittest.mock import patch

from services.state import external_changes_scanner as scanner_module
from services.state.external_changes_scanner import ExternalChangesScanner


class AsyncUpdateAndGetTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_detects_external_change_and_clears(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            assets = os.path.join(tmp, "Assets")
            os.makedirs(assets)
            target = os.path.join(assets, "a.txt")
            with open(target, "w", encoding="utf-8") as f:
                f.write("v1")

            scanner = ExternalChangesScanner(scan_interval_ms=0)
            scanner.set_project_root("inst", tmp)

            with patch.object(scanner_module, "_in_pytest", return_value=False):
                first = await scanner.async_update_and_get("inst")
                # Baseline scan: not dirty.
                self.assertFalse(first["external_changes_dirty"])

                # Simulate an external file modification with a newer mtime.
                with open(target, "w", encoding="utf-8") as f:
                    f.write("v2-longer")
                stat = os.stat(target)
                os.utime(target, ns=(stat.st_atime_ns,
                         stat.st_mtime_ns + 2_000_000_000))

                second = await scanner.async_update_and_get("inst")
                self.assertTrue(second["external_changes_dirty"])
                self.assertIsNotNone(second["dirty_since_unix_ms"])

                scanner.clear_dirty("inst")
                third = await scanner.async_update_and_get("inst")
                self.assertFalse(third["external_changes_dirty"])
                self.assertIsNotNone(third["last_cleared_unix_ms"])

    async def test_no_project_root_returns_snapshot_without_scan(self):
        scanner = ExternalChangesScanner(scan_interval_ms=0)
        with patch.object(scanner_module, "_in_pytest", return_value=False):
            result = await scanner.async_update_and_get("unknown-inst")
        self.assertFalse(result["external_changes_dirty"])

    async def test_project_root_for_roundtrip(self):
        scanner = ExternalChangesScanner()
        self.assertIsNone(scanner.project_root_for("inst"))
        scanner.set_project_root("inst", "/some/path")
        self.assertEqual(scanner.project_root_for("inst"), "/some/path")


if __name__ == "__main__":
    unittest.main()
