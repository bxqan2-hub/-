# -*- coding: utf-8 -*-
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import config
from core import at_validity, at_validity_scheduler, at_validity_service, db
from webui import config_editor
from webui.app import create_app


class AtValidityProbeTests(unittest.TestCase):
    @staticmethod
    def _session(status_code: int):
        env = MagicMock()
        env.device_id = "device-test"
        env.navigator_language.return_value = "zh-CN"
        env._get_common_headers.return_value = {"user-agent": "test"}
        env.session.get.return_value = SimpleNamespace(status_code=status_code)
        return env

    def test_route_uses_at_dedicated_pool_before_local_vpn(self):
        with patch.object(
            at_validity.detection_proxy,
            "configured_detection_proxy_spec",
            return_value="JP|socks5h://at-pool.example:1080",
        ) as configured_spec, patch.object(
            at_validity.proxy_cfg,
            "detect_system_proxy",
            return_value="http://127.0.0.1:7890",
        ) as detect_system:
            route = at_validity._resolve_at_validity_route()

        configured_spec.assert_called_once_with("at")
        detect_system.assert_not_called()
        self.assertEqual(route["proxy"], "socks5h://at-pool.example:1080")
        self.assertEqual(route["proxy_source"], "at_validity_static_pool")

    def test_empty_at_pool_uses_system_proxy_and_never_reads_proxy_pool(self):
        with patch.object(
            at_validity.detection_proxy,
            "configured_detection_proxy_spec",
            return_value=None,
        ) as configured_spec, patch.object(
            at_validity.proxy_cfg, "detect_system_proxy", return_value="http://127.0.0.1:7890"
        ) as detect_system, patch.object(
            at_validity.proxy_cfg, "pick_local_proxy", side_effect=AssertionError("PROXY_POOL must not be read")
        ) as pick_pool:
            route = at_validity._resolve_at_validity_route()

        configured_spec.assert_called_once_with("at")
        detect_system.assert_called_once_with()
        pick_pool.assert_not_called()
        self.assertEqual(route["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(route["proxy_source"], "local_vpn_system_proxy")

    def test_empty_at_pool_without_system_proxy_uses_local_vpn_tun(self):
        with patch.object(
            at_validity.detection_proxy,
            "configured_detection_proxy_spec",
            return_value=None,
        ), patch.object(
            at_validity.proxy_cfg, "detect_system_proxy", return_value=""
        ), patch.object(
            at_validity.proxy_cfg, "pick_local_proxy", side_effect=AssertionError("PROXY_POOL must not be read")
        ) as pick_pool:
            route = at_validity._resolve_at_validity_route()

        pick_pool.assert_not_called()
        self.assertEqual(route["proxy"], "")
        self.assertEqual(route["proxy_source"], "local_vpn_tun")
        self.assertEqual(route["network_route"], "local_vpn")

    @patch.object(at_validity, "BrowserSession")
    @patch.object(at_validity, "token_claims", return_value={"token_expired": True})
    def test_expired_jwt_is_invalid_without_network(self, _claims, session_cls):
        result = at_validity.check_access_token_validity("expired-token")

        self.assertEqual(result["outcome"], "invalid_confirmed")
        self.assertEqual(result["error_code"], "token_expired")
        session_cls.assert_not_called()

    @patch.object(at_validity, "_resolve_at_validity_route", return_value={
        "proxy": "http://127.0.0.1:7890",
        "network_route": "proxy",
        "proxy_used": "http://127.0.0.1:7890",
        "proxy_source": "configured",
    })
    @patch.object(at_validity, "BrowserSession")
    @patch.object(at_validity, "token_claims", return_value={"token_expired": False})
    def test_http_200_is_valid_and_uses_local_proxy(self, _claims, session_cls, _route):
        env = self._session(200)
        session_cls.return_value = env

        result = at_validity.check_access_token_validity("at-local", max_attempts=1)

        self.assertEqual(result["outcome"], "valid")
        self.assertTrue(result["valid"])
        self.assertEqual(result["network_route"], "proxy")
        self.assertEqual(result["proxy_used"], "http://127.0.0.1:7890")
        session_cls.assert_called_once_with(proxy="http://127.0.0.1:7890", detect_exit_geo=False)
        request = env.session.get.call_args
        self.assertEqual(request.args[0], "https://chatgpt.com/backend-api/me")
        self.assertEqual(request.kwargs["headers"]["authorization"], "Bearer at-local")
        env.close.assert_called_once_with()

    @patch.object(at_validity, "_resolve_at_validity_route", return_value={
        "proxy": "", "network_route": "direct", "proxy_used": None,
    })
    @patch.object(at_validity, "BrowserSession")
    @patch.object(at_validity, "token_claims", return_value={"token_expired": False})
    def test_http_401_is_confirmed_invalid(self, _claims, session_cls, _route):
        session_cls.return_value = self._session(401)

        result = at_validity.check_access_token_validity("at-401", max_attempts=1)

        self.assertEqual(result["outcome"], "invalid_confirmed")
        self.assertEqual(result["error_code"], "http_401")

    @patch.object(at_validity.time, "sleep")
    @patch.object(at_validity, "_resolve_at_validity_route", return_value={
        "proxy": "", "network_route": "direct", "proxy_used": None,
    })
    @patch.object(at_validity, "BrowserSession")
    @patch.object(at_validity, "token_claims", return_value={"token_expired": False})
    def test_rate_limit_remains_check_error(self, _claims, session_cls, _route, _sleep):
        session_cls.return_value = self._session(429)

        result = at_validity.check_access_token_validity("at-429", max_attempts=2)

        self.assertEqual(result["outcome"], "check_error")
        self.assertIsNone(result["valid"])
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["attempt_count"], 2)

    @patch.object(at_validity.time, "sleep")
    @patch.object(at_validity, "_resolve_at_validity_route", return_value={
        "proxy": "", "network_route": "local_vpn", "proxy_used": None,
        "proxy_source": "local_vpn_tun",
    })
    @patch.object(at_validity, "BrowserSession")
    @patch.object(at_validity, "token_claims", return_value={"token_expired": False})
    def test_network_tls_timeout_and_server_errors_retry_with_fresh_sessions(
        self, _claims, session_cls, resolve_route, sleep
    ):
        tls_error = self._session(0)
        tls_error.session.get.side_effect = RuntimeError("BoringSSL SSL_connect closed")
        timeout_error = self._session(0)
        timeout_error.session.get.side_effect = TimeoutError("timed out")
        server_error = self._session(503)
        success = self._session(200)
        session_cls.side_effect = [tls_error, timeout_error, server_error, success]

        result = at_validity.check_access_token_validity(
            "at-retry",
            max_attempts=5,
            retry_delay=1.0,
        )

        self.assertEqual(result["outcome"], "valid")
        self.assertEqual(result["attempt_count"], 4)
        self.assertEqual(session_cls.call_count, 4)
        self.assertEqual(resolve_route.call_count, 4)
        sleep.assert_has_calls([unittest.mock.call(1.0), unittest.mock.call(2.0), unittest.mock.call(4.0)])
        for env in (tls_error, timeout_error, server_error, success):
            env.close.assert_called_once_with()

    @patch.object(at_validity.time, "sleep")
    @patch.object(at_validity, "_resolve_at_validity_route", return_value={
        "proxy": "", "network_route": "local_vpn", "proxy_used": None,
        "proxy_source": "local_vpn_tun",
    })
    @patch.object(at_validity, "BrowserSession")
    @patch.object(at_validity, "token_claims", return_value={"token_expired": False})
    def test_network_error_exhausts_five_attempts_without_marking_at_invalid(
        self, _claims, session_cls, _route, sleep
    ):
        sessions = [self._session(0) for _ in range(5)]
        for env in sessions:
            env.session.get.side_effect = TimeoutError("timed out")
        session_cls.side_effect = sessions

        result = at_validity.check_access_token_validity(
            "at-still-unknown",
            max_attempts=5,
            retry_delay=0.5,
        )

        self.assertEqual(result["outcome"], "check_error")
        self.assertIsNone(result["valid"])
        self.assertEqual(result["attempt_count"], 5)
        self.assertEqual(session_cls.call_count, 5)
        sleep.assert_has_calls([
            unittest.mock.call(0.5), unittest.mock.call(1.0),
            unittest.mock.call(2.0), unittest.mock.call(4.0),
        ])

    @patch.object(at_validity, "_resolve_at_validity_route", side_effect=ValueError("动态代理被拒绝"))
    @patch.object(at_validity, "BrowserSession")
    @patch.object(at_validity, "token_claims", return_value={"token_expired": False})
    def test_proxy_configuration_error_is_not_invalid(self, _claims, session_cls, _route):
        result = at_validity.check_access_token_validity("at-proxy")

        self.assertEqual(result["outcome"], "check_error")
        self.assertEqual(result["error_code"], "proxy_config_error")
        session_cls.assert_not_called()


class AtValidityPersistenceAndFilterTests(unittest.TestCase):
    def test_filter_combines_confirmed_invalid_and_check_error(self):
        invalid = {"at_validity_status": "invalid_confirmed"}
        error = {"at_validity_status": "check_error"}
        valid = {"at_validity_status": "valid"}

        self.assertTrue(db._account_matches_at_validity_filter(invalid, "invalid-or-error"))
        self.assertTrue(db._account_matches_at_validity_filter(error, "invalid-or-error"))
        self.assertFalse(db._account_matches_at_validity_filter(valid, "invalid-or-error"))

    def test_validity_result_persists_route_without_erasing_account(self):
        captured = {}

        def save(rows):
            captured["rows"] = json.loads(json.dumps(rows))

        with patch.object(db, "_load_accounts", return_value=[{"id": 7, "email": "one@example.test"}]), \
             patch.object(db, "_save_accounts", side_effect=save):
            updated = db.update_account_at_validity(7, {
                "outcome": "invalid_confirmed",
                "valid": False,
                "checked_at": "2026-08-23T12:00:00",
                "http_status": 401,
                "error_code": "http_401",
                "error": "AT 已失效",
                "network_route": "proxy",
                "proxy_used": "http://127.0.0.1:7890",
                "proxy_source": "configured",
                "attempt_count": 1,
            }, trigger="scheduled-at")

        self.assertTrue(updated)
        row = captured["rows"][0]
        self.assertEqual(row["at_validity_status"], "invalid_confirmed")
        self.assertFalse(row["at_validity_valid"])
        self.assertEqual(row["at_validity_trigger"], "scheduled-at")
        self.assertEqual(row["at_validity_proxy_used"], "http://127.0.0.1:7890")
        self.assertEqual(row["at_validity_attempt_count"], 1)


class AtValidityServiceAndSchedulerTests(unittest.TestCase):
    @patch.object(at_validity_scheduler.at_validity_service, "enqueue_account_at_validity_check")
    @patch.object(at_validity_scheduler.db, "list_accounts")
    def test_scheduler_enqueues_only_accounts_with_tokens_in_at_queue(self, list_accounts, enqueue):
        list_accounts.return_value = [
            {"id": 1, "email": "one@example.test", "access_token": "at-one"},
            {"id": 2, "email": "two@example.test", "access_token": ""},
            {"id": 3, "email": "three@example.test", "access_token": "at-three"},
        ]
        enqueue.side_effect = [
            {"accepted": True, "busy": False},
            {"accepted": False, "busy": True},
        ]

        result = at_validity_scheduler.enqueue_accounts(trigger="scheduled-at")

        self.assertEqual(result["started_count"], 1)
        self.assertEqual(result["busy_count"], 1)
        self.assertEqual(result["skipped_no_token_count"], 1)
        self.assertEqual(result["skipped_recheck_count"], 0)
        list_accounts.assert_called_once_with(limit=1_000_000, archived="0")
        self.assertEqual(enqueue.call_count, 2)

    @patch.object(at_validity_scheduler.at_validity_service, "enqueue_account_at_validity_check")
    @patch.object(at_validity_scheduler.db, "list_accounts")
    def test_scheduler_skips_recently_checked_accounts_until_recheck_interval(self, list_accounts, enqueue):
        list_accounts.return_value = [
            {
                "id": 1,
                "email": "recent@example.test",
                "access_token": "at-recent",
                "at_validity_checked_at": (datetime.now() - timedelta(minutes=30)).isoformat(),
            },
            {"id": 2, "email": "new@example.test", "access_token": "at-new"},
        ]
        enqueue.return_value = {"accepted": True, "busy": False}

        with patch.object(
            at_validity_scheduler.validity_cfg,
            "AT_VALIDITY_RECHECK_INTERVAL_MINUTES",
            1_440,
            create=True,
        ):
            result = at_validity_scheduler.enqueue_accounts(trigger="scheduled-at")

        self.assertEqual(result["started_count"], 1)
        self.assertEqual(result["skipped_recheck_count"], 1)
        self.assertEqual(enqueue.call_args.kwargs["account_id"], 2)

    @patch.object(at_validity_scheduler.at_validity_service, "enqueue_account_at_validity_check")
    @patch.object(at_validity_scheduler.db, "list_accounts")
    def test_manual_check_keeps_force_all_behavior(self, list_accounts, enqueue):
        list_accounts.return_value = [{
            "id": 1,
            "email": "recent@example.test",
            "access_token": "at-recent",
            "at_validity_checked_at": datetime.now().isoformat(),
        }]
        enqueue.return_value = {"accepted": True, "busy": False}

        result = at_validity_scheduler.enqueue_accounts(trigger="manual-at")

        self.assertEqual(result["started_count"], 1)
        self.assertEqual(result["skipped_recheck_count"], 0)
        enqueue.assert_called_once()

    @patch.object(at_validity_service, "_wait_for_rate_slot")
    @patch.object(at_validity_service, "check_access_token_validity", return_value={
        "outcome": "valid", "valid": True, "http_status": 200,
    })
    @patch.object(at_validity_service.db, "update_account_plan_check")
    @patch.object(at_validity_service.db, "update_account_at_validity")
    def test_worker_updates_only_at_validity_fields(self, update_validity, update_plan, check, _wait):
        at_validity_service._QUEUE_SLOTS.acquire()
        with at_validity_service._RUNNING_LOCK:
            at_validity_service._RUNNING.add(9)

        result = at_validity_service._run_at_validity_check(
            account_id=9,
            email="nine@example.test",
            access_token="at-nine",
            trigger="scheduled-at",
            proxy=None,
        )

        self.assertEqual(result["outcome"], "valid")
        check.assert_called_once()
        update_validity.assert_called_once_with(9, result, trigger="scheduled-at")
        update_plan.assert_not_called()

    def test_source_has_no_plan_or_trial_detection_dependency(self):
        project = Path(__file__).resolve().parents[1]
        scheduler_source = (project / "core" / "at_validity_scheduler.py").read_text(encoding="utf-8")
        probe_source = (project / "core" / "at_validity.py").read_text(encoding="utf-8")
        plan_service_source = (project / "core" / "plan_check_service.py").read_text(encoding="utf-8")

        self.assertNotIn("plan_check_service", scheduler_source)
        self.assertNotIn("check_account_plan", probe_source)
        self.assertNotIn("plus_trial_eligible", probe_source)
        self.assertNotIn("pick_local_proxy", probe_source)
        self.assertNotIn("proxy_cfg.PROXY_POOL", probe_source)
        self.assertIn("detect_system_proxy", probe_source)
        self.assertNotIn("update_account_at_validity", plan_service_source)


class AtValidityWebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(at_validity_scheduler, "ensure_started"):
            cls.client = create_app(auth_code="test-auth").test_client()
        cls.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_schedule_get_returns_runtime_status(self):
        with patch.object(at_validity_scheduler, "status", return_value={
            "enabled": True,
            "interval_minutes": 90,
            "recheck_interval_minutes": 1_440,
            "next_run_at": "2026-08-23T13:30:00",
        }):
            response = self.client.get("/api/accounts/at-validity-schedule")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["interval_minutes"], 90)
        self.assertEqual(response.get_json()["recheck_interval_minutes"], 1_440)

    def test_schedule_update_validates_and_hot_reloads(self):
        invalid = self.client.post("/api/accounts/at-validity-schedule", json={
            "enabled": True,
            "interval_minutes": 0,
        })
        self.assertEqual(invalid.status_code, 400)

        invalid_recheck = self.client.post("/api/accounts/at-validity-schedule", json={
            "enabled": True,
            "interval_minutes": 60,
            "recheck_interval_minutes": 0,
        })
        self.assertEqual(invalid_recheck.status_code, 400)

        with patch.object(config_editor, "update_config", return_value={"updated": ["AT_VALIDITY_CHECK_INTERVAL_MINUTES", "AT_VALIDITY_RECHECK_INTERVAL_MINUTES"]}) as update, \
             patch.object(config, "reload_all") as reload_all, \
             patch.object(at_validity_scheduler, "wakeup") as wakeup, \
             patch.object(at_validity_scheduler, "status", return_value={"enabled": False, "interval_minutes": 120, "recheck_interval_minutes": 1_440}):
            response = self.client.post("/api/accounts/at-validity-schedule", json={
                "enabled": False,
                "interval_minutes": 120,
                "recheck_interval_minutes": 1_440,
            })

        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with({
            "AT_VALIDITY_AUTO_CHECK_ENABLED": False,
            "AT_VALIDITY_CHECK_INTERVAL_MINUTES": 120,
            "AT_VALIDITY_RECHECK_INTERVAL_MINUTES": 1_440,
        })
        reload_all.assert_called_once_with()
        wakeup.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
