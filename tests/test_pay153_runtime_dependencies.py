import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
PAY153 = ROOT / "integrations" / "pay153_checkout"


def load_sentinel_module():
    spec = importlib.util.spec_from_file_location(
        "_test_pay153_sentinel_runtime", PAY153 / "sentinel_token.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class Pay153RuntimeDependencyTests(unittest.TestCase):
    def test_node_protocol_dependencies_are_exactly_locked(self):
        package = json.loads((PAY153 / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((PAY153 / "package-lock.json").read_text(encoding="utf-8"))

        self.assertEqual(package["dependencies"]["jsdom"], "26.1.0")
        self.assertEqual(lock["packages"][""]["dependencies"]["jsdom"], "26.1.0")
        self.assertIn("node_modules/jsdom", lock["packages"])
        self.assertEqual(package["dependencies"]["undici"], "8.7.0")
        self.assertEqual(lock["packages"][""]["dependencies"]["undici"], "8.7.0")
        self.assertIn("node_modules/undici", lock["packages"])
        self.assertEqual(package["engines"]["node"], ">=22.19.0")

    def test_node_helper_returns_actionable_stderr(self):
        sentinel = load_sentinel_module()
        completed = SimpleNamespace(
            stdout="",
            stderr="Error: Cannot find module 'jsdom'\nRequire stack: helper.js",
            returncode=1,
        )
        with patch.object(subprocess, "run", return_value=completed):
            result = sentinel._run_vm_bundle_via_node({}, "proof", "chatgpt_checkout")

        self.assertIn("Cannot find module 'jsdom'", result["_error"])

    def test_node_helpers_request_hidden_windows_workers(self):
        sentinel = load_sentinel_module()
        with patch.object(sentinel.os, "name", "nt"):
            self.assertTrue(sentinel._windows_creation_flags() & 0x08000000)

        upi_spec = importlib.util.spec_from_file_location(
            "_test_pay153_upi_runner", PAY153 / "upi_go_runner.py"
        )
        upi = importlib.util.module_from_spec(upi_spec)
        assert upi_spec and upi_spec.loader
        upi_spec.loader.exec_module(upi)
        with patch.object(upi.os, "name", "nt"):
            self.assertTrue(upi._windows_creation_flags() & 0x08000000)

        paypal_path = PAY153 / "paypal_oaics_link_pp" / "protocol" / "sentinel.py"
        paypal_spec = importlib.util.spec_from_file_location(
            "_test_paypal_sentinel_runtime", paypal_path
        )
        paypal = importlib.util.module_from_spec(paypal_spec)
        assert paypal_spec and paypal_spec.loader
        paypal_spec.loader.exec_module(paypal)
        with patch.object(paypal.os, "name", "nt"):
            self.assertTrue(paypal._windows_creation_flags() & 0x08000000)

    def test_installer_does_not_exit_early_at_npm_cmd(self):
        installer = (ROOT / "install-integrations.bat").read_text(encoding="utf-8")
        one_click = (ROOT / "一键安装.bat").read_text(encoding="utf-8")
        self.assertIn("call npm ci --prefix integrations\\pay153_checkout", installer)
        self.assertIn("tools\\check_integrations.py --launch-browser", installer)
        self.assertIn("OpenJS.NodeJS.LTS", one_click)
        self.assertIn("Python.Python.3.12", one_click)


if __name__ == "__main__":
    unittest.main()
