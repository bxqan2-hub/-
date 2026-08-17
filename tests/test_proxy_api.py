# -*- coding: utf-8 -*-
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, call, patch

from config import proxy as proxy_cfg
from webui import config_editor


class ProxyApiTests(unittest.TestCase):
    def setUp(self):
        proxy_cfg._PROXY_API_CACHE["key"] = None
        proxy_cfg._PROXY_API_CACHE["proxy"] = ""
        proxy_cfg._PROXY_API_CACHE["expires_at"] = 0.0
        proxy_cfg._PROXY_API_FLIGHTS.clear()
        proxy_cfg._STATIC_PROXY_INDEX = 0

    def test_normalizes_fullwidth_socks5_punctuation(self):
        self.assertEqual(
            proxy_cfg._normalize_proxy_url("socks5：／／user：pass＠proxy．test：3010"),
            "socks5://user:pass@proxy.test:3010",
        )

    def test_static_pool_uses_round_robin(self):
        with patch.object(proxy_cfg, "PROXY_POOL", [
            "socks5h://one.example:3010",
            "socks5h://two.example:3010",
        ]):
            self.assertEqual(proxy_cfg._pick_static_or_system_proxy(), "socks5h://one.example:3010")
            self.assertEqual(proxy_cfg._pick_static_or_system_proxy(), "socks5h://two.example:3010")
            self.assertEqual(proxy_cfg._pick_static_or_system_proxy(), "socks5h://one.example:3010")

    def test_parse_cliproxy_txt_host_port(self):
        self.assertEqual(
            proxy_cfg.parse_proxy_api_response("107.151.197.81:22403\n", "socks5h"),
            ["socks5h://107.151.197.81:22403"],
        )

    def test_parse_host_port_username_password(self):
        self.assertEqual(
            proxy_cfg.parse_proxy_api_response("proxy.test:3010:user:name-pass", "socks5h"),
            ["socks5h://user:name-pass@proxy.test:3010"],
        )

    def test_parse_common_json_shape(self):
        body = '{"data":[{"ip":"1.2.3.4","port":1080}]}'
        self.assertEqual(
            proxy_cfg.parse_proxy_api_response(body, "socks5"),
            ["socks5://1.2.3.4:1080"],
        )

    def test_parse_named_api_profiles(self):
        profiles = proxy_cfg.parse_proxy_api_profiles([
            "US|https://api.example.test/us?num=1",
            "JP=https://api.example.test/jp?num=1",
        ])
        self.assertEqual(profiles, [
            ("US", "https://api.example.test/us?num=1"),
            ("JP", "https://api.example.test/jp?num=1"),
        ])

    def test_plain_urls_infer_region_and_friendly_label(self):
        profiles = proxy_cfg.parse_proxy_api_profiles([
            "https://api.example.test/white/api?region=US&num=1",
            "https://api.example.test/white/api?region=Rand&num=1",
        ])
        self.assertEqual([name for name, _ in profiles], ["US", "RAND"])
        self.assertEqual(proxy_cfg.proxy_region_label("US"), "美国")
        self.assertEqual(proxy_cfg.proxy_region_label("RAND"), "随机地区")

    def test_region_query_is_case_insensitive(self):
        self.assertEqual(
            proxy_cfg.infer_proxy_api_region("https://api.example.test/white?REGION=jp"),
            "JP",
        )

    def test_active_api_profile_selects_matching_region(self):
        with (
            patch.object(proxy_cfg, "PROXY_API_PROFILES", [
                "US|https://api.example.test/us",
                "JP|https://api.example.test/jp",
            ]),
            patch.object(proxy_cfg, "PROXY_API_ACTIVE", "JP"),
            patch.object(proxy_cfg, "PROXY_API_URL", "https://api.example.test/fallback"),
        ):
            self.assertEqual(proxy_cfg.get_active_proxy_api_url(), "https://api.example.test/jp")

    def test_fetch_proxy_from_api(self):
        response = Mock()
        response.text = "107.151.197.81:22403"
        response.raise_for_status.return_value = None
        with (
            patch.object(proxy_cfg.requests, "get", return_value=response) as get,
            patch.object(proxy_cfg, "PROXY_API_VALIDATE", False),
        ):
            value = proxy_cfg.fetch_proxy_from_api(
                api_url="https://api.example.test/proxy",
                protocol="socks5h",
                max_attempts=1,
                force=True,
            )
        self.assertEqual(value, "socks5h://107.151.197.81:22403")
        self.assertEqual(get.call_count, 1)

    def test_fetch_retries_when_endpoint_validation_fails(self):
        first = Mock(text="1.2.3.4:1080")
        second = Mock(text="5.6.7.8:1080")
        first.raise_for_status.return_value = None
        second.raise_for_status.return_value = None
        with (
            patch.object(proxy_cfg.requests, "get", side_effect=[first, second]) as get,
            patch.object(proxy_cfg, "PROXY_API_VALIDATE", True),
            patch.object(proxy_cfg, "validate_proxy_endpoint", side_effect=[RuntimeError("expired"), None]) as validate,
            patch.object(proxy_cfg.time, "sleep"),
        ):
            value = proxy_cfg.fetch_proxy_from_api(
                api_url="https://api.example.test/proxy",
                protocol="socks5h",
                max_attempts=2,
                force=True,
            )
        self.assertEqual(value, "socks5h://5.6.7.8:1080")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(validate.call_count, 2)

    def test_configured_fetch_waits_and_succeeds_on_third_attempt(self):
        response = Mock(text="5.6.7.8:1080")
        response.raise_for_status.return_value = None
        with (
            patch.object(proxy_cfg.requests, "get", side_effect=[
                TimeoutError("first timeout"),
                TimeoutError("second timeout"),
                response,
            ]) as get,
            patch.object(proxy_cfg, "PROXY_API_VALIDATE", False),
            patch.object(proxy_cfg, "PROXY_API_MAX_ATTEMPTS", 3),
            patch.object(proxy_cfg, "PROXY_API_RETRY_DELAY", 3.0),
            patch.object(proxy_cfg.time, "sleep") as sleep,
        ):
            value = proxy_cfg.fetch_proxy_from_api(
                api_url="https://api.example.test/proxy",
                force=True,
            )

        self.assertEqual(value, "socks5h://5.6.7.8:1080")
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(3.0), call(6.0)])

    def test_configured_fetch_reports_failure_after_three_attempts(self):
        with (
            patch.object(proxy_cfg.requests, "get", side_effect=TimeoutError("still unavailable")) as get,
            patch.object(proxy_cfg, "PROXY_API_MAX_ATTEMPTS", 3),
            patch.object(proxy_cfg, "PROXY_API_RETRY_DELAY", 3.0),
            patch.object(proxy_cfg.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "代理 API 请求失败.*still unavailable"):
                proxy_cfg.fetch_proxy_from_api(
                    api_url="https://api.example.test/proxy",
                    force=True,
                )

        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(3.0), call(6.0)])

    def test_fetch_can_skip_redundant_endpoint_validation(self):
        response = Mock(text="128.1.12.147:16601")
        response.raise_for_status.return_value = None
        with (
            patch.object(proxy_cfg.requests, "get", return_value=response),
            patch.object(proxy_cfg, "PROXY_API_VALIDATE", True),
            patch.object(proxy_cfg, "validate_proxy_endpoint") as validate_endpoint,
        ):
            value = proxy_cfg.fetch_proxy_from_api(
                api_url="https://api.example.test/proxy",
                protocol="socks5h",
                max_attempts=1,
                validate=False,
                force=True,
            )

        self.assertEqual(value, "socks5h://128.1.12.147:16601")
        validate_endpoint.assert_not_called()

    def test_fetch_reports_node_validation_separately_from_api_request(self):
        response = Mock(text="128.1.12.147:16601")
        response.raise_for_status.return_value = None
        with (
            patch.object(proxy_cfg.requests, "get", return_value=response),
            patch.object(proxy_cfg, "PROXY_API_VALIDATE", True),
            patch.object(proxy_cfg, "validate_proxy_endpoint", side_effect=TimeoutError("timed out")),
        ):
            with self.assertRaisesRegex(RuntimeError, "API 已成功返回节点.*节点连接验证失败"):
                proxy_cfg.fetch_proxy_from_api(
                    api_url="https://api.example.test/proxy",
                    protocol="socks5h",
                    max_attempts=1,
                    force=True,
                )

    def test_concurrent_fetches_share_one_request_and_validation(self):
        response = Mock(text="128.1.12.147:16601")
        response.raise_for_status.return_value = None
        start = threading.Barrier(11)

        def slow_get(*_args, **_kwargs):
            time.sleep(0.1)
            return response

        def fetch():
            start.wait(timeout=2)
            return proxy_cfg.fetch_proxy_from_api(
                api_url="https://api.example.test/proxy",
                protocol="socks5h",
                max_attempts=1,
                force=True,
            )

        with (
            patch.object(proxy_cfg.requests, "get", side_effect=slow_get) as get,
            patch.object(proxy_cfg, "PROXY_API_VALIDATE", True),
            patch.object(proxy_cfg, "validate_proxy_endpoint") as validate,
            ThreadPoolExecutor(max_workers=10) as executor,
        ):
            futures = [executor.submit(fetch) for _ in range(10)]
            start.wait(timeout=2)
            values = [future.result(timeout=2) for future in futures]

        self.assertEqual(values, ["socks5h://128.1.12.147:16601"] * 10)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(validate.call_count, 1)

    def test_concurrent_fetches_share_one_three_attempt_sequence(self):
        response = Mock(text="128.1.12.147:16601")
        response.raise_for_status.return_value = None
        start = threading.Barrier(11)
        attempt_lock = threading.Lock()
        attempt_count = 0

        def flaky_get(*_args, **_kwargs):
            nonlocal attempt_count
            with attempt_lock:
                attempt_count += 1
                current_attempt = attempt_count
            time.sleep(0.05)
            if current_attempt < 3:
                raise TimeoutError(f"attempt {current_attempt}")
            return response

        def fetch():
            start.wait(timeout=2)
            return proxy_cfg.fetch_proxy_from_api(
                api_url="https://api.example.test/proxy",
                protocol="socks5h",
                max_attempts=3,
                retry_delay=0.01,
                validate=False,
                force=True,
            )

        with (
            patch.object(proxy_cfg.requests, "get", side_effect=flaky_get) as get,
            ThreadPoolExecutor(max_workers=10) as executor,
        ):
            futures = [executor.submit(fetch) for _ in range(10)]
            start.wait(timeout=2)
            values = [future.result(timeout=3) for future in futures]

        self.assertEqual(values, ["socks5h://128.1.12.147:16601"] * 10)
        self.assertEqual(get.call_count, 3)

    def test_concurrent_failure_is_shared_but_next_call_retries(self):
        start = threading.Barrier(6)

        def slow_failure(*_args, **_kwargs):
            time.sleep(0.1)
            raise TimeoutError("api timed out")

        def fetch():
            start.wait(timeout=2)
            try:
                proxy_cfg.fetch_proxy_from_api(
                    api_url="https://api.example.test/proxy",
                    max_attempts=1,
                    force=True,
                )
            except RuntimeError as exc:
                return str(exc)
            self.fail("expected proxy API failure")

        response = Mock(text="5.6.7.8:1080")
        response.raise_for_status.return_value = None
        with (
            patch.object(proxy_cfg.requests, "get", side_effect=slow_failure) as get,
            ThreadPoolExecutor(max_workers=5) as executor,
        ):
            futures = [executor.submit(fetch) for _ in range(5)]
            start.wait(timeout=2)
            errors = [future.result(timeout=2) for future in futures]
            self.assertEqual(get.call_count, 1)
            self.assertTrue(all("api timed out" in error for error in errors))

            get.side_effect = None
            get.return_value = response
            with patch.object(proxy_cfg, "PROXY_API_VALIDATE", False):
                value = proxy_cfg.fetch_proxy_from_api(
                    api_url="https://api.example.test/proxy",
                    max_attempts=1,
                    force=True,
                )

        self.assertEqual(value, "socks5h://5.6.7.8:1080")
        self.assertEqual(get.call_count, 2)

    def test_pick_proxy_prefers_api(self):
        with (
            patch.object(proxy_cfg, "PROXY_API_ENABLED", True),
            patch.object(proxy_cfg, "PROXY_API_FAIL_CLOSED", True),
            patch.object(proxy_cfg, "fetch_proxy_from_api", return_value="socks5h://1.2.3.4:1080") as fetch,
            patch.object(proxy_cfg, "_pick_static_or_system_proxy", return_value="http://127.0.0.1:10808") as fallback,
        ):
            self.assertEqual(proxy_cfg.pick_proxy(), "socks5h://1.2.3.4:1080")
        fetch.assert_called_once_with()
        fallback.assert_not_called()

    def test_pick_proxy_fails_closed(self):
        with (
            patch.object(proxy_cfg, "PROXY_API_ENABLED", True),
            patch.object(proxy_cfg, "PROXY_API_FAIL_CLOSED", True),
            patch.object(proxy_cfg, "fetch_proxy_from_api", side_effect=RuntimeError("api down")),
            patch.object(proxy_cfg, "_pick_static_or_system_proxy", return_value="http://127.0.0.1:10808") as fallback,
        ):
            with self.assertRaisesRegex(RuntimeError, "api down"):
                proxy_cfg.pick_proxy()
        fallback.assert_not_called()

    def test_webui_exposes_proxy_api_fields(self):
        keys = {item["key"] for item in config_editor.EDITABLE_FIELDS}
        self.assertTrue({
            "PROXY_API_ENABLED",
            "PROXY_API_URL",
            "PROXY_API_PROFILES",
            "PROXY_API_ACTIVE",
            "PROXY_API_PROTOCOL",
            "PROXY_API_FAIL_CLOSED",
        }.issubset(keys))


if __name__ == "__main__":
    unittest.main()
