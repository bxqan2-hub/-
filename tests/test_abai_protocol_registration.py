from dataclasses import dataclass
from unittest import TestCase
from unittest.mock import patch

from core.abai_protocol_registration import run_abai_protocol_registration


@dataclass(frozen=True)
class _Profile:
    name: str = "test-profile"
    impersonate: str = "firefox133"


class AbaiProtocolRegistrationTests(TestCase):
    @patch("core.abai_protocol_registration.trigger_flow", return_value={"status": "skipped", "ok": False})
    @patch("core.abai_protocol_registration.save_account_data", return_value=42)
    @patch("core.abai_protocol_registration.persist_confirmed_registration_password", return_value=True)
    @patch("core.abai_protocol_registration._next_profile", return_value=_Profile())
    @patch("core.abais_protocol.protocol_register.ChatGPTProtocolRegister")
    @patch("core.abai_protocol_registration.registration_password", return_value="Abcd1234!xyz")
    @patch("core.abai_protocol_registration._pick_protocol_proxy", return_value="http://proxy.test:8080")
    def test_calls_copied_flow_and_maps_password_totp_result(
        self,
        _pick_proxy,
        _password,
        worker_cls,
        _profile,
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
        self.assertEqual(saved["access_token"], "at-test")
        self.assertEqual(saved["totp_secret"], "SECRET")
        self.assertEqual(saved["extra"]["protocol_source"]["commit"], "98e0ad6717566dcaec2a2d7feb7b3bea2458de1")
        persist_password.assert_called_once_with("user@example.com", "Abcd1234!xyz")
        trigger_flow.assert_called_once_with("at-test")

    @patch("core.abais_protocol.protocol_register.ChatGPTProtocolRegister")
    @patch("core.abai_protocol_registration.registration_password", return_value="Abcd1234!xyz")
    @patch("core.abai_protocol_registration._next_profile", return_value=_Profile())
    @patch("core.abai_protocol_registration._pick_protocol_proxy", return_value="http://proxy.test:8080")
    def test_rejects_missing_totp_activation(self, _pick_proxy, _profile, _password, worker_cls):
        worker_cls.return_value.run.return_value = {"access_token": "at-test", "totp_2fa": {"bound": False}}
        with self.assertRaisesRegex(RuntimeError, "未确认 TOTP 激活"):
            run_abai_protocol_registration(
                email="user@example.com", name="User Example", birthday="1990-01-01"
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
