# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import detection_proxy


class DetectionProxyProfilesTests(unittest.TestCase):
    def test_plan_and_checkout_profiles_are_independent(self):
        with patch.object(detection_proxy.proxy_cfg, "PLAN_CHECK_PROXY_PROFILES", [
            "DE|https://proxy.example/get?region=de",
            "JP|socks5h://jp.example:1080",
        ]), patch.object(detection_proxy.proxy_cfg, "PLAN_CHECK_PROXY_ACTIVE", "JP"), \
             patch.object(detection_proxy.proxy_cfg, "CHECKOUT_CHECK_PROXY_PROFILES", [
                 "OAI-DE|http://checkout.example:8080",
             ]), patch.object(detection_proxy.proxy_cfg, "CHECKOUT_CHECK_PROXY_ACTIVE", "OAI-DE"):
            self.assertEqual(
                detection_proxy.configured_detection_proxy_spec("plan"),
                "JP|socks5h://jp.example:1080",
            )
            self.assertEqual(
                detection_proxy.configured_detection_proxy_spec("checkout"),
                "OAI-DE|http://checkout.example:8080",
            )

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


if __name__ == "__main__":
    unittest.main()
