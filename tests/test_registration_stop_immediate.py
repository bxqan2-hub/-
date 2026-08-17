# -*- coding: utf-8 -*-
import threading
import unittest
from unittest.mock import patch

from core import registration_service


class RegistrationImmediateStopTests(unittest.TestCase):
    def test_profile_created_after_stop_is_deleted_before_open(self):
        with patch.object(registration_service, "current_job_id", return_value=13), \
             patch.object(registration_service.db, "update_job") as update_job, \
             patch.object(registration_service.db, "get_job", return_value={"id": 13, "status": "stopped"}), \
             patch.object(registration_service, "_cleanup_roxy_on_stop", return_value=True) as cleanup:
            with self.assertRaises(registration_service.StopRequested):
                registration_service.bind_roxy_profile("profile-13")

        update_job.assert_called_once_with(13, roxy_profile_id="profile-13", gc_window_state="open")
        cleanup.assert_called_once_with(13, "profile-13")

    def test_running_job_is_marked_stopped_without_async_thread_injection(self):
        stop_event = threading.Event()
        job = {
            "id": 42,
            "status": "running",
            "email": "mail@example.test",
            "roxy_profile_id": "profile-42",
        }
        with patch.object(registration_service, "_ACTIVE_JOBS", {42}), \
             patch.object(registration_service, "_STOP_EVENTS", {42: stop_event}), \
             patch.object(registration_service.db, "get_job", return_value=job), \
             patch.object(registration_service.db, "update_job") as update_job, \
             patch.object(registration_service, "_cleanup_roxy_on_stop", return_value=True) as cleanup, \
             patch.object(registration_service, "_append_job_log"):
            result = registration_service.request_stop_job(42)

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "stopped")
        self.assertNotIn("injected", result)
        self.assertTrue(stop_event.is_set())
        cleanup.assert_called_once_with(42, "profile-42")
        self.assertEqual(update_job.call_args.kwargs["status"], "stopped")
        self.assertNotEqual(update_job.call_args.kwargs["status"], "stopping")

    def test_pending_job_still_cancels_without_thread_interrupt(self):
        job = {"id": 7, "status": "pending", "email": None}
        with patch.object(registration_service.db, "get_job", return_value=job), \
             patch.object(registration_service.db, "cancel_pending_job", return_value=job) as cancel_pending_job, \
             patch.object(registration_service, "_cleanup_roxy_on_stop") as cleanup, \
             patch.object(registration_service, "_append_job_log"):
            result = registration_service.request_stop_job(7)

        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(cancel_pending_job.call_args.args[0], 7)
        cleanup.assert_not_called()

    def test_orphaned_running_job_closes_and_deletes_bound_profile(self):
        job = {
            "id": 9,
            "status": "running",
            "email": "mail@example.test",
            "roxy_profile_id": "profile-9",
        }
        with patch.object(registration_service, "_ACTIVE_JOBS", set()), \
             patch.object(registration_service, "_STOP_EVENTS", {}), \
             patch.object(registration_service.db, "get_job", return_value=job), \
             patch.object(registration_service.db, "update_job") as update_job, \
             patch.object(registration_service, "_cleanup_roxy_on_stop", return_value=True) as cleanup, \
             patch.object(registration_service, "_release_unconsumed_job_email"), \
             patch.object(registration_service, "_append_job_log"):
            result = registration_service.request_stop_job(9)

        self.assertTrue(result["ok"])
        self.assertTrue(result["browser_cleanup"])
        cleanup.assert_called_once_with(9, "profile-9")
        self.assertEqual(update_job.call_args_list[0].kwargs["status"], "stopped")

    @patch.object(registration_service.db, "update_job")
    @patch("core.roxybrowser_client.RoxyBrowserClient")
    def test_stop_cleanup_requires_profile_deletion(self, client_cls, update_job):
        client = client_cls.return_value
        client.close_profile.return_value = True
        client.delete_profile.return_value = True

        result = registration_service._cleanup_roxy_on_stop(11, "profile-11")

        self.assertTrue(result)
        client.close_profile.assert_called_once_with("profile-11")
        client.delete_profile.assert_called_once_with("profile-11")
        update_job.assert_called_once_with(11, gc_window_state="deleted")


if __name__ == "__main__":
    unittest.main()
