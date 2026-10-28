import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.tools.manage_asset import manage_asset
from services.tools.read_console import read_console


class FakeContext(SimpleNamespace):
    def __init__(self):
        super().__init__(unity_instance="FortuneDao@test-hash")

    async def info(self, *_args, **_kwargs):
        return None


def _ok():
    return {"success": True, "message": "ok", "data": []}


class ReadConsoleFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_regex_is_rejected_without_touching_unity(self):
        send = AsyncMock(return_value=_ok())
        with patch("services.tools.read_console.send_with_unity_instance", send):
            result = await read_console(FakeContext(), action="get", regex="([")

        self.assertFalse(result["success"])
        self.assertIn("Invalid regex", result["message"])
        send.assert_not_awaited()

    async def test_over_long_regex_is_rejected(self):
        send = AsyncMock(return_value=_ok())
        with patch("services.tools.read_console.send_with_unity_instance", send):
            result = await read_console(FakeContext(), action="get", regex="a" * 201)

        self.assertFalse(result["success"])
        self.assertIn("200 characters", result["message"])
        send.assert_not_awaited()

    async def test_regex_and_since_timestamp_are_forwarded(self):
        send = AsyncMock(return_value=_ok())
        with patch("services.tools.read_console.send_with_unity_instance", send):
            result = await read_console(
                FakeContext(),
                action="get",
                regex=r"\[Probe\]",
                since_timestamp="2026-10-28T01:40:00Z",
                include_stacktrace=True,
            )

        self.assertTrue(result["success"])
        params = send.await_args.args[3]
        self.assertEqual(params["regex"], r"\[Probe\]")
        self.assertEqual(params["sinceTimestamp"], "2026-10-28T01:40:00Z")

    async def test_blank_regex_is_dropped(self):
        send = AsyncMock(return_value=_ok())
        with patch("services.tools.read_console.send_with_unity_instance", send):
            await read_console(FakeContext(), action="get", regex="   ")

        params = send.await_args.args[3]
        self.assertNotIn("regex", params)


class ManageAssetRegexTests(unittest.IsolatedAsyncioTestCase):
    async def test_regex_is_forwarded_for_search(self):
        send = AsyncMock(return_value={"success": True, "data": {}})
        with patch("services.tools.manage_asset.send_with_unity_instance", send):
            result = await manage_asset(
                FakeContext(), action="search", path="Assets", regex=r".*\.prefab$"
            )

        self.assertTrue(result["success"])
        params = send.await_args.args[3]
        self.assertEqual(params["regex"], r".*\.prefab$")

    async def test_regex_rejected_outside_search(self):
        send = AsyncMock(return_value={"success": True, "data": {}})
        with patch("services.tools.manage_asset.send_with_unity_instance", send):
            result = await manage_asset(
                FakeContext(), action="get_info", path="Assets/a.prefab", regex=".*"
            )

        self.assertFalse(result["success"])
        self.assertIn("only supported for action='search'", result["message"])
        send.assert_not_awaited()

    async def test_invalid_regex_rejected_before_dispatch(self):
        send = AsyncMock(return_value={"success": True, "data": {}})
        with patch("services.tools.manage_asset.send_with_unity_instance", send):
            result = await manage_asset(FakeContext(), action="search", path="Assets", regex="*bad")

        self.assertFalse(result["success"])
        self.assertIn("Invalid regex", result["message"])
        send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
