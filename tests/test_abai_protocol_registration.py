from unittest import TestCase
from unittest.mock import patch

from core.abai_protocol_registration import run_abai_protocol_registration


class AbaiProtocolRegistrationTests(TestCase):
    def setUp(self):
        factory = patch("core.abai_protocol_registration.LocalBrowserSession")
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.transport = self.factory.return_value.__enter__.return_value
        self.transport.stage = 'protocol_request'
        self.transport.http_status = None
        self.transport.profile = {"name": "local-fixture", "user_agent": "Chrome/153.0.0.0", "core_version": "153.0.0.0"}

    @patch("core.abai_protocol_registration.trigger_flow", return_value={"status": "skipped", "ok": False})
    @patch("core.abai_protocol_registration.save_account_data", return_value=42)
    @patch("core.abai_protocol_registration.persist_confirmed_registration_password", return_value=True)
    @patch("core.abais_protocol.protocol_register.ChatGPTProtocolRegister")
    @patch("core.abai_protocol_registration.registration_password", return_value="Abcd1234!xyz")
    @patch("core.abai_protocol_registration._pick_protocol_proxy", return_value="http://proxy.test:8080")
    def test_calls_copied_flow_and_maps_password_totp_result(
        self,
        _pick_proxy,
        _password,
        worker_cls,
        persist_password,
        save_account,
        trigger_flow,
    ):
        worker_cls.return_value.run.return_value = {
            "access_token": "at-test",
            "refresh_token": "rt-test",
            "account_id": "acct-test",
            "workspace_id": "ws-test",
            "profile": {"id": "user-test"},
            "cookies": {"session": "cookie"},
            "totp_2fa": {"bound": True, "secret": "SECRET"},
            "password_registered": True,
        }

        result = run_abai_protocol_registration(
            email="user@example.com",
            name="User Example",
            birthday="1990-01-01",
            otp_code="123456",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["account_id"], 42)
        worker_cls.return_value.run.assert_called_once_with(
            email="user@example.com", password="Abcd1234!xyz"
        )
        saved = save_account.call_args.kwargs
        self.factory.assert_called_once_with(proxy="http://proxy.test:8080", email="user@example.com")
        self.assertIs(worker_cls.call_args.kwargs["session"], self.transport)
        self.assertNotIn("profile", worker_cls.call_args.kwargs)
        self.assertNotIn("proxy_rotate_callback", worker_cls.call_args.kwargs)
        self.assertIs(worker_cls.return_value.sentinel, self.transport)
        self.assertEqual(worker_cls.return_value.user_agent, "Chrome/153.0.0.0")
        self.assertEqual(saved["extra"]["browser_profile"], self.transport.profile)
        self.factory.return_value.__exit__.assert_called_once()
        self.assertEqual(saved["access_token"], "at-test")
        self.assertEqual(saved["totp_secret"], "SECRET")
        self.assertEqual(saved["extra"]["protocol_source"]["commit"], "98e0ad6717566dcaec2a2d7feb7b3bea2458de1")
        persist_password.assert_called_once_with("user@example.com", "Abcd1234!xyz")
        trigger_flow.assert_called_once_with("at-test")

    @patch("core.abais_protocol.protocol_register.ChatGPTProtocolRegister")
    @patch("core.abai_protocol_registration.registration_password", return_value="Abcd1234!xyz")
    @patch("core.abai_protocol_registration._pick_protocol_proxy", return_value="http://proxy.test:8080")
    def test_rejects_missing_totp_activation(self, _pick_proxy, _password, worker_cls):
        worker_cls.return_value.run.return_value = {"access_token": "at-test", "totp_2fa": {"bound": False}}
        with self.assertRaisesRegex(RuntimeError, "未确认 TOTP 激活"):
            run_abai_protocol_registration(
                email="user@example.com", name="User Example", birthday="1990-01-01"
            )

    @patch("core.abai_protocol_registration.persist_confirmed_registration_password")
    @patch("core.abais_protocol.protocol_register.ChatGPTProtocolRegister")
    def test_missing_password_confirmation_never_saves_checkpoint(self, worker, checkpoint):
        worker.return_value.run.return_value = {
            "access_token": "at", "totp_2fa": {"bound": True, "secret": "secret"},
        }
        with self.assertRaisesRegex(RuntimeError, "未确认密码"):
            run_abai_protocol_registration(email="u@example.test", name="U", birthday=None, proxy="http://proxy.test")
        checkpoint.assert_not_called()
        self.factory.return_value.__exit__.assert_called_once()

    @patch("core.abais_protocol.protocol_register.ChatGPTProtocolRegister")
    def test_worker_failure_still_closes_owned_profile(self, worker):
        worker.return_value.run.side_effect = RuntimeError("fixture failure")
        with self.assertRaisesRegex(RuntimeError, "stage=protocol_request"):
            run_abai_protocol_registration(email="u@example.test", name="U", birthday=None, proxy="http://proxy.test")
        self.factory.return_value.__exit__.assert_called_once()

    @patch("core.abai_protocol_registration.save_account_data")
    @patch("core.abai_protocol_registration.persist_confirmed_registration_password")
    @patch("core.abais_protocol.protocol_register.ChatGPTProtocolRegister")
    def test_explicit_deactivation_is_preserved_without_saving_credentials(self, worker, checkpoint, save):
        from core.abais_protocol.credential_checks import ChatGPTAccountBannedDuringRelogin
        from core.openai_auth import AccountUnusableError
        worker.return_value.run.side_effect = ChatGPTAccountBannedDuringRelogin("PRIVATE", code="account_deactivated")
        self.transport.http_status = 403
        with self.assertRaises(AccountUnusableError) as error:
            run_abai_protocol_registration(email="u@example.test", name="U", birthday=None, proxy="http://proxy.test")
        self.assertEqual(error.exception.error_code, "account_deactivated")
        self.assertIn("http_status=403", str(error.exception))
        self.assertNotIn("PRIVATE", str(error.exception))
        checkpoint.assert_not_called()
        save.assert_not_called()
        self.factory.return_value.__exit__.assert_called_once()


if __name__ == "__main__":
    import unittest

    unittest.main()
