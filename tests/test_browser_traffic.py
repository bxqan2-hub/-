# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.browser_traffic import (
    RoxyTrafficOptimizer,
    StaticResourceCache,
    block_reason,
    is_cacheable_response,
    is_cacheable_request,
    summarize_performance_logs,
)


class BrowserTrafficClassifierTests(unittest.TestCase):
    def test_network_enable_uses_bounded_per_profile_buffers(self):
        driver = MagicMock()
        optimizer = RoxyTrafficOptimizer(
            driver,
            low_traffic=True,
            static_cache=False,
            capture=False,
            cache_dir=Path("unused-test-cache"),
            cache_max_age=60,
            cache_max_item_bytes=1024,
            cache_refresh_rate=0,
            cache_refresh_budget_bytes=0,
            cache_refresh_max_item_bytes=0,
            budget_bytes=1024,
        )

        optimizer.install()

        enable_calls = [
            item for item in driver.execute_cdp_cmd.call_args_list
            if item.args and item.args[0] == "Network.enable"
        ]
        self.assertEqual(len(enable_calls), 1)
        self.assertEqual(enable_calls[0].args[1], {
            "maxTotalBufferSize": 2 * 1024 * 1024,
            "maxResourceBufferSize": 512 * 1024,
            "maxPostDataSize": 4 * 1024,
        })

    def test_network_enable_falls_back_for_legacy_cdp(self):
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = [RuntimeError("unknown parameter"), None, None, None, None]
        optimizer = RoxyTrafficOptimizer(
            driver,
            low_traffic=True,
            static_cache=False,
            capture=False,
            cache_dir=Path("unused-test-cache"),
            cache_max_age=60,
            cache_max_item_bytes=1024,
            cache_refresh_rate=0,
            cache_refresh_budget_bytes=0,
            cache_refresh_max_item_bytes=0,
            budget_bytes=1024,
        )

        optimizer.install()

        enable_calls = [
            item for item in driver.execute_cdp_cmd.call_args_list
            if item.args and item.args[0] == "Network.enable"
        ]
        self.assertEqual([item.args[1] for item in enable_calls], [
            {
                "maxTotalBufferSize": 2 * 1024 * 1024,
                "maxResourceBufferSize": 512 * 1024,
                "maxPostDataSize": 4 * 1024,
            },
            {},
        ])

    def test_recovery_disables_fetch_and_clears_blocked_urls(self):
        driver = MagicMock()
        optimizer = RoxyTrafficOptimizer(
            driver,
            low_traffic=True,
            static_cache=True,
            capture=False,
            cache_dir=Path("unused-test-cache"),
            cache_max_age=60,
            cache_max_item_bytes=1024,
            cache_refresh_rate=0,
            cache_refresh_budget_bytes=0,
            cache_refresh_max_item_bytes=0,
            budget_bytes=1024,
        )
        optimizer._fetch_enabled = True
        optimizer._devtools = MagicMock()
        optimizer._connection = MagicMock()

        optimizer.disable_for_recovery("auth_error")

        self.assertFalse(optimizer.low_traffic)
        self.assertFalse(optimizer.static_cache_enabled)
        self.assertFalse(optimizer._fetch_enabled)
        self.assertEqual(optimizer._degraded_reason, "auth_error")
        driver.execute_cdp_cmd.assert_called_once_with("Network.setBlockedURLs", {"urls": []})
        optimizer._connection.execute.assert_called_once_with(optimizer._devtools.fetch.disable())

    def test_security_hosts_are_allowed_before_resource_rules(self):
        self.assertEqual(block_reason("https://challenges.cloudflare.com/widget.js", "script"), "")
        self.assertEqual(block_reason("https://sentinel.openai.com/a.png", "image"), "")

    def test_nonessential_resources_are_classified(self):
        self.assertEqual(block_reason("https://statsigapi.net/v1/log", "xhr"), "telemetry")
        self.assertEqual(block_reason("https://cdn.openai.com/a.woff2", "font"), "font")
        self.assertEqual(block_reason("https://accounts.google.com/o/oauth2", "document"), "optional_identity")
        self.assertEqual(block_reason("https://auth.openai.com/awe/api/v2/rum", "xhr"), "telemetry")
        self.assertEqual(block_reason("https://unknown.example/a.js", "script"), "")

    def test_session_only_keeps_session_exceptions(self):
        self.assertEqual(block_reason("https://chatgpt.com/api/auth/session", "xhr", session_only=True), "")
        self.assertEqual(block_reason("https://chatgpt.com/api/auth/callback/openai", "xhr", session_only=True), "")
        self.assertEqual(block_reason("https://chatgpt.com/_next/static/app.js", "script", session_only=True), "post_auth_script")

    def test_cache_scope_is_first_party_public_get_script_or_css(self):
        self.assertTrue(is_cacheable_request("https://chatgpt.com/_next/static/app.js", "GET", "script"))
        self.assertTrue(is_cacheable_request("https://chatgpt.com/cdn/assets/site.css", "GET", "stylesheet"))
        self.assertFalse(is_cacheable_request(
            "https://auth-cdn.oaistatic.com/assets/app-core.css", "GET", "stylesheet",
        ))
        self.assertFalse(is_cacheable_request(
            "https://auth.openai.com/assets/login.js", "GET", "script",
        ))
        self.assertTrue(is_cacheable_request(
            "https://chatgpt.com/cdn/assets/app.js", "GET", "script", {"Cookie": "session=browser-state"},
        ))
        self.assertFalse(is_cacheable_request("https://chatgpt.com/_next/static/app.js", "POST", "script"))
        self.assertFalse(is_cacheable_request("https://chatgpt.com/sw.js", "GET", "script"))
        self.assertFalse(is_cacheable_request("https://example.com/app.js", "GET", "script"))
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/api/account-script", "GET", "script", {"Cookie": "account-state"},
        ))
        # Challenge, Sentinel, unauthenticated script, and backend paths must
        # never be replayed from the cross-profile static cache, even without
        # a Cookie header.
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/backend-api/sentinel/sdk.js", "GET", "script",
        ))
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/sentinel/20260810913b/sdk.js", "GET", "script",
        ))
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/cdn-cgi/challenge-platform/main.js", "GET", "script",
        ))
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/unauth-mweb/scripts/declarative-partial-updates.js", "GET", "script",
        ))
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/_next/static/app.js", "GET", "script", {"Authorization": "Bearer token"},
        ))
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/_next/static/app.js", "GET", "script", {"Proxy-Authorization": "Basic proxy"},
        ))
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/_next/static/app.js", "GET", "script", {"Cache-Control": "no-cache"},
        ))
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/_next/static/app.js", "GET", "script", {"Pragma": "no-cache"},
        ))
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/assets/../backend-api/sdk.js", "GET", "script",
        ))
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/assets/%2e%2e/backend-api/sdk.js", "GET", "script",
        ))

    def test_roxy_patterns_cover_current_telemetry_and_session_shell_paths(self):
        driver = MagicMock()
        optimizer = RoxyTrafficOptimizer(
            driver,
            low_traffic=True,
            static_cache=False,
            capture=False,
            cache_dir=Path("unused-test-cache"),
            cache_max_age=60,
            cache_max_item_bytes=1024,
            cache_refresh_rate=0,
            cache_refresh_budget_bytes=0,
            cache_refresh_max_item_bytes=0,
            budget_bytes=1024,
        )

        optimizer.set_session_only(True)

        patterns = driver.execute_cdp_cmd.call_args.args[1]["urls"]
        self.assertIn("*://auth.openai.com/awe/api/v2/rum*", patterns)
        self.assertIn("*://chatgpt.com/cdn/assets/*", patterns)
        self.assertIn("*://chatgpt.com/unauth-mweb/assets/*", patterns)

    def test_cache_rejects_stateful_responses(self):
        self.assertFalse(is_cacheable_response([{"name": "Set-Cookie", "value": "sid=secret"}]))
        self.assertFalse(is_cacheable_response([{"name": "Cache-Control", "value": "private, max-age=60"}]))
        self.assertFalse(is_cacheable_response([{"name": "Cache-Control", "value": "no-cache, must-revalidate, s-maxage=0"}]))
        self.assertFalse(is_cacheable_response([{"name": "Vary", "value": "Accept-Encoding, Cookie"}]))
        self.assertFalse(is_cacheable_response([{"name": "Vary", "value": "Accept-Language"}]))
        self.assertFalse(is_cacheable_response([{"name": "Vary", "value": "User-Agent"}]))
        self.assertFalse(is_cacheable_response([{"name": "Vary", "value": "*"}]))
        self.assertFalse(is_cacheable_response([
            {"name": "Vary", "value": "Accept-Encoding"},
            {"name": "Vary", "value": "Origin"},
        ]))
        self.assertTrue(is_cacheable_response([
            {"name": "Cache-Control", "value": "public, max-age=3600"},
            {"name": "Vary", "value": "Accept-Encoding"},
        ]))
        self.assertFalse(is_cacheable_response([]))
        self.assertTrue(is_cacheable_response([{"name": "Cache-Control", "value": "public, max-age=3600"}]))


class StaticCacheTests(unittest.TestCase):
    def test_cookie_bearing_public_asset_replays_validated_body_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = "https://chatgpt.com/cdn/assets/app.js"
            cache = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            self.assertTrue(cache.write(
                url,
                status=200,
                phrase="OK",
                headers=[
                    {"name": "Content-Type", "value": "application/javascript"},
                    {"name": "Cache-Control", "value": "public, max-age=3600"},
                ],
                body=b"public-body",
            ))
            optimizer = RoxyTrafficOptimizer(
                MagicMock(),
                low_traffic=False,
                static_cache=True,
                capture=False,
                cache_dir=Path(tmp),
                cache_max_age=3600,
                cache_max_item_bytes=1024,
                cache_refresh_rate=0,
                cache_refresh_budget_bytes=0,
                cache_refresh_max_item_bytes=0,
                budget_bytes=1024,
            )
            optimizer._devtools = MagicMock()
            optimizer._connection = MagicMock()
            event = SimpleNamespace(
                request_id="cookie-public",
                request=SimpleNamespace(
                    url=url,
                    method="GET",
                    headers={"Cookie": "account-state"},
                ),
                resource_type="script",
                response_status_code=None,
            )

            optimizer._on_request_paused(event)

            self.assertEqual(optimizer._stats["cache_hits"], 1)
            self.assertEqual(optimizer._stats["cached_bytes"], len(b"public-body"))
            optimizer._devtools.fetch.fulfill_request.assert_called_once()
            optimizer._devtools.fetch.continue_request.assert_not_called()

    def test_each_profile_miss_continues_to_live_network(self):
        optimizer = RoxyTrafficOptimizer(
            MagicMock(),
            low_traffic=False,
            static_cache=True,
            capture=False,
            cache_dir=Path("unused-test-cache"),
            cache_max_age=60,
            cache_max_item_bytes=1024,
            cache_refresh_rate=0,
            cache_refresh_budget_bytes=0,
            cache_refresh_max_item_bytes=0,
            budget_bytes=1024,
        )
        optimizer._devtools = MagicMock()
        optimizer._connection = MagicMock()
        optimizer._devtools.fetch.continue_request.side_effect = (
            lambda request_id, **kwargs: ("continue_request", request_id, kwargs)
        )
        optimizer.cache.read = MagicMock(return_value=None)

        def event(request_id):
            return SimpleNamespace(
                request_id=request_id,
                request=SimpleNamespace(
                    url="https://chatgpt.com/_next/static/app.js",
                    method="GET",
                    headers={},
                ),
                resource_type="script",
                response_status_code=None,
            )

        optimizer._on_request_paused(event("profile-a"))
        optimizer._on_request_paused(event("profile-b"))

        self.assertEqual(optimizer._connection.execute.call_count, 2)
        self.assertEqual(optimizer._stats["cache_candidates"], 2)
        self.assertTrue(all(
            call_args.args[0][0] == "continue_request"
            and call_args.args[0][2]["intercept_response"] is True
            for call_args in optimizer._connection.execute.call_args_list
        ))

    def test_cache_instances_share_only_validated_public_asset_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            second = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            url = "https://chatgpt.com/cdn/assets/app.js"
            self.assertTrue(first.write(
                url, status=200, phrase="OK",
                headers=[{"name": "Cache-Control", "value": "public, max-age=3600"}], body=b"shared",
            ))
            self.assertEqual(second.read(url)["body"], b"shared")
            # Cache bytes remain shareable only after validation; misses are
            # fetched independently by each Profile, matching the upstream
            # implementation and avoiding a synchronized waiter pattern.

    def test_write_read_and_header_sanitization(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            url = "https://chatgpt.com/_next/static/app.js"
            self.assertTrue(cache.write(
                url,
                status=200,
                phrase="OK",
                headers=[
                    {"name": "Content-Type", "value": "application/javascript"},
                    {"name": "Cache-Control", "value": "public, max-age=3600"},
                    {"name": "Content-Length", "value": "3"},
                    {"name": "CF-Ray", "value": "edge-id"},
                    {"name": "Date", "value": "Wed, 03 Sep 2026 00:00:00 GMT"},
                    {"name": "Report-To", "value": '{"group":"cf-nel"}'},
                    {"name": "ETag", "value": '"stale-edge-tag"'},
                    {"name": "Traceparent", "value": "00-edge-trace"},
                    {"name": "X-Envoy-Upstream-Service-Time", "value": "12"},
                ],
                body=b"abc",
            ))
            item = cache.read(url)
            self.assertIsNotNone(item)
            self.assertEqual(item["body"], b"abc")
            self.assertEqual(item["headers"], [
                {"name": "Content-Type", "value": "application/javascript"},
                {"name": "Cache-Control", "value": "public, max-age=3600"},
            ])
            self.assertFalse(list(Path(tmp).glob("*.tmp")))
            self.assertFalse(cache.write(
                url,
                status=200,
                phrase="OK",
                headers=[
                    {"name": "Cache-Control", "value": "public, max-age=3600"},
                    {"name": "Set-Cookie", "value": "secret=never-replay"},
                ],
                body=b"private",
            ))

    def test_legacy_cache_metadata_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            url = "https://chatgpt.com/_next/static/app.js"
            meta_path, body_path = cache._paths(url)
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_bytes(b"abc")
            meta_path.write_text(json.dumps({
                "url": url,
                "status": 200,
                "saved_at": 1e20,
                "body_sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            }), encoding="utf-8")
            self.assertIsNone(cache.read(url))

    def test_corrupt_digest_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            url = "https://chatgpt.com/_next/static/app.js"
            cache.write(
                url, status=200, phrase="OK",
                headers=[{"name": "Cache-Control", "value": "public, max-age=3600"}], body=b"abc",
            )
            body_path = next(Path(tmp).glob("*.bin"))
            body_path.write_bytes(b"tampered")
            self.assertIsNone(cache.read(url))

    def test_non_success_status_metadata_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            url = "https://chatgpt.com/_next/static/app.js"
            self.assertTrue(cache.write(
                url, status=200, phrase="OK",
                headers=[{"name": "Cache-Control", "value": "public, max-age=3600"}], body=b"abc",
            ))
            meta_path, _ = cache._paths(url)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["status"] = 404
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            self.assertIsNone(cache.read(url))


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
        self.assertEqual(summary["blocked_by_reason"], {"telemetry": 1})

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

    def test_blocked_reason_uses_request_resource_type(self):
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

        self.assertEqual(summary["blocked_by_reason"], {"image": 1})

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
            "blocked_by_reason": {"telemetry": 2},
            "by_host": {"chatgpt.com": 300},
            "by_path": {"chatgpt.com/api/auth/session": 300},
            "degraded_reason": "",
        }

        with self.assertLogs("core.roxy_registration", level="INFO") as captured:
            summary = _finish_traffic_optimizer(optimizer)

        self.assertEqual(summary["downloaded"], 300)
        detail = next(line for line in captured.output if "blocked_by_reason=" in line)
        self.assertIn('blocked_by_reason={"telemetry":2}', detail)
        self.assertIn('by_host={"chatgpt.com":300}', detail)


if __name__ == "__main__":
    unittest.main()
