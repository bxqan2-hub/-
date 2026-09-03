# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class GPTMailWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_gptmail_without_api_key_before_creating_tasks(self, submit_registration):
        submit_registration.return_value = []
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail"
        ), patch.object(email_config, "GPTMAIL_API_KEY", ""):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("请填写 GPTMail API Key", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_gptmail_key_does_not_check_outlook_pool(self, submit_registration, outlook_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail"
        ), patch.object(email_config, "GPTMAIL_API_KEY", "key-123"):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_default_to_one_worker(self, submit_registration, outlook_pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail"
        ), patch.object(email_config, "GPTMAIL_API_KEY", "key-123"):
            response = self.client.post("/api/jobs", json={"count": 1})

        self.assertEqual(response.status_code, 200)
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_clamp_registration_page_worker_count_for_roxy(self, submit_registration, outlook_pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail"
        ), patch.object(email_config, "GPTMAIL_API_KEY", "key-123"):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 50})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["workers"], 2)
        self.assertIn("冷启动并发", response.get_json()["warning"])
        submit_registration.assert_called_once_with(count=1, workers=2)

    @patch("webui.app.db.generic_api_email_pool_summary", return_value={"total": 2, "available": 2, "used": 0, "failed": 0})
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_can_select_inbox_mate_per_registration(self, submit_registration, pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "generic_api"
        ):
            response = self.client.post(
                "/api/jobs",
                json={"count": 1, "workers": 2, "email_source": "inbox_mate"},
            )

        self.assertEqual(response.status_code, 200)
        pool_summary.assert_called_once_with(provider="inbox_mate")
        submit_registration.assert_called_once_with(
            count=1,
            workers=2,
            email_source="inbox_mate",
        )

    @patch("webui.app.svc.submit_registration")
    def test_jobs_reject_invalid_selected_email_source(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            response = self.client.post(
                "/api/jobs",
                json={"count": 1, "email_source": "unknown_pool"},
            )

        self.assertEqual(response.status_code, 400)
        submit_registration.assert_not_called()

    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_can_select_local_proxy_mode(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "generic_api"
        ):
            response = self.client.post(
                "/api/jobs",
                json={"count": 1, "workers": 1, "proxy_mode": "local"},
            )

        self.assertEqual(response.status_code, 200)
        submit_registration.assert_called_once_with(count=1, workers=1, proxy_mode="local")

    @patch("webui.app.svc.submit_registration", return_value=[{
        "id": 1,
        "email_source": "outlook",
        "email": "chosen@mail.com",
    }])
    def test_jobs_pushes_selected_pool_emails_without_random_source_validation(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False):
            response = self.client.post(
                "/api/jobs",
                json={
                    "workers": 2,
                    "email_items": [{"source": "outlook", "email": "chosen@mail.com"}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["submitted"], 1)
        self.assertEqual(response.get_json()["requested_count"], 1)
        self.assertEqual(response.get_json()["skipped"], [])
        submit_registration.assert_called_once_with(
            workers=2,
            email_items=[{"source": "outlook", "email": "chosen@mail.com"}],
        )

    @patch("webui.app.svc.submit_registration", return_value=[{
        "id": 1,
        "email_source": "outlook",
        "email": "available@mail.com",
    }])
    def test_jobs_reports_each_selected_email_that_was_not_submitted(self, submit_registration):
        response = self.client.post(
            "/api/jobs",
            json={
                "workers": 2,
                "email_items": [
                    {"source": "outlook", "email": "available@mail.com"},
                    {"source": "outlook", "email": "used@mail.com"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["submitted"], 1)
        self.assertEqual(payload["requested_count"], 2)
        self.assertEqual(payload["skipped"], [{
            "source": "outlook",
            "email": "used@mail.com",
            "reason": "邮箱已被领取、不可用或来源不匹配",
        }])

    @patch("webui.app.db.domain_email_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.db.count_accounts", return_value=0)
    def test_summary_does_not_count_gptmail_as_outlook_pool(self, count_accounts, outlook_pool_summary, domain_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "EMAIL_SOURCE", "gptmail"):
            response = self.client.get("/api/summary")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["outlook_total"], 0)
        outlook_pool_summary.assert_not_called()

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_mailnest_without_api_key_before_creating_tasks(self, submit_registration):
        submit_registration.return_value = []
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "mailnest"
        ), patch.object(email_config, "MAIL_NEST_API_KEY", "", create=True), patch.object(
            email_config, "MAIL_NEST_PROJECT_CODE", "chatgpt001", create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("MailNest API Key", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_mailnest_key_does_not_check_outlook_pool(self, submit_registration, outlook_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "mailnest"
        ), patch.object(email_config, "MAIL_NEST_API_KEY", "key-123", create=True), patch.object(
            email_config, "MAIL_NEST_PROJECT_CODE", "chatgpt001", create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("webui.app.svc.submit_registration")
    def test_jobs_allows_cloudmail_without_manual_domains(self, submit_registration):
        submit_registration.return_value = []
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "cloudmail"
        ), patch.object(email_config, "CLOUDMAIL_API_BASE", "https://mail.example.com", create=True), patch.object(
            email_config, "CLOUDMAIL_AUTH_TOKEN", "token", create=True
        ), patch.object(email_config, "CLOUDMAIL_DOMAINS", [], create=True):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_cloudmail_config_does_not_check_outlook_pool(self, submit_registration, outlook_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "cloudmail"
        ), patch.object(email_config, "CLOUDMAIL_API_BASE", "https://mail.example.com", create=True), patch.object(
            email_config, "CLOUDMAIL_AUTH_TOKEN", "token", create=True
        ), patch.object(email_config, "CLOUDMAIL_DOMAINS", ["example.com"], create=True):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1)
