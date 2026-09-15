# -*- coding: utf-8 -*-
from contextlib import ExitStack
import unittest
from unittest.mock import MagicMock, patch

from core import roxy_codex_oauth


class RoxyCodexPhoneClassificationTests(unittest.TestCase):
    def test_invalid_auth_step_stops_before_duplicate_phone_submit(self):
        state = {"url": "https://auth.openai.com/add-phone", "bodyText": "error_code: invalid_auth_step", "inputs": [], "forms": []}
        with (
            patch.object(roxy_codex_oauth, "_phone_page_state", return_value=state),
            patch.object(roxy_codex_oauth, "_force_submit_add_phone_form") as submit,
            patch.object(roxy_codex_oauth.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stage=phone_auth; invalid_auth_step"):
                roxy_codex_oauth._wait_after_phone_send(MagicMock(), timeout=120)
        submit.assert_not_called()

    def test_expired_phone_auth_stops_before_acquiring_another_number(self):
        driver = MagicMock()
        state = {"url": "https://auth.openai.com/add-phone", "bodyText": "error_code: invalid_auth_step", "inputs": [], "forms": []}
        with (
            patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True),
            patch.object(roxy_codex_oauth, "_phone_page_state", return_value=state),
            patch.object(roxy_codex_oauth.sms_provider, "validate_configuration", return_value="smsbower"),
            patch.object(roxy_codex_oauth.sms_provider, "_http"),
            patch.object(roxy_codex_oauth.sms_provider, "acquire_number") as acquire,
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid_auth_step"):
                roxy_codex_oauth._do_phone_verification_if_present(driver)
        acquire.assert_not_called()
        driver.get.assert_not_called()

    def test_whatsapp_label_does_not_override_selected_sms(self):
        state = {
            "url": "https://auth.openai.com/add-phone",
            "radios": [
                {"value": "sms", "checked": True},
                {"value": "whatsapp", "checked": False},
            ],
            "inputs": [{"type": "tel", "name": "phone"}],
            "forms": [{"action": "/add-phone"}],
            "bodyText": "SMS WhatsApp",
        }

        self.assertEqual(roxy_codex_oauth._classify_phone_page_failure(state), "")

    def test_checked_whatsapp_with_sms_available_is_not_misclassified(self):
        state = {
            "url": "https://auth.openai.com/add-phone",
            "radios": [
                {"value": "sms", "checked": False},
                {"value": "whatsapp", "checked": True},
            ],
            "inputs": [{"type": "tel", "name": "phone"}],
            "forms": [{"action": "/add-phone"}],
            "bodyText": "WhatsApp",
        }

        self.assertEqual(roxy_codex_oauth._classify_phone_page_failure(state), "")

    def test_whatsapp_only_page_is_rejected(self):
        state = {
            "url": "https://auth.openai.com/add-phone",
            "radios": [{"value": "whatsapp", "checked": True}],
            "inputs": [{"type": "tel", "name": "phone"}],
            "forms": [{"action": "/add-phone"}],
            "bodyText": "WhatsApp",
        }

        self.assertEqual(roxy_codex_oauth._classify_phone_page_failure(state), "whatsapp_channel")

    def test_phone_code_page_detects_whatsapp_delivery(self):
        state = {
            "url": "https://auth.openai.com/phone-verification",
            "bodyText": "We sent your verification code to WhatsApp.",
        }

        self.assertTrue(roxy_codex_oauth._phone_code_uses_whatsapp(state))

    def test_phone_code_page_does_not_misclassify_sms_delivery(self):
        state = {
            "url": "https://auth.openai.com/phone-verification",
            "bodyText": "We sent a text message with your verification code.",
        }

        self.assertFalse(roxy_codex_oauth._phone_code_uses_whatsapp(state))

    def test_formatted_visible_number_is_accepted_when_hidden_e164_matches(self):
        driver = MagicMock()
        driver.execute_script.return_value = {
            "ok": True,
            "visibleValue": "+44 07365 879495",
            "hiddenValue": "+447365879495",
            "expected": "+447365879495",
            "hiddenMatches": True,
        }

        result = roxy_codex_oauth._verify_add_phone_value_before_submit(
            driver,
            "+447365879495",
        )

        self.assertTrue(result["ok"])

    def test_phone_send_waits_for_dom_transition_before_rotating(self):
        driver = MagicMock()
        clock = [0.0]
        add_phone_state = {
            "url": "https://auth.openai.com/add-phone",
            "inputs": [{"type": "tel", "ariaInvalid": ""}],
            "forms": [{"action": "/add-phone"}],
        }

        def now():
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds

        with (
            patch.object(roxy_codex_oauth, "_phone_page_state", return_value=add_phone_state),
            patch.object(roxy_codex_oauth, "_is_phone_code_state", return_value=False),
            patch.object(roxy_codex_oauth, "_is_phone_code_page", return_value=False),
            patch.object(roxy_codex_oauth, "_is_add_phone_page", return_value=True),
            patch.object(roxy_codex_oauth, "_force_submit_add_phone_form", return_value={"ok": True}) as force_submit,
            patch.object(roxy_codex_oauth.time, "time", side_effect=now),
            patch.object(roxy_codex_oauth.time, "sleep", side_effect=sleep),
        ):
            with self.assertRaisesRegex(RuntimeError, "send_not_accepted"):
                roxy_codex_oauth._wait_after_phone_send(driver, timeout=120)

        force_submit.assert_called_once_with(driver)
        self.assertGreaterEqual(clock[0], 120)

    def test_retry_keeps_manual_sms_selection_and_restarts_authorization(self):
        """手机号收不到码时，重取同国家/供应商号码并要求外层重建授权事务。"""
        driver = MagicMock()
        http = MagicMock()
        restart_authorization = MagicMock()
        acquire = MagicMock(side_effect=[("activation-1", "15551230001"), ("activation-2", "15551230002")])

        with ExitStack() as stack:
            for target, kwargs in (
                (roxy_codex_oauth.sms_provider._cfg, {"SMS_COUNTRY": "187", "SMSBOWER_PROVIDER_ID": "3370", "SMS_MAX_RETRIES": 2}),
            ):
                for name, value in kwargs.items():
                    stack.enter_context(patch.object(target, name, value))
            patches = (
                patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True),
                patch.object(roxy_codex_oauth, "_is_phone_code_page", return_value=False),
                patch.object(roxy_codex_oauth.sms_provider, "validate_configuration", return_value="smsbower"),
                patch.object(roxy_codex_oauth.sms_provider, "_http", return_value=http),
                patch.object(roxy_codex_oauth.sms_provider, "acquire_number", acquire),
                patch.object(roxy_codex_oauth.sms_provider, "activation_country", return_value="187"),
                patch.object(roxy_codex_oauth.sms_provider, "cancel"),
                patch.object(roxy_codex_oauth.sms_provider, "complete"),
                patch.object(roxy_codex_oauth, "_ensure_add_phone_input"),
                patch.object(roxy_codex_oauth, "_set_phone_value", return_value={"e164": "+15551230001"}),
                patch.object(roxy_codex_oauth, "_blur_active_input_and_wait"),
                patch.object(roxy_codex_oauth, "_verify_add_phone_value_before_submit", return_value={"ok": True}),
                patch.object(roxy_codex_oauth, "_select_sms_channel_or_raise"),
                patch.object(roxy_codex_oauth, "_click_add_phone_continue_button", return_value={"ok": True}),
                patch.object(roxy_codex_oauth, "_wait_after_phone_send", side_effect=[RuntimeError("fixture timeout"), "code_page"]),
                patch.object(roxy_codex_oauth.sms_provider, "wait_for_sms_code", return_value="123456"),
                patch.object(roxy_codex_oauth, "_clear_otp_inputs"),
                patch.object(roxy_codex_oauth, "_type_otp"),
                patch.object(roxy_codex_oauth, "_click_if_present", return_value=True),
                patch.object(roxy_codex_oauth, "_wait_after_phone_otp_submit", return_value="left_phone_flow"),
                patch.object(roxy_codex_oauth, "_find_any"),
                patch.object(roxy_codex_oauth, "_sleep_before_phone_retry"),
                patch.object(roxy_codex_oauth.time, "sleep"),
            )
            for item in patches:
                stack.enter_context(item)
            roxy_codex_oauth._do_phone_verification_if_present(
                driver,
                restart_authorization=restart_authorization,
            )

        self.assertEqual(acquire.call_count, 2)
        for call in acquire.call_args_list:
            self.assertEqual(call.kwargs["country"], "187")
            self.assertEqual(call.kwargs["provider_id"], "3370")
            self.assertEqual(call.kwargs["excluded_countries"], set())
        restart_authorization.assert_called_once_with()
        driver.refresh.assert_not_called()
        driver.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
