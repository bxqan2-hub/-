# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import MagicMock, call, patch

from core import registration_service, roxy_registration


class RoxyRegistrationOtpRecoveryTests(unittest.TestCase):
    def test_generic_mail_failure_is_counted_before_service_can_requeue_it(self):
        class GenericApiMailError(Exception):
            pass

        with patch("core.email_provider.release_email_if_unconsumed", return_value=True) as release_unconsumed, \
             patch("core.email_provider.release_email") as release_email:
            state = roxy_registration._release_roxy_registration_email_failure(
                "mail@example.test",
                GenericApiMailError("code null"),
                create_acknowledged=False,
            )

        self.assertEqual(state, "mailbox_failure")
        release_unconsumed.assert_called_once()
        self.assertTrue(release_unconsumed.call_args.kwargs["count_failure"])
        release_email.assert_not_called()

    def test_email_auth_error_is_eligible_for_normal_network_recovery(self):
        driver = MagicMock()
        driver.current_url = "https://chatgpt.com/auth/error?error=undefined"
        self.assertTrue(
            roxy_registration._should_retry_email_entry_without_optimization(
                driver,
                RuntimeError("找不到邮箱输入框/邮箱入口"),
            )
        )

    def test_email_error_on_unrelated_page_does_not_trigger_recovery(self):
        driver = MagicMock()
        driver.current_url = "https://chatgpt.com/"
        self.assertFalse(
            roxy_registration._should_retry_email_entry_without_optimization(
                driver,
                RuntimeError("找不到邮箱输入框/邮箱入口"),
            )
        )

    def test_email_auth_error_recovery_disables_optimization_before_resubmit(self):
        driver = MagicMock()
        optimizer = MagicMock()
        with patch("core.roxy_registration._safe_get") as safe_get, \
             patch("core.roxy_registration._page_warmup") as warmup, \
             patch("core.roxy_registration._maybe_accept"), \
             patch("core.roxy_registration.human_delay"), \
             patch("core.roxy_registration._submit_email_and_wait_next", return_value="otp") as submit:
            state = roxy_registration._retry_email_entry_after_traffic_fallback(
                driver,
                "mail@example.test",
                optimizer,
            )

        self.assertEqual(state, "otp")
        optimizer.disable_for_recovery.assert_called_once_with("email_submit_auth_error")
        safe_get.assert_called_once()
        warmup.assert_called_once_with(driver, reason="email_submit_recovery")
        submit.assert_called_once_with(driver, "mail@example.test", attempts=2)

    def test_empty_login_shell_detection(self):
        self.assertTrue(roxy_registration._is_empty_login_shell({
            "url": "https://chatgpt.com/auth/login",
            "inputs": [],
            "actions": [],
        }))
        self.assertFalse(roxy_registration._is_empty_login_shell({
            "url": "https://chatgpt.com/auth/login",
            "inputs": [{"type": "email"}],
            "actions": [],
        }))

    def test_empty_login_shell_reload_uses_clean_navigation(self):
        driver = MagicMock()
        with patch("core.roxy_registration._safe_get") as safe_get, \
             patch("core.roxy_registration._page_warmup") as warmup, \
             patch("core.roxy_registration.time.sleep"):
            roxy_registration._reload_empty_login_shell(driver)

        driver.get.assert_called_once_with("about:blank")
        safe_get.assert_called_once_with(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=45,
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        warmup.assert_called_once_with(driver, reason="empty_login_shell_reload")

    def test_local_webdriver_bypasses_clash_proxy(self):
        with patch.dict(os.environ, {"NO_PROXY": "example.test"}, clear=True):
            roxy_registration._ensure_local_proxy_bypass()
            self.assertEqual(
                os.environ["NO_PROXY"],
                "example.test,127.0.0.1,localhost,::1",
            )
            self.assertEqual(os.environ["no_proxy"], os.environ["NO_PROXY"])

    def test_email_retry_stops_when_otp_dom_already_exists(self):
        driver = MagicMock()
        with patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._type_email_address") as type_email:
            state = roxy_registration._submit_email_and_wait_next(driver, "mail@example.test")
        self.assertEqual(state, "otp")
        type_email.assert_not_called()

    def test_type_email_detects_late_otp_transition_before_trying_to_type(self):
        driver = MagicMock()
        with patch("core.roxy_registration._has_access_token", return_value=False), \
             patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._find_visible_email_input_js", return_value=None), \
             patch("core.roxy_registration._human_type_text") as type_text:
            state = roxy_registration._type_email_address(driver, "mail@example.test", timeout=1)
        self.assertEqual(state, "otp")
        type_text.assert_not_called()

    def test_email_retry_returns_late_transition_reported_by_type_step(self):
        driver = MagicMock()
        with patch("core.roxy_registration._is_email_verification_page", return_value=False), \
             patch("core.roxy_registration._is_signup_password_page", return_value=False), \
             patch("core.roxy_registration._has_access_token", return_value=False), \
             patch("core.roxy_registration._type_email_address", return_value="otp"), \
             patch("core.roxy_registration._email_input_value_state") as input_state:
            state = roxy_registration._submit_email_and_wait_next(driver, "mail@example.test", attempts=1)
        self.assertEqual(state, "otp")
        input_state.assert_not_called()

    def test_email_submit_uses_nextauth_once_when_ui_route_stalls(self):
        driver = MagicMock()
        with patch("core.roxy_registration._is_email_verification_page", return_value=False), \
             patch("core.roxy_registration._is_signup_password_page", return_value=False), \
             patch("core.roxy_registration._has_access_token", return_value=False), \
             patch("core.roxy_registration._type_email_address"), \
             patch("core.roxy_registration._email_input_value_state", return_value={"inputs": [{"value": "mail@example.test"}]}), \
             patch("core.roxy_registration._submit_email_step"), \
             patch("core.roxy_registration._wait_email_submit_next_state", side_effect=["email_cleared", "otp"]), \
             patch("core.roxy_registration._submit_email_via_browser_nextauth", return_value={"ok": True}) as nextauth, \
             patch("core.roxy_registration.human_delay"), \
             patch("core.roxy_registration.time.sleep"):
            state = roxy_registration._submit_email_and_wait_next(driver, "mail@example.test")
        self.assertEqual(state, "otp")
        nextauth.assert_called_once_with(driver, "mail@example.test")

    def test_email_submit_uses_fast_configured_wait_budget(self):
        driver = MagicMock()
        with patch.object(roxy_registration._cfg, "ROXY_EMAIL_SUBMIT_TIMEOUT", 17), \
             patch("core.roxy_registration._is_email_verification_page", return_value=False), \
             patch("core.roxy_registration._is_signup_password_page", return_value=False), \
             patch("core.roxy_registration._has_access_token", return_value=False), \
             patch("core.roxy_registration._type_email_address"), \
             patch("core.roxy_registration._email_input_value_state", return_value={"inputs": [{"value": "mail@example.test"}]}), \
             patch("core.roxy_registration._submit_email_step"), \
             patch("core.roxy_registration._wait_email_submit_next_state", return_value="otp") as wait_next, \
             patch("core.roxy_registration.human_delay"):
            state = roxy_registration._submit_email_and_wait_next(driver, "mail@example.test", attempts=1)

        self.assertEqual(state, "otp")
        wait_next.assert_called_once_with(driver, "mail@example.test", timeout=17)

    def test_profile_page_fast_fails_when_dom_stays_incomplete(self):
        driver = MagicMock()
        snapshot = {
            "url": "https://auth.openai.com/about-you",
            "inputs": [{"name": "name", "type": "text", "value": "Ready User"}],
            "widgets": [],
        }
        with patch.object(roxy_registration._cfg, "ROXY_PROFILE_STALL_LIMIT", 2), \
             patch("core.roxy_registration._has_access_token", return_value=False), \
             patch("core.roxy_registration._page_snapshot", return_value=snapshot), \
             patch("core.roxy_registration._is_profile_like", return_value=True), \
             patch("core.roxy_registration._select_or_type", return_value=True), \
             patch("core.roxy_registration._fill_birthday_or_age", return_value=None), \
             patch("core.roxy_registration.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "快速结束"):
                roxy_registration._complete_profile_page(
                    driver,
                    "Ready User",
                    "1990-01-01",
                    timeout=60,
                )

    def test_plain_data_type_birthday_segments_are_typed_and_verified(self):
        driver = MagicMock()
        segments = {
            '[data-type="year"]': MagicMock(),
            '[data-type="month"]': MagicMock(),
            '[data-type="day"]': MagicMock(),
        }
        driver.find_element.side_effect = lambda _by, selector: segments[selector]

        def execute(script, *_args):
            if "const birthday = String(arguments[0])" in script:
                return {"ok": False, "mode": "segmented_date_needed"}
            if "const numeric = el =>" in script:
                return {"ok": True, "year": 1990, "month": 1, "day": 2}
            return None

        driver.execute_script.side_effect = execute
        with patch("core.roxy_registration.time.sleep"):
            mode = roxy_registration._fill_birthday_or_age(driver, "1990-01-02", 36)

        self.assertEqual(mode, "segmented_date")
        self.assertIn(call("1990"), segments['[data-type="year"]'].send_keys.call_args_list)
        self.assertIn(call("01"), segments['[data-type="month"]'].send_keys.call_args_list)
        self.assertIn(call("02"), segments['[data-type="day"]'].send_keys.call_args_list)

    def test_profile_page_does_not_retype_name_when_target_value_is_present(self):
        driver = MagicMock()
        snapshot = {
            "url": "https://auth.openai.com/about-you",
            "inputs": [{"name": "name", "type": "text", "value": "Emma Roberts"}],
            "widgets": [
                {"dataType": "year", "value": "2026"},
                {"dataType": "month", "value": "08"},
                {"dataType": "day", "value": "23"},
            ],
        }
        with patch("core.roxy_registration._has_access_token", return_value=False), \
             patch("core.roxy_registration._page_snapshot", return_value=snapshot), \
             patch("core.roxy_registration._is_profile_like", return_value=True), \
             patch("core.roxy_registration._select_or_type") as type_name, \
             patch("core.roxy_registration._fill_birthday_or_age", return_value="segmented_date"), \
             patch("core.roxy_registration._accept_profile_consents"), \
             patch("core.roxy_registration._click_if_enabled_submit", return_value=True), \
             patch("core.roxy_registration.human_delay"), \
             patch("core.roxy_registration.time.sleep"):
            submitted = roxy_registration._complete_profile_page(
                driver, "Emma Roberts", "1990-01-02", timeout=10,
            )

        self.assertTrue(submitted)
        type_name.assert_not_called()

    def test_exit_geo_uses_same_proxy_preflight_when_browser_probe_is_temporarily_empty(self):
        selected = roxy_registration._select_registration_exit_geo(
            {},
            {"ip": "203.0.113.9", "country": "JP"},
            has_proxy=True,
        )
        self.assertEqual(selected["ip"], "203.0.113.9")
        self.assertEqual(selected["verification_source"], "same_proxy_preflight_fallback")

    def test_existing_otp_only_account_continues_when_signup_password_link_is_absent(self):
        driver = MagicMock()
        with patch.object(roxy_registration, "_click_signup_password_link_if_present", return_value=False):
            state = roxy_registration._switch_to_signup_password_branch(driver, "otp")
        self.assertEqual(state, "otp")

    def test_new_account_switches_to_signup_password_when_link_is_present(self):
        driver = MagicMock()
        with patch.object(roxy_registration, "_click_signup_password_link_if_present", return_value=True):
            state = roxy_registration._switch_to_signup_password_branch(driver, "otp")
        self.assertEqual(state, "password")

    def test_resend_atomic_snapshot_happens_before_click(self):
        driver = MagicMock()
        driver.execute_script.return_value = {"ok": True, "text": "もう一度試す", "kind": "retry"}
        with patch("core.roxy_registration.time.sleep"):
            action = roxy_registration._click_resend_email_otp(driver, timeout=1)
        self.assertEqual(action["kind"], "retry")
        script = next(
            call.args[0]
            for call in driver.execute_script.call_args_list
            if "target.click();" in call.args[0]
        )
        self.assertLess(script.index("const kind ="), script.index("target.click();"))

    def test_resend_acknowledgement_accepts_disabled_countdown_button(self):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/email-verification"
        before = {
            "url": driver.current_url,
            "text": "Check your inbox",
            "buttons": [{"text": "Resend email", "disabled": False}],
        }
        after = {
            "url": driver.current_url,
            "text": "Check your inbox",
            "buttons": [{"text": "Resend email in 59s", "disabled": True}],
        }
        with patch.object(roxy_registration, "_email_otp_page_state", return_value=after), \
             patch.object(roxy_registration, "_is_email_verification_page", return_value=True), \
             patch.object(roxy_registration.time, "sleep"):
            self.assertTrue(
                roxy_registration._wait_for_resend_acknowledgement(
                    driver,
                    before_state=before,
                    timeout=1,
                )
            )

    def test_stuck_otp_reload_reuses_visible_auth_url_once(self):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/email-verification"
        with patch.object(roxy_registration, "_safe_get") as safe_get, \
             patch.object(roxy_registration, "_page_warmup") as warmup, \
             patch.object(roxy_registration, "_wait_for_otp_input") as wait_input:
            self.assertEqual(roxy_registration._reload_stuck_otp_page(driver), "otp")
        safe_get.assert_called_once_with(
            driver,
            driver.current_url,
            timeout=30,
            attempts=1,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        warmup.assert_called_once_with(driver, reason="otp_resend_stuck_recovery")
        wait_input.assert_called_once_with(driver, timeout=20)

    def test_next_otp_attempt_reloads_once_when_resend_is_not_acknowledged(self):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/email-verification"
        with patch.object(roxy_registration, "_email_otp_page_state", return_value={}), \
             patch.object(roxy_registration, "_otp_flow_advanced_state", return_value=None), \
             patch.object(roxy_registration, "_is_email_login_page_still_present", return_value=False), \
             patch.object(roxy_registration, "_is_email_verification_page", return_value=True), \
             patch.object(
                 roxy_registration,
                 "_click_resend_email_otp",
                 side_effect=[{"ok": True, "kind": "resend", "acknowledged": False},
                              {"ok": True, "kind": "resend", "acknowledged": True}],
             ) as resend, \
             patch.object(roxy_registration, "_reload_stuck_otp_page", return_value="otp") as reload_page, \
             patch.object(roxy_registration, "_wait_for_otp_input") as wait_input:
            state = roxy_registration._prepare_next_email_otp_attempt(driver, "mail@example.test")
        self.assertEqual(state, "otp")
        reload_page.assert_called_once_with(driver)
        self.assertEqual([call.kwargs["timeout"] for call in resend.call_args_list], [25, 15])
        wait_input.assert_called_once_with(driver, timeout=30)

    def test_next_otp_attempt_stops_after_unacknowledged_resend_retry(self):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/email-verification"
        with patch.object(roxy_registration, "_email_otp_page_state", return_value={}), \
             patch.object(roxy_registration, "_otp_flow_advanced_state", return_value=None), \
             patch.object(roxy_registration, "_is_email_login_page_still_present", return_value=False), \
             patch.object(roxy_registration, "_is_email_verification_page", return_value=True), \
             patch.object(
                 roxy_registration,
                 "_click_resend_email_otp",
                 side_effect=[{"ok": True, "kind": "resend", "acknowledged": False},
                              {"ok": True, "kind": "resend", "acknowledged": False}],
             ), \
             patch.object(roxy_registration, "_reload_stuck_otp_page", return_value="otp"):
            with self.assertRaisesRegex(RuntimeError, "stopping blind polling"):
                roxy_registration._prepare_next_email_otp_attempt(driver, "mail@example.test")

    def test_atomic_otp_fill_remains_as_fallback_without_live_element(self):
        driver = MagicMock()
        driver.find_elements.return_value = []
        driver.execute_script.return_value = {"ok": True, "mode": "single", "values": ["123456"]}
        roxy_registration._type_otp(driver, "123456")
        driver.execute_script.assert_called_once()
        driver.find_elements.assert_called()

    def test_single_otp_prefers_real_webdriver_keys(self):
        driver = MagicMock()
        element = MagicMock()
        element.id = "otp-input"
        element.is_displayed.return_value = True
        element.is_enabled.return_value = True
        element.get_attribute.return_value = ""
        driver.find_elements.return_value = [element]
        with patch(
            "core.roxy_registration._email_otp_page_state",
            return_value={"inputs": [{"name": "code", "autocomplete": "one-time-code", "value": "123456"}]},
        ), patch("core.roxy_registration.time.sleep"):
            roxy_registration._type_otp(driver, "123456")
        self.assertEqual(
            [call.args[0] for call in element.send_keys.call_args_list],
            list("123456"),
        )

    def test_single_otp_preserves_leading_zeroes_with_character_key_events(self):
        driver = MagicMock()
        element = MagicMock()
        element.id = "otp-input"
        element.is_displayed.return_value = True
        element.is_enabled.return_value = True
        element.get_attribute.return_value = ""
        driver.find_elements.return_value = [element]
        with patch(
            "core.roxy_registration._email_otp_page_state",
            return_value={"inputs": [{"name": "code", "autocomplete": "one-time-code", "value": "001414"}]},
        ), patch("core.roxy_registration.time.sleep"):
            roxy_registration._type_otp(driver, "001414")
        self.assertEqual(
            [call.args[0] for call in element.send_keys.call_args_list],
            list("001414"),
        )

    def test_atomic_otp_fill_checks_segmented_boxes_before_single_input(self):
        driver = MagicMock()
        driver.execute_script.return_value = {"ok": True, "mode": "segmented", "values": list("123456")}
        roxy_registration._type_otp(driver, "123456")
        script = driver.execute_script.call_args.args[0]
        self.assertLess(
            script.index("if (boxes.length >= code.length)"),
            script.index("const single = candidates.find"),
        )

    def test_otp_page_without_explicit_error_is_pending(self):
        driver = MagicMock()
        with patch("core.roxy_registration.time.time", side_effect=[0.0, 0.0]), \
             patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._email_otp_page_state", return_value={"inputs": [], "errors": []}):
            outcome = roxy_registration._wait_after_email_otp_submit(driver, timeout=0)
        self.assertEqual(outcome, "pending")

    def test_pending_otp_submit_never_becomes_accepted(self):
        with self.assertRaisesRegex(RuntimeError, "without an acceptance signal"):
            roxy_registration._require_confirmed_otp_submit("pending", 20)

    def test_pending_otp_refreshes_refills_and_resubmits_same_code_once(self):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/email-verification"
        with patch("core.roxy_registration._wait_for_otp_input", return_value="otp_ready") as wait_input, \
             patch("core.roxy_registration._clear_otp_inputs") as clear_inputs, \
             patch("core.roxy_registration._type_otp") as type_otp, \
             patch("core.roxy_registration._click_continue") as click_continue:
            outcome = roxy_registration._reload_and_resubmit_otp_once(
                driver,
                "001414",
                timeout=6,
            )

        self.assertEqual(outcome, "submitted")
        driver.refresh.assert_called_once_with()
        wait_input.assert_called_once_with(driver, timeout=6)
        clear_inputs.assert_called_once_with(driver)
        type_otp.assert_called_once_with(driver, "001414")
        click_continue.assert_called_once_with(driver)

    def test_confirmed_otp_submit_state_is_preserved(self):
        self.assertEqual(
            roxy_registration._require_confirmed_otp_submit("accepted", 20),
            "accepted",
        )

    def test_clicks_signup_password_link_from_email_verification(self):
        driver = MagicMock()
        driver.execute_script.return_value = True
        states = iter([False, True])
        with patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._is_signup_password_page", side_effect=lambda _driver: next(states)), \
             patch("core.roxy_registration.time.sleep"):
            switched = roxy_registration._click_signup_password_link_if_present(driver)

        self.assertTrue(switched)
        script = driver.execute_script.call_args.args[0]
        self.assertIn("/create-account/password", script)
        self.assertIn("new URL(el.href, location.href).pathname", script)

    def test_does_not_click_signup_password_link_outside_email_verification(self):
        driver = MagicMock()
        with patch("core.roxy_registration._is_email_verification_page", return_value=False):
            switched = roxy_registration._click_signup_password_link_if_present(driver)

        self.assertFalse(switched)
        driver.execute_script.assert_not_called()

    def test_password_rejection_stops_before_otp_polling(self):
        driver = MagicMock()
        checkpoints = []
        driver.execute_script.return_value = {"ok": True, "reason": "submitted_password"}
        with patch("core.roxy_registration._is_email_verification_page", return_value=False), \
             patch("core.roxy_registration._has_access_token", side_effect=[False, True]), \
             patch("core.roxy_registration._is_signup_password_page", return_value=True), \
             patch("core.roxy_registration._is_login_password_page", return_value=False), \
             patch("core.roxy_registration._password_page_state", side_effect=[
                 {"url": "https://auth.openai.com/create-account/password", "errors": []},
                 {"url": "https://auth.openai.com/create-account/password", "errors": ["Failed to create account"]},
             ]), \
             patch("core.roxy_registration._click_passwordless_signup_if_present", return_value={"ok": False}), \
             patch("core.roxy_registration._registration_password", return_value="Password1!"), \
             patch("core.roxy_registration.human_delay"):
            with self.assertRaisesRegex(RuntimeError, "Failed to create account"):
                roxy_registration._fill_password_page_if_present(
                    driver,
                    "mail@example.test",
                    timeout=1,
                    allow_passwordless=False,
                    on_confirmed=lambda *args: checkpoints.append(args),
                )
        password_target_script = driver.execute_script.call_args.args[0]
        self.assertIn("HTMLInputElement.prototype", password_target_script)
        self.assertIn("buttons[0].el.click()", password_target_script)
        self.assertEqual(checkpoints, [])

    def test_password_form_noop_is_relocated_and_submitted_once_more(self):
        driver = MagicMock()
        checkpoints = []
        clock = iter(range(100))
        with patch("core.roxy_registration.time.time", side_effect=lambda: float(next(clock))), \
             patch("core.roxy_registration.time.sleep"), \
             patch("core.roxy_registration._submit_signup_password_direct", return_value={"ok": True}) as submit_password, \
             patch("core.roxy_registration._has_access_token", return_value=False), \
             patch("core.roxy_registration._is_signup_password_page", return_value=True), \
             patch("core.roxy_registration._is_login_password_page", return_value=False), \
             patch("core.roxy_registration._password_page_state", return_value={
                 "url": "https://auth.openai.com/create-account/password",
                 "errors": [],
             }), \
             patch("core.roxy_registration._click_passwordless_signup_if_present", return_value={"ok": False}), \
             patch("core.roxy_registration._registration_password", return_value="Password1!"), \
             patch("core.roxy_registration._is_email_verification_page", side_effect=lambda *_args: submit_password.call_count >= 2), \
             patch("core.roxy_registration.human_delay"), \
             patch("core.roxy_registration._cfg.ROXY_PASSWORD_SUBMIT_TIMEOUT", 6), \
             patch("core.roxy_registration._cfg.ROXY_PASSWORD_SUBMIT_ATTEMPTS", 2):
            password = roxy_registration._fill_password_page_if_present(
                driver,
                "mail@example.test",
                timeout=10,
                allow_passwordless=False,
                on_confirmed=lambda *args: checkpoints.append(args),
            )

        self.assertEqual(password, "Password1!")
        self.assertEqual(submit_password.call_count, 2)
        self.assertEqual(checkpoints, [("mail@example.test", "Password1!")])

    def test_email_already_verified_page_is_success_not_invalid_otp(self):
        driver = MagicMock()
        verified = {
            "title": "Email verified - OpenAI",
            "text": "Email verified\nYour email has already been verified",
            "inputs": [],
            "errors": [],
        }
        with patch("core.roxy_registration.time.time", side_effect=[0.0, 0.0]), \
             patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._email_otp_page_state", return_value=verified):
            outcome = roxy_registration._wait_after_email_otp_submit(driver, timeout=0)
        self.assertEqual(outcome, "email_verified")

    def test_japanese_email_verified_page_is_success(self):
        driver = MagicMock()
        verified = {
            "title": "メールが確認されました - OpenAI",
            "text": "メールが確認されました\nこのメールアドレスはすでに確認済みです。",
            "inputs": [],
            "errors": [],
        }
        with patch("core.roxy_registration.time.time", side_effect=[0.0, 0.0]), \
             patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._email_otp_page_state", return_value=verified):
            outcome = roxy_registration._wait_after_email_otp_submit(driver, timeout=0)
        self.assertEqual(outcome, "email_verified")

    def test_advanced_state_recognizes_email_verified_confirmation(self):
        driver = MagicMock()
        verified = {
            "title": "Email verified - OpenAI",
            "text": "Your email has already been verified",
        }
        with patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._email_otp_page_state", return_value=verified):
            state = roxy_registration._otp_flow_advanced_state(driver)
        self.assertEqual(state, "email_verified")

    def test_otp_submit_detects_redirect_back_to_email_login(self):
        driver = MagicMock()
        with patch("core.roxy_registration.time.sleep"), \
             patch("core.roxy_registration._is_email_verification_page", return_value=False), \
             patch("core.roxy_registration._is_email_login_page_still_present", return_value=True):
            outcome = roxy_registration._wait_after_email_otp_submit(driver, timeout=1)
        self.assertEqual(outcome, "email_login")

    def test_otp_submit_detects_account_deactivated_as_terminal(self):
        driver = MagicMock()
        page_state = {
            "title": "認証エラー - OpenAI",
            "text": "error_code: account_deactivated\nもう一度試す",
            "inputs": [],
            "errors": [],
        }
        with patch("core.roxy_registration.time.sleep"), \
             patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._email_otp_page_state", return_value=page_state):
            outcome = roxy_registration._wait_after_email_otp_submit(driver, timeout=1)
        self.assertEqual(outcome, "account_deactivated")

    def test_callback_resume_resubmits_email_when_login_form_is_empty(self):
        driver = MagicMock()
        with patch("core.roxy_registration._safe_get") as safe_get, \
             patch("core.roxy_registration.time.sleep"), \
             patch("core.roxy_registration._is_email_login_page_still_present", return_value=True), \
             patch("core.roxy_registration._submit_email_and_wait_next", return_value="otp") as submit_email:
            state = roxy_registration._resume_chatgpt_login_callback(driver, email="mail@example.test")
        self.assertEqual(state, "otp")
        safe_get.assert_called_once_with(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=45,
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        submit_email.assert_called_once_with(driver, "mail@example.test", attempts=2)

    def test_wait_for_otp_input_fails_fast_on_account_deactivated_page(self):
        driver = MagicMock()
        page_state = {
            "title": "認証エラー - OpenAI",
            "text": "error_code: account_deactivated",
            "inputs": [],
            "errors": [],
        }
        with patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._email_otp_page_state", return_value=page_state), \
             patch("core.roxy_registration.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "account_deactivated"):
                roxy_registration._wait_for_otp_input(driver, timeout=1)

    def test_wait_for_otp_input_accepts_email_verified_confirmation_page(self):
        driver = MagicMock()
        verified = {
            "url": "https://auth.openai.com/email-verification",
            "title": "Email verified - OpenAI",
            "text": "Your email has already been verified",
            "inputs": [],
            "errors": [],
        }
        with patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._email_otp_page_state", return_value=verified):
            self.assertEqual(
                roxy_registration._wait_for_otp_input(driver, timeout=1),
                "email_verified",
            )

    def test_stuck_otp_reload_propagates_email_verified_confirmation(self):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/email-verification"
        with patch.object(roxy_registration, "_safe_get"), \
             patch.object(roxy_registration, "_page_warmup"), \
             patch.object(roxy_registration, "_wait_for_otp_input", return_value="email_verified"):
            self.assertEqual(
                roxy_registration._reload_stuck_otp_page(driver),
                "email_verified",
            )

    def test_next_otp_attempt_resubmits_email_after_login_redirect(self):
        driver = MagicMock()
        with patch("core.roxy_registration._otp_flow_advanced_state", return_value="email_login"), \
             patch("core.roxy_registration._is_email_login_page_still_present", return_value=True), \
             patch("core.roxy_registration._submit_email_and_wait_next", return_value="otp") as submit_email, \
             patch("core.roxy_registration._wait_for_otp_input") as wait_input, \
             patch("core.roxy_registration._click_resend_email_otp") as resend:
            state = roxy_registration._prepare_next_email_otp_attempt(driver, "mail@example.test")
        self.assertEqual(state, "otp")
        submit_email.assert_called_once_with(driver, "mail@example.test", attempts=2)
        wait_input.assert_called_once_with(driver, timeout=30)
        resend.assert_not_called()

    def test_next_otp_attempt_uses_resend_only_while_still_on_otp_page(self):
        driver = MagicMock()
        with patch("core.roxy_registration._otp_flow_advanced_state", return_value=None), \
             patch("core.roxy_registration._is_email_login_page_still_present", return_value=False), \
             patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._click_resend_email_otp", return_value={"ok": True, "kind": "resend"}) as resend, \
             patch("core.roxy_registration._wait_for_otp_input") as wait_input:
            state = roxy_registration._prepare_next_email_otp_attempt(driver, "mail@example.test")
        self.assertEqual(state, "otp")
        resend.assert_called_once_with(driver, timeout=25)
        wait_input.assert_called_once_with(driver, timeout=30)

    def test_next_otp_attempt_recovers_chrome_navigation_error(self):
        driver = MagicMock()
        driver.current_url = "chrome-error://chromewebdata/"
        with patch("core.roxy_registration._safe_get") as safe_get, \
             patch("core.roxy_registration._page_warmup") as warmup, \
             patch("core.roxy_registration._submit_email_and_wait_next", return_value="otp") as submit_email, \
             patch("core.roxy_registration._wait_for_otp_input") as wait_input:
            state = roxy_registration._prepare_next_email_otp_attempt(driver, "mail@example.test")
        self.assertEqual(state, "otp")
        safe_get.assert_called_once_with(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=45,
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        warmup.assert_called_once_with(driver, reason="otp_navigation_error_recovery")
        submit_email.assert_called_once_with(driver, "mail@example.test", attempts=2)
        wait_input.assert_called_once_with(driver, timeout=30)

    def test_next_otp_attempt_recovers_navigation_error_from_wait_exception(self):
        driver = MagicMock()
        with patch("core.roxy_registration._is_browser_navigation_error", return_value=False), \
             patch("core.roxy_registration._otp_flow_advanced_state", return_value=None), \
             patch("core.roxy_registration._is_email_login_page_still_present", return_value=False), \
             patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._click_resend_email_otp", return_value={"ok": True, "kind": "resend"}), \
             patch("core.roxy_registration._wait_for_otp_input", side_effect=RuntimeError(
                 "等待 OTP 输入框超时: url=chrome-error://chromewebdata/; inputs=[]"
             )), \
             patch("core.roxy_registration._email_otp_page_state", return_value={"url": ""}), \
             patch("core.roxy_registration._restart_email_otp_from_login", return_value="otp") as restart:
            state = roxy_registration._prepare_next_email_otp_attempt(driver, "mail@example.test")
        self.assertEqual(state, "otp")
        restart.assert_called_once_with(driver, "mail@example.test", password_state=None)

    def test_next_otp_attempt_recovers_empty_otp_shell_after_resend(self):
        driver = MagicMock()
        with patch("core.roxy_registration._is_browser_navigation_error", return_value=False), \
             patch("core.roxy_registration._otp_flow_advanced_state", return_value=None), \
             patch("core.roxy_registration._is_signup_password_page", return_value=False), \
             patch("core.roxy_registration._is_email_login_page_still_present", return_value=False), \
             patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._click_resend_email_otp", return_value={"ok": True, "kind": "resend"}), \
             patch("core.roxy_registration._wait_for_otp_input", side_effect=RuntimeError(
                 "等待 OTP 输入框超时: url=https://auth.openai.com/email-verification; inputs=[]"
             )), \
             patch("core.roxy_registration._email_otp_page_state", return_value={
                 "url": "https://auth.openai.com/email-verification",
                 "inputs": [],
             }), \
             patch("core.roxy_registration._restart_email_otp_from_login", return_value="otp") as restart:
            state = roxy_registration._prepare_next_email_otp_attempt(driver, "mail@example.test")
        self.assertEqual(state, "otp")
        restart.assert_called_once_with(driver, "mail@example.test", password_state=None)

    def test_next_otp_attempt_resubmits_signup_password_page(self):
        driver = MagicMock()
        with patch("core.roxy_registration._is_browser_navigation_error", return_value=False), \
             patch("core.roxy_registration._otp_flow_advanced_state", return_value=None), \
             patch("core.roxy_registration._is_signup_password_page", return_value=True), \
             patch("core.roxy_registration.registration_password_required", return_value=False), \
             patch("core.roxy_registration._fill_password_page_if_present") as fill_password, \
             patch("core.roxy_registration._wait_for_otp_input") as wait_input, \
             patch("core.roxy_registration._click_resend_email_otp") as resend:
            state = roxy_registration._prepare_next_email_otp_attempt(driver, "mail@example.test")
        self.assertEqual(state, "otp")
        fill_password.assert_called_once_with(
            driver,
            "mail@example.test",
            timeout=25,
            allow_passwordless=True,
            password=None,
            on_confirmed=roxy_registration.persist_confirmed_registration_password,
        )
        wait_input.assert_called_once_with(driver, timeout=30)
        resend.assert_not_called()

    def test_disabled_twofa_never_falls_through_to_password_generation(self):
        driver = MagicMock()
        with patch("core.roxy_registration._is_email_verification_page", return_value=False), \
             patch("core.roxy_registration._has_access_token", return_value=False), \
             patch("core.roxy_registration._password_page_state", return_value={"errors": []}), \
             patch("core.roxy_registration._is_signup_password_page", return_value=True), \
             patch("core.roxy_registration._is_login_password_page", return_value=False), \
             patch("core.roxy_registration._click_passwordless_signup_if_present", return_value={"ok": False}), \
             patch("core.roxy_registration._registration_password", side_effect=AssertionError("must not generate")):
            with self.assertRaisesRegex(RuntimeError, "password_setup_disabled"):
                roxy_registration._fill_password_page_if_present(
                    driver,
                    "mail@example.test",
                    timeout=1,
                    allow_passwordless=True,
                )

    def test_otp_recovery_reuses_and_records_task_password(self):
        driver = MagicMock()
        state = {"desired": "Stable-pass-1!", "configured": None}
        with patch("core.roxy_registration._is_browser_navigation_error", return_value=False), \
             patch("core.roxy_registration._otp_flow_advanced_state", return_value=None), \
             patch("core.roxy_registration._is_signup_password_page", return_value=True), \
             patch("core.roxy_registration.registration_password_required", return_value=True), \
             patch("core.roxy_registration._fill_password_page_if_present", return_value="Stable-pass-1!") as fill_password, \
             patch("core.roxy_registration._wait_for_otp_input", return_value="otp"):
            result = roxy_registration._prepare_next_email_otp_attempt(
                driver,
                "mail@example.test",
                password_state=state,
            )
        self.assertEqual(result, "otp")
        self.assertEqual(state["configured"], "Stable-pass-1!")
        fill_password.assert_called_once_with(
            driver,
            "mail@example.test",
            timeout=25,
            allow_passwordless=False,
            password="Stable-pass-1!",
            on_confirmed=roxy_registration.persist_confirmed_registration_password,
        )

    def test_try_again_redirect_resubmits_email_instead_of_waiting_on_login_page(self):
        driver = MagicMock()
        with patch("core.roxy_registration._otp_flow_advanced_state", side_effect=[None, "email_login"]), \
             patch("core.roxy_registration._is_email_login_page_still_present", side_effect=[False, True]), \
             patch("core.roxy_registration._is_email_verification_page", return_value=True), \
             patch("core.roxy_registration._click_resend_email_otp", return_value={"ok": True, "kind": "retry"}), \
             patch("core.roxy_registration._submit_email_and_wait_next", return_value="otp") as submit_email, \
             patch("core.roxy_registration._wait_for_otp_input") as wait_input:
            state = roxy_registration._prepare_next_email_otp_attempt(driver, "mail@example.test")
        self.assertEqual(state, "otp")
        submit_email.assert_called_once_with(driver, "mail@example.test", attempts=2)
        wait_input.assert_called_once_with(driver, timeout=30)

    def test_continuous_rejected_otp_is_retryable(self):
        self.assertFalse(
            registration_service._should_disable_failed_registration_email(
                "RuntimeError: 邮箱验证码连续错误/过期，已达到最大重试次数"
            )
        )

    def test_existing_account_password_page_disables_mailbox(self):
        self.assertTrue(
            registration_service._should_disable_failed_registration_email(
                "RuntimeError: 邮箱提交后进入登录密码页: auth.openai.com/log-in/password"
            )
        )

    def test_account_deactivated_disables_mailbox(self):
        self.assertTrue(
            registration_service._should_disable_failed_registration_email(
                "RuntimeError: OpenAI 返回 account_deactivated：该邮箱对应的账号已删除或停用"
            )
        )

    def test_transient_mail_api_timeout_does_not_disable_mailbox(self):
        self.assertFalse(
            registration_service._should_disable_failed_registration_email(
                "GenericApiMailError: 等待通用 API 验证码超时"
            )
        )


if __name__ == "__main__":
    unittest.main()
