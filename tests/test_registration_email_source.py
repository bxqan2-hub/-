# -*- coding: utf-8 -*-
import unittest
from unittest.mock import ANY, MagicMock, patch

from config import email as email_config
from config import register as register_config
from core import registration_service


class RegistrationEmailSourceTests(unittest.TestCase):
    @patch("config.proxy.pick_local_proxy", return_value="http://127.0.0.1:7890")
    def test_local_proxy_mode_uses_static_or_system_pool(self, pick_local_proxy):
        self.assertEqual(
            registration_service._resolve_registration_proxy("local"),
            "http://127.0.0.1:7890",
        )
        pick_local_proxy.assert_called_once_with()

    @patch("config.proxy.pick_local_proxy", return_value="")
    def test_local_proxy_mode_fails_closed_without_local_proxy(self, pick_local_proxy):
        with self.assertRaisesRegex(RuntimeError, "本地代理模式"):
            registration_service._resolve_registration_proxy("local")

    @patch("core.profile_utils.generate_random_birthday", return_value="1992-03-04")
    @patch("core.registration_service._random_display_name", return_value="Example User")
    @patch("core.email_provider.acquire_email", return_value="selected@mail.com")
    def test_prepare_registration_args_uses_job_email_source(
        self,
        acquire_email,
        _random_display_name,
        _generate_random_birthday,
    ):
        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", ""
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            result = registration_service._prepare_registration_args(email_source="inbox_mate")

        self.assertEqual(result, ("selected@mail.com", "Example User", "1992-03-04"))
        acquire_email.assert_called_once_with(email_source="inbox_mate")

    @patch("core.profile_utils.generate_random_birthday", return_value="1992-03-04")
    @patch("core.registration_service._random_display_name", return_value="Example User")
    @patch("core.email_provider.acquire_email")
    def test_prepare_registration_args_uses_selected_email_without_reacquiring(
        self,
        acquire_email,
        _random_display_name,
        _generate_random_birthday,
    ):
        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", ""
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            result = registration_service._prepare_registration_args(
                email_source="outlook",
                email_override="chosen@mail.com",
            )

        self.assertEqual(result, ("chosen@mail.com", "Example User", "1992-03-04"))
        acquire_email.assert_not_called()

    @patch.object(registration_service.db, "get_job", return_value={"id": 17, "log_file": "registration.log"})
    @patch.object(registration_service.db, "create_job", return_value={"id": 17, "log_file": "registration.log"})
    @patch.object(registration_service.db, "claim_email", return_value={"email": "chosen@mail.com"})
    @patch.object(registration_service, "get_executor_workers", return_value=2)
    @patch.object(registration_service, "get_executor")
    def test_submit_registration_binds_each_selected_email_to_its_job(
        self,
        get_executor,
        _get_executor_workers,
        claim_email,
        create_job,
        _get_job,
    ):
        executor = MagicMock()
        get_executor.return_value = executor

        jobs = registration_service.submit_registration(
            workers=2,
            email_items=[
                {"source": "outlook", "email": "chosen@mail.com"},
                {"source": "outlook", "email": "chosen@mail.com"},
            ],
        )

        self.assertEqual(len(jobs), 1)
        claim_email.assert_called_once_with("chosen@mail.com", "outlook")
        create_job.assert_called_once_with(
            email_source="outlook",
            email="chosen@mail.com",
            gc_mode=ANY,
            proxy_mode=None,
        )
        executor.submit.assert_called_once_with(registration_service._run_one_job, 17, "registration.log")
        executor.submit.return_value.add_done_callback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
