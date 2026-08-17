# -*- coding: utf-8 -*-
import json
import tempfile
import threading
import unittest
from pathlib import Path
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
        self.assertTrue(is_cacheable_request("https://oaistatic.com/site.css", "GET", "stylesheet"))
        self.assertTrue(is_cacheable_request(
            "https://chatgpt.com/cdn/assets/app.js", "GET", "script", {"Cookie": "session=browser-state"},
        ))
        self.assertFalse(is_cacheable_request("https://chatgpt.com/_next/static/app.js", "POST", "script"))
        self.assertFalse(is_cacheable_request("https://chatgpt.com/sw.js", "GET", "script"))
        self.assertFalse(is_cacheable_request("https://example.com/app.js", "GET", "script"))
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/api/account-script", "GET", "script", {"Cookie": "account-state"},
        ))
        self.assertFalse(is_cacheable_request(
            "https://chatgpt.com/_next/static/app.js", "GET", "script", {"Authorization": "Bearer token"},
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
        self.assertFalse(is_cacheable_response([{"name": "Vary", "value": "Accept-Encoding, Cookie"}]))
        self.assertTrue(is_cacheable_response([{"name": "Cache-Control", "value": "public, max-age=3600"}]))


class StaticCacheTests(unittest.TestCase):
    def test_parallel_caches_reuse_the_first_public_asset_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            second = StaticResourceCache(Path(tmp), max_age=3600, max_item_bytes=1024)
            url = "https://chatgpt.com/cdn/assets/app.js"
            result = {}

            self.assertTrue(first.claim_load(url))

            waiter = threading.Thread(
                target=lambda: result.setdefault("cached", second.wait_for_load(url, timeout=1)),
            )
            waiter.start()
            self.assertTrue(first.write(url, status=200, phrase="OK", headers=[], body=b"shared"))
            first.release_load(url)
            waiter.join(timeout=2)

            self.assertFalse(waiter.is_alive())
            self.assertEqual(result["cached"]["body"], b"shared")

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
                    {"name": "Content-Length", "value": "3"},
                    {"name": "Set-Cookie", "value": "secret=never-replay"},
                ],
                body=b"abc",
            ))
            item = cache.read(url)
            self.assertIsNotNone(item)
            self.assertEqual(item["body"], b"abc")
            self.assertEqual(item["headers"], [{"name": "Content-Type", "value": "application/javascript"}])
            self.assertFalse(list(Path(tmp).glob("*.tmp")))

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
            cache.write(url, status=200, phrase="OK", headers=[], body=b"abc")
            body_path = next(Path(tmp).glob("*.bin"))
            body_path.write_bytes(b"tampered")
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
        self.assertEqual(summary["network_requests"], 2)
        self.assertEqual(summary["cache_saved_bytes"], 500)
        self.assertTrue(summary["within_budget"])
        self.assertEqual(summary["blocked_by_reason"], {"telemetry": 1})


if __name__ == "__main__":
    unittest.main()
