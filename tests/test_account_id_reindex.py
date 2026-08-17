# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountIdReindexTests(unittest.TestCase):
    def test_delete_reindexes_accounts_and_updates_local_references(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = {
                "accounts": root / "accounts.json",
                "outlook": root / "outlook.json",
                "generic": root / "generic.json",
                "jobs": root / "jobs.json",
                "groups": root / "groups.json",
            }
            paths["accounts"].write_text(json.dumps([
                {"id": 1, "email": "one@test.com"},
                {"id": 2, "email": "two@test.com"},
                {"id": 3, "email": "three@test.com"},
            ]), encoding="utf-8")
            paths["outlook"].write_text(json.dumps([
                {"id": 1, "email": "two@test.com", "registered_account_id": 2},
                {"id": 2, "email": "three@test.com", "registered_account_id": 3},
            ]), encoding="utf-8")
            paths["generic"].write_text(json.dumps([
                {"id": 1, "email": "three@test.com", "registered_account_id": 3},
            ]), encoding="utf-8")
            paths["jobs"].write_text(json.dumps([
                {"id": 1, "account_id": 2}, {"id": 2, "account_id": 3},
            ]), encoding="utf-8")
            paths["groups"].write_text(json.dumps([
                {"id": "gc", "name": "GC", "emails": ["two@test.com", "three@test.com"]},
            ]), encoding="utf-8")

            with patch.object(db, "_ACCOUNTS_JSON", paths["accounts"]), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy_accounts.json"), \
                 patch.object(db, "_OUTLOOK_JSON", paths["outlook"]), \
                 patch.object(db, "_LEGACY_OUTLOOK_JSON", root / "legacy_outlook.json"), \
                 patch.object(db, "_GENERIC_API_EMAIL_JSON", paths["generic"]), \
                 patch.object(db, "_JOBS_JSON", paths["jobs"]), \
                 patch.object(db, "_LEGACY_JOBS_JSON", root / "legacy_jobs.json"), \
                 patch.object(db, "_ACCOUNT_GROUPS_JSON", paths["groups"]), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"), \
                 patch.object(db, "_GENERIC_API_EMAIL_TXT", root / "generic.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                self.assertTrue(db.delete_account(acc_id=2))

                self.assertEqual([r["id"] for r in db._load_accounts()], [1, 2])
                outlook = db._load_outlook()
                self.assertNotIn("registered_account_id", outlook[0])
                self.assertEqual(outlook[1]["registered_account_id"], 2)
                self.assertEqual(db._load_generic_api_emails()[0]["registered_account_id"], 2)
                jobs = db._load_jobs()
                self.assertIsNone(jobs[0]["account_id"])
                self.assertEqual(jobs[1]["account_id"], 2)
                groups = db.list_account_groups()
                self.assertEqual(groups[0]["emails"], ["three@test.com"])
                self.assertEqual(groups[0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
