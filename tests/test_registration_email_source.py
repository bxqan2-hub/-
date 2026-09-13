# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import ANY, MagicMock, patch

from config import email as email_config
from config import register as register_config
from core import account_security_service, registration_service


class RegistrationEmailSourceTests(unittest.TestCase):
    @patch.object(registration_service, "is_stop_requested", return_value=False)
    @patch.object(registration_service.db, "update_job")
    @patch.object(registration_service.db, "get_job", side_effect=[{"id": 17}, RuntimeError("db unavailable")])
    @patch.object(registration_service.db, "start_pending_job", return_value=None)
    def test_startup_skip_lookup_error_cleans_thread_context(
        self, _start_pending_job, _get_job, _update_job, _is_stop_requested
    ):
        registration_service._run_one_job(17, "registration.log")

        self.assertIsNone(registration_service.current_job_id())
        self.assertNotIn(17, registration_service._ACTIVE_JOBS)
        self.assertNotIn(17, registration_service._STOP_EVENTS)

    @patch.object(registration_service.codex_retry_service, "release")
    @patch.object(registration_service.db, "update_job")
    @patch.object(registration_service.db, "get_job", side_effect=RuntimeError("db unavailable"))
    def test_codex_retry_startup_database_error_releases_reservation_and_context(
        self, _get_job, _update_job, release
    ):
        registration_service._run_codex_retry_job(17, "registration.log", "user@example.com", 3)

        release.assert_called_once_with("user@example.com")
        self.assertIsNone(registration_service.current_job_id())
        self.assertNotIn(17, registration_service._ACTIVE_JOBS)
        self.assertNotIn(17, registration_service._STOP_EVENTS)

    @patch.object(registration_service.codex_retry_service, "release")
    @patch.object(registration_service.codex_retry_service, "run_worker")
    @patch.object(registration_service.db, "update_job", side_effect=[None, RuntimeError("write failed"), None])
    @patch.object(registration_service.db, "get_job", return_value={"id": 17, "status": "pending"})
    def test_codex_result_write_error_does_not_release_new_reservation(
        self, _get_job, update_job, run_worker, release
    ):
        # Model run_worker handing off its reservation and a newer retry reserving
        # the same email before result persistence fails.
        def worker_side_effect(*args, **kwargs):
            release("user@example.com")
            return {"ok": True, "status": "success"}

        run_worker.side_effect = worker_side_effect
        registration_service._run_codex_retry_job(17, "registration.log", "user@example.com", 3)

        release.assert_called_once_with("user@example.com")
        self.assertIsNone(registration_service.current_job_id())
        self.assertNotIn(17, registration_service._ACTIVE_JOBS)
        self.assertNotIn(17, registration_service._STOP_EVENTS)

    @patch("config.proxy.pick_local_proxy", return_value="http://127.0.0.1:7890")
    def test_local_proxy_mode_uses_static_or_system_pool(self, pick_local_proxy):
        self.assertEqual(
            registration_service._resolve_registration_proxy("local"),
            "http://127.0.0.1:7890",
        )
        pick_local_proxy.assert_called_once_with()

    @patch("config.proxy.pick_local_proxy", return_value="")
    def test_local_proxy_mode_fails_closed_without_local_proxy(self, pick_local_proxy):
        with self.assertRaisesRegex(RuntimeError, "本地代理模式"):
            registration_service._resolve_registration_proxy("local")

    @patch("core.profile_utils.generate_random_birthday", return_value="1992-03-04")
    @patch("core.registration_service._random_display_name", return_value="Example User")
    @patch("core.email_provider.acquire_email", return_value="selected@mail.com")
    def test_prepare_registration_args_uses_job_email_source(
        self,
        acquire_email,
        _random_display_name,
        _generate_random_birthday,
    ):
        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", ""
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            result = registration_service._prepare_registration_args(email_source="inbox_mate")

        self.assertEqual(result, ("selected@mail.com", "Example User", "1992-03-04"))
        acquire_email.assert_called_once_with(email_source="inbox_mate")

    @patch("core.profile_utils.generate_random_birthday", return_value="1992-03-04")
    @patch("core.registration_service._random_display_name", return_value="Example User")
    @patch("core.email_provider.acquire_email")
    def test_prepare_registration_args_uses_selected_email_without_reacquiring(
        self,
        acquire_email,
        _random_display_name,
        _generate_random_birthday,
    ):
        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", ""
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            result = registration_service._prepare_registration_args(
                email_source="outlook",
                email_override="chosen@mail.com",
            )

        self.assertEqual(result, ("chosen@mail.com", "Example User", "1992-03-04"))
        acquire_email.assert_not_called()

    @patch.object(registration_service.db, "get_job", return_value={"id": 17, "log_file": "registration.log"})
    @patch.object(registration_service.db, "create_job", return_value={"id": 17, "log_file": "registration.log"})
    @patch.object(registration_service.db, "claim_email", return_value={"email": "chosen@mail.com"})
    @patch.object(registration_service, "get_executor_workers", return_value=2)
    @patch.object(registration_service, "get_executor")
    def test_submit_registration_binds_each_selected_email_to_its_job(
        self,
        get_executor,
        _get_executor_workers,
        claim_email,
        create_job,
        _get_job,
    ):
        executor = MagicMock()
        get_executor.return_value = executor

        jobs = registration_service.submit_registration(
            workers=2,
            email_items=[
                {"source": "outlook", "email": "chosen@mail.com"},
                {"source": "outlook", "email": "chosen@mail.com"},
            ],
        )

        self.assertEqual(len(jobs), 1)
        claim_email.assert_called_once_with("chosen@mail.com", "outlook")
        create_job.assert_called_once_with(
            email_source="outlook",
            email="chosen@mail.com",
            gc_mode=ANY,
            proxy_mode=None,
        )
        executor.submit.assert_called_once_with(registration_service._run_one_job, 17, "registration.log")
        executor.submit.return_value.add_done_callback.assert_called_once()


class RegistrationSecurityRetryTests(unittest.TestCase):
    def setUp(self):
        self.job = {"id": 17, "status": "failed", "account_id": 700, "email": "retry@example.com"}
        self.account = {
            "id": 700, "email": "retry@example.com", "codex_status": "failed",
            "access_token": "private-token", "totp_secret": "",
            "extra_json": json.dumps({"twofa": {"status": "failed", "error": {
                "stage": "password_email", "code": "password_email_reauth_submit_failed",
                "message": "private-error-detail",
            }}}),
        }
        for owner, name, value in (
            (registration_service.db, "get_account", self.account),
            (registration_service.db, "get_job", self.job),
            (registration_service.db, "get_successful_retry_for_job", None),
            (registration_service.db, "create_retry_job", ({"id": 18, "log_file": "retry.log"}, True)),
            (registration_service, "get_executor", MagicMock()),
            (registration_service.codex_retry_service, "is_retrying", False),
            (account_security_service, "enqueue_account_security_setup", {"accepted": True}),
        ):
            patcher = patch.object(owner, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_latest_seven_failures_prioritize_security_even_after_successful_codex_retry(self):
        for account_id in (700, 701, 702, 704, 705, 707, 708):
            with self.subTest(account_id=account_id):
                self.account["id"] = account_id
                self.job["account_id"] = account_id
                extra = json.loads(self.account["extra_json"])
                extra["twofa"]["error"]["code"] = (
                    "password_email_reauth_not_advanced" if account_id == 701
                    else "password_email_reauth_submit_failed"
                )
                self.account["extra_json"] = json.dumps(extra)
                self.account["codex_status"] = "success"
                self.get_successful_retry_for_job.return_value = {"id": 19, "status": "success"}
                info = registration_service.get_retry_info(self.job)
                self.assertTrue(info["retryable"])
                self.assertEqual(info["retry_action"], "security")
                self.assertEqual(info["display_status"], "failed")
                self.assertNotIn("private-", json.dumps(info))

    def test_missing_password_or_totp_with_requested_security_still_requires_setup(self):
        for password, secret in (("Confirmed-password", ""), ("", "Confirmed-secret")):
            with self.subTest(password_saved=bool(password)):
                self.account["extra_json"] = {"registration_password": password, "twofa": {"status": "partial_success"}}
                self.account["totp_secret"] = secret
                self.assertEqual(registration_service.get_retry_info(self.job)["retry_action"], "security")
        self.account["extra_json"] = "{}"
        self.account["security_setup_status"] = "partial"
        self.assertEqual(registration_service.get_retry_info(self.job)["retry_action"], "security")

    def test_legacy_and_explicitly_skipped_security_keep_codex_retry(self):
        for extra in (None, "{}", "malformed", "[]", '{"twofa":{"status":"skipped"}}'):
            with self.subTest(extra=extra):
                self.account["extra_json"] = extra
                info = registration_service.get_retry_info(self.job)
                self.assertEqual(info["retry_action"], "codex")
                self.assertEqual(info["display_status"], "partial_success")

    def test_email_fallback_does_not_match_account_created_by_later_registration(self):
        # A failed job without account_id can share its mailbox with a later
        # successful registration.  The later account must not rewrite the
        # old job as "security complete".
        later_account = {
            **self.account,
            "id": 701,
            "email": "retry@example.com",
            "created_at": "2026-09-13T19:11:13",
            "totp_secret": "Confirmed-secret",
            "extra_json": json.dumps({
                "registration_password": "Confirmed-password",
                "twofa": {"status": "success"},
                "codex": {"status": "skipped", "ok": True},
            }),
        }
        failed_job = {
            "id": 240,
            "status": "failed",
            "account_id": None,
            "email": "retry@example.com",
            "started_at": "2026-09-13T19:03:09",
            "completed_at": "2026-09-13T19:08:36",
        }
        with patch.object(registration_service.db, "get_account", return_value=None), \
             patch.object(registration_service.db, "get_account_by_email", return_value=later_account), \
             patch.object(registration_service.db, "get_successful_retry_for_job", return_value=None):
            info = registration_service.get_retry_info(failed_job)
        self.assertEqual(info["display_status"], "failed")
        self.assertEqual(info["retry_action"], "registration")
        self.assertNotIn("密码/2FA 已完成", info.get("retry_reason") or "")

    def test_active_security_setup_prevents_duplicate_submission(self):
        for status in ("queued", "running"):
            with self.subTest(status=status):
                self.account["security_setup_status"] = status
                info = registration_service.get_retry_info(self.job)
                self.assertFalse(info["retryable"])
                self.assertIn("账号页", info["retry_reason"])
                result = registration_service.retry_job(17)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], 409)
        self.enqueue_account_security_setup.assert_not_called()
        self.create_retry_job.assert_not_called()

    def test_active_codex_status_or_reservation_blocks_security_retry(self):
        for status, reserved in (("retrying", False), ("failed", True)):
            with self.subTest(status=status, reserved=reserved):
                self.account["codex_status"] = status
                self.is_retrying.return_value = reserved
                info = registration_service.get_retry_info(self.job)
                self.assertFalse(info["retryable"])
                self.assertIn("正在补跑 Codex", info["retry_reason"])
                result = registration_service.retry_job(17)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], 409)
        self.enqueue_account_security_setup.assert_not_called()
        self.create_retry_job.assert_not_called()

    def test_security_submission_rechecks_codex_after_retry_info(self):
        self.is_retrying.side_effect = [False, True]
        result = registration_service.retry_job(17)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 409)
        self.is_retrying.side_effect = None
        self.get_account.side_effect = [dict(self.account), {**self.account, "codex_status": "retrying"}]
        result = registration_service.retry_job(17)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 409)
        self.enqueue_account_security_setup.assert_not_called()
        self.create_retry_job.assert_not_called()
        self.get_executor.assert_not_called()

    def test_confirmed_credentials_override_historical_security_error(self):
        self.account["extra_json"] = json.dumps({
            "chatgpt_password": "Confirmed-password",
            "twofa": {"status": "failed", "error": {"code": "totp_token_validation_failed"}},
        })
        self.account["totp_secret"] = "Confirmed-secret"
        self.account["security_setup_status"] = "success"
        info = registration_service.get_retry_info(self.job)
        self.assertEqual(info["retry_action"], "codex")
        self.assertEqual(info["display_status"], "partial_success")
        self.account["codex_status"] = "success"
        info = registration_service.get_retry_info(self.job)
        self.assertFalse(info["retryable"])
        self.assertEqual(info["display_status"], "success")
        self.get_successful_retry_for_job.return_value = {"id": 19, "status": "success"}
        self.assertEqual(registration_service.get_retry_info(self.job)["display_status"], "success")

    def test_completed_security_respects_skipped_codex_without_changing_legacy_routing(self):
        self.account["totp_secret"] = "Confirmed-secret"
        for current, saved in (("skipped", {}), ("", {"status": "skipped"}), ("", {"ok": True})):
            with self.subTest(current=current, saved=saved):
                self.account["codex_status"] = current
                self.account["extra_json"] = json.dumps({
                    "registration_password": "Confirmed-password", "codex": saved,
                    "twofa": {"status": "failed"},
                })
                info = registration_service.get_retry_info(self.job)
                self.assertFalse(info["retryable"])
                self.assertEqual(info["display_status"], "success")
        self.account["codex_status"] = "failed"
        self.assertEqual(registration_service.get_retry_info(self.job)["retry_action"], "codex")
        self.account["codex_status"] = "skipped"
        self.account["extra_json"] = "{}"
        self.assertEqual(registration_service.get_retry_info(self.job)["retry_action"], "codex")

    def test_deactivated_account_does_not_enqueue_security_setup(self):
        self.account["codex_status"] = "deactivated"
        self.assertFalse(registration_service.retry_job(17)["ok"])
        self.enqueue_account_security_setup.assert_not_called()

    def test_security_retry_reuses_existing_worker_and_returns_no_credentials(self):
        self.job["access_token"] = "private-job-token"
        result = registration_service.retry_job(17, workers=10)
        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        self.assertFalse(result["reused"])
        self.assertEqual(result["source_job_id"], 17)
        self.assertEqual(result["job"], {"id": 17, "status": "failed", "account_id": 700})
        self.assertEqual(result["retry_action"], "security")
        self.assertEqual(result["account_id"], 700)
        self.assertNotIn("private-", json.dumps(result))
        self.enqueue_account_security_setup.assert_called_once_with(
            account_id=700, password_mode="add", trigger="registration_retry",
        )
        self.create_retry_job.assert_not_called()
        self.get_executor.assert_not_called()

    def test_security_claim_race_and_queue_full_are_reported_without_new_registration(self):
        for rejection, status in (({"busy": True}, 409), ({"queue_full": True}, 429)):
            with self.subTest(status=status):
                self.enqueue_account_security_setup.return_value = {"accepted": False, **rejection}
                result = registration_service.retry_job(17)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], status)
        self.create_retry_job.assert_not_called()
        self.get_executor.assert_not_called()

    def test_codex_retry_keeps_existing_executor_and_payload(self):
        self.account["extra_json"] = "{}"
        with patch.object(registration_service.codex_retry_service, "reserve", return_value=True), patch.object(
            registration_service.db, "update_account_codex_status"
        ):
            result = registration_service.retry_job(17)
        self.assertEqual(result["retry_action"], "codex")
        self.assertEqual(self.create_retry_job.call_args.kwargs["job_type"], "codex_retry")
        self.get_executor.return_value.submit.assert_called_once_with(
            registration_service._run_codex_retry_job, 18, "retry.log", "retry@example.com", 700,
        )
        self.enqueue_account_security_setup.assert_not_called()

    def test_retry_api_single_bulk_and_compact_jobs_support_security_action(self):
        from webui.app import create_app

        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        response = client.post("/api/jobs/17/retry", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["retry_action"], "security")
        response = client.post("/api/jobs/retry-bulk", json={"job_ids": [17, 17]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["started_count"], 1)
        self.assertEqual(response.get_json()["started"][0]["retry_action"], "security")
        with patch.object(registration_service.db, "list_jobs", return_value=[dict(self.job)]):
            payload = client.get("/api/jobs?paged=1").get_json()
        row = payload["items"][0]
        self.assertEqual(row["display_status"], "failed")
        self.assertEqual(row["retry_action"], "security")
        self.assertIn("密码/2FA", row["retry_reason"])
        self.assertNotIn("private-", json.dumps(payload))
        html = client.get("/").get_data(as_text=True)
        self.assertIn("job.retry_action === 'security'", html)
        self.assertIn("安全设置未完成的账号优先补密码/2FA", html)


if __name__ == "__main__":
    unittest.main()
