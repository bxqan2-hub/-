import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import checkout_kind_service, db, registration_service
from webui.app import create_app


class CheckoutKindDetectionTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.checkout_kind_service.enqueue")
    @patch("webui.app.checkout_kind_service.get_executor")
    @patch("webui.app.db.get_account")
    def test_bulk_route_uses_saved_at_without_returning_it(self, get_account, get_executor, enqueue):
        get_account.return_value = {
            "id": 7, "email": "oaics@example.com", "access_token": "secret-account-at",
        }
        executor = object()
        get_executor.return_value = executor
        enqueue.return_value = {"accepted": True, "busy": False, "status": "queued"}

        response = self.client.post(
            "/api/accounts/check-checkout-kind-bulk",
            json={"account_ids": [7], "workers": 4},
        )

        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once_with(
            account_id=7,
            access_token="secret-account-at",
            trigger="manual_bulk",
            proxy=None,
            executor=executor,
        )
        self.assertFalse(response.get_json()["confirm_sent"])
        self.assertNotIn("secret-account-at", response.get_data(as_text=True))

    def test_result_persistence_exposes_classification_not_token_or_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            accounts = root / "accounts.json"
            accounts.write_text(
                '[{"id":7,"email":"oaics@example.com","access_token":"secret-at"}]',
                encoding="utf-8",
            )
            with patch.object(db, "_ACCOUNTS_JSON", accounts), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"):
                self.assertTrue(db.claim_account_checkout_kind(7))
                self.assertTrue(db.mark_account_checkout_kind_running(7))
                self.assertTrue(db.update_account_checkout_kind(7, {
                    "ok": True,
                    "kind": "oaics",
                    "checkout_provider": "open_ai",
                    "processor_entity": "openai_ie",
                    "session_prefix": "oaics_",
                    "confirm_sent": False,
                    "checkout_url": "https://should-not-be-persisted.example",
                    "access_token": "should-not-be-persisted",
                }))
                saved = db.get_account(7)

        self.assertEqual(saved["checkout_kind"], "oaics")
        self.assertEqual(saved["checkout_kind_session_prefix"], "oaics_")
        self.assertFalse(saved["checkout_kind_confirm_sent"])
        self.assertNotIn("checkout_url", saved)
        self.assertNotEqual(saved["access_token"], "should-not-be-persisted")

    @patch("core.checkout_kind_service.get_pay153_module")
    def test_pay153_adapter_marks_no_confirm(self, get_pay153_module):
        get_pay153_module.return_value.detect_checkout_kind.return_value = ({
            "ok": True, "kind": "cs_live", "session_prefix": "cs_live_",
            "confirm_sent": False,
        }, 200)

        result = checkout_kind_service.check_checkout_kind("account-at", proxy="http://127.0.0.1:8080")

        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "cs_live")
        self.assertFalse(result["confirm_sent"])
        body = get_pay153_module.return_value.detect_checkout_kind.call_args.args[0]
        self.assertEqual(body["token"], "account-at")
        self.assertEqual(body["proxy"], "http://127.0.0.1:8080")

    @patch("core.checkout_kind_service.get_pay153_module")
    def test_pay153_adapter_reports_in_process_exception(self, get_pay153_module):
        get_pay153_module.side_effect = RuntimeError("load failed")

        result = checkout_kind_service.check_checkout_kind("account-at", proxy="")

        self.assertFalse(result["ok"])
        self.assertIn("检测执行异常", result["error"])
        self.assertFalse(result["confirm_sent"])

    @patch("core.checkout_kind_service.enqueue")
    @patch("main.run_registration")
    @patch.object(registration_service, "_JobLogContext")
    @patch.object(
        registration_service,
        "_prepare_registration_args",
        return_value=("user@example.com", "Test User", "1990-01-01"),
    )
    @patch.object(registration_service, "check_stop_requested")
    @patch.object(registration_service, "is_stop_requested", return_value=False)
    @patch.object(registration_service, "current_gc_mode", return_value=False)
    @patch.object(registration_service, "_activate_job")
    @patch.object(registration_service, "_deactivate_job")
    @patch.object(registration_service.db, "update_job")
    @patch.object(
        registration_service.db,
        "start_pending_job",
        return_value={"id": 9, "status": "running", "email_source": "outlook"},
    )
    @patch.object(
        registration_service.db,
        "get_job",
        return_value={"id": 9, "status": "queued"},
    )
    def test_registration_success_waits_for_manual_checkout_kind_check(
        self,
        get_job,
        start_pending_job,
        update_job,
        deactivate_job,
        activate_job,
        current_gc_mode,
        is_stop_requested,
        check_stop_requested,
        prepare_registration_args,
        job_log_context,
        run_registration,
        enqueue,
    ):
        run_registration.return_value = {
            "success": True,
            "account_id": 17,
            "email": "user@example.com",
            "access_token": "registration-at",
        }
        job_log_context.return_value.__enter__.return_value = job_log_context.return_value

        registration_service._run_one_job(9, "registration.log")

        enqueue.assert_not_called()
        self.assertTrue(
            any(call.kwargs.get("status") == "success" for call in update_job.call_args_list)
        )

    @patch("webui.app.db.list_accounts")
    def test_accounts_route_filters_oaics_checkout_kind(self, list_accounts):
        list_accounts.return_value = [
            {"id": 1, "email": "oaics@example.com", "checkout_kind": "oaics"},
            {"id": 2, "email": "live@example.com", "checkout_kind": "cs_live"},
        ]

        response = self.client.get(
            "/api/accounts?paged=1&page=1&page_size=20&checkout_kind=oaics"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual([item["email"] for item in payload["items"]], ["oaics@example.com"])


if __name__ == "__main__":
    unittest.main()
