# -*- coding: utf-8 -*-
from unittest.mock import patch

from core import roxy_codex_oauth


def test_codex_proxy_preflight_rejects_dead_route_before_profile_creation():
    with patch.object(roxy_codex_oauth, "_resolve_codex_local_proxy", return_value="http://127.0.0.1:7890"), \
         patch.object(roxy_codex_oauth, "probe_proxy_exit_geo", return_value={}):
        result = roxy_codex_oauth.run_roxy_codex_oauth("dead-proxy@example.test", force=True)

    assert result["status"] == "failed"
    assert result["ok"] is False
    assert "stage=proxy_transport" in result["message"]
    assert "Profile" not in result["message"]


def test_codex_existing_registration_profile_skips_duplicate_preflight():
    with patch.object(roxy_codex_oauth, "_resolve_codex_local_proxy", return_value="http://127.0.0.1:7890"), \
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
