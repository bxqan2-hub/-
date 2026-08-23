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
