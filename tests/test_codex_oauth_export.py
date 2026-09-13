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
