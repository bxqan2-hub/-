# -*- coding: utf-8 -*-
import json
import base64
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import _parse_rebound_account_lines, create_app


class ReboundAccountImportTests(unittest.TestCase):
    @staticmethod
    def _jwt(email: str, plan: str = "plus") -> str:
        def enc(value):
            return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")
        return ".".join([
            enc({"alg": "none"}),
            enc({"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": plan}, "exp": 4102444800}),
            "signature",
        ])

    def _patch_storage(self, root: Path):
        stack = ExitStack()
        stack.enter_context(patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"))
        stack.enter_context(patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"))
        stack.enter_context(patch.object(db, "_ACCOUNT_GROUPS_JSON", root / "groups.json"))
        stack.enter_context(patch.object(db, "_GENERIC_API_EMAIL_JSON", root / "generic-emails.json"))
        stack.enter_context(patch.object(db, "_GENERIC_API_EMAIL_TXT", root / "generic-emails.txt"))
        stack.enter_context(patch.object(db, "_DOMAIN_EMAIL_JSON", root / "domain-emails.json"))
        stack.enter_context(patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"))
        stack.enter_context(patch.object(db, "_TOKENS_TXT", root / "tokens.txt"))
        stack.enter_context(patch.object(db, "_render_static_viewer", return_value=root / "viewer.html"))
        return stack

    @staticmethod
    def _seed(root: Path):
        root.joinpath("accounts.json").write_text(json.dumps([{
            "id": 7,
            "email": "old@example.com",
            "access_token": "at-old",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "extra_json": json.dumps({"registration_password": "Password!"}),
            "plan_type": "free",
            "at_validity_status": "valid",
            "at_validity_valid": True,
            "original_email_line": "old@example.com",
        }]), encoding="utf-8")
        root.joinpath("groups.json").write_text(json.dumps([
            {"id": "group-1", "name": "分组1", "emails": ["old@example.com"], "created_at": "a", "updated_at": "a"},
            {"id": "group-2", "name": "分组2", "emails": [], "created_at": "b", "updated_at": "b"},
        ]), encoding="utf-8")

    def test_parser_accepts_requested_formats_and_redacts_invalid_secrets(self):
        records, invalid = _parse_rebound_account_lines(
            "old@example.com----new@example.com----Password!----JBSWY3DPEHPK3PXP----at-password\n"
            "api-old@example.com----api-new@example.com----https://mail.example/code?id=7----at-url\n"
            "bad@example.com----secret-value"
        )

        self.assertEqual([record["import_format"] for record in records], [
            "old_new_password_2fa_at",
            "old_new_url_at",
        ])
        self.assertEqual(records[0]["access_token"], "at-password")
        self.assertEqual(records[0]["old_email"], "old@example.com")
        self.assertEqual(records[0]["email"], "new@example.com")
        self.assertEqual(records[1]["old_email"], "api-old@example.com")
        self.assertEqual(records[1]["source_api_url"], "https://mail.example/code?id=7")
        self.assertEqual(len(invalid), 1)
        self.assertNotIn("secret-value", json.dumps(invalid, ensure_ascii=False))

    def test_password_format_matches_only_old_email_and_replaces_result_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            with self._patch_storage(root):
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
                response = client.post("/api/accounts/import-rebound", json={
                    "group_id": "group-2",
                    "text": "old@example.com----new@example.com----ChangedPassword!----GEZDGNBVGY3TQOJQ----at-new",
                })

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["updated_count"], 1)
                self.assertEqual(payload["format_counts"], {"old_new_password_2fa_at": 1})
                self.assertEqual(payload["updated"][0]["match_mode"], "old_email")
                self.assertTrue(payload["updated"][0]["token_replaced"])
                self.assertIsNone(db.get_account_by_email("old@example.com"))
                account = db.get_account_by_email("new@example.com")
                self.assertEqual(account["id"], 7)
                self.assertEqual(account["access_token"], "at-new")
                self.assertEqual(account["totp_secret"], "GEZDGNBVGY3TQOJQ")
                self.assertEqual(json.loads(account["extra_json"])["registration_password"], "ChangedPassword!")
                self.assertEqual(account["at_validity_status"], "unchecked")

    def test_url_format_matches_old_email_not_url_and_keeps_new_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            root.joinpath("generic-emails.json").write_text(json.dumps([{
                "id": 3,
                "email": "old@example.com",
                "code_url": "https://mail.example/code?id=7",
                "status": "used",
            }]), encoding="utf-8")
            with self._patch_storage(root):
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
                response = client.post("/api/accounts/import-rebound", json={
                    "group_id": "group-2",
                    "text": "old@example.com----new@example.com----https://MAIL.example/new-code?id=99----at-new",
                })

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["updated_count"], 1)
                self.assertEqual(payload["format_counts"], {"old_new_url_at": 1})
                self.assertEqual(payload["updated"][0]["match_mode"], "old_email")
                self.assertTrue(payload["updated"][0]["token_replaced"])
                self.assertIsNone(db.get_account_by_email("old@example.com"))
                account = db.get_account_by_email("new@example.com")
                self.assertEqual(account["access_token"], "at-new")
                self.assertEqual(account["at_validity_status"], "unchecked")
                self.assertEqual(account["original_email_line"], "new@example.com----https://MAIL.example/new-code?id=99")
                pool = db.list_generic_api_email_pool(limit=100)
                self.assertEqual([row["email"] for row in pool], ["new@example.com"])
                self.assertEqual(pool[0]["code_url"], "https://MAIL.example/new-code?id=99")
                self.assertEqual(pool[0]["status"], "used")
                copied = client.post("/api/emails/copy-mailbox-lines", json={"account_ids": [7]})
                self.assertEqual(copied.status_code, 200)
                self.assertEqual(copied.get_json()["lines"], [
                    "new@example.com----https://MAIL.example/new-code?id=99"
                ])
                self.assertEqual(copied.get_json()["missing_url_count"], 0)

    def test_copy_mailbox_url_falls_back_to_rebound_account_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            accounts = json.loads(root.joinpath("accounts.json").read_text(encoding="utf-8"))
            accounts[0]["email"] = "new@example.com"
            accounts[0]["original_email_line"] = "new@example.com----https://mail.example/rebound"
            root.joinpath("accounts.json").write_text(json.dumps(accounts), encoding="utf-8")
            with self._patch_storage(root):
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
                response = client.post("/api/emails/copy-mailbox-lines", json={"account_ids": [7]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["lines"], [
                    "new@example.com----https://mail.example/rebound"
                ])
                self.assertEqual(response.get_json()["missing_url_count"], 0)

    def test_url_format_extracts_real_at_from_full_session_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            token = self._jwt("new@example.com")
            session = json.dumps({
                "WARNING_BANNER": "do not share",
                "accessToken": token,
                "user": {"email": "new@example.com"},
                "account": {"planType": "plus"},
            }, separators=(",", ":"))
            with self._patch_storage(root):
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
                response = client.post("/api/accounts/import-rebound", json={
                    "group_id": "group-2",
                    "text": f"old@example.com----new@example.com----https://mail.example/new-code----{session}",
                })
                self.assertEqual(response.status_code, 200)
                account = db.get_account_by_email("new@example.com")
                self.assertEqual(account["access_token"], token)
                self.assertNotIn("WARNING_BANNER", account["access_token"])
                self.assertEqual(account["plan_import_hint"], "plus")
                self.assertEqual(account["plan_import_hint_source"], "api/auth/session")

    def test_existing_rebound_account_is_kept_and_original_account_is_deleted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            accounts = json.loads(root.joinpath("accounts.json").read_text(encoding="utf-8"))
            accounts.append({
                "id": 8,
                "email": "new@example.com",
                "access_token": "at-existing-new",
                "plan_type": "plus",
                "original_email_line": "new@example.com",
            })
            root.joinpath("accounts.json").write_text(json.dumps(accounts), encoding="utf-8")
            with self._patch_storage(root):
                updated, skipped = db.import_rebound_accounts([{
                    "old_email": "old@example.com",
                    "email": "new@example.com",
                    "password": "Password!",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "access_token": "at-replacement",
                }], target_group_id="group-2")

                self.assertEqual(skipped, [])
                self.assertEqual(updated[0]["account_id"], 8)
                self.assertEqual(updated[0]["removed_original_account_id"], 7)
                self.assertIsNone(db.get_account(7))
                kept = db.get_account(8)
                self.assertEqual(kept["email"], "new@example.com")
                self.assertEqual(kept["access_token"], "at-replacement")
                self.assertEqual(kept["plan_type"], "plus")
                self.assertEqual(len(db.list_accounts()), 1)

    def test_import_replaces_identity_and_moves_from_group_one_to_group_two(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            with self._patch_storage(root):
                updated, skipped = db.import_rebound_accounts([{
                    "old_email": "old@example.com",
                    "email": "new@example.com",
                    "password": "Password!",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "access_token": "at-new",
                }], target_group_id="group-2")

                self.assertEqual(skipped, [])
                self.assertEqual(updated[0]["account_id"], 7)
                account = db.get_account(7)
                self.assertEqual(account["email"], "new@example.com")
                self.assertEqual(account["access_token"], "at-new")
                self.assertEqual(account["totp_secret"], "JBSWY3DPEHPK3PXP")
                self.assertEqual(account["plan_type"], "free")
                self.assertEqual(account["email_rebind_label"], "换绑过后的")
                self.assertEqual(root.joinpath("accounts.txt").read_text(encoding="utf-8"), "new@example.com\n")
                groups = {group["id"]: group for group in db.list_account_groups()}
                self.assertEqual(groups["group-1"]["emails"], [])
                self.assertEqual(groups["group-2"]["emails"], ["new@example.com"])

    def test_old_shapes_are_rejected_instead_of_guessing_an_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            with self._patch_storage(root):
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
                response = client.post("/api/accounts/import-rebound", json={
                    "group_id": "group-2",
                    "text": "old@example.com----new@example.com----Password!----JBSWY3DPEHPK3PXP\n"
                            "new@example.com----https://mail.example/code----at-new",
                })
                self.assertEqual(response.status_code, 400)
                payload = response.get_json()
                self.assertFalse(payload["ok"])
                self.assertEqual(len(payload["invalid"]), 2)

    def test_email_api_result_matches_original_account_by_old_email(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            with self._patch_storage(root):
                updated, skipped = db.import_rebound_accounts([{
                    "email": "new@example.com",
                    "old_email": "old@example.com",
                    "source_api_url": "https://mail.example/old-code",
                    "access_token": "at-new",
                }], target_group_id="group-2")

                self.assertEqual(skipped, [])
                self.assertEqual(updated[0]["old_email"], "old@example.com")
                self.assertEqual(db.get_account(7)["email"], "new@example.com")
                groups = {group["id"]: group for group in db.list_account_groups()}
                self.assertEqual(groups["group-1"]["emails"], [])
                self.assertEqual(groups["group-2"]["emails"], ["new@example.com"])

    def test_url_cannot_match_when_first_old_email_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            root.joinpath("generic-emails.json").write_text(json.dumps([{
                "id": 3, "email": "old@example.com", "code_url": "https://mail.example/old-code"
            }]), encoding="utf-8")
            with self._patch_storage(root):
                updated, skipped = db.import_rebound_accounts([{
                    "old_email": "missing@example.com", "email": "new@example.com",
                    "source_api_url": "https://mail.example/old-code", "access_token": "at-new",
                }], target_group_id="group-2")
                self.assertEqual(updated, [])
                self.assertIn("原邮箱", skipped[0]["reason"])

    def test_route_accepts_email_api_subsite_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            with self._patch_storage(root):
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
                response = client.post("/api/accounts/import-rebound", json={
                    "group_id": "group-2",
                    "text": "old@example.com----new@example.com----https://mail.example/new-code----at-new",
                })

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["updated_count"], 1)
                self.assertEqual(payload["updated"][0]["old_email"], "old@example.com")
                self.assertEqual(db.get_account(7)["email"], "new@example.com")

    def test_template_exposes_rebound_import_controls_and_label(self):
        template = Path("webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="btnImportReboundAccountsV2"', template)
        self.assertIn('id="reboundImportModal"', template)
        self.assertIn("换绑过后的", template)
        self.assertIn("/api/accounts/import-rebound", template)
        self.assertIn("原邮箱+换绑后邮箱+密码+2FA+AT", template)
        self.assertIn("原邮箱+换绑后邮箱+换绑后邮箱URL+AT", template)
        self.assertIn("两种格式都只按第一段原邮箱查询", template)
        self.assertIn("删除账号列表中的原邮箱", template)


if __name__ == "__main__":
    unittest.main()
