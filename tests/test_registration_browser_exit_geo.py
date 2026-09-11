# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from core.browser_exit_geo import (
    normalize_browser_exit_geo,
    probe_proxy_exit_geo,
    probe_playwright_context_exit_geo,
    probe_selenium_driver_exit_geo,
)


class _PlaywrightResponse:
    status = 200


class _PlaywrightProbePage:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []
        self.closed = False

    def set_default_timeout(self, _value):
        return None

    def set_default_navigation_timeout(self, _value):
        return None

    def goto(self, url, **_kwargs):
        self.urls.append(url)
        return _PlaywrightResponse()

    def evaluate(self, _script):
        return self.payload

    def close(self):
        self.closed = True


class _PlaywrightContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page


class _SeleniumSwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def new_window(self, _kind):
        self.driver.current_window_handle = "probe"

    def window(self, handle):
        self.driver.current_window_handle = handle


class _SeleniumDriver:
    def __init__(self, payload):
        self.payload = payload
        self.current_window_handle = "registration"
        self.switch_to = _SeleniumSwitchTo(self)
        self.urls = []
        self.closed_handles = []
        self.page_load_timeouts = []
        self.script_timeouts = []

    def set_page_load_timeout(self, value):
        self.page_load_timeouts.append(value)

    def set_script_timeout(self, value):
        self.script_timeouts.append(value)

    def get(self, url):
        self.urls.append(url)

    def execute_script(self, _script):
        if isinstance(self.payload, list):
            return self.payload.pop(0)
        return self.payload

    def close(self):
        self.closed_handles.append(self.current_window_handle)


class RegistrationBrowserExitGeoTests(unittest.TestCase):
    def test_normalizes_supported_geo_shape_and_requires_real_ip(self):
        self.assertEqual(
            normalize_browser_exit_geo(
                {
                    "query": "2001:db8::5",
                    "countryCode": "jp",
                    "regionName": "Tokyo",
                    "connection": {"org": "Residential ISP"},
                }
            ),
            {
                "ip": "2001:db8::5",
                "country": "JP",
                "region": "Tokyo",
                "city": None,
                "timezone": "",
                "org": "Residential ISP",
            },
        )
        self.assertEqual(normalize_browser_exit_geo({"ip": "proxy.example.test"}), {})
        self.assertEqual(normalize_browser_exit_geo({"ip": "203.0.113.7", "success": False}), {})
        self.assertEqual(normalize_browser_exit_geo({"ip": "203.0.113.7", "status": "fail"}), {})

    @patch("core.browser_exit_geo._probe_settings", return_value=(["https://geo.example/json"], 3.0))
    @patch("curl_cffi.requests.Session")
    def test_proxy_preflight_uses_exact_proxy_before_window_open(self, session_cls, _settings):
        response = session_cls.return_value.get.return_value
        response.status_code = 200
        response.json.return_value = {"ip": "203.0.113.7", "country": "jp"}
        geo = probe_proxy_exit_geo(
            "socks5h://proxy.example:1080",
            label="Roxy预检",
            attempts=3,
        )
        self.assertEqual(geo["ip"], "203.0.113.7")
        self.assertEqual(
            session_cls.return_value.proxies,
            {"http": "socks5h://proxy.example:1080", "https": "socks5h://proxy.example:1080"},
        )
        session_cls.return_value.get.assert_called_once()
        session_cls.assert_called_once_with(impersonate="chrome", trust_env=False)
        self.assertFalse(session_cls.return_value.get.call_args.kwargs["allow_redirects"])

    @patch("core.browser_exit_geo._probe_settings", return_value=(["https://geo.example/json"], 3.0))
    def test_playwright_probe_uses_temporary_page_in_same_context(self, _settings):
        page = _PlaywrightProbePage({"ip": "203.0.113.41", "country": "JP"})
        geo = probe_playwright_context_exit_geo(_PlaywrightContext(page), label="BrowserUse注册")

        self.assertEqual(geo["ip"], "203.0.113.41")
        self.assertEqual(page.urls, ["https://geo.example/json"])
        self.assertTrue(page.closed)

    @patch("core.browser_exit_geo._probe_settings", return_value=(["https://geo.example/json"], 3.0))
    def test_selenium_probe_restores_registration_tab(self, _settings):
        driver = _SeleniumDriver({"ip": "198.51.100.29", "country_code": "br"})
        geo = probe_selenium_driver_exit_geo(
            driver,
            label="Roxy注册",
            restore_page_load_timeout=90,
            restore_script_timeout=12,
        )

        self.assertEqual(geo, {
            "ip": "198.51.100.29",
            "country": "BR",
            "region": None,
            "city": None,
            "timezone": "",
            "org": None,
        })
        self.assertEqual(driver.urls, ["https://geo.example/json"])
        self.assertEqual(driver.closed_handles, ["probe"])
        self.assertEqual(driver.current_window_handle, "registration")
        self.assertEqual(driver.page_load_timeouts[-1], 90)
        self.assertEqual(driver.script_timeouts[-1], 12)

    @patch(
        "core.browser_exit_geo._probe_settings",
        return_value=(["https://geo-a.example/json", "https://geo-b.example/json"], 4.0),
    )
    def test_selenium_probe_stops_after_first_valid_configured_endpoint(self, _settings):
        driver = _SeleniumDriver({"ip": "198.51.100.30", "country": "GB"})

        geo = probe_selenium_driver_exit_geo(driver, label="Roxy registration")

        self.assertEqual(geo["ip"], "198.51.100.30")
        self.assertEqual(driver.urls, ["https://geo-a.example/json"])
        self.assertEqual(driver.page_load_timeouts, [4])

    @patch("core.browser_exit_geo.time.sleep")
    @patch("core.browser_exit_geo._probe_settings", return_value=(["https://geo-a.example/json", "https://geo-b.example/json"], 3.0))
    def test_selenium_probe_tries_next_endpoint_when_first_response_is_invalid(self, _settings, sleep):
        driver = _SeleniumDriver([None, {"ip": "198.51.100.31", "country": "JP"}])
        geo = probe_selenium_driver_exit_geo(
            driver,
            label="Roxy注册",
            attempts=3,
            retry_delay=2,
        )
        self.assertEqual(geo["ip"], "198.51.100.31")
        self.assertEqual(driver.urls, ["https://geo-a.example/json", "https://geo-b.example/json"])
        sleep.assert_not_called()

    @patch("core.browser_exit_geo._probe_settings", return_value=(["https://geo-a.example/json", "https://geo-b.example/json"], 8.0))
    def test_selenium_timeout_still_tries_next_endpoint_with_full_configured_budget(self, _settings):
        from selenium.common.exceptions import TimeoutException

        driver = _SeleniumDriver({"ip": "203.0.113.45", "country": "JP"})
        driver.get = MagicMock(side_effect=[TimeoutException("private-proxy-password"), None])
        with self.assertLogs("core.browser_exit_geo", level="INFO") as logs:
            result = probe_selenium_driver_exit_geo(driver, label="Roxy", restore_page_load_timeout=90, restore_script_timeout=20)
        self.assertEqual(result["ip"], "203.0.113.45")
        self.assertEqual([c.args[0] for c in driver.get.call_args_list], ["https://geo-a.example/json", "https://geo-b.example/json"])
        self.assertEqual(driver.page_load_timeouts, [8, 90])
        self.assertEqual(driver.script_timeouts, [20])
        self.assertNotIn("private-proxy-password", str(logs.output))
        self.assertIn("error_type=TimeoutException", str(logs.output))

    @patch("core.browser_exit_geo._probe_settings", return_value=(["https://geo.example/json"], 8.0))
    def test_selenium_probe_cancellation_restores_tab_and_propagates(self, _settings):
        driver = _SeleniumDriver(None)
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            probe_selenium_driver_exit_geo(driver, label="Roxy", stop_check=MagicMock(side_effect=RuntimeError("stopped")))
        self.assertEqual(driver.closed_handles, ["probe"])
        self.assertEqual(driver.current_window_handle, "registration")
        self.assertEqual(driver.urls, [])

    @patch("core.browser_exit_geo.time.sleep")
    @patch("core.browser_exit_geo._probe_settings", return_value=(["https://geo.example/json"], 8.0))
    def test_selenium_probe_empty_result_stays_bounded_and_does_not_use_preflight(self, _settings, sleep):
        driver = _SeleniumDriver(None)
        self.assertEqual(probe_selenium_driver_exit_geo(driver, label="Roxy", attempts=0), {})
        self.assertEqual(driver.urls, ["https://geo.example/json"])
        sleep.assert_not_called()

    @patch("core.browser_exit_geo._probe_settings", return_value=(["https://geo-a.example/json?token=secret", "https://geo-b.example/json"], 8.0))
    @patch("curl_cffi.requests.Session")
    def test_preflight_logs_http_failure_without_private_query_or_response_body(self, session_cls, _settings):
        invalid = MagicMock(status_code=429)
        valid = MagicMock(status_code=200)
        valid.json.return_value = {"ip": "203.0.113.46"}
        session_cls.return_value.get.side_effect = [invalid, valid]
        with self.assertLogs("core.browser_exit_geo", level="INFO") as logs:
            result = probe_proxy_exit_geo("socks5h://private-user:private-password@proxy.example:1080", label="Roxy")
        self.assertEqual(result["ip"], "203.0.113.46")
        self.assertIn("http_status=429", str(logs.output))
        for value in ("token=secret", "private-user", "private-password"):
            self.assertNotIn(value, str(logs.output))

    @patch("core.browser_exit_geo.time.sleep")
    @patch("core.browser_exit_geo._probe_settings", return_value=(["https://geo.example/json"], 3.0))
    @patch("curl_cffi.requests.Session")
    def test_proxy_preflight_zero_attempts_is_bounded_to_one(self, session_cls, _settings, sleep):
        first = MagicMock(status_code=503)
        second = MagicMock(status_code=200)
        second.json.return_value = {"ip": "203.0.113.99", "country": "JP"}
        session_cls.return_value.get.side_effect = [first, second]

        geo = probe_proxy_exit_geo(
            "socks5h://proxy.example:1080",
            label="Roxy预检",
            attempts=0,
            retry_delay=1,
        )

        self.assertEqual(geo, {})
        self.assertEqual(session_cls.return_value.get.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
