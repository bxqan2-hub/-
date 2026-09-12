# -*- coding: utf-8 -*-
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.browser_traffic import (
    CACHE_SCHEMA_VERSION,
    RoxyTrafficOptimizer,
    StaticResourceCache,
    block_reason,
    is_cacheable_response,
    is_cacheable_request,
    summarize_performance_logs,
)


ASSET_URL = "https://cdn.openai.com/assets/app.0123456789abcdef.js"
CSS_URL = "https://cdn.openai.com/assets/site-0123456789abcdef.css"
APP_ASSET_URL = "https://chatgpt.com/cdn/assets/conversation-small-h1dtzoris1y9588z.js"
APP_CSS_URL = "https://chatgpt.com/cdn/assets/root-exiuv0yn.css"


def _public_headers(mime="application/javascript", cache_control="public, max-age=3600"):
    return [
        {"name": "Content-Type", "value": mime},
        {"name": "Cache-Control", "value": cache_control},
    ]


def _optimizer(cache_dir=Path("unused-test-cache"), *, low_traffic=False, static_cache=True):
    optimizer = RoxyTrafficOptimizer(
        MagicMock(), low_traffic=low_traffic, static_cache=static_cache, capture=False,
        cache_dir=Path(cache_dir), cache_max_age=3600, cache_max_item_bytes=1024,
        cache_refresh_rate=0, cache_refresh_budget_bytes=0,
        cache_refresh_max_item_bytes=0, budget_bytes=1024,
    )
    optimizer._devtools = MagicMock()
    optimizer._connection = MagicMock()
    optimizer._fetch_enabled = True
    optimizer._devtools.fetch.HeaderEntry.side_effect = lambda **kwargs: kwargs
    optimizer.driver.start_devtools.return_value = (optimizer._devtools, optimizer._connection)
    return optimizer


def _event(url=ASSET_URL, *, request_id="fixture-request", headers=None, resource="script", response_headers=None):
    return SimpleNamespace(
        request_id=request_id, request=SimpleNamespace(url=url, method="GET", headers=headers or {}),
        resource_type=resource, response_status_code=200 if response_headers is not None else None,
        response_status_text="OK", response_headers=response_headers, network_id=request_id,
    )


class BrowserTrafficClassifierTests(unittest.TestCase):
    def test_network_enable_uses_bounded_per_profile_buffers(self):
        optimizer = _optimizer(low_traffic=True, static_cache=False)
        optimizer.install()
        enable_calls = [item for item in optimizer.driver.execute_cdp_cmd.call_args_list if item.args[0] == "Network.enable"]
        self.assertEqual(len(enable_calls), 1)
        self.assertEqual(enable_calls[0].args[1], {
            "maxTotalBufferSize": 2 * 1024 * 1024,
            "maxResourceBufferSize": 512 * 1024,
            "maxPostDataSize": 4 * 1024,
        })

    def test_network_enable_falls_back_for_legacy_cdp(self):
        optimizer = _optimizer(low_traffic=True, static_cache=False)
        def execute(command, params):
            if command == "Network.enable" and params:
                raise RuntimeError("unknown parameter")
        optimizer.driver.execute_cdp_cmd.side_effect = execute
        optimizer.install()
        enable_calls = [item for item in optimizer.driver.execute_cdp_cmd.call_args_list if item.args[0] == "Network.enable"]
        self.assertEqual([item.args[1] for item in enable_calls], [
            {"maxTotalBufferSize": 2 * 1024 * 1024, "maxResourceBufferSize": 512 * 1024, "maxPostDataSize": 4 * 1024},
            {},
        ])

    def test_recovery_disables_fetch_and_clears_blocked_urls(self):
        optimizer = _optimizer(low_traffic=True)
        optimizer._fetch_enabled = True
        optimizer.disable_for_recovery("auth_error")
        self.assertFalse(optimizer.low_traffic)
        self.assertFalse(optimizer.static_cache_enabled)
        self.assertFalse(optimizer._fetch_enabled)
        self.assertEqual(optimizer._degraded_reason, "auth_error")
        optimizer.driver.execute_cdp_cmd.assert_called_once_with("Network.setBlockedURLs", {"urls": []})
        optimizer._connection.execute.assert_called_once_with(optimizer._devtools.fetch.disable())

    def test_only_exact_public_cdn_optional_media_is_blocked(self):
        for extension in ("mp3", "mp4", "ogg", "webm"):
            with self.subTest(extension=extension):
                self.assertEqual(block_reason(f"https://cdn.openai.com/assets/demo.{extension}", "media"), "optional_media")
        allowed = [
            ("https://cdn.openai.com/assets/demo.mp4?identity=fixture", "media"),
            ("https://cdn.openai.com/assets/demo.mp4?", "media"),
            ("https://cdn.openai.com/assets/demo.mp4#fixture", "media"),
            ("https://cdn.openai.com/assets/config.js?x=.mp4", "media"),
            ("https://cdn.openai.com/assets/sentinel/demo.mp4", "media"),
            ("https://cdn.openai.com/assets/cdn-cgi/challenge-platform/demo.mp4", "media"),
            ("https://cdn.openai.com/assets/../account/demo.mp4", "media"),
            ("https://cdn.openai.com/assets/%64emo.mp4", "media"),
            ("https://cdn.openai.com/assets/demo.mp4", "script"),
            ("https://cdn.openai.com/assets/demo.mp4", "image"),
            ("https://cdn.openai.com/assets/font.woff2", "font"),
            ("https://cdn.openai.com/assets/icon.png", "image"),
            ("https://cdn.openai.com/other/demo.mp4", "media"),
            ("https://sub.cdn.openai.com/assets/demo.mp4", "media"),
            ("https://cdn.openai.com:8443/assets/demo.mp4", "media"),
            ("http://cdn.openai.com/assets/demo.mp4", "media"),
            ("https://auth.openai.com/assets/demo.mp4", "media"),
            ("https://chatgpt.com/assets/demo.mp4", "media"),
        ]
        for url, resource in allowed:
            with self.subTest(url=url, resource=resource):
                self.assertEqual(block_reason(url, resource), "")

    def test_identity_challenge_configuration_and_telemetry_stay_live(self):
        for url, resource in [
            ("https://challenges.cloudflare.com/widget.js", "script"),
            ("https://sentinel.openai.com/a.png", "image"),
            ("https://cdn.openai.com/assets/recaptcha/audio.mp3", "media"),
            ("https://cdn.openai.com/assets/hcaptcha/audio.mp3", "media"),
            ("https://cdn.openai.com/assets/challenge/audio.mp3", "media"),
            ("https://statsigapi.net/v1/config", "xhr"),
            ("https://featuregates.org/v1/initialize", "fetch"),
            ("https://browser-intake-datadoghq.com/v1/log", "xhr"),
            ("https://accounts.google.com/o/oauth2", "document"),
            ("https://auth.openai.com/awe/api/v2/rum", "xhr"),
            ("https://chatgpt.com/manifest.json", "manifest"),
        ]:
            with self.subTest(url=url):
                self.assertEqual(block_reason(url, resource), "")

    def test_session_only_keeps_application_shell_and_security_requests_live(self):
        for path, resource in [
            ("/api/auth/session", "xhr"), ("/api/auth/callback/openai", "document"),
            ("/_next/static/app.js", "script"), ("/cdn/assets/site.css", "stylesheet"),
            ("/backend-api/accounts/mfa/enroll", "fetch"), ("/", "document"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(block_reason("https://chatgpt.com" + path, resource, session_only=True), "")

    def test_install_uses_exact_fetch_asset_and_media_filters_and_clears_legacy_globs(self):
        for static_cache, low_traffic in ((True, False), (False, True), (True, True)):
            with self.subTest(static_cache=static_cache, low_traffic=low_traffic):
                optimizer = _optimizer(static_cache=static_cache, low_traffic=low_traffic)
                optimizer.install()
                optimizer.driver.execute_cdp_cmd.assert_any_call("Network.setBlockedURLs", {"urls": []})
                expected = []
                if static_cache:
                    for url_pattern in (
                        "https://cdn.openai.com/assets/*", "https://cdn.openai.com/cdn/assets/*",
                        "https://cdn.openai.com/_next/static/*", "https://cdn.openai.com/unauth-mweb/assets/*",
                        "https://chatgpt.com/cdn/assets/*",
                    ):
                        for resource in ("SCRIPT", "STYLESHEET"):
                            expected.append((url_pattern, getattr(optimizer._devtools.network.ResourceType, resource)))
                if low_traffic:
                    expected.append(("https://cdn.openai.com/assets/*", optimizer._devtools.network.ResourceType.MEDIA))
                patterns = optimizer._devtools.fetch.RequestPattern.call_args_list
                self.assertCountEqual(
                    [(pattern.kwargs["url_pattern"], pattern.kwargs["resource_type"]) for pattern in patterns],
                    expected,
                )
                for pattern in patterns:
                    self.assertEqual(pattern.kwargs["request_stage"], optimizer._devtools.fetch.RequestStage.REQUEST)
                self.assertTrue(optimizer._fetch_enabled)

    def test_optional_media_handler_blocks_only_exact_media_request(self):
        optimizer = _optimizer(low_traffic=True, static_cache=False)
        optimizer._on_request_paused(_event("https://cdn.openai.com/assets/demo.mp4", resource="media"))
        optimizer._devtools.fetch.fail_request.assert_called_once_with(
            "fixture-request", optimizer._devtools.network.ErrorReason.BLOCKED_BY_CLIENT,
        )
        optimizer._devtools.fetch.continue_request.assert_not_called()
        optimizer._devtools.fetch.fail_request.reset_mock()
        optimizer._on_request_paused(_event("https://cdn.openai.com/assets/config.js?x=.mp4", resource="media"))
        optimizer._devtools.fetch.fail_request.assert_not_called()
        optimizer._devtools.fetch.continue_request.assert_called_once_with("fixture-request")

    def test_cache_scope_is_exact_versioned_public_asset_paths(self):
        for url, resource in [
            (ASSET_URL, "script"), (CSS_URL, "stylesheet"),
            (APP_ASSET_URL, "script"), (APP_CSS_URL, "stylesheet"),
            ("https://cdn.openai.com:443/_next/static/app.01234567.js", "script"),
            (APP_ASSET_URL.replace("chatgpt.com", "chatgpt.com:443"), "script"),
            ("https://chatgpt.com/cdn/assets/a5f68020-e8hhtychj8o2dcvt.js", "script"),
            ("https://chatgpt.com/cdn/assets/app.01234567.js", "script"),
            ("https://chatgpt.com/cdn/assets/settings-h1dtzoris1y9588z.js", "script"),
            ("https://chatgpt.com/cdn/assets/app." + "a" * 64 + ".js", "script"),
        ]:
            with self.subTest(url=url, resource=resource):
                self.assertTrue(is_cacheable_request(url, "GET", resource))
        rejected = [
            ASSET_URL.replace("cdn.openai.com", "chatgpt.com"),
            ASSET_URL.replace("cdn.openai.com", "auth.openai.com"),
            ASSET_URL.replace("cdn.openai.com", "auth-cdn.oaistatic.com"),
            ASSET_URL.replace("cdn.openai.com", "sub.cdn.openai.com"),
            ASSET_URL.replace("cdn.openai.com", "cdn.openai.com:8443"),
            ASSET_URL.replace("https://", "http://"),
            ASSET_URL.replace("https://", "https://fixture:fixture@"),
            ASSET_URL + "?identity=fixture", ASSET_URL + "?", ASSET_URL + "#fixture", ASSET_URL + "#",
            ASSET_URL.replace("/assets/", "/api/"),
            ASSET_URL.replace("/assets/", "/assets/../api/"),
            ASSET_URL.replace("/assets/", "/assets/%2e%2e/api/"),
            ASSET_URL.replace("/assets/", "/assets/sentinel/"),
            ASSET_URL.replace("/assets/", "/assets/recaptcha/"),
            ASSET_URL.replace("/assets/", "/assets/hcaptcha/"),
            ASSET_URL.replace("app.0123456789abcdef.js", "app.js"),
            ASSET_URL.replace("app.0123456789abcdef.js", "app.0123456.js"),
            ASSET_URL.replace("app.0123456789abcdef.js", "app0123456789abcdef.js"),
            ASSET_URL.replace("app.0123456789abcdef.js", "app.0123456789abcdef.runtime.js"),
            ASSET_URL.replace("app.0123456789abcdef.js", "service-worker.0123456789abcdef.js"),
            APP_ASSET_URL.replace("chatgpt.com", "www.chatgpt.com"),
            APP_ASSET_URL.replace("chatgpt.com", "sub.chatgpt.com"),
            APP_ASSET_URL.replace("chatgpt.com", "chatgpt.com:8443"),
            APP_ASSET_URL.replace("/cdn/assets/", "/assets/"),
            APP_ASSET_URL.replace("/cdn/assets/", "/_next/static/"),
            APP_ASSET_URL.replace("/cdn/assets/", "/api/auth/"),
            APP_ASSET_URL.replace("/cdn/assets/", "/backend-api/"),
            APP_ASSET_URL.replace("/cdn/assets/", "/cdn/assets/../api/"),
            APP_ASSET_URL.replace("/cdn/assets/", "/cdn/assets/%2e%2e/api/"),
            APP_ASSET_URL.replace("/cdn/assets/", "/cdn/assets/challenge/"),
            APP_ASSET_URL + "?identity=fixture", APP_ASSET_URL + "?", APP_ASSET_URL + "#fixture", APP_ASSET_URL + "#",
            APP_ASSET_URL.replace("h1dtzoris1y9588z", "h1dtzoris1y9588"),
            APP_ASSET_URL.replace("h1dtzoris1y9588z", "H1DTZORIS1Y9588Z"),
            APP_ASSET_URL.replace("h1dtzoris1y9588z", "a" * 65),
            APP_ASSET_URL.replace("conversation-small-h1dtzoris1y9588z.js", "settings.js"),
            APP_ASSET_URL.replace("conversation-small-h1dtzoris1y9588z.js", "features.js"),
            APP_ASSET_URL.replace("conversation-small-h1dtzoris1y9588z.js", "manifest.js"),
            APP_ASSET_URL.replace("conversation-small-h1dtzoris1y9588z.js", "sentinel.js"),
            APP_ASSET_URL.replace("conversation-small-h1dtzoris1y9588z.js", "h1dtzoris1y9588z.js"),
            APP_ASSET_URL.replace("h1dtzoris1y9588z", "settings"),
            APP_ASSET_URL.replace("conversation-small", "sentinel"),
            APP_ASSET_URL.replace("conversation-small", "captcha"),
            APP_ASSET_URL.replace("conversation-small", "challenge"),
        ]
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(is_cacheable_request(url, "GET", "script"))
        for url, resource in ((ASSET_URL, "script"), (APP_ASSET_URL, "script"), (APP_CSS_URL, "stylesheet")):
            with self.subTest(url=url):
                self.assertFalse(is_cacheable_request(url, "POST", resource))
                self.assertFalse(is_cacheable_request(url, "GET", "fetch"))
        self.assertFalse(is_cacheable_request(ASSET_URL, "GET", "stylesheet"))
        self.assertFalse(is_cacheable_request(CSS_URL, "GET", "script"))
        self.assertFalse(is_cacheable_request(APP_ASSET_URL, "GET", "stylesheet"))
        self.assertFalse(is_cacheable_request(APP_CSS_URL, "GET", "script"))

    def test_only_cookie_is_permitted_on_already_qualified_public_assets(self):
        for url in (ASSET_URL, APP_ASSET_URL):
            self.assertTrue(is_cacheable_request(url, "GET", "script", {"Cookie": "sid=fixture-A"}))
            for name, value in [
                ("Authorization", "Bearer fixture"), ("Proxy-Authorization", "Basic fixture"),
                ("Range", "bytes=0-1"), ("If-None-Match", '"fixture"'), ("If-Modified-Since", "fixture"),
                ("If-Range", '"fixture"'), ("X-Device-ID", "fixture-A"),
                ("X-Session-ID", "fixture-A"), ("X-OpenAI-Token", "fixture-A"),
                ("Cache-Control", "no-cache"), ("Cache-Control", "no-store"),
                ("Cache-Control", "max-age=0"), ("Cache-Control", 'max-age = "0"'),
                ("Pragma", "no-cache"),
            ]:
                with self.subTest(url=url, header=name, value=value):
                    self.assertFalse(is_cacheable_request(url, "GET", "script", {"Cookie": "sid=fixture-A", name: value}))

    def test_public_fresh_mime_and_nonvarying_response_required(self):
        self.assertTrue(is_cacheable_response(_public_headers(), resource_type="script"))
        self.assertTrue(is_cacheable_response(_public_headers(cache_control="public, max-age=2592000"), resource_type="script"))
        self.assertTrue(is_cacheable_response(_public_headers(cache_control="public, immutable, max-age=3600"), resource_type="script"))
        self.assertTrue(is_cacheable_response(_public_headers("text/css; charset=utf-8"), resource_type="stylesheet"))
        self.assertTrue(is_cacheable_response(_public_headers() + [{"name": "Vary", "value": "Accept-Encoding"}]))
        for control in [
            "max-age=3600", "immutable, max-age=3600", "public, immutable", "public",
            "public, immutable, max-age=0", "public, immutable, max-age=-1",
            "public, immutable, max-age=fixture", "public, immutable, max-age=3600, s-maxage=0",
            "public, immutable, max-age=3600, private", "public, immutable, max-age=3600, no-store",
            "public, immutable, max-age=3600, no-cache", "public, immutable, max-age=3600, must-revalidate",
            "public, immutable, max-age=3600, max-age=7200",
        ]:
            with self.subTest(control=control):
                self.assertFalse(is_cacheable_response(_public_headers(cache_control=control)))
        for mime in ("text/html", "application/json", "application/octet-stream", ""):
            with self.subTest(mime=mime):
                self.assertFalse(is_cacheable_response(_public_headers(mime)))
        self.assertFalse(is_cacheable_response(_public_headers("text/css"), resource_type="script"))
        self.assertFalse(is_cacheable_response(_public_headers(), resource_type="stylesheet"))
        self.assertFalse(is_cacheable_response(_public_headers(), resource_type="fetch"))
        self.assertFalse(is_cacheable_response([]))

    def test_stateful_negotiation_and_nonencoding_variants_stay_live(self):
        for name in (
            "Set-Cookie", "WWW-Authenticate", "Authentication-Info", "Proxy-Authenticate",
            "Proxy-Authentication-Info", "Clear-Site-Data", "Accept-CH", "Critical-CH", "Origin-Trial",
            "Content-Security-Policy", "Content-Security-Policy-Report-Only",
        ):
            with self.subTest(header=name):
                self.assertFalse(is_cacheable_response(_public_headers() + [{"name": name, "value": "fixture"}]))
        for vary in ("Cookie", "Authorization", "User-Agent", "Accept-Language", "Sec-CH-UA-Platform", "Origin", "*", "Accept-Encoding, Cookie"):
            with self.subTest(vary=vary):
                self.assertFalse(is_cacheable_response(_public_headers() + [{"name": "Vary", "value": vary}]))
        self.assertFalse(is_cacheable_response(_public_headers() + [
            {"name": "Vary", "value": "Accept-Encoding"}, {"name": "Vary", "value": "Origin"},
        ]))
        self.assertFalse(is_cacheable_response(_public_headers() + [{"name": "Access-Control-Allow-Origin", "value": "https://fixture.example"}]))
        self.assertFalse(is_cacheable_response(_public_headers() + [{"name": "Access-Control-Allow-Credentials", "value": "true"}]))
        self.assertTrue(is_cacheable_response(_public_headers() + [{"name": "Access-Control-Allow-Origin", "value": "*"}]))

    def test_origin_age_date_and_shared_ttl_precedence(self):
        headers = _public_headers(cache_control="public, immutable, max-age=3600, s-maxage=10")
        self.assertTrue(is_cacheable_response(headers + [{"name": "Age", "value": "9"}]))
        self.assertFalse(is_cacheable_response(headers + [{"name": "Age", "value": "10"}]))
        self.assertFalse(is_cacheable_response(headers + [{"name": "Age", "value": "fixture"}]))
        self.assertFalse(is_cacheable_response(headers + [{"name": "Age", "value": "-1"}]))
        with patch("core.browser_traffic.time.time", return_value=20):
            self.assertFalse(is_cacheable_response(headers + [{"name": "Date", "value": "Thu, 01 Jan 1970 00:00:00 GMT"}]))


class StaticCacheTests(unittest.TestCase):
    def test_dynamic_cookie_or_authorization_requests_never_read_or_join_shared_cache(self):
        for url, headers in [
            ("https://chatgpt.com/api/auth/session", {"Cookie": "sid=fixture-A"}),
            ("https://chatgpt.com/backend-api/accounts/check", {"Cookie": "sid=fixture-B"}),
            (APP_ASSET_URL, {"Cookie": "sid=fixture-A", "Authorization": "Bearer fixture"}),
            (APP_ASSET_URL + "?session=fixture", {"Cookie": "sid=fixture-B"}),
        ]:
            with self.subTest(url=url, headers=headers):
                optimizer = _optimizer()
                optimizer.cache = MagicMock()
                optimizer._on_request_paused(_event(url, headers=headers))
                optimizer.cache.read.assert_not_called()
                optimizer.cache.claim_load.assert_not_called()
                optimizer.cache.wait_for_load.assert_not_called()
                optimizer._devtools.fetch.fulfill_request.assert_not_called()
                optimizer._devtools.fetch.continue_request.assert_called_once_with("fixture-request")

    def test_application_nonasset_paths_never_replay_even_when_cdp_omits_cookie(self):
        optimizer = _optimizer()
        optimizer.cache = MagicMock()
        optimizer._on_request_paused(_event(ASSET_URL.replace("cdn.openai.com", "chatgpt.com")))
        optimizer.cache.read.assert_not_called()
        optimizer.cache.claim_load.assert_not_called()
        optimizer._devtools.fetch.continue_request.assert_called_once_with("fixture-request")

    def test_each_strict_public_miss_continues_to_own_live_network(self):
        optimizer = _optimizer()
        optimizer.cache.read = MagicMock(return_value=None)
        for request_id in ("profile-a", "profile-b"):
            optimizer._on_request_paused(_event(request_id=request_id))
            optimizer._release_loading_request(request_id)
        self.assertEqual(optimizer._stats["cache_candidates"], 2)
        self.assertEqual(optimizer._devtools.fetch.continue_request.call_count, 2)
        for call in optimizer._devtools.fetch.continue_request.call_args_list:
            self.assertTrue(call.kwargs["intercept_response"])

    def test_cache_instances_share_only_fresh_public_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            second = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            self.assertTrue(first.write(ASSET_URL, status=200, phrase="OK", headers=_public_headers(), body=b"public-fixture"))
            self.assertEqual(second.read(ASSET_URL)["body"], b"public-fixture")

    def test_distinct_profile_cookies_share_only_validated_public_versioned_bytes(self):
        for url, resource, mime, body in (
            (APP_ASSET_URL, "script", "application/javascript", b"console.log('public bundle');"),
            (APP_CSS_URL, "stylesheet", "text/css; charset=utf-8", b".public-shell{display:block}"),
        ):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as tmp:
                first = _optimizer(tmp)
                second = _optimizer(tmp)
                first_cookie = {"Cookie": "sid=profile-A; __cf_bm=fixture-A"}
                second_cookie = {"Cookie": "sid=profile-B; __cf_bm=fixture-B"}
                headers = _public_headers(mime, "public, max-age=2592000")
                first._on_request_paused(_event(url, request_id="profile-A", headers=first_cookie, resource=resource))
                first._devtools.fetch.continue_request.assert_called_once_with("profile-A", intercept_response=True)
                self.assertEqual(first._loading_requests, {"profile-A": url})
                first._connection.execute.return_value = (body.decode("utf-8"), False)
                first._on_request_paused(_event(url, request_id="profile-A", headers=first_cookie, resource=resource, response_headers=headers))
                self.assertEqual(first._stats["cache_writes"], 1)
                self.assertEqual(first._loading_requests, {})
                second._on_request_paused(_event(url, request_id="profile-B", headers=second_cookie, resource=resource))
                self.assertEqual(second._stats["cache_hits"], 1)
                self.assertEqual(second._stats["cache_misses"], 0)
                second._devtools.fetch.continue_request.assert_not_called()
                replay = second._devtools.fetch.fulfill_request.call_args.kwargs
                self.assertEqual(base64.b64decode(replay["body"]), body)
                replay_headers = {item["name"].lower(): item["value"] for item in replay["response_headers"]}
                self.assertEqual(replay_headers, {"content-type": mime, "cache-control": "no-store"})
                metadata = next(Path(tmp).glob("*.json")).read_text(encoding="utf-8")
                for identity in ("profile-A", "profile-B", "fixture-A", "fixture-B", "__cf_bm"):
                    self.assertNotIn(identity, metadata)

    def test_cache_miss_waiter_receives_only_validated_public_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            second = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            self.assertTrue(first.claim_load(ASSET_URL))
            self.assertFalse(second.claim_load(ASSET_URL))
            self.assertTrue(first.write(ASSET_URL, status=200, phrase="OK", headers=_public_headers(), body=b"single-flight"))
            self.assertEqual(second.wait_for_load(ASSET_URL, timeout=0.1)["body"], b"single-flight")
            first.release_load(ASSET_URL)

    def test_private_response_is_not_published_to_waiter(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            second = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            self.assertTrue(first.claim_load(ASSET_URL))
            headers = _public_headers() + [{"name": "Set-Cookie", "value": "sid=fixture"}]
            self.assertFalse(first.write(ASSET_URL, status=200, phrase="OK", headers=headers, body=b"private-fixture"))
            first.release_load(ASSET_URL)
            self.assertIsNone(second.wait_for_load(ASSET_URL, timeout=0.1))
            self.assertFalse(list(Path(tmp).glob("*.bin")))

    def test_write_read_strips_unknown_and_origin_specific_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            extra = [
                {"name": name, "value": "fixture"}
                for name in ("Content-Encoding", "Content-Length", "CF-Ray", "ETag", "Report-To", "Traceparent", "X-Envoy-Upstream-Service-Time", "X-Profile-ID", "X-Unknown-State")
            ]
            self.assertTrue(cache.write(ASSET_URL, status=200, phrase="OK", headers=_public_headers() + extra, body=b"abc"))
            item = cache.read(ASSET_URL)
            self.assertEqual(item["body"], b"abc")
            self.assertEqual({header["name"].lower() for header in item["headers"]}, {"content-type", "cache-control"})
            self.assertFalse(list(Path(tmp).glob("*.tmp")))

    def test_replay_uses_no_store_and_keeps_no_profile_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            optimizer = _optimizer(tmp)
            self.assertTrue(optimizer.cache.write(ASSET_URL, status=200, phrase="OK", headers=_public_headers(), body=b"public-fixture"))
            optimizer._on_request_paused(_event())
            self.assertEqual(optimizer._stats["cache_hits"], 1)
            replay = optimizer._devtools.fetch.fulfill_request.call_args.kwargs
            headers = {item["name"].lower(): item["value"] for item in replay["response_headers"]}
            self.assertEqual(headers, {"content-type": "application/javascript", "cache-control": "no-store"})
            optimizer._devtools.fetch.continue_request.assert_not_called()

    def test_origin_freshness_and_configured_max_age_are_both_upper_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            headers = _public_headers(cache_control="public, immutable, max-age=10") + [{"name": "Age", "value": "7"}]
            with patch("core.browser_traffic.time.time", return_value=1000):
                self.assertTrue(cache.write(ASSET_URL, status=200, phrase="OK", headers=headers, body=b"abc"))
            with patch("core.browser_traffic.time.time", return_value=1002):
                self.assertIsNotNone(cache.read(ASSET_URL))
            with patch("core.browser_traffic.time.time", return_value=1003):
                self.assertIsNone(cache.read(ASSET_URL))
            cache.max_age = 2
            with patch("core.browser_traffic.time.time", return_value=2000):
                self.assertTrue(cache.write(ASSET_URL, status=200, phrase="OK", headers=_public_headers(), body=b"abc"))
            with patch("core.browser_traffic.time.time", return_value=2002):
                self.assertIsNone(cache.read(ASSET_URL))

    def test_old_schema_and_missing_expiry_are_never_replayed(self):
        self.assertEqual(CACHE_SCHEMA_VERSION, 3)
        with tempfile.TemporaryDirectory() as tmp:
            cache = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            with patch("core.browser_traffic.time.time", return_value=1000):
                self.assertTrue(cache.write(ASSET_URL, status=200, phrase="OK", headers=_public_headers(), body=b"abc"))
                meta_path, _ = cache._paths(ASSET_URL)
                baseline = json.loads(meta_path.read_text(encoding="utf-8"))
                for schema in (None, 1, 2):
                    with self.subTest(schema=schema):
                        meta = dict(baseline)
                        meta["schema_version"] = schema
                        meta_path.write_text(json.dumps(meta), encoding="utf-8")
                        self.assertIsNone(cache.read(ASSET_URL))
                baseline.pop("expires_at")
                meta_path.write_text(json.dumps(baseline), encoding="utf-8")
                self.assertIsNone(cache.read(ASSET_URL))

    def test_cached_metadata_rechecks_url_mime_response_gate_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            with patch("core.browser_traffic.time.time", return_value=1000):
                self.assertTrue(cache.write(ASSET_URL, status=200, phrase="OK", headers=_public_headers(), body=b"abc"))
                meta_path, _ = cache._paths(ASSET_URL)
                baseline = json.loads(meta_path.read_text(encoding="utf-8"))
                changes = [
                    {"status": 404}, {"saved_at": 1001}, {"expires_at": 1e20}, {"expires_at": float("nan")},
                    {"headers": _public_headers("text/html")},
                    {"headers": _public_headers("text/css")},
                    {"headers": _public_headers(cache_control="max-age=3600")},
                    {"headers": _public_headers() + [{"name": "Vary", "value": "Cookie"}]},
                    {"headers": _public_headers() + [{"name": "Set-Cookie", "value": "sid=fixture"}]},
                ]
                for change in changes:
                    with self.subTest(change=change):
                        meta_path.write_text(json.dumps(dict(baseline, **change)), encoding="utf-8")
                        self.assertIsNone(cache.read(ASSET_URL))
                unsafe_url = ASSET_URL.replace("cdn.openai.com", "chatgpt.com")
                unsafe_meta, unsafe_body = cache._paths(unsafe_url)
                unsafe_meta.write_text(json.dumps(dict(baseline, url=unsafe_url)), encoding="utf-8")
                unsafe_body.write_bytes(b"abc")
                self.assertIsNone(cache.read(unsafe_url))

    def test_write_rechecks_asset_url_and_matching_mime(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            for url, headers in [
                (ASSET_URL.replace("cdn.openai.com", "chatgpt.com"), _public_headers()),
                (ASSET_URL + "?identity=fixture", _public_headers()),
                (ASSET_URL, _public_headers("text/html")),
                (CSS_URL, _public_headers()),
                (ASSET_URL, _public_headers("text/css")),
            ]:
                with self.subTest(url=url, headers=headers):
                    self.assertFalse(cache.write(url, status=200, phrase="OK", headers=headers, body=b"fixture"))
            self.assertFalse(list(Path(tmp).glob("*")))

    def test_corrupt_digest_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            self.assertTrue(cache.write(ASSET_URL, status=200, phrase="OK", headers=_public_headers(), body=b"abc"))
            body_path = next(Path(tmp).glob("*.bin"))
            body_path.write_bytes(b"tampered")
            self.assertIsNone(cache.read(ASSET_URL))

    def test_response_with_wrong_mime_or_profile_headers_is_not_read(self):
        for headers in (_public_headers("text/css"), _public_headers("text/html"), _public_headers() + [{"name": "Set-Cookie", "value": "sid=fixture"}]):
            with self.subTest(headers=headers):
                optimizer = _optimizer()
                optimizer.cache = MagicMock()
                optimizer._on_request_paused(_event(response_headers=headers))
                optimizer._devtools.fetch.get_response_body.assert_not_called()
                optimizer.cache.write.assert_not_called()
                optimizer._devtools.fetch.continue_response.assert_called_once_with("fixture-request")

    def test_cookie_setting_public_response_stays_in_its_profile(self):
        for cookie in ("sid=fixture-A", "__cf_bm=fixture-A; Path=/; Secure; HttpOnly"):
            with self.subTest(cookie=cookie), tempfile.TemporaryDirectory() as tmp:
                optimizer = _optimizer(tmp)
                optimizer._on_request_paused(_event(APP_ASSET_URL))
                self.assertEqual(optimizer._loading_requests, {"fixture-request": APP_ASSET_URL})
                optimizer._on_request_paused(_event(
                    APP_ASSET_URL,
                    response_headers=_public_headers(cache_control="public, max-age=2592000") + [
                        {"name": "Set-Cookie", "value": cookie},
                    ],
                ))
                optimizer._devtools.fetch.get_response_body.assert_not_called()
                optimizer._devtools.fetch.fulfill_request.assert_not_called()
                optimizer._devtools.fetch.continue_response.assert_called_once_with("fixture-request")
                self.assertEqual(optimizer._stats["cache_writes"], 0)
                self.assertEqual(optimizer._loading_requests, {})
                self.assertIsNone(optimizer.cache.read(APP_ASSET_URL))
                self.assertFalse(list(Path(tmp).glob("*.bin")))

    def test_session_only_keeps_public_cache_reads_writes_and_pending_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            optimizer = _optimizer(tmp)
            optimizer._on_request_paused(_event(APP_ASSET_URL, headers={"Cookie": "sid=fixture-A"}))
            self.assertEqual(optimizer._loading_requests, {"fixture-request": APP_ASSET_URL})
            optimizer.set_session_only(True)
            self.assertTrue(optimizer._session_only)
            self.assertEqual(optimizer._loading_requests, {"fixture-request": APP_ASSET_URL})
            self.assertFalse(optimizer.cache.claim_load(APP_ASSET_URL))
            optimizer.driver.execute_cdp_cmd.assert_not_called()
            optimizer._connection.execute.return_value = ("public-fixture", False)
            optimizer._on_request_paused(_event(APP_ASSET_URL, headers={"Cookie": "sid=fixture-A"}, response_headers=_public_headers()))
            self.assertEqual(optimizer._stats["cache_writes"], 1)
            self.assertEqual(optimizer._loading_requests, {})
            self.assertEqual(optimizer.cache.read(APP_ASSET_URL)["body"], b"public-fixture")
            optimizer._on_request_paused(_event(APP_ASSET_URL, request_id="session-asset", headers={"Cookie": "sid=fixture-A"}))
            self.assertEqual(optimizer._stats["cache_hits"], 1)
            optimizer._devtools.fetch.continue_request.reset_mock()
            optimizer._devtools.fetch.fulfill_request.reset_mock()
            optimizer._on_request_paused(_event("https://chatgpt.com/api/auth/session", request_id="session-api", headers={"Cookie": "sid=fixture-A"}, resource="fetch"))
            optimizer._devtools.fetch.fulfill_request.assert_not_called()
            optimizer._devtools.fetch.continue_request.assert_called_once_with("session-api")

    def test_session_only_transition_during_wait_preserves_public_asset_replay(self):
        optimizer = _optimizer()
        optimizer.cache = MagicMock()
        optimizer.cache.read.return_value = None
        optimizer.cache.claim_load.return_value = False
        def wait(*args, **kwargs):
            optimizer.set_session_only(True)
            return {"body": b"public-fixture", "headers": _public_headers(), "status": 200, "phrase": "OK", "expires_at": 1e20}
        optimizer.cache.wait_for_load.side_effect = wait
        optimizer._on_request_paused(_event(APP_ASSET_URL, headers={"Cookie": "sid=fixture-A"}))
        optimizer._devtools.fetch.fulfill_request.assert_called_once()
        optimizer._devtools.fetch.continue_request.assert_not_called()
        self.assertEqual(optimizer._stats["cache_hits"], 1)

    def test_session_only_transition_during_body_read_preserves_public_asset_write(self):
        optimizer = _optimizer()
        optimizer.cache = MagicMock()
        marker = object()
        optimizer._devtools.fetch.get_response_body.return_value = marker
        def execute(command):
            if command is marker:
                optimizer.set_session_only(True)
                return "public-fixture", False
        optimizer._connection.execute.side_effect = execute
        optimizer._on_request_paused(_event(APP_ASSET_URL, headers={"Cookie": "sid=fixture-A"}, response_headers=_public_headers()))
        optimizer.cache.write.assert_called_once_with(APP_ASSET_URL, status=200, phrase="OK", headers=_public_headers(), body=b"public-fixture")
        optimizer._devtools.fetch.continue_response.assert_called_once_with("fixture-request")

    def test_entry_expiring_during_wait_continues_live(self):
        optimizer = _optimizer()
        optimizer.cache = MagicMock()
        optimizer.cache.read.return_value = None
        optimizer.cache.claim_load.return_value = False
        optimizer.cache.wait_for_load.return_value = {"body": b"public-fixture", "headers": _public_headers(), "status": 200, "phrase": "OK", "expires_at": 1}
        optimizer._on_request_paused(_event())
        optimizer._devtools.fetch.fulfill_request.assert_not_called()
        optimizer._devtools.fetch.continue_request.assert_called_once_with("fixture-request")
        self.assertEqual(optimizer._stats["cache_hits"], 0)


    def test_network_error_response_is_not_replaced_with_other_profile_cache(self):
        optimizer = _optimizer()
        optimizer.cache = MagicMock()
        optimizer._loading_requests["fixture-request"] = ASSET_URL
        event = _event()
        event.response_error_reason = "CONNECTION_RESET"
        optimizer._on_request_paused(event)
        optimizer.cache.read.assert_not_called()
        optimizer.cache.write.assert_not_called()
        optimizer._devtools.fetch.get_response_body.assert_not_called()
        optimizer._devtools.fetch.fulfill_request.assert_not_called()
        optimizer._devtools.fetch.continue_response.assert_called_once_with("fixture-request")
        optimizer.cache.release_load.assert_called_once_with(ASSET_URL)

    def test_shutdown_prevents_late_callbacks_from_rebuilding_leases(self):
        for shutdown in ("finalize", "recovery"):
            with self.subTest(shutdown=shutdown):
                optimizer = _optimizer()
                optimizer.cache = MagicMock()
                optimizer._loading_requests["pending"] = ASSET_URL
                optimizer.finalize() if shutdown == "finalize" else optimizer.disable_for_recovery("fixture_recovery")
                self.assertFalse(optimizer._fetch_enabled)
                optimizer.cache.release_load.assert_called_once_with(ASSET_URL)
                optimizer._on_request_paused(_event())
                optimizer._on_request_paused(_event(response_headers=_public_headers()))
                optimizer.cache.read.assert_not_called()
                optimizer.cache.claim_load.assert_not_called()
                optimizer.cache.write.assert_not_called()
                optimizer._devtools.fetch.fulfill_request.assert_not_called()
                optimizer._devtools.fetch.get_response_body.assert_not_called()
                self.assertEqual(optimizer._loading_requests, {})

    def test_shutdown_during_wait_prevents_late_cache_replay_or_continue(self):
        for shutdown in ("finalize", "recovery"):
            with self.subTest(shutdown=shutdown):
                cached = {"body": b"public-fixture", "headers": _public_headers(), "status": 200, "phrase": "OK", "expires_at": 1e20}
                for wait_result in (cached, None):
                    with self.subTest(cache_hit=wait_result is not None):
                        optimizer = _optimizer()
                        optimizer.cache = MagicMock()
                        optimizer.cache.read.return_value = None
                        optimizer.cache.claim_load.return_value = False
                        def wait(*args, **kwargs):
                            optimizer.finalize() if shutdown == "finalize" else optimizer.disable_for_recovery("fixture_recovery")
                            return wait_result
                        optimizer.cache.wait_for_load.side_effect = wait
                        optimizer._on_request_paused(_event())
                        optimizer._devtools.fetch.fulfill_request.assert_not_called()
                        optimizer._devtools.fetch.continue_request.assert_not_called()
                        optimizer._devtools.fetch.continue_response.assert_not_called()
                        optimizer._connection.execute.assert_called_once_with(optimizer._devtools.fetch.disable.return_value)
                        self.assertEqual(optimizer._stats["cache_hits"], 0)
                        self.assertEqual(optimizer._loading_requests, {})

    def test_shutdown_during_body_read_prevents_write_or_continue(self):
        for shutdown in ("finalize", "recovery"):
            with self.subTest(shutdown=shutdown):
                optimizer = _optimizer()
                optimizer.cache = MagicMock()
                marker = object()
                optimizer._devtools.fetch.get_response_body.return_value = marker
                def execute(command):
                    if command is marker:
                        optimizer.finalize() if shutdown == "finalize" else optimizer.disable_for_recovery("fixture_recovery")
                        return "public-fixture", False
                optimizer._connection.execute.side_effect = execute
                optimizer._on_request_paused(_event(response_headers=_public_headers()))
                optimizer.cache.write.assert_not_called()
                optimizer._devtools.fetch.continue_response.assert_not_called()
                self.assertEqual(optimizer._connection.execute.call_count, 2)
                self.assertFalse(optimizer._fetch_enabled)


class PerformanceSummaryTests(unittest.TestCase):
    def test_summary_uses_encoded_network_bytes_and_cache_bytes(self):
        def entry(method, params):
            return {"message": json.dumps({"message": {"method": method, "params": params}})}

        entries = [
            entry("Network.requestWillBeSent", {"requestId": "1", "request": {"url": "https://chatgpt.com/a.js"}}),
            entry("Network.loadingFinished", {"requestId": "1", "encodedDataLength": 1234}),
            entry("Network.requestWillBeSent", {"requestId": "cached", "request": {"url": "https://chatgpt.com/cached.js"}}),
            entry("Network.loadingFinished", {"requestId": "cached", "encodedDataLength": 500}),
            entry("Network.requestWillBeSent", {"requestId": "2", "request": {"url": "https://statsigapi.net/log"}}),
            entry("Network.loadingFailed", {"requestId": "2", "blockedReason": "inspector"}),
        ]
        summary = summarize_performance_logs(
            entries,
            cached_bytes=500,
            cache_hits=1,
            cache_misses=1,
            cached_request_urls=["https://chatgpt.com/cached.js"],
            budget_bytes=2000,
        )
        self.assertEqual(summary["downloaded"], 1234)
        self.assertEqual(summary["logical_downloaded"], 1734)
        self.assertEqual(summary["network_requests"], 1)
        self.assertEqual(summary["cache_saved_bytes"], 500)
        self.assertTrue(summary["within_budget"])
        self.assertEqual(summary["blocked_by_reason"], {"inspector": 1})

    def test_summary_prefers_exact_cache_request_id_for_repeated_url(self):
        def entry(method, params):
            return {"message": json.dumps({"message": {"method": method, "params": params}})}

        url = "https://chatgpt.com/cdn/assets/shared.js"
        entries = [
            entry("Network.requestWillBeSent", {"requestId": "network", "request": {"url": url}}),
            entry("Network.loadingFinished", {"requestId": "network", "encodedDataLength": 1200}),
            entry("Network.requestWillBeSent", {"requestId": "replay", "request": {"url": url}}),
            entry("Network.loadingFinished", {"requestId": "replay", "encodedDataLength": 500}),
        ]

        summary = summarize_performance_logs(
            entries,
            cached_bytes=500,
            cache_hits=1,
            cached_request_ids=["replay"],
            budget_bytes=2000,
        )

        self.assertEqual(summary["downloaded"], 1200)
        self.assertEqual(summary["logical_downloaded"], 1700)
        self.assertEqual(summary["network_requests"], 1)

    def test_network_request_count_excludes_blocked_requests(self):
        def entry(method, params):
            return {"message": json.dumps({"message": {"method": method, "params": params}})}

        entries = [
            entry("Network.requestWillBeSent", {
                "requestId": "allowed", "request": {"url": "https://chatgpt.com/api/auth/session"},
            }),
            entry("Network.loadingFinished", {"requestId": "allowed", "encodedDataLength": 300}),
            entry("Network.requestWillBeSent", {
                "requestId": "blocked", "type": "Fetch",
                "request": {"url": "https://statsigapi.net/log"},
            }),
            entry("Network.loadingFailed", {"requestId": "blocked", "blockedReason": "inspector"}),
        ]

        summary = summarize_performance_logs(entries)

        self.assertEqual(summary["network_requests"], 1)
        self.assertEqual(summary["blocked"], 1)

    def test_external_blocked_reason_is_preserved_without_reclassifying_images(self):
        def entry(method, params):
            return {"message": json.dumps({"message": {"method": method, "params": params}})}

        entries = [
            entry("Network.requestWillBeSent", {
                "requestId": "image", "type": "Image",
                "request": {"url": "https://chatgpt.com/cdn/assets/avatar"},
            }),
            entry("Network.loadingFailed", {"requestId": "image", "blockedReason": "inspector"}),
        ]

        summary = summarize_performance_logs(entries)

        self.assertEqual(summary["blocked_by_reason"], {"inspector": 1})

    def test_roxy_finalize_logs_document_diagnostics_without_losing_summary(self):
        from core.roxy_registration import _finish_traffic_optimizer

        optimizer = MagicMock()
        optimizer.finalize.return_value = {
            "downloaded": 300,
            "logical_downloaded": 800,
            "cache_saved_bytes": 500,
            "cache_hits": 1,
            "cache_misses": 1,
            "blocked": 2,
            "network_requests": 1,
            "within_budget": True,
            "errors": [],
            "blocked_by_reason": {"optional_media": 2},
            "by_host": {"chatgpt.com": 300},
            "by_path": {"chatgpt.com/api/auth/session": 300},
            "degraded_reason": "",
        }

        with self.assertLogs("core.roxy_registration", level="INFO") as captured:
            summary = _finish_traffic_optimizer(optimizer)

        self.assertEqual(summary["downloaded"], 300)
        detail = next(line for line in captured.output if "blocked_by_reason=" in line)
        self.assertIn('blocked_by_reason={"optional_media":2}', detail)
        self.assertIn('by_host={"chatgpt.com":300}', detail)


if __name__ == "__main__":
    unittest.main()
