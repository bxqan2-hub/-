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
                    "cache_misses": 3,
                    "cache_candidates": 5,
                    "cache_writes": 1,
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
                    "cache_misses": 3,
                    "cache_candidates": 5,
                    "cache_writes": 1,
                    "blocked": 3,
                    "within_budget": True,
                },
            }),
        })
        self.assertEqual(legacy["registration_traffic"]["network_bytes"], 500)
        self.assertEqual(current["registration_traffic"]["network_bytes"], 500)
        self.assertEqual(current["registration_traffic"]["cache_saved_bytes"], 1_000)
        self.assertEqual(current["registration_traffic"]["cache_candidates"], 5)
        self.assertEqual(current["registration_traffic"]["cache_misses"], 3)
        self.assertEqual(current["registration_traffic"]["cache_writes"], 1)
        self.assertNotIn("extra_json", current)
        self.assertFalse(current["password_configured"])

    @patch("webui.app.db.list_accounts")
    def test_account_qualification_filter_separates_gcash_and_gopay(self, list_accounts):
        list_accounts.return_value = [
            {"id": 1, "email": "gcash@test.com", "gcash_eligible": True, "gopay_eligible": False},
            {"id": 2, "email": "gopay@test.com", "gcash_eligible": False, "gopay_eligible": True},
            {"id": 3, "email": "both@test.com", "gcash_eligible": True, "gopay_eligible": True},
        ]
        for value, expected in (("gcash", {1, 3}), ("gopay", {2, 3}), ("any", {1, 2, 3})):
            response = self.client.get(f"/api/accounts?paged=1&page=1&page_size=20&qualification={value}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual({row["id"] for row in response.get_json()["items"]}, expected)

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

    @patch("webui.app.db.list_account_groups")
    @patch("webui.app._list_pool_rows")
    def test_outlook_pool_supports_status_and_group_filters(self, list_pool_rows, list_groups):
        list_pool_rows.return_value = [
            {"email": "available@test.com", "status": "available"},
            {"email": "used@test.com", "status": "used"},
            {"email": "other@test.com", "status": "available"},
        ]
        list_groups.return_value = [{"id": "g1", "name": "分组1", "emails": ["available@test.com"]}]

        response = self.client.get("/api/outlook?paged=1&page=1&page_size=50&status=available&group=g1")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["email"], "available@test.com")
        list_pool_rows.assert_called_once_with(source="outlook", status="available", fetch_limit=1_000_000)

    def test_outlook_pool_offers_email_only_copy_action(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('id="btnCopySelectedOutlookEmailsV2"', html)
        self.assertIn('title="只复制选中的邮箱账号，每个邮箱一行"', html)
        self.assertIn("bind('btnCopySelectedOutlookEmailsV2', () => copySelectionEmails({emails:selectedOutlookEmails()}))", html)
        self.assertIn("'btnCopySelectedOutlookEmailsV2',", html)

    def test_modal_scroll_lock_does_not_fix_the_whole_page(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn("body.modal-open { overflow: hidden; }", html)
        self.assertNotIn("body.modal-open { overflow: hidden; position: fixed", html)
        self.assertNotIn("document.body.style.top", html)
        self.assertNotIn("modalScrollY", html)
        self.assertIn("document.documentElement.style.overflow = 'hidden';", html)
        self.assertIn("document.documentElement.style.overflow = '';", html)

    @patch("webui.app.db.get_account")
    def test_account_secret_single_and_bulk_routes_return_allowlisted_values(self, get_account):
        get_account.side_effect = lambda account_id: {
            "id": account_id,
            "email": f"user{account_id}@test.com",
            "access_token": f"token-{account_id}",
            "copy_line": f"line-{account_id}",
            "codex_agent_token": f"agent-{account_id}",
            "totp_secret": "JBSWY3DPEHPK3PXP",
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
        self.assertEqual(totp.get_json()["value"], "JBSWY3DPEHPK3PXP")

        totp_bulk = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [7], "field": "totp_secret"},
        )
        self.assertEqual(totp_bulk.status_code, 200)
        self.assertEqual(totp_bulk.get_json()["values"][0]["value"], "JBSWY3DPEHPK3PXP")

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
            "user7@test.com----OpenAI-pass-7!----JBSWY3DPEHPK3PXP",
        )

        credentials_bulk = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [7, 8], "field": "account_password_2fa"},
        )
        self.assertEqual(credentials_bulk.status_code, 200)
        self.assertEqual(
            [item["value"] for item in credentials_bulk.get_json()["values"]],
            [
                "user7@test.com----OpenAI-pass-7!----JBSWY3DPEHPK3PXP",
                "user8@test.com----OpenAI-pass-8!----JBSWY3DPEHPK3PXP",
            ],
        )

        credentials_url = self.client.get("/api/accounts/7/secret?field=account_password_2fa_url")
        self.assertEqual(credentials_url.status_code, 200)
        self.assertEqual(
            credentials_url.get_json()["value"],
            "user7@test.com----OpenAI-pass-7!----https://2fa.fb.tools/JBSWY3DPEHPK3PXP",
        )

        rejected = self.client.get("/api/accounts/7/secret?field=password")
        self.assertEqual(rejected.status_code, 400)

    @patch("webui.app.db.get_account")
    def test_account_password_2fa_bulk_skips_incomplete_credentials(self, get_account):
        rows = {
            1: {
                "id": 1,
                "email": "ready@test.com",
                "totp_secret": "JBSWY3DPEHPK3PXP",
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
        self.assertEqual(payload["values"][0]["value"], "ready@test.com----Password-1!----JBSWY3DPEHPK3PXP")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["skipped"][0]["id"], 2)

    @patch("webui.app.db.get_account")
    @patch("time.time", return_value=1700000000)
    def test_totp_code_keeps_local_generation(self, _time, get_account):
        get_account.return_value = {
            "id": 7,
            "email": "user7@test.com",
            "totp_secret": "jbsw y3dp ehpk 3pxp",
        }

        result = self.client.get("/api/accounts/7/totp-code")

        self.assertEqual(result.status_code, 200)
        payload = result.get_json()
        self.assertEqual(payload["totp_code"], "324550")
        self.assertEqual(payload["remaining_seconds"], 10)
        self.assertNotIn("source", payload)

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
        self.assertIn('id="btnCheckSelectedQualificationsV2"', html)
        self.assertIn('id="qualificationQueryModal"', html)
        self.assertIn('id="qualificationQueryMenuV2"', html)
        self.assertIn('function bindQualificationQueryMenu()', html)
        self.assertIn('id="btnQualificationFilterV2"', html)
        self.assertIn('data-account-qualification-filter="gcash"', html)
        self.assertIn('data-account-qualification-filter="gopay"', html)
        self.assertNotIn('id="showGcashAccountsOnlyV2"', html)
        self.assertIn("/api/accounts/check-gcash-bulk", html)
        self.assertIn("/api/accounts/check-gopay-bulk", html)
        self.assertIn("/api/accounts/extract-oaics-bulk", html)
        self.assertNotIn('data-account-reveal-secret="totp_secret"', html)
        self.assertNotIn('>显示2FA</button>', html)
        self.assertNotIn('data-account-copy-secret="totp_secret"', html)
        self.assertNotIn('data-account-copy-secret="registration_password"', html)
        self.assertIn('data-account-copy-secret="account_password_2fa"', html)
        self.assertIn('id="btnCopySelectedCredentialsV2"', html)
        self.assertIn('id="btnCopySelectedCredentialsV2" disabled title="按账号一行复制所选账号：账号----密码----原始2FA Secret">复制所选密码2FA</button>', html)
        self.assertIn('id="btnCopySelectedCredentialsUrlV2" disabled title="按账号一行复制所选账号：账号----密码----2FA取码地址">复制所选2FA URL</button>', html)
        self.assertIn('data-account-copy-secret="account_password_2fa_url"', html)
        self.assertIn('id="poolStatusV2"', html)
        self.assertIn('<option value="available">可用邮箱</option>', html)
        self.assertIn('<option value="used">已用邮箱</option>', html)
        self.assertIn('id="poolGroupV2"', html)
        self.assertIn('function renderPoolGroupFilter()', html)
        self.assertIn('status:POOL_STATUS_FILTER', html)
        self.assertIn('group:POOL_GROUP_FILTER', html)
        self.assertNotIn('id="btnCopyAllCredentialsV2"', html)
        self.assertNotIn("copyAllAccountPasswordTwoFa", html)
        self.assertIn("const copyButton = `<button", html)
        self.assertIn(">复制</button>`;", html)
        self.assertIn("field:useUrl ? 'account_password_2fa_url' : 'account_password_2fa'", html)
        self.assertIn("复制为：账号----密码----2FA取码地址", html)
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
        self.assertIn('data-account-totp-copy="${id}"', html)
        self.assertIn("const totpCopyTarget = e.target.closest('[data-account-totp-copy]')", html)
        self.assertIn("await copyText(state.code)", html)
        self.assertIn("showToast('验证码已复制')", html)
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
        self.assertIn('id="atValidityRecheckIntervalV2"', html)
        self.assertIn('id="btnSaveAtValidityScheduleV2"', html)
        self.assertIn('id="btnRunAtValidityNowV2"', html)
        self.assertIn("ACCOUNT_AT_VALIDITY_FILTER === 'invalid-or-error'", html)
        self.assertIn("'/api/accounts/at-validity-schedule'", html)
        self.assertIn("'/api/accounts/at-validity-check-now'", html)
        self.assertIn("atStatus === 'invalid_confirmed'", html)
        self.assertIn("atStatus === 'check_error'", html)

    def test_accounts_ui_global_stop_cancels_all_account_operations(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('id="btnStopSelectedCodexV2"', html)
        self.assertIn('全局停止账号页面的所有后台操作', html)
        self.assertIn("'/api/accounts/stop-all'", html)
        self.assertIn("function stopAccountPagePolling()", html)
        self.assertIn("ACCOUNT_OPERATION_CONTROLLER.abort()", html)
        self.assertIn('id="btnAtQualificationCheckV2"', html)
        self.assertIn('AT检测大全', html)
        self.assertIn('id="atQualificationModal"', html)
        self.assertIn("/api/accounts/at-qualification-check", html)
        self.assertIn("renderAtQualificationResults(payload)", html)
        self.assertIn('class="outlook-import-dialog at-qualification-dialog"', html)
        self.assertIn('class="outlook-import-result at-qualification-result"', html)
        self.assertIn('class="at-qualification-table"', html)
        self.assertIn('<th>检测时间</th>', html)
        self.assertIn('<th>代理</th>', html)
        self.assertIn("row.error || row.detection_outcome || '—'", html)
        self.assertIn('id="btnCopyAtQualificationEligible"', html)
        self.assertIn('复制成功资格邮箱+AT（0）', html)
        self.assertIn("function copyAtQualificationEligible()", html)
        self.assertIn("row.email}----${row.access_token}", html)

    @patch("webui.app.gcash_service.probe_access_token")
    @patch("core.detection_proxy.resolve_detection_proxy", return_value="http://proxy.test")
    @patch("core.detection_proxy.resolve_static_detection_proxy", return_value="http://proxy.test")
    @patch("core.detection_proxy.parse_detection_proxy_pool", return_value=["gc-profile"])
    @patch("core.detection_proxy.qualification_proxy_specs", return_value=["PH|http://proxy.test"])
    @patch("webui.app._access_token_identity")
    @patch("webui.app._parse_at_import_text", return_value=(["entry-1", "entry-2"], []))
    def test_at_qualification_returns_at_only_for_successful_rows(
        self, _parse_import, identity, _parse_pool, _qualification_pool, _resolve_proxy, _resolve_static_proxy, probe
    ):
        identity.side_effect = [
            {"email": "eligible@test.com", "access_token": "AT-ELIGIBLE"},
            {"email": "ineligible@test.com", "access_token": "AT-INELIGIBLE"},
        ]
        probe.side_effect = [
            {"ok": True, "gcash": True, "attempt_count": 1, "checked_at": "now"},
            {"ok": True, "gcash": False, "attempt_count": 1, "checked_at": "now"},
        ]

        response = self.client.post(
            "/api/accounts/at-qualification-check",
            json={"text": "entry-1\nentry-2", "qualification": "gcash"},
        )

        self.assertEqual(response.status_code, 200)
        results = response.get_json()["results"]
        self.assertEqual(results[0]["access_token"], "AT-ELIGIBLE")
        self.assertEqual(results[1]["access_token"], "")

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
        self.assertIn('GCash 资格', html)
        self.assertIn('GoPay 资格', html)
        self.assertIn('data-add-detection-proxy="gcash"', html)
        self.assertIn('data-add-detection-proxy="gopay"', html)
        self.assertIn('当前已加入 <strong>${qualificationPoolCount(gcashProfiles)}</strong> 条 GCash 专属代理', html)
        self.assertIn('当前已加入 <strong>${qualificationPoolCount(gopayProfiles)}</strong> 条 GoPay 专属代理', html)
        self.assertNotIn('旧 GCash PH 代理池', html)
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
        self.assertIn('data-delete-detection-proxy-country', html)
        self.assertIn('/api/detection-proxy-pools/delete', html)
        self.assertIn('删除当前国家池', html)
        self.assertIn('不影响其他代理池', html)
        self.assertIn('随机洗牌', html)
        self.assertNotIn('${renderConfigPlainFieldV2(planProfiles)}', html)
        self.assertNotIn('${renderConfigPlainFieldV2(checkoutProfiles)}', html)
        self.assertNotIn('${renderConfigPlainFieldV2(atProfiles)}', html)

    def test_qualification_ui_offers_individual_and_all_queries(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn("const workers = Math.min(ids.length, 8)", html)
        self.assertIn('data-query-qualification="gcash"', html)
        self.assertIn('data-query-qualification="gopay"', html)
        self.assertIn('data-query-qualification="all"', html)
        self.assertIn('>资格</th>', html)
        self.assertIn("function _qualificationBadges(r)", html)
        self.assertIn("r.gcash_eligible === true ?", html)
        self.assertIn("r.gopay_eligible === true ?", html)
        self.assertIn("const momoFailed = String(r.momo_status || '').toLowerCase() === 'failed'", html)
        self.assertIn("const momoNegative = momoFailed ||", html)
        self.assertIn("r.momo_eligible !== true", html)
        self.assertIn("qualification-badge-failed", html)
        self.assertIn("qualification-status-dot", html)
        self.assertIn("const momoDot = momoNegative ?", html)
        self.assertIn("GCash 检测中", html)
        self.assertIn("GoPay 检测中", html)
        self.assertIn("function markQualificationChecksQueued(payloads)", html)
        self.assertIn("account.gcash_status = 'queued'", html)
        self.assertIn("account.gopay_status = 'queued'", html)
        self.assertIn('class="pill ${gcashClass}"', html)
        self.assertIn('class="pill ${gopayClass}"', html)
        self.assertIn('justify-content: center', html)
        self.assertNotIn("合并查询：创建 Checkout 同时判定类型与 GCash 资格", html)

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
        self.assertIn("plan_check_proxy_country", html)
        self.assertIn('class="plan-proxy-country-v2"', html)
        self.assertIn("套餐查询代理地区", html)
        self.assertIn("${esc(proxyCountry)}", html)
        self.assertIn("请求语言: ${requestLanguage}", html)
        self.assertIn("plan_check_request_language", html)

    def test_account_list_exposes_plan_check_proxy_country(self):
        compact = _compact_account_for_list({
            "id": 8,
            "email": "proxy-region@test.com",
            "plan_check_proxy_country": "PH",
            "plan_check_locale_country": "PH",
            "plan_check_request_language": "en-PH",
        })

        self.assertEqual(compact["plan_check_proxy_country"], "PH")
        self.assertEqual(compact["plan_check_locale_country"], "PH")
        self.assertEqual(compact["plan_check_request_language"], "en-PH")

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
