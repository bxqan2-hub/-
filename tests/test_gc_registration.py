# -*- coding: utf-8 -*-
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import gc_registration_service as gc
from core import db
from core import plan_check_service
from core.chatgpt_plan import parse_accounts_check
from core.roxybrowser_client import RoxyBrowserClient


class GcRegistrationTests(unittest.TestCase):
    def tearDown(self):
        with gc._POLL_LOCK:
            for event in gc._POLL_EVENTS.values():
                event.set()
            gc._POLL_EVENTS.clear()

    def test_accounts_check_marks_promo_only_capability(self):
        result = parse_accounts_check({
            "accounts": {
                "default": {
                    "account": {"account_id": "acct", "plan_type": "free"},
                    "entitlement": {},
                    "eligible_promo_campaigns": {"plus": {"id": "trial"}},
                }
            }
        })
        self.assertFalse(result["subscription_state_available"])
        self.assertEqual(result["plan_detection_capability"], "promo_only")

    def test_gc_group_is_idempotent_and_accepts_successful_account(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            groups_path = root / "groups.json"
            accounts_path.write_text('[{"id":7,"email":"gc@example.com"}]', encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts_path), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"), \
                 patch.object(db, "_ACCOUNT_GROUPS_JSON", groups_path):
                first = db.ensure_account_group("GC")
                second = db.ensure_account_group("gc")
                self.assertEqual(first["id"], second["id"])
                group, skipped = db.add_accounts_to_group(first["id"], [7])
                self.assertEqual(skipped, [])
                self.assertEqual(group["account_ids"], [7])
                self.assertEqual(group["emails"], ["gc@example.com"])
                other = db.create_account_group("Paid")
                moved, skipped = db.add_accounts_to_group(other["id"], [7])
                self.assertEqual(skipped, [])
                self.assertEqual(moved["emails"], ["gc@example.com"])
                groups = {item["name"]: item for item in db.list_account_groups()}
                self.assertEqual(groups["GC"]["count"], 0)
                self.assertEqual(groups["Paid"]["count"], 1)

                default_group, skipped = db.move_accounts_to_default_group([7])
                self.assertEqual(skipped, [])
                self.assertEqual(default_group["id"], "default")
                self.assertEqual(default_group["count"], 1)
                groups = {item["name"]: item for item in db.list_account_groups()}
                self.assertEqual(groups["Paid"]["count"], 0)

    @patch("core.gc_registration_service.start_plan_poll")
    @patch("core.gc_registration_service.db.list_jobs")
    def test_start_all_only_targets_open_gc_windows(self, list_jobs, start_poll):
        list_jobs.return_value = [
            {"id": 1, "gc_mode": True, "gc_window_state": "open"},
            {"id": 2, "gc_mode": True, "gc_window_state": "deleted"},
            {"id": 3, "gc_mode": False, "gc_window_state": "open"},
        ]
        start_poll.return_value = {"ok": True, "started": True}
        result = gc.start_all_plan_polls()
        self.assertEqual(result["started"], [1])
        start_poll.assert_called_once_with(1)

    def test_open_profile_explicit_headless_override(self):
        client = RoxyBrowserClient()
        with patch("core.roxybrowser_client._cfg.ROXY_OPEN_HEADLESS", True), \
             patch("core.roxybrowser_client._cfg.ROXY_PROFILE_ID", ""), \
             patch.object(client, "create_profile", return_value="visible-profile"), \
             patch.object(client, "request", return_value={"debuggerAddress": "127.0.0.1:9222"}) as request:
            client.open_profile(headless=False)
        self.assertFalse(request.call_args.kwargs["json_body"]["headless"])

    @patch("core.gc_registration_service.close_plus_window_for_account")
    @patch(
        "core.gc_registration_service._mailbox_plus_fallback",
        return_value={"status": "plus", "plan_promoted": True},
    )
    @patch("core.plan_check_service.db.get_account", return_value={"id": 31, "email": "paid@example.com"})
    @patch("core.plan_check_service.db.update_account_plan_check")
    @patch("core.plan_check_service.db.mark_account_plan_check_running", return_value=True)
    @patch("core.plan_check_service._wait_for_rate_slot")
    @patch("core.plan_check_service.check_account_plan")
    def test_account_mail_plus_closes_only_bound_gc_window(
        self, check_plan, _wait, _mark, _update, _get_account, _mail, close_window
    ):
        check_plan.return_value = {
            "ok": True,
            "has_active_plus_subscription": True,
            "current_plan_type": "plus",
        }
        close_window.return_value = {"ok": True, "closed": True, "account_id": 31}
        plan_check_service._QUEUE_SLOTS.acquire()
        result = plan_check_service._run_plan_check(
            account_id=31,
            email="paid@example.com",
            access_token="account-at",
            trigger="manual",
            proxy="",
            timezone_offset_min="-",
        )
        self.assertTrue(result["has_active_plus_subscription"])
        _mail.assert_not_called()
        close_window.assert_called_once_with(31)

    @patch("core.gc_registration_service.close_plus_window_for_account")
    @patch("core.gc_registration_service._mailbox_plus_fallback")
    @patch("core.plan_check_service.db.get_account")
    @patch("core.plan_check_service.db.update_account_plan_check")
    @patch("core.plan_check_service.db.mark_account_plan_check_running", return_value=True)
    @patch("core.plan_check_service._wait_for_rate_slot")
    @patch("core.plan_check_service.check_account_plan")
    def test_account_at_401_stays_separate_from_mailbox_detection(
        self, check_plan, _wait, _mark, _update, get_account, mail_fallback, close_window
    ):
        check_plan.return_value = {
            "ok": False, "http_status": 401, "token_expired": True,
            "error": "AT已过期/失效", "account_unusable_code": None,
        }
        get_account.return_value = {"id": 32, "email": "paid@example.com"}
        mail_fallback.return_value = {"status": "plus", "plan_promoted": True}
        close_window.return_value = {"ok": True, "closed": True}
        plan_check_service._QUEUE_SLOTS.acquire()

        result = plan_check_service._run_plan_check(
            account_id=32, email="paid@example.com", access_token="at",
            trigger="manual", proxy="", timezone_offset_min="-",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 401)
        get_account.assert_not_called()
        mail_fallback.assert_not_called()
        close_window.assert_not_called()

    @patch("core.gc_registration_service.db.list_jobs")
    @patch("core.gc_registration_service.db.get_job")
    @patch("core.gc_registration_service.RoxyBrowserClient")
    def test_close_targets_only_profile_bound_to_job(self, client_cls, get_job, list_jobs):
        get_job.return_value = {
            "id": 12, "gc_mode": True, "roxy_profile_id": "profile-12",
            "gc_window_state": "open", "status": "gc_waiting",
        }
        list_jobs.return_value = [get_job.return_value]
        client = client_cls.return_value
        client.close_profile.return_value = True
        client.delete_profile.return_value = True
        with patch("core.gc_registration_service.db.update_job"):
            result = gc.close_job_window(12)
        self.assertTrue(result["ok"])
        client.close_profile.assert_called_once_with("profile-12")
        client.delete_profile.assert_called_once_with("profile-12")

    @patch("core.gc_registration_service.db.list_jobs")
    @patch("core.gc_registration_service.db.get_job")
    @patch("core.gc_registration_service.RoxyBrowserClient")
    def test_duplicate_open_profile_is_refused(self, client_cls, get_job, list_jobs):
        target = {"id": 12, "gc_mode": True, "roxy_profile_id": "same", "gc_window_state": "open"}
        get_job.return_value = target
        list_jobs.return_value = [target, {"id": 13, "roxy_profile_id": "same", "gc_window_state": "open"}]
        result = gc.close_job_window(12)
        self.assertFalse(result["ok"])
        self.assertIn("安全校验失败", result["error"])
        client_cls.assert_not_called()

    @patch("core.gc_registration_service.close_job_window")
    @patch(
        "core.gc_registration_service._mailbox_plus_fallback",
        return_value={"status": "plus", "plan_promoted": True},
    )
    @patch("core.gc_registration_service.check_account_plan")
    @patch("core.gc_registration_service.db.update_account_plan_check")
    @patch("core.gc_registration_service.db.update_job")
    @patch("core.gc_registration_service.db.get_account")
    @patch("core.gc_registration_service.db.get_job")
    def test_plus_result_auto_closes_same_job(
        self, get_job, get_account, update_job, update_plan, check_plan, mailbox, close_window
    ):
        get_job.return_value = {
            "id": 21, "gc_mode": True, "gc_window_state": "open",
            "account_id": 7, "email": "plus@example.com",
        }
        get_account.return_value = {"id": 7, "email": "plus@example.com", "access_token": "at-value"}
        check_plan.return_value = {"ok": True, "has_active_plus_subscription": True, "checked_at": "now"}
        gc._poll(21, threading.Event())
        check_plan.assert_called_once()
        self.assertEqual(check_plan.call_args.args, ("at-value",))
        self.assertEqual(check_plan.call_args.kwargs["max_attempts"], 0)
        self.assertTrue(callable(check_plan.call_args.kwargs["continue_check"]))
        mailbox.assert_called_once()
        update_plan.assert_called_once()
        close_window.assert_called_once_with(21, reason="plus")

    @patch("core.gc_registration_service.close_job_window")
    @patch("core.gc_registration_service.db.update_account_plan_check")
    @patch("core.gc_registration_service.db.update_job")
    @patch("core.gc_registration_service.db.get_account")
    @patch("core.gc_registration_service.db.get_job")
    def test_stop_during_request_prevents_late_plus_auto_close(
        self, get_job, get_account, update_job, update_plan, close_window
    ):
        stop_event = threading.Event()
        get_job.return_value = {
            "id": 22, "gc_mode": True, "gc_window_state": "open",
            "account_id": 8, "email": "late@example.com",
        }
        get_account.return_value = {"id": 8, "email": "late@example.com", "access_token": "at-late"}

        def late_result(_token, **_kwargs):
            stop_event.set()
            return {"ok": True, "has_active_plus_subscription": True, "checked_at": "now"}

        with patch("core.gc_registration_service.check_account_plan", side_effect=late_result):
            gc._poll(22, stop_event)
        update_plan.assert_called_once()
        close_window.assert_not_called()

    @patch("core.gc_registration_service.close_job_window")
    @patch("core.gc_registration_service._mailbox_plus_fallback")
    @patch("core.gc_registration_service.db.update_account_plan_check")
    @patch("core.gc_registration_service.db.update_job")
    @patch("core.gc_registration_service.db.get_account")
    @patch("core.gc_registration_service.db.get_job")
    def test_valid_promo_only_at_uses_mailbox_fallback(
        self, get_job, get_account, update_job, update_plan, mailbox, close_window
    ):
        get_job.return_value = {
            "id": 23, "gc_mode": True, "gc_window_state": "open",
            "account_id": 9, "email": "mailplus@example.com",
        }
        account = {"id": 9, "email": "mailplus@example.com", "access_token": "valid-at"}
        get_account.return_value = account
        mailbox.return_value = {
            "status": "plus", "evidence": "订阅成功", "plan_promoted": True,
        }
        with patch("core.gc_registration_service.check_account_plan", return_value={
            "ok": True,
            "has_active_plus_subscription": False,
            "subscription_state_available": False,
            "plus_trial_eligible": True,
        }):
            gc._poll(23, threading.Event())
        mailbox.assert_called_once_with(account)
        close_window.assert_called_once_with(23, reason="plus")

    @patch("core.gc_registration_service.close_job_window")
    @patch("core.gc_registration_service._mailbox_plus_fallback")
    @patch("core.gc_registration_service.db.update_account_plan_check")
    @patch("core.gc_registration_service.db.update_job")
    @patch("core.gc_registration_service.db.get_account")
    @patch("core.gc_registration_service.db.get_job")
    def test_expired_at_can_use_positive_mailbox_evidence(
        self, get_job, get_account, update_job, update_plan, mailbox, close_window
    ):
        get_job.return_value = {
            "id": 24, "gc_mode": True, "gc_window_state": "open",
            "account_id": 10, "email": "expired@example.com",
        }
        get_account.return_value = {"id": 10, "email": "expired@example.com", "access_token": "expired-at"}
        mailbox.return_value = {
            "status": "plus", "evidence": "订阅成功", "plan_promoted": True,
        }
        with patch("core.gc_registration_service.check_account_plan", return_value={
            "ok": False, "http_status": 401, "token_expired": True, "error": "AT已过期/失效"
        }):
            gc._poll(24, threading.Event())
        mailbox.assert_called_once()
        close_window.assert_called_once_with(24, reason="plus")

    @patch("core.gc_registration_service._mailbox_plus_fallback")
    @patch("core.gc_registration_service.db.update_account_plan_check")
    @patch("core.gc_registration_service.db.update_job")
    @patch("core.gc_registration_service.db.get_account")
    @patch("core.gc_registration_service.db.get_job")
    def test_deactivated_account_never_uses_mailbox_fallback(
        self, get_job, get_account, update_job, update_plan, mailbox
    ):
        get_job.return_value = {
            "id": 26, "gc_mode": True, "gc_window_state": "open",
            "account_id": 12, "email": "disabled@example.com",
        }
        get_account.return_value = {"id": 12, "email": "disabled@example.com", "access_token": "bad-at"}
        stop_event = threading.Event()
        stop_event.wait = MagicMock(side_effect=lambda _seconds: stop_event.set() or True)
        with patch("core.gc_registration_service.check_account_plan", return_value={
            "ok": False,
            "http_status": 401,
            "account_unusable_code": "account_deactivated",
            "error": "账号已停用",
        }):
            gc._poll(26, stop_event)
        mailbox.assert_not_called()

    @patch("core.gc_registration_service._mailbox_plus_fallback")
    @patch("core.gc_registration_service.db.update_account_plan_check")
    @patch("core.gc_registration_service.db.update_job")
    @patch("core.gc_registration_service.db.get_account")
    @patch("core.gc_registration_service.db.get_job")
    def test_network_error_still_checks_mailbox_for_plus(
        self, get_job, get_account, update_job, update_plan, mailbox
    ):
        get_job.return_value = {
            "id": 27, "gc_mode": True, "gc_window_state": "open",
            "account_id": 13, "email": "network@example.com",
        }
        get_account.return_value = {"id": 13, "email": "network@example.com", "access_token": "at"}
        stop_event = threading.Event()
        stop_event.wait = MagicMock(side_effect=lambda _seconds: stop_event.set() or True)
        with patch("core.gc_registration_service.check_account_plan", return_value={
            "ok": False, "http_status": None, "retryable": True, "error": "ReadTimeout"
        }):
            gc._poll(27, stop_event)
        mailbox.assert_called_once()

    @patch("core.gc_registration_service._mailbox_plus_fallback")
    @patch("core.gc_registration_service.db.update_account_plan_check")
    @patch("core.gc_registration_service.db.update_job")
    @patch("core.gc_registration_service.db.get_account")
    @patch("core.gc_registration_service.db.get_job")
    def test_explicit_free_entitlement_still_uses_mailbox_for_plus(
        self, get_job, get_account, update_job, update_plan, mailbox
    ):
        get_job.return_value = {
            "id": 25, "gc_mode": True, "gc_window_state": "open",
            "account_id": 11, "email": "free@example.com",
        }
        get_account.return_value = {"id": 11, "email": "free@example.com", "access_token": "valid-at"}
        stop_event = threading.Event()

        def wait_once(_seconds):
            stop_event.set()
            return True

        stop_event.wait = MagicMock(side_effect=wait_once)
        with patch("core.gc_registration_service.check_account_plan", return_value={
            "ok": True,
            "current_plan_type": "free",
            "has_active_plus_subscription": False,
            "subscription_state_available": True,
        }):
            gc._poll(25, stop_event)
        mailbox.assert_called_once()


if __name__ == "__main__":
    unittest.main()
