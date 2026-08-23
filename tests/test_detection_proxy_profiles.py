# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import detection_proxy
from webui.app import create_app


class DetectionProxyProfilesTests(unittest.TestCase):
    def setUp(self):
        detection_proxy._POOL_ROTATIONS.clear()

    def test_plan_at_and_checkout_profiles_are_independent(self):
        with patch.object(detection_proxy.proxy_cfg, "PLAN_CHECK_PROXY_PROFILES", [
            "JP|socks5h://jp-one.example:1080",
            "JP|socks5h://jp-two.example:1080",
            "DE|socks5h://de.example:1080",
        ]), patch.object(detection_proxy.proxy_cfg, "PLAN_CHECK_PROXY_ACTIVE", "JP"), \
             patch.object(detection_proxy.proxy_cfg, "AT_VALIDITY_PROXY_PROFILES", [
                 "US|socks5h://at-us.example:1080",
             ]), patch.object(detection_proxy.proxy_cfg, "AT_VALIDITY_PROXY_ACTIVE", "US"), \
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
                detection_proxy.configured_detection_proxy_spec("at"),
                "US|socks5h://at-us.example:1080",
            )
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

    def test_detection_proxy_country_prefers_profile_label(self):
        self.assertEqual(
            detection_proxy.infer_detection_proxy_country(
                "ph|socks5h://proxy.example:1080"
            ),
            "PH",
        )

    def test_detection_proxy_country_falls_back_to_region_username(self):
        self.assertEqual(
            detection_proxy.infer_detection_proxy_country(
                "socks5h://user-region-jp:pass@proxy.example:1080"
            ),
            "JP",
        )

    def test_detection_proxy_country_is_empty_for_unlabelled_local_route(self):
        self.assertEqual(
            detection_proxy.infer_detection_proxy_country(
                "socks5h://127.0.0.1:1080"
            ),
            "",
        )

    @patch("curl_cffi.requests.Session")
    def test_proxy_region_tag_is_used_without_network_probe(self, session_cls):
        fixtures = [
            (
                "us.arxlabs.io:3010:mfrp1243966-region-PH-sid-f5cgegV8-t-20:cqwbwg",
                "PH",
            ),
            (
                "us.arxlabs.io:3010:mfrp1243966-region-JP-sid-RDvm5YmM-t-20:cqwbwg",
                "JP",
            ),
        ]

        for proxy, expected_country in fixtures:
            with self.subTest(expected_country=expected_country):
                self.assertEqual(
                    detection_proxy.infer_static_proxy_country(proxy),
                    expected_country,
                )
                result = detection_proxy.inspect_static_proxy(proxy)
                self.assertEqual(result["country"], expected_country)
                self.assertEqual(result["country_source"], "proxy_region_tag")
                self.assertEqual(result["exit_ip"], "")

        session_cls.assert_not_called()

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
        self.assertEqual(result["country_source"], "exit_geo")
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

    def test_import_saves_at_validity_dedicated_pool(self):
        inspection = {
            "country": "JP",
            "country_label": "日本",
            "country_source": "proxy_region_tag",
            "proxy": "socks5h://at-jp.example:1080",
            "masked_proxy": "socks5h://at-jp.example:1080",
            "exit_ip": "",
            "region": "",
            "city": "",
        }
        with patch.object(detection_proxy.proxy_cfg, "AT_VALIDITY_PROXY_PROFILES", []), \
             patch.object(detection_proxy.proxy_cfg, "AT_VALIDITY_PROXY_ACTIVE", ""), \
             patch.object(detection_proxy, "inspect_static_proxy", return_value=inspection), \
             patch("webui.app.config_editor.update_config", return_value={
                 "updated": ["AT_VALIDITY_PROXY_PROFILES", "AT_VALIDITY_PROXY_ACTIVE"],
                 "ignored": [],
             }) as update, patch("config.reload_all"):
            response = self.client.post("/api/detection-proxy-pools/import", json={
                "purpose": "at",
                "proxies": ["socks5h://at-jp.example:1080"],
            })

        self.assertEqual(response.status_code, 200)
        saved = update.call_args.args[0]
        self.assertEqual(saved["AT_VALIDITY_PROXY_ACTIVE"], "JP")
        self.assertEqual(saved["AT_VALIDITY_PROXY_PROFILES"], [
            "JP|socks5h://at-jp.example:1080",
        ])

    def test_import_can_grow_existing_pool_beyond_legacy_500_limit(self):
        existing = [
            f"JP|socks5h://old-{index}.example:1080"
            for index in range(500)
        ]
        inspection = {
            "country": "JP",
            "country_label": "JP",
            "country_source": "proxy_region_tag",
            "proxy": "socks5h://new.example:1080",
            "masked_proxy": "socks5h://new.example:1080",
            "exit_ip": "",
            "region": "",
            "city": "",
        }

        with patch.object(
            detection_proxy.proxy_cfg,
            "PLAN_CHECK_PROXY_PROFILES",
            existing,
        ), patch.object(
            detection_proxy.proxy_cfg,
            "PLAN_CHECK_PROXY_ACTIVE",
            "JP",
        ), patch.object(
            detection_proxy,
            "inspect_static_proxy",
            return_value=inspection,
        ), patch(
            "webui.app.config_editor.update_config",
            return_value={
                "updated": ["PLAN_CHECK_PROXY_PROFILES", "PLAN_CHECK_PROXY_ACTIVE"],
                "ignored": [],
            },
        ) as update, patch("config.reload_all"):
            response = self.client.post("/api/detection-proxy-pools/import", json={
                "purpose": "plan",
                "proxies": ["socks5h://new.example:1080"],
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "application/json")
        payload = response.get_json()
        self.assertEqual(payload["total_count"], 501)
        saved = update.call_args.args[0]["PLAN_CHECK_PROXY_PROFILES"]
        self.assertEqual(len(saved), 501)

    def test_parser_accepts_large_runtime_detection_pool(self):
        entries = [f"JP|socks5h://proxy-{index}.example:1080" for index in range(1_200)]

        self.assertEqual(len(detection_proxy.parse_detection_proxy_pool(entries)), 1_200)


if __name__ == "__main__":
    unittest.main()
