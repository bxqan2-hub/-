from unittest import TestCase
from unittest.mock import MagicMock, patch

import main
from config import browser as browser_config
from core.session import BrowserSession


_EXIT_GEO = {
    "ip": "203.0.113.7",
    "country": "JP",
    "timezone": "Asia/Tokyo",
}


class ProtocolFingerprintAlignmentTests(TestCase):
    def test_default_profile_aligns_tls_ua_client_hints_and_os(self):
        profile = browser_config.build_browser_environment(
            _EXIT_GEO,
            browser_config.HAR_CAPTURE_BASE_PROFILE,
        )

        self.assertEqual(browser_config.validate_browser_profile(profile), [])
        self.assertEqual(profile["impersonate"], "chrome146")
        self.assertIn("Chrome/146.0.0.0", profile["user_agent"])
        self.assertIn('v="146"', profile["sec_ch_ua"])
        self.assertEqual(profile["browser_os"], "macOS")
        self.assertEqual(profile["sec_ch_ua_platform"], '"macOS"')
        self.assertEqual(profile["navigator_platform"], "MacIntel")
        self.assertEqual(profile["user_agent_data_platform"], "macOS")

    def test_validator_reports_tls_ua_and_os_mismatches(self):
        profile = browser_config.build_browser_environment(
            _EXIT_GEO,
            browser_config.HAR_CAPTURE_BASE_PROFILE,
        )
        profile.update({
            "impersonate": "chrome145",
            "user_agent": profile["user_agent"].replace("Macintosh", "Windows"),
            "sec_ch_ua_platform": '"Windows"',
        })

        issues = browser_config.validate_browser_profile(profile)

        self.assertIn("TLS impersonate 与 UA Chrome 主版本不一致", issues)
        self.assertIn("macOS 画像但 UA 不是 Macintosh", issues)
        self.assertIn("macOS 画像但 sec-ch-ua-platform 不是 macOS", issues)

    @patch.object(BrowserSession, "_detect_exit_geo", return_value=_EXIT_GEO)
    def test_strict_session_pins_proxy_transport_and_alignment(self, _detect_exit_geo):
        session = BrowserSession(
            proxy="http://127.0.0.1:65535",
            require_proxy=True,
            strict_fingerprint=True,
        )
        self.addCleanup(session.session.close)

        alignment = session.fingerprint_alignment()
        self.assertEqual(alignment["tls"], "chrome146")
        self.assertEqual(alignment["ua_major"], "146")
        self.assertEqual(alignment["os"], "macOS")
        self.assertTrue(alignment["proxy_enforced"])
        self.assertEqual(alignment["exit_ip"], "203.0.113.7")
        self.assertFalse(session.session.trust_env)
        self.assertEqual(session.session.proxies["http"], session.proxy)
        self.assertEqual(session.session.proxies["https"], session.proxy)

    @patch.object(BrowserSession, "_detect_exit_geo", return_value=_EXIT_GEO)
    @patch("core.session.pick_proxy", return_value="http://selected-proxy.example:8080")
    def test_strict_session_resolves_the_selected_pool_proxy_once(self, pick_proxy, _detect_exit_geo):
        session = BrowserSession(require_proxy=True, strict_fingerprint=True)
        self.addCleanup(session.session.close)

        pick_proxy.assert_called_once_with(strict=True)
        self.assertEqual(session.proxy, "http://selected-proxy.example:8080")

    @patch("core.session.pick_proxy", return_value="")
    def test_strict_session_fails_closed_without_proxy(self, _pick_proxy):
        with self.assertRaisesRegex(RuntimeError, "要求严格代理出口"):
            BrowserSession(require_proxy=True, strict_fingerprint=True)

    def test_strict_session_rejects_socks5_local_dns(self):
        with self.assertRaisesRegex(RuntimeError, "socks5h"):
            BrowserSession(
                proxy="socks5://127.0.0.1:1080",
                require_proxy=True,
                strict_fingerprint=True,
            )

    @patch.object(BrowserSession, "_detect_exit_geo", return_value={})
    def test_strict_session_requires_verified_exit_ip(self, _detect_exit_geo):
        with self.assertRaisesRegex(RuntimeError, "确认出口 IP"):
            BrowserSession(
                proxy="http://127.0.0.1:65535",
                require_proxy=True,
                strict_fingerprint=True,
            )

    @patch.object(BrowserSession, "_detect_exit_geo", return_value=_EXIT_GEO)
    def test_strict_session_rejects_request_level_route_or_ua_override(self, _detect_exit_geo):
        session = BrowserSession(
            proxy="http://127.0.0.1:65535",
            require_proxy=True,
            strict_fingerprint=True,
        )
        self.addCleanup(session.session.close)

        with self.assertRaisesRegex(RuntimeError, "请求级覆盖"):
            session.get("https://example.invalid/", proxy="http://127.0.0.1:1")
        with self.assertRaisesRegex(RuntimeError, "User-Agent"):
            session.get("https://example.invalid/", headers={"User-Agent": "mismatched"})

    @patch.object(BrowserSession, "_detect_exit_geo", return_value=_EXIT_GEO)
    def test_strict_request_injects_the_session_ua_and_client_hints(self, _detect_exit_geo):
        session = BrowserSession(
            proxy="http://127.0.0.1:65535",
            require_proxy=True,
            strict_fingerprint=True,
        )
        self.addCleanup(session.session.close)
        response = MagicMock(status_code=200, headers={})
        with patch.object(session.session, "get", return_value=response) as request:
            session.get("https://example.invalid/")

        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers["User-Agent"], browser_config.USER_AGENT)
        self.assertEqual(headers["sec-ch-ua"], browser_config.SEC_CH_UA)
        self.assertEqual(headers["sec-ch-ua-platform"], '"macOS"')
        self.assertEqual(headers["accept-language"], session.browser_profile["accept_language"])

    @patch.object(BrowserSession, "_detect_exit_geo", return_value=_EXIT_GEO)
    def test_strict_session_detects_mutated_session_proxy(self, _detect_exit_geo):
        session = BrowserSession(
            proxy="http://127.0.0.1:65535",
            require_proxy=True,
            strict_fingerprint=True,
        )
        self.addCleanup(session.session.close)
        session.session.proxies = {}

        with self.assertRaisesRegex(RuntimeError, "会话代理被修改"):
            session.get("https://example.invalid/")

    @patch("main.network_preflight", side_effect=RuntimeError("stop-after-construction"))
    @patch("main.BrowserSession")
    def test_protocol_driver_enables_both_strict_guards(self, session_cls, _network_preflight):
        fake = MagicMock()
        fake.proxy = "http://127.0.0.1:65535"
        fake.device_id = "device-id"
        fake.auth_session_logging_id = "logging-id"
        fake.fingerprint_alignment.return_value = {
            "tls": "chrome146",
            "ua_major": "146",
            "os": "macOS",
            "sec_ch_ua_platform": '"macOS"',
            "navigator_platform": "MacIntel",
            "exit_ip": "203.0.113.7",
        }
        session_cls.return_value = fake

        with patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "protocol"):
            result = main.run_registration(
                email="strict@example.com",
                name="Strict Example",
                birthday="1990-01-01",
                proxy="http://127.0.0.1:65535",
            )

        self.assertFalse(result["success"])
        self.assertIn("stop-after-construction", result["error"])
        session_cls.assert_called_once_with(
            proxy="http://127.0.0.1:65535",
            require_proxy=True,
            strict_fingerprint=True,
        )
