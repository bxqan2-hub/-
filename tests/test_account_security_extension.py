# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import account_security_service, db
from webui.app import _compact_account_for_list, create_app


def _isolate_storage(monkeypatch, root: Path) -> None:
    paths = {
        "_DATA_DIR": root,
        "_LOG_DIR": root / "logs",
        "_ACCOUNTS_JSON": root / "accounts.json",
        "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
        "_OUTLOOK_JSON": root / "outlook.json",
        "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
        "_GENERIC_API_EMAIL_JSON": root / "generic.json",
        "_GENERIC_API_EMAIL_TXT": root / "generic.txt",
        "_DOMAIN_EMAIL_JSON": root / "domain.json",
        "_SECURITY_CHECKPOINTS_JSON": root / "security.json",
        "_SECURITY_CHECKPOINTS_LOCK": root / "security.lock",
        "_ACCOUNTS_TXT": root / "accounts.txt",
        "_TOKENS_TXT": root / "tokens.txt",
        "_OUTLOOK_TXT": root / "outlook.txt",
        "_VIEWER_HTML": root / "viewer.html",
    }
    for name, value in paths.items():
        monkeypatch.setattr(db, name, value)
    monkeypatch.setattr(db, "_render_static_viewer", lambda *_args, **_kwargs: root / "viewer.html")


def test_security_setup_state_is_independent_and_credentials_are_not_compacted(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    account_id = db.insert_account(
        email="secure@example.com",
        access_token="old-token",
        plan_type="plus",
        extra={"preserve_me": "yes"},
    )
    rows = json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))
    rows[0]["live_check_status"] = "alive"
    rows[0]["current_plan_type"] = "plus"
    (tmp_path / "accounts.json").write_text(json.dumps(rows), encoding="utf-8")

    assert db.claim_account_security_setup(account_id, trigger="manual") is True
    assert db.claim_account_security_setup(account_id, trigger="manual") is False
    assert db.mark_account_security_setup_running(account_id, profile_id="profile-1") is True
    assert db.update_account_security_setup(
        account_id,
        {
            "ok": False,
            "status": "running",
            "stage": "password_done",
            "message": "密码已补设，正在开启 2FA",
            "password_done": True,
            "totp_done": False,
        },
        registration_password="Unique-pass-1!",
        access_token="fresh-token",
    ) is True
    assert db.update_account_security_setup(
        account_id,
        {
            "ok": True,
            "status": "success",
            "stage": "complete",
            "message": "密码和 2FA 已完成",
            "password_done": True,
            "totp_done": True,
        },
        totp_secret="JBSWY3DPEHPK3PXP",
    ) is True

    stored = db.get_account(account_id)
    extra = json.loads(stored["extra_json"])
    assert stored["security_setup_status"] == "success"
    assert stored["security_setup_password_done"] is True
    assert stored["security_setup_totp_done"] is True
    assert stored["access_token"] == "fresh-token"
    assert stored["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert extra["registration_password"] == "Unique-pass-1!"
    assert extra["preserve_me"] == "yes"
    assert stored["plan_type"] == "plus"
    assert stored["current_plan_type"] == "plus"
    assert stored["live_check_status"] == "alive"

    compact = _compact_account_for_list(stored)
    serialized = json.dumps(compact, ensure_ascii=False)
    assert compact["password_configured"] is True
    assert compact["totp_enabled"] is True
    assert compact["security_setup_status"] == "success"
    for secret in ("Unique-pass-1!", "JBSWY3DPEHPK3PXP", "fresh-token"):
        assert secret not in serialized


def test_security_setup_api_routes_are_independent_and_do_not_return_secrets(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    account_id = db.insert_account(
        email="route@example.com",
        access_token="route-token",
        totp_secret="ROUTE-TOTP",
        extra={"registration_password": "Route-pass-1!"},
    )
    captured = {}

    def fake_enqueue(**kwargs):
        captured.update(kwargs)
        return {
            "accepted": True,
            "busy": False,
            "account_id": kwargs["account_id"],
            "email": "route@example.com",
            "status": "queued",
            "password_mode": kwargs["password_mode"],
        }

    monkeypatch.setattr(account_security_service, "enqueue_account_security_setup", fake_enqueue)
    client = create_app(auth_code="test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    response = client.post(
        f"/api/accounts/{account_id}/security-setup",
        json={"password_mode": "reset"},
    )
    assert response.status_code == 202
    assert captured == {"account_id": account_id, "password_mode": "reset", "trigger": "manual"}

    status = client.get(f"/api/accounts/{account_id}/security-setup")
    assert status.status_code == 200
    payload = status.get_json()
    assert payload["password_configured"] is True
    assert payload["totp_enabled"] is True
    serialized = json.dumps(payload, ensure_ascii=False)
    for secret in ("Route-pass-1!", "ROUTE-TOTP", "route-token"):
        assert secret not in serialized

    rejected = client.post(
        f"/api/accounts/{account_id}/security-setup",
        json={"password_mode": "unexpected"},
    )
    assert rejected.status_code == 400


def test_security_worker_reuses_validated_helpers_without_entering_registration(monkeypatch, tmp_path) -> None:
    from config import roxybrowser as roxy_cfg
    from core import account_export, registration_password, roxy_codex_oauth, roxy_registration, session as session_module

    calls = {"updates": []}
    account = {
        "id": 7,
        "email": "worker@example.com",
        "access_token": "old-token",
        "totp_secret": "",
        "extra_json": "{}",
    }

    class Driver:
        def set_page_load_timeout(self, value):
            calls["page_timeout"] = value

        def set_script_timeout(self, value):
            calls["script_timeout"] = value

        def quit(self):
            calls["driver_quit"] = True

    class Client:
        profile_proxy = ""

        def open_profile(self, **kwargs):
            calls["open_profile"] = kwargs
            return SimpleNamespace(profile_id="profile-7", created_by_run=True)

        def cleanup_profile(self, opened):
            calls["cleanup_profile"] = opened.profile_id

    class BrowserSession:
        def __init__(self, proxy, *, detect_exit_geo):
            calls["session"] = {"proxy": proxy, "detect_exit_geo": detect_exit_geo}

        def close(self):
            calls["session_closed"] = True

    monkeypatch.setattr(account_security_service, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(db, "get_account", lambda account_id: dict(account) if account_id == 7 else None)
    monkeypatch.setattr(db, "mark_account_security_setup_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(db, "save_security_checkpoint", lambda *args, **kwargs: calls.setdefault("checkpoints", []).append((args, kwargs)))
    monkeypatch.setattr(
        db,
        "update_account_security_setup",
        lambda *args, **kwargs: calls["updates"].append((args, kwargs)) or True,
    )
    monkeypatch.setattr("core.roxybrowser_client.RoxyBrowserClient", Client)
    monkeypatch.setattr(roxy_registration, "_build_driver", lambda opened: Driver())
    monkeypatch.setattr(roxy_registration, "_center_browser_window", lambda driver: calls.setdefault("centered", True))
    monkeypatch.setattr(
        roxy_registration,
        "_fetch_chatgpt_session",
        lambda *args, **kwargs: {
            "user": {"email": "worker@example.com"},
            "accessToken": "browser-token",
        },
    )
    monkeypatch.setattr(
        roxy_codex_oauth,
        "_fill_email_and_otp",
        lambda driver, email, provider, url: calls.update({"login_email": email, "login_url": url}),
    )
    monkeypatch.setattr(account_export, "import_browser_cookies", lambda *args, **kwargs: calls.setdefault("cookies", kwargs))

    def fake_password_setup(**kwargs):
        calls["password"] = kwargs
        return {"ok": True, "status": "success"}

    monkeypatch.setattr(account_export, "_setup_password_with_driver", fake_password_setup)
    monkeypatch.setattr(
        account_export,
        "_setup_totp_with_driver",
        lambda *args, **kwargs: ("WORKER-TOTP", "fresh-token", None),
    )
    monkeypatch.setattr(account_export, "_validate_2fa_token", lambda *args, **kwargs: 200)
    monkeypatch.setattr(registration_password, "registration_password", lambda: "Worker-pass-1!")
    monkeypatch.setattr(session_module, "BrowserSession", BrowserSession)
    monkeypatch.setattr(roxy_cfg, "ROXY_SELENIUM_TIMEOUT", 30)
    monkeypatch.setattr(roxy_cfg, "ROXY_SESSION_WAIT_TIMEOUT", 25)
    monkeypatch.setattr(roxy_cfg, "ROXY_SESSION_AUTO_JUMP_WAIT", 8)

    # _run_security_setup 只由已占用队列槽的 worker 调用；测试镜像相同前置条件。
    assert account_security_service._QUEUE_SLOTS.acquire(blocking=False) is True
    result = account_security_service._run_security_setup(
        account_id=7,
        password_mode="reset",
        trigger="manual",
    )

    assert result["ok"] is True
    assert result["password_done"] is True
    assert result["totp_done"] is True
    assert calls["open_profile"] == {"headless": False, "require_proxy_exit_ip": True}
    assert calls["login_email"] == "worker@example.com"
    assert calls["login_url"] == "https://chatgpt.com/auth/login"
    assert calls["password"]["password_mode"] == "reset"
    assert calls["password"]["console_compat"] is True
    assert calls["password"]["password"] == "Worker-pass-1!"
    assert calls["cleanup_profile"] == "profile-7"
    assert calls["driver_quit"] is True
    assert calls["session_closed"] is True
    assert calls["updates"][-1][1]["registration_password"] == "Worker-pass-1!"
    assert calls["updates"][-1][1]["totp_secret"] == "WORKER-TOTP"
    assert calls["updates"][-1][1]["access_token"] == "fresh-token"


def test_accounts_template_contains_security_extension_button_and_polling() -> None:
    source = (Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'data-account-security-setup="${id}"' in source
    assert "补密码+2FA" in source
    assert "password_mode: passwordMode" in source
    assert "/security-setup" in source
    assert "不会进入或改动现有注册任务" in source
