# -*- coding: utf-8 -*-
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

from core import roxy_registration
from core.roxybrowser_client import RoxyOpenResult


class RoxyRegistrationSessionRecoveryTests(unittest.TestCase):
    def test_session_recovery_stops_terminal_page_before_read_or_background_login(self):
        for page_text, expected in (
            ("Route Error (400)", "code=auth_route_error page_status=400"),
            ("error_code: account_deactivated", "account_deactivated"),
        ):
            with self.subTest(page_text=page_text), \
                 patch.object(roxy_registration, "_email_otp_page_state", return_value={
                     "text": page_text, "inputs": [], "errors": [],
                 }), \
                 patch.object(roxy_registration, "_fetch_chatgpt_session") as fetch, \
                 patch.object(roxy_registration, "_resume_chatgpt_login_callback") as callback, \
                 patch("core.account_liveness.check_account_liveness") as background_login:
                with self.assertRaisesRegex(RuntimeError, expected):
                    roxy_registration._fetch_or_recover_chatgpt_session(
                        MagicMock(), email="mail@example.test", proxy=None, registration_created=True,
                    )
                fetch.assert_not_called()
                callback.assert_not_called()
                background_login.assert_not_called()

    def test_registration_otp_wait_preserves_page_stop_and_mail_failure_categories(self):
        for branch in (
            "route_stop", "deactivated_stop", "route_return", "login_stop",
            "login_after_error", "login_return", "mail_timeout", "transport_failure",
            "transport_login",
        ):
            with self.subTest(branch=branch), ExitStack() as stack:
                client = MagicMock()
                client.profile_proxy = "http://proxy.example:8080"
                opened = RoxyOpenResult(
                    "profile-fixture", {}, preflight_exit_geo={"ip": "198.51.100.7"},
                )
                client.open_profile.return_value = opened
                driver = MagicMock()
                page_state = [None]
                reads = []
                terminal = (
                    "account_deactivated" if branch == "deactivated_stop"
                    else "OTP page stage=otp_page code=auth_route_error page_status=400"
                )

                def advanced_state(actual_driver):
                    self.assertIs(actual_driver, driver)
                    if isinstance(page_state[0], Exception):
                        raise page_state[0]
                    return page_state[0]

                def read_mail(email, **kwargs):
                    self.assertEqual(email, "mail@example.test")
                    reads.append(set(kwargs["exclude_codes"]))
                    if len(reads) > 1:
                        return "222222"
                    if branch.startswith(("route", "deactivated")):
                        page_state[0] = RuntimeError(terminal)
                    elif branch.startswith("login") or branch == "transport_login":
                        page_state[0] = "email_login"
                    if branch.endswith(("_stop", "_return")):
                        self.assertTrue(kwargs["should_stop"]())
                        # A later navigation must not erase an already seen stop.
                        page_state[0] = None
                    if branch.endswith("_return"):
                        return "111111"
                    if branch.startswith("transport"):
                        raise roxy_registration.GenericApiTransportError("fixture transport failure")
                    raise roxy_registration.GenericApiMailError("fixture mail wait ended")

                mocked = {
                    "RoxyBrowserClient": client,
                    "_build_driver": driver,
                    "_center_browser_window": None,
                    "probe_selenium_driver_exit_geo": {"ip": "198.51.100.7"},
                    "_start_traffic_optimizer": None,
                    "_safe_get": None,
                    "human_delay": None,
                    "_page_warmup": None,
                    "_maybe_accept": None,
                    "_check_manual_stop": None,
                    "_submit_email_and_wait_next": "otp",
                    "registration_password_required": False,
                    "_fill_password_page_if_present": None,
                    "_snapshot_current_email_otp": "000000",
                    "_prepare_next_email_otp_attempt": "otp",
                    "_wait_for_otp_input": "profile",
                    "_complete_profile_page": True,
                    "_fetch_or_recover_chatgpt_session": {
                        "accessToken": "registration-at", "user": {"email": "mail@example.test"},
                    },
                    "_finish_traffic_optimizer": {},
                    "resolve_email_source": "generic_api",
                    "_release_roxy_registration_email_failure": "fixture_failed",
                    "save_account_data": 706,
                }
                calls = {
                    name: stack.enter_context(patch.object(roxy_registration, name, return_value=value))
                    for name, value in mocked.items()
                }
                stack.enter_context(patch.object(roxy_registration._cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True))
                stack.enter_context(patch.object(roxy_registration._cfg, "ROXY_OTP_RETRY_ON_MAIL_TIMEOUT", False))
                stack.enter_context(patch.object(roxy_registration._twofa_cfg, "ENABLE_2FA", False))
                stack.enter_context(patch("config.codex.ENABLE_CODEX_AUTO", False))
                stack.enter_context(patch.object(roxy_registration, "_ROXY_OTP_MAX_ATTEMPTS", 2))
                stack.enter_context(patch.object(roxy_registration, "_otp_flow_advanced_state", side_effect=advanced_state))
                stack.enter_context(patch.object(roxy_registration, "wait_for_otp", side_effect=read_mail))
                type_otp = stack.enter_context(patch.object(roxy_registration, "_type_otp"))
                log = stack.enter_context(patch.object(roxy_registration, "logger"))

                result = roxy_registration.run_roxy_registration(
                    "mail@example.test", "Test User", "1990-01-01",
                )

                recovered = branch.startswith("login")
                self.assertEqual(result["success"], recovered, result.get("error"))
                self.assertEqual(len(reads), 2 if recovered else 1)
                self.assertEqual(calls["_prepare_next_email_otp_attempt"].call_count, int(recovered))
                self.assertEqual(calls["save_account_data"].call_count, int(recovered))
                self.assertEqual(calls["_complete_profile_page"].call_count, int(recovered))
                self.assertEqual(calls["_fetch_or_recover_chatgpt_session"].call_count, int(recovered))
                if branch.startswith(("route", "deactivated")):
                    self.assertIn(terminal, result["error"])
                    self.assertNotIn("GenericApiMailError", result["error"])
                elif branch.startswith("transport"):
                    self.assertIn("GenericApiTransportError", result["error"])
                elif branch == "mail_timeout":
                    self.assertIn("GenericApiMailError", result["error"])
                if branch == "login_return":
                    self.assertEqual(reads[1], {"000000", "111111"})
                type_otp.assert_not_called()
                self.assertNotIn("111111", repr(log.mock_calls))
                driver.quit.assert_called_once()
                client.cleanup_profile.assert_called_once_with(opened)

    def test_registration_callback_keeps_mail_received_during_navigation(self):
        for branch in (
            "wait_verified", "wait_prepare_verified", "input_verified",
            "submit_verified", "rejected_prepare_verified",
        ):
            with self.subTest(branch=branch), ExitStack() as stack:
                client = MagicMock()
                client.profile_proxy = "http://proxy.example:8080"
                client.open_profile.return_value = RoxyOpenResult(
                    "profile-fixture", {}, preflight_exit_geo={"ip": "198.51.100.7"},
                )
                driver = MagicMock()
                clock = [1000.0]
                sent_at = []
                mail_reads = []

                def resume_callback(actual_driver, *, email):
                    self.assertIs(actual_driver, driver)
                    self.assertEqual(email, "mail@example.test")
                    sent_at.append(clock[0])
                    clock[0] += 10.0
                    return "otp"

                def read_mail(email, **kwargs):
                    self.assertEqual(email, "mail@example.test")
                    mail_reads.append((kwargs["after_ts"], set(kwargs["exclude_codes"])))
                    if len(mail_reads) == 1:
                        if branch in ("wait_verified", "wait_prepare_verified"):
                            raise RuntimeError("fixture initial OTP wait interrupted")
                        return "111111"
                    return "222222"

                mocked = {
                    "RoxyBrowserClient": client,
                    "_build_driver": driver,
                    "_center_browser_window": None,
                    "probe_selenium_driver_exit_geo": {"ip": "198.51.100.7"},
                    "_start_traffic_optimizer": None,
                    "_safe_get": None,
                    "human_delay": None,
                    "_page_warmup": None,
                    "_maybe_accept": None,
                    "_check_manual_stop": None,
                    "_submit_email_and_wait_next": "otp",
                    "registration_password_required": False,
                    "_fill_password_page_if_present": "confirmed-password",
                    "_snapshot_current_email_otp": "000000",
                    "_otp_flow_advanced_state": "email_verified" if branch == "wait_verified" else None,
                    "_prepare_next_email_otp_attempt": "email_verified",
                    "_clear_otp_inputs": None,
                    "_is_email_verification_page": False,
                    "_complete_profile_page": True,
                    "_fetch_or_recover_chatgpt_session": {
                        "accessToken": "registration-at", "user": {"email": "mail@example.test"},
                    },
                    "_finish_traffic_optimizer": {},
                    "resolve_email_source": "generic_api",
                    "_release_roxy_registration_email_failure": "fixture_failed",
                    "save_account_data": 706,
                }
                mocked_calls = {}
                for name, result in mocked.items():
                    mocked_calls[name] = stack.enter_context(patch.object(roxy_registration, name, return_value=result))
                stack.enter_context(patch.object(roxy_registration._cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True))
                stack.enter_context(patch.object(roxy_registration._twofa_cfg, "ENABLE_2FA", False))
                stack.enter_context(patch("config.codex.ENABLE_CODEX_AUTO", False))
                stack.enter_context(patch.object(roxy_registration.time, "time", side_effect=lambda: clock[0]))
                stack.enter_context(patch.object(roxy_registration, "logger"))
                resume = stack.enter_context(patch.object(
                    roxy_registration, "_resume_chatgpt_login_callback", side_effect=resume_callback,
                ))
                stack.enter_context(patch.object(roxy_registration, "wait_for_otp", side_effect=read_mail))
                stack.enter_context(patch.object(
                    roxy_registration, "_wait_for_otp_input",
                    side_effect=["email_verified", None] if branch == "input_verified" else None,
                    return_value=None,
                ))
                outcomes = {
                    "submit_verified": ["email_verified", "accepted"],
                    "rejected_prepare_verified": ["invalid", "accepted"],
                }
                stack.enter_context(patch.object(
                    roxy_registration, "_wait_after_email_otp_submit",
                    side_effect=outcomes.get(branch), return_value="accepted",
                ))
                type_otp = stack.enter_context(patch.object(roxy_registration, "_type_otp"))

                result = roxy_registration.run_roxy_registration(
                    "mail@example.test", "Test User", "1990-01-01",
                )

                self.assertTrue(result["success"], result.get("error"))
                resume.assert_called_once_with(driver, email="mail@example.test")
                self.assertEqual(len(mail_reads), 2)
                self.assertEqual(mail_reads[0][1], {"000000"})
                expected_exclusions = {"000000"}
                if branch not in ("wait_verified", "wait_prepare_verified"):
                    expected_exclusions.add("111111")
                self.assertEqual(mail_reads[1][1], expected_exclusions)
                expected_inputs = [(driver, "222222")]
                if branch in ("submit_verified", "rejected_prepare_verified"):
                    expected_inputs.insert(0, (driver, "111111"))
                self.assertEqual([item.args for item in type_otp.call_args_list], expected_inputs)
                self.assertEqual(
                    mocked_calls["_prepare_next_email_otp_attempt"].call_count,
                    int(branch in ("wait_prepare_verified", "rejected_prepare_verified")),
                )
                mocked_calls["RoxyBrowserClient"].assert_called_once()
                mocked_calls["_build_driver"].assert_called_once()
                self.assertLessEqual(mail_reads[1][0], sent_at[0])

    def test_registration_saves_refreshed_session_even_when_mfa_returns_failure_or_raises(self):
        for outcome, input_proxy, effective_proxy, saved_proxy in (
            ("failure_return", None, "socks5h://pool-user:pool-password@pool.example:1080", "socks5h://***:***@pool.example:1080"),
            ("exception", "http://input-user:input-password@input.example:8080", "http://rotated-user:rotated-password@rotated.example:8080", "http://***:***@rotated.example:8080"),
            ("success", "http://explicit-user:explicit-password@explicit.example:8080", None, "http://***:***@explicit.example:8080"),
            ("absent", None, None, None),
            ("codex", "http://fallback-user:fallback-password@fallback.example:8080", "  ", "http://***:***@fallback.example:8080"),
        ):
            with self.subTest(outcome=outcome, proxy=saved_proxy), ExitStack() as stack:
                client = MagicMock()
                client.profile_proxy = effective_proxy
                client.open_profile.return_value = RoxyOpenResult(
                    "profile-fixture", {}, preflight_exit_geo={"ip": "198.51.100.7"},
                )
                session = SimpleNamespace(close=MagicMock())
                optimizer = MagicMock()
                setup_result = SimpleNamespace(
                    secret="fixture-secret", access_token="result-at", expires="fresh-expires",
                    password="confirmed-password", password_configured=True, security_ok=True,
                )

                def setup(*args, **kwargs):
                    optimizer.set_session_only.assert_called_once_with(True)
                    if outcome != "absent":
                        session._twofa_refreshed_access_token = "refreshed-at"
                        session._twofa_session_expires = "fresh-expires"
                    if outcome == "exception":
                        raise RuntimeError("fixture MFA failure")
                    return setup_result if outcome in ("success", "codex") else None

                mocked = {
                    "RoxyBrowserClient": client,
                    "_build_driver": MagicMock(),
                    "_center_browser_window": None,
                    "probe_selenium_driver_exit_geo": {"ip": "198.51.100.7"},
                    "_start_traffic_optimizer": optimizer,
                    "_safe_get": None,
                    "human_delay": None,
                    "_page_warmup": None,
                    "_maybe_accept": None,
                    "_check_manual_stop": None,
                    "_submit_email_and_wait_next": "otp",
                    "registration_password_required": False,
                    "_fill_password_page_if_present": "confirmed-password",
                    "_wait_for_otp_input": "profile",
                    "_complete_profile_page": True,
                    "_fetch_or_recover_chatgpt_session": {
                        "accessToken": "registration-at", "expires": "old-expires",
                        "user": {"email": "mail@example.test"},
                    },
                    "resolve_twofa_proxy": "http://proxy.example:8080",
                    "build_twofa_session": session,
                    "_finish_traffic_optimizer": {},
                    "resolve_email_source": "generic_api",
                }
                for name, result in mocked.items():
                    if name == "_fetch_or_recover_chatgpt_session":
                        def fetch_session(*args, session_info=result, **kwargs):
                            optimizer.set_session_only.assert_not_called()
                            return session_info
                        stack.enter_context(patch.object(roxy_registration, name, side_effect=fetch_session))
                    else:
                        stack.enter_context(patch.object(roxy_registration, name, return_value=result))
                stack.enter_context(patch.object(roxy_registration._cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True))
                stack.enter_context(patch.object(roxy_registration._twofa_cfg, "ENABLE_2FA", True))
                stack.enter_context(patch("config.codex.ENABLE_CODEX_AUTO", outcome == "codex"))
                def codex_oauth(*args, **kwargs):
                    optimizer.set_session_only.assert_called_with(False)
                    return {"status": "success", "ok": True}
                codex_mock = stack.enter_context(patch("core.roxy_codex_oauth.run_roxy_codex_oauth", side_effect=codex_oauth))
                setup_mock = stack.enter_context(patch("core.account_export.maybe_setup_2fa_result", side_effect=setup))
                save = stack.enter_context(patch.object(roxy_registration, "save_account_data", return_value=706))
                log = stack.enter_context(patch.object(roxy_registration, "logger"))

                result = roxy_registration.run_roxy_registration(
                    "mail@example.test", "Test User", "1990-01-01", proxy=input_proxy, otp_code="123456",
                )

                expected_token = "registration-at" if outcome == "absent" else "refreshed-at"
                expected_expires = "old-expires" if outcome == "absent" else "fresh-expires"
                save.assert_called_once()
                self.assertEqual(save.call_args.kwargs["proxy_used"], saved_proxy)
                self.assertEqual(client.profile_proxy, effective_proxy)
                self.assertEqual(save.call_args.kwargs["access_token"], expected_token)
                self.assertEqual(save.call_args.kwargs["extra"]["expires"], expected_expires)
                self.assertEqual(result["access_token"], expected_token)
                self.assertEqual(result["success"], outcome in ("success", "codex"))
                self.assertEqual(bool(save.call_args.kwargs["totp_secret"]), outcome in ("success", "codex"))
                self.assertEqual(codex_mock.call_count, int(outcome == "codex"))
                self.assertEqual(setup_mock.call_args.kwargs["access_token"], "registration-at")
                session.close.assert_called_once()
                self.assertNotIn("refreshed-at", repr(log.mock_calls))
                for credential in ("pool-user", "pool-password", "input-user", "input-password", "rotated-user", "rotated-password", "explicit-user", "explicit-password", "fallback-user", "fallback-password"):
                    self.assertNotIn(credential, repr(save.call_args))
                    self.assertNotIn(credential, repr(log.mock_calls))

    def test_proxy_transport_failure_is_classified(self):
        self.assertTrue(
            roxy_registration._is_proxy_transport_failure(
                RuntimeError("unknown error: net::ERR_PROXY_CONNECTION_FAILED")
            )
        )
        self.assertFalse(roxy_registration._is_proxy_transport_failure(RuntimeError("password rejected")))

    def test_proxy_isolation_failure_is_separate_from_transport(self):
        self.assertTrue(
            roxy_registration._is_proxy_isolation_failure(
                RuntimeError("Roxy 浏览器出口 IP 与创建前预检不一致")
            )
        )
        self.assertFalse(
            roxy_registration._is_proxy_isolation_failure(
                RuntimeError("net::ERR_PROXY_CONNECTION_FAILED")
            )
        )

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

    def test_visible_recovery_does_not_request_otp_after_wait_finds_session(self):
        with patch.object(roxy_registration, "_snapshot_current_email_otp", return_value=None), \
             patch.object(roxy_registration, "_resume_chatgpt_login_callback", return_value="otp"), \
             patch.object(roxy_registration, "_wait_for_otp_input", return_value="logged_in"), \
             patch.object(roxy_registration, "wait_for_otp") as wait_otp, \
             patch.object(roxy_registration, "_type_otp") as type_otp, \
             patch.object(roxy_registration, "_fetch_chatgpt_session", return_value={"accessToken": "settled-at"}):
            result = roxy_registration._recover_chatgpt_session_in_browser(MagicMock(), "mail@example.test")
        self.assertEqual(result["accessToken"], "settled-at")
        wait_otp.assert_not_called()
        type_otp.assert_not_called()

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
