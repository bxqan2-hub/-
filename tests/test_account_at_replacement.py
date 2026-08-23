# -*- coding: utf-8 -*-
import base64
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import _access_token_identity, _parse_at_import_text, create_app


def _jwt(
    email: str,
    *,
    exp: int,
    top_level_email: bool = False,
    marker: str = "",
    plan_type: str = "",
) -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {"exp": exp, "marker": marker}
    if top_level_email:
        payload["email"] = email
    else:
        payload["https://api.openai.com/profile"] = {"email": email}
    if plan_type:
        payload["https://api.openai.com/auth"] = {"chatgpt_plan_type": plan_type}

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

    def test_identity_accepts_full_session_json_and_validates_session_email(self):
        token = _jwt("plus@example.com", exp=4_202_444_800, plan_type="plus")
        session = {
            "WARNING_BANNER": "DO NOT SHARE",
            "accessToken": token,
            "sessionToken": "not-the-access-token",
            "user": {"email": "plus@example.com", "name": "Plus User"},
            "account": {"planType": "plus", "structure": "personal", "isDelinquent": False},
        }
        pretty = json.dumps(session, indent=2)

        identity = _access_token_identity(pretty)

        self.assertEqual(identity["email"], "plus@example.com")
        self.assertEqual(identity["access_token"], token)
        self.assertNotIn("plan_type", identity)
        self.assertNotIn("plan_evidence_source", identity)
        self.assertEqual(identity["plan_import_hint"], "plus")
        self.assertEqual(identity["plan_import_hint_source"], "api/auth/session")
        self.assertEqual(identity["input_format"], "session_json")
        session["user"]["email"] = "mismatch@example.com"
        with self.assertRaisesRegex(ValueError, "与 AT 邮箱 .* 不一致"):
            _access_token_identity(session)

    def test_import_text_accepts_session_object_json_array_ndjson_and_email_at(self):
        one = _jwt("one@example.com", exp=4_102_444_800)
        two = _jwt("two@example.com", exp=4_202_444_800)
        entries, skipped = _parse_at_import_text(json.dumps([
            {"accessToken": one, "user": {"email": "one@example.com"}},
            two,
        ]))
        self.assertEqual(len(entries), 2)
        self.assertEqual(skipped, [])
        ndjson, skipped = _parse_at_import_text(
            json.dumps({"accessToken": one, "user": {"email": "one@example.com"}})
            + "\n"
            + f"two@example.com----{two}"
        )
        self.assertEqual(len(ndjson), 2)
        self.assertEqual(skipped, [])
        self.assertEqual(_access_token_identity(ndjson[1])["input_format"], "email_at")
        pretty_stream, skipped = _parse_at_import_text(
            json.dumps({"accessToken": one, "user": {"email": "one@example.com"}}, indent=2)
            + "\n\n"
            + json.dumps({"accessToken": two, "user": {"email": "two@example.com"}}, indent=2)
        )
        self.assertEqual(len(pretty_stream), 2)
        self.assertEqual(skipped, [])

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

    def test_single_route_accepts_full_session_json_without_changing_plan_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._patch_storage(root):
                account_id = db.insert_account(
                    email="plus@example.com",
                    access_token="old-token",
                    plan_type="free",
                )
                db.update_account_plan_check(acc_id=account_id, result={
                    "ok": True,
                    "current_plan_type": "free",
                    "is_free_plan": True,
                    "has_active_subscription": False,
                    "has_active_plus_subscription": False,
                    "plan_detection_source": "backend-api/accounts/check",
                    "plan_authority": "authoritative",
                })
                token = _jwt("plus@example.com", exp=4_202_444_800, plan_type="plus")
                session = {
                    "accessToken": token,
                    "user": {"email": "plus@example.com"},
                    "account": {"planType": "plus", "isDelinquent": False},
                }
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

                response = client.post(f"/api/accounts/{account_id}/replace-at", json={
                    "access_token": json.dumps(session, indent=2),
                })

                self.assertEqual(response.status_code, 200)
                stored = db.get_account(account_id)
                self.assertEqual(stored["access_token"], token)
                self.assertEqual(stored["current_plan_type"], "free")
                self.assertFalse(stored["has_active_subscription"])
                self.assertFalse(stored["has_active_plus_subscription"])
                self.assertTrue(stored["is_free_plan"])
                self.assertEqual(stored["plan_detection_source"], "backend-api/accounts/check")
                self.assertEqual(stored["plan_authority"], "authoritative")
                self.assertEqual(stored["plan_check_status"], "success")
                self.assertEqual(stored["plan_import_hint"], "plus")
                self.assertEqual(stored["plan_import_hint_source"], "api/auth/session")

    def test_bulk_route_accepts_json_array_of_sessions_and_raw_tokens(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._patch_storage(root):
                session_id = db.insert_account(
                    email="session@example.com", access_token="old-session", plan_type="free",
                )
                raw_id = db.insert_account(
                    email="raw@example.com", access_token="old-raw", plan_type="free",
                )
                session_token = _jwt("session@example.com", exp=4_202_444_800, plan_type="plus")
                raw_token = _jwt("raw@example.com", exp=4_202_444_800, plan_type="plus")
                client = create_app(auth_code="test-auth").test_client()
                client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
                text = json.dumps([
                    {
                        "accessToken": session_token,
                        "user": {"email": "session@example.com"},
                        "account": {"planType": "plus"},
                    },
                    raw_token,
                ], indent=2)

                response = client.post("/api/accounts/replace-at-bulk", json={"text": text})

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["input_count"], 2)
                self.assertEqual(payload["updated_count"], 2)
                self.assertEqual(payload["format_counts"], {"raw_at": 1, "session_json": 1})
                self.assertNotIn(session_token, json.dumps(payload))
                self.assertNotIn(raw_token, json.dumps(payload))
                self.assertEqual(db.get_account(session_id)["access_token"], session_token)
                self.assertEqual(db.get_account(session_id)["plan_type"], "free")
                self.assertIsNone(db.get_account(session_id).get("has_active_plus_subscription"))
                self.assertEqual(db.get_account(session_id)["plan_import_hint"], "plus")
                self.assertEqual(db.get_account(session_id)["plan_import_hint_source"], "api/auth/session")
                self.assertEqual(db.get_account(raw_id)["access_token"], raw_token)
                self.assertEqual(db.get_account(raw_id)["plan_type"], "free")
                self.assertIsNone(db.get_account(raw_id).get("has_active_plus_subscription"))
                self.assertEqual(db.get_account(raw_id)["plan_import_hint"], "plus")
                self.assertEqual(db.get_account(raw_id)["plan_import_hint_source"], "access_token_claim")

    def test_authoritative_plan_query_clears_import_hint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._patch_storage(root):
                account_id = db.insert_account(email="query@example.com", access_token="old", plan_type="free")
                db.replace_account_access_tokens([{
                    "account_id": account_id,
                    "email": "query@example.com",
                    "access_token": _jwt("query@example.com", exp=4_202_444_800, plan_type="plus"),
                    "plan_import_hint": "plus",
                    "plan_import_hint_source": "access_token_claim",
                }])
                self.assertEqual(db.get_account(account_id)["plan_import_hint"], "plus")
                self.assertEqual([row["id"] for row in db.list_accounts(plan_filter="plus")], [account_id])

                db.update_account_plan_check(acc_id=account_id, result={
                    "ok": True,
                    "current_plan_type": "free",
                    "is_free_plan": True,
                    "has_active_subscription": False,
                    "has_active_plus_subscription": False,
                    "plan_detection_source": "backend-api/accounts/check",
                    "plan_authority": "authoritative",
                })

                stored = db.get_account(account_id)
                self.assertNotIn("plan_import_hint", stored)
                self.assertEqual(stored["current_plan_type"], "free")
                self.assertFalse(stored["has_active_plus_subscription"])
                self.assertEqual(stored["plan_detection_source"], "backend-api/accounts/check")
                self.assertEqual(db.list_accounts(plan_filter="plus"), [])

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
        self.assertIn("导入后可显示 AT 套餐声明提示；原实时套餐查询方式不变", template)
        self.assertIn("Plus（AT声明）", template)
        self.assertIn("完整 /api/auth/session JSON", template)
        self.assertIn("Session JSON、JSON 数组", template)


if __name__ == "__main__":
    unittest.main()
