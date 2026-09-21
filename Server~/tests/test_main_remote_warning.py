import sys
import unittest


class RemoteHostWarningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Other test files install stub modules under these names; drop them
        # so importing main wires up the real (headless-safe) server module.
        for module_name in (
            "core.config",
            "models.models",
            "transport.plugin_hub",
        ):
            sys.modules.pop(module_name, None)
        import main as main_module
        cls.main = main_module

    def test_remote_host_triggers_warning(self):
        with self.assertLogs("mcp-for-unity-server", level="WARNING") as cm:
            self.main._warn_if_remote_host("0.0.0.0")
        self.assertTrue(any("loopback" in message for message in cm.output))
        self.assertTrue(any("0.0.0.0" in message for message in cm.output))

    def test_loopback_hosts_do_not_warn(self):
        for host in ("localhost", "LOCALHOST", "127.0.0.1", "::1", None, ""):
            with self.subTest(host=host):
                with self.assertNoLogs("mcp-for-unity-server", level="WARNING"):
                    self.main._warn_if_remote_host(host)


if __name__ == "__main__":
    unittest.main()
