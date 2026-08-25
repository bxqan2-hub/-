from unittest import TestCase
from unittest.mock import MagicMock, patch

import main
from config import browser as browser_config
from core.openai_auth import CloudflareChallengeError, network_preflight
from core.session import BrowserSession, is_cloudflare_challenge_response


_EXIT_GEO = {
    "ip": "203.0.113.7",
    "country": "JP",
    "timezone": "Asia/Tokyo",
}


class ProtocolFingerprintAlignmentTests(TestCase):
    def test_cloudflare_challenge_response_is_detected_from_edge_header(self):
        response = MagicMock(
            status_code=403,
            headers={"cf-mitigated": "challenge"},
            text="<html><title>Just a moment...</title></html>",
        )
        self.assertTrue(is_cloudflare_challenge_response(response))

    def test_cloudflare_challenge_response_requires_403(self):
        response = MagicMock(
            status_code=200,
            headers={"cf-mitigated": "challenge"},
            text="<html><title>Just a moment...</title></html>",
        )
        self.assertFalse(is_cloudflare_challenge_response(response))

    def test_network_preflight_fails_fast_with_cloudflare_error(self):
        response = MagicMock(
            status_code=403,
            headers={"cf-mitigated": "challenge", "cf-ray": "abc123"},
            text="<html><title>Just a moment...</title></html>",
            url="https://chatgpt.com/login",
        )
        session = MagicMock()
        session.get.side_effect = [response]
        session.exit_geo = {"ip": "203.0.113.7"}

        with self.assertRaises(CloudflareChallengeError) as ctx:
            network_preflight(session)

        self.assertEqual(ctx.exception.error_code, "cloudflare_managed_challenge")
        self.assertIn("Cloudflare Managed Challenge", str(ctx.exception))
        self.assertIn("cf-ray=abc123", str(ctx.exception))
        self.assertIn("protocol flow stopped before OTP", str(ctx.exception))
        session.get.assert_called_once()

    def test_network_preflight_keeps_non_cloudflare_403_as_runtime_error(self):
        response = MagicMock(
            status_code=403,
            headers={},
            text="application forbidden",
            url="https://chatgpt.com/login",
        )
        session = MagicMock()
        session.get.return_value = response

        with self.assertRaisesRegex(RuntimeError, "chatgpt-login status=403"):
            network_preflight(session)

        session.get.assert_called_once()

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
    def test_strict_session_waits_for_server_issued_oai_did(self, _detect_exit_geo):
        session = BrowserSession(
            proxy="http://127.0.0.1:65535",
            require_proxy=True,
            strict_fingerprint=True,
        )
        self.addCleanup(session.session.close)

        self.assertNotIn("oai-did", {cookie.name for cookie in session.session.cookies.jar})

        server_did = "7e8bcb4d-e96b-4678-a71a-5c5a60c17f17"
        session.session.cookies.set("oai-did", server_did, domain=".chatgpt.com", path="/")
        response = MagicMock(status_code=200, headers={})
        session._observe_response_for_circuit_breaker(response, "https://chatgpt.com/login")
        self.assertEqual(session.device_id, server_did)

    @patch.object(BrowserSession, "_detect_exit_geo", return_value=_EXIT_GEO)
    def test_cf_challenge_rotates_transport_on_same_selected_proxy(self, _detect_exit_geo):
        session = BrowserSession(
            proxy="http://127.0.0.1:65535",
            require_proxy=True,
            strict_fingerprint=True,
        )
        self.addCleanup(lambda: getattr(session.session, "close", lambda: None)())
        first_transport = session.session
        challenge = MagicMock(
            status_code=403,
            headers={"cf-mitigated": "challenge"},
            text="<html>Cloudflare challenge-platform Just a moment</html>",
        )
        success = MagicMock(status_code=200, headers={}, text="ok")
        first_transport.get = MagicMock(return_value=challenge)
        second_transport = MagicMock()
        second_transport.get.return_value = success

        def install_second(_url):
            session.session = second_transport
            session.blocked_until = 0
            session.blocked_reason = ""

        with (
            patch.object(session, "_challenge_retry_policy", return_value=(2, 0)),
            patch.object(session, "_rebuild_transport_after_challenge", side_effect=install_second) as rebuild,
        ):
            result = session.get("https://chatgpt.com/login")

        self.assertIs(result, success)
        first_transport.get.assert_called_once()
        second_transport.get.assert_called_once()
        rebuild.assert_called_once_with("https://chatgpt.com/login")
        self.assertEqual(session.proxy, "http://127.0.0.1:65535")

    @patch.object(BrowserSession, "_detect_exit_geo", return_value=_EXIT_GEO)
    def test_transport_rebuild_keeps_flow_cookies_and_drops_cf_cookies(self, _detect_exit_geo):
        session = BrowserSession(
            proxy="http://127.0.0.1:65535",
            require_proxy=True,
            strict_fingerprint=True,
        )
        self.addCleanup(session.session.close)
        session.session.cookies.set("flow", "keep", domain="chatgpt.com", path="/")
        session.session.cookies.set("__cf_bm", "drop", domain=".chatgpt.com", path="/")

        session._rebuild_transport_after_challenge("https://chatgpt.com/login")

        cookies = {cookie.name: cookie.value for cookie in session.session.cookies.jar}
        self.assertEqual(cookies.get("flow"), "keep")
        self.assertNotIn("__cf_bm", cookies)
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

    @patch("core.abai_protocol_registration.run_abai_protocol_registration", return_value={"success": True})
    def test_protocol_driver_calls_vendored_abai_flow(self, run_abai):
        with patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "protocol"), \
             patch.object(main._twofa_cfg, "ENABLE_2FA", False):
            result = main.run_registration(
                email="strict@example.com",
                name="Strict Example",
                birthday="1990-01-01",
                proxy="http://127.0.0.1:65535",
            )

        self.assertTrue(result["success"])
        run_abai.assert_called_once_with(
            email="strict@example.com",
            name="Strict Example",
            birthday="1990-01-01",
            proxy="http://127.0.0.1:65535",
            otp_code=None,
            batch_dir=None,
        )
