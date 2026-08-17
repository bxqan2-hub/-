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

    def test_installer_does_not_exit_early_at_npm_cmd(self):
        installer = (ROOT / "install-integrations.bat").read_text(encoding="utf-8")
        one_click = (ROOT / "一键安装.bat").read_text(encoding="utf-8")
        self.assertIn("call npm ci --prefix integrations\\pay153_checkout", installer)
        self.assertIn("tools\\check_integrations.py --launch-browser", installer)
        self.assertIn("OpenJS.NodeJS.LTS", one_click)
        self.assertIn("Python.Python.3.12", one_click)


if __name__ == "__main__":
    unittest.main()
