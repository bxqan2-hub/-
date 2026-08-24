from __future__ import annotations

import subprocess
from unittest.mock import patch

from core import upstream_registration_bridge as bridge


def test_direct_upstream_checkout_is_pinned_and_runnable():
    info = bridge.verify_checkout()
    assert info["platform_commit"] == bridge.EXPECTED_PLATFORM_COMMIT
    assert info["source_commit"] == bridge.EXPECTED_SOURCE_COMMIT
    assert bridge.run_bridge(
        "bridge.ping", run_item_id=0, input_data={}, config={}, timeout=20,
    ) == {"ok": True, "protocol_version": 1}


def test_windows_bridge_and_node_workers_use_no_window_flag():
    with patch.object(bridge.os, "name", "nt"):
        flags = bridge._windows_creation_flags()
    assert flags & int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def test_local_sentinel_node_worker_uses_no_window_flag(monkeypatch):
    from core import sentinel_runner

    monkeypatch.setattr(sentinel_runner.sys, "platform", "win32")
    assert sentinel_runner._windows_creation_flags() & int(
        getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )


def test_main_roxy_route_uses_direct_upstream_bridge():
    source = (bridge.PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    assert "from core.upstream_registration_bridge import run_roxy_registration" in source
    assert "from core.roxy_registration import run_roxy_registration" not in source


def test_upstream_registration_result_is_persisted_by_local_shell():
    registration = {
        "email": "direct@example.com",
        "access_token": "token-value",
        "totp_secret": "totp-value",
        "extra": {
            "proxy_used": "http://proxy.example:8080",
            "roxybrowser": {"profile_id": "profile-1"},
        },
    }
    with (
        patch.object(bridge, "_sync_runtime_materials"),
        patch.object(bridge, "_registration_config", return_value={"roxy_keep_browser_open": False}),
        patch.object(bridge, "run_bridge", return_value={"ok": True, "registration": registration}) as run,
        patch("core.registration_service.current_job_id", return_value=91),
        patch("core.registration_service.check_stop_requested"),
        patch("core.registration_service.bind_roxy_profile") as bind,
        patch("core.account_export.save_account_data", return_value=27) as save,
        patch("core.email_provider.resolve_email_source", return_value="generic_api"),
    ):
        result = bridge.run_roxy_registration(
            email="direct@example.com",
            name="Direct User",
            birthday="1995-04-03",
            proxy="http://proxy.example:8080",
        )

    assert run.call_args.args[0] == "register.roxy"
    assert run.call_args.kwargs["run_item_id"] == 91
    assert run.call_args.kwargs["input_data"]["email"] == "direct@example.com"
    assert result["success"] is True
    assert result["account_id"] == 27
    assert result["roxy_profile_id"] == "profile-1"
    assert save.call_args.kwargs["access_token"] == "token-value"
    bind.assert_not_called()


def test_bridge_environment_loads_no_window_sitecustomize():
    env = bridge._bridge_environment()
    assert env["PYTHONPATH"].split(bridge.os.pathsep)[0] == str(bridge.SITE_CUSTOMIZE_ROOT)
    assert env["APP_DATA_DIR"] == str(bridge.PROJECT_ROOT / "data")
