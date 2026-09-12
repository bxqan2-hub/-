# -*- coding: utf-8 -*-
import unittest

from core.chatgpt_plan import _format_plan_probe_errors, _format_plan_request_error


class PlanProbeErrorTests(unittest.TestCase):
    def test_formats_curl_timeout_as_actionable_proxy_error(self):
        error = _format_plan_request_error(
            TimeoutError("Failed to perform, curl: (28) Operation timed out after 8004 milliseconds"),
            12.0,
        )

        self.assertEqual(error, "套餐查询超时（12 秒）：专用代理节点响应过慢，请重试")

    def test_builtin_timeout_is_classified_without_leaking_detail(self):
        result = _format_plan_request_error(TimeoutError("private-proxy-password private-token"), 15)
        self.assertIn("15 秒", result)
        self.assertNotIn("private", result)

    def test_tls_handshake_failure_is_distinct_from_request_timeout(self):
        result = _format_plan_request_error(RuntimeError("curl: (35) private-proxy-password"), 15)
        self.assertIn("TLS", result)
        self.assertNotIn("超时", result)
        self.assertNotIn("private", result)

    def test_unknown_transport_exception_does_not_expose_credentials(self):
        result = _format_plan_request_error(ConnectionError("http://user:password@proxy.test token=private-token"), 15)
        self.assertIn("ConnectionError", result)
        self.assertNotIn("password", result)
        self.assertNotIn("private-token", result)

    def test_formats_real_probe_errors_with_sources(self):
        result = _format_plan_probe_errors([
            {"source": "subscriptions", "error": "forbidden", "http_status": 403},
            {"source": "wham_usage", "error": "ReadTimeout: timed out"},
            {"source": "me", "error": "HTTP 401", "http_status": 401},
        ])

        self.assertEqual(
            result,
            "subscriptions：HTTP 403 · forbidden；wham/usage：ReadTimeout: timed out；me：HTTP 401",
        )

    def test_deduplicates_retried_probe_errors(self):
        result = _format_plan_probe_errors([
            {"source": "wham_usage", "error": "HTTP 502", "http_status": 502},
            {"source": "wham_usage", "error": "HTTP 502", "http_status": 502},
        ])

        self.assertEqual(result, "wham/usage：HTTP 502")


if __name__ == "__main__":
    unittest.main()
