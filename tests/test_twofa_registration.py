from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from core import account_export


@pytest.fixture(autouse=True)
def _disable_real_security_checkpoint_writes(monkeypatch):
    """本模块使用伪账号验证状态机，禁止测试凭据写进工作区运行时文件。"""
    from core import registration_password

    monkeypatch.setattr(account_export, "_persist_activated_totp_checkpoint", lambda *args: True)
    monkeypatch.setattr(
        registration_password,
        "persist_confirmed_registration_password",
        lambda *args: True,
    )


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
    monkeypatch.setattr(
        account_export,
        "_persist_activated_totp_checkpoint",
        lambda email, secret, token: calls.append("checkpoint") or True,
    )
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
    assert result.totp_checkpoint_persisted is True
    assert mail_kwargs["settle_seconds"] == 1
    assert mail_kwargs["exclude_codes"] == set()
    assert calls == [
        "reauth", "follow", "mail", "otp", "token", "enroll", "window",
        "activate", "checkpoint", "validate",
    ]


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
    checkpoints = []
    monkeypatch.setattr(
        account_export,
        "_persist_activated_totp_checkpoint",
        lambda *args: checkpoints.append(args) or True,
    )
    import core.email_provider

    monkeypatch.setattr(core.email_provider, "wait_for_otp", lambda *args, **kwargs: "123456")
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export.setup_2fa_result(fake_session, "user@example.com")
    assert exc_info.value.code == "totp_activate_failed"
    assert checkpoints == []


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


def test_maybe_setup_2fa_disabled_skips_mfa_and_password(monkeypatch) -> None:
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", False)
    monkeypatch.setattr(
        account_export,
        "import_browser_cookies",
        lambda *args, **kwargs: pytest.fail("disabled switch must not import cookies"),
    )
    monkeypatch.setattr(
        account_export,
        "setup_2fa_result",
        lambda *args, **kwargs: pytest.fail("disabled switch must not enroll MFA or set password"),
    )

    session = SimpleNamespace()
    assert account_export.maybe_setup_2fa_result(session, "user@example.com", driver=object()) is None
    assert session._twofa_last_error is None


def test_twofa_defaults_are_disabled_and_isolated_from_registration_timeout() -> None:
    from config import twofa
    from pathlib import Path
    from webui import config_editor

    source = Path(twofa.__file__).read_text(encoding="utf-8")
    assert config_editor._parse_value_from_source(source, "ENABLE_2FA", "bool") is False
    assert twofa.TWOFA_GENERIC_API_REQUEST_TIMEOUT >= 10
    assert twofa.TWOFA_GENERIC_API_RETRY_TIMEOUT >= 5


def test_password_requirement_follows_enable_2fa_only(monkeypatch) -> None:
    from config import twofa
    from core.registration_password import registration_password_required

    monkeypatch.setattr(twofa, "ENABLE_2FA", False)
    assert registration_password_required() is False
    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
    assert registration_password_required() is True


def test_twofa_security_requires_a_confirmed_nonempty_password() -> None:
    skipped = account_export.TwoFASetupResult(
        secret="JBSWY3DPEHPK3PXP",
        access_token="token",
        activated_at="2026-08-22T00:00:00+00:00",
        validation_status=200,
        password=None,
        password_setup={"ok": True, "status": "skipped"},
    )
    assert skipped.password_configured is False
    assert skipped.security_ok is False

    confirmed = account_export.TwoFASetupResult(
        secret="JBSWY3DPEHPK3PXP",
        access_token="token",
        activated_at="2026-08-22T00:00:00+00:00",
        validation_status=200,
        password="Strong-pass-1!",
        password_setup={"ok": True, "status": "already_configured"},
    )
    assert confirmed.password_configured is True
    assert confirmed.security_ok is True


def test_setup_wrapper_preserves_totp_validation_failure(monkeypatch) -> None:
    result = account_export.TwoFASetupResult(
        secret="JBSWY3DPEHPK3PXP",
        access_token="token",
        activated_at="2026-08-22T00:00:00+00:00",
        validation_status=429,
        validation_ok=False,
        validation_code="totp_token_validation_failed",
        validation_message="models HTTP 429",
        password="Strong-pass-1!",
        password_setup={"ok": True, "status": "success"},
    )
    monkeypatch.setattr(account_export, "_setup_2fa_result", lambda *args, **kwargs: result)
    session = SimpleNamespace()

    assert account_export.setup_2fa_result(session, "user@example.com") is result
    assert session._twofa_last_error == {
        "stage": "totp_validate",
        "code": "totp_token_validation_failed",
        "http_status": 429,
        "message": "models HTTP 429",
    }


def test_password_done_callback_requires_trusted_explicit_stage() -> None:
    assert account_export._password_done_callback(
        "https://chatgpt.com/?tm_action=password&tm_stage=password_done"
    ) is True
    assert account_export._password_done_callback("https://chatgpt.com/") is False
    assert account_export._password_done_callback(
        "https://example.invalid/?tm_action=password&tm_stage=password_done"
    ) is False


def test_browser_use_password_is_returned_only_after_confirmed_transition(monkeypatch) -> None:
    from core import browser_use_registration as browser_use

    class Keyboard:
        def press(self, _key):
            return None

    page = SimpleNamespace(keyboard=Keyboard())
    states = iter([
        {"state": "password", "url": "https://auth.openai.com/create-account/password"},
        {"state": "email_verification", "url": "https://auth.openai.com/email-verification"},
    ])
    monkeypatch.setattr(browser_use, "_browser_use_heartbeat", lambda page, **kwargs: page)
    monkeypatch.setattr(browser_use, "_quick_auth_state", lambda page: next(states))
    monkeypatch.setattr(browser_use, "_fill_first", lambda *args, **kwargs: True)
    monkeypatch.setattr(browser_use, "_click_first", lambda *args, **kwargs: True)
    monkeypatch.setattr(browser_use, "_bu_delay", lambda *args, **kwargs: None)
    checkpoints = []

    assert browser_use._fill_password_if_present(
        page,
        "user@example.com",
        timeout=5,
        allow_passwordless=False,
        password="Stable-pass-1!",
        on_confirmed=lambda *args: checkpoints.append(args),
    ) == "Stable-pass-1!"
    assert checkpoints == [("user@example.com", "Stable-pass-1!")]


def test_browser_use_password_rejection_is_not_persisted(monkeypatch) -> None:
    from core import browser_use_registration as browser_use

    page = SimpleNamespace(keyboard=SimpleNamespace(press=lambda _key: None))
    states = iter([
        {"state": "password", "url": "https://auth.openai.com/create-account/password"},
        {
            "state": "password",
            "url": "https://auth.openai.com/create-account/password",
            "textPreview": "password is invalid, try again",
        },
    ])
    monkeypatch.setattr(browser_use, "_browser_use_heartbeat", lambda page, **kwargs: page)
    monkeypatch.setattr(browser_use, "_quick_auth_state", lambda page: next(states))
    monkeypatch.setattr(browser_use, "_fill_first", lambda *args, **kwargs: True)
    monkeypatch.setattr(browser_use, "_click_first", lambda *args, **kwargs: True)
    monkeypatch.setattr(browser_use, "_bu_delay", lambda *args, **kwargs: None)
    checkpoints = []

    with pytest.raises(RuntimeError, match="registration_password_rejected"):
        browser_use._fill_password_if_present(
            page,
            "user@example.com",
            timeout=5,
            allow_passwordless=False,
            password="Stable-pass-1!",
            on_confirmed=lambda *args: checkpoints.append(args),
        )
    assert checkpoints == []


def test_browser_use_anonymous_chatgpt_landing_does_not_confirm_password(monkeypatch) -> None:
    from core import browser_use_registration as browser_use

    page = SimpleNamespace()
    clock = {"value": 0.0}

    def now():
        clock["value"] += 1.0
        return clock["value"]

    monkeypatch.setattr(browser_use.time, "time", now)
    monkeypatch.setattr(browser_use.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(browser_use, "_check_manual_stop", lambda: None)
    monkeypatch.setattr(browser_use, "_browser_use_heartbeat", lambda page, **kwargs: page)
    monkeypatch.setattr(
        browser_use,
        "_quick_auth_state",
        lambda page: {"state": "chatgpt", "url": "https://chatgpt.com/", "textPreview": "Welcome"},
    )
    monkeypatch.setattr(browser_use, "_has_chatgpt_access_token", lambda page, expected_email=None: False)

    with pytest.raises(RuntimeError, match="registration_password_submit_timeout"):
        browser_use._wait_after_password_submit(
            page,
            email="user@example.com",
            timeout=3,
        )


def test_browser_use_chatgpt_session_must_match_expected_email(monkeypatch) -> None:
    from core import browser_use_registration as browser_use

    page = SimpleNamespace(
        url="https://chatgpt.com/",
        evaluate=lambda _script: {
            "accessToken": "token",
            "user": {"email": "other@example.com"},
        },
    )

    assert browser_use._has_chatgpt_access_token(page) is True
    assert browser_use._has_chatgpt_access_token(page, expected_email="other@example.com") is True
    assert browser_use._has_chatgpt_access_token(page, expected_email="user@example.com") is False


def test_browser_use_disabled_twofa_does_not_generate_password(monkeypatch) -> None:
    from core import browser_use_registration as browser_use

    page = SimpleNamespace()
    monkeypatch.setattr(browser_use, "_browser_use_heartbeat", lambda page, **kwargs: page)
    monkeypatch.setattr(
        browser_use,
        "_quick_auth_state",
        lambda page: {"state": "password", "url": "https://auth.openai.com/create-account/password"},
    )
    monkeypatch.setattr(browser_use, "_click_passwordless_signup_if_present", lambda page: False)
    monkeypatch.setattr(
        browser_use,
        "_registration_password",
        lambda: pytest.fail("disabled switch must not generate a password"),
    )

    with pytest.raises(RuntimeError, match="password_setup_disabled"):
        browser_use._fill_password_if_present(
            page,
            "user@example.com",
            timeout=5,
            allow_passwordless=True,
        )


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


def test_setup_2fa_result_adds_password_when_enable_2fa_with_driver(monkeypatch) -> None:
    """ENABLE_2FA=True + driver 时，TOTP 激活后会补设账号密码并透传结果。"""
    from config import email as email_cfg
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
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
    monkeypatch.setattr(account_export, "_validate_2fa_token", lambda *args: 200)
    import core.email_provider
    monkeypatch.setattr(core.email_provider, "wait_for_otp", lambda *args, **kwargs: "123456")

    password_setup_calls: list[dict] = []

    def fake_setup_password(*, driver, session, email, password, totp_secret, timeout_seconds=120.0):
        password_setup_calls.append({"driver": driver, "password": password, "totp_secret": totp_secret})
        return {"ok": True, "status": "success", "stage": "password_done", "code": "password_setup_success", "message": "密码已补设", "password": password}

    monkeypatch.setattr(account_export, "_setup_password_with_driver", fake_setup_password)

    from core import roxy_registration
    from core import registration_password
    monkeypatch.setattr(
        roxy_registration,
        "_registration_password",
        lambda: pytest.fail("task desired_password must be reused"),
    )
    password_checkpoints = []
    monkeypatch.setattr(
        registration_password,
        "persist_confirmed_registration_password",
        lambda email, password: password_checkpoints.append((email, password)) or True,
    )

    fake_driver = object()
    result = account_export.setup_2fa_result(
        object(),
        "user@example.com",
        driver=fake_driver,
        desired_password="Ab3!cdefgh123",
    )

    assert result.secret == "JBSWY3DPEHPK3PXP"
    assert result.password == "Ab3!cdefgh123"
    assert result.password_setup is not None
    assert result.password_setup["ok"] is True
    assert result.password_setup["checkpoint_persisted"] is True
    assert result.checkpoint == {"totp_persisted": True, "password_persisted": True}
    assert len(password_setup_calls) == 1
    assert password_setup_calls[0]["driver"] is fake_driver
    assert password_setup_calls[0]["password"] == "Ab3!cdefgh123"
    assert password_setup_calls[0]["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert password_checkpoints == [("user@example.com", "Ab3!cdefgh123")]


def test_setup_2fa_result_keeps_secret_when_password_setup_fails(monkeypatch) -> None:
    """补设密码失败不能影响账号保存与 TOTP Secret 返回。"""
    from config import email as email_cfg
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
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
    monkeypatch.setattr(account_export, "_validate_2fa_token", lambda *args: 200)
    import core.email_provider
    monkeypatch.setattr(core.email_provider, "wait_for_otp", lambda *args, **kwargs: "123456")

    def fake_setup_password_fail(**kwargs):
        raise account_export.TwoFASetupError("password_setup", "password_setup_failed", "补设失败")

    monkeypatch.setattr(account_export, "_setup_password_with_driver", fake_setup_password_fail)

    from core import registration_password, roxy_registration
    monkeypatch.setattr(roxy_registration, "_registration_password", lambda: "Ab3!cdefgh123")
    password_checkpoints = []
    monkeypatch.setattr(
        registration_password,
        "persist_confirmed_registration_password",
        lambda *args: password_checkpoints.append(args) or True,
    )

    result = account_export.setup_2fa_result(object(), "user@example.com", driver=object())

    assert result.secret == "JBSWY3DPEHPK3PXP"
    assert result.access_token == "fresh-token"
    assert result.password is None  # 失败则不带密码
    assert result.password_setup is not None
    assert result.password_setup["ok"] is False
    assert result.password_setup["code"] == "password_setup_failed"
    assert password_checkpoints == []


def test_password_setup_uses_selenium_async_callback_and_skips_totp_when_password_page_is_ready(monkeypatch) -> None:
    """Selenium/Cloak 分支必须等待异步重认证结果，且可直接提交密码页。"""
    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: set())

    class Field:
        def __init__(self, owner):
            self.owner = owner
            self.values = []

        def is_displayed(self):
            return not self.owner.submitted

        def is_enabled(self):
            return True

        def clear(self):
            self.values.clear()

        def send_keys(self, value):
            self.values.append(str(value))

        def find_element(self, *_args):
            return self.owner.form

    class Form:
        def __init__(self, owner):
            self.owner = owner

        def find_element(self, *_args):
            return self.owner.submit

    class Submit:
        def __init__(self, owner):
            self.owner = owner

        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

        def click(self):
            self.owner.submitted = True

    class Driver:
        def __init__(self):
            self.submitted = False
            self.current_url = "https://auth.openai.com/create-account/password"
            self.async_scripts = []
            self.password = Field(self)
            self.form = Form(self)
            self.submit = Submit(self)

        def execute_async_script(self, script, *args):
            self.async_scripts.append(script)
            return {"ok": True, "stage": "signin", "status": 200, "url": "https://auth.openai.com/reauth"}

        def execute_script(self, script, *args):
            if "document.body" in script:
                return "Password updated" if self.submitted else "Set a password"
            return False

        def get(self, url):
            self.current_url = url

        def find_elements(self, *_args):
            selector = str(_args[-1])
            if "password" in selector and not self.submitted:
                return [self.password]
            return []

        def save_screenshot(self, _path):
            return True

    driver = Driver()
    result = account_export._setup_password_with_driver(
        driver=driver,
        session=object(),
        email="user@example.com",
        password="Ab3!cdefgh123",
        totp_secret="JBSWY3DPEHPK3PXP",
        timeout_seconds=30,
    )

    assert result["ok"] is True, result
    assert result["code"] == "password_setup_success"
    assert result["password"] == "Ab3!cdefgh123"
    assert driver.async_scripts
    assert "execute_async" not in driver.async_scripts[0]
    assert "const done = arguments[arguments.length - 1]" in driver.async_scripts[0]
    assert "post_login_add_password" in driver.async_scripts[0]


def test_password_resend_invokes_arrow_function_for_selenium() -> None:
    class Driver:
        def __init__(self):
            self.scripts = []

        def execute_script(self, script):
            self.scripts.append(script)
            return True

    driver = Driver()
    assert account_export._password_click_resend(driver) is True
    assert driver.scripts
    assert driver.scripts[0].startswith("return (")
    assert driver.scripts[0].rstrip().endswith(")();")


def test_password_setup_uses_playwright_evaluate_for_async_reauth(monkeypatch) -> None:
    """Browser Use/Skyvern Playwright 页面走 evaluate，而不是 Selenium API。"""
    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: set())

    class Locator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector
            self.first = self

        def count(self):
            if "password" in self.selector:
                return 0 if self.page.submitted else 1
            if "one-time-code" in self.selector or "name=\'code\'" in self.selector or "inputmode" in self.selector:
                return 0
            if "button" in self.selector or "input[type='submit']" in self.selector:
                return 1
            return 1

        def nth(self, _index):
            return self

        def is_visible(self, timeout=0):
            if "password" in self.selector:
                return not self.page.submitted
            return True

        def inner_text(self, timeout=0):
            return "Password updated" if self.page.submitted else "Set a password"

        def locator(self, selector):
            return Locator(self.page, selector)

        def fill(self, value, timeout=0):
            self.page.filled = str(value)

        def click(self, timeout=0):
            self.page.submitted = True

    class Page:
        def __init__(self):
            self.url = "https://chatgpt.com/"
            self.submitted = False
            self.filled = ""
            self.evaluate_calls = []

        def evaluate(self, script, *args):
            self.evaluate_calls.append(script)
            if "api/auth/session" in script:
                return {"ok": True, "stage": "signin", "status": 200, "url": "https://auth.openai.com/reauth"}
            return False

        def locator(self, selector):
            return Locator(self, selector)

        def goto(self, url, **_kwargs):
            self.url = url

        def screenshot(self, **_kwargs):
            return None

    page = Page()
    result = account_export._setup_password_with_driver(
        driver=page,
        session=object(),
        email="user@example.com",
        password="Ab3!cdefgh123",
        totp_secret=None,
        timeout_seconds=30,
    )

    assert result["ok"] is True
    assert result["password"] == "Ab3!cdefgh123"
    assert any("api/auth/session" in call for call in page.evaluate_calls)


def test_password_setup_handles_email_reauth_code_before_password(monkeypatch) -> None:
    """密码补设重认证落到邮箱验证码页时，先取新码再提交密码。"""
    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: set())
    import core.email_provider
    monkeypatch.setattr(core.email_provider, "wait_for_otp", lambda *args, **kwargs: "654321")

    class Locator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector
            self.first = self

        def count(self):
            if "password" in self.selector:
                return 1 if self.page.stage == "password" else 0
            if "one-time-code" in self.selector or "name=\'code\'" in self.selector or "inputmode" in self.selector:
                return 1 if self.page.stage == "email" else 0
            if "button" in self.selector or "input[type='submit']" in self.selector:
                return 1
            return 1

        def nth(self, _index):
            return self

        def is_visible(self, timeout=0):
            return self.count() > 0

        def inner_text(self, timeout=0):
            if self.page.stage == "email":
                return "Enter the verification code sent to your email"
            if self.page.stage == "password":
                return "Set a password"
            return "Password updated"

        def locator(self, selector):
            return Locator(self.page, selector)

        def fill(self, value, timeout=0):
            self.page.last_fill = str(value)

        def click(self, timeout=0):
            if self.page.stage == "email":
                self.page.stage = "password"
            elif self.page.stage == "password":
                self.page.stage = "done"

    class Page:
        def __init__(self):
            self.url = "https://chatgpt.com/"
            self.stage = "email"
            self.last_fill = ""
            self.evaluate_calls = []

        def evaluate(self, script, *args):
            self.evaluate_calls.append(script)
            if "api/auth/session" in script:
                return {"ok": True, "stage": "signin", "status": 200, "url": "https://auth.openai.com/reauth"}
            return False

        def locator(self, selector):
            return Locator(self, selector)

        def goto(self, url, **_kwargs):
            self.url = "https://auth.openai.com/email-verification"

        def screenshot(self, **_kwargs):
            return None

    page = Page()
    result = account_export._setup_password_with_driver(
        driver=page,
        session=object(),
        email="user@example.com",
        password="Ab3!cdefgh123",
        totp_secret=None,
        timeout_seconds=30,
    )

    assert result["ok"] is True, result
    assert result["email_reauth_used"] is True
    assert result["totp_reauth_used"] is False


def test_setup_2fa_does_not_repeat_password_when_signup_already_set_it(monkeypatch) -> None:
    """初始 create-account/password 成功后，TOTP 阶段不能再次改密。"""
    from config import email as email_cfg
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
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
    monkeypatch.setattr(account_export, "_validate_2fa_token", lambda *args: 200)
    import core.email_provider
    monkeypatch.setattr(core.email_provider, "wait_for_otp", lambda *args, **kwargs: "123456")
    called = []
    monkeypatch.setattr(account_export, "_setup_password_with_driver", lambda **kwargs: called.append(kwargs) or {"ok": True})
    from core import registration_password
    password_checkpoints = []
    monkeypatch.setattr(
        registration_password,
        "persist_confirmed_registration_password",
        lambda email, password: password_checkpoints.append((email, password)) or True,
    )

    result = account_export.setup_2fa_result(
        object(),
        "user@example.com",
        driver=object(),
        existing_password="Existing-pass-1!",
    )

    assert result.password == "Existing-pass-1!"
    assert result.password_setup["code"] == "password_already_configured"
    assert called == []
    assert password_checkpoints == [("user@example.com", "Existing-pass-1!")]
