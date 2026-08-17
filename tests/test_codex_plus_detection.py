# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class CodexPlanApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.plan_check_service.enqueue_account_plan_check")
    @patch("webui.app.db.get_account")
    def test_single_check_uses_account_at(self, get_account, enqueue):
        get_account.return_value = {"id": 7, "email": "plus@example.com", "access_token": "web-at"}
        enqueue.return_value = {"accepted": True, "busy": False}

        response = self.client.post("/api/accounts/check-plan", json={"account_id": 7})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(enqueue.call_args.kwargs["access_token"], "web-at")

    @patch("webui.app.db.move_accounts_to_default_group")
    def test_move_accounts_to_default_group(self, move_default):
        move_default.return_value = ({"id": "default", "name": "默认组", "count": 2}, [])

        response = self.client.post(
            "/api/account-groups/default/members",
            json={"account_ids": [7, 8]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["group"]["id"], "default")
        move_default.assert_called_once_with([7, 8])

    @patch("webui.app.plan_check_service.enqueue_account_plan_check")
    @patch("webui.app.db.get_account")
    def test_single_check_rejects_account_without_at(self, get_account, enqueue):
        get_account.return_value = {"id": 7, "email": "missing@example.com", "access_token": ""}

        response = self.client.post("/api/accounts/check-plan", json={"account_id": 7})

        self.assertEqual(response.status_code, 400)
        self.assertIn("缺少 AT", response.get_json()["error"])
        enqueue.assert_not_called()

if __name__ == "__main__":
    unittest.main()
