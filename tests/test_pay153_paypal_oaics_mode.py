import sys
import unittest
from pathlib import Path
from unittest.mock import patch


INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integrations" / "pay153_checkout"
sys.path.insert(0, str(INTEGRATION_DIR))

import app as pay153_app  # noqa: E402


class PayPalOaicsModeTests(unittest.TestCase):
    def setUp(self):
        self.client = pay153_app.app.test_client()

    @patch.object(pay153_app.STORE, "create")
    @patch.object(pay153_app.STORE, "queue_position", return_value=1)
    def test_paypal_oaics_mode_uses_single_proxy_pool(self, _queue_position, create):
        create.return_value = "oaics-job"
        response = self.client.post(
            "/api/checkout",
            json={
                "token": "aaa.bbb.ccc",
                "plan": "plus",
                "link_type": "paypal_oaics",
                "entry_proxies": ["127.0.0.1:18080"],
            },
        )

        self.assertEqual(response.status_code, 202)
        options = create.call_args.args[0]
        self.assertEqual(options["link_type"], "paypal_oaics")
        self.assertEqual(options["entry_proxies"], options["exit_proxies"])
        self.assertEqual(options["entry_proxies"], ["socks5h://127.0.0.1:18080"])
        self.assertEqual(options["provider_attempts"], 10)
        self.assertEqual(options["proxy_country"], "BR")
        self.assertEqual(options["billing_country"], "DE")
        self.assertEqual(options["checkout_country"], "DE")
        self.assertEqual(options["checkout_currency"], "EUR")
        self.assertTrue(options["use_promo"])
        self.assertEqual(options["promo_campaign"], "plus-1-month-free")

    @patch.object(pay153_app.STORE, "create", return_value="oaics-country-job")
    @patch.object(pay153_app.STORE, "queue_position", return_value=1)
    def test_paypal_oaics_accepts_independent_proxy_and_billing_countries(
        self, _queue_position, create
    ):
        response = self.client.post(
            "/api/checkout",
            json={
                "token": "aaa.bbb.ccc",
                "plan": "plus",
                "link_type": "paypal_oaics",
                "proxy_country": "JP",
                "billing_country": "DE",
                "entry_proxies": ["127.0.0.1:18080"],
            },
        )

        self.assertEqual(response.status_code, 202)
        options = create.call_args.args[0]
        self.assertEqual(options["proxy_country"], "JP")
        self.assertEqual(options["billing_country"], "DE")
        self.assertEqual(options["country"], "JP")
        self.assertEqual(options["checkout_country"], "DE")
        self.assertEqual(options["checkout_currency"], "EUR")

    def test_paypal_oaics_rejects_unknown_country_before_queueing(self):
        response = self.client.post(
            "/api/checkout",
            json={
                "token": "aaa.bbb.ccc",
                "plan": "plus",
                "link_type": "paypal_oaics",
                "proxy_country": "XX",
                "billing_country": "DE",
                "entry_proxies": ["127.0.0.1:18080"],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("不支持的国家代码", response.get_json()["error"])

    def test_paypal_oaics_rejects_non_plus_plans(self):
        response = self.client.post(
            "/api/checkout",
            json={
                "token": "aaa.bbb.ccc",
                "plan": "pro",
                "link_type": "paypal_oaics",
                "entry_proxies": ["127.0.0.1:18080"],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("仅支持 Plus", response.get_json()["error"])

    @patch.object(pay153_app.STORE, "queue_position", return_value=1)
    @patch.object(pay153_app.STORE, "create", return_value="paypal-job")
    def test_public_boolean_cannot_silently_change_normal_paypal_mode(self, create, _queue_position):
        response = self.client.post(
            "/api/checkout",
            json={
                "token": "aaa.bbb.ccc",
                "plan": "plus",
                "link_type": "paypal",
                "oaics_paypal": True,
                "entry_proxies": ["127.0.0.1:18080"],
                "exit_proxies": ["127.0.0.1:18081"],
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertNotIn("oaics_paypal", create.call_args.args[0])

    def test_public_page_uses_one_run_mode_select(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        response.close()
        self.assertIn('id="runMode" name="link_type"', html)
        self.assertIn('<option value="paypal_oaics">PayPal OAICS</option>', html)
        self.assertNotIn('id="railGrid"', html)
        self.assertNotIn('class="rail', html)
        self.assertIn('id="paypalOaicsProxyCheck"', html)
        self.assertIn('id="paypalOaicsManualToggle"', html)
        self.assertIn('id="paypalOaicsProxyCountry"', html)
        self.assertIn('id="paypalOaicsBillingCountry"', html)

    def test_proxy_check_reports_format_error_with_no_network_probe(self):
        response = self.client.post(
            "/api/paypal-oaics/proxy-check",
            json={"proxies": ["not-a-proxy"]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["kind"], "format")

    @patch.object(pay153_app, "_cached_paypal_oaics_proxy_probe")
    def test_proxy_check_normalizes_and_summarizes_br_connectivity(self, probe):
        probe.return_value = {
            "reachable": True,
            "country": "BR",
            "br_compatible": True,
            "source": "ipinfo",
            "exit_id": "exit#test",
        }
        response = self.client.post(
            "/api/paypal-oaics/proxy-check",
            json={"proxies": ["127.0.0.1:18080"]},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["checked"], 1)
        self.assertEqual(payload["reachable"], 1)
        self.assertEqual(payload["br_compatible"], 1)
        self.assertEqual(payload["expected_country"], "BR")
        self.assertEqual(payload["country_compatible"], 1)
        probe.assert_called_once_with("socks5h://127.0.0.1:18080")

    @patch.object(pay153_app, "_cached_paypal_oaics_proxy_probe")
    def test_proxy_check_matches_selected_non_br_country(self, probe):
        probe.return_value = {
            "reachable": True,
            "country": "JP",
            "br_compatible": False,
            "source": "ipinfo",
            "exit_id": "exit#test",
        }
        response = self.client.post(
            "/api/paypal-oaics/proxy-check",
            json={"proxies": ["127.0.0.1:18080"], "expected_country": "JP"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["expected_country"], "JP")
        self.assertEqual(payload["country_compatible"], 1)
        self.assertEqual(payload["br_compatible"], 0)

    def test_proxy_formats_use_remote_dns_and_support_reversed_credentials(self):
        samples = [
            "proxy.test:3010:user-region-BR-sid-one:password",
            "socks5://user-region-BR-sid-two:password@proxy.test:3010",
            "user-region-BR-sid-three:password@proxy.test:3010",
            "proxy.test:3010@user-region-BR-sid-four:password",
        ]

        normalized = pay153_app.normalize_paypal_oaics_proxies(samples)

        self.assertEqual(normalized, [
            "socks5h://user-region-BR-sid-one:password@proxy.test:3010",
            "socks5h://user-region-BR-sid-two:password@proxy.test:3010",
            "socks5h://user-region-BR-sid-three:password@proxy.test:3010",
            "socks5h://user-region-BR-sid-four:password@proxy.test:3010",
        ])

    def test_http_proxy_scheme_is_preserved(self):
        normalized = pay153_app.normalize_paypal_oaics_proxies(
            ["http://user:password@proxy.test:3010"]
        )

        self.assertEqual(normalized, ["http://user:password@proxy.test:3010"])

    @patch.object(pay153_app, "run_paypal_oaics")
    def test_worker_maps_link_pp_result_into_existing_job(self, runner):
        runner.return_value = {
            "link_type": "paypal_oaics",
            "paypal_approve_url": "https://www.paypal.com/agreements/approve?ba_token=BA-TEST",
            "provider_redirect_url": "https://pm-redirects.stripe.com/authorize/test",
        }
        store = pay153_app.JobStore.__new__(pay153_app.JobStore)
        store.lock = pay153_app.threading.RLock()
        store.file_lock = pay153_app.threading.RLock()
        store.jobs = {"job": {"status": "running", "logs": [], "cancel": False}}
        recorded = []
        store._record_success = lambda _job_id, result: recorded.append(dict(result))
        store._append_backend_log = lambda *_args: None
        store._run_paypal_oaics(
            "job",
            {
                "token_raw": "aaa.bbb.ccc",
                "entry_proxies": ["127.0.0.1:18080"],
                "retry_count": 5,
                "provider_attempts": 10,
                "proxy_country": "JP",
                "billing_country": "DE",
                "checkout_currency": "EUR",
            },
        )
        self.assertEqual(store.jobs["job"]["status"], "done")
        self.assertEqual(store.jobs["job"]["result"]["link_type"], "paypal_oaics")
        expected = "https://www.paypal.com/agreements/approve?ba_token=BA-TEST"
        self.assertEqual(store.jobs["job"]["result"]["url"], expected)
        self.assertEqual(store.jobs["job"]["result"]["paypal_link"], expected)
        self.assertEqual(recorded[0]["url"], expected)
        self.assertEqual(runner.call_args.kwargs["proxy_country"], "JP")
        self.assertEqual(runner.call_args.kwargs["billing_country"], "DE")

    def test_extract_center_prefers_paypal_approval_and_remembers_form(self):
        script = (pay153_app.ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("result.paypal_approve_url || result.paypal_link", script)
        self.assertIn("pay153.checkout.form.v1", script)
        self.assertIn("restoreFormState();", script)
        self.assertIn("pay153.checkout.job.v1", script)
        self.assertIn("sessionStorage.setItem(JOB_STATE_KEY, jobId);", script)
        self.assertIn("body.proxy_country = oaicsCountries.proxy;", script)
        self.assertIn("body.billing_country = oaicsCountries.billing;", script)


if __name__ == "__main__":
    unittest.main()
