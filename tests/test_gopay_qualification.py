# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from core import db, detection_proxy, gopay_service
from webui.app import create_app


INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integrations" / "pay153_checkout"
if str(INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_DIR))
import app as pay153_app  # noqa: E402


class GoPayProtocolBoundaryTests(unittest.TestCase):
    @patch.object(pay153_app.sc, "init_checkout")
    @patch.object(pay153_app, "create_checkout")
    def test_eligible_stops_after_checkout_and_single_stripe_init(self, create_checkout, init_checkout):
        create_checkout.return_value = {
            "data": {
                "checkout_session_id": "cs_live_example",
                "publishable_key": "pk_live_example",
            },
            "http": Mock(),
        }
        init_checkout.return_value = (
            {"currency": "idr", "payment_method_types": ["card", "gopay"]},
            "version",
            {"checkout_amount": 0, "currency": "idr", "payment_method_types": ["card", "gopay"]},
        )

        result, status = pay153_app.detect_gopay({
            "token": "aaa.bbb.ccc",
            "proxy": "http://proxy.example:8080",
        })

        self.assertEqual(status, 200)
        self.assertTrue(result["gopay"])
        self.assertFalse(result["confirm_sent"])
        payload = create_checkout.call_args.args[1]
        self.assertEqual(payload["billing_details"], {"country": "ID", "currency": "IDR"})
        self.assertEqual(payload["promo_campaign"]["promo_campaign_id"], "plus-1-month-free")
        init_checkout.assert_called_once()

    @patch.object(pay153_app.sc, "init_checkout")
    @patch.object(pay153_app, "create_checkout")
    def test_oaics_checkout_is_definitively_not_eligible_without_stripe_init(self, create_checkout, init_checkout):
        create_checkout.return_value = {
            "data": {"checkout_session_id": "oaics_example", "checkout_provider": "open_ai"},
            "http": Mock(),
        }

        result, status = pay153_app.detect_gopay({"token": "aaa.bbb.ccc"})

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertFalse(result["gopay"])
        self.assertEqual(result["detection_outcome"], "unsupported_custom_checkout")
        init_checkout.assert_not_called()


class GoPayServiceTests(unittest.TestCase):
    def test_http_proxy_transport_falls_back_to_https(self):
        runtime = MagicMock()
        runtime.detect_gopay.side_effect = [
            ({"ok": False, "gopay": False, "error": "CONNECT tunnel failed"}, 502),
            ({"ok": True, "gopay": True}, 200),
        ]
        with patch.object(gopay_service, "get_pay153_module", return_value=runtime):
            result = gopay_service.check_gopay("token", proxy="http://user:pass@proxy.example:8080")
        self.assertTrue(result["gopay"])
        self.assertEqual(result["proxy_transport"], "https")
        self.assertEqual(runtime.detect_gopay.call_count, 2)

    def test_persistence_does_not_touch_gcash_fields(self):
        row = {"id": 9, "gcash_status": "success", "gcash_eligible": True}
        with patch.object(db, "_load_accounts", return_value=[row]), patch.object(db, "_save_accounts"):
            self.assertTrue(db.update_account_gopay(9, {
                "ok": True, "gopay": True, "checkout_country": "ID", "checkout_currency": "IDR",
            }))
        self.assertTrue(row["gopay_eligible"])
        self.assertTrue(row["gcash_eligible"])

    def test_global_stop_marks_running_gopay_as_stopped_failure(self):
        row = {"id": 3, "gopay_status": "running", "gopay_eligible": True}
        with patch.object(db, "_load_accounts", return_value=[row]), patch.object(db, "_save_accounts"):
            result = db.stop_account_page_operations("用户停止")
        self.assertEqual(result["gopay_status"], 1)
        self.assertEqual(row["gopay_status"], "failed")
        self.assertFalse(row["gopay_eligible"])
        self.assertEqual(row["gopay_error"], "用户停止")


class GoPayBulkApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_bulk_route_uses_only_id_country_pool(self):
        with patch.object(detection_proxy, "qualification_proxy_specs", return_value=["ID|http://id.proxy:8080"]) as specs, \
             patch.object(detection_proxy, "resolve_static_detection_proxy", return_value="http://id.proxy:8080"), \
             patch("webui.app.db.get_account", return_value={"id": 1, "email": "a@test", "access_token": "token"}), \
             patch("webui.app.gopay_service.get_executor", return_value=MagicMock()), \
             patch("webui.app.gopay_service.enqueue", return_value={"accepted": True, "busy": False}) as enqueue:
            response = self.client.post("/api/accounts/check-gopay-bulk", json={"account_ids": [1]})
        self.assertEqual(response.status_code, 202)
        specs.assert_called_once_with("ID")
        self.assertEqual(enqueue.call_args.kwargs["proxies"], ["http://id.proxy:8080"])
        self.assertFalse(response.get_json()["confirm_sent"])

    def test_settings_import_accepts_ph_and_id_in_shared_qualification_pool(self):
        inspections = [
            {"country": "PH", "country_label": "菲律宾", "country_source": "proxy_region_tag", "proxy": "http://ph.proxy:8080", "masked_proxy": "ph", "exit_ip": "", "region": "", "city": ""},
            {"country": "ID", "country_label": "印度尼西亚", "country_source": "proxy_region_tag", "proxy": "http://id.proxy:8080", "masked_proxy": "id", "exit_ip": "", "region": "", "city": ""},
        ]
        with patch("core.detection_proxy.inspect_static_proxy", side_effect=inspections), \
             patch("core.detection_proxy.resolve_static_detection_proxy", side_effect=lambda value: value.split("|", 1)[-1]), \
             patch("webui.app.config_editor.update_config", return_value={"updated": ["QUALIFICATION_CHECK_PROXY_PROFILES", "QUALIFICATION_CHECK_PROXY_ACTIVE"], "ignored": []}) as update, \
             patch("config.proxy.QUALIFICATION_CHECK_PROXY_PROFILES", []), \
             patch("config.proxy.QUALIFICATION_CHECK_PROXY_ACTIVE", ""):
            response = self.client.post("/api/detection-proxy-pools/import", json={
                "purpose": "qualification",
                "proxies": ["http://ph.proxy:8080", "http://id.proxy:8080"],
            })
        self.assertEqual(response.status_code, 200)
        saved = update.call_args.args[0]["QUALIFICATION_CHECK_PROXY_PROFILES"]
        self.assertEqual(set(saved), {"PH|http://ph.proxy:8080", "ID|http://id.proxy:8080"})


if __name__ == "__main__":
    unittest.main()
