import json
import unittest
from unittest.mock import Mock, patch

import requests

from core import db
from core.inbox_mate_mail_client import _code_from_payload, looks_like_labeled_import, parse_import_text
from webui.app import create_app


class InboxMateParserTests(unittest.TestCase):
    def test_parses_chinese_account_password_rows_and_detects_provider(self):
        rows = parse_import_text(
            "账号: first@mail.com | 密码: secret-a\n"
            "账号: second@custom.example | 密码: secret-b"
        )

        self.assertEqual([row["email_domain"] for row in rows], ["mail.com", "custom.example"])
        self.assertEqual(rows[0]["mail_provider"], "mailcom")
        self.assertEqual(rows[1]["mail_provider"], "custom")
        self.assertTrue(rows[0]["code_url"].endswith("/api/v1/jobs"))

    def test_extracts_code_fields_before_job_metadata_numbers(self):
        self.assertEqual(
            _code_from_payload({"jobId": "123456", "messages": [{"subject": "Your code is 654321"}]}),
            "654321",
        )

    def test_labeled_detection_does_not_match_outlook_delimited_rows(self):
        self.assertTrue(looks_like_labeled_import("账号: one@mail.com | 密码: secret"))
        self.assertFalse(looks_like_labeled_import("one@mail.com----secret----client-id----refresh-token"))


class InboxMateJobTests(unittest.TestCase):
    @patch("core.inbox_mate_mail_client.requests.Session")
    @patch("core.inbox_mate_mail_client._row_for_email")
    def test_fetches_otp_from_sse_message(self, row_for_email, session_factory):
        row_for_email.return_value = {
            "email": "member@mail.com",
            "password": "secret",
            "provider": "inbox_mate",
            "mail_provider": "mailcom",
            "api_base": "https://mail.ap1x.xyz",
        }
        session = Mock()
        session.get.side_effect = [
            self._response({"csrfToken": "csrf"}),
            self._stream_response([
                'data: {"clientAccountId":"x","state":"searching"}',
                'data: {"messages":[{"subject":"Your verification code is 482915"}]}',
            ]),
        ]
        session.post.return_value = self._response({"jobId": "job-1"}, status=202)
        session_factory.return_value = session

        from core.inbox_mate_mail_client import fetch_latest_otp
        self.assertEqual(fetch_latest_otp("member@mail.com", max_wait=5), "482915")
        submitted = session.post.call_args.kwargs["json"]
        self.assertEqual(submitted["accounts"][0]["provider"], "mailcom")
        self.assertEqual(submitted["accounts"][0]["auth"]["type"], "app_password")
        self.assertNotIn("customHost", submitted["accounts"][0])

    @patch("core.inbox_mate_mail_client.time.sleep")
    @patch("core.inbox_mate_mail_client.requests.Session")
    @patch("core.inbox_mate_mail_client._row_for_email")
    def test_retries_transient_session_timeout(self, row_for_email, session_factory, sleep):
        row_for_email.return_value = {
            "email": "member@mail.com",
            "password": "secret",
            "provider": "inbox_mate",
            "mail_provider": "mailcom",
            "api_base": "https://mail.ap1x.xyz",
        }
        session = Mock()
        session.get.side_effect = [
            requests.exceptions.ConnectTimeout("temporary timeout"),
            self._response({"csrfToken": "csrf"}),
            self._stream_response([
                'data: {"messages":[{"subject":"Your verification code is 731204"}]}',
            ]),
        ]
        session.post.return_value = self._response({"jobId": "job-1"}, status=202)
        session_factory.return_value = session

        from core.inbox_mate_mail_client import fetch_latest_otp
        self.assertEqual(fetch_latest_otp("member@mail.com", max_wait=5), "731204")
        sleep.assert_called_once()
        self.assertEqual(session.post.call_count, 1)

    @patch("core.inbox_mate_mail_client.time.sleep")
    @patch("core.inbox_mate_mail_client.requests.Session")
    @patch("core.inbox_mate_mail_client._row_for_email")
    def test_rescans_after_completed_job_has_no_code(self, row_for_email, session_factory, sleep):
        row_for_email.return_value = {
            "email": "member@mail.com",
            "password": "secret",
            "provider": "inbox_mate",
            "mail_provider": "mailcom",
            "api_base": "https://mail.ap1x.xyz",
        }
        session = Mock()
        session.get.side_effect = [
            self._response({"csrfToken": "csrf-1"}),
            self._stream_response(['data: {"state":"completed","messages":[]}']),
            self._response({"csrfToken": "csrf-2"}),
            self._stream_response([
                'data: {"messages":[{"subject":"Your verification code is 845219"}]}',
            ]),
        ]
        session.post.side_effect = [
            self._response({"jobId": "job-1"}, status=202),
            self._response({"jobId": "job-2"}, status=202),
        ]
        session_factory.return_value = session

        from core.inbox_mate_mail_client import fetch_latest_otp
        self.assertEqual(fetch_latest_otp("member@mail.com", max_wait=5), "845219")
        sleep.assert_called_once()
        self.assertEqual(session.post.call_count, 2)

    @patch("core.inbox_mate_mail_client.requests.Session")
    @patch("core.inbox_mate_mail_client._row_for_email")
    def test_snapshot_does_not_rescan_completed_empty_job(self, row_for_email, session_factory):
        row_for_email.return_value = {
            "email": "member@mail.com",
            "password": "secret",
            "provider": "inbox_mate",
            "mail_provider": "mailcom",
            "api_base": "https://mail.ap1x.xyz",
        }
        session = Mock()
        session.get.side_effect = [
            self._response({"csrfToken": "csrf"}),
            self._stream_response(['data: {"state":"completed","messages":[]}']),
        ]
        session.post.return_value = self._response({"jobId": "job-1"}, status=202)
        session_factory.return_value = session

        from core.inbox_mate_mail_client import snapshot_current_otp
        self.assertIsNone(snapshot_current_otp("member@mail.com", timeout=1))
        self.assertEqual(session.post.call_count, 1)

    @patch("core.inbox_mate_mail_client.parse_import_text")
    @patch.object(db, "import_generic_api_emails", return_value=(2, 0))
    def test_webui_import_accepts_inbox_mate_source(self, import_rows, parse_rows):
        parse_rows.return_value = [
            {"email": "one@mail.com", "email_domain": "mail.com", "provider": "inbox_mate", "code_url": "https://mail.ap1x.xyz/api/v1/jobs"},
            {"email": "two@mail.com", "email_domain": "mail.com", "provider": "inbox_mate", "code_url": "https://mail.ap1x.xyz/api/v1/jobs"},
        ]
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        response = client.post(
            "/api/outlook/import",
            json={"source": "inbox_mate", "text": "账号: one@mail.com | 密码: secret", "as_registered": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["domains"], ["mail.com"])
        self.assertEqual(response.get_json()["source"], "inbox_mate")
        import_rows.assert_called_once_with(parse_rows.return_value)

    @patch.object(db, "import_generic_api_emails", return_value=(1, 0))
    def test_webui_import_detects_inbox_mate_when_source_is_missing(self, import_rows):
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        response = client.post(
            "/api/outlook/import",
            json={"text": "账号: one@mail.com | 密码: secret", "as_registered": False},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["source"], "inbox_mate")
        self.assertEqual(payload["parsed"], 1)
        import_rows.assert_called_once()

    @patch("core.inbox_mate_mail_client.parse_import_text")
    @patch.object(db, "import_generic_api_emails", return_value=(1, 0))
    def test_webui_import_normalizes_mailcom_alias(self, import_rows, parse_rows):
        parse_rows.return_value = [{
            "email": "one@mail.com",
            "email_domain": "mail.com",
            "provider": "inbox_mate",
            "code_url": "https://mail.ap1x.xyz/api/v1/jobs",
        }]
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        response = client.post(
            "/api/outlook/import",
            json={"source": "mailcom", "text": "账号: one@mail.com | 密码: secret"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["source"], "inbox_mate")
        import_rows.assert_called_once_with(parse_rows.return_value)

    @staticmethod
    def _response(payload, status=200):
        response = Mock()
        response.status_code = status
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    @staticmethod
    def _stream_response(lines):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_lines.return_value = lines
        return response


if __name__ == "__main__":
    unittest.main()
