# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from core import roxy_codex_oauth


class RoxyCodexOtpPollingTests(unittest.TestCase):
    def test_invalid_otp_resends_in_place_without_reopening_authorize_url(self):
        driver = MagicMock()
        provider_calls = []

        def provider(email, **kwargs):
            provider_calls.append(kwargs)
            return "111111" if len(provider_calls) == 1 else "222222"

        patches = (
            patch.object(roxy_codex_oauth, "_maybe_accept"),
            patch.object(roxy_codex_oauth, "_type_email_address"),
            patch.object(roxy_codex_oauth, "_submit_email_step"),
            patch.object(roxy_codex_oauth, "_maybe_click_passwordless_after_email"),
            patch.object(roxy_codex_oauth, "_wait_for_otp_input"),
            patch.object(roxy_codex_oauth, "_clear_otp_inputs"),
            patch.object(roxy_codex_oauth, "_type_otp"),
            patch.object(roxy_codex_oauth, "_install_email_otp_validate_hook"),
            patch.object(roxy_codex_oauth, "_click_if_present", return_value=True),
            patch.object(roxy_codex_oauth, "_wait_after_email_otp_submit", side_effect=["invalid", "accepted"]),
            patch.object(roxy_codex_oauth, "_click_resend_email_otp", return_value={"ok": True, "text": "Resend"}),
            patch.object(roxy_codex_oauth, "human_delay"),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10] as resend, patches[11]:
            roxy_codex_oauth._fill_email_and_otp(
                driver,
                "user@example.com",
                provider,
                "https://auth.openai.com/oauth/authorize?test=1",
            )

        self.assertEqual(driver.get.call_count, 1)
        resend.assert_called_once_with(driver, timeout=25)
        self.assertEqual(provider_calls[0]["max_wait"], 120)
        self.assertEqual(provider_calls[1]["max_wait"], 120)
        self.assertEqual(provider_calls[1]["exclude_codes"], {"111111"})

    def test_fresh_otp_passes_timeout_and_exclusions_to_provider(self):
        calls = []

        def provider(email, **kwargs):
            calls.append((email, kwargs))
            return "222222"

        code = roxy_codex_oauth._wait_for_fresh_email_otp(
            provider,
            "user@example.com",
            after_ts=123.0,
            used_codes={"111111"},
            timeout=45,
        )

        self.assertEqual(code, "222222")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["max_wait"], 45)
        self.assertEqual(calls[0][1]["exclude_codes"], {"111111"})

    def test_legacy_provider_still_rejects_old_code(self):
        def provider(email, after_ts):
            return "111111"

        with self.assertRaisesRegex(RuntimeError, "仍返回已失败验证码"):
            roxy_codex_oauth._wait_for_fresh_email_otp(
                provider,
                "user@example.com",
                after_ts=123.0,
                used_codes={"111111"},
                timeout=45,
            )


if __name__ == "__main__":
    unittest.main()
