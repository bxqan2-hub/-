# -*- coding: utf-8 -*-
from unittest.mock import Mock, patch
from types import SimpleNamespace

import pytest

from config import codex as codex_config, proxy as proxy_config
from core import sms_provider, codex_oauth

from core import roxy_codex_oauth


def test_codex_proxy_preflight_rejects_dead_route_before_profile_creation():
    with patch.object(codex_config, "resolve_local_proxy", return_value="http://127.0.0.1:7890"), \
         patch.object(roxy_codex_oauth, "probe_proxy_exit_geo", return_value={}), \
         patch.object(roxy_codex_oauth, "RoxyBrowserClient") as client:
        result = roxy_codex_oauth.run_roxy_codex_oauth("dead-proxy@example.test", force=True)

    assert result["status"] == "failed"
    assert result["ok"] is False
    assert "stage=proxy_transport" in result["message"]
    client.assert_not_called()


def test_codex_existing_registration_profile_skips_duplicate_preflight():
    with patch.object(codex_config, "resolve_local_proxy", return_value="http://127.0.0.1:7890"), \
         patch.object(roxy_codex_oauth, "probe_proxy_exit_geo") as probe, \
         patch.object(roxy_codex_oauth, "_run_roxy_codex_oauth_once", return_value={"status": "failed", "ok": False, "message": "fixture"}):
        result = roxy_codex_oauth.run_roxy_codex_oauth(
            "existing@example.test",
            force=True,
            reuse_existing_profile=True,
            existing_driver=object(),
            existing_opened=object(),
        )

    assert result["message"] == "fixture"
    probe.assert_not_called()


@pytest.mark.parametrize("setting", ["system", "", "auto"])
def test_clash_system_resolution_never_consults_registration_pool(monkeypatch, setting):
    monkeypatch.setattr(codex_config, "CODEX_LOCAL_PROXY", setting)
    monkeypatch.setattr(proxy_config, "detect_system_proxy", lambda: "http://127.0.0.1:7897")
    for name in ("pick_proxy", "pick_local_proxy"):
        monkeypatch.setattr(proxy_config, name, Mock(side_effect=AssertionError("registration pool used")))
    assert codex_config.resolve_local_proxy() == "http://127.0.0.1:7897"
    with sms_provider._http() as http:
        assert http.proxies == {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
        assert http.trust_env is False


@pytest.mark.parametrize("detected", ["", "http://remote.example:8080", "http://127.0.0.1:bad"])
def test_missing_or_nonlocal_system_proxy_stops_without_pool_fallback(monkeypatch, detected):
    monkeypatch.setattr(codex_config, "CODEX_LOCAL_PROXY", "system")
    monkeypatch.setattr(proxy_config, "detect_system_proxy", lambda: detected)
    monkeypatch.setattr(proxy_config, "pick_local_proxy", Mock(side_effect=AssertionError("pool used")))
    with pytest.raises(RuntimeError, match="stage=proxy_transport"):
        codex_config.resolve_local_proxy()


def test_explicit_local_override_does_not_use_system_proxy(monkeypatch):
    monkeypatch.setattr(codex_config, "CODEX_LOCAL_PROXY", "socks5h://127.0.0.1:1080")
    monkeypatch.setattr(proxy_config, "detect_system_proxy", Mock(side_effect=AssertionError("system read")))
    assert codex_config.resolve_local_proxy() == "socks5h://127.0.0.1:1080"


def test_standalone_codex_ignores_incoming_registration_proxy(monkeypatch):
    monkeypatch.setattr(codex_config, "resolve_local_proxy", lambda: "http://127.0.0.1:7897")
    probe = Mock(return_value={"ip": "203.0.113.2"})
    run = Mock(return_value={"ok": True})
    monkeypatch.setattr(roxy_codex_oauth, "probe_proxy_exit_geo", probe)
    monkeypatch.setattr(roxy_codex_oauth, "_run_roxy_codex_oauth_once", run)
    assert roxy_codex_oauth.run_roxy_codex_oauth("fixture@example.test", force=True, proxy="http://registration.example:8000")["ok"]
    assert probe.call_args.args[0] == "http://127.0.0.1:7897"
    assert run.call_args.kwargs["proxy"] == "http://127.0.0.1:7897"


def test_protocol_entry_receives_local_route_instead_of_pool(monkeypatch):
    monkeypatch.setattr(codex_config, "CODEX_OAUTH_DRIVER", "protocol")
    monkeypatch.setattr(codex_config, "resolve_local_proxy", lambda: "http://127.0.0.1:7897")
    factory = Mock(side_effect=RuntimeError("fixture stop before login"))
    monkeypatch.setattr(codex_oauth, "BrowserSession", factory)
    with pytest.raises(RuntimeError, match="fixture stop"):
        codex_oauth.run_codex_oauth("fixture@example.test", force=True, proxy="http://registration.example:8000")
    factory.assert_called_once_with(proxy="http://127.0.0.1:7897")


def test_codex_headless_skips_visible_window_positioning(monkeypatch):
    monkeypatch.setattr(codex_config, "CODEX_HEADLESS", True)
    monkeypatch.setattr(roxy_codex_oauth._roxy_cfg, "ROXY_OPEN_HEADLESS", False)
    monkeypatch.setattr(roxy_codex_oauth._roxy_cfg, "ROXY_KEEP_BROWSER_OPEN", False)
    client, driver, center = Mock(), Mock(), Mock()
    client.open_profile.return_value = SimpleNamespace(profile_id="fixture", raw={})
    monkeypatch.setattr(roxy_codex_oauth, "RoxyBrowserClient", Mock(return_value=client))
    monkeypatch.setattr(roxy_codex_oauth, "_build_driver", Mock(return_value=driver))
    monkeypatch.setattr(roxy_codex_oauth, "_center_browser_window", center)
    monkeypatch.setattr(codex_oauth, "_codex_auth_url_source", lambda: "local")
    monkeypatch.setattr(roxy_codex_oauth, "_fill_email_and_otp", Mock(side_effect=RuntimeError("fixture stop")))
    result = roxy_codex_oauth._run_roxy_codex_oauth_once("fixture@example.test", force=True, proxy="http://127.0.0.1:7897")
    assert result["status"] == "failed"
    client.open_profile.assert_called_once_with(headless=True)
    center.assert_not_called()
    driver.quit.assert_called_once()
    client.cleanup_profile.assert_called_once_with(client.open_profile.return_value)


@pytest.mark.parametrize("provider,label,key,base", [
    ("herosms", "HeroSMS", "hero-key", "https://hero.example/handler"),
    ("smsbower", "SMSBower", "bower-key", "https://bower.example/handler"),
])
def test_sms_polling_keeps_platform_route_key_and_redacted_logs(monkeypatch, caplog, provider, label, key, base):
    from core import registration_service
    monkeypatch.setattr(registration_service, "check_stop_requested", lambda: None)
    monkeypatch.setattr(codex_config, "SMS_PROVIDER", provider)
    for name, value in {"SMS_API_BASE": "https://hero.example/handler", "SMSBOWER_API_BASE": "https://bower.example/handler",
                        "SMS_API_KEY": "hero-key", "SMSBOWER_API_KEY": "bower-key"}.items():
        monkeypatch.setattr(codex_config, name, value)
    http = Mock()
    http.get.return_value = SimpleNamespace(status_code=200, text="STATUS_OK:812649")
    with caplog.at_level("INFO"):
        assert sms_provider.wait_for_sms_code("fixture-activation", http=http, max_wait=1) == "812649"
    assert http.get.call_args.args[0] == base
    assert http.get.call_args.kwargs["params"]["api_key"] == key
    assert f"[SMS:{label}]" in caplog.text
    assert "812649" not in caplog.text and key not in caplog.text
