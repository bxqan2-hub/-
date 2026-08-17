import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from core import db
from core.domain_api_mail_client import parse_import_text
from webui.app import create_app


class DomainApiMailClientTests(unittest.TestCase):
    @staticmethod
    def _response(html: str) -> Mock:
        response = Mock()
        response.text = html
        response.raise_for_status.return_value = None
        return response

    @patch("core.domain_api_mail_client.requests.get")
    def test_account_page_detects_each_email_domain_independently(self, get):
        get.side_effect = [
            self._response('<a href="/eid/10p.php?batch=42">open</a>'),
            self._response(
                """
                <div class="row">
                  <input data-copy="first@alpha.example"><br>
                  <button data-copy="secret-a">copy</button>
                </div>
                <div class="row">
                  <span data-copy="second@beta.test">copy</span>
                  <span data-copy="secret-b">copy</span>
                </div>
                """
            ),
        ]

        rows = parse_import_text("https://pickup.vendor.test/eid/nb.php?batch=42")

        self.assertEqual([row["email_domain"] for row in rows], ["alpha.example", "beta.test"])
        self.assertEqual({row["provider"] for row in rows}, {"domain_api"})
        for row in rows:
            parsed = urlparse(row["code_url"])
            query = parse_qs(parsed.query)
            self.assertEqual(parsed.netloc, "pickup.vendor.test")
            self.assertEqual(parsed.path, "/m.php")
            self.assertEqual(query["u"], [row["email"]])
            self.assertTrue(query["p"][0].startswith("secret-"))

    def test_direct_pickup_url_uses_address_domain_not_api_host(self):
        rows = parse_import_text(
            "member@custom-domain.example----https://api.vendor.test/messages?id=7"
        )

        self.assertEqual(rows[0]["email_domain"], "custom-domain.example")
        self.assertEqual(rows[0]["code_url"], "https://api.vendor.test/messages?id=7")

    def test_password_row_accepts_explicit_provider_base(self):
        rows = parse_import_text(
            "member@another.example----pass-123----https://mail.vendor.test/m.php"
        )

        parsed = urlparse(rows[0]["code_url"])
        self.assertEqual(rows[0]["email_domain"], "another.example")
        self.assertEqual(parsed.netloc, "mail.vendor.test")
        self.assertEqual(parse_qs(parsed.query)["u"], ["member@another.example"])

    def test_registered_import_groups_accounts_by_detected_domain(self):
        records = [
            {
                "email": "one@alpha.example",
                "email_domain": "alpha.example",
                "code_url": "https://provider.test/m.php?id=1",
            },
            {
                "email": "two@beta.example",
                "email_domain": "beta.example",
                "code_url": "https://provider.test/m.php?id=2",
            },
        ]
        with patch.object(db, "_load_accounts", return_value=[]), \
             patch.object(db, "_load_outlook", return_value=[]), \
             patch.object(db, "_load_generic_api_emails", return_value=[]), \
             patch.object(db, "_save_accounts"), \
             patch.object(db, "_save_outlook"), \
             patch.object(db, "_save_generic_api_emails"), \
             patch.object(db, "ensure_account_group", side_effect=lambda name: {"id": name, "name": name}) as ensure, \
             patch.object(db, "add_accounts_to_group") as add:
            inserted, skipped = db.import_registered_email_accounts(records, source="domain_api")

        self.assertEqual((inserted, skipped), (2, 0))
        self.assertEqual(
            {call.args[0] for call in ensure.call_args_list},
            {"域名邮箱 · alpha.example", "域名邮箱 · beta.example"},
        )
        self.assertEqual(add.call_count, 2)

    @patch("core.domain_api_mail_client.parse_import_text")
    @patch.object(db, "import_generic_api_emails", return_value=(1, 0))
    def test_import_route_returns_detected_domains(self, import_rows, parse_rows):
        parse_rows.return_value = [{
            "email": "one@pasted-domain.example",
            "email_domain": "pasted-domain.example",
            "code_url": "https://provider.test/m.php?id=1",
            "provider": "domain_api",
        }]
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

        response = client.post(
            "/api/outlook/import",
            json={"source": "domain_api", "text": "https://provider.test/accounts", "as_registered": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["domains"], ["pasted-domain.example"])
        import_rows.assert_called_once_with(parse_rows.return_value)


class DomainApiPoolIsolationTests(unittest.TestCase):
    def test_provider_filter_keeps_generic_and_domain_pools_separate(self):
        rows = [
            {"id": 1, "email": "generic@example.test", "status": "available"},
            {"id": 2, "email": "domain@custom.test", "status": "available", "provider": "domain_api"},
        ]
        with patch.object(db, "_load_generic_api_emails", return_value=rows), \
             patch.object(db, "_save_generic_api_emails"):
            generic = db.claim_next_generic_api_email(provider="generic_api")
            domain = db.claim_next_generic_api_email(provider="domain_api")

        self.assertEqual(generic["email"], "generic@example.test")
        self.assertEqual(domain["email"], "domain@custom.test")


if __name__ == "__main__":
    unittest.main()
