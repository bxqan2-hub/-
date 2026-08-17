# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

import requests

from core.mail_status_detector import (
    classify_mailbox,
    fetch_flysms_messages,
    fetch_legacy_messages,
    parse_legacy_mailbox_html,
)


def msg(subject="", body="", sender="OpenAI <noreply@tm.openai.com>"):
    return {"subject": subject, "body": body, "from": sender, "date": "2026-08-07", "source": "test"}


class MailClassificationTests(unittest.TestCase):
    def test_japanese_plus_receipt(self):
        result = classify_mailbox([msg(
            "ChatGPT - 新しいプラン",
            "ChatGPT Plus に正常に登録されました。サブスクリプションの管理 注文番号: sub_1Ui1hCC6h1nxGol31Tmbms4R ChatGPT Plus Subscription",
        )])
        self.assertEqual(result["status"], "plus")

    def test_english_plus_receipt(self):
        result = classify_mailbox([msg(
            "ChatGPT - Your new plan",
            "You successfully subscribed. Plan: ChatGPT Plus Subscription. Order sub_abcdef123456. Manage your subscription.",
        )])
        self.assertEqual(result["status"], "plus")

    def test_plus_manage_link_extracts_workspace_account_id(self):
        result = classify_mailbox([msg(
            "ChatGPT - Your new plan",
            (
                "<p>You successfully subscribed to ChatGPT Plus Subscription.</p>"
                '<a href="https://chatgpt.com/account/manage?source=email&amp;account_id=acct-workspace-42">'
                "Manage your subscription</a>"
            ),
        )])
        self.assertEqual(result["status"], "plus")
        self.assertEqual(result["account_id"], "acct-workspace-42")

    def test_chinese_plus_receipt(self):
        result = classify_mailbox([msg(
            "ChatGPT - 您的新套餐",
            "您已成功订阅 ChatGPT Plus。管理您的订阅。ChatGPT Plus Subscription sub_abc123xyz",
        )])
        self.assertEqual(result["status"], "plus")

    def test_upgrade_marketing_is_not_plus(self):
        result = classify_mailbox([msg("Upgrade to ChatGPT Plus", "Try our best plan today")])
        self.assertEqual(result["status"], "nonplus")

    def test_deactivation_overrides_older_receipt(self):
        result = classify_mailbox([
            msg("ChatGPT - Your new plan", "ChatGPT Plus Subscription, sub_abc123xyz. Manage your subscription."),
            msg("OpenAI - Access Deactivated", "Your account has been deactivated. It can no longer be used. Initiate an appeal."),
        ])
        self.assertEqual(result["status"], "banned")
        self.assertEqual(result["label"], "账号被封禁")

    def test_chinese_deactivation(self):
        result = classify_mailbox([msg("OpenAI 账号通知", "您的账号已被停用，无法再继续使用。如有异议请提交申诉。")])
        self.assertEqual(result["status"], "banned")

    def test_legacy_cards_are_parsed(self):
        html = '<div class="card"><div class="su">ChatGPT - Your new plan</div><div class="fr">OpenAI</div><div class="dt">today</div><div class="bd">ChatGPT Plus Subscription<br>sub_abc123xyz</div></div>'
        messages = parse_legacy_mailbox_html(html)
        self.assertEqual(len(messages), 1)
        self.assertIn("ChatGPT Plus", messages[0]["body"])
        self.assertEqual(classify_mailbox(messages)["status"], "plus")


class _Response:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._data


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/42"):
            return _Response({"message": {"uid": 42, "subject": "ChatGPT - Your new plan", "from": "OpenAI", "body": "ChatGPT Plus Subscription sub_abcdef123456"}})
        return _Response({"messages": [{"uid": 42, "subject": "ChatGPT - Your new plan", "from": "OpenAI", "preview": "new plan"}]})


class FlySmsReadTests(unittest.TestCase):
    def test_list_and_detail_use_pickup_api_auth(self):
        session = _Session()
        messages = fetch_flysms_messages(
            "https://flysms.top/icloud/pickup#email=a%40icloud.com&key=tok_abc",
            session=session,
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("Plus Subscription", messages[0]["body"])
        self.assertEqual(session.calls[0][1]["headers"]["Authorization"], "Bearer tok_abc")
        self.assertEqual(session.calls[0][1]["headers"]["X-Mailbox-Email"], "a@icloud.com")
        self.assertEqual(session.calls[1][1]["params"]["mailbox"], "a@icloud.com")

    @patch("core.mail_status_detector.time.sleep")
    def test_transient_mailbox_timeout_is_retried(self, sleep):
        session = MagicMock()
        success = _Response({}, status_code=200)
        success.text = '<div class="card"><div class="su">ChatGPT</div><div class="bd">hello</div></div>'
        session.get.side_effect = [requests.ConnectTimeout("slow"), success]

        messages = fetch_legacy_messages("https://icloud-api.top/read?key=test", session=session)

        self.assertEqual(len(messages), 1)
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once_with(1.0)


class WordckReadTests(unittest.TestCase):
    def test_list_page_fetches_detail_and_classifies_japanese_plus(self):
        list_url = "https://mail.wordck.top/m/share-token?n=50"
        detail_url = "https://mail.wordck.top/m/share-token/message-42"
        list_response = _Response({}, status_code=200)
        list_response.url = list_url
        list_response.text = (
            '<div class="list"><a class="mail" href="/m/share-token/message-42">'
            '<strong>OpenAI</strong><div class="subject">ChatGPT - 新しいプラン</div></a></div>'
        )
        detail_response = _Response({}, status_code=200)
        detail_response.url = detail_url
        detail_response.text = (
            '<div class="article"><h1>ChatGPT - 新しいプラン</h1>'
            '<div class="meta">发件人：OpenAI &lt;noreply@tm.openai.com&gt;</div>'
            '<div class="meta">时间：2026/08/17 03:22</div>'
            '<iframe srcdoc="&lt;p&gt;ChatGPT Plus に正常に登録されました。&lt;/p&gt;'
            '&lt;p&gt;サブスクリプションの管理&lt;/p&gt;'
            '&lt;a href=&quot;https://chatgpt.com/account/manage?account_id=acct-workspace-42&quot;&gt;'
            '管理&lt;/a&gt;"></iframe></div>'
        )
        session = MagicMock()
        session.get.side_effect = [list_response, detail_response]

        messages = fetch_legacy_messages("https://mail.wordck.top/m/share-token", session=session)
        result = classify_mailbox(messages)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["source"], "wordck")
        self.assertEqual(messages[0]["subject"], "ChatGPT - 新しいプラン")
        self.assertEqual(messages[0]["date"], "2026/08/17 03:22")
        self.assertEqual(result["status"], "plus")
        self.assertEqual(result["account_id"], "acct-workspace-42")
        self.assertEqual(session.get.call_args_list[1].args[0], detail_url)

    def test_list_page_ignores_cross_origin_mail_links(self):
        response = _Response({}, status_code=200)
        response.url = "https://mail.wordck.top/m/share-token?n=50"
        response.text = '<a class="mail" href="https://example.test/message-42">OpenAI</a>'
        session = MagicMock()
        session.get.return_value = response

        messages = fetch_legacy_messages("https://mail.wordck.top/m/share-token", session=session)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["source"], "legacy")
        self.assertEqual(session.get.call_count, 1)

    def test_list_page_does_not_fetch_unrelated_mail_details(self):
        response = _Response({}, status_code=200)
        response.url = "https://mail.wordck.top/m/share-token?n=50"
        response.text = '<a class="mail" href="/m/share-token/message-42">Unrelated newsletter</a>'
        session = MagicMock()
        session.get.return_value = response

        fetch_legacy_messages("https://mail.wordck.top/m/share-token", session=session)

        self.assertEqual(session.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
