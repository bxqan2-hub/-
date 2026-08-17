# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from core import roxy_codex_oauth


class RoxyCodexPhoneClassificationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
