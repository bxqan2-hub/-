# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import account_export, db


class AccountProfilePersistenceTests(unittest.TestCase):
    def _storage_patches(self, root: Path):
        return (
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy_accounts.json"),
            patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"),
            patch.object(db, "_LEGACY_OUTLOOK_JSON", root / "legacy_outlook.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
        )

    def test_legacy_json_stays_readable_without_profile_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "accounts.json").write_text(
                json.dumps([{"id": 1, "email": "legacy@test.com", "access_token": "old-token"}]),
                encoding="utf-8",
            )
            patches = self._storage_patches(root)
            for item in patches:
                item.start()
            self.addCleanup(lambda: [item.stop() for item in reversed(patches)])

            row = db.get_account(1)

            self.assertEqual(row["email"], "legacy@test.com")
            self.assertNotIn("birth_date", row)
            self.assertNotIn("registration_exit_ip", row)

    def test_profile_fields_round_trip_and_name_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            patches = self._storage_patches(root)
            for item in patches:
                item.start()
            self.addCleanup(lambda: [item.stop() for item in reversed(patches)])

            account_id = db.insert_account(
                email="profile@test.com",
                access_token="token",
                user_name=None,
                registration_name="Alex Morgan",
                birth_date="1994-06-18",
                registration_exit_ip="203.0.113.24",
                registration_exit_country="JP",
                openai_created_at="2026-08-11T12:00:00Z",
            )
            row = db.get_account(account_id)

            self.assertEqual(row["user_name"], "Alex Morgan")
            self.assertEqual(row["registration_name"], "Alex Morgan")
            self.assertEqual(row["birth_date"], "1994-06-18")
            self.assertEqual(row["registration_exit_ip"], "203.0.113.24")
            self.assertEqual(row["registration_exit_country"], "JP")
            self.assertEqual(row["openai_created_at"], "2026-08-11T12:00:00Z")

            db.insert_account(email="profile@test.com", access_token="new-token")
            updated = db.get_account(account_id)
            self.assertEqual(updated["birth_date"], "1994-06-18")
            self.assertEqual(updated["registration_exit_ip"], "203.0.113.24")

    @patch("core.account_export._append_batch_archive", return_value=Path("batch"))
    @patch("core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": False})
    @patch("core.db.insert_account", return_value=7)
    def test_save_account_data_maps_registration_fields_and_created_time(
        self,
        insert_account,
        _enqueue_plan_check,
        _append_batch_archive,
    ):
        account_export.save_account_data(
            email="new@test.com",
            access_token="token",
            registration_name="Alex Morgan",
            birth_date="1994-06-18",
            registration_exit_ip="203.0.113.24",
            registration_exit_country="JP",
            extra={
                "user": {"id": "user-1", "name": ""},
                "account": {"planType": "free", "createdTime": 1786468709.873},
            },
        )

        saved = insert_account.call_args.kwargs
        self.assertEqual(saved["user_name"], "Alex Morgan")
        self.assertEqual(saved["registration_name"], "Alex Morgan")
        self.assertEqual(saved["birth_date"], "1994-06-18")
        self.assertEqual(saved["registration_exit_ip"], "203.0.113.24")
        self.assertEqual(saved["registration_exit_country"], "JP")
        self.assertEqual(saved["openai_created_at"], "2026-08-11T17:18:29Z")


if __name__ == "__main__":
    unittest.main()
