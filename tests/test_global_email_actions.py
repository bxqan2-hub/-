import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from core import db, plan_check_service
from webui.app import create_app


class GlobalEmailApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.list_codex_accounts", return_value=[])
    @patch("webui.app.db.get_job")
    @patch("webui.app.db.get_account")
    def test_resolve_selection_to_unique_emails(self, get_account, get_job, _list_codex):
        get_account.return_value = {"email": "One@Test.com"}
        get_job.return_value = {"email": "one@test.com"}
        response = self.client.post(
            "/api/emails/resolve",
            json={"account_ids": [1], "job_ids": [2], "emails": ["Two@Test.com"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["emails"], ["Two@Test.com", "One@Test.com"])

    @patch("webui.app.db.purge_emails_everywhere")
    @patch("webui.app.db.list_codex_accounts", return_value=[])
    def test_global_purge_endpoint(self, _list_codex, purge):
        purge.return_value = {
            "requested": 1,
            "purged_emails": ["one@test.com"],
            "protected": [],
            "counts": {"accounts": 1},
        }
        response = self.client.post("/api/emails/purge", json={"emails": ["one@test.com"]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        purge.assert_called_once_with(["one@test.com"], protect_active_jobs=True)

    @patch("webui.app.db.list_accounts")
    def test_free_delete_targets_only_returns_confirmed_safe_free(self, list_accounts):
        list_accounts.return_value = [
            {"id": 1, "email": "free@test.com", "current_plan_type": "free", "plan_last_success_at": "now"},
            {"id": 2, "email": "unknown@test.com", "current_plan_type": "free"},
            {"id": 3, "email": "mail-plus@test.com", "current_plan_type": "free", "plan_last_success_at": "now", "mail_plus_status": "plus"},
            {"id": 4, "email": "paid@test.com", "current_plan_type": "plus", "plan_last_success_at": "now", "has_active_plus_subscription": True},
        ]
        response = self.client.get("/api/accounts/free-delete-targets")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"], [{"id": 1, "email": "free@test.com"}])
        list_accounts.assert_called_once_with(limit=1_000_000, archived="0", plan_filter="free")

    @patch("webui.app.db.add_mail_status_emails")
    @patch("webui.app.db.list_accounts")
    def test_mail_status_add_all_accounts_returns_complete_target_set(self, list_accounts, add_emails):
        list_accounts.return_value = [
            {"id": 1, "email": "one@test.com"},
            {"id": 2, "email": "two@test.com"},
        ]
        add_emails.return_value = ([{"email": "one@test.com"}], [{"email": "two@test.com", "reason": "已存在"}])

        response = self.client.post("/api/mail-status/add", json={"all_accounts": True})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["target_emails"], ["one@test.com", "two@test.com"])
        self.assertEqual(payload["target_count"], 2)
        list_accounts.assert_called_once_with(limit=1_000_000, archived=False)
        add_emails.assert_called_once_with(["one@test.com", "two@test.com"])


class RegistrationFreeCleanupTests(unittest.TestCase):
    @patch.object(plan_check_service, "_QUEUE_SLOTS", new_callable=MagicMock)
    @patch.object(plan_check_service.db, "update_account_plan_check")
    @patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True)
    @patch.object(plan_check_service, "_wait_for_rate_slot")
    @patch.object(plan_check_service, "check_account_plan")
    def test_registration_free_is_checked_once_and_kept(
        self, check_plan, _rate, _mark, update, _slots
    ):
        check_plan.return_value = {"ok": True, "current_plan_type": "free"}
        result = plan_check_service._run_plan_check(
            account_id=1,
            email="free@test.com",
            access_token="token",
            trigger="registration_auto",
            proxy="",
            timezone_offset_min="-",
        )
        self.assertEqual(result["current_plan_type"], "free")
        self.assertEqual(check_plan.call_count, 1)
        update.assert_called_once()


class MailboxPlusEvidenceBoundaryTests(unittest.TestCase):
    def test_transient_mailbox_error_preserves_previous_plus_result(self):
        rows = [{
            "id": 1,
            "email": "plus@test.com",
            "status": "plus",
            "label": "Plus",
            "evidence": "Plus 套餐、订阅成功",
            "subject": "ChatGPT - Your new plan",
            "checked_at": "2026-08-15T01:00:00",
            "error": "",
        }]
        with patch.object(db, "_load_mail_status_pool", return_value=rows), \
             patch.object(db, "_save_mail_status_pool"):
            db.mark_mail_status_checking("plus@test.com")
            result = db.update_mail_status_result("plus@test.com", {
                "status": "error",
                "label": "检测失败",
                "error": "ConnectTimeout",
            })

        self.assertEqual(result["status"], "plus")
        self.assertEqual(result["evidence"], "Plus 套餐、订阅成功")
        self.assertEqual(result["last_check_status"], "error")
        self.assertEqual(result["last_check_error"], "ConnectTimeout")
        self.assertTrue(result["preserved_previous_result"])

    @staticmethod
    def _sync_account(account, result):
        import json
        import tempfile
        from pathlib import Path
        from core import db

        temporary = tempfile.TemporaryDirectory()
        accounts_path = Path(temporary.name) / "accounts.json"
        accounts_path.write_text(json.dumps([account]), encoding="utf-8")
        account_patch = patch.object(db, "_ACCOUNTS_JSON", accounts_path)
        legacy_patch = patch.object(db, "_LEGACY_ACCOUNTS_JSON", Path(temporary.name) / "legacy.json")
        account_patch.start()
        legacy_patch.start()
        try:
            synced = db.sync_account_mail_status(account["email"], result)
            row = db.get_account(account["id"])
        finally:
            legacy_patch.stop()
            account_patch.stop()
            temporary.cleanup()
        return synced, row

    @staticmethod
    def _confirmed_free(**updates):
        row = {
            "id": 9,
            "email": "candidate@test.com",
            "account_id": "acct-current",
            "current_plan_type": "free",
            "plan_type": "free",
            "subscription_plan": "chatgptfreeplan",
            "subscription_status": "active",
            "has_active_subscription": False,
            "has_active_plus_subscription": False,
            "is_free_plan": True,
            "plan_check_status": "success",
            "plan_check_ok": True,
            "plan_authority": "authoritative",
            "plan_last_success_at": datetime.now().isoformat(timespec="seconds"),
        }
        row.update(updates)
        return row

    @staticmethod
    def _plus_mail(**updates):
        result = {
            "status": "plus",
            "evidence": "Plus 套餐、订阅成功、订阅管理",
            "subject": "ChatGPT - Your new plan",
            "mail_date": datetime.now().isoformat(timespec="seconds"),
            "account_id": "acct-current",
        }
        result.update(updates)
        return result

    def test_confirmed_free_promotes_only_recent_matching_workspace_mail(self):
        synced, row = self._sync_account(self._confirmed_free(), self._plus_mail())

        self.assertTrue(synced)
        self.assertEqual(row["mail_plus_status"], "plus")
        self.assertEqual(row["mail_plus_account_id"], "acct-current")
        self.assertTrue(row["mail_plus_promoted"])
        self.assertEqual(row["current_plan_type"], "plus")
        self.assertTrue(row["has_active_plus_subscription"])
        self.assertEqual(row["plan_detection_source"], "mail/plus-confirmation")

    def test_confirmed_free_keeps_raw_old_or_wrong_workspace_mail_without_promotion(self):
        stale_date = (datetime.now() - timedelta(days=36)).isoformat(timespec="seconds")
        cases = (
            ("old", self._plus_mail(mail_date=stale_date)),
            ("wrong-workspace", self._plus_mail(account_id="acct-other")),
        )
        for label, result in cases:
            with self.subTest(case=label):
                synced, row = self._sync_account(self._confirmed_free(), result)

                self.assertTrue(synced)
                self.assertEqual(row["mail_plus_status"], "plus")
                self.assertEqual(row["mail_plus_account_id"], result["account_id"])
                self.assertEqual(row["mail_plus_date"], result["mail_date"])
                self.assertFalse(row["mail_plus_promoted"])
                self.assertEqual(row["current_plan_type"], "free")
                self.assertFalse(row["has_active_plus_subscription"])

    def test_inconclusive_plan_check_allows_recognized_plus_mail_promotion(self):
        account = self._confirmed_free(
            plan_check_status="failed",
            plan_check_ok=False,
            plan_authority="none",
            plan_check_error="temporary endpoint failure",
        )
        result = self._plus_mail(mail_date="", account_id="")

        synced, row = self._sync_account(account, result)

        self.assertTrue(synced)
        self.assertTrue(row["mail_plus_promoted"])
        self.assertEqual(row["current_plan_type"], "plus")
        self.assertEqual(row["last_at_plan_check_error"], "temporary endpoint failure")

    def test_terminal_subscription_status_blocks_mail_promotion(self):
        for status in ("expired", "canceled", "past_due"):
            with self.subTest(subscription_status=status):
                synced, row = self._sync_account(
                    self._confirmed_free(subscription_status=status),
                    self._plus_mail(),
                )

                self.assertTrue(synced)
                self.assertEqual(row["mail_plus_status"], "plus")
                self.assertFalse(row["mail_plus_promoted"])
                self.assertEqual(row["current_plan_type"], "free")
                self.assertFalse(row["has_active_plus_subscription"])

    def test_nonplus_mail_result_never_downgrades_existing_plus_plan(self):
        account = self._confirmed_free(
            current_plan_type="plus",
            plan_type="plus",
            subscription_plan="chatgptplusplan",
            has_active_subscription=True,
            has_active_plus_subscription=True,
            is_free_plan=False,
            mail_plus_promoted=True,
        )

        synced, row = self._sync_account(account, {
            "status": "nonplus",
            "evidence": "未找到 Plus 订阅成功邮件",
            "subject": "",
        })

        self.assertTrue(synced)
        self.assertEqual(row["mail_plus_status"], "nonplus")
        self.assertEqual(row["current_plan_type"], "plus")
        self.assertEqual(row["subscription_plan"], "chatgptplusplan")
        self.assertTrue(row["has_active_plus_subscription"])


if __name__ == "__main__":
    unittest.main()
