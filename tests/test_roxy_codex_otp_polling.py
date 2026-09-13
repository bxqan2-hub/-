# -*- coding: utf-8 -*-
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock, patch

from core import roxy_codex_oauth


class RoxyCodexOtpPollingTests(unittest.TestCase):
    def test_invalid_otp_resends_in_place_without_reopening_authorize_url(self):
        driver = MagicMock()
        provider_calls = []

        def provider(email, **kwargs):
            provider_calls.append(kwargs)
            return "111111" if len(provider_calls) == 1 else "222222"

        patches = (
            patch.object(roxy_codex_oauth, "_maybe_accept"),
            patch.object(roxy_codex_oauth, "_type_email_address"),
            patch.object(roxy_codex_oauth, "_submit_email_step"),
            patch.object(roxy_codex_oauth, "_complete_login_after_email", side_effect=["email_otp", "advanced"]),
            patch.object(roxy_codex_oauth, "_wait_for_otp_input"),
            patch.object(roxy_codex_oauth, "_clear_otp_inputs"),
            patch.object(roxy_codex_oauth, "_type_otp"),
            patch.object(roxy_codex_oauth, "_install_email_otp_validate_hook"),
            patch.object(roxy_codex_oauth, "_click_if_present", return_value=True),
            patch.object(roxy_codex_oauth, "_wait_after_email_otp_submit", side_effect=["invalid", "accepted"]),
            patch.object(roxy_codex_oauth, "_click_resend_email_otp", return_value={"ok": True, "text": "Resend"}),
            patch.object(roxy_codex_oauth, "human_delay"),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10] as resend, patches[11]:
            roxy_codex_oauth._fill_email_and_otp(
                driver,
                "user@example.com",
                provider,
                "https://auth.openai.com/oauth/authorize?test=1",
            )

        self.assertEqual(driver.get.call_count, 1)
        resend.assert_called_once_with(driver, timeout=25)
        self.assertEqual(provider_calls[0]["max_wait"], 120)
        self.assertEqual(provider_calls[1]["max_wait"], 120)
        self.assertEqual(provider_calls[1]["exclude_codes"], {"111111"})

    def test_fresh_otp_passes_timeout_and_exclusions_to_provider(self):
        calls = []

        def provider(email, **kwargs):
            calls.append((email, kwargs))
            return "222222"

        code = roxy_codex_oauth._wait_for_fresh_email_otp(
            provider,
            "user@example.com",
            after_ts=123.0,
            used_codes={"111111"},
            timeout=45,
        )

        self.assertEqual(code, "222222")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["max_wait"], 45)
        self.assertEqual(calls[0][1]["exclude_codes"], {"111111"})

    def test_legacy_provider_still_rejects_old_code(self):
        def provider(email, after_ts):
            return "111111"

        with self.assertRaisesRegex(RuntimeError, "仍返回已失败验证码"):
            roxy_codex_oauth._wait_for_fresh_email_otp(
                provider,
                "user@example.com",
                after_ts=123.0,
                used_codes={"111111"},
                timeout=45,
            )


if __name__ == "__main__":
    unittest.main()


@pytest.fixture
def password_login_fixture(monkeypatch):
    from core import account_export, account_security_service, db
    account = {"email": "login@example.test", "extra_json": json.dumps({"registration_password": "fixture-password"}), "totp_secret": "JBSWY3DPEHPK3PXP"}
    driver = SimpleNamespace(current_url="https://auth.openai.com/log-in/password")
    field = MagicMock()
    monkeypatch.setattr(db, "get_account_by_email", lambda email: account)
    monkeypatch.setattr(roxy_codex_oauth, "_has_strict_add_phone_form", lambda d: d.current_url.endswith("/add-phone"))
    monkeypatch.setattr(roxy_codex_oauth, "_phone_page_state", lambda d: {"inputs": [], "bodyText": "Authenticator code" if "mfa" in d.current_url else "Password"})
    monkeypatch.setattr(roxy_codex_oauth, "_is_email_verification_page", lambda d: d.current_url.endswith("/email-verification"))
    monkeypatch.setattr(account_export, "_password_visible_inputs", lambda d, selector: [field] if (('type="password"' in selector) == d.current_url.endswith("/password")) else [])
    monkeypatch.setattr(account_export, "_wait_for_totp_window", lambda: None)
    monkeypatch.setattr(roxy_codex_oauth, "_clear_otp_inputs", lambda d: None)
    type_otp = MagicMock()
    monkeypatch.setattr(roxy_codex_oauth, "_type_otp", type_otp)
    monkeypatch.setattr(roxy_codex_oauth.time, "sleep", lambda seconds: None)
    return driver, account, field, type_otp, account_export


def test_saved_password_and_totp_reach_phone_once_without_credential_logs(monkeypatch, password_login_fixture, caplog):
    driver, account, field, type_otp, account_export = password_login_fixture
    submits = []
    def click(d, f):
        submits.append(d.current_url)
        d.current_url = "https://auth.openai.com/mfa-challenge" if len(submits) == 1 else "https://auth.openai.com/add-phone"
        return True
    monkeypatch.setattr(account_export, "_password_click_selenium", click)
    with caplog.at_level("INFO"):
        assert roxy_codex_oauth._complete_login_after_email(driver, account["email"]) == "advanced"
    field.send_keys.assert_called_once_with("fixture-password")
    assert len(submits) == 2
    type_otp.assert_called_once()
    assert "stage=login_password" in caplog.text and "stage=login_totp" in caplog.text
    for value in ("fixture-password", account["totp_secret"], type_otp.call_args.args[1]):
        assert value not in caplog.text


def test_password_submission_can_require_email_otp(monkeypatch, password_login_fixture):
    driver, account, field, type_otp, account_export = password_login_fixture
    def click(d, f):
        d.current_url = "https://auth.openai.com/email-verification"
        return True
    monkeypatch.setattr(account_export, "_password_click_selenium", click)
    assert roxy_codex_oauth._complete_login_after_email(driver, account["email"]) == "email_otp"
    type_otp.assert_not_called()


@pytest.mark.parametrize("next_state", ["advanced", RuntimeError("stage=login_password; fixture timeout")])
def test_email_restart_preserves_login_transition_or_failure(next_state):
    driver, provider = MagicMock(), MagicMock(side_effect=RuntimeError("fixture email timeout"))
    with ExitStack() as stack:
        for name in ("_maybe_accept", "_type_email_address", "_submit_email_step", "human_delay"):
            stack.enter_context(patch.object(roxy_codex_oauth, name))
        stack.enter_context(patch.object(roxy_codex_oauth, "_complete_login_after_email", side_effect=["email_otp", next_state]))
        stack.enter_context(patch.object(roxy_codex_oauth, "_click_resend_email_otp", side_effect=RuntimeError("fixture missing resend")))
        if isinstance(next_state, Exception):
            with pytest.raises(RuntimeError, match="stage=login_password"):
                roxy_codex_oauth._fill_email_and_otp(driver, "fixture@example.test", provider, "https://auth.openai.com/oauth/authorize")
        else:
            roxy_codex_oauth._fill_email_and_otp(driver, "fixture@example.test", provider, "https://auth.openai.com/oauth/authorize")
    assert provider.call_count == 1
    assert driver.get.call_count == 2


def test_login_rejects_account_mismatch_before_typing(password_login_fixture):
    driver, account, field, type_otp, _ = password_login_fixture
    with pytest.raises(RuntimeError, match="stage=login_identity"):
        roxy_codex_oauth._complete_login_after_email(driver, "other@example.test")
    field.send_keys.assert_not_called()
    type_otp.assert_not_called()


def test_stalled_password_is_not_resubmitted_or_treated_as_email_sent(monkeypatch, password_login_fixture):
    driver, account, field, type_otp, account_export = password_login_fixture
    click = MagicMock(return_value=True)
    monkeypatch.setattr(account_export, "_password_click_selenium", click)
    ticks = iter(range(20))
    monkeypatch.setattr(roxy_codex_oauth.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="stage=login_password"):
        roxy_codex_oauth._complete_login_after_email(driver, account["email"], timeout=3)
    click.assert_called_once()
    field.send_keys.assert_called_once()
    type_otp.assert_not_called()


@pytest.mark.parametrize("origin", ["https://auth.openai.com.example.test", "http://auth.openai.com", "https://auth.openai.com:8443"])
def test_login_never_types_credentials_on_an_unexpected_origin(monkeypatch, password_login_fixture, origin):
    driver, account, field, type_otp, _ = password_login_fixture
    driver.current_url = origin + "/log-in/password"
    ticks = iter(range(20))
    monkeypatch.setattr(roxy_codex_oauth.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="stage=email_transition"):
        roxy_codex_oauth._complete_login_after_email(driver, account["email"], timeout=3)
    field.send_keys.assert_not_called()
    type_otp.assert_not_called()
