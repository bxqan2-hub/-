from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from core import account_export


def test_normalize_totp_secret_validates_base32() -> None:
    assert account_export.normalize_totp_secret(" jbsw y3dp ehpk 3pxp ") == "JBSWY3DPEHPK3PXP"
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export.normalize_totp_secret("not-a-secret")
    assert exc_info.value.code == "totp_enroll_response_invalid"


def test_reauth_urls_are_limited_to_https_openai_hosts() -> None:
    assert account_export._validate_trusted_openai_url(
        "https://auth.openai.com/authorize/x",
        stage="test",
        code="bad",
        message="bad",
    ).startswith("https://auth.openai.com/")
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export._validate_trusted_openai_url(
            "https://example.invalid/callback",
            stage="test",
            code="untrusted",
            message="bad",
        )
    assert exc_info.value.code == "untrusted"


def test_setup_2fa_result_returns_fresh_token_and_validates_models(monkeypatch) -> None:
    calls: list[str] = []
    fake_session = object()
    mail_kwargs = {}

    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: set())
    monkeypatch.setattr(account_export, "_trigger_reauth", lambda session, email: calls.append("reauth") or "https://auth.openai.com/authorize/x")
    monkeypatch.setattr(account_export, "_follow_reauth", lambda session, url: calls.append("follow") or url)
    monkeypatch.setattr(account_export, "_validate_reauth_otp", lambda session, code: calls.append("otp") or "https://auth.openai.com/continue/x")
    monkeypatch.setattr(account_export, "_exchange_new_token", lambda session, url: calls.append("token") or "fresh-token")
    monkeypatch.setattr(account_export, "_enroll_totp", lambda session, token: calls.append("enroll") or ("JBSWY3DPEHPK3PXP", "sid"))
    monkeypatch.setattr(account_export, "_wait_for_totp_window", lambda: calls.append("window"))
    monkeypatch.setattr(account_export, "_activate_totp", lambda session, token, secret, sid: calls.append("activate") or True)
    monkeypatch.setattr(account_export, "_validate_2fa_token", lambda session, token: calls.append("validate") or 200)

    import core.email_provider

    monkeypatch.setattr(
        core.email_provider,
        "wait_for_otp",
        lambda *args, **kwargs: (calls.append("mail"), mail_kwargs.update(kwargs), "123456")[-1],
    )
    result = account_export.setup_2fa_result(fake_session, "user@example.com")

    assert result.secret == "JBSWY3DPEHPK3PXP"
    assert result.access_token == "fresh-token"
    assert result.validation_status == 200
    assert result.validation_ok is True
    assert result.validation["status"] == "passed"
    assert mail_kwargs["settle_seconds"] == 1
    assert mail_kwargs["exclude_codes"] == set()
    assert calls == ["reauth", "follow", "mail", "otp", "token", "enroll", "window", "activate", "validate"]


def test_setup_2fa_keeps_secret_when_models_validation_fails(monkeypatch) -> None:
    """激活成功后只读 models 失败不能让调用方丢失 TOTP Secret。"""
    fake_session = object()
    from config import email as email_cfg

    monkeypatch.setattr(email_cfg, "USE_EMAIL_SERVICE", True)
    monkeypatch.setattr(account_export, "human_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: set())
    monkeypatch.setattr(account_export, "_trigger_reauth", lambda *args: "https://auth.openai.com/authorize/x")
    monkeypatch.setattr(account_export, "_follow_reauth", lambda *args: "https://auth.openai.com/email-verification")
    monkeypatch.setattr(account_export, "_validate_reauth_otp", lambda *args: "https://auth.openai.com/continue/x")
    monkeypatch.setattr(account_export, "_exchange_new_token", lambda *args: "fresh-token")
    monkeypatch.setattr(account_export, "_enroll_totp", lambda *args: ("JBSWY3DPEHPK3PXP", "sid"))
    monkeypatch.setattr(account_export, "_wait_for_totp_window", lambda: None)
    monkeypatch.setattr(account_export, "_activate_totp", lambda *args: True)
    monkeypatch.setattr(
        account_export,
        "_validate_2fa_token",
        lambda *args: (_ for _ in ()).throw(
            account_export.TwoFASetupError(
                "totp_validate",
                "totp_token_validation_failed",
                "models returned HTTP 429",
                http_status=429,
            )
        ),
    )
    import core.email_provider

    monkeypatch.setattr(core.email_provider, "wait_for_otp", lambda *args, **kwargs: "123456")
    result = account_export.setup_2fa_result(fake_session, "user@example.com")

    assert result.secret == "JBSWY3DPEHPK3PXP"
    assert result.access_token == "fresh-token"
    assert result.validation_ok is False
    assert result.validation_status == 429
    assert result.validation_code == "totp_token_validation_failed"
    assert result.validation["status"] == "failed"
    assert result.validation_state == result.validation


def test_setup_2fa_does_not_treat_false_activation_as_success(monkeypatch) -> None:
    fake_session = object()
    from config import email as email_cfg

    monkeypatch.setattr(email_cfg, "USE_EMAIL_SERVICE", True)
    monkeypatch.setattr(account_export, "human_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: set())
    monkeypatch.setattr(account_export, "_trigger_reauth", lambda *args: "https://auth.openai.com/authorize/x")
    monkeypatch.setattr(account_export, "_follow_reauth", lambda *args: None)
    monkeypatch.setattr(account_export, "_validate_reauth_otp", lambda *args: "https://auth.openai.com/continue/x")
    monkeypatch.setattr(account_export, "_exchange_new_token", lambda *args: "fresh-token")
    monkeypatch.setattr(account_export, "_enroll_totp", lambda *args: ("JBSWY3DPEHPK3PXP", "sid"))
    monkeypatch.setattr(account_export, "_wait_for_totp_window", lambda: None)
    monkeypatch.setattr(account_export, "_activate_totp", lambda *args: False)
    import core.email_provider

    monkeypatch.setattr(core.email_provider, "wait_for_otp", lambda *args, **kwargs: "123456")
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export.setup_2fa_result(fake_session, "user@example.com")
    assert exc_info.value.code == "totp_activate_failed"


def test_setup_2fa_forwards_historical_otp_exclusion(monkeypatch) -> None:
    fake_session = object()
    from config import email as email_cfg

    monkeypatch.setattr(email_cfg, "USE_EMAIL_SERVICE", True)
    monkeypatch.setattr(account_export, "human_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: {"654321"})
    monkeypatch.setattr(account_export, "_trigger_reauth", lambda *args: "https://auth.openai.com/authorize/x")
    monkeypatch.setattr(account_export, "_follow_reauth", lambda *args: "https://auth.openai.com/email-verification")
    monkeypatch.setattr(account_export, "_validate_reauth_otp", lambda *args: "https://auth.openai.com/continue/x")
    monkeypatch.setattr(account_export, "_exchange_new_token", lambda *args: "fresh-token")
    monkeypatch.setattr(account_export, "_enroll_totp", lambda *args: ("JBSWY3DPEHPK3PXP", "sid"))
    monkeypatch.setattr(account_export, "_wait_for_totp_window", lambda: None)
    monkeypatch.setattr(account_export, "_activate_totp", lambda *args: True)
    monkeypatch.setattr(account_export, "_validate_2fa_token", lambda *args: 200)
    import core.email_provider

    captured = {}

    def fake_wait(*args, **kwargs):
        captured.update(kwargs)
        return "123456"

    monkeypatch.setattr(core.email_provider, "wait_for_otp", fake_wait)
    result = account_export.setup_2fa_result(fake_session, "user@example.com")

    assert result.validation_ok is True
    assert captured["exclude_codes"] == {"654321"}
    assert captured["settle_seconds"] == 1


def test_import_browser_cookies_supports_selenium_driver() -> None:
    class CookieJar:
        def __init__(self):
            self.values = []

        def set(self, name, value, domain=None, path="/"):
            self.values.append((name, value, domain, path))

    class Session:
        def __init__(self):
            self.session = SimpleNamespace(cookies=CookieJar())

        def _sync_device_id_from_cookie(self):
            return None

    class Driver:
        def get_cookies(self):
            return [
                {"name": "oai-did", "value": "device", "domain": ".chatgpt.com", "path": "/"},
                {"name": "session", "value": "cookie", "domain": "chatgpt.com", "path": "/"},
            ]

    session = Session()
    assert account_export.import_browser_cookies(session, Driver()) == 2
    assert len(session.session.cookies.values) == 2


def test_import_browser_cookies_strict_requires_auth_cookie() -> None:
    class Cookie:
        name = "oai-did"
        value = "device"
        domain = ".chatgpt.com"
        path = "/"

    class CookieJar:
        def __init__(self):
            self.jar = [Cookie()]
            self.values = []

        def set(self, name, value, domain=None, path="/"):
            self.values.append((name, value, domain, path))
            if "session-token" in name.lower():
                cookie = Cookie()
                cookie.name = name
                cookie.value = value
                cookie.domain = domain
                cookie.path = path
                self.jar.append(cookie)

    class Session:
        def __init__(self):
            self.session = SimpleNamespace(cookies=CookieJar())

        def _sync_device_id_from_cookie(self):
            return None

    class Driver:
        def get_cookies(self):
            return [{"name": "oai-did", "value": "device", "domain": ".chatgpt.com", "path": "/"}]

    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export.import_browser_cookies(Session(), Driver(), require_auth=True)
    assert exc_info.value.code == "cookie_auth_missing"


def test_maybe_setup_2fa_records_cookie_import_failure(monkeypatch) -> None:
    from config import email as email_cfg
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
    monkeypatch.setattr(email_cfg, "USE_EMAIL_SERVICE", True)

    class Driver:
        def get_cookies(self):
            raise RuntimeError("driver closed")

    class CookieJar:
        jar = []

    session = SimpleNamespace(session=SimpleNamespace(cookies=CookieJar()))
    result = account_export.maybe_setup_2fa_result(session, "user@example.com", driver=Driver())

    assert result is None
    assert session._twofa_last_error["stage"] == "cookie_import"
    assert session._twofa_last_error["code"] == "cookie_import_failed"


def test_inbox_mate_history_snapshot_uses_short_budget(monkeypatch) -> None:
    import core.inbox_mate_mail_client as inbox_mate

    captured = {}

    def fake_run_job(email, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(inbox_mate, "_run_job", fake_run_job)
    assert inbox_mate.snapshot_current_otp("user@example.com", timeout=2) is None
    assert captured["max_wait"] == 2
    assert captured["rescan_completed"] is False


def test_browser_session_close_is_idempotent() -> None:
    class Transport:
        def __init__(self):
            self.calls = 0

        def close(self):
            self.calls += 1

    session = object.__new__(account_export.BrowserSession)
    transport = Transport()
    session.session = transport
    session.close()
    session.close()
    assert transport.calls == 1


def test_reauth_navigation_http_error_fails_before_otp_wait() -> None:
    class Response:
        status_code = 500
        url = "https://auth.openai.com/error"

    class Session:
        def get_auth_navigate_headers(self, **kwargs):
            return {}

        def get(self, *args, **kwargs):
            return Response()

    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export._follow_reauth(Session(), "https://auth.openai.com/authorize/x")
    assert exc_info.value.code == "totp_reauth_navigation_failed"
    assert exc_info.value.http_status == 500


def test_reauth_callback_http_error_fails_before_session_fetch(monkeypatch) -> None:
    class Response:
        status_code = 502
        url = "https://chatgpt.com/api/auth/callback/openai"

    class Session:
        def get_auth_navigate_headers(self, **kwargs):
            return {}

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(
        account_export,
        "fetch_session",
        lambda *args, **kwargs: pytest.fail("HTTP error must fail before fetch_session"),
    )
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export._exchange_new_token(Session(), "https://auth.openai.com/continue/x")
    assert exc_info.value.code == "totp_session_refresh_failed"
    assert exc_info.value.http_status == 502


def test_maybe_setup_2fa_keeps_failures_non_fatal(monkeypatch) -> None:
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
    monkeypatch.setattr(account_export, "import_browser_cookies", lambda *args, **kwargs: 1)
    monkeypatch.setattr(account_export, "setup_2fa_result", lambda *args, **kwargs: (_ for _ in ()).throw(account_export.TwoFASetupError("totp_enroll", "totp_enroll_failed", "failed")))
    result = account_export.maybe_setup_2fa_result(object(), "user@example.com")
    assert result is None


def test_twofa_defaults_are_enabled_and_isolated_from_registration_timeout() -> None:
    from config import twofa

    assert twofa.ENABLE_2FA is True
    assert twofa.TWOFA_GENERIC_API_REQUEST_TIMEOUT >= 10
    assert twofa.TWOFA_GENERIC_API_RETRY_TIMEOUT >= 5


def test_twofa_transport_overrides_stay_on_generic_api_provider(monkeypatch) -> None:
    import core.email_provider as email_provider
    import core.mailnest_client as mailnest_client

    captured = {}

    def fake_fetch(email, after_ts=None, **kwargs):
        captured.update(kwargs)
        return "123456"

    monkeypatch.setattr(email_provider, "resolve_email_source", lambda email: "mailnest")
    monkeypatch.setattr(mailnest_client, "fetch_latest_otp", fake_fetch)
    assert email_provider.wait_for_otp(
        "user@example.com",
        after_ts=1,
        max_wait=10,
        request_timeout=12,
        retry_timeout=8,
        max_consecutive_errors=2,
    ) == "123456"
    assert "request_timeout" not in captured
    assert "retry_timeout" not in captured
    assert "max_consecutive_errors" not in captured


def test_twofa_proxy_resolution_never_falls_back_to_local_pool() -> None:
    from core.twofa_proxy import resolve_twofa_proxy

    assert resolve_twofa_proxy(
        {"latest": {"proxy": "socks5://user:pass@proxy.example:3010"}},
        source="CloakBrowser",
    ) == "socks5h://user:pass@proxy.example:3010"
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        resolve_twofa_proxy(None, {"query": {"proxyCountryCode": "jp"}}, source="BrowserUse")
    assert exc_info.value.code == "totp_proxy_unavailable"


def test_twofa_session_uses_explicit_proxy_without_random_pick(monkeypatch) -> None:
    import core.twofa_proxy as proxy_helper

    captured = {}

    class FakeSession:
        def __init__(self, *, proxy, detect_exit_geo):
            captured.update(proxy=proxy, detect_exit_geo=detect_exit_geo)
            self.proxy = proxy

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(proxy_helper, "BrowserSession", FakeSession)
    session = proxy_helper.build_twofa_session(
        "socks5://user:pass@proxy.example:3010",
        source="RoxyBrowser",
    )
    assert session.proxy == "socks5h://user:pass@proxy.example:3010"
    assert captured == {
        "proxy": "socks5h://user:pass@proxy.example:3010",
        "detect_exit_geo": False,
    }


def test_twofa_session_rejects_missing_proxy_before_constructing_session(monkeypatch) -> None:
    import core.twofa_proxy as proxy_helper

    called = []

    class NeverSession:
        def __init__(self, **kwargs):
            called.append(kwargs)

    monkeypatch.setattr(proxy_helper, "BrowserSession", NeverSession)
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        proxy_helper.build_twofa_session(None, source="BrowserUse")
    assert exc_info.value.code == "totp_proxy_unavailable"
    assert called == []


def test_twofa_failure_payload_does_not_copy_arbitrary_exception_text() -> None:
    from core.twofa_proxy import twofa_failure_payload

    payload = twofa_failure_payload(RuntimeError("proxy password should not be persisted"))
    assert payload == {
        "stage": "totp_setup",
        "code": "totp_setup_failed",
        "http_status": None,
        "message": "RuntimeError",
    }
