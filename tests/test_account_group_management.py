# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class AccountGroupManagementTests(unittest.TestCase):
    def _paths(self, root: Path):
        return (
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
            patch.object(db, "_ACCOUNT_GROUPS_JSON", root / "groups.json"),
        )

    def test_deleted_gc_group_is_not_recreated_when_app_refreshes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.joinpath("accounts.json").write_text("[]", encoding="utf-8")
            paths = self._paths(root)
            with paths[0], paths[1], paths[2]:
                group = db.create_account_group("GC")
                self.assertTrue(db.delete_account_group(group["id"]))

                create_app(auth_code="test-auth")

                self.assertEqual(db.list_account_groups(), [])

    def test_group_can_be_renamed_and_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.joinpath("accounts.json").write_text("[]", encoding="utf-8")
            paths = self._paths(root)
            with paths[0], paths[1], paths[2]:
                first = db.create_account_group("GC")
                db.create_account_group("Paid")

                renamed = db.rename_account_group(first["id"], "GC Active")

                self.assertEqual(renamed["name"], "GC Active")
                with self.assertRaisesRegex(ValueError, "同名"):
                    db.rename_account_group(first["id"], "Paid")

    def test_group_rename_route_persists_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.joinpath("accounts.json").write_text("[]", encoding="utf-8")
            paths = self._paths(root)
            with paths[0], paths[1], paths[2]:
                group = db.create_account_group("Old Name")
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

                response = client.patch(
                    f"/api/account-groups/{group['id']}",
                    json={"name": "New Name"},
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["group"]["name"], "New Name")
                self.assertEqual(db.list_account_groups()[0]["name"], "New Name")
