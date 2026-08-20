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

    @patch("core.browser_exit_geo.time.sleep")
    @patch("core.browser_exit_geo._probe_settings", return_value=(["https://geo.example/json"], 3.0))
    def test_selenium_probe_retries_until_exit_ip_is_available(self, _settings, sleep):
        driver = _SeleniumDriver([None, {"ip": "198.51.100.31", "country": "JP"}])
        geo = probe_selenium_driver_exit_geo(
            driver,
            label="Roxy注册",
            attempts=3,
            retry_delay=2,
        )
        self.assertEqual(geo["ip"], "198.51.100.31")
        self.assertEqual(driver.urls, ["https://geo.example/json", "https://geo.example/json"])
        sleep.assert_called_once_with(2.0)

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
