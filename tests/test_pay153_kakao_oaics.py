import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integrations" / "pay153_checkout"
sys.path.insert(0, str(INTEGRATION_DIR))

import kakao_oaics  # noqa: E402
import app as pay153_app  # noqa: E402


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


class KakaoOaicsTests(unittest.TestCase):
    def test_checkout_classifier_matches_upstream_session_boundary(self):
        self.assertEqual(pay153_app.classify_checkout_kind({
            "checkout_session_id": "oaics_abc", "checkout_provider": "open_ai",
        }), "oaics")
        self.assertEqual(pay153_app.classify_checkout_kind({
            "checkout_session_id": "cs_live_abc", "checkout_provider": "stripe",
        }), "cs_live")
        self.assertEqual(pay153_app.classify_checkout_kind({
            "checkout_session_id": "cs_test_abc", "checkout_provider": "stripe",
        }), "unknown")

    @patch.object(pay153_app, "fetch_dynamic_attempt_proxy")
    @patch.object(pay153_app, "create_checkout")
    def test_detection_endpoint_is_generic_minimal_and_stops_after_create(
        self, create_checkout, fetch_dynamic_attempt_proxy,
    ):
        create_checkout.return_value = {
            "data": {
                "checkout_session_id": "oaics_abc",
                "checkout_provider": "open_ai",
                "processor_entity": "openai_ie",
            },
            "http": Mock(),
        }

        response = pay153_app.app.test_client().post(
            "/api/checkout-kind",
            json={"token": "aaa.bbb.ccc", "proxy": "http://127.0.0.1:8080"},
        )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["kind"], "oaics")
        self.assertEqual(result["session_prefix"], "oaics_")
        self.assertFalse(result["confirm_sent"])
        self.assertFalse(result["promo_attached"])
        self.assertFalse(result["promo_update_sent"])
        payload = create_checkout.call_args.args[1]
        self.assertEqual(payload["billing_details"], {"country": "DE", "currency": "EUR"})
        self.assertEqual(payload["checkout_ui_mode"], "custom")
        self.assertNotIn("promo_campaign", payload)
        fetch_dynamic_attempt_proxy.assert_not_called()

    @patch.object(pay153_app, "fetch_dynamic_attempt_proxy")
    @patch.object(pay153_app, "create_checkout")
    def test_detection_allows_direct_checkout_without_kr_proxy(
        self, create_checkout, fetch_dynamic_attempt_proxy,
    ):
        create_checkout.return_value = {
            "data": {
                "checkout_session_id": "cs_live_abc",
                "checkout_provider": "stripe",
            },
            "http": Mock(),
        }

        response = pay153_app.app.test_client().post(
            "/api/checkout-kind", json={"token": "aaa.bbb.ccc"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["kind"], "cs_live")
        self.assertEqual(response.get_json()["proxy_source"], "direct")
        self.assertEqual(create_checkout.call_args.args[2], "")
        fetch_dynamic_attempt_proxy.assert_not_called()

    def _run(self, confirm_payload, *, amount=0):
        chatgpt = Mock()
        chatgpt.post.return_value = _Response(200, confirm_payload)
        self.last_chatgpt = chatgpt
        elements = {
            "session_id": "elements_123",
            "config_id": "config_123",
            "stripe_js_id": "js_123",
            "payment_method_specs": [{"type": "kakao_pay"}],
        }
        snapshot = {
            "publishable_key": "pk_live_test",
            "total_summary": {"due": amount},
            "payment_method_types": ["card", "kakao_pay"],
        }
        patches = (
            patch.object(kakao_oaics, "_bootstrap", return_value=(snapshot, {}, "live-at")),
            patch.object(kakao_oaics, "_elements_session", return_value=elements),
            patch.object(kakao_oaics, "_runtime_version", return_value=("c00af4ce81", "test")),
            patch.object(kakao_oaics, "_confirmation_token", return_value="ctoken_once"),
        )
        with patches[0] as bootstrap, patches[1] as create_elements, patches[2], patches[3]:
            self.last_create_elements = create_elements
            result = kakao_oaics.run_kakao_oaics(
                chatgpt_http=chatgpt,
                stripe_http=Mock(),
                token="at",
                session_id="oaics_test",
                processor="openai_ie",
                device_id="did",
                initial_checkout=snapshot,
                billing={"email": "u@example.com", "address": {"country": "KR"}},
                sentinel_headers={},
                log=lambda _message: None,
                update_taxes=lambda *_args: {"total_summary": {"due": amount}},
            )
        return result, chatgpt, bootstrap, create_elements

    def test_direct_nicepay_redirect_completes(self):
        result, chatgpt, _bootstrap, _elements = self._run({
            "status": "requires_action",
            "next_action": {"redirect_to_url": {"url": "https://pay.nicepay.co.kr/start/abc"}},
        })

        self.assertEqual(result["redirect_url"], "https://pay.nicepay.co.kr/start/abc")
        self.assertEqual(result["state_history"][-1], "complete")
        sent = chatgpt.post.call_args.kwargs["json"]
        self.assertEqual(sent["confirm_token"], "ctoken_once")
        self.assertEqual(sent["selected_payment_method_type"], "kakao_pay")

    def test_blocked_confirm_marks_one_shot_boundary(self):
        with self.assertRaises(kakao_oaics.KakaoOaicsError) as caught:
            self._run({"status": "blocked", "error": "risk blocked"})

        self.assertTrue(caught.exception.confirm_sent)
        self.assertEqual(caught.exception.state, "chatgpt_confirm")

    def test_nonzero_checkout_stops_before_stripe_or_confirm(self):
        with self.assertRaises(kakao_oaics.KakaoOaicsError) as caught:
            self._run({}, amount=1999)

        self.assertFalse(caught.exception.confirm_sent)
        self.assertEqual(caught.exception.state, "amount_check")
        self.last_create_elements.assert_not_called()
        self.last_chatgpt.post.assert_not_called()

    def test_intent_reuses_same_confirmation_token(self):
        confirm_payload = {
            "status": "requires_confirmation",
            "setup_intent": {"client_secret": "seti_123_secret_abc"},
        }
        intent = {
            "next_action": {"redirect_to_url": {"url": "https://pm-redirects.stripe.com/kakao/abc"}}
        }
        with patch.object(kakao_oaics, "_confirm_intent", return_value=intent) as confirm_intent:
            result, _chatgpt, _bootstrap, _elements = self._run(confirm_payload)

        self.assertEqual(result["redirect_url"], "https://pm-redirects.stripe.com/kakao/abc")
        self.assertEqual(confirm_intent.call_args.kwargs["confirmation_token"], "ctoken_once")

    def test_redirect_allowlist_rejects_unrelated_host(self):
        self.assertEqual(
            kakao_oaics.extract_kakao_redirect_url({"url": "https://evil.example/steal"}),
            "",
        )


if __name__ == "__main__":
    unittest.main()
