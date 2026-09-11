# -*- coding: utf-8 -*-
import base64
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

from core import generic_api_mail_client as generic_client
from core.generic_api_mail_client import (
    GenericApiEmailAccount,
    GenericApiMailError,
    GenericApiTransportError,
    _decode_data_uri,
    _extract_inline_messages_html_otp,
    _fetch_yangyang_otp,
    _parse_generic_api_ts,
    _parse_yangyang_code_url,
    _parse_yangyang_ts,
    fetch_latest_otp,
    snapshot_current_otp,
)


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        return self._data


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "/api/messages/" in url:
            return FakeResponse(data={
                "items": [
                    {"id": 1, "subject": "旧码", "received_at": "2026-08-01 10:00:00"},
                    {"id": 2, "subject": "Your OpenAI code is 654321", "received_at": "2026-08-01 10:01:00"},
                ],
                "has_more": False,
            })
        if "/message/2/" in url:
            html = "<html><body>Your verification code is <b>654321</b></body></html>"
            body = "data:text/html;charset=utf-8;base64," + base64.b64encode(html.encode()).decode()
            return FakeResponse(data={"subject": "Your OpenAI code is 654321", "body": body, "receivedAt": "2026-08-01 10:01:00"})
        if "/message/1/" in url:
            return FakeResponse(data={"subject": "旧码", "body": "code 111111", "receivedAt": "2026-08-01 10:00:00"})
        return FakeResponse(status_code=404, text="not found")


class FakeInlineSession:
    def get(self, url, **kwargs):
        if "/api/messages/" in url:
            return FakeResponse(status_code=404, text="Not Found")
        return FakeResponse(text="""
        <article class="mail-card">
          <details open>
            <summary>
              <span class="subject">Your temporary ChatGPT verification code</span>
              <span class="date">2026-08-02 13:18:53</span>
            </summary>
            <div class="meta">发件人：otp@example.com</div>
            <pre class="body">Enter this temporary verification code to continue:

541409

Please ignore this email.</pre>
          </details>
        </article>
        """)


class GenericApiYangyangTests(unittest.TestCase):
    def test_parse_yangyang_url(self):
        self.assertEqual(
            _parse_yangyang_code_url("http://yangyang.website/messages/tok/a@icloud.com"),
            ("http://yangyang.website", "tok", "a@icloud.com"),
        )

    def test_decode_data_uri_base64(self):
        body = "data:text/html;base64," + base64.b64encode("验证码 123456".encode()).decode()
        self.assertIn("123456", _decode_data_uri(body))

    def test_fetch_yangyang_otp_uses_api_and_detail(self):
        result = _fetch_yangyang_otp(
            FakeSession(),
            "http://yangyang.website/messages/tok/a@icloud.com",
            {"User-Agent": "test"},
        )
        code, meta = result
        self.assertEqual(code, "654321")
        self.assertEqual(meta["mail_id"], 2)

    def test_fetch_yangyang_otp_respects_after_ts(self):
        import datetime
        after = datetime.datetime(2026, 8, 1, 10, 2, 0).timestamp()
        result = _fetch_yangyang_otp(
            FakeSession(),
            "http://yangyang.website/messages/tok/a@icloud.com",
            {"User-Agent": "test"},
            after_ts=after,
        )
        self.assertIsNone(result)

    def test_yangyang_timestamp_preserves_explicit_timezone(self):
        expected = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc).timestamp()
        for value in ("2026-09-11T10:00:00.000Z", "2026-09-11T18:00:00+08:00"):
            with self.subTest(value=value):
                self.assertEqual(_parse_yangyang_ts(value), expected)
        session = MagicMock()
        session.get.side_effect = [
            FakeResponse(data={"items": [{"id": 2, "received_at": "2026-09-11T10:00:00Z"}]}),
            FakeResponse(data={"subject": "Your ChatGPT code", "body": "Your code is 654321"}),
        ]
        result = _fetch_yangyang_otp(session, "https://example.test/messages/TOKEN/a@example.test", {}, after_ts=expected - 1)
        self.assertEqual(result[0], "654321")

    def test_yangyang_excludes_api_message_ids_before_reading_detail(self):
        session = FakeSession()
        result = _fetch_yangyang_otp(
            session,
            "https://example.test/messages/TOKEN/a@example.test",
            {},
            exclude_message_ids={"2"},
        )
        self.assertEqual(result[1]["mail_id"], 1)
        self.assertTrue(result[1]["mail_id_stable"])
        self.assertFalse(any("/message/2/" in url for url in session.urls))

    def test_yangyang_http_errors_are_not_empty_mailboxes(self):
        for stage in ("mail_list", "mail_detail", "mail_inline"):
            for status in (401, 503):
                with self.subTest(stage=stage, status=status):
                    responses = []
                    if stage == "mail_detail":
                        responses.append(FakeResponse(data={"items": [{"id": 2}]}))
                    elif stage == "mail_inline":
                        responses.append(FakeResponse(status_code=404))
                    responses.append(FakeResponse(status_code=status, text="TOKEN_WITH_SECRET 654321"))
                    session = MagicMock()
                    session.get.side_effect = responses
                    with self.assertRaises(GenericApiMailError) as caught:
                        _fetch_yangyang_otp(session, "https://example.test/messages/TOKEN/a@example.test", {})
                    self.assertIn(f"stage={stage}", str(caught.exception))
                    self.assertIn(f"http_status={status}", str(caught.exception))
                    self.assertEqual(caught.exception.retryable, status == 503)
                    self.assertNotIn("TOKEN_WITH_SECRET", str(caught.exception))

    def test_yangyang_empty_mailbox_is_not_an_api_error(self):
        session = MagicMock()
        session.get.return_value = FakeResponse(data={"items": [], "has_more": False})
        self.assertIsNone(_fetch_yangyang_otp(session, "https://example.test/messages/TOKEN/a@example.test", {}))
        self.assertEqual(session.get.call_count, 1)

    def test_yangyang_invalid_response_is_not_empty_mail(self):
        for payload in (None, [], {"error": "TOKEN_WITH_SECRET"}, {"items": "invalid"}):
            with self.subTest(payload=type(payload).__name__):
                session = MagicMock()
                session.get.return_value = FakeResponse(data=payload)
                with self.assertRaisesRegex(GenericApiMailError, "stage=mail_list.*type=invalid_schema"):
                    _fetch_yangyang_otp(session, "https://example.test/messages/TOKEN/a@example.test", {})
        response = MagicMock(status_code=200)
        response.json.side_effect = ValueError("TOKEN_WITH_SECRET")
        session.get.return_value = response
        with self.assertRaisesRegex(GenericApiMailError, "stage=mail_list.*type=invalid_json") as caught:
            _fetch_yangyang_otp(session, "https://example.test/messages/TOKEN/a@example.test", {})
        self.assertNotIn("TOKEN_WITH_SECRET", str(caught.exception))

    def test_yangyang_request_budget_covers_pages_and_details(self):
        clock = [0.0]
        timeouts = []
        session = MagicMock()
        responses = [
            FakeResponse(data={"items": [], "has_more": True, "next_cursor": "next"}),
            FakeResponse(data={"items": [{"id": 2}], "has_more": False}),
            FakeResponse(data={"subject": "Your ChatGPT code", "body": "Your code is 654321"}),
        ]

        def get(_url, **kwargs):
            timeouts.append(kwargs["timeout"])
            clock[0] += 2.5 if len(timeouts) < 3 else 0.5
            return responses.pop(0)

        session.get.side_effect = get
        with patch.object(generic_client.time, "monotonic", side_effect=lambda: clock[0]):
            result = _fetch_yangyang_otp(session, "https://example.test/messages/TOKEN/a@example.test", {}, request_timeout=6)
        self.assertEqual(result[0], "654321")
        self.assertEqual(timeouts, [6.0, 3.5, 1.0])

    def test_yangyang_stops_details_when_total_budget_expires(self):
        clock = [0.0]
        session = MagicMock()

        def get(_url, **_kwargs):
            clock[0] += 3
            return FakeResponse(data={"items": [{"id": 2}], "has_more": False})

        session.get.side_effect = get
        with patch.object(generic_client.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(GenericApiMailError, "stage=mail_detail type=deadline_exhausted"):
                _fetch_yangyang_otp(session, "https://example.test/messages/TOKEN/a@example.test", {}, request_timeout=2)
        self.assertEqual(session.get.call_count, 1)

    def test_yangyang_inline_fallback_uses_remaining_budget(self):
        clock = [0.0]
        timeouts = []
        session = MagicMock()

        def get(url, **kwargs):
            timeouts.append(kwargs["timeout"])
            if "/api/messages/" in url:
                clock[0] += 1.5
                return FakeResponse(status_code=404)
            return FakeResponse(text='<article class="mail-card"><p>Your code is 654321</p></article>')

        session.get.side_effect = get
        with patch.object(generic_client.time, "monotonic", side_effect=lambda: clock[0]):
            result = _fetch_yangyang_otp(session, "https://example.test/messages/TOKEN/a@example.test", {}, request_timeout=2)
        self.assertEqual(result[0], "654321")
        self.assertEqual(timeouts, [2.0, 0.5])

    def test_polling_passes_remaining_budget_and_bounds_provider_errors(self):
        account = GenericApiEmailAccount("a@example.test", "https://example.test/messages/TOKEN/a@example.test")
        clock = [0.0]
        with patch.object(generic_client, "get_account_context", return_value=account), \
             patch.object(generic_client.requests, "Session"), \
             patch.object(generic_client.time, "time", side_effect=lambda: clock[0]), \
             patch.object(generic_client, "_fetch_yangyang_otp", return_value=("654321", {})) as fetch:
            self.assertEqual(fetch_latest_otp(account.email, max_wait=2, request_timeout=8, settle_seconds=0), "654321")
        self.assertEqual(fetch.call_args.kwargs["request_timeout"], 2.0)
        with patch.object(generic_client, "get_account_context", return_value=account), \
             patch.object(generic_client.requests, "Session"), \
             patch.object(generic_client.time, "time", side_effect=lambda: clock[0]), \
             patch.object(generic_client.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch.object(generic_client, "_fetch_yangyang_otp", side_effect=GenericApiMailError("stage=mail_detail http_status=503 type=http_error", retryable=True)) as fetch:
            with self.assertRaisesRegex(GenericApiTransportError, "stage=mail_detail http_status=503") as caught:
                fetch_latest_otp(account.email, max_wait=20, poll_interval=1, max_consecutive_errors=2)
        self.assertEqual(fetch.call_count, 2)
        self.assertNotIn("尚未出现", str(caught.exception))

    def test_polling_preserves_nonretryable_provider_error(self):
        account = GenericApiEmailAccount("a@example.test", "https://example.test/messages/TOKEN/a@example.test")
        with patch.object(generic_client, "get_account_context", return_value=account), \
             patch.object(generic_client.requests, "Session"), \
             patch.object(generic_client, "_fetch_yangyang_otp", side_effect=GenericApiMailError("stage=mail_list http_status=401 type=http_error")) as fetch:
            with self.assertRaisesRegex(GenericApiMailError, "stage=mail_list http_status=401"):
                fetch_latest_otp(account.email, max_wait=20)
        self.assertEqual(fetch.call_count, 1)

    def test_yangyang_subject_and_snapshot_errors_are_redacted(self):
        with self.assertLogs(generic_client.logger, level="INFO") as captured:
            _fetch_yangyang_otp(FakeSession(), "https://example.test/messages/TOKEN/a@example.test", {})
        self.assertNotIn("654321", "\n".join(captured.output))
        account = GenericApiEmailAccount("a@example.test", "https://example.test/messages/TOKEN_WITH_SECRET/a@example.test")
        session = MagicMock()
        session.get.side_effect = requests.exceptions.ConnectionError(account.code_url + "?otp=654321")
        with self.assertRaisesRegex(GenericApiMailError, "stage=mail_list type=ConnectionError") as caught:
            _fetch_yangyang_otp(session, account.code_url, {})
        self.assertNotIn("TOKEN_WITH_SECRET", str(caught.exception))
        with patch.object(generic_client, "get_account_context", return_value=account), \
             patch.object(generic_client.requests, "Session", return_value=session), \
             self.assertLogs(generic_client.logger, level="DEBUG") as captured:
            self.assertIsNone(snapshot_current_otp(account.email))
        logs = "\n".join(captured.output)
        self.assertNotIn("TOKEN_WITH_SECRET", logs)
        self.assertNotIn("654321", logs)

    def test_fetch_inline_messages_page_without_api(self):
        result = _fetch_yangyang_otp(
            FakeInlineSession(),
            "https://mail.ai1998.xyz/messages/tok/cookies-benzene.48%40icloud.com",
            {"User-Agent": "test"},
        )
        code, meta = result
        self.assertEqual(code, "541409")
        self.assertEqual(meta["mail_id"], "inline-0")

    def test_rfc2822_mail_date_is_parsed(self):
        ts = _parse_generic_api_ts("Mon, 10 Aug 2026 04:57:49 +0000 (UTC)")
        self.assertIsNotNone(ts)
        self.assertEqual(int(ts), 1786337869)

    def test_icloud_card_only_accepts_mail_after_send_time(self):
        html = """
        <div class="card">
          <div class="fr">ChatGPT</div>
          <div class="su">ChatGPT 用の一時ログインコード</div>
          <div class="dt">Mon, 10 Aug 2026 04:57:49 +0000 (UTC)</div>
          <div class="bd">この一時検証コードを入力して続行してください: 017838</div>
        </div>
        """
        before_mail = _parse_generic_api_ts("Mon, 10 Aug 2026 04:57:00 +0000")
        after_mail = _parse_generic_api_ts("Mon, 10 Aug 2026 04:58:00 +0000")
        result = _extract_inline_messages_html_otp(html, after_ts=before_mail)
        self.assertEqual(result[0], "017838")
        self.assertEqual(result[1]["source"], "inline_html")
        self.assertIsNone(_extract_inline_messages_html_otp(html, after_ts=after_mail))


if __name__ == "__main__":
    unittest.main()
