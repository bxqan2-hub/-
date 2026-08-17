import unittest

from webui.app import create_app


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

    def test_extract_center_keeps_paypal_protocol_out_of_extract_modes(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("1537271403/pay153-checkout-link", html)
        self.assertIn("1537271403/paypal-agreement-protocol", html)
        self.assertIn("eatWhitePorridge/link-pp", html)
        self.assertIn("m1243808154/kakao_oaics_source", html)
        self.assertIn('src="/pay153/"', html)
        self.assertNotIn('data-extract-src="/paypal-pay/"', html)
        self.assertNotIn('data-extract-src=', html)
        self.assertNotIn('>PayPal OAICS</button>', html)
        self.assertNotIn("btnExtractSelectedPixV2", html)
        self.assertNotIn("btnExtractSelectedPpV2", html)
        self.assertNotIn("btnExtractSelectedKkV2", html)
        self.assertNotIn("btnExtractSelectedGcashV2", html)
        self.assertIn("btnCheckSelectedCheckoutKindV2", html)
        self.assertIn("检测 OAICS/CSLIVE", html)

    def test_independent_paypal_workbench_is_removed(self):
        routes = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertNotIn("/paypal/", routes)
        self.assertNotIn("/paypal/<path:subpath>", routes)

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn('data-tab="paypal"', html)
        self.assertNotIn('id="tab-paypal"', html)
        self.assertNotIn('src="/paypal/"', html)

    def test_original_protocol_entry_targets_new_oaics_console(self):
        response = self.client.get("/pay153/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('href="/paypal-pay/"', html)
        self.assertIn("PayPal 协议支付", html)

        protocol = self.client.get("/paypal-pay/")
        self.assertEqual(protocol.status_code, 200)
        protocol_html = protocol.get_data(as_text=True)
        self.assertIn("PayPal OAICS 协议支付", protocol_html)
        self.assertIn('href="/pay153/"', protocol_html)
        self.assertNotIn('href="/checkout-link/"', protocol_html)

        legacy_return = self.client.get("/checkout-link/", follow_redirects=False)
        self.assertEqual(legacy_return.status_code, 302)
        self.assertTrue(legacy_return.headers["Location"].endswith("/pay153/"))

    def test_bundled_apps_share_main_webui_routes(self):
        pay153 = self.client.get("/pay153/api/health")
        paypal = self.client.get("/paypal-pay/api/health")
        health = self.client.get("/api/integrations/health")

        self.assertEqual(pay153.status_code, 200)
        self.assertIn("checkout_kind_v1", pay153.get_json()["capabilities"])
        self.assertEqual(paypal.status_code, 200)
        self.assertTrue(paypal.get_json()["ok"])
        services = health.get_json()["services"]
        self.assertTrue(services["pay153"]["in_process"])
        self.assertTrue(services["paypal-agreement"]["in_process"])
        self.assertEqual(services["pay153"]["port"], services["paypal-agreement"]["port"])

    def test_bundled_apps_keep_main_authentication(self):
        anonymous = self.app.test_client()
        for path in ("/pay153/api/health", "/paypal-pay/api/health"):
            response = anonymous.get(path)
            self.assertIn(response.status_code, {302, 401})
            if response.status_code == 302:
                self.assertIn("/login", response.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main()
