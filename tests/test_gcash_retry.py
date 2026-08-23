# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from config import proxy as proxy_cfg
from core import db, detection_proxy
from core import gcash_service
from webui.app import create_app


class GcashProxyRetryTests(unittest.TestCase):
    def test_should_not_retry_on_success_or_eligible(self):
        self.assertFalse(gcash_service._should_retry_with_next_proxy(
            {"ok": True, "gcash": True}))
        self.assertFalse(gcash_service._should_retry_with_next_proxy(
            {"ok": True, "gcash": False,
             "detection_outcome": "no_cpmt_after_full_probe"}))
        self.assertFalse(gcash_service._should_retry_with_next_proxy(
            {"ok": True, "gcash": False,
             "detection_outcome": "no_gcash_in_create_response"}))

    def test_should_retry_on_proxy_ssl_or_risk_control_failure(self):
        for error in [
            "SSLError: Failed to perform, curl: (35)",
            "ProxyError: could not connect to proxy",
            "OpenAI Checkout HTTP 400: unusual activity",
            "OpenAI Checkout HTTP 429: rate limit exceeded",
            "Sentinel token generation failed after fresh-session retry",
            "Connection reset by peer",
        ]:
            self.assertTrue(gcash_service._should_retry_with_next_proxy(
                {"ok": False, "gcash": False, "error": error}),
                msg=error)

    def test_http_proxy_transport_falls_back_to_https_for_tunnel_failure(self):
        runtime = MagicMock()
        runtime.detect_gcash.side_effect = [
            ({"ok": False, "gcash": False, "error": "CONNECT tunnel failed"}, 502),
            ({"ok": True, "gcash": True, "custom_payment_method_id": "cpmt_gcash"}, 200),
        ]

        with patch.object(gcash_service, "get_pay153_module", return_value=runtime):
            result = gcash_service.check_gcash(
                "token",
                proxy="http://user:pass@proxy.example:8080",
            )

        calls = [call.args[0]["proxy"] for call in runtime.detect_gcash.call_args_list]
        self.assertEqual(calls, [
            "http://user:pass@proxy.example:8080",
            "https://user:pass@proxy.example:8080",
        ])
        self.assertTrue(result["gcash"])
        self.assertEqual(result["proxy_transport"], "https")
        self.assertEqual(result["transport_attempt_count"], 2)

    def test_rate_limit_retry_uses_bounded_backoff(self):
        calls = []

        def fake_check(token, proxy=None):
            calls.append(proxy)
            if len(calls) == 1:
                return {
                    "ok": False,
                    "gcash": False,
                    "upstream_http_status": 429,
                    "error": "OpenAI Checkout HTTP 429: rate limit exceeded",
                }
            return {"ok": True, "gcash": True}

        with patch.object(gcash_service, "check_gcash", side_effect=fake_check), \
             patch.object(gcash_service.db, "update_account_gcash", return_value=True), \
             patch.object(gcash_service.time, "sleep") as sleep:
            result = gcash_service._run_with_proxy_retry(
                account_id=6,
                access_token="tok",
                proxies=["p1", "p2"],
                max_retries=None,
            )

        self.assertTrue(result["gcash"])
        sleep.assert_called_once_with(0.75)

    def test_queue_defaults_use_raised_direct_checkout_concurrency(self):
        settings = gcash_service.queue_settings()
        self.assertEqual(settings["default_workers"], 8)
        self.assertEqual(settings["max_workers"], 32)

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

    def test_total_timeout_watchdog_aborts_retry_loop(self):
        calls = []

        def fake_check(token, proxy=None):
            calls.append(proxy)
            return {"ok": False, "gcash": False,
                    "error": "SSLError: Failed to perform"}

        with patch.object(gcash_service, "check_gcash", side_effect=fake_check), \
             patch.object(gcash_service.db, "update_account_gcash", return_value=True), \
             patch.object(gcash_service.time, "monotonic", side_effect=[0.0] * 4 + [999.0]):
            # deadline = 0 + 2 = 2；前 3 次调用都 < 2，第 4 次触发超时中止。
            result = gcash_service._run_with_proxy_retry(
                account_id=5, access_token="tok",
                proxies=["p1", "p2", "p3", "p4", "p5", "p6"],
                max_retries=None,
                total_timeout=2.0,
            )

        self.assertEqual(len(calls), 3)
        self.assertIn("总耗时超过", result["error"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempt_count"], 3)

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


class GcashBulkApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_bulk_api_defaults_to_eight_workers(self):
        profiles = [f"PH|http://proxy-{index}.example:8080" for index in range(8)]
        with patch.object(proxy_cfg, "GC_CHECK_PROXY_PROFILES", profiles), \
             patch.object(
                 detection_proxy,
                 "resolve_detection_proxy",
                 side_effect=lambda spec: spec.split("|", 1)[-1],
             ), patch("webui.app.db.get_account", side_effect=lambda account_id: {
                 "id": account_id,
                 "email": f"user-{account_id}@example.test",
                 "access_token": "token",
             }), patch(
                 "webui.app.gcash_service.get_executor",
                 return_value=MagicMock(),
             ) as get_executor, patch(
                 "webui.app.gcash_service.enqueue",
                 return_value={"accepted": True, "busy": False},
             ) as enqueue:
            response = self.client.post("/api/accounts/check-gcash-bulk", json={
                "account_ids": list(range(1, 11)),
            })

        self.assertEqual(response.status_code, 202)
        response_data = response.get_json()
        self.assertEqual(response_data["workers"], 8)
        self.assertIn("只创建一次 PH/PHP Checkout", response_data["message"])
        self.assertIn("不识别 OAICS 类型", response_data["message"])
        get_executor.assert_called_once_with(8)
        self.assertEqual(enqueue.call_count, 10)


class GcashPersistenceTests(unittest.TestCase):
    def test_gcash_result_never_overwrites_checkout_kind_fields(self):
        row = {
            "id": 7,
            "checkout_kind_status": "success",
            "checkout_kind": "cs_live",
            "checkout_kind_provider": "stripe",
        }
        with patch.object(db, "_load_accounts", return_value=[row]), \
             patch.object(db, "_save_accounts") as save_accounts:
            updated = db.update_account_gcash(7, {
                "ok": True,
                "gcash": True,
                # Even a legacy caller-provided kind must be ignored here.
                "kind": "oaics",
                "checkout_provider": "open_ai",
                "checkout_country": "PH",
                "checkout_currency": "PHP",
            })

        self.assertTrue(updated)
        self.assertEqual(row["checkout_kind"], "cs_live")
        self.assertEqual(row["checkout_kind_provider"], "stripe")
        save_accounts.assert_called_once()


if __name__ == "__main__":
    unittest.main()
