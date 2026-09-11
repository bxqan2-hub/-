from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core import account_export


@pytest.fixture(autouse=True)
def _disable_real_security_checkpoint_writes(monkeypatch):
    """本模块使用伪账号验证状态机，禁止测试凭据写进工作区运行时文件。"""
    from core import db, registration_password
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", False)
    monkeypatch.setattr(account_export, "_persist_activated_totp_checkpoint", lambda *args: True)
    monkeypatch.setattr(db, "save_security_checkpoint", lambda *args, **kwargs: {})
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


def test_browser_post_preserves_renderer_transport_detail() -> None:
    driver = SimpleNamespace(execute_async_script=Mock(side_effect=RuntimeError("renderer disconnected")))
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export._browser_authenticated_json_post(
            driver,
            "/backend-api/accounts/mfa/user/activate_enrollment",
            {},
            access_token="token",
            stage="totp_activate",
            code="totp_browser_activate_failed",
            message="浏览器 MFA activate 请求失败",
        )
    assert exc_info.value.http_status is None
    assert "stage=exception" in str(exc_info.value)
    assert "renderer disconnected" in str(exc_info.value)


@pytest.mark.parametrize("failure_kind", ["exception", "http"])
def test_browser_mfa_redacts_long_credentials_before_truncating(failure_kind, monkeypatch):
    token = "sensitive-token-prefix-" + "A" * 220
    secret = "JBSWY3DPEHPK3PXP"
    monkeypatch.setattr(account_export, "_wait_for_totp_window", lambda: None)
    enrollment = "sensitive-enrollment-" + "B" * 200
    detail = f"failed token={token} enrollment={enrollment} secret={secret} OTP=123456"
    if failure_kind == "exception":
        execute = Mock(side_effect=RuntimeError(detail))
    else:
        execute = Mock(return_value={"status": 403, "body": {"error": detail}})
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export._browser_authenticated_json_post(
            SimpleNamespace(execute_async_script=execute),
            "/backend-api/accounts/mfa/user/activate_enrollment", {"session_id": enrollment},
            access_token=token, totp_secret=secret, stage="totp_activate", code="totp_browser_activate_failed",
            message="MFA request failed",
        )
    assert "sensitive-token-prefix" not in str(exc_info.value)
    assert "sensitive-enrollment" not in str(exc_info.value)
    assert secret not in str(exc_info.value)
    assert "123456" not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)
    import traceback
    trace = "".join(traceback.format_exception(type(exc_info.value), exc_info.value, exc_info.value.__traceback__))
    for sensitive in (token, secret, enrollment, "123456", "sensitive-token-prefix", "sensitive-enrollment"):
        assert sensitive not in trace
    assert execute.call_count == 1


@pytest.fixture
def browser_mfa_replay(monkeypatch):
    replay = SimpleNamespace(responses=[], calls=[], sleeps=[], waits=[])

    def post(script, path, payload, access_token, expected_email):
        replay.calls.append((path, dict(payload), access_token, expected_email))
        result = replay.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    replay.driver = SimpleNamespace(
        current_url="https://chatgpt.com/", execute_async_script=Mock(side_effect=post),
    )
    monkeypatch.setattr(account_export.time, "sleep", replay.sleeps.append)
    monkeypatch.setattr(account_export, "_wait_for_totp_window", lambda: replay.waits.append(True))
    replay.enroll = {
        "status": 200, "stage": "request", "json": True, "email": "user@example.test",
        "accessToken": "browser-token-private",
        "body": {"secret": "JBSWY3DPEHPK3PXP", "session_id": "enrollment-private"},
    }
    replay.activate = {"status": 200, "stage": "request", "json": True, "body": {"success": True}}
    replay.temporary = {"status": 503, "stage": "request", "body": {}, "accessToken": "browser-token-private"}
    return replay


@pytest.mark.parametrize("status", [429, 503])
def test_browser_totp_enroll_recovers_temporary_http_with_same_token(browser_mfa_replay, caplog, status):
    replay = browser_mfa_replay
    first = dict(replay.temporary, status=status, email="user@example.test", expires="2026-09-12T00:00:00Z")
    replay.responses = [first, dict(replay.enroll, email=""), replay.activate]
    with caplog.at_level("INFO"):
        result = account_export._setup_totp_with_driver(
            replay.driver, "user@example.test", authenticated_email="user@example.test",
        )
    assert result == ("JBSWY3DPEHPK3PXP", "browser-token-private", "2026-09-12T00:00:00Z")
    assert [call[2] for call in replay.calls] == ["", "browser-token-private", "browser-token-private"]
    assert [call[3] for call in replay.calls] == ["user@example.test"] * 3
    assert [call[0].rsplit("/", 1)[-1] for call in replay.calls] == ["enroll", "enroll", "activate_enrollment"]
    assert replay.sleeps == [2]
    assert "stage=totp_enroll" in caplog.text and "attempt=2/3" in caplog.text
    for sensitive in ("browser-token-private", "enrollment-private", "JBSWY3DPEHPK3PXP"):
        assert sensitive not in caplog.text


@pytest.mark.parametrize("phase", ["enroll", "activate"])
def test_browser_totp_temporary_http_exhaustion_is_bounded(browser_mfa_replay, phase):
    replay = browser_mfa_replay
    replay.responses = ([replay.enroll] if phase == "activate" else []) + [replay.temporary] * 3
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export._setup_totp_with_driver(replay.driver, "user@example.test", access_token="browser-token-private")
    assert exc_info.value.stage == f"totp_{phase}"
    assert exc_info.value.http_status == 503
    assert "attempt=3/3" in str(exc_info.value)
    assert len(replay.calls) == (4 if phase == "activate" else 3)
    assert replay.sleeps == [2, 4]
    if phase == "activate":
        assert [call[1]["session_id"] for call in replay.calls[1:]] == ["enrollment-private"] * 3


@pytest.mark.parametrize("phase", ["enroll", "activate"])
@pytest.mark.parametrize("failure", [
    {"status": 401, "body": {"error": "token_revoked"}},
    {"status": 403, "body": {"error": "permission_denied"}},
    {"status": 408, "body": {}},
    {"status": 425, "body": {}},
    {"status": 500, "body": {}},
    {"status": 502, "body": {}},
    {"status": 504, "body": {}},
    {"status": 503, "body": {"error": {"code": "account_rejected"}}},
    {"status": 503, "body": {"session_id": "already-created"}},
    {"status": 503, "body": {"success": True}},
    {"status": 0, "stage": "exception", "error": "fetch failed", "body": {}},
    RuntimeError("renderer disconnected browser-token-private enrollment-private 123456"),
])
def test_browser_totp_does_not_replay_business_or_ambiguous_writes(browser_mfa_replay, phase, failure):
    replay = browser_mfa_replay
    replay.responses = ([replay.enroll] if phase == "activate" else []) + [failure]
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export._setup_totp_with_driver(replay.driver, "user@example.test", access_token="browser-token-private")
    assert exc_info.value.stage == f"totp_{phase}"
    assert len(replay.calls) == (2 if phase == "activate" else 1)
    assert replay.sleeps == []
    assert "browser-token-private" not in str(exc_info.value)
    assert "123456" not in str(exc_info.value)
    if phase == "activate":
        assert "enrollment-private" not in str(exc_info.value)
    if isinstance(failure, dict) and failure.get("status") == 401:
        assert "token_revoked" in str(exc_info.value)


def test_browser_totp_failed_enroll_retains_matched_refreshed_token_after_password(browser_mfa_replay, monkeypatch):
    from config import twofa
    from core import db, registration_password

    replay = browser_mfa_replay
    replay.responses = [dict(replay.temporary, email="user@example.test", expires="2026-09-12T00:00:00Z")] * 3
    checkpoints = []
    secret_checkpoints = []
    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
    monkeypatch.setattr(account_export, "_setup_password_with_driver", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(account_export, "import_browser_cookies", lambda *args, **kwargs: None)
    monkeypatch.setattr(registration_password, "persist_confirmed_registration_password", lambda *args: checkpoints.append(("password", args)) or True)
    monkeypatch.setattr(db, "save_security_checkpoint", lambda *args, **kwargs: checkpoints.append(("token", args, kwargs)) or {})
    monkeypatch.setattr(account_export, "_persist_activated_totp_checkpoint", lambda *args: secret_checkpoints.append(args))
    session = SimpleNamespace(_twofa_refreshed_access_token="old-context-token")
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export.setup_2fa_result(
            session, "user@example.test", driver=replay.driver,
            authenticated_email="user@example.test", access_token="revoked-registration-token",
            desired_password="Stable-pass-1!",
        )
    assert exc_info.value.stage == "totp_enroll" and exc_info.value.http_status == 503
    assert session._twofa_refreshed_access_token == "browser-token-private"
    assert session._twofa_session_expires == "2026-09-12T00:00:00Z"
    assert checkpoints == [
        ("password", ("user@example.test", "Stable-pass-1!")),
        ("token", ("user@example.test",), {"access_token": "browser-token-private"}),
    ]
    assert secret_checkpoints == []
    assert "browser-token-private" not in str(session._twofa_last_error)
    assert [call[2] for call in replay.calls] == ["", "browser-token-private", "browser-token-private"]


def test_browser_totp_password_failure_clears_previous_refreshed_token(browser_mfa_replay, monkeypatch):
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
    monkeypatch.setattr(account_export, "_setup_password_with_driver", lambda **kwargs: {
        "ok": False, "stage": "password_email", "code": "password_email_code_wait_failed",
    })
    session = SimpleNamespace(_twofa_refreshed_access_token="previous-operation-token")
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export.setup_2fa_result(
            session, "user@example.test", driver=browser_mfa_replay.driver,
            authenticated_email="user@example.test", desired_password="Stable-pass-1!",
        )
    assert exc_info.value.stage == "password_email"
    assert session._twofa_refreshed_access_token == ""
    assert browser_mfa_replay.calls == []


@pytest.mark.parametrize("failure_mode", ["mismatch", "explicit_token", "checkpoint_write"])
def test_browser_totp_refreshed_token_checkpoint_boundaries(browser_mfa_replay, monkeypatch, caplog, failure_mode):
    from core import db

    replay = browser_mfa_replay
    first = dict(replay.temporary, email="other@example.test" if failure_mode == "mismatch" else "user@example.test")
    replay.responses = [first] if failure_mode == "mismatch" else [first] * 3
    writes = []

    def checkpoint(*args, **kwargs):
        writes.append((args, kwargs))
        if failure_mode == "checkpoint_write":
            raise OSError("failure with browser-token-private")
        return {}

    monkeypatch.setattr(db, "save_security_checkpoint", checkpoint)
    context = SimpleNamespace(_twofa_refreshed_access_token="")
    with pytest.raises(account_export.TwoFASetupError):
        account_export._setup_totp_with_driver(
            replay.driver, "user@example.test", session_context=context,
            access_token="browser-token-private" if failure_mode == "explicit_token" else "",
        )
    if failure_mode == "checkpoint_write":
        assert context._twofa_refreshed_access_token == "browser-token-private"
        assert writes == [(("user@example.test",), {"access_token": "browser-token-private"})]
        assert "OSError" in caplog.text
    else:
        assert context._twofa_refreshed_access_token == ""
        assert writes == []
    assert "browser-token-private" not in caplog.text


def test_browser_totp_retry_stops_on_token_drift(browser_mfa_replay):
    replay = browser_mfa_replay
    replay.responses = [replay.temporary, dict(replay.enroll, accessToken="different-token")]
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export._setup_totp_with_driver(replay.driver, "user@example.test", access_token="browser-token-private")
    assert exc_info.value.code == "totp_session_refresh_failed"
    assert len(replay.calls) == 2 and replay.sleeps == [2]
    assert all(call[2] == "browser-token-private" for call in replay.calls)


def test_browser_totp_activate_retry_refreshes_code_without_reenroll(browser_mfa_replay, monkeypatch):
    replay = browser_mfa_replay
    replay.responses = [replay.enroll, replay.temporary, replay.activate]
    codes = Mock(side_effect=["123456", "654321"])
    monkeypatch.setattr(account_export.pyotp, "TOTP", lambda secret: SimpleNamespace(now=codes))
    secret, token, _expires = account_export._setup_totp_with_driver(
        replay.driver, "user@example.test", access_token="browser-token-private",
    )
    assert secret == "JBSWY3DPEHPK3PXP" and token == "browser-token-private"
    assert [call[0].rsplit("/", 1)[-1] for call in replay.calls] == ["enroll", "activate_enrollment", "activate_enrollment"]
    assert [call[1]["session_id"] for call in replay.calls[1:]] == ["enrollment-private"] * 2
    assert [call[1]["code"] for call in replay.calls[1:]] == ["123456", "654321"]
    assert replay.waits == [True, True]


@pytest.mark.parametrize("activation_body", [{"success": True}, {"success": False}, {"success": "true"}, {}])
def test_browser_totp_checkpoint_requires_confirmed_activation(browser_mfa_replay, monkeypatch, activation_body):
    from config import twofa

    replay = browser_mfa_replay
    replay.responses = [replay.enroll, replay.temporary, dict(replay.activate, body=activation_body)]
    checkpoints = []
    validations = []
    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
    monkeypatch.setattr(account_export, "_persist_activated_totp_checkpoint", lambda *args: checkpoints.append((len(replay.calls), args)) or True)
    monkeypatch.setattr(account_export, "_validate_2fa_token", lambda *args: validations.append(args) or 200)
    kwargs = dict(driver=replay.driver, existing_password="Stable-pass-1!", authenticated_email="user@example.test")
    if activation_body.get("success") is True:
        result = account_export.setup_2fa_result(SimpleNamespace(), "user@example.test", **kwargs)
        assert result.security_ok is True and result.totp_checkpoint_persisted is True
        assert checkpoints == [(3, ("user@example.test", "JBSWY3DPEHPK3PXP", "browser-token-private"))]
        assert len(validations) == 1
    else:
        with pytest.raises(account_export.TwoFASetupError) as exc_info:
            account_export.setup_2fa_result(SimpleNamespace(), "user@example.test", **kwargs)
        assert exc_info.value.code == "totp_activate_failed"
        assert checkpoints == [] and validations == []
    assert len(replay.calls) == 3


@pytest.mark.parametrize("runtime", ["selenium", "playwright"])
@pytest.mark.parametrize("session, expected_error", [
    ({"user": {"email": "user@example.test"}}, "missing_access_token"),
    ({"accessToken": "token", "user": {"email": "other@example.test"}}, "session_account_mismatch"),
    ({"accessToken": "token", "user": {}}, "missing_session_email"),
    ({"accessToken": "matched-session-token", "user": {"email": "user@example.test"},
      "expires": "2026-09-12T00:00:00Z"}, "fixture fetch failed matched-session-token"),
])
def test_browser_mfa_javascript_preserves_session_boundary_on_fetch_failure(runtime, session, expected_error, monkeypatch):
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute the browser script fixture")
    source = account_export._TOTP_BROWSER_POST_SELENIUM_JS if runtime == "selenium" else account_export._TOTP_BROWSER_POST_JS
    script = """
const {source, runtime, session} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const calls = [];
global.fetch = async (url, options = {}) => {
  calls.push({url, method: options.method || 'GET'});
  if (url !== '/api/auth/session') throw `fixture fetch failed ${session.accessToken}`;
  return {ok:true, status:200, json:async () => session};
};
const request = {path:'/backend-api/accounts/mfa/enroll', payload:{factor_type:'totp'},
  accessToken:'', expectedEmail:'user@example.test'};
const run = runtime === 'selenium'
  ? new Promise(resolve => new Function(source)(request.path, request.payload, '', request.expectedEmail, resolve))
  : eval(`(${source})`)(request);
run.then(result => process.stdout.write(JSON.stringify({result, calls})));
"""
    completed = subprocess.run(
        [node, "-e", script], input=json.dumps({"source": source, "runtime": runtime, "session": session}),
        text=True, capture_output=True, timeout=10, check=True,
    )
    outcome = json.loads(completed.stdout)
    transport_failed = expected_error.startswith("fixture fetch failed")
    assert outcome["result"]["stage"] == ("exception" if transport_failed else "session")
    assert outcome["result"]["error"] == expected_error
    expected_calls = [{"url": "/api/auth/session", "method": "GET"}]
    if transport_failed:
        expected_calls.append({"url": "/backend-api/accounts/mfa/enroll", "method": "POST"})
    assert outcome["calls"] == expected_calls

    from core import db
    writes = []
    monkeypatch.setattr(db, "save_security_checkpoint", lambda *args, **kwargs: writes.append((args, kwargs)) or {})
    execute = Mock(return_value=outcome["result"])
    driver = SimpleNamespace(current_url="https://chatgpt.com/")
    if runtime == "selenium":
        driver.execute_async_script = execute
    else:
        driver.evaluate = execute
        driver.locator = Mock()
    context = SimpleNamespace(_twofa_refreshed_access_token="")
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export._setup_totp_with_driver(driver, "user@example.test", session_context=context)
    assert execute.call_count == 1
    if transport_failed:
        assert exc_info.value.stage == "totp_enroll" and exc_info.value.http_status is None
        assert context._twofa_refreshed_access_token == "matched-session-token"
        assert context._twofa_session_expires == "2026-09-12T00:00:00Z"
        assert writes == [(("user@example.test",), {"access_token": "matched-session-token"})]
        assert "matched-session-token" not in str(exc_info.value)
    else:
        assert exc_info.value.stage == "totp_session"
        assert context._twofa_refreshed_access_token == ""
        assert writes == []


@pytest.mark.parametrize("failure, expected_code", [
    ({"status": 200, "stage": "session", "error": "missing_access_token", "body": {}}, "totp_session_refresh_failed"),
    ({"status": 503, "stage": "session", "body": {}}, "totp_session_refresh_failed"),
    ({"status": 200, "stage": "session", "error": "session_account_mismatch", "body": {}}, "totp_session_account_mismatch"),
    ({"status": 503, "stage": "request", "email": "other@example.test", "accessToken": "other-token", "body": {}}, "totp_session_account_mismatch"),
])
def test_browser_mfa_session_errors_stop_before_retry(browser_mfa_replay, failure, expected_code):
    replay = browser_mfa_replay
    replay.responses = [failure]
    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export._setup_totp_with_driver(replay.driver, "user@example.test")
    assert exc_info.value.code == expected_code
    assert len(replay.calls) == 1 and replay.sleeps == []


def test_twofa_rejects_authenticated_account_mismatch_before_writes(monkeypatch) -> None:
    monkeypatch.setattr(
        account_export,
        "_trigger_reauth",
        lambda *args, **kwargs: pytest.fail("账号绑定失败后不得开始 MFA 重认证"),
    )

    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export.setup_2fa_result(
            object(),
            "target@example.com",
            existing_password="Stable-pass-1!",
            authenticated_email="other@example.com",
        )

    assert exc_info.value.stage == "totp_session"
    assert exc_info.value.code == "totp_session_account_mismatch"


def test_twofa_authenticated_account_match_is_case_insensitive(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: set())
    monkeypatch.setattr(account_export, "_snapshot_otp_message_ids", lambda *args, **kwargs: set())
    monkeypatch.setattr(
        account_export,
        "_trigger_reauth",
        lambda *args: calls.append("reauth") or "https://auth.openai.com/authorize/x",
    )
    monkeypatch.setattr(
        account_export,
        "_follow_reauth",
        lambda *args: (_ for _ in ()).throw(
            account_export.TwoFASetupError(
                "totp_reauth",
                "totp_reauth_navigation_failed",
                "stop after identity assertion",
                http_status=500,
            )
        ),
    )

    with pytest.raises(account_export.TwoFASetupError):
        account_export.setup_2fa_result(
            object(),
            "Target@Example.com",
            existing_password="Stable-pass-1!",
            authenticated_email="target@example.COM",
        )

    assert calls == ["reauth"]


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
    assert result.security_ok is False  # 本用例未提供密码，激活成功也仍缺安全链的密码部分


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
    monkeypatch.setattr(account_export, "_snapshot_otp_message_ids", lambda *args, **kwargs: {"mail-old"})
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
    assert captured["exclude_message_ids"] == {"mail-old"}
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


def test_reauth_csrf_error_preserves_http_status() -> None:
    class Response:
        status_code = 403

        @staticmethod
        def raise_for_status():
            raise RuntimeError("managed challenge")

    class Session:
        @staticmethod
        def get_nextauth_headers(**_kwargs):
            return {}

        @staticmethod
        def get(*_args, **_kwargs):
            return Response()

    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export._trigger_reauth(Session(), "user@example.com")

    assert exc_info.value.code == "totp_reauth_request_failed"
    assert exc_info.value.http_status == 403


def test_browser_reauth_fallback_reuses_current_driver_and_syncs_cookies(monkeypatch) -> None:
    navigated = []
    synced = []

    class Driver:
        current_url = "https://chatgpt.com/"

        def get(self, url):
            navigated.append(url)
            self.current_url = "https://auth.openai.com/email-verification"

        def execute_script(self, *_args):
            return "Check your email for a verification code"

        def find_elements(self, *_args):
            return []

    monkeypatch.setattr(
        account_export,
        "import_browser_cookies",
        lambda session, driver, require_auth=False: synced.append(require_auth) or 3,
    )

    session = SimpleNamespace(blocked_until=9999999999.0, blocked_reason="Cloudflare challenge")
    result = account_export._follow_reauth_with_driver(
        session,
        Driver(),
        "https://auth.openai.com/authorize/x",
        password="Stable-pass-1!",
        timeout_seconds=15,
    )

    assert result == ("https://auth.openai.com/email-verification", False)
    assert navigated == ["https://auth.openai.com/authorize/x"]
    assert synced == [True]
    assert session.blocked_until == 0.0
    assert session.blocked_reason == ""


def test_setup_2fa_uses_browser_fallback_only_for_protocol_403(monkeypatch) -> None:
    from config import email as email_cfg
    from config import twofa
    import core.email_provider

    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
    monkeypatch.setattr(email_cfg, "USE_EMAIL_SERVICE", True)
    monkeypatch.setattr(account_export, "human_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: set())
    monkeypatch.setattr(account_export, "_trigger_reauth", lambda *args: "https://auth.openai.com/authorize/x")
    monkeypatch.setattr(
        account_export,
        "_follow_reauth",
        lambda *args: (_ for _ in ()).throw(
            account_export.TwoFASetupError(
                "totp_reauth",
                "totp_reauth_navigation_failed",
                "managed challenge",
                http_status=403,
            )
        ),
    )
    fallback = []
    monkeypatch.setattr(
        account_export,
        "_follow_reauth_with_driver",
        lambda session, driver, url, password, **kwargs: fallback.append((driver, password))
        or ("https://auth.openai.com/email-verification", False),
    )
    monkeypatch.setattr(core.email_provider, "wait_for_otp", lambda *args, **kwargs: "123456")
    monkeypatch.setattr(account_export, "_validate_reauth_otp", lambda *args: "https://auth.openai.com/continue/x")
    monkeypatch.setattr(account_export, "_exchange_new_token", lambda *args: "fresh-token")
    monkeypatch.setattr(account_export, "_enroll_totp", lambda *args: ("JBSWY3DPEHPK3PXP", "sid"))
    monkeypatch.setattr(account_export, "_wait_for_totp_window", lambda: None)
    monkeypatch.setattr(account_export, "_activate_totp", lambda *args: True)
    monkeypatch.setattr(account_export, "_validate_2fa_token", lambda *args: 200)

    driver = object()
    result = account_export.setup_2fa_result(
        SimpleNamespace(),
        "user@example.com",
        driver=driver,
        existing_password="Stable-pass-1!",
    )

    assert result.security_ok is True
    assert fallback == [(driver, "Stable-pass-1!")]


def test_setup_2fa_prefers_live_browser_mfa_and_skips_csrf_reauth(monkeypatch) -> None:
    """已登录浏览器直接 enroll/activate，不再触发会被 CF 拦截的 CSRF 请求。"""
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
    monkeypatch.setattr(account_export, "_wait_for_totp_window", lambda: None)
    monkeypatch.setattr(
        account_export,
        "_trigger_reauth",
        lambda *args: pytest.fail("实时浏览器 MFA 分支不得请求 /api/auth/csrf"),
    )
    validation_tokens = []
    monkeypatch.setattr(
        account_export,
        "_validate_2fa_token",
        lambda _session, token: validation_tokens.append(token) or 200,
    )
    calls = []

    class Driver:
        def execute_async_script(self, script, path, payload, access_token, expected_email):
            calls.append((script, path, payload, access_token))
            if path.endswith("/enroll"):
                return {
                    "status": 200,
                    "json": True,
                    "email": "user@example.com",
                    "expires": "2026-08-24T00:00:00.000Z",
                    "accessToken": "browser-access-token",
                    "body": {"secret": "JBSWY3DPEHPK3PXP", "session_id": "enrollment-session"},
                }
            return {
                "status": 200,
                "json": True,
                "accessToken": access_token,
                "body": {"success": True},
            }

    session = SimpleNamespace()
    result = account_export.setup_2fa_result(
        session,
        "user@example.com",
        driver=Driver(),
        existing_password="Stable-pass-1!",
        authenticated_email="USER@example.com",
        access_token="session-access-token",
    )

    assert result.secret == "JBSWY3DPEHPK3PXP"
    assert result.access_token == "browser-access-token"
    assert result.expires == "2026-08-24T00:00:00.000Z"
    assert result.validation_status == 200
    assert validation_tokens == ["browser-access-token"]
    assert [call[1] for call in calls] == [
        "/backend-api/accounts/mfa/enroll",
        "/backend-api/accounts/mfa/user/activate_enrollment",
    ]
    assert calls[0][3] == "session-access-token"
    assert calls[1][3] == "browser-access-token"
    assert calls[1][2]["factor_type"] == "totp"
    assert calls[1][2]["session_id"] == "enrollment-session"
    assert len(str(calls[1][2]["code"])) == 6
    assert "fetch('/api/auth/session'" in calls[0][0]


def test_setup_2fa_refreshes_token_after_browser_password_reauth(monkeypatch) -> None:
    """补设密码可能吊销注册 Token，MFA enroll 必须读取同窗新会话。"""
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
    monkeypatch.setattr(account_export, "_wait_for_totp_window", lambda *args, **kwargs: None)
    monkeypatch.setattr(account_export, "_setup_password_with_driver", lambda **kwargs: {
        "ok": True,
        "status": "success",
        "stage": "password_done",
        "code": "password_setup_success",
        "message": "密码已补设",
    })
    monkeypatch.setattr(account_export, "import_browser_cookies", lambda *args, **kwargs: 2)
    monkeypatch.setattr(account_export, "_validate_2fa_token", lambda *args: 200)
    calls = []

    class Driver:
        def execute_async_script(self, script, path, payload, access_token, expected_email):
            calls.append((path, access_token))
            if path.endswith("/enroll"):
                return {
                    "status": 200,
                    "json": True,
                    "email": "user@example.com",
                    "accessToken": "fresh-after-password",
                    "body": {"secret": "JBSWY3DPEHPK3PXP", "session_id": "sid"},
                }
            return {
                "status": 200,
                "json": True,
                "accessToken": "fresh-after-password",
                "body": {"success": True},
            }

    result = account_export.setup_2fa_result(
        SimpleNamespace(),
        "user@example.com",
        driver=Driver(),
        desired_password="Strong-pass-1!",
        authenticated_email="user@example.com",
        access_token="stale-registration-token",
    )

    assert result.access_token == "fresh-after-password"
    assert calls == [
        ("/backend-api/accounts/mfa/enroll", ""),
        ("/backend-api/accounts/mfa/user/activate_enrollment", "fresh-after-password"),
    ]


def test_browser_mfa_http_failure_keeps_stage_and_status(monkeypatch) -> None:
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
    monkeypatch.setattr(
        account_export,
        "_trigger_reauth",
        lambda *args: pytest.fail("浏览器 MFA 失败不得悄悄退回旧 CSRF 协议"),
    )

    class Driver:
        @staticmethod
        def execute_async_script(_script, _path, _payload, _access_token, _expected_email):
            return {"status": 403, "json": False, "body": {}}

    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export.setup_2fa_result(
            SimpleNamespace(),
            "user@example.com",
            driver=Driver(),
            existing_password="Stable-pass-1!",
            authenticated_email="user@example.com",
        )

    assert exc_info.value.stage == "totp_enroll"
    assert exc_info.value.code == "totp_browser_enroll_failed"
    assert exc_info.value.http_status == 403


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
    """ENABLE_2FA=True + driver 时，必须先确认密码再开始 MFA。"""
    from config import email as email_cfg
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
    monkeypatch.setattr(email_cfg, "USE_EMAIL_SERVICE", True)
    monkeypatch.setattr(account_export, "human_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: set())
    flow_order = []
    monkeypatch.setattr(account_export, "_trigger_reauth", lambda *args: flow_order.append("totp_reauth") or "https://auth.openai.com/authorize/x")
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
        flow_order.append("password")
        password_setup_calls.append({"driver": driver, "password": password, "totp_secret": totp_secret})
        return {"ok": True, "status": "success", "stage": "password_done", "code": "password_setup_success", "message": "密码已补设", "password": password}

    monkeypatch.setattr(account_export, "_setup_password_with_driver", fake_setup_password)
    monkeypatch.setattr(account_export, "import_browser_cookies", lambda *args, **kwargs: flow_order.append("cookie_sync") or 2)

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
    assert password_setup_calls[0]["totp_secret"] is None
    assert password_checkpoints == [("user@example.com", "Ab3!cdefgh123")]
    assert flow_order[:3] == ["password", "cookie_sync", "totp_reauth"]


def test_setup_2fa_aborts_before_mfa_when_password_setup_fails(monkeypatch) -> None:
    """密码未确认时不能继续 enroll/activate MFA。"""
    from config import email as email_cfg
    from config import twofa

    monkeypatch.setattr(twofa, "ENABLE_2FA", True)
    monkeypatch.setattr(email_cfg, "USE_EMAIL_SERVICE", True)
    monkeypatch.setattr(account_export, "human_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: set())
    monkeypatch.setattr(account_export, "_trigger_reauth", lambda *args: pytest.fail("password failure must stop before MFA reauth"))
    monkeypatch.setattr(account_export, "_follow_reauth", lambda *args: "https://auth.openai.com/email-verification")
    monkeypatch.setattr(account_export, "_validate_reauth_otp", lambda *args: "https://auth.openai.com/continue/x")
    monkeypatch.setattr(account_export, "_exchange_new_token", lambda *args: "fresh-token")
    monkeypatch.setattr(account_export, "_enroll_totp", lambda *args: pytest.fail("password failure must stop before TOTP enroll"))
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

    with pytest.raises(account_export.TwoFASetupError) as exc_info:
        account_export.setup_2fa_result(object(), "user@example.com", driver=object())

    assert exc_info.value.stage == "password_setup"
    assert exc_info.value.code == "password_setup_failed"
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
    assert "post_login_password_reset" not in driver.async_scripts[0]
    assert "query.set('connection','password')" not in driver.async_scripts[0]

    # 账号页扩展显式选择 reset + console_compat；默认注册分支仍保持上面的 add 脚本。
    reset_driver = Driver()
    reset_result = account_export._setup_password_with_driver(
        driver=reset_driver,
        session=object(),
        email="user@example.com",
        password="Ab3!cdefgh123",
        totp_secret="JBSWY3DPEHPK3PXP",
        timeout_seconds=30,
        password_mode="reset",
        console_compat=True,
    )

    assert reset_result["ok"] is True, reset_result
    assert "post_login_password_reset" in reset_driver.async_scripts[0]
    assert "post_login_add_password" not in reset_driver.async_scripts[0]
    assert "query.set('connection','password')" in reset_driver.async_scripts[0]
    assert "query.set('reauth','password')" in reset_driver.async_scripts[0]


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
    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: {"654321"})
    monkeypatch.setattr(account_export, "_snapshot_otp_message_ids", lambda *args, **kwargs: {"mail-old"})
    import core.email_provider
    mail_kwargs = {}
    monkeypatch.setattr(
        core.email_provider,
        "wait_for_otp",
        lambda *args, **kwargs: (mail_kwargs.update(kwargs), "654321")[-1],
    )

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

        def input_value(self, timeout=0):
            return self.page.last_fill

        def press(self, value, timeout=0):
            self.page.last_fill += str(value)

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
    assert mail_kwargs["exclude_codes"] == {"654321"}
    assert mail_kwargs["exclude_message_ids"] == {"mail-old"}


@pytest.fixture
def password_reauth_driver(monkeypatch):
    """Exercise real DOM helpers with a deterministic Selenium/time fixture."""
    import core.email_provider
    from selenium.common.exceptions import StaleElementReferenceException

    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(account_export, "time", SimpleNamespace(
        monotonic=lambda: clock.now,
        time=lambda: 1_700_000_000 + clock.now,
        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
    ))
    monkeypatch.setattr(account_export, "_snapshot_otp_history", lambda *args, **kwargs: set())
    monkeypatch.setattr(account_export, "_snapshot_otp_message_ids", lambda *args, **kwargs: set())
    monkeypatch.setattr(account_export, "_wait_for_totp_window", lambda **kwargs: None)
    monkeypatch.setattr(account_export.pyotp, "TOTP", lambda secret: SimpleNamespace(now=lambda: "654321"))

    class Field:
        def __init__(self, owner, kind, index=0):
            self.owner, self.kind, self.index = owner, kind, index
            self.generation = owner.generation

        def check_live(self):
            if self.kind == "code" and self.generation != self.owner.generation:
                raise StaleElementReferenceException("fixture rerender")

        def is_displayed(self):
            self.check_live()
            return True

        def is_enabled(self):
            self.check_live()
            return self.kind == "password" or (
                clock.now >= self.owner.disabled_until
                and not (self.owner.submitted_at is not None and self.owner.disable_after_submit)
            )

        def get_attribute(self, name):
            self.check_live()
            assert name == "value"
            return self.owner.values[self.index]

        def clear(self):
            self.check_live()
            if self.kind == "code":
                self.owner.values[self.index] = ""
                self.owner.generation += 1

        def send_keys(self, value):
            self.check_live()
            if self.kind == "password":
                self.owner.password_values.append(value)
                return
            assert self.is_enabled()
            assert len(value) == 1, "OTP must use individual real keystrokes"
            if self.owner.stale_before_final and len(self.owner.keys) == 5:
                self.owner.stale_before_final = False
                self.owner.generation += 1
                raise StaleElementReferenceException("fixture last key not accepted")
            self.owner.keys.append(value)
            self.owner.values[self.index] += value
            self.owner.generation += 1
            if self.owner.stale_midway and len(self.owner.keys) == 3:
                raise StaleElementReferenceException("fixture middle key rerender")
            if len("".join(self.owner.values)) == 6 and self.owner.auto_submit:
                self.owner.submit_code()
                raise StaleElementReferenceException("fixture last key auto-submit")

        def find_element(self, *_args):
            self.check_live()
            return SimpleNamespace(find_element=lambda *_: Submit(self.owner, self.kind))

    class Submit:
        def __init__(self, owner, kind):
            self.owner, self.kind = owner, kind

        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

        def click(self):
            if self.kind == "code":
                self.owner.explicit_code_clicks += 1
                self.owner.submit_code()
            else:
                self.owner.password_submitted = True

    class Driver:
        def __init__(self, *, disabled_after_fetch=0.0, advance_after=0.0, auto_submit=False,
                     stale_midway=False, reject=False, reject_on_fetch=False,
                     disable_after_submit=False, split_fields=False, hide_after_fetch=False,
                     confirm_password=True, challenge="email", second_challenge=None, stale_before_final=False):
            self.second_challenge, self.stale_before_final = second_challenge, stale_before_final
            self.disabled_after_fetch, self.advance_after = disabled_after_fetch, advance_after
            self.auto_submit, self.stale_midway, self.reject = auto_submit, stale_midway, reject
            self.reject_on_fetch, self.disable_after_submit = reject_on_fetch, disable_after_submit or auto_submit
            self.hide_after_fetch, self.confirm_password, self.challenge = hide_after_fetch, confirm_password, challenge
            self.values = [""] * (6 if split_fields else 1)
            self.keys, self.password_values, self.screenshots = [], [], []
            self.submitted_codes, self.explicit_code_clicks = [], 0
            self.generation, self.mail_calls, self.submit_calls = 0, 0, 0
            self.disabled_until, self.submitted_at, self.password_submitted = 0.0, None, False
            self.clock = clock

        @property
        def stage(self):
            if self.password_submitted:
                return "done"
            if self.submitted_at is not None and not self.reject and self.advance_after is not None:
                if clock.now - self.submitted_at >= self.advance_after:
                    if self.second_challenge and self.submit_calls == 1:
                        self.challenge, self.second_challenge = self.second_challenge, None
                        self.submitted_at = None
                        self.values = [""] * len(self.values)
                        self.generation += 1
                        return self.challenge
                    return "password"
            return self.challenge

        @property
        def current_url(self):
            path = "email-verification" if self.stage == "email" else self.stage
            return f"https://auth.openai.com/{path}?token=secret-query&email=user@example.test"

        def execute_async_script(self, _script):
            return {"ok": True, "status": 200, "url": self.current_url}

        def execute_script(self, script, *_args):
            if "document.body" in script:
                if self.reject_on_fetch and self.mail_calls or self.reject and self.submitted_at is not None:
                    return "Invalid verification code"
                return {
                    "email": "Enter the verification code sent to your email",
                    "totp": "Enter your authenticator code",
                    "password": "Set a password",
                    "done": "Password updated" if self.confirm_password else "Working",
                }[self.stage]
            return False

        def find_elements(self, _by, selector):
            if "password" in selector:
                return [Field(self, "password")] if self.stage == "password" else []
            if self.stage not in {"email", "totp"} or self.hide_after_fetch and self.mail_calls:
                return []
            return [Field(self, "code", index) for index in range(len(self.values))]

        def get(self, _url):
            pass

        def save_screenshot(self, path):
            self.screenshots.append(path)

        def submit_code(self):
            challenge = self.stage
            value = "".join(self.values)
            assert value == {"email": "012345", "totp": "654321"}[challenge]
            self.submitted_codes.append((challenge, value))
            self.submit_calls += 1
            self.submitted_at = clock.now

        def fetch_code(self, *_args, **_kwargs):
            self.mail_calls += 1
            self.disabled_until = clock.now + self.disabled_after_fetch
            return "012345"

    def make_driver(**kwargs):
        driver = Driver(**kwargs)
        monkeypatch.setattr(core.email_provider, "wait_for_otp", driver.fetch_code)
        return driver

    return make_driver


@pytest.mark.parametrize("options", [
    {"disabled_after_fetch": 3.0},
    {"auto_submit": True, "advance_after": 3.0},
    {"advance_after": 10.0},
    {"stale_midway": True},
    {"split_fields": True},
    {"stale_before_final": True},
])
def test_password_setup_retries_transient_email_code_submit(password_reauth_driver, options) -> None:
    driver = password_reauth_driver(**options)
    result = account_export._setup_password_with_driver(
        driver=driver, session=object(), email="user@example.test",
        password="Ab3!cdefgh123", timeout_seconds=60,
    )
    assert result["ok"] is True, result
    assert result["email_reauth_used"] is True
    assert driver.mail_calls == driver.submit_calls == 1
    assert "".join(driver.values) == "012345"
    assert driver.password_values == ["Ab3!cdefgh123"]
    if not options.get("stale_midway") and not options.get("stale_before_final"):
        assert driver.keys == list("012345")
    assert driver.clock.now >= max(options.get("advance_after", 0), options.get("disabled_after_fetch", 0))


@pytest.mark.parametrize("challenge", ["email", "totp"])
def test_password_reauth_rejection_is_detected_with_disabled_input(password_reauth_driver, challenge) -> None:
    driver = password_reauth_driver(challenge=challenge, reject=True, disable_after_submit=True)
    result = account_export._setup_password_with_driver(
        driver=driver, session=object(), email="user@example.test",
        password="Ab3!cdefgh123", totp_secret="JBSWY3DPEHPK3PXP", timeout_seconds=60,
    )
    assert result["ok"] is False
    assert result["code"] == f"password_{challenge}_reauth_rejected"
    assert driver.clock.now < 5
    assert driver.password_values == []
    assert "password" not in result


@pytest.mark.parametrize("disabled", [False, True])
def test_password_reauth_waits_boundedly_without_false_success(password_reauth_driver, disabled) -> None:
    driver = password_reauth_driver(advance_after=None, disable_after_submit=disabled)
    result = account_export._setup_password_with_driver(
        driver=driver, session=object(), email="user@example.test",
        password="Ab3!cdefgh123", timeout_seconds=120,
    )
    assert result["ok"] is False
    assert result["code"] == "password_email_reauth_not_advanced"
    assert 45 <= driver.clock.now < 47
    assert driver.mail_calls == driver.submit_calls == 1
    assert driver.password_values == []
    assert "password" not in result


@pytest.mark.parametrize("options, expected", [
    ({"disabled_after_fetch": 100.0}, "password_email_reauth_submit_failed"),
    ({"hide_after_fetch": True}, "password_email_reauth_submit_failed"),
    ({"disabled_after_fetch": 3.0, "reject_on_fetch": True}, "password_email_reauth_rejected"),
])
def test_password_reauth_missing_inputs_never_confirm_advancement(password_reauth_driver, options, expected, caplog) -> None:
    driver = password_reauth_driver(**options)
    result = account_export._setup_password_with_driver(
        driver=driver, session=object(), email="user@example.test",
        password="Ab3!cdefgh123", timeout_seconds=60,
    )
    assert result["ok"] is False
    assert result["code"] == expected
    assert driver.submit_calls == 0
    assert driver.password_values == []
    assert "password" not in result
    assert driver.clock.now <= 45
    for sensitive in ("012345", "secret-query", "user@example.test", "Ab3!cdefgh123"):
        assert sensitive not in caplog.text
        assert sensitive not in result["message"]


def test_password_reauth_total_deadline_and_success_confirmation(password_reauth_driver) -> None:
    driver = password_reauth_driver(disabled_after_fetch=20.0, advance_after=20.0)
    result = account_export._setup_password_with_driver(
        driver=driver, session=object(), email="user@example.test",
        password="Ab3!cdefgh123", timeout_seconds=30,
    )
    assert result["ok"] is False
    assert driver.clock.now < 31
    assert driver.password_values == []
    assert "password" not in result
    assert "secret-query" not in result["message"]


def test_password_success_requires_confirmed_final_page(password_reauth_driver) -> None:
    driver = password_reauth_driver(confirm_password=False)
    result = account_export._setup_password_with_driver(
        driver=driver, session=object(), email="user@example.test",
        password="Ab3!cdefgh123", timeout_seconds=30,
    )
    assert result["ok"] is False
    assert result["code"] == "password_settings_timeout"
    assert driver.password_values == ["Ab3!cdefgh123"]
    assert "password" not in result
    assert "secret-query" not in result["message"]


@pytest.mark.parametrize("first, second", [("email", "totp"), ("totp", "email")])
@pytest.mark.parametrize("auto_submit, advance_after", [(False, 0), (True, 3), (False, 30), (True, 30)])
def test_password_reauth_preserves_challenge_switch(password_reauth_driver, first, second, auto_submit, advance_after) -> None:
    driver = password_reauth_driver(
        challenge=first, second_challenge=second, auto_submit=auto_submit, advance_after=advance_after,
    )
    result = account_export._setup_password_with_driver(
        driver=driver, session=object(), email="user@example.test",
        password="Ab3!cdefgh123", totp_secret="JBSWY3DPEHPK3PXP", timeout_seconds=120,
    )
    assert result["ok"] is True, result
    assert result["email_reauth_used"] is result["totp_reauth_used"] is True
    assert driver.mail_calls == 1
    assert driver.submit_calls == 2
    assert driver.password_values == ["Ab3!cdefgh123"]
    codes = {"email": "012345", "totp": "654321"}
    assert driver.submitted_codes == [(first, codes[first]), (second, codes[second])]
    assert driver.explicit_code_clicks == (0 if auto_submit else 2)
    assert driver.clock.now >= advance_after * 2


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
