# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import codex as codex_config
from config import env_loader
from core import sms_provider
from webui import config_editor


class _Response:
    status_code = 200

    def __init__(self, text):
        self.text = text


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params or {}})
        return _Response(self.responses.pop(0))

    def close(self):
        pass


class HeroSmsProviderTests(unittest.TestCase):
    def setUp(self):
        with sms_provider._ACTIVATION_LOCK:
            sms_provider._ACTIVATION_META.clear()
            sms_provider._SCHEDULED_CANCELS.clear()
        self.config = (
            patch.object(codex_config, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"),
            patch.object(codex_config, "SMS_API_KEY", "api-key"),
            patch.object(codex_config, "SMS_SERVICE", "dr"),
            patch.object(codex_config, "SMS_COUNTRY", "187"),
            patch.object(codex_config, "SMS_MAX_PRICE", "0.25"),
        )
        for item in self.config:
            item.start()

    def tearDown(self):
        for item in reversed(self.config):
            item.stop()
        with sms_provider._ACTIVATION_LOCK:
            sms_provider._ACTIVATION_META.clear()
            sms_provider._SCHEDULED_CANCELS.clear()

    def test_secret_registry_and_webui_only_include_herosms(self):
        self.assertEqual(env_loader.SECRET_ENV_KEYS["SMS_API_KEY"], "HeroSMS API Key")
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertIn("SMS_API_BASE", fields)
        self.assertTrue(fields["SMS_API_KEY"].get("secret"))
        self.assertNotIn("SMS_PROVIDER", fields)
        self.assertFalse(any(key.startswith(("H_", "L_")) for key in fields))

    def test_http_session_uses_codex_local_proxy(self):
        with patch.object(codex_config, "CODEX_LOCAL_PROXY", "http://127.0.0.1:7890"):
            http = sms_provider._http()
        try:
            self.assertEqual(http.proxies["http"], "http://127.0.0.1:7890")
            self.assertEqual(http.proxies["https"], "http://127.0.0.1:7890")
        finally:
            http.close()

    def test_acquire_number_uses_herosms_get_number(self):
        http = _Http(["ACCESS_NUMBER:activation-1:+12025550123"])

        activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("activation-1", "12025550123"))
        self.assertEqual(http.calls[0]["url"], "https://hero-sms.com/stubs/handler_api.php")
        self.assertEqual(
            http.calls[0]["params"],
            {
                "api_key": "api-key",
                "action": "getNumber",
                "service": "dr",
                "country": "187",
                "maxPrice": "0.25",
            },
        )

    def test_auto_country_selects_lowest_affordable_offer(self):
        http = _Http([
            '{"1":{"dr":{"cost":0.12,"count":3}},"2":{"dr":{"cost":0.09,"count":2}},"3":{"dr":{"cost":0.05,"count":0}},"4":{"dr":{"cost":0.18,"count":5}}}',
            '{"1":{"id":1,"eng":"Country A"},"2":{"id":2,"eng":"Country B"}}',
            "ACCESS_NUMBER:activation-2:+447700900123",
        ])

        with (
            patch.object(codex_config, "SMS_COUNTRY", "auto"),
            patch.object(codex_config, "SMS_MAX_PRICE", "0.15"),
        ):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("activation-2", "447700900123"))
        self.assertEqual(http.calls[0]["params"]["action"], "getPrices")
        self.assertEqual(http.calls[1]["params"]["action"], "getCountries")
        self.assertEqual(
            http.calls[2]["params"],
            {
                "api_key": "api-key",
                "action": "getNumber",
                "service": "dr",
                "country": "2",
                "maxPrice": "0.15",
            },
        )

    def test_auto_country_prioritizes_spain_mexico_colombia(self):
        http = _Http([
            '{"2":{"dr":{"cost":0.01,"count":10}},"56":{"dr":{"cost":0.14,"count":10}},"54":{"dr":{"cost":0.13,"count":10}},"33":{"dr":{"cost":0.12,"count":10}}}',
            '{"2":{"id":2,"eng":"Kazakhstan"},"56":{"id":56,"eng":"Spain"},"54":{"id":54,"eng":"Mexico"},"33":{"id":33,"eng":"Colombia"}}',
            "ACCESS_NUMBER:activation-es:+34600111222",
        ])

        with (
            patch.object(codex_config, "SMS_COUNTRY", "auto"),
            patch.object(codex_config, "SMS_PRIORITY_COUNTRIES", "56,54,33"),
            patch.object(codex_config, "SMS_MAX_PRICE", "0.15"),
        ):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("activation-es", "34600111222"))
        self.assertEqual(http.calls[2]["params"]["country"], "56")

    def test_auto_country_skips_failed_country(self):
        http = _Http([
            '{"1":{"dr":{"cost":0.12,"count":3}},"2":{"dr":{"cost":0.09,"count":2}}}',
            '{"1":{"id":1,"eng":"Country A"},"2":{"id":2,"eng":"Country B"}}',
            "ACCESS_NUMBER:activation-3:+12025550124",
        ])

        with (
            patch.object(codex_config, "SMS_COUNTRY", "auto"),
            patch.object(codex_config, "SMS_MAX_PRICE", "0.15"),
        ):
            activation_id, phone = sms_provider.acquire_number(http=http, excluded_countries={"2"})

        self.assertEqual((activation_id, phone), ("activation-3", "12025550124"))
        self.assertEqual(http.calls[2]["params"]["country"], "1")
        self.assertEqual(sms_provider.activation_country(activation_id), "1")

    def test_auto_country_permanently_excludes_philippines(self):
        http = _Http([
            '{"4":{"dr":{"cost":0.01,"count":9999}},"16":{"dr":{"cost":0.04,"count":50}}}',
            '{"4":{"id":4,"chn":"菲律宾"},"16":{"id":16,"eng":"England"}}',
            "ACCESS_NUMBER:activation-uk:+447700900999",
        ])

        with (
            patch.object(codex_config, "SMS_COUNTRY", "auto"),
            patch.object(codex_config, "SMS_EXCLUDED_COUNTRIES", "4"),
        ):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("activation-uk", "447700900999"))
        self.assertEqual(http.calls[2]["params"]["country"], "16")

    def test_fixed_philippines_country_is_rejected_by_configuration(self):
        with (
            patch.object(codex_config, "SMS_COUNTRY", "4"),
            patch.object(codex_config, "SMS_EXCLUDED_COUNTRIES", "4"),
        ):
            with self.assertRaisesRegex(sms_provider.SmsProviderConfigurationError, "永久排除"):
                sms_provider.validate_configuration()

    def test_wait_for_sms_code_polls_get_status(self):
        http = _Http(["STATUS_WAIT_CODE", "STATUS_OK:123456"])

        code = sms_provider.wait_for_sms_code("activation-1", http=http, max_wait=1, poll_interval=0)

        self.assertEqual(code, "123456")
        self.assertEqual(http.calls[0]["params"]["action"], "getStatus")
        self.assertEqual(http.calls[0]["params"]["id"], "activation-1")

    def test_lists_only_affordable_countries_with_stock(self):
        http = _Http([
            '{"1":{"dr":{"cost":0.20,"count":5}},"2":{"dr":{"cost":0.60,"count":9}},"3":{"dr":{"cost":0.10,"count":0}}}',
            '{"1":{"id":1,"chn":"国家甲","eng":"Country A","iso":"AA"},"2":{"id":2,"chn":"国家乙","iso":"BB"}}',
        ])

        countries = sms_provider.list_affordable_countries(service="dr", max_price="0.50", http=http)

        self.assertEqual(countries, [{
            "id": "1", "name": "国家甲", "iso": "AA", "price": 0.2,
            "count": 5, "service": "dr",
        }])
        self.assertEqual(http.calls[0]["params"]["action"], "getPrices")
        self.assertEqual(http.calls[1]["params"]["action"], "getCountries")

    def test_complete_and_cancel_use_supported_statuses(self):
        http = _Http(["ACCESS_ACTIVATION", "ACCESS_CANCEL"])

        sms_provider.complete("activation-1", http=http)
        sms_provider.cancel("activation-2", http=http)

        self.assertEqual(http.calls[0]["params"]["status"], "6")
        self.assertEqual(http.calls[1]["params"]["status"], "8")

    def test_cancel_waits_for_minimum_activation_age_in_background(self):
        acquired_http = _Http(["ACCESS_NUMBER:activation-delay:+12025550125"])
        with patch.object(sms_provider.time, "time", return_value=1000):
            sms_provider.acquire_number(http=acquired_http)

        scheduled = []

        class _Thread:
            def __init__(self, target, args, name, daemon):
                scheduled.append((target, args, name, daemon))

            def start(self):
                pass

        immediate_http = _Http([])
        with (
            patch.object(sms_provider.time, "time", return_value=1010),
            patch.object(sms_provider.threading, "Thread", _Thread),
        ):
            sms_provider.cancel("activation-delay", http=immediate_http)

        self.assertEqual(immediate_http.calls, [])
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][1][0], "activation-delay")

        delayed_http = _Http(["ACCESS_CANCEL"])
        with (
            patch.object(sms_provider, "_http", return_value=delayed_http),
            patch.object(sms_provider.time, "sleep") as sleep,
        ):
            scheduled[0][0](*scheduled[0][1])

        sleep.assert_called_once_with(115)
        self.assertEqual(delayed_http.calls[0]["params"]["status"], "8")
        self.assertEqual(sms_provider.activation_country("activation-delay"), "")

    def test_status_one_is_local_no_op(self):
        http = _Http([])

        result = sms_provider.set_status("activation-1", 1, http=http)

        self.assertEqual(result, "NO_ACTION")
        self.assertEqual(http.calls, [])

    def test_maps_no_numbers_and_no_balance(self):
        with self.assertRaises(sms_provider.SmsNoNumbersError):
            sms_provider.acquire_number(http=_Http(["NO_NUMBERS"]))
        with self.assertRaises(sms_provider.SmsNoBalanceError):
            sms_provider.acquire_number(http=_Http(["NO_BALANCE"]))


if __name__ == "__main__":
    unittest.main()
