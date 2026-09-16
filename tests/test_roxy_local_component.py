"""Local/official routing and vendored API contracts; no account registration."""
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from config import env_loader, roxybrowser
from core import browser_cache_service
from core import roxybrowser_client as mod
from webui import config_editor


@pytest.fixture
def local_client(monkeypatch):
    monkeypatch.setattr(roxybrowser, "ROXY_LOCAL_COMPONENT", True)
    monkeypatch.setattr(roxybrowser, "ROXY_CORE_VERSION", "151")
    monkeypatch.setattr(roxybrowser, "ROXY_WORKSPACE_ID", "")
    monkeypatch.setattr(roxybrowser, "ROXY_PROJECT_ID", "")
    monkeypatch.setattr(roxybrowser, "ROXY_PROFILE_ID", "")
    monkeypatch.setattr(roxybrowser, "ROXY_ONE_PROFILE_PER_ACCOUNT", True)
    monkeypatch.setattr(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False)
    monkeypatch.setattr(roxybrowser, "ROXY_DELETE_PROFILE_AFTER_RUN", True)
    monkeypatch.setattr("core.browser_exit_geo.probe_proxy_exit_geo", lambda *a, **k: {"ip": "203.0.113.8", "country": "GB", "timezone": "Europe/London"})
    return mod.RoxyBrowserClient(profile_proxy="socks5h://user:p%3Aa%40ss@proxy.example:1080")


def test_switch_retains_official_connection_and_local_never_sends_its_token(monkeypatch):
    monkeypatch.setattr(roxybrowser, "ROXY_API_BASE", "http://127.0.0.1:50000")
    monkeypatch.setattr(roxybrowser, "ROXY_API_TOKEN", "fixture-official-token")
    monkeypatch.setattr(roxybrowser, "ROXY_LOCAL_COMPONENT", False)
    official = mod.RoxyBrowserClient()
    monkeypatch.setattr(roxybrowser, "ROXY_LOCAL_COMPONENT", True)
    local = mod.RoxyBrowserClient()
    assert official.api_base.endswith(":50000") and not official.local_component
    assert official.http.headers["token"] == "fixture-official-token"
    assert local.api_base == mod._LOCAL_API_BASE and local.local_component
    assert "token" not in local.http.headers and "Authorization" not in local.http.headers
    assert local.http.trust_env is False


def test_switch_field_round_trip_uses_existing_environment_loader(monkeypatch, tmp_path):
    field = next(f for f in config_editor.EDITABLE_FIELDS if f["key"] == "ROXY_LOCAL_COMPONENT")
    assert field["type"] == "bool" and field["group"] == "RoxyBrowser"
    monkeypatch.setattr(env_loader, "_ENV_PATH", tmp_path / ".env")
    with patch.dict(os.environ):
        for enabled in (True, False):
            assert config_editor.update_config({"ROXY_LOCAL_COMPONENT": enabled})["updated"] == ["ROXY_LOCAL_COMPONENT"]
            values = {"ROXY_LOCAL_COMPONENT": False}
            env_loader.apply_env_overrides(values, {"ROXY_LOCAL_COMPONENT": "bool"})
            assert values["ROXY_LOCAL_COMPONENT"] is enabled


def test_local_create_open_delete_translate_without_changing_registration(local_client, monkeypatch):
    client = local_client
    monkeypatch.setattr(roxybrowser, "ROXY_OPEN_EXTRA_PARAMS", {"dirId": "stale", "args": ["--custom"]})
    pid = "a" * 32
    calls = []
    def request(method, path, **kw):
        calls.append((method, path, kw))
        return {"code": 0, "data": {"dirId": pid, "http": "127.0.0.1:9222", "coreVersion": "151"}}
    monkeypatch.setattr(client, "request", request)
    reported = []
    opened = client.open_profile(headless=True, on_profile_ready=reported.append)
    monkeypatch.setattr(roxybrowser, "ROXY_LOCAL_COMPONENT", False)
    client.cleanup_profile(opened)
    create = calls[0][2]["json_body"]
    launch = calls[1][2]["json_body"]
    assert create["proxy"] == "socks5://user:p%3Aa%40ss@proxy.example:1080"
    assert create["open"] is False and create["randomFingerprint"] is True
    assert create["coreVersion"] == "151"
    assert create["country"] == "GB" and create["timeZone"] == "Europe/London"
    assert create["os"] in {"Windows 10", "Windows 11"}
    assert create["portScanWhiteList"] == "1455;"
    assert "workspaceId" not in create and "proxyInfo" not in create
    assert launch["dirId"] == pid and launch["headless"] is True
    assert "--disable-component-update" in launch["args"] and "--custom" in launch["args"]
    assert [item[1] for item in calls] == ["/browser/create", "/browser/open", "/browser/close", "/browser/delete"]
    assert calls[-1][2]["json_body"] == {"dirId": pid}
    assert reported == ["local-" + pid] and opened.profile_id == reported[0]
    assert client._last_profile_create_summary["os"] == create["os"]


@pytest.mark.parametrize("method", ["close_profile", "delete_profile"])
@pytest.mark.parametrize("local_id", [True, False])
def test_cleanup_uses_profile_owner_after_switch(monkeypatch, method, local_id):
    client = mod.RoxyBrowserClient(local_component=not local_id)
    profile = "local-" + "b" * 32 if local_id else "official-profile"
    owner = MagicMock()
    getattr(owner, method).return_value = True
    with patch.object(mod, "RoxyBrowserClient", return_value=owner) as factory:
        assert getattr(client, method)(profile) is True
    factory.assert_called_once_with(local_component=local_id)
    getattr(owner, method).assert_called_once_with(profile)


def test_local_create_direct_is_explicit(local_client, monkeypatch):
    monkeypatch.setattr(local_client, "_ensure_profile_proxy", lambda: "")
    monkeypatch.setattr(roxybrowser, "ROXY_PROFILE_CREATE_PAYLOAD", {})
    with patch.object(local_client, "request", return_value={"data": {"dirId": "c" * 32}}) as request:
        local_client.create_profile()
    assert request.call_args.kwargs["json_body"]["proxy"] == "direct"


@pytest.mark.parametrize("geo", [{"ip": "203.0.113.8", "country": "GB"}, {"ip": "203.0.113.8", "timezone": "Europe/London"}])
def test_local_geo_missing_stops_before_creating_profile(local_client, monkeypatch, geo):
    monkeypatch.setattr("core.browser_exit_geo.probe_proxy_exit_geo", lambda *a, **k: geo)
    with patch.object(local_client, "request") as request:
        with pytest.raises(RuntimeError, match="local_fingerprint_geo"):
            local_client.open_profile()
    request.assert_not_called()


def test_local_workspace_health_does_not_discover_official_teams(local_client):
    with patch.object(local_client, "ensure_local_api") as ensure, patch.object(local_client, "request") as request:
        result = local_client.list_workspaces()
    ensure.assert_called_once()
    request.assert_not_called()
    assert result["ok"] and result["local_component"] and result["items"] == []


def test_local_request_ensures_service_once_and_keeps_proxy_out_of_debug(local_client, caplog):
    response = MagicMock(status_code=200, text='{"code":0}')
    response.json.return_value = {"code": 0}
    with patch.object(local_client, "ensure_local_api") as ensure, patch.object(local_client.http, "request", return_value=response) as request, caplog.at_level("DEBUG", logger=mod.__name__):
        local_client.request("POST", "/browser/create", json_body={"proxy": "http://secret:password@fixture:80"})
        local_client.request("POST", "/browser/open", json_body={})
    ensure.assert_called_once()
    assert request.call_args.kwargs["timeout"] >= 55
    assert "secret" not in caplog.text and "password" not in caplog.text


def test_ready_local_api_is_reused_without_process_start(local_client):
    with patch.object(local_client, "_health", return_value=(True, "")), patch.object(mod.subprocess, "Popen") as spawn:
        local_client.ensure_local_api()
    spawn.assert_not_called()


def test_occupied_port_is_not_replaced(local_client):
    with patch.object(local_client, "_health", return_value=(False, "other service")), patch.object(mod.subprocess, "Popen") as spawn:
        with pytest.raises(RuntimeError, match="占用"):
            local_client.ensure_local_api()
    spawn.assert_not_called()


def test_local_service_start_passes_profile_scope_and_waits_for_health(local_client, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_ROOT", tmp_path)
    process = MagicMock(pid=123)
    with patch.object(local_client, "_health", side_effect=[(False, ""), (False, ""), (True, "")]), patch.object(mod.shutil, "which", return_value="node"), patch.object(mod.subprocess, "Popen", return_value=process) as spawn:
        local_client.ensure_local_api()
    command = spawn.call_args.args[0]
    assert command[-2:] == ["--profile-dir", str(mod._LOCAL_PROFILE_ROOT)]
    assert spawn.call_args.kwargs["stdin"] == subprocess.DEVNULL
    if os.name == "nt":
        assert spawn.call_args.kwargs["startupinfo"].wShowWindow == 0


def test_cache_inventory_matches_local_profile_scope(local_client, monkeypatch):
    monkeypatch.setattr(mod, "RoxyBrowserClient", lambda: local_client)
    pid = "a" * 32
    monkeypatch.setattr(local_client, "request", lambda *a: {"data": {"rows": [{"dirId": pid}], "total": 1}})
    assert browser_cache_service._profile_inventory() == ({pid}, True, "")
    assert browser_cache_service._roxy_root() == mod._LOCAL_PROFILE_ROOT.parent


@pytest.fixture(scope="module")
def node_api(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for local component contracts")
    root = tmp_path_factory.mktemp("roxy-node-contract")
    for version in ("151", "153"):
        core = root / "roxy" / "chrome-bin" / version
        core.mkdir(parents=True)
        (core / "RoxyChrome.exe").write_bytes(b"test fixture: never launched")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    profiles = root / "profiles"
    session = requests.Session()
    session.trust_env = False
    with (root / "node.log").open("wb") as log:
        process = subprocess.Popen([node, str(mod._BUNDLED_ROXY_API), "--port", str(port), "--data-dir", str(root / "roxy"), "--profile-dir", str(profiles)], stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if session.get(base + "/health", timeout=.5).status_code == 200:
                    break
            except requests.RequestException:
                pass
            if process.poll() is not None:
                pytest.fail((root / "node.log").read_text(errors="replace"))
            time.sleep(.1)
        else:
            pytest.fail("fixture API health timeout")
        yield session, base, profiles
    finally:
        process.terminate()
        process.wait(timeout=10)
        session.close()


def test_vendored_api_random_profiles_proxy_codec_and_idempotent_delete(node_api):
    session, base, profiles = node_api
    created = []
    fingerprints = []
    try:
        for _ in range(2):
            response = session.post(base + "/browser/create", json={"proxy": "socks5h://u:p%3Aa%40ss@[::1]:1080", "coreVersion": "151", "randomFingerprint": True}, timeout=5).json()
            assert response["code"] == 0
            pid = response["data"]["dirId"]
            created.append(pid)
            fp = session.get(base + "/browser/fingerprint", params={"dirId": pid, "full": 1}, timeout=5).json()["data"]
            assert fp["fproxy"]["type"] == "socks5"
            assert fp["chromeVersion"] == "151" and "Chrome/151." in fp["userAgent"]
            assert fp["fproxy"]["password"] == "p:a@ss" and fp["fproxy"]["host"] == "::1"
            fingerprints.append(fp)
        assert created[0] != created[1]
        assert fingerprints[0]["canvasContext"] != fingerprints[1]["canvasContext"]
        direct = session.post(base + "/browser/create", json={}, timeout=5).json()["data"]["dirId"]
        created.append(direct)
        fp = session.get(base + "/browser/fingerprint", params={"dirId": direct, "full": 1}, timeout=5).json()["data"]
        assert "fproxy" not in fp
        assert fp["chromeVersion"] == "153"  # Only an unspecified core selects latest.
    finally:
        for pid in created:
            for _ in range(2):
                assert session.post(base + "/browser/delete", json={"dirId": pid}, timeout=5).json()["code"] == 0
            assert not (profiles / pid).exists()


def test_vendored_api_rejects_cross_origin_bad_json_paths_and_missing_core(node_api):
    session, base, profiles = node_api
    before = list(profiles.iterdir())
    assert session.get(base + "/browser/create", timeout=5).status_code == 405
    assert session.post(base + "/browser/create", json={}, headers={"Origin": "https://foreign.invalid"}, timeout=5).status_code == 403
    assert session.get(base + "/health", headers={"Host": "foreign.invalid"}, timeout=5).status_code == 403
    assert session.post(base + "/browser/create", data="{}", timeout=5).status_code == 415
    assert session.post(base + "/browser/create", data="{bad", headers={"Content-Type": "application/json"}, timeout=5).json()["code"] != 0
    assert session.post(base + "/browser/create", json={"coreVersion": "152"}, timeout=5).json()["code"] != 0
    assert session.post(base + "/browser/delete", json={"dirId": "../escape"}, timeout=5).json()["code"] != 0
    assert list(profiles.iterdir()) == before


@pytest.mark.parametrize("country,zone,locale", [
    ("GB", "Europe/London", "en-GB"), ("JP", "Asia/Tokyo", "ja-JP"),
    ("US", "America/Chicago", "en-US"), ("US", "America/Los_Angeles", "en-US"),
    ("AU", "Australia/Sydney", "en-AU"), ("CA", "America/Toronto", "en-CA"),
    ("SG", "Asia/Singapore", "en-SG"), ("MX", "America/Mexico_City", "es-MX"),
])
def test_vendored_api_matches_actual_proxy_geography(node_api, country, zone, locale):
    session, base, profiles = node_api
    response = session.post(base + "/browser/create", json={"country": country, "timeZone": zone, "locale": "ja-JP", "acceptLang": "ja-JP", "coreVersion": "151"}, timeout=5).json()
    assert response["code"] == 0
    pid = response["data"]["dirId"]
    try:
        fp = session.get(base + "/browser/fingerprint", params={"dirId": pid, "full": 1}, timeout=5).json()["data"]
        assert fp["chromeVersion"] == "151"
        assert fp["appLocale"] == locale and fp["timeZone"] == zone
        assert fp["acceptLang"] == locale
        assert response["data"]["locale"] == locale and response["data"]["timeZone"] == zone
    finally:
        session.post(base + "/browser/delete", json={"dirId": pid}, timeout=5)


@pytest.mark.parametrize("body", [{"country": "GB"}, {"country": "GB", "timeZone": "Invalid/Zone"}, {"country": "invalid", "timeZone": "Europe/London"}])
def test_vendored_api_bad_geo_leaves_no_profile(node_api, body):
    session, base, profiles = node_api
    before = list(profiles.iterdir())
    assert session.post(base + "/browser/create", json=body, timeout=5).json()["code"] != 0
    assert list(profiles.iterdir()) == before
