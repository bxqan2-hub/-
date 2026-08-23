# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db, plan_check_service
from webui.app import _parse_manual_account_lines, create_app


class ManualAccountAddTests(unittest.TestCase):
    def _patch_storage(self, root: Path):
        stack = ExitStack()
        paths = {
            "_ACCOUNTS_JSON": "accounts.json",
            "_LEGACY_ACCOUNTS_JSON": "legacy-accounts.json",
            "_OUTLOOK_JSON": "outlook.json",
            "_LEGACY_OUTLOOK_JSON": "legacy-outlook.json",
            "_GENERIC_API_EMAIL_JSON": "generic.json",
            "_DOMAIN_EMAIL_JSON": "domain.json",
            "_ACCOUNT_GROUPS_JSON": "groups.json",
            "_SECURITY_CHECKPOINTS_JSON": "security.json",
            "_SECURITY_CHECKPOINTS_LOCK": "security.lock",
            "_ACCOUNTS_TXT": "accounts.txt",
            "_TOKENS_TXT": "tokens.txt",
            "_OUTLOOK_TXT": "outlook.txt",
            "_GENERIC_API_EMAIL_TXT": "generic.txt",
        }
        for name, filename in paths.items():
            stack.enter_context(patch.object(db, name, root / filename))
        stack.enter_context(patch.object(db, "_render_static_viewer", return_value=root / "viewer.html"))
        return stack

    def test_parser_accepts_both_formats_and_never_echoes_credentials_on_error(self):
        records, invalid = _parse_manual_account_lines(
            "url@example.com----https://mail.example/code?id=1----at-url\n"
            "password@example.com====OpenAI-Password====JBSWY3DPEHPK3PXP====at-password\n"
            "broken-line"
        )

        self.assertEqual([record["mode"] for record in records], ["email_url", "password_2fa"])
        self.assertEqual(records[0]["access_token"], "at-url")
        self.assertEqual(records[1]["password"], "OpenAI-Password")
        self.assertEqual(invalid, [{"line": 3, "reason": "邮箱格式无效"}])
        self.assertNotIn("broken-line", json.dumps(invalid, ensure_ascii=False))

    def test_route_persists_both_account_types_and_queues_supplied_at(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._patch_storage(root):
                group = db.create_account_group("手动账号")
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
                executor = object()
                with patch.object(plan_check_service, "get_executor_workers", return_value=10), \
                     patch.object(plan_check_service, "get_executor", return_value=executor), \
                     patch.object(
                         plan_check_service,
                         "enqueue_account_plan_check",
                         side_effect=lambda **kwargs: {"accepted": True, "busy": False, "message": "queued"},
                     ) as enqueue:
                    response = client.post("/api/accounts/manual-add", json={
                        "group_id": group["id"],
                        "check_plan": True,
                        "workers": 7,
                        "text": (
                            "url@example.com----https://mail.example/code?id=1----at-url-secret\n"
                            "password@example.com====OpenAI-Password====JBSWY3DPEHPK3PXP====at-password-secret"
                        ),
                    })

                self.assertEqual(response.status_code, 202)
                payload = response.get_json()
                self.assertEqual(payload["added_count"], 2)
                self.assertEqual(payload["started_count"], 2)
                self.assertEqual(payload["workers"], 7)
                serialized = json.dumps(payload, ensure_ascii=False)
                for secret in ("at-url-secret", "at-password-secret", "OpenAI-Password", "JBSWY3DPEHPK3PXP"):
                    self.assertNotIn(secret, serialized)

                queued = {call.kwargs["email"]: call.kwargs for call in enqueue.call_args_list}
                self.assertEqual(queued["url@example.com"]["access_token"], "at-url-secret")
                self.assertEqual(queued["password@example.com"]["access_token"], "at-password-secret")
                self.assertIs(queued["url@example.com"]["executor"], executor)
                self.assertEqual(queued["url@example.com"]["trigger"], "manual_add")

                url_account = db.get_account_by_email("url@example.com")
                password_account = db.get_account_by_email("password@example.com")
                self.assertEqual(url_account["access_token"], "at-url-secret")
                self.assertEqual(url_account["at_validity_status"], "unchecked")
                self.assertEqual(password_account["totp_secret"], "JBSWY3DPEHPK3PXP")
                raw_accounts = json.loads((root / "accounts.json").read_text(encoding="utf-8"))
                raw_password = next(row for row in raw_accounts if row["email"] == "password@example.com")
                self.assertEqual(json.loads(raw_password["extra_json"])["registration_password"], "OpenAI-Password")
                generic_rows = json.loads((root / "generic.json").read_text(encoding="utf-8"))
                self.assertEqual(generic_rows[0]["code_url"], "https://mail.example/code?id=1")
                self.assertEqual(generic_rows[0]["status"], "used")
                stored_group = next(item for item in db.list_account_groups() if item["id"] == group["id"])
                self.assertEqual(set(stored_group["emails"]), {"url@example.com", "password@example.com"})

    def test_template_exposes_top_button_modal_formats_and_immediate_plan_option(self):
        template = Path("webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="btnManualAddAccountV2"', template)
        self.assertIn('id="manualAccountAddModal"', template)
        self.assertIn('id="manualAccountCheckPlanV2" type="checkbox" checked', template)
        self.assertIn("邮箱----取码URL----AT", template)
        self.assertIn("邮箱----密码----2FA----AT", template)
        self.assertIn("/api/accounts/manual-add", template)


if __name__ == "__main__":
    unittest.main()
