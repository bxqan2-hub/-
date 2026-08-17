# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import db


class GenericApiEmailClaimTests(unittest.TestCase):
    def test_claim_prefers_newest_clean_email_over_old_recycled_email(self):
        rows = [
            {"id": 1, "email": "old@example.test", "code_url": "https://mail.test/1", "status": "available", "note": "旧失败"},
            {"id": 2, "email": "clean-old@example.test", "code_url": "https://mail.test/2", "status": "available", "note": None},
            {"id": 3, "email": "clean-new@example.test", "code_url": "https://mail.test/3", "status": "available", "note": None},
        ]
        with patch.object(db, "_load_accounts", return_value=[]), \
             patch.object(db, "_load_generic_api_emails", return_value=rows), \
             patch.object(db, "_save_generic_api_emails") as save:
            claimed = db.claim_next_generic_api_email()
        self.assertEqual(claimed["email"], "clean-new@example.test")
        self.assertEqual(rows[2]["status"], "used")
        save.assert_called_once()

    def test_retryable_email_moves_to_back_after_each_failure(self):
        rows = [
            {"id": 1, "email": "first@example.test", "code_url": "https://mail.test/1", "status": "available", "note": "可重试", "retry_count": 1, "retry_queue_seq": 1},
            {"id": 2, "email": "second@example.test", "code_url": "https://mail.test/2", "status": "available", "note": "可重试", "retry_count": 1, "retry_queue_seq": 2},
        ]
        with patch.object(db, "_load_accounts", return_value=[]), \
             patch.object(db, "_load_generic_api_emails", return_value=rows), \
             patch.object(db, "_save_generic_api_emails"):
            first = db.claim_next_generic_api_email()
            db.release_generic_api_email(first["email"], status="available", note="再次失败，可重试")
            second = db.claim_next_generic_api_email()
        self.assertEqual(first["email"], "first@example.test")
        self.assertEqual(second["email"], "second@example.test")
        self.assertEqual(rows[0]["retry_count"], 2)
        self.assertGreater(rows[0]["retry_queue_seq"], rows[1]["retry_queue_seq"])

    def test_registered_email_is_excluded_from_automatic_claim(self):
        rows = [
            {"id": 1, "email": "fresh@example.test", "status": "available"},
            {"id": 2, "email": "registered@example.test", "status": "available"},
        ]
        accounts = [{"id": 4, "email": "REGISTERED@example.test"}]
        with patch.object(db, "_load_accounts", return_value=accounts), \
             patch.object(db, "_load_generic_api_emails", return_value=rows), \
             patch.object(db, "_save_generic_api_emails"):
            claimed = db.claim_next_generic_api_email()

        self.assertEqual(claimed["email"], "fresh@example.test")


if __name__ == "__main__":
    unittest.main()
