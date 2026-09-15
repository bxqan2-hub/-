import base64
import io
import json
import zipfile
from unittest.mock import Mock

import pytest

from core import codex_oauth, db
from webui.app import create_app


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"fixture.{payload}.signature"


def oauth_storage(*, email="one@example.test", account_id="account-1", plan_type=""):
    claims = {
        "email": email,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    token = jwt(claims)
    return {
        "type": "codex", "email": email, "account_id": account_id,
        "access_token": token, "refresh_token": "refresh-fixture",
        "id_token": token, "expired": "2030-01-01T00:00:00Z",
        "plan_type": plan_type,
    }


@pytest.fixture
def export_fixture(monkeypatch):
    storage = {"type": "codex", "email": "one@example.test", "access_token": "access-fixture",
               "refresh_token": "refresh-fixture", "id_token": "id-fixture", "account_id": "account-1",
               "expired": "2030-01-01T00:00:00Z", "account_password": "do-not-export-password",
               "two_factor_secret": "do-not-export-totp"}
    monkeypatch.setattr(db, "list_codex_accounts", lambda: [{"email": storage["email"], "filename": "codex-one.json"}])
    monkeypatch.setattr(db, "get_account", lambda i: {"id": 1, "email": "one@example.test", "codex_status": "success"} if i == 1 else None)
    monkeypatch.setattr(db, "read_codex_credential", lambda f: (json.dumps(storage), f))
    marked = Mock()
    monkeypatch.setattr(db, "mark_codex_exported", marked)
    remote = Mock(side_effect=AssertionError("unexpected remote CPA request"))
    monkeypatch.setattr(codex_oauth, "list_cpa_codex_auth_files", remote)
    client = create_app(auth_code="test-export").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-export"
    return client, storage, remote, marked


def test_sub2_exports_oauth_instead_of_agent_identity(export_fixture):
    client, storage, remote, marked = export_fixture
    r = client.post("/api/accounts/download-codex-bulk", json={"account_ids": [1, 1], "format": "sub2"})
    assert r.status_code == 200 and "no-store" in r.headers["Cache-Control"]
    data = r.get_json()
    assert data["type"] == "sub2api-data" and data["version"] == 1 and data["proxies"] == []
    assert len(data["accounts"]) == 1
    account = data["accounts"][0]
    assert account["type"] == "oauth" and account["platform"] == "openai"
    assert (account["concurrency"], account["priority"]) == (3, 50)
    credentials = account["credentials"]
    for key in ("access_token", "refresh_token", "id_token", "email"):
        assert credentials[key] == storage[key]
    assert credentials["chatgpt_account_id"] == "account-1"
    assert credentials["client_id"] == codex_oauth._cfg.CODEX_CLIENT_ID
    assert credentials["expires_at"] == storage["expired"]
    assert "do-not-export" not in r.get_data(as_text=True)
    remote.assert_not_called()
    marked.assert_called_once_with("codex-one.json")


def test_cpa_export_prefers_plan_from_json_over_stale_db_filename(export_fixture, monkeypatch):
    client, storage, remote, marked = export_fixture
    storage.update(oauth_storage(plan_type="plus"))
    monkeypatch.setattr(db, "list_codex_accounts", lambda: [{
        "email": storage["email"], "filename": "codex-one@example.test.json",
    }])
    monkeypatch.setattr(db, "read_codex_credential", lambda _f: (json.dumps(storage), _f))
    r = client.post("/api/accounts/download-codex-bulk", json={"account_ids": [1], "format": "cpa"})
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        assert "codex-one@example.test-plus.json" in z.namelist()
    remote.assert_not_called()
    marked.assert_called_once_with("codex-one@example.test.json")


def test_cpa_uses_local_callback_file_without_remote_configuration(export_fixture):
    client, storage, remote, _ = export_fixture
    r = client.post("/api/accounts/download-codex-bulk", json={"account_ids": [1], "format": "cpa"})
    assert r.status_code == 200 and r.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        data = json.loads(z.read("codex-one.json"))
        assert data["access_token"] == storage["access_token"] and data["type"] == "codex"
        assert "account_password" not in data and "two_factor_secret" not in data
        assert json.loads(z.read("manifest.json"))["count"] == 1
    remote.assert_not_called()


def test_export_counter_failure_does_not_misreport_a_valid_download(export_fixture):
    client, _, _, marked = export_fixture
    marked.side_effect = OSError("counter unavailable")
    r = client.post("/api/accounts/download-codex-bulk", json={"account_ids": [1], "format": "sub2", "prepare": True})
    assert r.status_code == 200
    assert r.get_json()["added_count"] == 1 and r.get_json()["error_count"] == 0


def test_prepared_sub2_download_contains_no_tokens_in_metadata_and_is_one_shot(export_fixture):
    client, storage, _, _ = export_fixture
    r = client.post("/api/accounts/download-codex-bulk", json={"account_ids": [1, 2], "format": "sub2", "prepare": True})
    d = r.get_json()
    assert d["added_count"] == 1 and d["error_count"] == 1
    assert storage["access_token"] not in r.get_data(as_text=True)
    download = client.get(d["download_url"])
    assert download.status_code == 200 and download.get_json()["accounts"][0]["credentials"]["refresh_token"] == storage["refresh_token"]
    assert client.get(d["download_url"]).status_code == 404


@pytest.mark.parametrize("change", [{"type": "codex_cpa_callback"}, {"access_token": ""}, {"email": "other@example.test"}])
def test_receipts_missing_tokens_and_wrong_accounts_fail_without_secret_errors(export_fixture, change):
    client, storage, remote, marked = export_fixture
    storage.update(change)
    r = client.post("/api/accounts/download-codex-bulk", json={"account_ids": [1], "format": "sub2"})
    assert r.status_code == 404
    assert "refresh-fixture" not in r.get_data(as_text=True) and "do-not-export" not in r.get_data(as_text=True)
    marked.assert_not_called()
    remote.assert_not_called()


def test_cpa_remote_fallback_is_lazy_and_checks_identity(export_fixture, monkeypatch):
    client, storage, remote, _ = export_fixture
    monkeypatch.setattr(db, "list_codex_accounts", lambda: [])
    remote.side_effect = None
    remote.return_value = [{"name": "codex-one.json", "email": storage["email"]}]
    monkeypatch.setattr(codex_oauth, "download_cpa_codex_auth_text", lambda **kw: (json.dumps(storage), "../codex-one.json", {}))
    r = client.post("/api/accounts/download-codex-bulk", json={"account_ids": [1], "format": "cpa"})
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        assert "codex-one.json" in z.namelist() and not any(".." in name for name in z.namelist())
    remote.assert_called_once()


def test_download_requires_existing_auth_and_rejects_unknown_format(export_fixture):
    client, _, _, marked = export_fixture
    assert client.post("/api/accounts/download-codex-bulk", json={"account_ids": [1], "format": "other"}).status_code == 400
    client.environ_base.pop("HTTP_X_AUTH_CODE")
    assert client.post("/api/accounts/download-codex-bulk", json={"account_ids": [1], "format": "sub2"}).status_code == 401
    marked.assert_not_called()


def test_export_checks_each_token_email_and_account_id(export_fixture):
    _, storage, _, _ = export_fixture
    storage["id_token"] = jwt({"email": "other@example.test"})
    storage["access_token"] = jwt({"email": storage["email"]})
    with pytest.raises(ValueError, match="Token 邮箱"):
        codex_oauth.build_sub2api_oauth_account(storage, email=storage["email"])
    storage["id_token"] = jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "wrong-account"}})
    with pytest.raises(ValueError, match="账号 ID"):
        codex_oauth.build_sub2api_oauth_account(storage, email=storage["email"])


def test_callback_auth_payload_nested_credentials_are_extracted_and_plan_is_enriched(monkeypatch):
    token = jwt({
        "email": "one@example.test",
        "https://api.openai.com/auth": {"chatgpt_account_id": "account-1"},
    })
    calls = []

    def fake_plan(actual, **kwargs):
        calls.append((actual, kwargs))
        return {
            "ok": True,
            "current_plan_type": "plus",
            "subscription_plan": "chatgptplus",
            "has_active_subscription": True,
            "has_active_plus_subscription": True,
            "is_free_plan": False,
            "checked_at": "2030-01-01T00:00:00",
            "plan_detection_source": "backend-api/subscriptions",
            "plan_authority": "authoritative",
            "plan_confidence": "high",
            "account_id": "account-1",
            "expires_at": "2030-02-01T00:00:00Z",
        } if actual == token else {"ok": False}

    monkeypatch.setattr(
        "core.chatgpt_plan.check_account_plan",
        fake_plan,
    )
    payload = {"data": {"account": {"credentials": {"access_token": token, "email": "one@example.test"}}}}
    auth = codex_oauth._extract_cpa_auth_json(payload)
    assert auth["access_token"] == token
    auth["account_id"] = "account-1"
    enriched = codex_oauth._enrich_codex_auth_json_plan(auth, proxy="http://127.0.0.1:18080")
    assert enriched["plan_type"] == "plus"
    assert enriched["subscription_plan"] == "chatgptplus"
    assert enriched["subscription_expires_at"] == "2030-02-01T00:00:00Z"
    assert calls == [(token, {
        "proxy": "http://127.0.0.1:18080", "fast_mode": False,
        "max_attempts": 1, "timeout": 5,
    })]


def test_cpa_callback_receipt_downloads_credential_and_saves_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_oauth, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(codex_oauth._cfg, "CODEX_OUTPUT_DIRNAME", "codex_accounts")
    monkeypatch.setattr(codex_oauth._cfg, "CPA_SAVE_CALLBACK_RECEIPT", True)
    monkeypatch.setattr(
        codex_oauth,
        "download_cpa_codex_auth_text",
        lambda **_kwargs: (
            json.dumps({
                "type": "codex",
                "email": "one@example.test",
                "access_token": jwt({
                    "email": "one@example.test",
                    "https://api.openai.com/auth": {"chatgpt_account_id": "account-1"},
                }),
                "refresh_token": "refresh-fixture",
                "id_token": jwt({
                    "email": "one@example.test",
                    "https://api.openai.com/auth": {"chatgpt_account_id": "account-1"},
                }),
                "account_id": "account-1",
            }),
            "codex-one@example.test-plus.json",
            {},
        ),
    )
    monkeypatch.setattr(
        "core.chatgpt_plan.check_account_plan",
        lambda *_args, **_kwargs: {
            "ok": True,
            "current_plan_type": "plus",
            "checked_at": "2030-01-01T00:00:00",
            "plan_authority": "authoritative",
            "account_id": "account-1",
        },
    )
    path = codex_oauth._save_cpa_local_record(
        email="one@example.test",
        callback_url="http://localhost:1455/auth/callback?code=fixture&state=fixture",
        auth_url="https://auth.example.test/fixture",
        state="fixture",
        submit_payload={"message": "ok"},
    )
    assert path.name == "codex-one@example.test-plus.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["plan_type"] == "plus"
    assert saved["plan_checked_at"] == "2030-01-01T00:00:00"


def test_save_codex_credential_writes_valid_credential_before_plan_query(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_oauth, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(codex_oauth._cfg, "CODEX_OUTPUT_DIRNAME", "codex_accounts")
    storage = oauth_storage()
    observed = []

    def fake_plan(token, **kwargs):
        path = tmp_path / "codex_accounts" / "codex-one@example.test.json"
        observed.append((path.exists(), token, kwargs))
        return {"ok": True, "account_id": "account-1", "current_plan_type": "plus",
                "plan_authority": "verified", "checked_at": "2030-01-01T00:00:00Z"}

    monkeypatch.setattr("core.chatgpt_plan.check_account_plan", fake_plan)
    path = codex_oauth.save_codex_credential(storage, storage["email"], "free", proxy="http://127.0.0.1:18080")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert observed and observed[0][0] is True
    assert observed[0][1] == storage["access_token"]
    assert observed[0][2] == {"proxy": "http://127.0.0.1:18080", "fast_mode": False,
                               "max_attempts": 1, "timeout": 5}
    assert saved["plan_type"] == "plus" and saved["plan_check_ok"] is True
    assert saved["plan_authority"] == "verified"


@pytest.mark.parametrize("result", [
    {"ok": False, "error": "fixture backend failed"},
    {"ok": True, "account_id": "account-other", "current_plan_type": "pro",
     "plan_authority": "authoritative"},
    {"ok": True, "account_id": "account-1", "current_plan_type": "pro",
     "plan_authority": "heuristic"},
])
def test_plan_enrichment_keeps_claim_hint_when_evidence_is_weak(monkeypatch, tmp_path, result):
    monkeypatch.setattr(codex_oauth, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(codex_oauth._cfg, "CODEX_OUTPUT_DIRNAME", "codex_accounts")
    storage = oauth_storage(plan_type="team")
    monkeypatch.setattr("core.chatgpt_plan.check_account_plan", lambda *_args, **_kwargs: result)
    path = codex_oauth.save_codex_credential(storage, storage["email"], "team")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["plan_type"] == "team"
    assert saved["plan_check_ok"] is False
    assert saved["plan_authority"] == "token_claim"
    assert saved["account_id"] == "account-1"
    assert "fixture backend failed" not in path.read_text(encoding="utf-8")


def test_invalid_identity_does_not_query_or_write(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_oauth, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(codex_oauth._cfg, "CODEX_OUTPUT_DIRNAME", "codex_accounts")
    storage = oauth_storage(email="other@example.test")
    called = Mock(side_effect=AssertionError("plan query must not run"))
    monkeypatch.setattr("core.chatgpt_plan.check_account_plan", called)
    with pytest.raises(ValueError, match="邮箱"):
        codex_oauth.save_codex_credential(storage, "one@example.test", "free")
    assert not (tmp_path / "codex_accounts").exists()
    called.assert_not_called()


def test_storage_and_jwt_account_id_mismatch_does_not_query_or_write(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_oauth, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(codex_oauth._cfg, "CODEX_OUTPUT_DIRNAME", "codex_accounts")
    storage = oauth_storage(account_id="account-1")
    storage["account_id"] = "account-2"
    called = Mock(side_effect=AssertionError("plan query must not run"))
    monkeypatch.setattr("core.chatgpt_plan.check_account_plan", called)
    with pytest.raises(ValueError, match="账号 ID"):
        codex_oauth.save_codex_credential(storage, storage["email"], "free")
    assert not (tmp_path / "codex_accounts").exists()
    called.assert_not_called()


def test_plan_label_is_safe_and_cannot_escape_output_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_oauth, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(codex_oauth._cfg, "CODEX_OUTPUT_DIRNAME", "codex_accounts")
    storage = oauth_storage()
    monkeypatch.setattr("core.chatgpt_plan.check_account_plan", lambda *_args, **_kwargs: {"ok": False})
    with pytest.raises(ValueError, match="套餐标签"):
        codex_oauth.save_codex_credential(storage, storage["email"], "../outside")
    assert not (tmp_path / "outside.json").exists()


def test_extract_cpa_auth_json_requires_unique_explicit_wrapper_and_normalizes_camel_case():
    credentials = {"accessToken": "access-fixture", "refreshToken": "refresh-fixture",
                   "idToken": "id-fixture", "email": "one@example.test"}
    payload = {"data": {"credentials": credentials}}
    auth = codex_oauth._extract_cpa_auth_json(payload)
    assert auth["access_token"] == "access-fixture"
    assert auth["refresh_token"] == "refresh-fixture"
    assert auth["id_token"] == "id-fixture"
    assert auth["email"] == "one@example.test"
    # A list of accounts is not an unambiguous callback credential.
    assert codex_oauth._extract_cpa_auth_json({"accounts": [credentials]}) is None
    assert codex_oauth._extract_cpa_auth_json({"unexpected": [credentials]}) is None


def test_cpa_download_error_keeps_receipt_without_starting_another_oauth(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_oauth, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(codex_oauth._cfg, "CODEX_OUTPUT_DIRNAME", "codex_accounts")
    monkeypatch.setattr(codex_oauth._cfg, "CPA_SAVE_CALLBACK_RECEIPT", True)
    monkeypatch.setattr(codex_oauth.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(codex_oauth, "download_cpa_codex_auth_text",
                        Mock(side_effect=RuntimeError("fixture download error")))
    oauth = Mock(side_effect=AssertionError("callback download must not restart OAuth"))
    monkeypatch.setattr(codex_oauth, "run_codex_oauth", oauth)
    path = codex_oauth._save_cpa_local_record(
        email="one@example.test", callback_url="http://callback", auth_url="http://auth",
        state="state", submit_payload={"message": "ok"},
    )
    assert path and path.name.endswith("-cpa-callback.json")
    assert "fixture download error" not in path.read_text(encoding="utf-8")
    oauth.assert_not_called()


@pytest.mark.parametrize("saver", [codex_oauth._save_cpa_local_record, codex_oauth._save_sub2_local_record])
def test_callback_savers_delegate_plan_lookup_to_single_save_entry(monkeypatch, saver, tmp_path):
    monkeypatch.setattr(codex_oauth, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(codex_oauth._cfg, "CODEX_OUTPUT_DIRNAME", "codex_accounts")
    storage = oauth_storage()
    payload = {"auth_json": storage}
    calls = []

    def fake_save(auth, email, plan, *, proxy=None):
        calls.append((auth, email, plan, proxy))
        return tmp_path / "saved.json"

    monkeypatch.setattr(codex_oauth, "save_codex_credential", fake_save)
    result = saver(email=storage["email"], callback_url="http://callback", auth_url="http://auth",
                   state="state", submit_payload=payload)
    assert result == tmp_path / "saved.json"
    assert len(calls) == 1
    assert calls[0][0]["access_token"] == storage["access_token"]
    assert calls[0][1] == storage["email"] and calls[0][2] == ""


def test_access_only_requires_expiry_and_never_uses_id_token_expiry(export_fixture):
    _, storage, _, _ = export_fixture
    storage.pop("refresh_token")
    storage["id_token"] = jwt({"exp": 1})
    item = codex_oauth.build_sub2api_oauth_account(storage, email=storage["email"])
    assert item["auto_pause_on_expired"] is True and item["expires_at"] == 1893456000
    assert "client_id" not in item["credentials"]
    storage.pop("expired")
    with pytest.raises(ValueError, match="过期时间"):
        codex_oauth.build_sub2api_oauth_account(storage, email=storage["email"])


def test_account_toolbar_reuses_selection_and_export_handler(export_fixture):
    client, _, _, _ = export_fixture
    html = client.get("/").get_data(as_text=True)
    assert 'id="btnDownloadSelectedSub2V2" disabled' in html
    assert "'btnDownloadSelectedCpaV2', 'btnDownloadSelectedSub2V2'" in html
    assert "downloadSelectedCodex('sub2')" in html
    assert "JSON.stringify({account_ids: ids, format, prepare: true})" in html
    assert "downloadSelectedCpa" not in html and "download-cpa-bulk" not in html
