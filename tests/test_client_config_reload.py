from types import SimpleNamespace
from unittest.mock import Mock

from config import browser, email, register
from core import outlook_client, sms_provider
import main


def test_new_mail_and_sms_clients_read_current_settings(monkeypatch):
    monkeypatch.setattr(browser, "IMPERSONATE", "current-profile")
    monkeypatch.setattr(browser, "USER_AGENT", "current-user-agent")
    monkeypatch.setattr(email, "OUTLOOK_API_BASE", "https://mail.example.test/")
    mail_factory = Mock(side_effect=lambda **kwargs: SimpleNamespace(headers={}))
    sms_factory = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(outlook_client, "CurlSession", mail_factory)
    monkeypatch.setattr(sms_provider, "CurlSession", sms_factory)
    http = outlook_client._http_session()
    outlook_client._ms_http()
    monkeypatch.setattr(sms_provider._cfg, "resolve_local_proxy", lambda: "http://127.0.0.1:7897")
    sms_provider._http()
    assert http.headers["Origin"] == "https://mail.example.test"
    assert http.headers["User-Agent"] == "current-user-agent"
    assert all(call.kwargs["impersonate"] == "current-profile" for call in mail_factory.call_args_list)
    sms_factory.assert_called_once_with(impersonate="current-profile", trust_env=False)


def test_mail_security_cache_is_bound_to_original_session_host(monkeypatch):
    monkeypatch.setattr(outlook_client, "_sec_session", None)
    monkeypatch.setattr(email, "OUTLOOK_API_BASE", "https://new.example.test")
    response = SimpleNamespace(status_code=200, json=lambda: {
        "success": True, "sessionId": "test-id", "sessionToken": "test-token",
        "sessionKey": "test-key", "expiresAt": "2099-01-01T00:00:00Z",
    })
    old = SimpleNamespace(headers={"Origin": "https://old.example.test"}, post=Mock(return_value=response))
    new = SimpleNamespace(headers={"Origin": "https://new.example.test"}, post=Mock(return_value=response))
    assert outlook_client._get_security_session(old)["baseUrl"] == old.headers["Origin"]
    outlook_client._get_security_session(old)
    old.post.assert_called_once()
    assert outlook_client._get_security_session(new)["baseUrl"] == new.headers["Origin"]
    new.post.assert_called_once()
    assert old.post.call_args.args[0] == "https://old.example.test/api/security-session"


def test_disabling_old_mail_host_does_not_disable_new_host(monkeypatch):
    monkeypatch.setattr(email, "OUTLOOK_FETCH_MODE", "auto")
    monkeypatch.setattr(outlook_client, "_REMOTE_DISABLED_BASE", "https://old.example.test")
    post = Mock(return_value={"success": True, "emails": []})
    monkeypatch.setattr(outlook_client, "_secure_post", post)
    http = SimpleNamespace(headers={"Origin": "https://new.example.test"})
    account = outlook_client.OutlookAccount("user@example.test", "", "client", "test-refresh")
    assert outlook_client._fetch_via(http, "graph", account) == []
    assert post.call_args.args[1] == "https://new.example.test/api/fetch-graph"


def test_cli_input_defaults_follow_reloaded_register_module(monkeypatch):
    monkeypatch.setattr(register, "REGISTER_EMAIL", "current@example.test")
    monkeypatch.setattr(register, "REGISTER_NAME", "Current User")
    email_value, name, _ = main.prepare_registration_inputs()
    assert (email_value, name) == ("current@example.test", "Current User")
