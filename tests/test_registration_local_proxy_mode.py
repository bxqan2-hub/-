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


if __name__ == "__main__":
    unittest.main()
