# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

import main


class RegistrationLocalProxyModeTests(unittest.TestCase):
    @patch("core.roxy_registration.run_roxy_registration", return_value={"success": True})
    def test_local_proxy_mode_skips_roxy_preflight(self, run_roxy_registration):
        with patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "roxy"):
            result = main.run_registration(
                email="local@example.com",
                name="Local User",
                birthday="1991-02-03",
                proxy="http://127.0.0.1:10808",
                proxy_mode="local",
            )

        self.assertEqual(result, {"success": True})
        run_roxy_registration.assert_called_once_with(
            email="local@example.com",
            name="Local User",
            birthday="1991-02-03",
            proxy="http://127.0.0.1:10808",
            otp_code=None,
            batch_dir=None,
            skip_proxy_preflight=True,
        )

    @patch("main.BrowserSession")
    def test_password_mfa_blocks_protocol_before_remote_registration(self, session_cls):
        with patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "protocol"), \
             patch.object(main._twofa_cfg, "ENABLE_2FA", True), \
             patch.object(main._email_cfg, "USE_EMAIL_SERVICE", True):
            result = main.run_registration(
                email="blocked@example.com",
                name="Blocked User",
                birthday="1991-02-03",
            )

        self.assertFalse(result["success"])
        self.assertFalse(result["security_ok"])
        self.assertIn("protocol", result["error"])
        session_cls.assert_not_called()

    @patch("core.roxy_registration.run_roxy_registration")
    def test_password_mfa_blocks_manual_email_before_browser_registration(self, run_roxy_registration):
        with patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "roxy"), \
             patch.object(main._twofa_cfg, "ENABLE_2FA", True), \
             patch.object(main._email_cfg, "USE_EMAIL_SERVICE", False):
            result = main.run_registration(
                email="manual@example.com",
                name="Manual User",
                birthday="1991-02-03",
            )

        self.assertFalse(result["success"])
        self.assertIn("第二次重认证验证码", result["error"])
        run_roxy_registration.assert_not_called()


if __name__ == "__main__":
    unittest.main()
