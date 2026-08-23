# -*- coding: utf-8 -*-
import base64
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import _access_token_identity, create_app


def _jwt(email: str, *, exp: int, top_level_email: bool = False, marker: str = "") -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {"exp": exp, "marker": marker}
    if top_level_email:
        payload["email"] = email
    else:
        payload["https://api.openai.com/profile"] = {"email": email}

    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode(header)}.{encode(payload)}.signature"


class AccountAtReplacementTests(unittest.TestCase):
    def _patch_storage(self, root: Path):
        stack = ExitStack()
        paths = {
            "_ACCOUNTS_JSON": "accounts.json",
            "_LEGACY_ACCOUNTS_JSON": "legacy-accounts.json",
            "_OUTLOOK_JSON": "outlook.json",
            "_LEGACY_OUTLOOK_JSON": "legacy-outlook.json",
            "_GENERIC_API_EMAIL_JSON": "generic.json",
            "_DOMAIN_EMAIL_JSON": "domain.json",
            "_ACCOUNT_GROUPS_JSON": "groups.json",
            "_SECURITY_CHECKPOINTS_JSON": "security.json",
            "_SECURITY_CHECKPOINTS_LOCK": "security.lock",
            "_ACCOUNTS_TXT": "accounts.txt",
            "_TOKENS_TXT": "tokens.txt",
            "_OUTLOOK_TXT": "outlook.txt",
            "_GENERIC_API_EMAIL_TXT": "generic.txt",
        }
        for name, filename in paths.items():
            stack.enter_context(patch.object(db, name, root / filename))
        stack.enter_context(patch.object(db, "_render_static_viewer", return_value=root / "viewer.html"))
        return stack

    def test_identity_accepts_nested_or_top_level_email_and_normalizes_bearer(self):
        nested = _jwt("nested@example.com", exp=4_102_444_800)
        top = _jwt("top@example.com", exp=4_202_444_800, top_level_email=True)

        self.assertEqual(_access_token_identity(f"Bearer {nested}")["email"], "nested@example.com")
        self.assertEqual(_access_token_identity(f"Authorization: Bearer {top}")["email"], "top@example.com")
        with self.assertRaisesRegex(ValueError, "未识别到邮箱"):
            _access_token_identity(_jwt("", exp=4_102_444_800))

    def test_single_route_rejects_mismatch_then_replaces_matching_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._patch_storage(root):
                account_id = db.insert_account(email="single@example.com", access_token="old-token")
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

                mismatch = client.post(f"/api/accounts/{account_id}/replace-at", json={
                    "access_token": _jwt("other@example.com", exp=4_102_444_800),
                })
                self.assertEqual(mismatch.status_code, 409)
                self.assertEqual(db.get_account(account_id)["access_token"], "old-token")

                replacement = _jwt("single@example.com", exp=4_202_444_800, top_level_email=True)
                response = client.post(f"/api/accounts/{account_id}/replace-at", json={
                    "access_token": replacement,
                })
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["updated"]["email"], "single@example.com")
                self.assertNotIn(replacement, json.dumps(payload))
                stored = db.get_account(account_id)
                self.assertEqual(stored["access_token"], replacement)
                self.assertEqual(stored["at_validity_status"], "unchecked")
                self.assertIsNone(stored["at_validity_trigger"])

    def test_bulk_route_matches_claim_email_prefers_later_expiry_and_syncs_pool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._patch_storage(root):
                db.upsert_manual_email_url("url@example.com", "https://mail.example/code")
                url_id = db.insert_account(email="url@example.com", access_token="old-url", email_source="generic_api")
                top_id = db.insert_account(email="top@example.com", access_token="old-top")
                db.update_account_at_validity(url_id, {
                    "outcome": "invalid_confirmed",
                    "error_code": "http_401",
                    "error": "expired",
                })
                low = _jwt("url@example.com", exp=4_102_444_800, marker="low")
                high = _jwt("url@example.com", exp=4_202_444_800, marker="high")
                top = _jwt("top@example.com", exp=4_202_444_800, top_level_email=True)
                unknown = _jwt("missing@example.com", exp=4_202_444_800)
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

                response = client.post("/api/accounts/replace-at-bulk", json={
                    "text": f"{low}\nBearer {high}\n{top}\nnot-a-jwt\n{unknown}",
                })

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["parsed_count"], 3)
                self.assertEqual(payload["updated_count"], 2)
                self.assertEqual(payload["skipped_count"], 3)
                serialized = json.dumps(payload, ensure_ascii=False)
                for token in (low, high, top, unknown):
                    self.assertNotIn(token, serialized)
                self.assertEqual(db.get_account(url_id)["access_token"], high)
                self.assertEqual(db.get_account(top_id)["access_token"], top)
                self.assertEqual(db.get_account(url_id)["at_validity_status"], "unchecked")
                generic = json.loads((root / "generic.json").read_text(encoding="utf-8"))
                self.assertEqual(generic[0]["access_token"], high)

    def test_template_exposes_per_account_and_batch_at_replacement_controls(self):
        template = Path("webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="btnBatchReplaceAtV2"', template)
        self.assertIn('id="atReplaceModal"', template)
        self.assertIn('data-account-replace-at=', template)
        self.assertIn("/api/accounts/replace-at-bulk", template)
        self.assertIn("/replace-at`,", template)
        self.assertIn("读取 JWT 内的邮箱", template)


if __name__ == "__main__":
    unittest.main()
