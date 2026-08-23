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
        self.assertFalse(current["password_configured"])

        secured = _compact_account_for_list({
            "id": 3,
            "email": "secured@test.com",
            "extra_json": json.dumps({"registration_password": "secret"}),
        })
        self.assertTrue(secured["password_configured"])
        self.assertNotIn("registration_password", secured)

    def test_account_list_exposes_trial_offer_display_metadata(self):
        compact = _compact_account_for_list({
            "id": 4,
            "email": "half@test.com",
            "current_plan_type": "free",
            "plus_trial_eligible": True,
            "plus_trial_offer_kind": "half_price",
            "plus_trial_offer_label": "半价试用",
            "plus_trial_offer_percentage": 50,
            "plus_trial_duration_num_periods": 1,
            "plus_trial_duration_period": "month",
            "plus_trial_campaign_id": "plus-half-price",
        })

        self.assertEqual(compact["plus_trial_offer_kind"], "half_price")
        self.assertEqual(compact["plus_trial_offer_label"], "半价试用")
        self.assertEqual(compact["plus_trial_offer_percentage"], 50)
        self.assertEqual(compact["plus_trial_duration_num_periods"], 1)
        self.assertEqual(compact["plus_trial_duration_period"], "month")

    @patch("webui.app.db.get_account")
    def test_account_secret_single_and_bulk_routes_return_allowlisted_values(self, get_account):
        get_account.side_effect = lambda account_id: {
            "id": account_id,
            "email": f"user{account_id}@test.com",
            "access_token": f"token-{account_id}",
            "copy_line": f"line-{account_id}",
            "codex_agent_token": f"agent-{account_id}",
            "totp_secret": f"totp-{account_id}",
            "extra_json": json.dumps({"registration_password": f"OpenAI-pass-{account_id}!"}),
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

        password = self.client.get("/api/accounts/7/secret?field=registration_password")
        self.assertEqual(password.status_code, 200)
        self.assertEqual(password.get_json()["value"], "OpenAI-pass-7!")

        password_bulk = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [7], "field": "registration_password"},
        )
        self.assertEqual(password_bulk.status_code, 200)
        self.assertEqual(password_bulk.get_json()["values"][0]["value"], "OpenAI-pass-7!")

        credentials = self.client.get("/api/accounts/7/secret?field=account_password_2fa")
        self.assertEqual(credentials.status_code, 200)
        self.assertEqual(
            credentials.get_json()["value"],
            "user7@test.com----OpenAI-pass-7!----totp-7",
        )

        credentials_bulk = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [7, 8], "field": "account_password_2fa"},
        )
        self.assertEqual(credentials_bulk.status_code, 200)
        self.assertEqual(
            [item["value"] for item in credentials_bulk.get_json()["values"]],
            [
                "user7@test.com----OpenAI-pass-7!----totp-7",
                "user8@test.com----OpenAI-pass-8!----totp-8",
            ],
        )

        rejected = self.client.get("/api/accounts/7/secret?field=password")
        self.assertEqual(rejected.status_code, 400)

    @patch("webui.app.db.get_account")
    def test_account_password_2fa_bulk_skips_incomplete_credentials(self, get_account):
        rows = {
            1: {
                "id": 1,
                "email": "ready@test.com",
                "totp_secret": "MFA-READY",
                "extra_json": json.dumps({"registration_password": "Password-1!"}),
            },
            2: {
                "id": 2,
                "email": "missing@test.com",
                "totp_secret": "",
                "extra_json": json.dumps({"registration_password": "Password-2!"}),
            },
        }
        get_account.side_effect = rows.get

        response = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [1, 2], "field": "account_password_2fa"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["values"][0]["value"], "ready@test.com----Password-1!----MFA-READY")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["skipped"][0]["id"], 2)

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
        self.assertNotIn('data-account-reveal-secret="totp_secret"', html)
        self.assertNotIn('>显示2FA</button>', html)
        self.assertNotIn('data-account-copy-secret="totp_secret"', html)
        self.assertNotIn('data-account-copy-secret="registration_password"', html)
        self.assertIn('data-account-copy-secret="account_password_2fa"', html)
        self.assertIn('id="btnCopySelectedCredentialsV2"', html)
        self.assertIn('id="btnCopySelectedCredentialsV2" disabled title="按账号一行复制为：账号----密码----MFA Secret">复制</button>', html)
        self.assertIn("const copyButton = `<button", html)
        self.assertIn(">复制</button>`;", html)
        self.assertIn("field:'account_password_2fa'", html)
        self.assertIn("复制为：账号----密码----MFA Secret", html)
        self.assertIn("const ACTIVE_TOTP_CODES = new Map()", html)
        self.assertIn("const PENDING_TOTP_CODES = new Set()", html)
        self.assertIn("function drawAccountTotpCode(id)", html)
        self.assertIn("function stopAccountTotpCode(id)", html)
        self.assertIn("async function tickAccountTotpCode(id)", html)
        self.assertIn("const totpState = ACTIVE_TOTP_CODES.get(accountId)", html)
        self.assertIn("const totpPending = PENDING_TOTP_CODES.has(accountId)", html)
        self.assertIn("if (PENDING_TOTP_CODES.has(id)) return", html)
        self.assertIn("PENDING_TOTP_CODES.add(id)", html)
        self.assertIn("PENDING_TOTP_CODES.delete(id)", html)
        self.assertIn("button.disabled = pending", html)
        self.assertIn("button.setAttribute('aria-busy', 'true')", html)
        self.assertNotIn("dataset.totpRunning", html)
        self.assertNotIn("dataset.totpTimer", html)
        self.assertIn('<th class="col-small">密码 / 2FA</th>', html)
        self.assertIn('id="btnRenameAccountGroupV2"', html)
        self.assertIn("const PAGER_DRAFT_SIZES = Object.create(null)", html)
        self.assertIn("oninput=\"pagerDraftSize('${id}',this.value)\"", html)
        self.assertIn("startsWith('pagerSizeInput-')", html)

    def test_accounts_ui_resets_tabs_and_exposes_archived_group(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn("window.scrollTo({top: 0, left: 0, behavior: 'auto'})", html)
        self.assertIn('<option value="archived">归档分组</option>', html)
        self.assertIn("const ARCHIVED_ACCOUNT_GROUP_ID = 'archived'", html)
        self.assertIn("ACTIVE_ACCOUNT_GROUP_ID === ARCHIVED_ACCOUNT_GROUP_ID ? '' : ACTIVE_ACCOUNT_GROUP_ID", html)
        self.assertIn('id="showArchivedAccountsV2"', html)
        self.assertIn('>归档分组</button>', html)

    def test_accounts_ui_has_at_validity_schedule_and_problem_filter(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('id="showAtInvalidAccountsOnlyV2"', html)
        self.assertIn('>AT失效/错误</button>', html)
        self.assertIn('id="atValidityIntervalV2"', html)
        self.assertIn('id="btnSaveAtValidityScheduleV2"', html)
        self.assertIn('id="btnRunAtValidityNowV2"', html)
        self.assertIn("ACCOUNT_AT_VALIDITY_FILTER === 'invalid-or-error'", html)
        self.assertIn("'/api/accounts/at-validity-schedule'", html)
        self.assertIn("'/api/accounts/at-validity-check-now'", html)
        self.assertIn("atStatus === 'invalid_confirmed'", html)
        self.assertIn("atStatus === 'check_error'", html)

    def test_accounts_ui_appends_custom_page_size_and_shows_registration_minute(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('type="text" pattern="[0-9]*" value="${attrEsc(draftSize)}"', html)
        self.assertIn('onfocus="pagerPlaceCaretAtEnd(this)"', html)
        self.assertIn('onclick="pagerPlaceCaretAtEnd(this)"', html)
        self.assertIn("input.setSelectionRange(end, end)", html)
        self.assertIn("const registeredAt = r.created_at || r.openai_created_at || ''", html)
        self.assertIn("formatDateTime(registeredAt).slice(0, 16)", html)
        self.assertIn("注册成功：${esc(registeredLine)}", html)

    @patch("webui.app.db.list_account_groups", return_value=[])
    @patch("webui.app.db.list_accounts")
    def test_account_groups_route_reports_archived_count(self, list_accounts, _list_groups):
        list_accounts.return_value = [{"id": 1}, {"id": 2}]

        response = self.client.get("/api/account-groups")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["archived_count"], 2)
        list_accounts.assert_called_once_with(limit=1_000_000, archived="only")

    def test_proxy_pool_uses_large_unbounded_editor(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('class="proxy-pool-editor-v2"', html)
        self.assertIn('data-bulk-list-toggle', html)
        self.assertIn('不设条数上限', html)
        self.assertIn('proxyPoolEntryCountV2', html)
        self.assertNotIn('maxlength="10"', html)

    def test_detection_proxy_ui_uses_add_button_and_country_selector(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('data-add-detection-proxy="${attrEsc(purpose)}"', html)
        self.assertIn("renderDetectionProxyCountryControlV2(checkoutProfiles, checkoutActive, 'checkout')", html)
        self.assertIn("renderDetectionProxyCountryControlV2(planProfiles, planActive, 'plan')", html)
        self.assertIn("renderDetectionProxyCountryControlV2(atProfiles, atActive, 'at')", html)
        self.assertIn('AT_VALIDITY_PROXY_PROFILES', html)
        self.assertIn('专属池非空时只用该池；池为空时只走本机 VPN/系统代理', html)
        self.assertIn('完全跳过 PROXY_POOL', html)
        self.assertIn("r.at_validity_network_route === 'local_vpn' ? '本机 VPN/TUN'", html)
        self.assertIn("atErrorCode === 'request_error' ? 'AT检测: 网络错误'", html)
        self.assertIn('`已尝试 ${atAttempts} 次`', html)
        self.assertIn('data-detection-country-select', html)
        self.assertIn('id="detectionProxyImportModal"', html)
        self.assertIn('加入代理池', html)
        self.assertIn('/api/detection-proxy-pools/import', html)
        self.assertIn('随机洗牌', html)
        self.assertNotIn('${renderConfigPlainFieldV2(planProfiles)}', html)
        self.assertNotIn('${renderConfigPlainFieldV2(checkoutProfiles)}', html)
        self.assertNotIn('${renderConfigPlainFieldV2(atProfiles)}', html)

    def test_settings_ui_filters_irrelevant_drivers_and_has_real_status_cards(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('id="configSearchV2"', html)
        self.assertIn('id="configScopeV2"', html)
        self.assertIn('id="configCardSmsLabelV2"', html)
        self.assertIn('id="configCardViewLabelV2"', html)
        self.assertIn("CONFIG_DRIVER_GROUPS_V2", html)
        self.assertIn("configContextDriverGroupsV2", html)
        self.assertIn("roxyConfigSectionForKey", html)
        self.assertIn("['连接与团队', '环境与代理', '流程重试']", html)
        self.assertIn("显示 ${visibleCount} / ${totalCount} 项", html)
        self.assertIn("接口返回格式异常", html)
        self.assertNotIn("占位卡片 3", html)
        self.assertNotIn("占位卡片 4", html)
        self.assertNotIn("内容稍后完善", html)

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
        self.assertIn("function _trialOfferView(r)", html)
        self.assertIn("trial-offer-pill--free", html)
        self.assertIn("trial-offer-pill--half", html)
        self.assertIn('id="showZeroTrialAccountsOnlyV2"', html)
        self.assertIn('id="showHalfTrialAccountsOnlyV2"', html)
        self.assertIn('id="showDiscountTrialAccountsOnlyV2"', html)
        self.assertIn("applyAccountsPlanFilter('zero-trial')", html)
        self.assertIn("applyAccountsPlanFilter('half-trial')", html)
        self.assertIn("applyAccountsPlanFilter('discount-trial')", html)
        self.assertIn('>0元试用</button>', html)
        self.assertIn('>半价试用</button>', html)
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

    def test_registration_count_has_no_upper_limit(self):
        with (
            patch("config.email.USE_EMAIL_SERVICE", True),
            patch("config.email.EMAIL_SOURCE", "gptmail"),
            patch("config.email.GPTMAIL_API_KEY", "test-key"),
            patch("webui.app.svc.submit_registration", return_value=[]) as submit_registration,
        ):
            response = self.client.post("/api/jobs", json={"count": 501, "workers": 7})

        self.assertEqual(response.status_code, 200)
        submit_registration.assert_called_once_with(count=501, workers=7)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="regCountV2" type="number" min="1" value="1"', html)
        self.assertNotIn('id="regCountV2" type="number" min="1" max="200"', html)
        self.assertIn('id="regCountHintV2">不限</span>', html)

    def test_selected_email_registration_has_no_two_hundred_item_limit(self):
        email_items = [
            {"source": "generic_api", "email": f"user{index}@example.test"}
            for index in range(201)
        ]
        with patch("webui.app.svc.submit_registration", return_value=[]) as submit_registration:
            response = self.client.post(
                "/api/jobs",
                json={"count": 1, "workers": 9, "email_items": email_items},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["requested_count"], 201)
        submit_registration.assert_called_once_with(workers=9, email_items=email_items)


if __name__ == "__main__":
    unittest.main()
