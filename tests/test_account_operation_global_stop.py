# -*- coding: utf-8 -*-
from unittest.mock import patch

from core import account_operation_control
from webui.app import create_app


def test_operation_control_cancels_old_generation_but_allows_new_work():
    before = account_operation_control.snapshot()
    assert account_operation_control.is_cancelled(before) is False
    after = account_operation_control.request_stop_all()
    assert after > before
    assert account_operation_control.is_cancelled(before) is True
    current = account_operation_control.snapshot()
    assert current == after
    assert account_operation_control.is_cancelled(current) is False


def test_accounts_stop_all_route_stops_all_account_page_services():
    with patch("webui.app.at_validity_scheduler.ensure_started"):
        app = create_app(auth_code="test-auth")
    client = app.test_client()
    headers = {"X-Auth-Code": "test-auth"}
    with patch("webui.app.account_operation_control.request_stop_all", return_value=17) as stop, \
         patch("webui.app.db.stop_account_page_operations", return_value={"total": 4}) as db_stop, \
         patch("webui.app.account_security_service.stop_all", return_value={"active_count": 1, "closed_count": 1}) as security, \
         patch("webui.app.gc_registration_service.stop_all_plan_polls", return_value={"stopped_count": 2}) as gc, \
         patch("webui.app.codex_retry_service.request_stop_all", return_value={"stopped_count": 3}) as codex:
        response = client.post("/api/accounts/stop-all", headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert body["generation"] == 17
    assert body["account_statuses"]["total"] == 4
    assert body["security"]["closed_count"] == 1
    assert body["gc"]["stopped_count"] == 2
    assert body["codex"]["stopped_count"] == 3
    stop.assert_called_once_with()
    db_stop.assert_called_once_with()
    security.assert_called_once_with()
    gc.assert_called_once_with()
    codex.assert_called_once_with()


def test_at_qualification_route_accepts_multiple_tokens_and_returns_each_email():
    with patch("webui.app.at_validity_scheduler.ensure_started"):
        app = create_app(auth_code="test-auth")
    client = app.test_client()
    headers = {"X-Auth-Code": "test-auth"}

    def identity(value):
        token = str(value).strip()
        return {"access_token": token, "email": f"{token}@example.com"}

    def probe(token, **kwargs):
        return {
            "ok": True,
            "gcash": str(token) == "token-1",
            "checked_at": "2026-08-23T12:00:00",
            "attempt_count": 1,
            "detection_outcome": "no_gcash_in_create_response" if str(token) != "token-1" else "gcash_found",
        }

    with patch("webui.app._access_token_identity", side_effect=identity), \
         patch("core.detection_proxy.qualification_proxy_specs", return_value=["PH|ph-spec"]), \
         patch("core.detection_proxy.parse_detection_proxy_pool", return_value=["ph-spec"]), \
         patch("core.detection_proxy.resolve_static_detection_proxy", return_value="http://ph.example:80"), \
         patch("core.detection_proxy.resolve_detection_proxy", return_value="http://ph.example:80"), \
         patch("webui.app.gcash_service.probe_access_token", side_effect=probe) as probe_mock:
        response = client.post(
            "/api/accounts/at-qualification-check",
            headers=headers,
            json={"text": "token-1\ntoken-2", "qualification": "gcash"},
        )

    assert response.status_code == 200
    body = response.get_json()
    assert body["count"] == 2
    assert [item["email"] for item in body["results"]] == [
        "token-1@example.com", "token-2@example.com",
    ]
    assert [item["status"] for item in body["results"]] == ["eligible", "not_eligible"]
    assert probe_mock.call_count == 2


def test_at_qualification_route_rejects_unknown_qualification():
    with patch("webui.app.at_validity_scheduler.ensure_started"):
        app = create_app(auth_code="test-auth")
    response = app.test_client().post(
        "/api/accounts/at-qualification-check",
        headers={"X-Auth-Code": "test-auth"},
        json={"text": "token-1", "qualification": "plus"},
    )
    assert response.status_code == 400
    assert "仅支持 GCash" in response.get_json()["error"]
