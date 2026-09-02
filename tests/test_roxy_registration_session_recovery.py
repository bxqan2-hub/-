# -*- coding: utf-8 -*-
import unittest
from unittest.mock import ANY, MagicMock, patch

from core import roxy_registration


class RoxyRegistrationSessionRecoveryTests(unittest.TestCase):
    def test_proxy_transport_failure_is_classified(self):
        self.assertTrue(
            roxy_registration._is_proxy_transport_failure(
                RuntimeError("unknown error: net::ERR_PROXY_CONNECTION_FAILED")
            )
        )
        self.assertFalse(roxy_registration._is_proxy_transport_failure(RuntimeError("password rejected")))

    @patch.object(roxy_registration, "_fetch_chatgpt_session", return_value={"accessToken": "settled-at"})
    @patch.object(roxy_registration, "_safe_get")
    @patch.object(roxy_registration.time, "sleep")
    def test_callback_uses_settled_session_before_requesting_another_otp(self, _sleep, _safe_get, _fetch):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/email-verification"
        self.assertEqual(
            roxy_registration._resume_chatgpt_login_callback(driver, email="user@example.test"),
            "logged_in",
        )
        _fetch.assert_called_once()
    def test_session_reader_keeps_warning_response_and_http_status(self):
        driver = MagicMock()
        driver.execute_async_script.return_value = {
            "ok": True,
            "status": 200,
            "data": {"WARNING_BANNER": "temporary"},
        }

        result = roxy_registration._read_chatgpt_session_once(driver)

        self.assertEqual(result["WARNING_BANNER"], "temporary")
        self.assertEqual(result["_http_status"], 200)
        script, timeout_ms = driver.execute_async_script.call_args.args
        self.assertIn("AbortController", script)
        self.assertEqual(timeout_ms, 6000)

    def test_access_token_probe_skips_cross_origin_auth_page(self):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/email-verification"

        self.assertFalse(roxy_registration._has_access_token(driver))
        driver.execute_async_script.assert_not_called()

    @patch.object(roxy_registration.time, "sleep")
    def test_repeated_warning_banner_short_circuits_to_relogin(self, _sleep):
        driver = MagicMock()
        driver.current_url = "https://chatgpt.com/"
        driver.execute_async_script.side_effect = [
            {"ok": True, "status": 200, "data": {"WARNING_BANNER": "one"}},
            {"ok": True, "status": 200, "data": {"WARNING_BANNER": "two"}},
            {"ok": True, "status": 200, "data": {"WARNING_BANNER": "three"}},
            {"ok": True, "status": 200, "data": {"WARNING_BANNER": "four"}},
        ]

        with self.assertRaises(roxy_registration.ChatGPTSessionExpiredError):
            roxy_registration._fetch_chatgpt_session_once(driver, timeout=10, auto_jump_wait=1)
        self.assertEqual(driver.execute_async_script.call_count, 4)

    @patch.object(roxy_registration.time, "sleep")
    def test_temporary_warning_banner_can_settle_into_access_token(self, _sleep):
        driver = MagicMock()
        driver.current_url = "https://chatgpt.com/"
        driver.execute_async_script.side_effect = [
            {"ok": True, "status": 200, "data": {"WARNING_BANNER": "one"}},
            {"ok": True, "status": 200, "data": {"WARNING_BANNER": "two"}},
            {"ok": True, "status": 200, "data": {"accessToken": "settled-at"}},
        ]

        result = roxy_registration._fetch_chatgpt_session_once(driver, timeout=10, auto_jump_wait=1)
        self.assertEqual(result["accessToken"], "settled-at")

    @patch.object(roxy_registration, "_fetch_chatgpt_session", return_value={"accessToken": "recovered-at"})
    @patch.object(roxy_registration, "_wait_after_email_otp_submit", return_value="accepted")
    @patch.object(roxy_registration, "_is_email_verification_page", return_value=False)
    @patch.object(roxy_registration, "_type_otp")
    @patch.object(roxy_registration, "wait_for_otp", return_value="222222")
    @patch.object(roxy_registration, "_wait_for_otp_input", return_value=None)
    @patch.object(roxy_registration, "_resume_chatgpt_login_callback", return_value="otp")
    @patch.object(roxy_registration, "_snapshot_current_email_otp", return_value="111111")
    def test_visible_recovery_excludes_previous_otp(
        self, _snapshot, _resume, _wait_input, wait_otp, _type, _is_otp_page, _outcome, _fetch,
    ):
        result = roxy_registration._recover_chatgpt_session_in_browser(
            MagicMock(), "created@example.com",
        )

        self.assertEqual(result["accessToken"], "recovered-at")
        self.assertEqual(wait_otp.call_args.kwargs["exclude_codes"], {"111111"})

    def test_unauthorized_session_response_is_marked_expired(self):
        driver = MagicMock()
        driver.execute_async_script.return_value = {
            "ok": True,
            "status": 401,
            "data": {"error": "Unauthorized"},
        }

        result = roxy_registration._read_chatgpt_session_once(driver)

        self.assertTrue(result["_session_expired"])

    @patch.object(roxy_registration, "_recover_chatgpt_session_in_browser")
    @patch("core.account_liveness.check_account_liveness")
    @patch.object(roxy_registration, "_fetch_chatgpt_session", side_effect=roxy_registration.ChatGPTSessionExpiredError("logged out"))
    def test_confirmed_logout_uses_visible_window_before_background_login(self, _fetch, live_check, visible_recovery):
        visible_recovery.return_value = {"accessToken": "visible-at"}

        result = roxy_registration._fetch_or_recover_chatgpt_session(
            MagicMock(),
            email="created@example.com",
            proxy=None,
            registration_created=True,
            should_stop=None,
        )

        self.assertEqual(result["accessToken"], "visible-at")
        visible_recovery.assert_called_once()
        live_check.assert_not_called()

    @patch("core.account_liveness.check_account_liveness")
    @patch.object(roxy_registration, "_fetch_chatgpt_session", side_effect=RuntimeError("WARNING_BANNER"))
    def test_created_account_recovers_at_by_email_otp_login(self, _fetch, live_check):
        live_check.return_value = {
            "ok": True,
            "access_token": "recovered-at",
            "session": {"user": {"id": "user-1"}},
        }

        result = roxy_registration._fetch_or_recover_chatgpt_session(
            MagicMock(),
            email="created@example.com",
            proxy="socks5h://proxy.example:1080",
            registration_created=True,
            should_stop=None,
        )

        self.assertEqual(result["accessToken"], "recovered-at")
        self.assertEqual(result["_at_recovery"], "email_otp_relogin")
        _fetch.assert_called_once_with(
            ANY,
            timeout=25,
            auto_jump_wait=8,
            refresh_attempts=0,
            stop_check=None,
        )
        live_check.assert_called_once_with(
            "created@example.com",
            proxy="socks5h://proxy.example:1080",
            clear_log=False,
            should_stop=None,
            repair_profile_name=None,
            repair_profile_birthday=None,
        )

    @patch.object(roxy_registration, "_fetch_chatgpt_session", side_effect=RuntimeError("no session"))
    def test_unconfirmed_registration_does_not_start_relogin(self, _fetch):
        with self.assertRaisesRegex(RuntimeError, "no session"):
            roxy_registration._fetch_or_recover_chatgpt_session(
                MagicMock(),
                email="not-created@example.com",
                proxy=None,
                registration_created=False,
            )

    @patch.object(roxy_registration, "_resume_chatgpt_login_callback")
    @patch.object(roxy_registration, "_otp_flow_advanced_state", return_value="email_verified")
    @patch.object(roxy_registration, "_fetch_chatgpt_session", return_value={"accessToken": "verified-at"})
    def test_email_verified_confirmation_page_resumes_callback_before_session_read(
        self, _fetch, _advanced_state, resume_callback
    ):
        result = roxy_registration._fetch_or_recover_chatgpt_session(
            MagicMock(),
            email="verified@example.com",
            proxy=None,
            registration_created=True,
            should_stop=None,
        )

        self.assertEqual(result["accessToken"], "verified-at")
        resume_callback.assert_called_once_with(ANY, email="verified@example.com")

    @patch("core.account_liveness.check_account_liveness")
    @patch.object(roxy_registration, "_fetch_chatgpt_session", side_effect=RuntimeError("AT 获取已停止"))
    def test_stopped_task_does_not_start_email_relogin(self, _fetch, live_check):
        with self.assertRaisesRegex(RuntimeError, "AT 获取已停止"):
            roxy_registration._fetch_or_recover_chatgpt_session(
                MagicMock(),
                email="created@example.com",
                proxy=None,
                registration_created=True,
                should_stop=lambda: True,
            )
        live_check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
