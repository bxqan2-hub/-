import unittest
from pathlib import Path

from webui.app import create_app


ROOT = Path(__file__).resolve().parents[1]


class ExtractCenterCleanupTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_legacy_account_extraction_routes_are_removed(self):
        routes = {rule.rule for rule in self.app.url_map.iter_rules()}
        removed = {
            "/api/accounts/extract-link",
            "/api/accounts/extract-link-bulk",
            "/api/accounts/extract-link-bulk-at",
            "/api/accounts/check-gcash-eligibility",
            "/api/accounts/check-kakao-eligibility",
            "/api/accounts/resolve-access-tokens",
            "/api/accounts/extract-center-status",
            "/api/extract-link/cdk",
            "/api/extract-link/settings",
        }
        self.assertTrue(removed.isdisjoint(routes))

    def test_extract_center_contains_only_pay153_runtime(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("1537271403/pay153-checkout-link", html)
        self.assertNotIn("1537271403/paypal-agreement-protocol", html)
        self.assertIn("eatWhitePorridge/link-pp", html)
        self.assertIn("m1243808154/kakao_oaics_source", html)
        self.assertIn('src="/pay153/"', html)
        self.assertNotIn("/paypal-pay/", html)
        self.assertNotIn("PayPal Agreement Protocol", html)
        self.assertNotIn("PayPal 协议支付", html)
        self.assertNotIn('data-extract-src=', html)
        self.assertNotIn('>PayPal OAICS</button>', html)
        self.assertNotIn("btnExtractSelectedPixV2", html)
        self.assertNotIn("btnExtractSelectedPpV2", html)
        self.assertNotIn("btnExtractSelectedKkV2", html)
        self.assertNotIn("btnExtractSelectedGcashV2", html)
        self.assertIn("btnCheckSelectedCheckoutKindV2", html)
        self.assertIn("检测 OAICS/CSLIVE", html)

    def test_protocol_payment_routes_and_runtime_are_removed(self):
        routes = {rule.rule for rule in self.app.url_map.iter_rules()}
        for route in (
            "/paypal/", "/paypal/<path:subpath>",
            "/paypal-pay/", "/paypal-pay/<path:subpath>",
        ):
            self.assertNotIn(route, routes)

        pay153 = self.client.get("/pay153/")
        self.assertEqual(pay153.status_code, 200)
        pay153_html = pay153.get_data(as_text=True)
        self.assertNotIn('href="/paypal-pay/"', pay153_html)
        self.assertNotIn("PayPal 协议支付", pay153_html)
        self.assertEqual(self.client.get("/paypal-pay/").status_code, 404)
        self.assertFalse((ROOT / "integrations" / "paypal_agreement_protocol").exists())

        legacy_return = self.client.get("/checkout-link/", follow_redirects=False)
        self.assertEqual(legacy_return.status_code, 302)
        self.assertTrue(legacy_return.headers["Location"].endswith("/pay153/"))

    def test_pay153_remains_in_process_and_healthy(self):
        pay153 = self.client.get("/pay153/api/health")
        removed_protocol = self.client.get("/paypal-pay/api/health")
        health = self.client.get("/api/integrations/health")

        self.assertEqual(pay153.status_code, 200)
        self.assertIn("checkout_kind_v1", pay153.get_json()["capabilities"])
        self.assertEqual(removed_protocol.status_code, 404)
        services = health.get_json()["services"]
        self.assertEqual(set(services), {"pay153"})
        self.assertTrue(services["pay153"]["in_process"])
        self.assertTrue(services["pay153"]["healthy"])

    def test_pay153_keeps_main_authentication(self):
        anonymous = self.app.test_client()
        response = anonymous.get("/pay153/api/health")
        self.assertIn(response.status_code, {302, 401})
        if response.status_code == 302:
            self.assertIn("/login", response.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main()
