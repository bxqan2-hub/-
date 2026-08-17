# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path


INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integrations" / "paypal_agreement_protocol"
sys.path.insert(0, str(INTEGRATION_DIR))

import web as paypal_web  # noqa: E402
from paypal.proxy import ProxyEntry  # noqa: E402


class PayPalProtocolProxyFormatTests(unittest.TestCase):
    def test_reference_pool_preserves_raw_1024proxy_lines(self):
        samples = [
            "proxy.test:3010:user-region-BR-sid-one:password",
            "http://user-region-BR-sid-two:password@proxy.test:3010",
        ]

        parsed = paypal_web.parse_proxy_pool(samples)

        self.assertEqual(parsed, samples)
        self.assertTrue(all(ProxyEntry.parse(item).scheme == "http" for item in parsed))

    def test_reference_parser_rejects_non_reference_shorthand(self):
        with self.assertRaisesRegex(ValueError, "host:port:username:password"):
            paypal_web.parse_proxy_pool(["user:password@proxy.test:3010"])

    def test_reference_parser_preserves_explicit_socks5_url(self):
        raw = "socks5://user-region-TH-sid-one:password@proxy.test:3010"

        parsed = paypal_web.parse_proxy_pool([raw])

        self.assertEqual(parsed, [raw])
        self.assertEqual(ProxyEntry.parse(parsed[0]).scheme, "socks5")

    def test_1024proxy_four_part_line_uses_remote_dns_socks_protocol(self):
        raw = "us.1024proxy.io:3000:user-region-TH-sid-one:password"

        parsed = paypal_web.parse_proxy_pool([raw])

        self.assertEqual(parsed, [raw])
        self.assertEqual(ProxyEntry.parse(parsed[0]).scheme, "socks5h")

    def test_generic_four_part_line_keeps_upstream_http_protocol(self):
        raw = "proxy.test:3000:user-region-TH-sid-one:password"

        self.assertEqual(ProxyEntry.parse(raw).scheme, "http")

    def test_protocol_ui_submits_proxy_fields_without_rewriting(self):
        app_js = (INTEGRATION_DIR / "web_static" / "app.js").read_text(encoding="utf-8")
        index_html = (INTEGRATION_DIR / "web_static" / "index.html").read_text(encoding="utf-8")

        self.assertNotIn("normalizeProxyText", app_js)
        self.assertNotIn("normalizeProxyField", app_js)
        self.assertIn("return $('proxies').value.split", app_js)
        self.assertIn("app.js?v=20260814-reference-proxy-1", index_html)


if __name__ == "__main__":
    unittest.main()
