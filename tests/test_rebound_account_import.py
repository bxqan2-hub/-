# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import _parse_rebound_account_lines, create_app


class ReboundAccountImportTests(unittest.TestCase):
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
            "old@example.com----new@example.com----Password!----JBSWY3DPEHPK3PXP\n"
            "api-new@example.com----https://mail.example/code?id=7----at-new\n"
            "bad@example.com----secret-value"
        )

        self.assertEqual([record["import_format"] for record in records], [
            "old_new_password_2fa",
            "new_url_at",
        ])
        self.assertNotIn("access_token", records[0])
        self.assertEqual(records[0]["old_email"], "old@example.com")
        self.assertEqual(records[0]["email"], "new@example.com")
        self.assertEqual(records[1]["source_api_url"], "https://mail.example/code?id=7")
        self.assertEqual(len(invalid), 1)
        self.assertNotIn("secret-value", json.dumps(invalid, ensure_ascii=False))

    def test_requested_old_new_password_2fa_replaces_email_without_replacing_at(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            with self._patch_storage(root):
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
                response = client.post("/api/accounts/import-rebound", json={
                    "group_id": "group-2",
                    "text": "old@example.com----new@example.com----Password!----JBSWY3DPEHPK3PXP",
                })

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["updated_count"], 1)
                self.assertEqual(payload["format_counts"], {"old_new_password_2fa": 1})
                self.assertEqual(payload["updated"][0]["match_mode"], "old_email")
                self.assertFalse(payload["updated"][0]["token_replaced"])
                self.assertIsNone(db.get_account_by_email("old@example.com"))
                account = db.get_account_by_email("new@example.com")
                self.assertEqual(account["id"], 7)
                self.assertEqual(account["access_token"], "at-old")
                self.assertEqual(account["at_validity_status"], "valid")
                self.assertTrue(account["at_validity_valid"])

    def test_requested_new_url_at_resolves_and_removes_original_email(self):
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
                    "text": "new@example.com----https://MAIL.example/code?id=7#fragment----at-new",
                })

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["updated_count"], 1)
                self.assertEqual(payload["format_counts"], {"new_url_at": 1})
                self.assertEqual(payload["updated"][0]["match_mode"], "source_url")
                self.assertTrue(payload["updated"][0]["token_replaced"])
                self.assertIsNone(db.get_account_by_email("old@example.com"))
                account = db.get_account_by_email("new@example.com")
                self.assertEqual(account["access_token"], "at-new")
                self.assertEqual(account["at_validity_status"], "unchecked")

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
                }], target_group_id="group-2")

                self.assertEqual(skipped, [])
                self.assertEqual(updated[0]["account_id"], 8)
                self.assertEqual(updated[0]["removed_original_account_id"], 7)
                self.assertIsNone(db.get_account(7))
                kept = db.get_account(8)
                self.assertEqual(kept["email"], "new@example.com")
                self.assertEqual(kept["access_token"], "at-existing-new")
                self.assertEqual(kept["plan_type"], "plus")
                self.assertEqual(len(db.list_accounts()), 1)

    def test_import_replaces_identity_and_moves_from_group_one_to_group_two(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            with self._patch_storage(root):
                updated, skipped = db.import_rebound_accounts([{
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

    def test_route_accepts_four_column_subsite_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            with self._patch_storage(root):
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
                response = client.post("/api/accounts/import-rebound", json={
                    "group_id": "group-2",
                    "text": "new@example.com----Password!----JBSWY3DPEHPK3PXP----at-new",
                })
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["updated_count"], 1)
                self.assertEqual(payload["updated"][0]["old_email"], "old@example.com")
                self.assertNotIn("access_token", payload["updated"][0])
                self.assertNotIn("password", payload["updated"][0])

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

    def test_route_accepts_email_api_subsite_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed(root)
            with self._patch_storage(root):
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
                response = client.post("/api/accounts/import-rebound", json={
                    "group_id": "group-2",
                    "text": "new@example.com----old@example.com----https://mail.example/old-code----at-new",
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
        self.assertIn("原邮箱+换绑后邮箱+密码+2FA", template)
        self.assertIn("换绑后邮箱+取码URL+AT", template)
        self.assertIn("删除账号列表中的原邮箱", template)


if __name__ == "__main__":
    unittest.main()
