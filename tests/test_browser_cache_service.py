# -*- coding: utf-8 -*-
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import browser_cache_service as cache_service
from webui.app import create_app


def _activity(*, jobs=0, browser=0):
    return (
        patch.object(cache_service, "_active_job_count", return_value=jobs),
        patch.object(
            cache_service,
            "_process_counts",
            return_value=({"RoxyBrowser": 1, "RoxyChrome": browser, "chromedriver": 0}, ""),
        ),
    )


def test_cache_status_separates_reclaimable_browser_data_from_shared_static_cache():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roxy_root = root / "RoxyBrowser"
        known_id = "a" * 32
        orphan_id = "b" * 32
        profile = roxy_root / "browser-cache" / known_id / "Default"
        orphan = roxy_root / "browser-cache" / orphan_id / "Default"
        (profile / "Cache").mkdir(parents=True)
        (profile / "Code Cache").mkdir(parents=True)
        (profile / "Cache" / "asset.bin").write_bytes(b"cache")
        (profile / "Cookies").write_bytes(b"keep-account-state")
        (orphan / "runtime.bin").mkdir(parents=True)
        (orphan / "runtime.bin" / "payload").write_bytes(b"orphan-profile")
        (roxy_root / "Cache").mkdir(parents=True)
        (roxy_root / "Cache" / "ui.bin").write_bytes(b"ui-cache")
        static = root / "static"
        static.mkdir()
        (static / "public.bin").write_bytes(b"shared-static")

        with patch.object(cache_service, "_roxy_root", return_value=roxy_root), \
             patch.object(cache_service, "_project_static_cache_root", return_value=static), \
             patch.object(cache_service, "_profile_inventory", return_value=({known_id}, True, "")), \
             _activity()[0], _activity()[1]:
            status = cache_service.cache_status()
            assert status["safe_to_clear"] is True
            assert status["scopes"]["profile_web_cache"]["bytes"] == 5
            assert status["scopes"]["roxy_app_cache"]["bytes"] == 8
            assert status["scopes"]["orphan_profile_store"]["bytes"] == len(b"orphan-profile")
            assert status["scopes"]["shared_static_cache"]["clearable"] is True
            assert status["browser_reclaimable_bytes"] == 5 + len(b"orphan-profile") + 8
            assert status["shared_reclaimable_bytes"] == len(b"shared-static")

            result = cache_service.clear_cache()

        assert result["ok"] is True
        assert result["deleted_bytes"] == 5 + len(b"orphan-profile") + 8
        assert not (profile / "Cache" / "asset.bin").exists()
        assert not (roxy_root / "Cache" / "ui.bin").exists()
        assert (profile / "Cookies").read_bytes() == b"keep-account-state"
        assert not (roxy_root / "browser-cache" / orphan_id).exists()
        assert (static / "public.bin").read_bytes() == b"shared-static"


def test_profile_inventory_reads_official_v3_ids_without_exposing_rows():
    response = MagicMock()
    response.json.return_value = {
        "code": 0,
        "data": {
            "total": 2,
            "rows": [{"dirId": "A" * 32}, {"dirId": "b" * 32}],
        },
    }
    client = MagicMock()
    client.api_base = "http://127.0.0.1:50000"
    client.http.get.return_value = response
    with patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client):
        profile_ids, ok, error = cache_service._profile_inventory()

    assert ok is True
    assert error == ""
    assert profile_ids == {"a" * 32, "b" * 32}
    client.http.get.assert_called_once()


def test_cache_clear_skips_when_registration_jobs_are_active():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roxy_root = root / "RoxyBrowser"
        cache = roxy_root / "Cache"
        cache.mkdir(parents=True)
        item = cache / "ui.bin"
        item.write_bytes(b"keep")
        with patch.object(cache_service, "_roxy_root", return_value=roxy_root), \
             patch.object(cache_service, "_project_static_cache_root", return_value=root / "static"), \
             patch.object(cache_service, "_profile_inventory", return_value=(set(), True, "")), \
             _activity(jobs=1)[0], _activity(jobs=1)[1]:
            result = cache_service.clear_cache()

        assert result["ok"] is False
        assert result["http_status"] == 409
        assert item.exists()


def test_cache_clear_skips_when_roxy_browser_process_is_active():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roxy_root = root / "RoxyBrowser"
        cache = roxy_root / "Cache"
        cache.mkdir(parents=True)
        item = cache / "ui.bin"
        item.write_bytes(b"keep")
        with patch.object(cache_service, "_roxy_root", return_value=roxy_root), \
             patch.object(cache_service, "_project_static_cache_root", return_value=root / "static"), \
             patch.object(cache_service, "_profile_inventory", return_value=(set(), True, "")), \
             _activity(browser=1)[0], _activity(browser=1)[1]:
            result = cache_service.clear_cache()

        assert result["ok"] is False
        assert result["http_status"] == 409
        assert item.exists()


def test_shared_static_cache_clear_is_separate_and_preserves_profile_data():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        roxy_root = root / "RoxyBrowser"
        roxy_root.mkdir(parents=True)
        static = root / "static"
        static.mkdir()
        (static / "public.bin").write_bytes(b"shared-static")
        with patch.object(cache_service, "_roxy_root", return_value=roxy_root), \
             patch.object(cache_service, "_project_static_cache_root", return_value=static), \
             patch.object(cache_service, "_PROJECT_ROOT", root), \
             _activity()[0], _activity()[1]:
            result = cache_service.clear_shared_static_cache()

        assert result["ok"] is True
        assert result["deleted_bytes"] == len(b"shared-static")
        assert not (static / "public.bin").exists()


def test_webui_cache_route_requires_confirmation_and_exposes_status():
    app = create_app(auth_code="test-auth")
    client = app.test_client()
    headers = {"X-Auth-Code": "test-auth"}
    page = client.get("/", headers=headers)
    assert page.status_code == 200
    assert b"btnClearRoxyCacheV2" in page.data
    assert b"btnClearRoxySharedCacheV2" in page.data
    with patch("webui.app.browser_cache_service.cache_status", return_value={
        "ok": True,
        "safe_to_clear": True,
        "reclaimable_bytes": 123,
        "reclaimable_files": 1,
        "scopes": {},
        "blockers": [],
    }) as status:
        response = client.get("/api/roxy/cache/status", headers=headers)
    assert response.status_code == 200
    assert response.get_json()["reclaimable_bytes"] == 123
    status.assert_called_once_with()

    response = client.post("/api/roxy/cache/clear", headers=headers, json={})
    assert response.status_code == 400
    assert "确认" in response.get_json()["error"]


def test_webui_cache_clear_route_forwards_confirmed_cleanup():
    app = create_app(auth_code="test-auth")
    client = app.test_client()
    headers = {"X-Auth-Code": "test-auth"}
    with patch("webui.app.browser_cache_service.clear_cache", return_value={
        "ok": True,
        "partial": False,
        "deleted_bytes": 456,
        "deleted_files": 2,
        "status": {"ok": True, "reclaimable_bytes": 0},
    }) as clear:
        response = client.post("/api/roxy/cache/clear", headers=headers, json={"confirm": True})
    assert response.status_code == 200
    assert response.get_json()["deleted_bytes"] == 456
    clear.assert_called_once_with()


def test_webui_shared_cache_clear_route_forwards_confirmed_cleanup():
    app = create_app(auth_code="test-auth")
    client = app.test_client()
    headers = {"X-Auth-Code": "test-auth"}
    with patch("webui.app.browser_cache_service.clear_shared_static_cache", return_value={
        "ok": True,
        "partial": False,
        "deleted_bytes": 789,
        "deleted_files": 3,
        "status": {"ok": True},
    }) as clear:
        response = client.post("/api/roxy/cache/clear-shared", headers=headers, json={"confirm": True})
    assert response.status_code == 200
    assert response.get_json()["deleted_bytes"] == 789
    clear.assert_called_once_with()
