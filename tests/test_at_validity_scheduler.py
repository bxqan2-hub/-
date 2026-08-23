# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

import config
from core import at_validity, at_validity_scheduler, db, plan_check_service
from webui import config_editor
from webui.app import create_app


class AtValidityClassificationTests(unittest.TestCase):
    def test_valid_result_is_conclusive(self):
        result = at_validity.classify_plan_check_result({
            "ok": True,
            "checked_at": "2026-08-23T12:00:00",
            "http_status": 200,
        })

        self.assertEqual(result["outcome"], "valid")
        self.assertTrue(result["valid"])

    def test_expired_or_401_result_is_invalid_confirmed(self):
        expired = at_validity.classify_plan_check_result({
            "ok": False,
            "token_expired": True,
            "error": "AT已过期/失效",
        })
        unauthorized = at_validity.classify_plan_check_result({
            "ok": False,
            "http_status": 401,
            "error": "Unauthorized",
        })

        self.assertEqual(expired["outcome"], "invalid_confirmed")
        self.assertEqual(expired["error_code"], "token_expired")
        self.assertEqual(unauthorized["outcome"], "invalid_confirmed")
        self.assertEqual(unauthorized["error_code"], "http_401")

    def test_transport_or_rate_limit_result_remains_check_error(self):
        result = at_validity.classify_plan_check_result({
            "ok": False,
            "http_status": 429,
            "error": "HTTP 429",
            "retryable": True,
        })

        self.assertEqual(result["outcome"], "check_error")
        self.assertIsNone(result["valid"])


class AtValidityPersistenceAndFilterTests(unittest.TestCase):
    def test_filter_combines_confirmed_invalid_and_check_error(self):
        invalid = {"at_validity_status": "invalid_confirmed"}
        error = {"at_validity_status": "check_error"}
        valid = {"at_validity_status": "valid"}

        self.assertTrue(db._account_matches_at_validity_filter(invalid, "invalid-or-error"))
        self.assertTrue(db._account_matches_at_validity_filter(error, "invalid-or-error"))
        self.assertFalse(db._account_matches_at_validity_filter(valid, "invalid-or-error"))

    def test_validity_result_persists_without_erasing_account(self):
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
            }, trigger="scheduled-at")

        self.assertTrue(updated)
        row = captured["rows"][0]
        self.assertEqual(row["at_validity_status"], "invalid_confirmed")
        self.assertFalse(row["at_validity_valid"])
        self.assertEqual(row["at_validity_trigger"], "scheduled-at")


class AtValiditySchedulerTests(unittest.TestCase):
    @patch.object(plan_check_service, "enqueue_account_plan_check")
    @patch.object(at_validity_scheduler.db, "list_accounts")
    def test_scheduler_enqueues_only_accounts_with_tokens(self, list_accounts, enqueue):
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
        list_accounts.assert_called_once_with(limit=1_000_000, archived="0")
        self.assertEqual(enqueue.call_count, 2)

    @patch.object(plan_check_service, "check_account_plan", return_value={
        "ok": False,
        "checked_at": "2026-08-23T12:00:00",
        "http_status": 401,
        "token_expired": True,
        "error": "AT已过期/失效",
    })
    @patch.object(plan_check_service.detection_proxy, "resolve_static_detection_proxy", return_value="")
    @patch.object(plan_check_service.detection_proxy, "configured_detection_proxy_spec", return_value=None)
    @patch.object(plan_check_service.detection_proxy, "infer_timezone_offset_min", return_value="-")
    @patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True)
    @patch.object(plan_check_service.db, "update_account_plan_check")
    @patch.object(plan_check_service.db, "update_account_at_validity")
    @patch.object(plan_check_service, "_wait_for_rate_slot")
    def test_scheduled_probe_has_finite_retry_and_persists_invalid_outcome(
        self,
        _wait,
        update_validity,
        _update_plan,
        _mark,
        _timezone,
        _configured,
        _resolve,
        check,
    ):
        plan_check_service._QUEUE_SLOTS.acquire()
        result = plan_check_service._run_plan_check(
            account_id=9,
            email="nine@example.test",
            access_token="at-nine",
            trigger="scheduled-at",
            proxy=None,
            timezone_offset_min="-",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(check.call_args.kwargs["max_attempts"], 2)
        persisted = update_validity.call_args.args[1]
        self.assertEqual(persisted["outcome"], "invalid_confirmed")


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
            "next_run_at": "2026-08-23T13:30:00",
        }):
            response = self.client.get("/api/accounts/at-validity-schedule")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["interval_minutes"], 90)

    def test_schedule_update_validates_and_hot_reloads(self):
        invalid = self.client.post("/api/accounts/at-validity-schedule", json={
            "enabled": True,
            "interval_minutes": 0,
        })
        self.assertEqual(invalid.status_code, 400)

        with patch.object(config_editor, "update_config", return_value={"updated": ["AT_VALIDITY_CHECK_INTERVAL_MINUTES"]}) as update, \
             patch.object(config, "reload_all") as reload_all, \
             patch.object(at_validity_scheduler, "wakeup") as wakeup, \
             patch.object(at_validity_scheduler, "status", return_value={"enabled": False, "interval_minutes": 120}):
            response = self.client.post("/api/accounts/at-validity-schedule", json={
                "enabled": False,
                "interval_minutes": 120,
            })

        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with({
            "AT_VALIDITY_AUTO_CHECK_ENABLED": False,
            "AT_VALIDITY_CHECK_INTERVAL_MINUTES": 120,
        })
        reload_all.assert_called_once_with()
        wakeup.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
