# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import roxy_codex_oauth, sms_provider


class SmsProviderConfigurationTests(unittest.TestCase):
    def test_reports_all_missing_required_fields(self):
        with (
            patch.object(sms_provider._cfg, "SMS_API_BASE", ""),
            patch.object(sms_provider._cfg, "SMS_API_KEY", ""),
            patch.object(sms_provider._cfg, "SMS_SERVICE", ""),
            patch.object(sms_provider._cfg, "SMS_COUNTRY", ""),
        ):
            with self.assertRaises(sms_provider.SmsProviderConfigurationError) as ctx:
                sms_provider.validate_configuration()

        message = str(ctx.exception)
        self.assertIn("SMS_API_BASE", message)
        self.assertIn("SMS_API_KEY", message)
        self.assertIn("SMS_SERVICE", message)
        self.assertIn("SMS_COUNTRY", message)

    def test_valid_herosms_configuration(self):
        with (
            patch.object(sms_provider._cfg, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"),
            patch.object(sms_provider._cfg, "SMS_API_KEY", "secret"),
            patch.object(sms_provider._cfg, "SMS_SERVICE", "dr"),
            patch.object(sms_provider._cfg, "SMS_COUNTRY", "187"),
            patch.object(sms_provider._cfg, "SMS_MAX_PRICE", "0.15"),
        ):
            self.assertEqual(sms_provider.validate_configuration(), "herosms")

    def test_valid_auto_country_configuration(self):
        with (
            patch.object(sms_provider._cfg, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"),
            patch.object(sms_provider._cfg, "SMS_API_KEY", "secret"),
            patch.object(sms_provider._cfg, "SMS_SERVICE", "dr"),
            patch.object(sms_provider._cfg, "SMS_COUNTRY", "auto"),
            patch.object(sms_provider._cfg, "SMS_MAX_PRICE", "0.15"),
        ):
            self.assertEqual(sms_provider.validate_configuration(), "herosms")

    def test_auto_country_requires_price_limit(self):
        with (
            patch.object(sms_provider._cfg, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"),
            patch.object(sms_provider._cfg, "SMS_API_KEY", "secret"),
            patch.object(sms_provider._cfg, "SMS_SERVICE", "dr"),
            patch.object(sms_provider._cfg, "SMS_COUNTRY", "auto"),
            patch.object(sms_provider._cfg, "SMS_MAX_PRICE", ""),
        ):
            with self.assertRaises(sms_provider.SmsProviderConfigurationError) as ctx:
                sms_provider.validate_configuration()

        self.assertIn("SMS_MAX_PRICE", str(ctx.exception))

    def test_country_must_be_numeric_herosms_id(self):
        with (
            patch.object(sms_provider._cfg, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"),
            patch.object(sms_provider._cfg, "SMS_API_KEY", "secret"),
            patch.object(sms_provider._cfg, "SMS_SERVICE", "dr"),
            patch.object(sms_provider._cfg, "SMS_COUNTRY", "us"),
        ):
            with self.assertRaises(sms_provider.SmsProviderConfigurationError) as ctx:
                sms_provider.validate_configuration()

        self.assertIn("数字国家 ID", str(ctx.exception))

    def test_acquire_number_fails_before_http_when_key_is_missing(self):
        fake_http = unittest.mock.MagicMock()
        with (
            patch.object(sms_provider._cfg, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"),
            patch.object(sms_provider._cfg, "SMS_API_KEY", ""),
            patch.object(sms_provider._cfg, "SMS_SERVICE", "dr"),
            patch.object(sms_provider._cfg, "SMS_COUNTRY", "187"),
        ):
            with self.assertRaises(sms_provider.SmsProviderConfigurationError):
                sms_provider.acquire_number(http=fake_http)

        fake_http.get.assert_not_called()

    def test_roxy_phone_flow_does_not_retry_invalid_config(self):
        driver = unittest.mock.MagicMock()
        error = sms_provider.SmsProviderConfigurationError("SMS_API_KEY missing")
        with (
            patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True),
            patch.object(roxy_codex_oauth, "_is_phone_code_page", return_value=False),
            patch.object(sms_provider, "validate_configuration", side_effect=error) as validate,
            patch.object(sms_provider, "_http") as make_http,
            patch.object(sms_provider, "acquire_number") as acquire,
        ):
            with self.assertRaises(sms_provider.SmsProviderConfigurationError):
                roxy_codex_oauth._do_phone_verification_if_present(driver)

        validate.assert_called_once_with()
        make_http.assert_not_called()
        acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
