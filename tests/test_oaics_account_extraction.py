# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, oaics_extract_service
from webui.app import create_app


class _ImmediateExecutor:
    def submit(self, function, *args):
        function(*args)


class _FakePay153:
    def __init__(self):
        self.calls = []

    @staticmethod
    def normalize_paypal_oaics_proxies(values):
        return [f"normalized:{value}" for value in values]

    def run_paypal_oaics(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "paypal_link": "https://www.paypal.com/agreements/approve?ba_token=BA-TEST",
            "checkout_session_id": "oaics_test",
        }


class OaicsAccountExtractionTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_configured_proxy_pool_uses_selected_profile(self):
        module = _FakePay153()
        with (
            patch.object(oaics_extract_service.proxy_cfg, "PAYPAL_OAICS_PROXY_PROFILES", [
                "BR|socks5://user:pass@br.example:3010",
                "US|http://user:pass@us.example:3010",
            ]),
            patch.object(oaics_extract_service.proxy_cfg, "PAYPAL_OAICS_PROXY_ACTIVE", "BR"),
            patch.object(oaics_extract_service, "get_pay153_module", return_value=module),
        ):
            self.assertEqual(
                oaics_extract_service.configured_proxy_pool(),
                ["normalized:socks5://user:pass@br.example:3010"],
            )

    def test_enqueue_calls_paypal_oaics_core(self):
        module = _FakePay153()
        with (
            patch.object(oaics_extract_service, "configured_proxy_pool", return_value=["proxy-a", "proxy-b"]),
            patch.object(oaics_extract_service, "get_pay153_module", return_value=module),
            patch.object(oaics_extract_service.db, "claim_account_oaics_extract", return_value=True),
            patch.object(oaics_extract_service.db, "mark_account_oaics_extract_running", return_value=True),
            patch.object(oaics_extract_service.db, "update_account_oaics_extract") as update_result,
            patch.object(oaics_extract_service.db, "update_account_oaics_extract_progress") as update_progress,
        ):
            queued = oaics_extract_service.enqueue(
                account_id=7,
                access_token="at-test",
                executor=_ImmediateExecutor(),
            )

        self.assertTrue(queued["accepted"])
        self.assertEqual(module.calls[0]["access_token"], "at-test")
        self.assertEqual(module.calls[0]["proxies"], ["proxy-b", "proxy-a"])
        update_result.assert_called_once()
        self.assertTrue(update_result.call_args.args[1]["ok"])
        update_progress.assert_called_once_with(7, "代理已解析", "已加载 2 条 PayPal OAICS 专用代理")

    def test_core_stage_log_is_persisted(self):
        module = _FakePay153()
        module.run_paypal_oaics = lambda **kwargs: (kwargs["log"]("[checkout] 生成 Checkout 1/5"), {
            "paypal_link": "https://www.paypal.com/agreements/approve?ba_token=BA-TEST"
        })[1]
        with (
            patch.object(oaics_extract_service, "configured_proxy_pool", return_value=["proxy-a"]),
            patch.object(oaics_extract_service, "get_pay153_module", return_value=module),
            patch.object(oaics_extract_service.db, "claim_account_oaics_extract", return_value=True),
            patch.object(oaics_extract_service.db, "mark_account_oaics_extract_running", return_value=True),
            patch.object(oaics_extract_service.db, "update_account_oaics_extract"),
            patch.object(oaics_extract_service.db, "update_account_oaics_extract_progress") as progress,
        ):
            oaics_extract_service.enqueue(account_id=7, access_token="at-test", executor=_ImmediateExecutor())
        self.assertEqual(progress.call_args_list[0].args, (7, "代理已解析", "已加载 1 条 PayPal OAICS 专用代理"))
        self.assertEqual(progress.call_args_list[1].args, (7, "生成 Checkout 1/5", "生成 Checkout 1/5"))

    def test_success_persists_link_and_moves_account_to_oaics_group(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.joinpath("accounts.json").write_text(json.dumps([
                {"id": 1, "email": "oaics@example.com", "access_token": "at-test"},
            ]), encoding="utf-8")
            with (
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_ACCOUNT_GROUPS_JSON", root / "groups.json"),
            ):
                self.assertTrue(db.update_account_oaics_extract(1, {
                    "ok": True,
                    "paypal_link": "https://www.paypal.com/agreements/approve?ba_token=BA-TEST",
                    "checkout_session_id": "oaics_test",
                }))

                account = db.get_account(1)
                groups = db.list_account_groups()

        self.assertEqual(account["oaics_extract_status"], "success")
        self.assertIn("BA-TEST", account["oaics_link"])
        self.assertEqual(groups[0]["name"], "OAICS账号")
        self.assertEqual(groups[0]["account_ids"], [1])

    def test_bulk_route_only_enqueues_confirmed_oaics_accounts(self):
        accounts = {
            1: {"id": 1, "email": "oaics@example.com", "access_token": "at-1", "checkout_kind": "oaics"},
            2: {"id": 2, "email": "live@example.com", "access_token": "at-2", "checkout_kind": "cs_live"},
        }
        with (
            patch("webui.app.db.get_account", side_effect=lambda account_id: accounts.get(account_id)),
            patch("webui.app.oaics_extract_service.configured_proxy_pool", return_value=["proxy-a"]),
            patch("webui.app.oaics_extract_service.configured_worker_count", return_value=4),
            patch("webui.app.oaics_extract_service.get_executor", return_value=_ImmediateExecutor()),
            patch("webui.app.oaics_extract_service.enqueue", return_value={"accepted": True, "busy": False}) as enqueue,
        ):
            response = self.client.post("/api/accounts/extract-oaics-bulk", json={"account_ids": [1, 2]})

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["workers"], 4)
        self.assertEqual(payload["started_count"], 1)
        self.assertEqual(payload["skipped_count"], 1)
        enqueue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
