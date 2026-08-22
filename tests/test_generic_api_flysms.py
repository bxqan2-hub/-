# -*- coding: utf-8 -*-
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

from core import generic_api_mail_client as generic_client
from core.generic_api_mail_client import (
    GenericApiEmailAccount,
    GenericApiMailError,
    GenericApiTransportError,
    _extract_code,
    _fetch_flysms_otp,
    _parse_flysms_pickup_url,
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
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("headers") or {}))
        return FakeResponse(data={
            "email": "a@icloud.com",
            "messages": [
                {
                    "uid": 1,
                    "subject": "旧码",
                    "from": "x@y.com",
                    "date": "2026-08-05T09:00:00.000Z",
                    "preview": "code 111111",
                },
                {
                    "uid": 2,
                    "subject": "ChatGPT 用の一時ログインコード",
                    "from": "ChatGPT <noreply@tm.openai.com>",
                    "date": "2026-08-05T10:46:06.000Z",
                    "preview": "この一時検証コードを入力して続行してください: 251476 検証コードをリクエストしていない場合",
                },
            ],
        })


class FlysmsPickupTests(unittest.TestCase):
    def test_generic_code_endpoint_fast_fails_after_bounded_transport_errors(self):
        account = GenericApiEmailAccount("a@icloud.com", "https://example.test/code")
        session = MagicMock()
        session.get.side_effect = requests.exceptions.ConnectTimeout("mail endpoint unavailable")

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client.requests.Session", return_value=session), \
             patch("core.generic_api_mail_client.time.sleep"), \
             patch.object(generic_client._email_cfg, "GENERIC_API_MAX_CONSECUTIVE_ERRORS", 1), \
             patch.object(generic_client._email_cfg, "GENERIC_API_REQUEST_TIMEOUT", 8), \
             patch.object(generic_client._email_cfg, "GENERIC_API_RETRY_TIMEOUT", 5):
            with self.assertRaisesRegex(GenericApiTransportError, "连续网络失败"):
                fetch_latest_otp(account.email, max_wait=60)

        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args_list[0].kwargs["timeout"], 8)
        self.assertEqual(session.get.call_args_list[1].kwargs["timeout"], 5)

    def test_html_css_color_is_not_treated_as_otp(self):
        html = """
        <style>body { color: #171717; background: #f6f7f9; }</style>
        <div class="mailtop">ChatGPT の一時的な認証コード</div>
        <div class="subject">742390</div>
        """
        self.assertEqual(_extract_code(html), "742390")

    def test_extracts_code_from_iframe_srcdoc_javascript(self):
        # api798 返回的页面把邮件正文放进 JS 字符串，再赋给 iframe.srcdoc；
        # 正文中的验证码不在页面可见 HTML 节点里。
        html = r'''
        <style>body { background: #667eea; }</style>
        <script>
          var htmlContent = "<html>\\r\\n<title>ChatGPT \\u306e\\u8a8d\\u8a3c\\u30b3\\u30fc\\u30c9</title>\\r\\n<p>この一時検証コードを入力してください:</p>\\r\\n<p>588969</p></html>";
          frame.srcdoc = htmlContent;
        </script>
        '''
        self.assertEqual(_extract_code(html), "588969")

    def test_parse_hash_url(self):
        url = "https://flysms.xyz/icloud/pickup#email=a%40icloud.com&key=tok_abc"
        self.assertEqual(
            _parse_flysms_pickup_url(url),
            ("https://flysms.xyz/icloud/api/pickup/messages", "a@icloud.com", "tok_abc"),
        )

    def test_parse_query_url(self):
        url = "https://flysms.xyz/icloud/pickup?email=a@icloud.com&key=tok_abc"
        self.assertEqual(
            _parse_flysms_pickup_url(url),
            ("https://flysms.xyz/icloud/api/pickup/messages", "a@icloud.com", "tok_abc"),
        )

    def test_non_flysms_url_not_matched(self):
        self.assertIsNone(_parse_flysms_pickup_url("http://yangyang.website/messages/tok/a@icloud.com"))
        self.assertIsNone(_parse_flysms_pickup_url("https://example.com/code?x=1"))

    def test_fetch_uses_bearer_and_picks_latest(self):
        session = FakeSession()
        result = _fetch_flysms_otp(
            session,
            "https://flysms.xyz/icloud/pickup#email=a@icloud.com&key=tok_abc",
            {"User-Agent": "test"},
            after_ts=None,
        )
        self.assertIsNotNone(result)
        code, meta = result
        self.assertEqual(code, "251476")
        self.assertEqual(meta.get("source"), "flysms")
        url, headers = session.calls[0]
        self.assertEqual(url, "https://flysms.xyz/icloud/api/pickup/messages")
        self.assertEqual(headers.get("Authorization"), "Bearer tok_abc")
        self.assertEqual(headers.get("X-Mailbox-Email"), "a@icloud.com")

    def test_after_ts_filters_old(self):
        session = FakeSession()
        after = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc).timestamp()
        result = _fetch_flysms_otp(
            session,
            "https://flysms.xyz/icloud/pickup#email=a@icloud.com&key=tok_abc",
            {"User-Agent": "test"},
            after_ts=after,
        )
        self.assertIsNone(result)

    def test_polling_skips_rejected_cached_code_until_new_code_arrives(self):
        clock = [0.0]

        class SequenceSession:
            calls = 0
            instances = 0

            def __init__(self):
                self.__class__.instances += 1

            def get(self, *_args, **_kwargs):
                self.__class__.calls += 1
                code = "111111" if self.__class__.calls == 1 else "222222"
                return FakeResponse(status_code=200, text='{"code":"' + code + '"}')

        def advance(seconds):
            clock[0] += seconds

        account = GenericApiEmailAccount("a@icloud.com", "https://example.test/code")
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client.requests.Session", SequenceSession), \
             patch("core.generic_api_mail_client.time.time", side_effect=lambda: clock[0]), \
             patch("core.generic_api_mail_client.time.sleep", side_effect=advance):
            code = fetch_latest_otp(
                account.email,
                after_ts=0,
                max_wait=5,
                poll_interval=1,
                settle_seconds=0,
                exclude_codes={"111111"},
            )
        self.assertEqual(code, "222222")
        self.assertEqual(SequenceSession.instances, 1)

    def test_settled_otp_returns_before_starting_another_network_request(self):
        clock = [0.0]

        class OneConnectionSession:
            calls = 0

            def get(self, *_args, **_kwargs):
                self.__class__.calls += 1
                return FakeResponse(status_code=200, text='{"code":"654321"}')

        def advance(seconds):
            clock[0] += seconds

        account = GenericApiEmailAccount("a@icloud.com", "https://example.test/code")
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client.requests.Session", OneConnectionSession), \
             patch("core.generic_api_mail_client.time.time", side_effect=lambda: clock[0]), \
             patch("core.generic_api_mail_client.time.sleep", side_effect=advance):
            code = fetch_latest_otp(
                account.email,
                max_wait=20,
                poll_interval=2,
                settle_seconds=1,
            )

        self.assertEqual(code, "654321")
        self.assertEqual(OneConnectionSession.calls, 1)

    def test_polling_html_mailbox_waits_for_mail_after_send_time(self):
        clock = [0.0]

        def mail_html(date_text, code):
            return f"""
            <div class="card">
              <div class="fr">ChatGPT</div>
              <div class="su">ChatGPT 用の一時ログインコード</div>
              <div class="dt">{date_text}</div>
              <div class="bd">この一時検証コードを入力して続行してください: {code}</div>
            </div>
            """

        class SequenceHtmlSession:
            calls = 0

            def get(self, *_args, **_kwargs):
                self.__class__.calls += 1
                if self.__class__.calls == 1:
                    return FakeResponse(text=mail_html("Mon, 10 Aug 2026 04:57:49 +0000", "111111"))
                return FakeResponse(text=mail_html("Mon, 10 Aug 2026 04:59:05 +0000", "222222"))

        def advance(seconds):
            clock[0] += seconds

        account = GenericApiEmailAccount("a@icloud.com", "https://example.test/code")
        sent_at = datetime(2026, 8, 10, 4, 58, 0, tzinfo=timezone.utc).timestamp()
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client.requests.Session", SequenceHtmlSession), \
             patch("core.generic_api_mail_client.time.time", side_effect=lambda: clock[0]), \
             patch("core.generic_api_mail_client.time.sleep", side_effect=advance):
            code = fetch_latest_otp(
                account.email,
                after_ts=sent_at,
                max_wait=5,
                poll_interval=1,
                settle_seconds=0,
            )
        self.assertEqual(code, "222222")
        self.assertGreaterEqual(SequenceHtmlSession.calls, 2)

    def test_polling_allows_same_code_when_new_mail_is_after_send_time(self):
        clock = [0.0]
        html = """
        <div class="card">
          <div class="fr">ChatGPT</div>
          <div class="su">ChatGPT 用の一時ログインコード</div>
          <div class="dt">Mon, 10 Aug 2026 05:59:45 +0000 (UTC)</div>
          <div class="bd">この一時検証コードを入力して続行してください: 398154</div>
        </div>
        """

        class FreshSameCodeSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse(text=html)

        def advance(seconds):
            clock[0] += seconds

        account = GenericApiEmailAccount("a@icloud.com", "https://example.test/code")
        sent_at = datetime(2026, 8, 10, 5, 59, 30, tzinfo=timezone.utc).timestamp()
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client.requests.Session", FreshSameCodeSession), \
             patch("core.generic_api_mail_client.time.time", side_effect=lambda: clock[0]), \
             patch("core.generic_api_mail_client.time.sleep", side_effect=advance):
            code = fetch_latest_otp(
                account.email,
                after_ts=sent_at,
                max_wait=5,
                poll_interval=1,
                settle_seconds=0,
                exclude_codes={"398154"},
            )
        self.assertEqual(code, "398154")

    def test_polling_stops_when_browser_has_advanced(self):
        account = GenericApiEmailAccount("a@icloud.com", "https://example.test/code")
        session = FakeSession()
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client.requests.Session", return_value=session):
            with self.assertRaisesRegex(GenericApiMailError, "进入下一步"):
                fetch_latest_otp(account.email, should_stop=lambda: True)
        self.assertEqual(session.calls, [])

    def test_snapshot_current_otp_reads_cached_code_once(self):
        account = GenericApiEmailAccount("a@icloud.com", "https://example.test/code")

        class SnapshotSession:
            calls = 0

            def get(self, *_args, **_kwargs):
                self.__class__.calls += 1
                return FakeResponse(status_code=200, text="Your verification code is 654321")

            def close(self):
                pass

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client.requests.Session", SnapshotSession):
            code = snapshot_current_otp(account.email)
        self.assertEqual(code, "654321")
        self.assertEqual(SnapshotSession.calls, 1)



if __name__ == "__main__":
    unittest.main()
