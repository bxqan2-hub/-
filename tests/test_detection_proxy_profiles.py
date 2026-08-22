# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import detection_proxy
from webui.app import create_app


class DetectionProxyProfilesTests(unittest.TestCase):
    def setUp(self):
        detection_proxy._POOL_ROTATIONS.clear()

    def test_plan_and_checkout_profiles_are_independent(self):
        with patch.object(detection_proxy.proxy_cfg, "PLAN_CHECK_PROXY_PROFILES", [
            "JP|socks5h://jp-one.example:1080",
            "JP|socks5h://jp-two.example:1080",
            "DE|socks5h://de.example:1080",
        ]), patch.object(detection_proxy.proxy_cfg, "PLAN_CHECK_PROXY_ACTIVE", "JP"), \
             patch.object(detection_proxy.proxy_cfg, "CHECKOUT_CHECK_PROXY_PROFILES", [
                 "DE|http://checkout.example:8080",
             ]), patch.object(detection_proxy.proxy_cfg, "CHECKOUT_CHECK_PROXY_ACTIVE", "DE"):
            selected = {
                detection_proxy.configured_detection_proxy_spec("plan"),
                detection_proxy.configured_detection_proxy_spec("plan"),
            }
            self.assertEqual(selected, {
                "JP|socks5h://jp-one.example:1080",
                "JP|socks5h://jp-two.example:1080",
            })
            self.assertEqual(
                detection_proxy.configured_detection_proxy_spec("checkout"),
                "DE|http://checkout.example:8080",
            )

    def test_dynamic_api_profiles_are_not_used_by_detection_pool(self):
        with patch.object(detection_proxy.proxy_cfg, "PLAN_CHECK_PROXY_PROFILES", [
            "JP|https://proxy.example/get?region=jp",
        ]), patch.object(detection_proxy.proxy_cfg, "PLAN_CHECK_PROXY_ACTIVE", "JP"):
            with self.assertRaisesRegex(ValueError, "动态 API"):
                detection_proxy.configured_detection_proxy_spec("plan")

    def test_country_groups_count_static_proxies(self):
        groups = detection_proxy.detection_proxy_country_groups([
            "JP|socks5h://jp-one.example:1080",
            "JP|socks5h://jp-two.example:1080",
            "US|http://us.example:8080",
            "DE|https://api.example/get?region=de",
        ])
        self.assertEqual(
            [(item["country"], item["count"]) for item in groups],
            [("JP", 2), ("US", 1)],
        )

    @patch("curl_cffi.requests.Session")
    def test_static_proxy_inspection_reads_exit_country(self, session_cls):
        response = session_cls.return_value.get.return_value
        response.status_code = 200
        response.json.return_value = {
            "ip": "203.0.113.9",
            "country": "jp",
            "region": "Tokyo",
            "city": "Tokyo",
        }

        result = detection_proxy.inspect_static_proxy(
            "socks5h://user:pass@proxy.example:1080",
            timeout=5,
        )

        self.assertEqual(result["country"], "JP")
        self.assertEqual(result["exit_ip"], "203.0.113.9")
        self.assertEqual(
            session_cls.return_value.proxies,
            {
                "http": "socks5h://user:pass@proxy.example:1080",
                "https": "socks5h://user:pass@proxy.example:1080",
            },
        )
        session_cls.return_value.close.assert_called_once()

    def test_api_profile_is_fetched_only_when_resolved(self):
        with patch.object(
            detection_proxy.proxy_cfg,
            "fetch_proxy_from_api",
            return_value="socks5h://fresh.example:1080",
        ) as fetch:
            resolved = detection_proxy.resolve_detection_proxy(
                "DE|https://proxy.example/get?region=de"
            )
        self.assertEqual(resolved, "socks5h://fresh.example:1080")
        fetch.assert_called_once_with(
            api_url="https://proxy.example/get?region=de",
            timeout=None,
            max_attempts=None,
            validation_timeout=None,
            force=True,
        )

    def test_region_profile_sets_accounts_check_timezone(self):
        self.assertEqual(
            detection_proxy.infer_timezone_offset_min(
                "JP|https://proxy.example/get?region=jp"
            ),
            "-540",
        )


class DetectionProxyImportApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_import_detects_countries_merges_and_saves_static_pool(self):
        def inspect(proxy, *, timeout):
            country = "JP" if "jp.example" in proxy else "US"
            return {
                "country": country,
                "country_label": country,
                "proxy": proxy,
                "masked_proxy": proxy,
                "exit_ip": "203.0.113.7",
                "region": "",
                "city": "",
            }

        with patch.object(detection_proxy.proxy_cfg, "PLAN_CHECK_PROXY_PROFILES", [
            "JP|socks5h://old-jp.example:1080",
            "JP|https://dynamic.example/get?region=JP",
        ]), patch.object(detection_proxy.proxy_cfg, "PLAN_CHECK_PROXY_ACTIVE", "JP"), \
             patch.object(detection_proxy, "inspect_static_proxy", side_effect=inspect), \
             patch("webui.app.config_editor.update_config", return_value={
                 "updated": ["PLAN_CHECK_PROXY_PROFILES", "PLAN_CHECK_PROXY_ACTIVE"],
                 "ignored": [],
             }) as update, patch("config.reload_all"):
            response = self.client.post("/api/detection-proxy-pools/import", json={
                "purpose": "plan",
                "proxies": [
                    "socks5h://jp.example:1080",
                    "http://us.example:8080",
                ],
            })

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["added_count"], 2)
        self.assertEqual(payload["total_count"], 3)
        saved = update.call_args.args[0]
        self.assertEqual(saved["PLAN_CHECK_PROXY_ACTIVE"], "JP")
        self.assertEqual(saved["PLAN_CHECK_PROXY_PROFILES"], [
            "JP|socks5h://old-jp.example:1080",
            "JP|socks5h://jp.example:1080",
            "US|http://us.example:8080",
        ])


if __name__ == "__main__":
    unittest.main()
