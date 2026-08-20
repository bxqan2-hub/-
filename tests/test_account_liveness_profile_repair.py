# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from core import account_liveness


class AccountLivenessProfileRepairTests(unittest.TestCase):
    def test_network_preflight_uses_bounded_configured_attempts(self):
        sessions = [MagicMock(), MagicMock()]
        with patch.object(account_liveness._roxy_cfg, "ROXY_AT_RECOVERY_PREFLIGHT_ATTEMPTS", 2), \
             patch("core.account_liveness.BrowserSession", side_effect=sessions) as session_factory, \
             patch("core.account_liveness.get_providers", side_effect=RuntimeError("HTTP 403")), \
             patch("core.account_liveness.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "403"):
                account_liveness._network_preflight_with_retry(
                    "created@example.test",
                    "socks5h://proxy.example:1080",
                )

        self.assertEqual(session_factory.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_normal_liveness_does_not_complete_about_you(self):
        session = MagicMock()
        result = {
            "continue_url": "https://auth.openai.com/about-you",
            "page": {"type": "about_you"},
        }
        with self.assertRaisesRegex(RuntimeError, "不是完整已注册账号"):
            account_liveness._resolve_oauth_continue_url(session, result)

    def test_registration_recovery_completes_about_you(self):
        session = MagicMock()
        result = {
            "continue_url": "https://auth.openai.com/about-you",
            "page": {"type": "about_you"},
        }
        with patch("core.account_liveness.navigate_about_you") as navigate, \
             patch("core.account_liveness.request_sentinel_token", return_value={"token": "challenge"}) as request_token, \
             patch("core.account_liveness.build_sentinel_header", return_value=("sentinel", "so")) as build_header, \
             patch("core.account_liveness.create_account", return_value={
                 "continue_url": "https://auth.openai.com/authorize/continue?state=ok"
             }) as create:
            continue_url, referer = account_liveness._resolve_oauth_continue_url(
                session,
                result,
                repair_profile_name="Test User",
                repair_profile_birthday="1995-01-02",
            )

        self.assertEqual(continue_url, "https://auth.openai.com/authorize/continue?state=ok")
        self.assertEqual(referer, "https://auth.openai.com/about-you")
        navigate.assert_called_once_with(session, "https://auth.openai.com/about-you")
        request_token.assert_called_once_with(session, "oauth_create_account")
        build_header.assert_called_once_with(session, {"token": "challenge"}, "oauth_create_account")
        create.assert_called_once_with(session, "Test User", "1995-01-02", "sentinel", "so")


if __name__ == "__main__":
    unittest.main()
