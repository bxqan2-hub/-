# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import db, jp_trial_service
from webui.app import _compact_account_for_list, create_app


class JpTrialClassificationTests(unittest.TestCase):
    def test_campaign_and_explicit_flags_are_classified(self):
        eligible = jp_trial_service.classify_jp_trial_eligibility({
            "accounts": {
                "default": {
                    "eligible_promo_campaigns": {
                        "plus": {"id": "plus-1-month-free"},
                    },
                },
            },
        })
        explicit = jp_trial_service.classify_jp_trial_eligibility({
            "accounts": {"default": {"one_click_trial_eligible": True}},
        })
        absent = jp_trial_service.classify_jp_trial_eligibility({"accounts": {"default": {}}})

        self.assertEqual(eligible, {
            "eligible": True,
            "evidence": "eligible_promo_campaigns.plus",
        })
        self.assertTrue(explicit["eligible"])
        self.assertIn("one_click_trial_eligible", explicit["evidence"])
        self.assertEqual(absent, {
            "eligible": False,
            "evidence": "eligible_promo_campaigns.plus absent",
        })

    @patch("core.jp_trial_service.BrowserSession")
    def test_probe_uses_fixed_endpoint_promo_and_japanese_headers(self, browser_session):
        response = MagicMock(status_code=200, text='{"plus_trial_eligible":true}')
        env = browser_session.return_value
        env.device_id = "test-device"
        env.session.get.return_value = response

        result = jp_trial_service.check_jp_trial_eligibility(
            "opaque-at",
            proxy="socks5h://jp.example:1080",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["eligible"])
        request_url = env.session.get.call_args.args[0]
        headers = env.session.get.call_args.kwargs["headers"]
        self.assertEqual(
            request_url,
            "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        )
        self.assertEqual(env.session.get.call_args.kwargs["timeout"], 45.0)
        self.assertEqual(headers["oai-language"], "ja-JP")
        self.assertTrue(headers["accept-language"].startswith("ja-JP"))
        self.assertIn("promo_campaign=plus-1-month-free", headers["referer"])

    @patch("core.jp_trial_service._inspect_proxy_exit")
    @patch.object(jp_trial_service.proxy_cfg, "PROXY_API_ENABLED", False)
    @patch.object(jp_trial_service.proxy_cfg, "PROXY_POOL", [
        "http://de.example:8080",
        "http://jp.example:8080",
    ])
    def test_registration_proxy_pool_is_inspected_until_jp_exit(self, inspect):
        inspect.side_effect = [
            {"ip": "198.51.100.10", "country_code": "DE"},
            {"ip": "203.0.113.7", "country_code": "JP"},
        ]
        with jp_trial_service._JP_PROXY_CACHE_LOCK:
            jp_trial_service._JP_PROXY_CACHE.update({"proxy": "", "expires_at": 0.0})

        route = jp_trial_service.resolve_japanese_trial_proxy()

        self.assertEqual(route["proxy"], "http://jp.example:8080")
        self.assertEqual(inspect.call_count, 2)


class JpTrialPersistenceTests(unittest.TestCase):
    def test_failed_probe_is_not_persisted_as_ineligible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            accounts = root / "accounts.json"
            accounts.write_text('[{"id":7,"email":"jp@example.com"}]', encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"):
                self.assertTrue(db.update_account_jp_trial(7, {
                    "ok": False,
                    "checked_at": "2026-08-12T12:00:00",
                    "error": "HTTP 503",
                }))
                saved = db.get_account(7)

        self.assertEqual(saved["jp_trial_status"], "failed")
        self.assertIsNone(saved["jp_trial_eligible"])
        self.assertEqual(saved["jp_trial_error"], "HTTP 503")

    def test_rate_limit_does_not_replace_existing_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            accounts = root / "accounts.json"
            accounts.write_text(
                '[{"id":7,"email":"jp@example.com","jp_trial_status":"eligible",'
                '"jp_trial_eligible":true}]',
                encoding="utf-8",
            )
            with patch.object(db, "_ACCOUNTS_JSON", accounts), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"):
                self.assertFalse(db.update_account_jp_trial(7, {
                    "ok": False,
                    "http_status": 429,
                    "rate_limited": True,
                }))
                saved = db.get_account(7)

        self.assertEqual(saved["jp_trial_status"], "eligible")
        self.assertTrue(saved["jp_trial_eligible"])

    def test_compact_payload_always_contains_frontend_fields(self):
        compact = _compact_account_for_list({"id": 1, "email": "one@example.com"})
        self.assertEqual(compact["jp_trial_status"], "unchecked")
        for key in (
            "jp_trial_eligible",
            "jp_trial_evidence",
            "jp_trial_error",
            "jp_trial_checked_at",
        ):
            self.assertIn(key, compact)


class JpTrialBulkApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.jp_trial_service.check_accounts_jp_trial")
    @patch("webui.app.db.get_account")
    def test_bulk_api_returns_aliashub_summary_without_access_token(self, get_account, check):
        get_account.return_value = {
            "id": 9,
            "email": "jp@example.com",
            "access_token": "secret-at",
        }
        check.return_value = {
            "requested": 1,
            "checked": 1,
            "eligible": 1,
            "ineligible": 0,
            "failed": 0,
            "rate_limited": 0,
            "skipped": 0,
            "items": [{
                "id": 9,
                "trial_status": "eligible",
                "trial_eligible": True,
                "trial_evidence": "eligible_promo_campaigns.plus",
                "trial_error": "",
                "trial_checked_at": "2026-08-12T12:00:00",
            }],
        }

        response = self.client.post(
            "/api/accounts/check-jp-trial-bulk",
            json={"account_ids": [9]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["eligible"], 1)
        check.assert_called_once()
        self.assertNotIn("secret-at", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
