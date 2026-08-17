# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import db


class SelectedEmailClaimTests(unittest.TestCase):
    def test_claim_selected_outlook_email_is_single_use(self):
        rows = [{"id": 1, "email": "chosen@mail.com", "status": "available", "used_at": None, "note": "old"}]
        with patch.object(db, "_load_accounts", return_value=[]), \
             patch.object(db, "_load_outlook", return_value=rows), \
             patch.object(db, "_save_outlook") as save:
            claimed = db.claim_email("chosen@mail.com", "outlook")
            duplicate = db.claim_email("chosen@mail.com", "outlook")

        self.assertEqual(claimed["email"], "chosen@mail.com")
        self.assertEqual(rows[0]["status"], "used")
        self.assertIsNone(duplicate)
        self.assertEqual(save.call_count, 1)

    def test_claim_selected_generic_email_requires_matching_pool_source(self):
        rows = [{"id": 1, "email": "chosen@mail.com", "provider": "domain_api", "status": "available"}]
        with patch.object(db, "_load_accounts", return_value=[]), \
             patch.object(db, "_load_generic_api_emails", return_value=rows), \
             patch.object(db, "_save_generic_api_emails"):
            self.assertIsNone(db.claim_email("chosen@mail.com", "generic_api"))
            claimed = db.claim_email("chosen@mail.com", "domain_api")

        self.assertEqual(claimed["email"], "chosen@mail.com")
        self.assertEqual(rows[0]["status"], "used")

    def test_registered_email_cannot_be_claimed_from_stale_available_row(self):
        rows = [{"id": 1, "email": "registered@mail.com", "status": "available"}]
        accounts = [{"id": 9, "email": "REGISTERED@mail.com"}]
        with patch.object(db, "_load_accounts", return_value=accounts), \
             patch.object(db, "_load_generic_api_emails", return_value=rows), \
             patch.object(db, "_save_generic_api_emails") as save:
            claimed = db.claim_email("registered@mail.com", "generic_api")

        self.assertIsNone(claimed)
        save.assert_not_called()

    def test_insert_account_marks_generic_pool_row_used(self):
        accounts = []
        generic_rows = [{"id": 1, "email": "new@mail.com", "status": "available", "used_at": None}]
        with patch.object(db, "_load_accounts", return_value=accounts), \
             patch.object(db, "_load_outlook", return_value=[]), \
             patch.object(db, "_load_generic_api_emails", return_value=generic_rows), \
             patch.object(db, "_load_domain_pool", return_value=[]), \
             patch.object(db, "_save_accounts"), \
             patch.object(db, "_save_outlook"), \
             patch.object(db, "_save_generic_api_emails") as save_generic, \
             patch.object(db, "_save_domain_pool"):
            account_id = db.insert_account(email="new@mail.com", access_token="token", email_source="generic_api")

        self.assertEqual(account_id, 1)
        self.assertEqual(generic_rows[0]["status"], "used")
        self.assertEqual(generic_rows[0]["registered_account_id"], account_id)
        self.assertEqual(generic_rows[0]["access_token"], "token")
        save_generic.assert_called_once_with(generic_rows)

    def test_registered_email_cannot_be_released_as_available(self):
        rows = [{"id": 1, "email": "registered@mail.com", "status": "used", "used_at": "earlier"}]
        accounts = [{"id": 9, "email": "REGISTERED@mail.com"}]
        with patch.object(db, "_load_accounts", return_value=accounts), \
             patch.object(db, "_load_generic_api_emails", return_value=rows), \
             patch.object(db, "_save_generic_api_emails"):
            db.release_generic_api_email("registered@mail.com", status="available")

        self.assertEqual(rows[0]["status"], "used")
        self.assertEqual(rows[0]["used_at"], "earlier")


if __name__ == "__main__":
    unittest.main()
