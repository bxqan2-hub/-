# -*- coding: utf-8 -*-
import unittest
import base64
import json
from unittest.mock import MagicMock, patch, sentinel

from core import chatgpt_plan, db, plan_check_service
from webui.app import create_app


class AccountPlanFilterTests(unittest.TestCase):
    @staticmethod
    def _token(account_id):
        payload = base64.urlsafe_b64encode(json.dumps({
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        }).encode()).decode().rstrip("=")
        return f"header.{payload}.signature"

    def test_trial_filters_use_successful_eligibility_result(self):
        trial = {
            "current_plan_type": "free",
            "plan_last_success_at": "2026-08-06T12:00:00",
            "plus_trial_eligible": True,
        }
        no_trial = {
            "current_plan_type": "free",
            "plan_last_success_at": "2026-08-06T12:00:00",
            "plus_trial_eligible": False,
        }
        unchecked = {"current_plan_type": "free", "plus_trial_eligible": False}
        plus = {
            "current_plan_type": "free",
            "subscription_plan": "chatgptplusplan",
            "has_active_subscription": True,
            "has_active_plus_subscription": True,
            "mail_plus_status": "plus",
        }

        self.assertTrue(db._account_matches_plan_filter(trial, "trial"))
        self.assertFalse(db._account_matches_plan_filter(no_trial, "trial"))
        self.assertTrue(db._account_matches_plan_filter(no_trial, "no-trial"))
        self.assertFalse(db._account_matches_plan_filter(trial, "no-trial"))
        self.assertFalse(db._account_matches_plan_filter(unchecked, "no-trial"))
        self.assertFalse(db._account_matches_plan_filter(plus, "trial"))
        self.assertTrue(db._account_matches_plan_filter(plus, "plus"))

    def test_trial_offer_filters_distinguish_zero_half_and_other_discounts(self):
        base = {
            "current_plan_type": "free",
            "is_free_plan": True,
            "plan_last_success_at": "2026-08-23T12:00:00",
            "plus_trial_eligible": True,
        }
        zero = {**base, "plus_trial_offer_kind": "free_trial"}
        half = {**base, "plus_trial_offer_kind": "half_price"}
        other = {**base, "plus_trial_offer_kind": "discount"}

        self.assertTrue(db._account_matches_plan_filter(zero, "zero-trial"))
        self.assertFalse(db._account_matches_plan_filter(zero, "half-trial"))
        self.assertTrue(db._account_matches_plan_filter(half, "half-trial"))
        self.assertFalse(db._account_matches_plan_filter(half, "discount-trial"))
        self.assertTrue(db._account_matches_plan_filter(other, "discount-trial"))
        self.assertTrue(all(db._account_matches_plan_filter(row, "trial") for row in (zero, half, other)))

    def test_legacy_trial_filter_uses_campaign_metadata_when_kind_is_missing(self):
        legacy_zero = {
            "current_plan_type": "free",
            "is_free_plan": True,
            "plan_last_success_at": "2026-08-23T12:00:00",
            "plus_trial_eligible": True,
            "plus_trial_campaign_id": "plus-1-month-free",
        }
        legacy_half = {
            **legacy_zero,
            "plus_trial_campaign_id": "plus-half-price",
            "plus_trial_discount_percentage": 50,
        }

        self.assertTrue(db._account_matches_plan_filter(legacy_zero, "zero-trial"))
        self.assertTrue(db._account_matches_plan_filter(legacy_half, "half-trial"))

    def test_unpromoted_raw_plus_mail_remains_in_account_page_free_filter(self):
        raw_mail_only = {
            "current_plan_type": "free",
            "subscription_plan": "chatgptfreeplan",
            "has_active_subscription": False,
            "has_active_plus_subscription": False,
            "is_free_plan": True,
            "mail_plus_status": "plus",
            "mail_plus_promoted": False,
        }

        self.assertFalse(db._account_matches_plan_filter(raw_mail_only, "plus"))
        self.assertTrue(db._account_matches_plan_filter(raw_mail_only, "free"))

    def test_accounts_check_uses_entitlement_to_mark_active_plus(self):
        result = chatgpt_plan.parse_accounts_check({
            "accounts": {
                "default": {
                    "account": {"account_id": "acct-1", "plan_type": "free"},
                    "entitlement": {
                        "subscription_plan": "chatgptplusplan",
                        "has_active_subscription": True,
                    },
                    "eligible_promo_campaigns": {"plus": {"id": "trial-offer"}},
                },
            },
        })
        self.assertTrue(result["has_active_plus_subscription"])
        self.assertFalse(result["is_free_plan"])
        self.assertFalse(result["plus_trial_eligible"])

    def test_accounts_check_classifies_zero_and_half_price_trial_metadata(self):
        def parse_campaign(campaign):
            return chatgpt_plan.parse_accounts_check({
                "accounts": {
                    "default": {
                        "account": {"account_id": "acct-trial", "plan_type": "free"},
                        "entitlement": {
                            "subscription_plan": "chatgptfreeplan",
                            "has_active_subscription": False,
                        },
                        "eligible_promo_campaigns": {"plus": campaign},
                    },
                },
            })

        zero = parse_campaign({
            "id": "plus-1-month-free",
            "metadata": {
                "title": "One month free",
                "discount": {"percentage": 100},
                "duration": {"num_periods": 1, "period": "month"},
            },
        })
        half = parse_campaign({
            "id": "plus-half-price",
            "metadata": {
                "title": "50% off first month",
                "discount": {"percentage": 50},
                "duration": {"num_periods": 1, "period": "month"},
            },
        })
        ratio = chatgpt_plan.classify_plus_trial_offer({
            "id": "plus-discount",
            "metadata": {"discount": {"percentage": 0.25}},
        })

        self.assertEqual(zero["plus_trial_offer_kind"], "free_trial")
        self.assertEqual(zero["plus_trial_offer_label"], "0元试用")
        self.assertEqual(zero["plus_trial_offer_percentage"], 100)
        self.assertEqual(half["plus_trial_offer_kind"], "half_price")
        self.assertEqual(half["plus_trial_offer_label"], "半价试用")
        self.assertEqual(half["plus_trial_offer_percentage"], 50)
        self.assertEqual(ratio["kind"], "discount")
        self.assertEqual(ratio["percentage"], 25)

    def test_trial_offer_classifier_does_not_invent_missing_discount(self):
        generic = chatgpt_plan.classify_plus_trial_offer({
            "id": "plus-seasonal-offer",
            "metadata": {"title": "Special offer"},
        })
        absent = chatgpt_plan.classify_plus_trial_offer(None)

        self.assertEqual(generic["kind"], "trial")
        self.assertIsNone(generic["percentage"])
        self.assertEqual(absent["kind"], "none")

    def test_plan_result_persists_trial_offer_classification_and_details(self):
        stored = {}

        def capture(rows):
            stored["rows"] = json.loads(json.dumps(rows))

        with patch.object(db, "_load_accounts", return_value=[{"id": 4, "email": "trial@test.com"}]), \
             patch.object(db, "_save_accounts", side_effect=capture):
            updated = db.update_account_plan_check(acc_id=4, result={
                "ok": True,
                "checked_at": "2026-08-23T12:00:00",
                "plan_authority": "authoritative",
                "current_plan_type": "free",
                "is_free_plan": True,
                "plus_trial_eligible": True,
                "plus_trial_campaign_id": "plus-half-price",
                "plus_trial_title": "50% off first month",
                "plus_trial_summary": "First month promotional price",
                "plus_trial_discount_percentage": 50,
                "plus_trial_duration_num_periods": 1,
                "plus_trial_duration_period": "month",
                "plus_trial_promotion_type_label": "Promotional pricing",
                "plus_trial_offer_kind": "half_price",
                "plus_trial_offer_label": "半价试用",
                "plus_trial_offer_percentage": 50,
                "plus_trial_offer_evidence": "metadata.discount.percentage",
            })

        self.assertTrue(updated)
        row = stored["rows"][0]
        self.assertEqual(row["plus_trial_offer_kind"], "half_price")
        self.assertEqual(row["plus_trial_offer_label"], "半价试用")
        self.assertEqual(row["plus_trial_offer_percentage"], 50)
        self.assertEqual(row["plus_trial_summary"], "First month promotional price")

    def test_accounts_check_never_borrows_default_workspace(self):
        with self.assertRaisesRegex(ValueError, "JWT 当前 workspace"):
            chatgpt_plan.parse_accounts_check({
                "accounts": {
                    "default": {
                        "account": {"plan_type": "plus"},
                        "entitlement": {
                            "subscription_plan": "chatgptplusplan",
                            "has_active_subscription": True,
                        },
                    },
                },
            }, token=self._token("acct-current"))

    def test_subscription_fallback_requires_exact_workspace(self):
        observation, error = chatgpt_plan._subscription_plan_signal({
            "account_id": "acct-other",
            "plan_type": "plus",
        }, "acct-current")
        self.assertIsNone(observation)
        self.assertIn("workspace", error)

    def test_fallback_preserves_accounts_check_trial_eligibility(self):
        result = chatgpt_plan._result_from_observation(
            {
                "plan_family": "plus",
                "plan_code_raw": "chatgptplusplan",
                "subscription_state": "active",
                "source": "backend-api/subscriptions",
                "scope": "subscription",
                "evidence_path": "response.plan_type",
            },
            claims={"account_id": "acct-current"},
            authority="authoritative",
            confidence="high",
        )
        self.assertTrue(result["has_active_plus_subscription"])
        self.assertTrue(result["preserve_plus_trial_eligibility"])
        self.assertNotIn("plus_trial_eligible", result)

    def test_terminal_token_failure_is_not_account_deactivation(self):
        result = chatgpt_plan._terminal_plan_error("access_token_expired", 401)
        self.assertNotIn("account_unusable_code", result)
        self.assertEqual(result["credential_unusable_code"], "access_token_expired")
        self.assertTrue(result["token_expired"])

    def test_contextual_auth_status_maps_to_credential_failure(self):
        self.assertEqual(
            chatgpt_plan._conclusive_account_code({"auth": {"status": "expired"}}),
            "authentication_expired",
        )

    def test_accounts_check_signal_requires_explicit_free_entitlement(self):
        signals = chatgpt_plan._accounts_plan_signals({
            "accounts": {
                "acct-current": {
                    "account": {"plan_type": "free"},
                    "entitlement": {},
                },
            },
        }, "acct-current")
        self.assertIsNone(signals["paid"])
        self.assertFalse(signals["free_inactive"])

    def test_accounts_check_paid_signal_uses_entitlement(self):
        signals = chatgpt_plan._accounts_plan_signals({
            "accounts": {
                "acct-current": {
                    "account": {"plan_type": "free"},
                    "entitlement": {
                        "subscription_plan": "chatgptplusplan",
                        "has_active_subscription": True,
                    },
                },
            },
        }, "acct-current")
        self.assertEqual(signals["paid"]["plan_family"], "plus")
        self.assertEqual(signals["paid"]["source"], "backend-api/accounts/check")


class PlanCheckWorkerTests(unittest.TestCase):
    def test_zero_attempt_limit_means_retry_until_result(self):
        timeout, attempts, delay = chatgpt_plan._plan_check_settings(30, 0, 1.5)
        self.assertEqual(timeout, 30)
        self.assertEqual(attempts, 0)
        self.assertEqual(delay, 1.5)

    @patch.object(chatgpt_plan, "BrowserSession")
    @patch.object(chatgpt_plan, "resolve_plan_check_route", return_value={
        "proxy": "",
        "proxy_mode": "request",
        "proxy_source": None,
        "network_route": "direct",
        "proxy_used": None,
        "proxy_fallback_reason": None,
    })
    def test_plan_probe_retries_transient_timeout_until_result(self, _route, session_cls):
        timeout_error = TimeoutError("temporary timeout")
        response = MagicMock(status_code=200, text='{"accounts": {}}')
        response.json.return_value = {"accounts": {}}
        session_cls.return_value.session.get.side_effect = [timeout_error, response]
        token = AccountPlanFilterTests._token("acct-current")
        with patch.object(chatgpt_plan, "parse_accounts_check", return_value={
            "ok": True,
            "checked_at": "now",
            "current_plan_type": "free",
        }), patch.object(chatgpt_plan.time, "sleep"):
            result = chatgpt_plan.check_account_plan(
                token,
                max_attempts=0,
                retry_delay=0,
                fast_mode=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempt_count"], 2)
        self.assertTrue(result["retry_until_result"])
        self.assertEqual(session_cls.return_value.session.get.call_count, 2)

    @patch.object(plan_check_service, "check_account_plan", return_value={"ok": True})
    @patch.object(plan_check_service.detection_proxy, "resolve_detection_proxy", return_value="proxy")
    @patch.object(plan_check_service.detection_proxy, "configured_detection_proxy_spec", return_value="api")
    @patch.object(plan_check_service.detection_proxy, "infer_timezone_offset_min", return_value="-540")
    @patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True)
    @patch.object(plan_check_service.db, "update_account_plan_check")
    def test_account_page_uses_fast_plan_probe_until_result(
        self,
        _update,
        _mark,
        _timezone,
        _configured,
        resolve,
        check,
    ):
        plan_check_service._QUEUE_SLOTS.acquire()
        plan_check_service._run_plan_check(
            account_id=1,
            email="one@example.test",
            access_token="at-test",
            trigger="manual",
            proxy=None,
            timezone_offset_min="-",
        )
        resolve.assert_called_once_with(
            "api",
            api_timeout=4.0,
            api_max_attempts=1,
            validation_timeout=4.0,
            validate=False,
        )
        self.assertTrue(check.call_args.kwargs["fast_mode"])
        self.assertEqual(check.call_args.kwargs["max_attempts"], 0)
        self.assertTrue(callable(check.call_args.kwargs["continue_check"]))
        self.assertTrue(callable(check.call_args.kwargs["retry_proxy_provider"]))

    def test_account_page_polls_proxy_api_until_it_returns_a_proxy(self):
        with patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True), \
             patch.object(plan_check_service.db, "get_account", return_value={"plan_check_status": "running"}), \
             patch.object(plan_check_service.db, "update_account_plan_check"), \
             patch.object(plan_check_service, "_wait_for_rate_slot"), \
             patch.object(plan_check_service.detection_proxy, "resolve_detection_proxy", side_effect=[
                 RuntimeError("first timeout"),
                 RuntimeError("second timeout"),
                 "socks5h://proxy.example:1080",
             ]) as resolve, \
             patch.object(plan_check_service.detection_proxy, "infer_timezone_offset_min", return_value="-540"), \
             patch.object(plan_check_service, "check_account_plan", return_value={"ok": True}), \
             patch.object(plan_check_service.proxy_cfg, "PROXY_API_RETRY_DELAY", 0.25), \
             patch.object(plan_check_service.time, "sleep") as sleep:
            plan_check_service._QUEUE_SLOTS.acquire()
            result = plan_check_service._run_plan_check(
                account_id=1,
                email="one@example.test",
                access_token="at-test",
                trigger="manual",
                proxy="JP|https://api.example.test/white/api?region=JP",
                timezone_offset_min="-",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(resolve.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(0.25)

    def test_proxy_api_polling_stops_when_plan_task_is_no_longer_running(self):
        with patch.object(plan_check_service.db, "get_account", return_value={"plan_check_status": "failed"}), \
             patch.object(plan_check_service.detection_proxy, "resolve_detection_proxy", side_effect=RuntimeError("timeout")) as resolve, \
             patch.object(plan_check_service.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "套餐查询已停止"):
                plan_check_service._resolve_plan_check_proxy(
                    "JP|https://api.example.test/white/api?region=JP",
                    account_id=1,
                )

        resolve.assert_called_once()
        sleep.assert_not_called()

    def test_executor_switches_to_requested_worker_count(self):
        original_workers = plan_check_service.get_executor_workers()
        requested_workers = 4 if original_workers != 4 else 5
        try:
            executor = plan_check_service.get_executor(requested_workers)
            self.assertEqual(plan_check_service.get_executor_workers(), requested_workers)
            self.assertEqual(executor._max_workers, requested_workers)
        finally:
            plan_check_service.get_executor(original_workers)

    def test_plan_check_does_not_run_external_mailbox_detection(self):
        with patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True), \
             patch.object(plan_check_service.db, "update_account_plan_check"), \
             patch.object(plan_check_service.detection_proxy, "configured_detection_proxy_spec", return_value="api"), \
             patch.object(plan_check_service.detection_proxy, "resolve_detection_proxy", return_value="proxy"), \
             patch.object(plan_check_service.detection_proxy, "infer_timezone_offset_min", return_value="-540"), \
             patch.object(plan_check_service, "check_account_plan", return_value={"ok": True, "current_plan_type": "free"}), \
             patch("core.gc_registration_service._mailbox_plus_fallback") as mailbox_fallback:
            plan_check_service._QUEUE_SLOTS.acquire()
            plan_check_service._run_plan_check(
                account_id=1,
                email="one@example.test",
                access_token="at-test",
                trigger="manual",
                proxy=None,
                timezone_offset_min="-",
            )

        mailbox_fallback.assert_not_called()

    def test_worker_count_has_no_fixed_upper_bound(self):
        self.assertEqual(plan_check_service._normalize_workers(0), 1)
        self.assertEqual(plan_check_service._normalize_workers(99), 99)


class PlanCheckBulkApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.plan_check_service.queue_settings", return_value={"workers": 7})
    @patch("webui.app.plan_check_service.enqueue_account_plan_check")
    @patch("webui.app.plan_check_service.get_executor", return_value=sentinel.plan_executor)
    @patch("webui.app.db.get_account")
    def test_bulk_plan_check_uses_requested_workers(
        self,
        get_account,
        get_executor,
        enqueue_account_plan_check,
        _queue_settings,
    ):
        get_account.side_effect = lambda account_id: {
            "id": account_id,
            "email": f"user{account_id}@test.com",
            "access_token": f"token-{account_id}",
        }
        enqueue_account_plan_check.return_value = {"accepted": True, "busy": False}

        response = self.client.post(
            "/api/accounts/check-plan-bulk",
            json={"account_ids": [1, 2], "workers": 7},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["workers"], 7)
        get_executor.assert_called_once_with(max_workers=7)
        self.assertEqual(enqueue_account_plan_check.call_count, 2)
        for call in enqueue_account_plan_check.call_args_list:
            self.assertIs(call.kwargs["executor"], sentinel.plan_executor)


class AccountEmailPoolLinkTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.get_account")
    def test_selected_account_ids_resolve_to_unique_emails(self, get_account):
        get_account.side_effect = lambda account_id: {
            1: {"id": 1, "email": "One@Test.com"},
            2: {"id": 2, "email": "one@test.com"},
        }.get(account_id)

        response = self.client.post(
            "/api/accounts/pool-emails",
            json={"account_ids": [1, 2, 99]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["emails"], ["One@Test.com"])
        self.assertEqual(len(response.get_json()["skipped"]), 1)

    @patch("webui.app.db.list_domain_email_pool", return_value=[])
    @patch("webui.app.db.list_generic_api_email_pool", return_value=[])
    @patch("webui.app.db.list_outlook_pool")
    def test_email_pool_filter_uses_exact_case_insensitive_matches(
        self,
        list_outlook_pool,
        _list_generic_api_email_pool,
        _list_domain_email_pool,
    ):
        list_outlook_pool.return_value = [
            {"email": "one@test.com", "status": "used"},
            {"email": "someone@test.com", "status": "used"},
        ]

        response = self.client.post(
            "/api/outlook/filter-emails",
            json={"emails": ["ONE@Test.com"], "source": "all", "page": 1, "page_size": 20},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["email"], "one@test.com")

    @patch("webui.app.db.list_domain_email_pool", return_value=[])
    @patch("webui.app.db.list_generic_api_email_pool", return_value=[])
    @patch("webui.app.db.list_outlook_pool")
    def test_email_pool_plan_filters_are_disjoint_and_exclude_unregistered(
        self,
        list_outlook_pool,
        _list_generic_api_email_pool,
        _list_domain_email_pool,
    ):
        list_outlook_pool.return_value = [
            {
                "email": "trial@test.com",
                "registered_account_id": 1,
                "current_plan_type": "free",
                "subscription_plan": "chatgptfreeplan",
                "is_free_plan": True,
                "plus_trial_eligible": True,
                "plan_last_success_at": "2026-08-07T12:00:00",
            },
            {
                "email": "plus@test.com",
                "registered_account_id": 2,
                "current_plan_type": "free",
                "subscription_plan": "chatgptplusplan",
                "has_active_subscription": True,
                "has_active_plus_subscription": True,
                "is_free_plan": False,
                "mail_plus_status": "plus",
            },
            {
                "email": "no-trial@test.com",
                "registered_account_id": 3,
                "current_plan_type": "free",
                "subscription_plan": "chatgptfreeplan",
                "is_free_plan": True,
                "plus_trial_eligible": False,
                "plan_last_success_at": "2026-08-07T12:00:00",
            },
            {"email": "unregistered@test.com"},
        ]

        expected = {
            "free": ["trial@test.com"],
            "plus": ["plus@test.com"],
            "nonfree": ["no-trial@test.com"],
        }
        for plan_filter, emails in expected.items():
            with self.subTest(plan_filter=plan_filter):
                response = self.client.get(
                    "/api/outlook",
                    query_string={
                        "paged": 1,
                        "page": 1,
                        "page_size": 20,
                        "source": "all",
                        "plan": plan_filter,
                    },
                )
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["total"], 1)
                self.assertEqual([item["email"] for item in payload["items"]], emails)

    def test_all_email_pool_decorators_copy_account_plan_fields(self):
        account = {
            "id": 7,
            "email": "linked@test.com",
            "current_plan_type": "free",
            "subscription_plan": "chatgptplusplan",
            "has_active_subscription": True,
            "has_active_plus_subscription": True,
            "plan_last_success_at": "2026-08-07T12:00:00",
        }
        account_by_email = {"linked@test.com": account}
        row = {"email": "Linked@Test.com", "status": "used"}

        for decorator in (
            db._decorate_outlook,
            db._decorate_generic_api_email,
            db._decorate_domain_email,
        ):
            with self.subTest(decorator=decorator.__name__):
                decorated = decorator(row, account_by_email)
                self.assertEqual(decorated["registered_account_id"], 7)
                self.assertEqual(decorated["subscription_plan"], "chatgptplusplan")
                self.assertTrue(decorated["has_active_plus_subscription"])
                self.assertEqual(decorated["plan_last_success_at"], "2026-08-07T12:00:00")

    def test_registered_account_overrides_stale_available_pool_status(self):
        account = {"id": 7, "email": "linked@test.com", "created_at": "2026-08-17T10:00:00"}
        row = {"email": "Linked@Test.com", "status": "available", "used_at": None}

        for decorator in (
            db._decorate_outlook,
            db._decorate_generic_api_email,
            db._decorate_domain_email,
        ):
            with self.subTest(decorator=decorator.__name__):
                decorated = decorator(row, {"linked@test.com": account})
                self.assertEqual(decorated["status"], "used")
                self.assertEqual(decorated["used_at"], account["created_at"])

    @patch("webui.app.db.get_account_by_email", return_value={"id": 7, "email": "linked@test.com"})
    def test_registered_email_cannot_be_manually_restored_available(self, _get_account_by_email):
        response = self.client.post(
            "/api/outlook/status",
            json={"email": "linked@test.com", "source": "generic_api", "status": "available"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("不能恢复为可用", response.get_json()["error"])

    @patch("webui.app.db.release_generic_api_email")
    @patch("webui.app.db.get_account_by_email")
    def test_bulk_available_status_skips_registered_email(self, get_account_by_email, release_generic_api_email):
        get_account_by_email.side_effect = lambda email: {"id": 7} if email == "linked@test.com" else None
        response = self.client.post(
            "/api/outlook/status-bulk",
            json={
                "status": "available",
                "items": [
                    {"email": "linked@test.com", "source": "generic_api"},
                    {"email": "unused@test.com", "source": "generic_api"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["updated_count"], 1)
        self.assertEqual(payload["skipped"][0]["email"], "linked@test.com")
        release_generic_api_email.assert_called_once_with("unused@test.com", status="available", note=None)


if __name__ == "__main__":
    unittest.main()
