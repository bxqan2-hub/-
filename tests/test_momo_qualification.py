# -*- coding: utf-8 -*-
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from core import db, momo_service
from webui.app import create_app

INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integrations" / "pay153_checkout"
if str(INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_DIR))
import app as pay153_app  # noqa: E402


def test_momo_protocol_requires_actual_trial_and_stops_after_stripe_init():
    with patch.object(pay153_app, "create_checkout", return_value={"data": {"checkout_session_id": "cs_live_momo", "publishable_key": "pk_live"}, "http": Mock()}) as create_checkout, \
         patch.object(pay153_app.sc, "init_checkout", return_value=({"currency": "vnd", "mode": "subscription", "subscription_data": {"trial_period_days": 30}, "payment_method_types": ["card", "momo"]}, "version", {"currency": "vnd", "payment_method_types": ["card", "momo"]})) as init_checkout:
        result, status = pay153_app.detect_momo({"token": "aaa.bbb.ccc", "proxy": "http://vn.proxy:8080"})
    assert status == 200
    assert result["momo"] is True
    assert result["actual_trial"] is True
    assert result["confirm_sent"] is False
    payload = create_checkout.call_args.args[1]
    assert payload["billing_details"] == {"country": "VN", "currency": "VND"}
    assert payload["subscription_data"]["trial_period_days"] == 30
    init_checkout.assert_called_once()


def test_momo_detector_reports_trial_and_method_without_confirm():
    runtime = MagicMock()
    runtime.detect_momo.return_value = ({"ok": True, "momo": True, "actual_trial": True}, 200)
    with patch.object(momo_service, "get_pay153_module", return_value=runtime):
        result = momo_service.check_momo("token", proxy="http://vn.proxy:8080")
    assert result["momo"] is True
    assert result["confirm_sent"] is False
    assert runtime.detect_momo.call_args.args[0]["proxy"] == "http://vn.proxy:8080"


def test_momo_retry_stops_on_definitive_no_method():
    calls = []

    def fake_check(token, proxy=None, trial_days=30):
        calls.append(proxy)
        return {"ok": True, "momo": False, "detection_outcome": "no_momo_in_stripe_init"}

    with patch.object(momo_service, "check_momo", side_effect=fake_check), \
         patch.object(momo_service.db, "mark_account_momo_running", return_value=True), \
         patch.object(momo_service.db, "update_account_momo", return_value=True):
        result = momo_service._run_with_proxy_retry(account_id=3, access_token="tok", proxies=["p1", "p2"])
    assert calls == ["p1"]
    assert result["momo"] is False
    assert result["attempt_count"] == 1


def test_momo_bulk_route_uses_vn_pool_only():
    client = create_app(auth_code="test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    with patch("core.detection_proxy.qualification_proxy_specs", return_value=["VN|http://vn.proxy:8080"]) as specs, \
         patch("core.detection_proxy.resolve_static_detection_proxy", return_value="http://vn.proxy:8080"), \
         patch("webui.app.db.get_account", return_value={"id": 1, "email": "a@test", "access_token": "token"}), \
         patch("webui.app.momo_service.get_executor", return_value=MagicMock()), \
         patch("webui.app.momo_service.enqueue", return_value={"accepted": True, "busy": False}) as enqueue:
        response = client.post("/api/accounts/check-momo-bulk", json={"account_ids": [1]})
    assert response.status_code == 202
    specs.assert_called_once_with("VN", "momo")
    assert enqueue.call_args.kwargs["proxies"] == ["http://vn.proxy:8080"]
    assert response.get_json()["confirm_sent"] is False


def test_momo_persistence_keeps_qualification_metadata():
    row = {"id": 8}
    with patch.object(db, "_load_accounts", return_value=[row]), patch.object(db, "_save_accounts"):
        assert db.update_account_momo(8, {"ok": True, "momo": True, "actual_trial": True, "payment_method_types": ["card", "momo"]})
    assert row["momo_eligible"] is True
    assert row["momo_actual_trial"] is True
    assert row["momo_payment_method_types"] == ["card", "momo"]
