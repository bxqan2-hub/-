# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import env_loader
from webui import config_editor


class ConfigDefaultFallbackTests(unittest.TestCase):
    def test_registration_timing_budgets_are_editable(self):
        keys = {item["key"] for item in config_editor.EDITABLE_FIELDS}
        self.assertTrue({
            "ROXY_EMAIL_SUBMIT_TIMEOUT",
            "ROXY_API_TIMEOUT",
            "ROXY_API_RETRIES",
            "ROXY_API_RETRY_DELAY",
            "ROXY_CREATE_API_ATTEMPTS",
            "ROXY_PROXY_PREFLIGHT_ATTEMPTS",
            "ROXY_PROXY_PREFLIGHT_PROXY_ATTEMPTS",
            "ROXY_BROWSER_EXIT_IP_ATTEMPTS",
            "ROXY_OTP_MAX_WAIT",
            "ROXY_OTP_SUBMIT_ATTEMPTS",
            "ROXY_PASSWORD_SUBMIT_TIMEOUT",
            "ROXY_PASSWORD_SUBMIT_ATTEMPTS",
            "ROXY_SESSION_REQUEST_TIMEOUT",
            "ROXY_AT_RECOVERY_PREFLIGHT_ATTEMPTS",
            "GENERIC_API_REQUEST_TIMEOUT",
            "GENERIC_API_MAX_CONSECUTIVE_ERRORS",
            "GENERIC_API_REGISTRATION_FAILURE_LIMIT",
            "REGISTER_PASSWORD",
        }.issubset(keys))

    def test_password_and_mfa_share_one_disabled_by_default_switch(self):
        from config import twofa
        from core.registration_password import registration_password_required

        source = Path(twofa.__file__).read_text(encoding="utf-8")
        self.assertFalse(config_editor._parse_value_from_source(source, "ENABLE_2FA", "bool"))
        with patch.object(twofa, "ENABLE_2FA", False):
            self.assertFalse(registration_password_required())
        keys = {item["key"] for item in config_editor.EDITABLE_FIELDS}
        self.assertNotIn("REQUIRE_REGISTRATION_PASSWORD", keys)
        self.assertIn("REGISTER_PASSWORD", keys)
        enable_2fa = next(item for item in config_editor.EDITABLE_FIELDS if item["key"] == "ENABLE_2FA")
        self.assertIn("密码", enable_2fa["help"])
        self.assertIn("MFA", enable_2fa["help"])

    def test_roxy_fixed_os_is_exposed_as_windows_macos_choice(self):
        field = next(item for item in config_editor.EDITABLE_FIELDS if item["key"] == "ROXY_DEFAULT_OS")
        self.assertEqual(field["options"], ["Windows", "macOS"])

    def test_unused_post_registration_chat_settings_are_not_exposed(self):
        keys = {item["key"] for item in config_editor.EDITABLE_FIELDS}
        self.assertTrue({
            "ROXY_POST_REGISTRATION_CHAT_ENABLED",
            "ROXY_POST_REGISTRATION_CHAT_PROMPT",
            "ROXY_POST_REGISTRATION_CHAT_TIMEOUT",
        }.isdisjoint(keys))

    def test_blank_env_value_uses_default_for_all_supported_types(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        try:
            with patch.dict(os.environ, {
                "BOOL_KEY": "",
                "INT_KEY": "",
                "FLOAT_KEY": "",
                "STR_KEY": "",
                "LIST_KEY": "",
            }, clear=True):
                self.assertTrue(env_loader.env_bool("BOOL_KEY", True))
                self.assertEqual(env_loader.env_int("INT_KEY", 90), 90)
                self.assertEqual(env_loader.env_float("FLOAT_KEY", 1.5), 1.5)
                self.assertEqual(env_loader.env_str("STR_KEY", "default"), "default")
                self.assertEqual(env_loader.env_list("LIST_KEY", ["a"]), ["a"])
        finally:
            env_loader._LOADED = old_loaded

    def test_proxy_pool_blank_env_value_means_empty_list(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"PROXY_POOL": ["socks5://127.0.0.1:7897"]}
        try:
            with patch.dict(os.environ, {"PROXY_POOL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"PROXY_POOL": "list_str_multiline"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertEqual(namespace["PROXY_POOL"], [])

    def test_paypal_oaics_proxy_pool_is_an_explicit_env_list(self):
        self.assertIn("PAYPAL_OAICS_PROXY_PROFILES", env_loader.EXPLICIT_EMPTY_LIST_ENV_KEYS)

    def test_config_editor_formats_empty_list_as_literal_empty_list(self):
        self.assertEqual(config_editor._format_env_value([], "list_str_multiline"), "[]")

    def test_proxy_pool_editor_is_unbounded_and_round_trips_1000_entries(self):
        field = next(item for item in config_editor.EDITABLE_FIELDS if item["key"] == "PROXY_POOL")
        self.assertEqual(field["ui_variant"], "large_pool")

        pool = [f"socks5h://user:pass@proxy-{index}.example:3010" for index in range(1000)]
        serialized = config_editor._format_env_value(pool, "list_str_multiline")
        parsed = config_editor._coerce_raw_value(serialized, [], "list_str_multiline")

        self.assertEqual(len(parsed), 1000)
        self.assertEqual(parsed[0], pool[0])
        self.assertEqual(parsed[-1], pool[-1])

    def test_proxy_pool_1000_entries_use_runtime_file_instead_of_large_environment_value(self):
        pool = [f"socks5h://user:pass@proxy-{index}.example:3010" for index in range(1000)]
        with tempfile.TemporaryDirectory() as tmp, \
             patch("config.env_loader._PROXY_POOL_PATH", Path(tmp) / "proxy_pool.txt"), \
             patch("config.env_loader._ENV_PATH", Path(tmp) / ".env"), \
             patch.dict(os.environ, {}, clear=False):
            result = config_editor.update_config({"PROXY_POOL": pool})
            stored = env_loader.read_proxy_pool_file()
            env_value = env_loader.read_env_file()["PROXY_POOL"]

        self.assertEqual(result["runtime_file_updated"], ["PROXY_POOL"])
        self.assertEqual(len(stored), 1000)
        self.assertEqual(stored[-1], pool[-1])
        self.assertEqual(env_value, "[]")

    def test_plan_detection_pool_1000_entries_use_own_runtime_file(self):
        pool = [f"JP|socks5h://user-region-JP-sid-{index}:pass@proxy.example:3010" for index in range(1000)]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(env_loader._RUNTIME_LIST_PATHS, {
                 "PLAN_CHECK_PROXY_PROFILES": Path(tmp) / "plan_check_proxy_pool.txt",
             }), patch("config.env_loader._ENV_PATH", Path(tmp) / ".env"), \
             patch.dict(os.environ, {}, clear=False):
            result = config_editor.update_config({"PLAN_CHECK_PROXY_PROFILES": pool})
            stored = env_loader.read_runtime_list_file("PLAN_CHECK_PROXY_PROFILES")
            env_value = env_loader.read_env_file()["PLAN_CHECK_PROXY_PROFILES"]

        self.assertEqual(result["runtime_file_updated"], ["PLAN_CHECK_PROXY_PROFILES"])
        self.assertEqual(len(stored), 1000)
        self.assertEqual(stored[-1], pool[-1])
        self.assertEqual(env_value, "[]")

    def test_at_validity_pool_uses_own_runtime_file(self):
        pool = ["JP|socks5h://at-one.example:1080", "US|http://at-two.example:8080"]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(env_loader._RUNTIME_LIST_PATHS, {
                 "AT_VALIDITY_PROXY_PROFILES": Path(tmp) / "at_validity_proxy_pool.txt",
             }), patch("config.env_loader._ENV_PATH", Path(tmp) / ".env"), \
             patch.dict(os.environ, {}, clear=False):
            result = config_editor.update_config({"AT_VALIDITY_PROXY_PROFILES": pool})
            stored = env_loader.read_runtime_list_file("AT_VALIDITY_PROXY_PROFILES")
            env_value = env_loader.read_env_file()["AT_VALIDITY_PROXY_PROFILES"]

        self.assertEqual(result["runtime_file_updated"], ["AT_VALIDITY_PROXY_PROFILES"])
        self.assertEqual(stored, pool)
        self.assertEqual(env_value, "[]")

    def test_apply_env_overrides_does_not_let_blank_values_mask_defaults(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"FEATURE_ENABLED": True, "BASE_URL": "https://example.test"}
        try:
            with patch.dict(os.environ, {"FEATURE_ENABLED": "", "BASE_URL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"FEATURE_ENABLED": "bool", "BASE_URL": "str"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertTrue(namespace["FEATURE_ENABLED"])
        self.assertEqual(namespace["BASE_URL"], "https://example.test")

    def test_config_editor_parses_env_str_default_from_source(self):
        source = 'API_KEY: str = env_str("API_KEY", "fallback-key")\n'
        self.assertEqual(
            config_editor._parse_value_from_source(source, "API_KEY", "str"),
            "fallback-key",
        )

    def test_config_editor_blank_env_value_falls_back_to_source_default(self):
        self.assertEqual(
            config_editor._coerce_raw_value("", "wss://connect.browser-use.com", "str"),
            "wss://connect.browser-use.com",
        )
        self.assertTrue(config_editor._coerce_raw_value("", True, "bool"))


if __name__ == "__main__":
    unittest.main()
