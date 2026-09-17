# -*- coding: utf-8 -*-
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import proxy as proxy_config
from core import account_operation_control, db, detection_proxy, upi_service
from webui.app import _compact_account_for_list, create_app


INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integrations" / "pay153_checkout"
if str(INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_DIR))
import app as pay153_app  # noqa: E402


@pytest.fixture
def protocol(monkeypatch):
    http = Mock(spec=["close"])
    mocks = SimpleNamespace(http=http)
    for owner, name in ((pay153_app, "create_checkout"), (pay153_app, "fetch_custom_checkout_session"),
                        (pay153_app.sc, "init_checkout"), (pay153_app.sc, "verify_pk")):
        mock = Mock(side_effect=AssertionError(f"unexpected {name}"))
        monkeypatch.setattr(owner, name, mock)
        setattr(mocks, name, mock)
    for owner, name in ((pay153_app.sc, "build_http"), (pay153_app.sc, "confirm_payment"),
                        (pay153_app, "confirm_custom_checkout_method"),
                        (pay153_app, "update_checkout_promo"), (pay153_app, "run_upi_go")):
        mock = Mock(side_effect=AssertionError(f"query must not call {name}"))
        monkeypatch.setattr(owner, name, mock)
        setattr(mocks, name, mock)
    mocks.create_checkout.side_effect = None
    mocks.create_checkout.return_value = {
        "data": {"checkout_session_id": "cs_live_upi", "publishable_key": "pk_live_fixture"},
        "http": http,
    }
    yield mocks
    for name in ("build_http", "confirm_payment", "confirm_custom_checkout_method", "update_checkout_promo", "run_upi_go"):
        getattr(mocks, name).assert_not_called()


def detect():
    return pay153_app.detect_upi({"token": "aaa.bbb.ccc", "proxy": "http://in.proxy:8080"})


@pytest.mark.parametrize("methods,currency,eligible,outcome", [
    (["card", "upi"], "inr", True, "qualified"),
    (["card"], "inr", False, "no_upi_payment_method"),
    (["card", "upi"], "usd", False, "currency_mismatch"),
])
def test_stripe_query_uses_one_inr_checkout_and_same_session(protocol, methods, currency, eligible, outcome):
    protocol.init_checkout.side_effect = None
    protocol.init_checkout.return_value = ({"currency": currency}, "version", {
        "currency": currency, "payment_method_types": methods, "checkout_amount": 199900,
    })
    result, status = detect()
    assert status == 200 and result["ok"] is True
    assert result["upi"] is eligible
    assert result["detection_outcome"] == outcome
    assert result["confirm_sent"] is False and result["promo_update_sent"] is False
    assert result["inspection_steps"] == ["checkout_create", "stripe_init"]
    protocol.create_checkout.assert_called_once()
    token, payload, proxy, *_ = protocol.create_checkout.call_args.args
    assert token == "aaa.bbb.ccc" and proxy == "http://in.proxy:8080"
    assert payload["billing_details"] == {"country": "IN", "currency": "INR"}
    assert "promo_campaign" not in payload and "subscription_data" not in payload
    protocol.init_checkout.assert_called_once()
    assert protocol.init_checkout.call_args.args[:3] == (protocol.http, "cs_live_upi", "pk_live_fixture")
    protocol.http.close.assert_called_once()


@pytest.mark.parametrize("session_id,method_payload", [
    ("cs_live_upi", {"payment_method_types": ["card", "upi"]}),
    ("oaics_upi", {"custom_payment_methods": [{"id": "cpmt_upi", "type": "UPI"}]}),
    ("oaics_upi", {"checkout_session": {"available_payment_methods": [{"name": "UPI"}]}}),
])
def test_creation_response_is_sufficient_method_evidence(protocol, session_id, method_payload):
    protocol.create_checkout.return_value["data"] = {"checkout_session_id": session_id, **method_payload}
    result, status = detect()
    assert status == 200 and result["upi"] is True
    assert result["inspection_steps"] == ["checkout_create"]
    protocol.init_checkout.assert_not_called()
    protocol.fetch_custom_checkout_session.assert_not_called()
    protocol.http.close.assert_called_once()


def test_oaics_read_preserves_http_token_and_checkout_identity(protocol):
    protocol.create_checkout.return_value["data"] = {
        "checkout_session_id": "oaics_upi", "processor_entity": "openai_ie",
    }
    protocol.fetch_custom_checkout_session.side_effect = None
    protocol.fetch_custom_checkout_session.return_value = {
        "currency": "inr", "custom_payment_methods": [{"type": "upi"}],
    }
    result, status = detect()
    assert status == 200 and result["upi"] is True
    assert result["inspection_steps"] == ["checkout_create", "oaics_custom_checkout"]
    protocol.fetch_custom_checkout_session.assert_called_once()
    assert protocol.fetch_custom_checkout_session.call_args.args[:4] == (
        protocol.http, "aaa.bbb.ccc", "oaics_upi", "openai_ie",
    )
    assert protocol.fetch_custom_checkout_session.call_args.args[4] == protocol.create_checkout.call_args.args[3]
    protocol.init_checkout.assert_not_called()
    protocol.http.close.assert_called_once()


def test_missing_publishable_key_discovery_keeps_same_http(protocol):
    protocol.create_checkout.return_value["data"].pop("publishable_key")
    protocol.verify_pk.side_effect = None
    protocol.verify_pk.return_value = "pk_fixture"
    protocol.init_checkout.side_effect = None
    protocol.init_checkout.return_value = ({"currency": "inr", "payment_method_types": ["upi"]}, "version", {})
    result, status = detect()
    assert status == 200 and result["upi"] is True
    assert protocol.verify_pk.call_args.args[:2] == (protocol.http, "cs_live_upi")
    assert protocol.init_checkout.call_args.args[:3] == (protocol.http, "cs_live_upi", "pk_fixture")


def test_missing_checkout_session_is_error_and_releases_http(protocol):
    protocol.create_checkout.return_value["data"] = {"payment_method_types": ["upi"]}
    result, status = detect()
    assert status == 502 and result["ok"] is False and result["upi"] is False
    assert result["retryable"] is False
    protocol.init_checkout.assert_not_called()
    protocol.http.close.assert_called_once()


@pytest.mark.parametrize("stage", ["checkout_create", "stripe_init"])
def test_upstream_exceptions_preserve_retry_classification_without_secrets(protocol, stage):
    sensitive = "private-token http://private-user:private-pass@in.proxy:8080 https://checkout.example/secret-session"
    failing = protocol.create_checkout if stage == "checkout_create" else protocol.init_checkout
    failing.side_effect = RuntimeError(f"OpenAI Checkout HTTP 429 {sensitive}")
    result, status = detect()
    assert status == 502 and result["retryable"] is True
    assert result["upstream_http_status"] == 429 and stage in result["error"]
    encoded = json.dumps(result)
    assert all(value not in encoded for value in ("private-token", "private-user", "private-pass", "secret-session"))
    if stage == "stripe_init":
        protocol.http.close.assert_called_once()


@pytest.mark.parametrize("code,retryable", [(401, False), (403, False), (429, True), (503, True)])
def test_stripe_bracketed_status_controls_retry_even_with_transient_words(protocol, code, retryable):
    protocol.init_checkout.side_effect = RuntimeError(f"init 失败 [{code}]: timeout private-token")
    result, status = detect()
    assert status == 502 and result["upstream_http_status"] == code
    assert result["retryable"] is retryable and "private-token" not in result["error"]
    protocol.http.close.assert_called_once()


def test_no_proxy_stops_before_checkout_or_runtime_load(protocol, monkeypatch):
    runtime = Mock()
    monkeypatch.setattr(upi_service, "get_pay153_module", runtime)
    result, status = pay153_app.detect_upi({"token": "aaa.bbb.ccc"})
    assert status == 400 and result["upi"] is False
    assert upi_service.check_upi("token")["ok"] is False
    protocol.create_checkout.assert_not_called()
    runtime.assert_not_called()


def test_proxy_transport_fallback_keeps_token_and_redacts_runtime_exception(monkeypatch):
    runtime = Mock()
    runtime.detect_upi.side_effect = [
        ({"ok": False, "upi": False, "transport_failed": True}, 502),
        ({"ok": True, "upi": True}, 200),
    ]
    monkeypatch.setattr(upi_service, "get_pay153_module", lambda: runtime)
    result = upi_service.check_upi("token", proxy="http://user:pass@in.proxy:8080")
    assert result["upi"] is True and result["transport_attempt_count"] == 2
    assert runtime.detect_upi.call_args.args[0] == {"token": "token", "proxy": "https://user:pass@in.proxy:8080"}
    runtime.detect_upi.side_effect = RuntimeError("private-token user:pass")
    result = upi_service.check_upi("token", proxy="http://in.proxy:8080")
    assert result["ok"] is False and "private-token" not in result["error"] and "user:pass" not in result["error"]


@pytest.mark.parametrize("first,expected_calls", [
    ({"ok": False, "upi": False, "retryable": True}, 2),
    ({"ok": True, "upi": False, "detection_outcome": "no_upi_payment_method"}, 1),
])
def test_proxy_retry_only_retries_indeterminate_errors(monkeypatch, first, expected_calls):
    check = Mock(side_effect=[first, {"ok": True, "upi": True}])
    persist = Mock()
    monkeypatch.setattr(upi_service, "check_upi", check)
    monkeypatch.setattr(db, "mark_account_upi_running", lambda _id, **_kwargs: True)
    monkeypatch.setattr(db, "update_account_upi", persist)
    result = upi_service._run_with_proxy_retry(account_id=3, access_token="token", proxies=["proxy-1", "proxy-2"])
    assert check.call_count == result["attempt_count"] == expected_calls
    assert result["upi"] is (expected_calls == 2)
    persist.assert_called_once_with(3, result, generation=account_operation_control.snapshot())


def test_stop_discards_inflight_success_and_releases_queue_slot(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(upi_service, "_QUEUE_SLOTS", slots)
    monkeypatch.setattr(db, "mark_account_upi_running", lambda _id, **_kwargs: True)
    persist = Mock()
    monkeypatch.setattr(db, "update_account_upi", persist)
    monkeypatch.setattr(upi_service, "check_upi", Mock(return_value={"ok": True, "upi": True}))
    monkeypatch.setattr(account_operation_control, "raise_if_cancelled", Mock(side_effect=[None, account_operation_control.AccountOperationStopped()]))
    result = upi_service._queued_run(account_id=3, access_token="token", proxies=["proxy"], generation=0)
    assert result["stopped"] is True and result["upi"] is False
    persist.assert_not_called()
    assert slots.acquire(blocking=False) is True
    slots.release()


def test_stopped_worker_does_not_overwrite_requeued_account(monkeypatch):
    monkeypatch.setattr(account_operation_control, "_GENERATION", 100)
    row = {"id": 3, "upi_status": "queued"}
    monkeypatch.setattr(db, "_load_accounts", lambda: [row])
    monkeypatch.setattr(db, "_save_accounts", Mock())

    def finish_old_request(*_args, **_kwargs):
        account_operation_control.request_stop_all()
        assert db.stop_account_page_operations()["upi_status"] == 1
        assert db.claim_account_upi(3) is True
        return {"ok": True, "upi": True}

    monkeypatch.setattr(upi_service, "check_upi", finish_old_request)
    result = upi_service._run_with_proxy_retry(account_id=3, access_token="old-token", proxies=["proxy"], generation=100)
    assert result["stopped"] is True
    assert row["upi_status"] == "queued" and row["upi_eligible"] is False
    assert db.update_account_upi(3, {"ok": True, "upi": True}, generation=100) is False
    assert row["upi_status"] == "queued"


def test_old_queued_worker_does_not_claim_new_generation(monkeypatch):
    monkeypatch.setattr(account_operation_control, "_GENERATION", 101)
    row = {"id": 3, "upi_status": "queued", "upi_eligible": False}
    monkeypatch.setattr(db, "_load_accounts", lambda: [row])
    save = Mock()
    monkeypatch.setattr(db, "_save_accounts", save)
    check = Mock()
    monkeypatch.setattr(upi_service, "check_upi", check)
    result = upi_service._run_with_proxy_retry(account_id=3, access_token="old-token", proxies=["proxy"], generation=100)
    assert result["ok"] is False and row["upi_status"] == "queued"
    check.assert_not_called()
    save.assert_not_called()


def test_stop_between_enqueue_snapshot_and_claim_keeps_slot_and_account_unchanged(monkeypatch):
    monkeypatch.setattr(account_operation_control, "_GENERATION", 100)
    row = {"id": 3, "upi_status": "success", "upi_eligible": True}
    monkeypatch.setattr(db, "_load_accounts", lambda: [row])
    save = Mock()
    monkeypatch.setattr(db, "_save_accounts", save)
    original_claim = db.claim_account_upi

    def claim_after_stop(*args, **kwargs):
        account_operation_control.request_stop_all()
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(db, "claim_account_upi", claim_after_stop)
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(upi_service, "_QUEUE_SLOTS", slots)
    executor = Mock()
    result = upi_service.enqueue(account_id=3, access_token="token", proxies=["proxy"], executor=executor)
    assert result["accepted"] is False
    assert row == {"id": 3, "upi_status": "success", "upi_eligible": True}
    executor.submit.assert_not_called()
    save.assert_not_called()
    assert slots.acquire(blocking=False) is True
    slots.release()


def test_stop_between_proxy_transports_prevents_second_checkout(monkeypatch):
    monkeypatch.setattr(account_operation_control, "_GENERATION", 100)

    def stopped_transport(*_args, **_kwargs):
        account_operation_control.request_stop_all()
        return {"ok": False, "upi": False, "transport_failed": True}, 502

    runtime = Mock()
    runtime.detect_upi.side_effect = stopped_transport
    monkeypatch.setattr(upi_service, "get_pay153_module", lambda: runtime)
    with pytest.raises(account_operation_control.AccountOperationStopped):
        upi_service.check_upi("token", proxy="http://in.proxy:8080", generation=100)
    runtime.detect_upi.assert_called_once()


def test_stop_after_creation_prevents_method_reads_and_closes_session(protocol):
    created = protocol.create_checkout.return_value
    cancelled = False

    def create_then_stop(*_args, **_kwargs):
        nonlocal cancelled
        cancelled = True
        return created

    def check_cancelled():
        if cancelled:
            raise account_operation_control.AccountOperationStopped()

    protocol.create_checkout.side_effect = create_then_stop
    with pytest.raises(account_operation_control.AccountOperationStopped):
        pay153_app.detect_upi({"token": "aaa.bbb.ccc", "proxy": "http://in.proxy:8080"}, check_cancelled=check_cancelled)
    protocol.init_checkout.assert_not_called()
    protocol.fetch_custom_checkout_session.assert_not_called()
    protocol.http.close.assert_called_once()


@pytest.mark.parametrize("failure", ["busy", "claim", "submit"])
def test_queue_reservation_is_released_on_failed_enqueue(monkeypatch, failure):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(upi_service, "_QUEUE_SLOTS", slots)
    claim = Mock(return_value=failure != "busy", side_effect=OSError("claim failed") if failure == "claim" else None)
    monkeypatch.setattr(db, "claim_account_upi", claim)
    monkeypatch.setattr(db, "update_account_upi", Mock())
    executor = Mock()
    executor.submit.side_effect = RuntimeError("private-token") if failure == "submit" else None
    for _ in range(2):
        if failure == "claim":
            with pytest.raises(OSError, match="claim failed"):
                upi_service.enqueue(account_id=1, access_token="token", proxies=["proxy"], executor=executor)
        else:
            result = upi_service.enqueue(account_id=1, access_token="token", proxies=["proxy"], executor=executor)
            assert result["accepted"] is False and "private-token" not in result["error"]
        assert slots.acquire(blocking=False) is True
        slots.release()


@pytest.mark.parametrize("operation", ["stop", "recover"])
def test_stop_and_restart_only_reset_unfinished_upi_results(monkeypatch, operation):
    rows = [{"id": i, "upi_status": status, "upi_eligible": True, "gcash_eligible": True}
            for i, status in enumerate(["queued", "running", "success"], 1)]
    monkeypatch.setattr(db, "_load_accounts", lambda: rows)
    save = Mock()
    monkeypatch.setattr(db, "_save_accounts", save)
    count = db.stop_account_page_operations()["upi_status"] if operation == "stop" else db.recover_interrupted_upi_checks()
    assert count == 2
    assert [row["upi_status"] for row in rows] == ["failed", "failed", "success"]
    assert [row["upi_eligible"] for row in rows] == [False, False, True]
    assert all(row["gcash_eligible"] is True for row in rows)
    save.assert_called_once()


def test_completed_negative_result_keeps_reason_without_sensitive_fields(monkeypatch):
    row = {"id": 1, "gcash_eligible": True}
    monkeypatch.setattr(db, "_load_accounts", lambda: [row])
    monkeypatch.setattr(db, "_save_accounts", Mock())
    assert db.update_account_upi(1, {"ok": True, "upi": False, "error": "no UPI", "payment_method_types": ["card"],
                                    "detection_outcome": "no_upi_payment_method", "token": "secret", "checkout_url": "secret"})
    assert row["upi_status"] == "success" and row["upi_eligible"] is False and row["upi_error"] == "no UPI"
    assert row["upi_payment_method_types"] == ["card"] and row["gcash_eligible"] is True
    assert "token" not in row and "checkout_url" not in row


def test_failed_result_cannot_promote_upi_eligibility(monkeypatch):
    row = {"id": 1, "upi_status": "running", "upi_eligible": False}
    monkeypatch.setattr(db, "_load_accounts", lambda: [row])
    monkeypatch.setattr(db, "_save_accounts", Mock())
    assert db.update_account_upi(1, {"ok": False, "upi": True, "error": "incomplete response"})
    assert row["upi_status"] == "failed" and row["upi_eligible"] is False


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("webui.app.at_validity_scheduler.ensure_started", lambda: None)
    result = create_app(auth_code="test-auth").test_client()
    result.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    return result


def test_bulk_route_uses_in_private_pool_deduplicates_and_skips_missing_tokens(client, monkeypatch):
    specs = Mock(return_value=["IN|http://in.proxy:8080"])
    monkeypatch.setattr(detection_proxy, "qualification_proxy_specs", specs)
    monkeypatch.setattr(detection_proxy, "resolve_static_detection_proxy", lambda _spec: "http://in.proxy:8080")
    rows = {1: {"id": 1, "email": "one@test", "access_token": "token"}, 2: {"id": 2, "email": "two@test"}}
    monkeypatch.setattr(db, "get_account", lambda account_id: rows.get(account_id))
    monkeypatch.setattr(upi_service, "get_executor", Mock())
    enqueue = Mock(return_value={"accepted": True, "busy": False})
    monkeypatch.setattr(upi_service, "enqueue", enqueue)
    response = client.post("/api/accounts/check-upi-bulk", json={"account_ids": [1, "1", 2, 3, "bad"]})
    assert response.status_code == 202
    data = response.get_json()
    assert data["started_count"] == 1 and data["skipped_count"] == 3 and data["confirm_sent"] is False
    specs.assert_called_once_with("IN", "upi")
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs["proxies"] == ["http://in.proxy:8080"]
    assert enqueue.call_args.kwargs["access_token"] == "token"


def test_upi_pool_never_uses_other_provider_or_shared_pool(client, monkeypatch):
    monkeypatch.setattr(proxy_config, "UPI_CHECK_PROXY_PROFILES", ["US|http://wrong.proxy:8080"])
    monkeypatch.setattr(proxy_config, "QUALIFICATION_CHECK_PROXY_PROFILES", ["IN|http://shared.proxy:8080"])
    monkeypatch.setattr(proxy_config, "GOPAY_CHECK_PROXY_PROFILES", ["IN|http://gopay.proxy:8080"])
    enqueue = Mock()
    monkeypatch.setattr(upi_service, "enqueue", enqueue)
    assert detection_proxy.qualification_proxy_specs("IN", "upi") == []
    assert client.post("/api/accounts/check-upi-bulk", json={"account_ids": [1]}).status_code == 409
    enqueue.assert_not_called()
    monkeypatch.setattr(proxy_config, "UPI_CHECK_PROXY_PROFILES", ["IN|http://in.proxy:8080", "US|http://wrong.proxy:8080"])
    assert detection_proxy.qualification_proxy_specs("IN", "upi") == ["IN|http://in.proxy:8080"]


def test_upi_proxy_import_and_clear_only_update_upi_configuration(client, monkeypatch):
    monkeypatch.setattr(proxy_config, "UPI_CHECK_PROXY_PROFILES", [])
    monkeypatch.setattr(proxy_config, "UPI_CHECK_PROXY_ACTIVE", "IN")
    monkeypatch.setattr("config.reload_all", lambda: None)
    inspections = {
        "http://in.proxy:8080": {"country": "IN", "country_label": "India", "country_source": "proxy_region_tag"},
        "http://us.proxy:8080": {"country": "US", "country_label": "US", "country_source": "proxy_region_tag"},
    }
    monkeypatch.setattr(detection_proxy, "inspect_static_proxy", lambda proxy, **_kwargs: {
        **inspections[proxy], "proxy": proxy, "masked_proxy": proxy, "exit_ip": "", "region": "", "city": "",
    })
    update = Mock(return_value={"updated": ["UPI_CHECK_PROXY_PROFILES", "UPI_CHECK_PROXY_ACTIVE"], "ignored": []})
    monkeypatch.setattr("webui.app.config_editor.update_config", update)
    response = client.post("/api/detection-proxy-pools/import", json={
        "purpose": "upi", "proxies": list(inspections),
    })
    assert response.status_code == 200 and response.get_json()["failed_count"] == 1
    assert update.call_args.args[0] == {"UPI_CHECK_PROXY_PROFILES": ["IN|http://in.proxy:8080"], "UPI_CHECK_PROXY_ACTIVE": "IN"}
    monkeypatch.setattr(proxy_config, "UPI_CHECK_PROXY_PROFILES", ["IN|http://in.proxy:8080"])
    response = client.post("/api/detection-proxy-pools/delete", json={"purpose": "upi", "country": "IN"})
    assert response.status_code == 200 and response.get_json()["deleted_count"] == 1
    assert update.call_args.args[0] == {"UPI_CHECK_PROXY_PROFILES": [], "UPI_CHECK_PROXY_ACTIVE": ""}


@pytest.mark.parametrize("qualification,expected", [("upi", {1}), ("any", {1, 2})])
def test_upi_filter_applies_to_account_list_and_email_filter(client, monkeypatch, qualification, expected):
    rows = [{"id": 1, "email": "upi@test", "upi_eligible": True},
            {"id": 2, "email": "gopay@test", "gopay_eligible": True}, {"id": 3, "email": "none@test"}]
    monkeypatch.setattr(db, "list_accounts", lambda **_kwargs: rows)
    response = client.get(f"/api/accounts?paged=1&qualification={qualification}")
    assert response.status_code == 200 and {row["id"] for row in response.get_json()["items"]} == expected
    response = client.post("/api/accounts/filter-emails", json={"emails": [r["email"] for r in rows], "qualification": qualification})
    assert response.status_code == 200 and {row["id"] for row in response.get_json()["items"]} == expected


def test_compact_status_includes_upi_and_revision_changes_without_exposing_secrets(monkeypatch):
    row = {"id": 1, "upi_status": "queued", "upi_eligible": False, "updated_at": "same-second",
           "access_token": "private-token", "totp_secret": "private-totp", "upi_payment_method_types": ["upi"]}
    monkeypatch.setattr(db, "_filtered_decorated_accounts", lambda **_kwargs: [row])
    before = db.list_account_plan_check_statuses()
    row.update(upi_status="success", upi_eligible=True)
    after = db.list_account_plan_check_statuses()
    assert before["revision"] != after["revision"]
    for compact in (_compact_account_for_list(row), after["items"][0]):
        assert compact["upi_status"] == "success" and compact["upi_eligible"] is True
        assert compact["upi_payment_method_types"] == ["upi"]
        assert "private-" not in json.dumps(compact)


def test_upi_frontend_badges_queue_marking_and_all_provider_submission(client):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend behavior checks")
    html = client.get("/").get_data(as_text=True)
    script = r"""
const assert = require('node:assert/strict');
const html = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
function source(name) {
  const lines = html.split('\n');
  const start = lines.findIndex(line => line.startsWith('function ' + name + '(') || line.startsWith('async function ' + name + '('));
  assert(start >= 0, name);
  if (lines[start].trimEnd().endsWith('}')) return lines[start];
  const end = lines.findIndex((line, i) => i > start && line.trimEnd() === '}');
  assert(end > start, name);
  return lines.slice(start, end + 1).join('\n');
}
const render = new Function(['fmt', 'esc', '_qualificationBadges'].map(source).join('\n') + '\nreturn _qualificationBadges;')();
for (const [row, css, label] of [
  [{}, 'qualification-badge-muted', 'UPI'],
  [{upi_status:'queued'}, 'status-running', 'UPI 检测中'],
  [{upi_status:'success', upi_eligible:true}, 'qualification-badge-upi', 'UPI'],
  [{upi_status:'success', upi_eligible:false}, 'qualification-badge-failed', 'UPI'],
  [{upi_status:'failed', upi_error:'"<svg onload=alert(1)>'}, 'qualification-badge-failed', 'UPI'],
]) {
  const rendered = render(row);
  const upiBadge = rendered.slice(rendered.lastIndexOf('<span class="pill '));
  assert(upiBadge.includes(`class="pill ${css}"`));
  assert(rendered.includes(`${label}</span></div>`));
  assert(!rendered.includes('<svg'));
  if (row.upi_error) assert(rendered.includes('&lt;svg'));
}
const ACCOUNTS = [{id:1,upi_eligible:true,gopay_eligible:true}, {id:2,upi_eligible:true}];
let renders=0;
const mark = new Function('ACCOUNTS','renderAccounts', source('markQualificationChecksQueued') + '\nreturn markQualificationChecksQueued;')(ACCOUNTS, () => renders++);
mark([{value:'upi',payload:{started:[{id:1}],busy:[{id:2}]}}]);
assert.equal(ACCOUNTS[0].upi_status,'queued');
assert.equal(ACCOUNTS[0].upi_eligible,false);
assert.equal(ACCOUNTS[0].gopay_eligible,true);
assert.equal(ACCOUNTS[1].upi_eligible,true);
assert.equal(renders,1);
const requests=[];
const buttons=[{disabled:false}];
const scope={
  ACCOUNT_SELECTED:new Set([1]), document:{querySelectorAll:()=>buttons}, showToast:()=>{},
  closeQualificationQueryMenu:()=>{}, qualificationQueueSummary:()=>'',
  pollAccountPlanStatuses:async()=>{}, updateAccountSelectionUi:()=>{},
  markQualificationChecksQueued:mark,
  api:async(endpoint,options)=>{requests.push({endpoint,body:JSON.parse(options.body)});return {started:[{id:1}]};},
};
const check = new Function(...Object.keys(scope),source('checkSelectedQualification')+'\nreturn checkSelectedQualification;')(...Object.values(scope));
(async()=>{
  await check('all');
  assert.equal(requests.length,4);
  assert.deepEqual(requests.find(r=>r.endpoint==='/api/accounts/check-upi-bulk').body,{account_ids:[1],workers:1});
  assert.equal(buttons[0].disabled,false);
  const deletionScope={document:{querySelector:()=>null},showToast:()=>{},proxyRegionLabelV2:value=>value,
    confirm:()=>true,loadConfig:async()=>{},api:scope.api};
  const remove=new Function(...Object.keys(deletionScope),source('deleteDetectionProxyCountryV2')+'\nreturn deleteDetectionProxyCountryV2;')(...Object.values(deletionScope));
  await remove({dataset:{deleteDetectionProxyCountry:'upi'},disabled:false});
  assert.deepEqual(requests.at(-1),{endpoint:'/api/detection-proxy-pools/delete',body:{purpose:'upi',country:'IN'}});
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script], input=json.dumps(html), text=True,
                            capture_output=True, timeout=15, encoding="utf-8")
    assert result.returncode == 0, result.stderr
