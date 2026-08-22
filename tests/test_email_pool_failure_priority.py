# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import db
from webui.app import _list_pool_rows


class EmailPoolFailurePriorityTests(unittest.TestCase):
    def test_outlook_list_places_failed_and_retried_rows_after_normal_rows(self):
        rows = [
            {"id": 5, "email": "failed@example.test", "status": "failed"},
            {"id": 4, "email": "retry@example.test", "status": "available", "retry_count": 1},
            {"id": 3, "email": "disabled@example.test", "status": "disabled"},
            {"id": 2, "email": "clean-new@example.test", "status": "available"},
            {"id": 1, "email": "clean-old@example.test", "status": "used"},
        ]
        with patch.object(db, "_load_accounts", return_value=[]), \
             patch.object(db, "_load_outlook", return_value=rows):
            listed = db.list_outlook_pool(limit=10)

        self.assertEqual(
            [row["email"] for row in listed],
            [
                "clean-new@example.test",
                "clean-old@example.test",
                "failed@example.test",
                "retry@example.test",
                "disabled@example.test",
            ],
        )

    def test_generic_and_domain_lists_apply_the_same_failure_priority(self):
        generic_rows = [
            {"id": 3, "email": "failed@generic.test", "status": "failed"},
            {"id": 2, "email": "retry@generic.test", "status": "available", "retry_queue_seq": 7},
            {"id": 1, "email": "clean@generic.test", "status": "available"},
        ]
        domain_rows = [
            {"id": 3, "email": "failed@domain.test", "status": "failed"},
            {"id": 2, "email": "retry@domain.test", "status": "available", "note": "可重试，已移至队尾"},
            {"id": 1, "email": "clean@domain.test", "status": "available"},
        ]
        with patch.object(db, "_load_accounts", return_value=[]), \
             patch.object(db, "_load_generic_api_emails", return_value=generic_rows), \
             patch.object(db, "_load_domain_pool", return_value=domain_rows):
            generic = db.list_generic_api_email_pool(limit=10)
            domain = db.list_domain_email_pool(limit=10)

        self.assertEqual(generic[0]["email"], "clean@generic.test")
        self.assertEqual(domain[0]["email"], "clean@domain.test")
        self.assertEqual([row["email"] for row in generic[1:]], ["failed@generic.test", "retry@generic.test"])
        self.assertEqual([row["email"] for row in domain[1:]], ["failed@domain.test", "retry@domain.test"])

    def test_automatic_outlook_claim_exhausts_clean_mailboxes_before_retries(self):
        rows = [
            {"id": 1, "email": "clean-old@example.test", "status": "available"},
            {
                "id": 2,
                "email": "retry@example.test",
                "status": "available",
                "retry_count": 1,
                "retry_queue_seq": 1,
                "note": "上次注册失败",
            },
            {"id": 3, "email": "clean-new@example.test", "status": "available"},
        ]
        with patch.object(db, "_load_accounts", return_value=[]), \
             patch.object(db, "_load_outlook", return_value=rows), \
             patch.object(db, "_save_outlook"):
            first = db.claim_next_outlook()
            second = db.claim_next_outlook()
            third = db.claim_next_outlook()

        self.assertEqual(first["email"], "clean-new@example.test")
        self.assertEqual(second["email"], "clean-old@example.test")
        self.assertEqual(third["email"], "retry@example.test")

    @patch("webui.app.db.list_domain_email_pool", return_value=[])
    @patch("webui.app.db.list_generic_api_email_pool", return_value=[])
    @patch("webui.app.db.list_outlook_pool")
    def test_combined_pool_keeps_recent_failure_behind_older_normal_email(
        self,
        list_outlook_pool,
        _list_generic_api_email_pool,
        _list_domain_email_pool,
    ):
        list_outlook_pool.return_value = [
            {
                "id": 2,
                "email": "recent-failure@example.test",
                "status": "available",
                "retry_count": 1,
                "imported_at": "2026-08-23T12:00:00",
            },
            {
                "id": 1,
                "email": "older-clean@example.test",
                "status": "available",
                "imported_at": "2026-08-01T12:00:00",
            },
        ]

        rows = _list_pool_rows(source="all", status=None, fetch_limit=500)

        self.assertEqual(
            [row["email"] for row in rows],
            ["older-clean@example.test", "recent-failure@example.test"],
        )


if __name__ == "__main__":
    unittest.main()
