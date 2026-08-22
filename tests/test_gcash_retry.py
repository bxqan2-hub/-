# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import gcash_service


class GcashProxyRetryTests(unittest.TestCase):
    def test_should_not_retry_on_success_or_eligible(self):
        self.assertFalse(gcash_service._should_retry_with_next_proxy(
            {"ok": True, "gcash": True}))
        self.assertFalse(gcash_service._should_retry_with_next_proxy(
            {"ok": True, "gcash": False,
             "detection_outcome": "no_cpmt_after_full_probe"}))

    def test_should_retry_on_proxy_ssl_or_risk_control_failure(self):
        for error in [
            "SSLError: Failed to perform, curl: (35)",
            "ProxyError: could not connect to proxy",
            "OpenAI Checkout HTTP 400: unusual activity",
            "Connection reset by peer",
        ]:
            self.assertTrue(gcash_service._should_retry_with_next_proxy(
                {"ok": False, "gcash": False, "error": error}),
                msg=error)

    def test_retry_switches_proxy_until_success(self):
        calls = []

        def fake_check(token, proxy=None):
            calls.append(proxy)
            # 前两条代理失败（风控/SSL），第三条成功。
            if len(calls) == 1:
                return {"ok": False, "gcash": False,
                        "error": "SSLError: Failed to perform"}
            if len(calls) == 2:
                return {"ok": False, "gcash": False,
                        "error": "OpenAI Checkout HTTP 400: unusual activity"}
            return {"ok": True, "gcash": True, "custom_payment_method_id": "cpmt_abc"}

        with patch.object(gcash_service, "check_gcash", side_effect=fake_check), \
             patch.object(gcash_service.db, "update_account_gcash", return_value=True):
            result = gcash_service._run_with_proxy_retry(
                account_id=1, access_token="tok",
                proxies=["p1", "p2", "p3"],
                max_retries=gcash_service.MAX_PROXY_RETRIES,
            )

        self.assertEqual(calls, ["p1", "p2", "p3"])
        self.assertTrue(result["gcash"])
        self.assertEqual(result["attempt_count"], 3)
        self.assertEqual(result["retried_proxies"], ["p1", "p2", "p3"])

    def test_retry_exhausts_pool_and_marks_failure_with_attempt_count(self):
        def fake_check(token, proxy=None):
            return {"ok": False, "gcash": False,
                    "error": "ProxyError: connect failed"}

        with patch.object(gcash_service, "check_gcash", side_effect=fake_check), \
             patch.object(gcash_service.db, "update_account_gcash", return_value=True):
            result = gcash_service._run_with_proxy_retry(
                account_id=2, access_token="tok",
                proxies=["p1", "p2", "p3"],
                max_retries=3,
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["gcash"])
        self.assertEqual(result["attempt_count"], 3)
        self.assertIn("已尝试 3 个代理仍失败", result["error"])

    def test_unlimited_walks_entire_pool_until_result(self):
        calls = []

        def fake_check(token, proxy=None):
            calls.append(proxy)
            # 只有最后一条代理成功，其余全失败 → 应遍历整个池子一次。
            if len(calls) < 100:
                return {"ok": False, "gcash": False,
                        "error": "SSLError: Failed to perform"}
            return {"ok": True, "gcash": True, "custom_payment_method_id": "cpmt_end"}

        pool = [f"p{i}" for i in range(100)]
        with patch.object(gcash_service, "check_gcash", side_effect=fake_check), \
             patch.object(gcash_service.db, "update_account_gcash", return_value=True):
            result = gcash_service._run_with_proxy_retry(
                account_id=4, access_token="tok",
                proxies=pool,
                max_retries=None,  # 无上限 → 遍历整个池
            )

        self.assertEqual(len(calls), 100)
        self.assertEqual(calls, pool)
        self.assertTrue(result["gcash"])
        self.assertEqual(result["attempt_count"], 100)
        self.assertEqual(len(result["retried_proxies"]), 100)

    def test_no_eligible_does_not_retry_even_with_proxy_pool(self):
        calls = []

        def fake_check(token, proxy=None):
            calls.append(proxy)
            return {"ok": True, "gcash": False,
                    "detection_outcome": "no_cpmt_after_full_probe"}

        with patch.object(gcash_service, "check_gcash", side_effect=fake_check), \
             patch.object(gcash_service.db, "update_account_gcash", return_value=True):
            result = gcash_service._run_with_proxy_retry(
                account_id=3, access_token="tok",
                proxies=["p1", "p2", "p3"],
                max_retries=3,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls, ["p1"])
        self.assertFalse(result["gcash"])
        self.assertNotIn("已尝试", result.get("error") or "")


if __name__ == "__main__":
    unittest.main()