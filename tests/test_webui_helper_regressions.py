# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from core import db
from webui.app import _compact_account_for_list, create_app


class WebUiHelperRegressionTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_account_list_exposes_safe_registration_traffic_totals(self):
        legacy = _compact_account_for_list({
            "id": 1,
            "email": "legacy@test.com",
            "extra_json": json.dumps({
                "registration_traffic": {
                    "downloaded": 1_500,
                    "cache_saved_bytes": 1_000,
                    "cache_hits": 2,
                    "blocked": 3,
                },
            }),
        })
        current = _compact_account_for_list({
            "id": 2,
            "email": "current@test.com",
            "extra_json": json.dumps({
                "registration_traffic": {
                    "metrics_version": 2,
                    "downloaded": 500,
                    "downloaded_excludes_cache_replay": True,
                    "cache_saved_bytes": 1_000,
                    "cache_hits": 2,
                    "blocked": 3,
                    "within_budget": True,
                },
            }),
        })
        self.assertEqual(legacy["registration_traffic"]["network_bytes"], 500)
        self.assertEqual(current["registration_traffic"]["network_bytes"], 500)
        self.assertEqual(current["registration_traffic"]["cache_saved_bytes"], 1_000)
        self.assertNotIn("extra_json", current)

    @patch("webui.app.db.get_account")
    def test_account_secret_single_and_bulk_routes_return_allowlisted_values(self, get_account):
        get_account.side_effect = lambda account_id: {
            "id": account_id,
            "email": f"user{account_id}@test.com",
            "access_token": f"token-{account_id}",
            "copy_line": f"line-{account_id}",
            "codex_agent_token": f"agent-{account_id}",
            "totp_secret": f"totp-{account_id}",
        }

        single = self.client.get("/api/accounts/7/secret?field=access_token")
        self.assertEqual(single.status_code, 200)
        self.assertEqual(single.get_json()["value"], "token-7")

        bulk = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [7, 8], "field": "access_token"},
        )
        self.assertEqual(bulk.status_code, 200)
        self.assertEqual(
            [item["value"] for item in bulk.get_json()["values"]],
            ["token-7", "token-8"],
        )

        totp = self.client.get("/api/accounts/7/secret?field=totp_secret")
        self.assertEqual(totp.status_code, 200)
        self.assertEqual(totp.get_json()["value"], "totp-7")

        totp_bulk = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [7], "field": "totp_secret"},
        )
        self.assertEqual(totp_bulk.status_code, 200)
        self.assertEqual(totp_bulk.get_json()["values"][0]["value"], "totp-7")

        rejected = self.client.get("/api/accounts/7/secret?field=password")
        self.assertEqual(rejected.status_code, 400)

    @patch("webui.app.svc.get_retry_info", return_value={})
    @patch("webui.app.db.list_jobs")
    def test_paged_jobs_route_compacts_normal_and_gc_jobs(self, list_jobs, _get_retry_info):
        list_jobs.return_value = [
            {
                "id": 1,
                "status": "pending",
                "email": "pending@test.com",
                "access_token": "must-not-leak",
            },
            {
                "id": 2,
                "status": "gc_checking",
                "email": "gc@test.com",
                "gc_mode": True,
                "gc_window_state": "open",
                "gc_window_label": "12",
                "roxy_profile_id": 345,
                "account_id": 99,
                "gc_check_message": "查询中",
            },
        ]

        response = self.client.get("/api/jobs?paged=1&page=1&page_size=20")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["compact"])
        self.assertEqual(payload["status_counts"]["pending"], 1)
        self.assertEqual(payload["status_counts"]["gc_checking"], 1)
        self.assertEqual(payload["status_counts"]["active"], 2)
        self.assertNotIn("access_token", payload["items"][0])
        self.assertEqual(payload["items"][1]["roxy_profile_id"], 345)
        self.assertEqual(payload["items"][1]["account_id"], 99)

    def test_accounts_ui_keeps_oaics_actions_without_result_column(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('id="showOaicsAccountsOnlyV2"', html)
        self.assertIn('id="btnExtractSelectedOaicsV2"', html)
        self.assertIn('id="btnCopyAllOaicsLinksV2"', html)
        self.assertNotIn('class="col-oaics"', html)
        self.assertNotIn("_oaicsLinkCell", html)
        self.assertIn('colspan="11"', html)
        self.assertIn('class="col-gcash"', html)
        self.assertIn('id="btnCheckSelectedGcashV2"', html)
        self.assertIn("/api/accounts/check-gcash-bulk", html)
        self.assertIn("/api/accounts/extract-oaics-bulk", html)
        self.assertIn('data-account-copy-secret="totp_secret"', html)
        self.assertIn('data-account-reveal-secret="totp_secret"', html)
        self.assertIn('id="btnRenameAccountGroupV2"', html)
        self.assertIn("const PAGER_DRAFT_SIZES = Object.create(null)", html)
        self.assertIn("oninput=\"pagerDraftSize('${id}',this.value)\"", html)
        self.assertIn("startsWith('pagerSizeInput-')", html)

    def test_proxy_pool_uses_large_unbounded_editor(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('class="proxy-pool-editor-v2"', html)
        self.assertIn('data-bulk-list-toggle', html)
        self.assertIn('不设条数上限', html)
        self.assertIn('proxyPoolEntryCountV2', html)
        self.assertNotIn('maxlength="10"', html)

    def test_plan_check_ui_bursts_until_completed_result_is_rendered(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn("const PLAN_STATUS_WATCHES = new Map()", html)
        self.assertIn("function markPlanChecksQueued(ids, trigger)", html)
        self.assertIn("function watchPlanChecks(ids, baselines)", html)
        self.assertIn("const planCompletedChanged = !isChecking", html)
        self.assertIn("watchPlanChecks([id], baselines)", html)
        self.assertIn("watchPlanChecks(startedIds, baselines)", html)
        self.assertIn("markPlanChecksQueued([id], 'manual')", html)
        self.assertIn("markPlanChecksQueued(ids, 'manual_bulk')", html)
        self.assertIn("检测套餐/Plus/试用", html)
        self.assertIn("plus_trial_eligible", html)
        self.assertIn('id="showNoTrialAccountsOnlyV2"', html)
        self.assertIn("applyAccountsPlanFilter('no-trial')", html)
        self.assertIn("ACCOUNT_PLAN_FILTER === 'no-trial'", html)

    @patch("webui.app.gc_registration_service.close_job_window")
    @patch("webui.app.db.get_job")
    @patch("webui.app.db.delete_job", side_effect=[True, True, True])
    def test_bulk_job_delete_directly_deletes_every_selected_id(self, delete_job, get_job, close_window):

        response = self.client.post("/api/jobs/delete-bulk", json={"job_ids": [1, 2, 3]})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["deleted"], [1, 2, 3])
        self.assertEqual(payload["skipped"], [])
        self.assertEqual(delete_job.call_args_list, [
            unittest.mock.call(1, delete_log=True, allow_running=True),
            unittest.mock.call(2, delete_log=True, allow_running=True),
            unittest.mock.call(3, delete_log=True, allow_running=True),
        ])
        get_job.assert_not_called()
        close_window.assert_not_called()

    @patch("webui.app.db.delete_job", return_value=False)
    def test_bulk_job_delete_only_reports_missing_ids(self, delete_job):

        response = self.client.post("/api/jobs/delete-bulk", json={"job_ids": [9]})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["deleted_count"], 0)
        self.assertEqual(payload["skipped"], [{"id": 9, "reason": "任务不存在"}])
        delete_job.assert_called_once_with(9, delete_log=True, allow_running=True)

    def test_bulk_job_delete_ui_has_no_status_or_window_distinction(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn("直接删除选中的任务记录和日志，不检查任务状态", html)
        self.assertIn("const pageRows = JOBS;", html)
        self.assertIn("const selectableIds = pageRows.map", html)
        self.assertNotIn("已结束任务若仍绑定 GC/Roxy 窗口", html)

    def test_plan_status_revision_changes_for_fast_repeated_completion(self):
        common = {
            "id": 7,
            "email": "free@example.test",
            "updated_at": "2026-08-14T00:00:00",
            "plan_check_status": "success",
            "plan_check_ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": True,
        }
        first = {**common, "plan_check_completed_at": "2026-08-14T00:00:01"}
        second = {**common, "plan_check_completed_at": "2026-08-14T00:00:02"}

        with patch.object(db, "_filtered_decorated_accounts", side_effect=[[first], [second]]):
            first_revision = db.list_account_plan_check_statuses()["revision"]
            second_revision = db.list_account_plan_check_statuses()["revision"]

        self.assertNotEqual(first_revision, second_revision)


if __name__ == "__main__":
    unittest.main()
