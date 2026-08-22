# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class ReboundAccountImportTests(unittest.TestCase):
    def _patch_storage(self, root: Path):
        stack = ExitStack()
        stack.enter_context(patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"))
        stack.enter_context(patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"))
        stack.enter_context(patch.object(db, "_ACCOUNT_GROUPS_JSON", root / "groups.json"))
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
            "original_email_line": "old@example.com",
        }]), encoding="utf-8")
        root.joinpath("groups.json").write_text(json.dumps([
            {"id": "group-1", "name": "分组1", "emails": ["old@example.com"], "created_at": "a", "updated_at": "a"},
            {"id": "group-2", "name": "分组2", "emails": [], "created_at": "b", "updated_at": "b"},
        ]), encoding="utf-8")

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

    def test_template_exposes_rebound_import_controls_and_label(self):
        template = Path("webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="btnImportReboundAccountsV2"', template)
        self.assertIn('id="reboundImportModal"', template)
        self.assertIn("换绑过后的", template)
        self.assertIn("/api/accounts/import-rebound", template)


if __name__ == "__main__":
    unittest.main()
