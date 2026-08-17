# -*- coding: utf-8 -*-
"""Read a mailbox and classify OpenAI subscription/deactivation mail evidence."""
from __future__ import annotations

import html
import logging
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

from core.generic_api_mail_client import _parse_flysms_pickup_url

logger = logging.getLogger(__name__)
_MAILBOX_REQUEST_ATTEMPTS = 4
_MAILBOX_RETRY_DELAY = 1.0
_RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}

_SPACE_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_OPENAI_RE = re.compile(r"(?:openai|chatgpt)", re.I)
_SUB_ID_RE = re.compile(r"\bsub_[a-z0-9_-]{6,}\b", re.I)
_PLUS_ACCOUNT_ID_RE = re.compile(
    r"chatgpt\.com/account/manage\?[^\s<>\"']*\baccount_id=([a-z0-9-]{8,})",
    re.I,
)


def _plain(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<(?:style|script)[^>]*>.*?</(?:style|script)>", " ", text, flags=re.I | re.S)
    text = _TAG_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _first(data: Any, *keys: str) -> Any:
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            if isinstance(value, dict):
                return value.get("address") or value.get("email") or value.get("name") or str(value)
            return value
    return ""


def _plus_confirmation_account_id(*values: Any) -> str:
    text = html.unescape(" ".join(str(value or "") for value in values))
    match = _PLUS_ACCOUNT_ID_RE.search(text)
    return match.group(1) if match else ""


def _message(raw: dict, source: str, index: int = 0) -> dict:
    body = _first(raw, "body", "html", "text", "content", "body_html", "bodyHtml")
    if isinstance(body, dict):
        body = _first(body, "html", "text", "content")
    subject = _first(raw, "subject", "title")
    preview = _first(raw, "preview", "snippet", "summary")
    return {
        "mail_id": str(_first(raw, "uid", "id", "message_id", "messageId") or f"mail-{index}"),
        "subject": _plain(subject),
        "from": _plain(_first(raw, "from", "fromAddress", "sender")),
        "date": str(_first(raw, "date", "received_at", "receivedAt", "time", "created_at") or ""),
        "preview": _plain(preview),
        "body": _plain(body),
        "account_id": _plus_confirmation_account_id(subject, preview, body),
        "source": source,
    }


class _LegacyMailboxParser(HTMLParser):
    """Parse the small card layout returned by legacy iCloud mailbox links."""

    _FIELDS = {"su": "subject", "fr": "from", "dt": "date", "bd": "body"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[dict] = []
        self._depth = 0
        self._card_depth: int | None = None
        self._field: str | None = None
        self._field_depth: int | None = None
        self._current: dict[str, str] | None = None

    @staticmethod
    def _classes(attrs) -> set[str]:
        return set(dict(attrs).get("class", "").split())

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "img", "meta", "link", "input", "hr"}:
            if tag == "br" and self._current is not None and self._field:
                self._current[self._field] = self._current.get(self._field, "") + "\n"
            return
        self._depth += 1
        classes = self._classes(attrs)
        if self._current is None and "card" in classes:
            self._current = {}
            self._card_depth = self._depth
        if self._current is not None:
            for cls, field in self._FIELDS.items():
                if cls in classes:
                    self._field = field
                    self._field_depth = self._depth
                    break

    def handle_data(self, data):
        if self._current is not None and self._field:
            self._current[self._field] = self._current.get(self._field, "") + data

    def handle_endtag(self, tag):
        if self._field_depth == self._depth:
            self._field = None
            self._field_depth = None
        if self._card_depth == self._depth and self._current is not None:
            if any(self._current.values()):
                self.messages.append(self._current)
            self._current = None
            self._card_depth = None
            self._field = None
            self._field_depth = None
        self._depth = max(0, self._depth - 1)


def parse_legacy_mailbox_html(text: str) -> list[dict]:
    # The message body is a complete HTML document and may itself contain
    # unbalanced/legacy markup. Split outer cards first so one mail can never
    # absorb the next card's subject/date.
    starts = list(re.finditer(r'<div\s+class=["\']card["\']\s*>', text or "", flags=re.I))
    if starts:
        parsed = []
        for index, start in enumerate(starts):
            chunk = (text or "")[start.end(): starts[index + 1].start() if index + 1 < len(starts) else len(text or "")]
            raw = {}
            for css, field in _LegacyMailboxParser._FIELDS.items():
                match = re.search(rf'<div\s+class=["\']{css}["\']\s*>(.*?)</div>', chunk, flags=re.I | re.S)
                if match:
                    raw[field] = match.group(1)
            # Body HTML frequently contains divs. Capture it to the card boundary,
            # not merely to the first inner closing div.
            body_start = re.search(r'<div\s+class=["\']bd["\']\s*>', chunk, flags=re.I)
            if body_start:
                raw["body"] = chunk[body_start.end():]
            if any(raw.values()):
                parsed.append(_message(raw, "legacy", index))
        if parsed:
            return parsed
    parser = _LegacyMailboxParser()
    parser.feed(text or "")
    return [_message(item, "legacy", i) for i, item in enumerate(parser.messages)]


def _response_json(resp) -> dict:
    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError("邮箱接口返回的不是 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("邮箱接口返回格式不正确")
    return data


def _mailbox_get(client, url: str, **kwargs):
    """Retry transient mailbox network failures before reporting a check error."""
    last_error: Exception | None = None
    for attempt in range(1, _MAILBOX_REQUEST_ATTEMPTS + 1):
        try:
            response = client.get(url, **kwargs)
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code not in _RETRYABLE_HTTP_STATUS:
                return response
            last_error = RuntimeError(f"邮箱服务临时返回 HTTP {status_code}")
        except (requests.RequestException, TimeoutError, ConnectionError) as exc:
            last_error = exc
        if attempt < _MAILBOX_REQUEST_ATTEMPTS:
            wait_seconds = _MAILBOX_RETRY_DELAY * attempt
            logger.warning(
                "邮箱读取临时失败，第 %s/%s 次，%.1f 秒后重试: %s: %s",
                attempt,
                _MAILBOX_REQUEST_ATTEMPTS,
                wait_seconds,
                type(last_error).__name__,
                last_error,
            )
            time.sleep(wait_seconds)
    raise RuntimeError(
        f"邮箱读取连续 {_MAILBOX_REQUEST_ATTEMPTS} 次失败: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def fetch_flysms_messages(code_url: str, *, session=None, limit: int = 50) -> list[dict]:
    parsed = _parse_flysms_pickup_url(code_url)
    if not parsed:
        raise ValueError("不是 FlySMS pickup 链接")
    api_url, mailbox, token = parsed
    client = session or requests.Session()
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Mailbox-Email": mailbox,
        "User-Agent": "Mozilla/5.0 GPT-Registrator-Mail-Status/1.0",
    }
    resp = _mailbox_get(client, api_url, params={"limit": max(1, min(100, int(limit)))}, headers=headers, timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"FlySMS 邮件列表 HTTP {resp.status_code}")
    data = _response_json(resp)
    raw_items = data.get("messages") or data.get("items") or data.get("data") or []
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("messages") or raw_items.get("items") or []
    if not isinstance(raw_items, list):
        raise RuntimeError("FlySMS 邮件列表格式不正确")

    result: list[dict] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            continue
        item = _message(raw, "flysms", index)
        candidate_text = " ".join((item["subject"], item["from"], item["preview"], item["body"]))
        # List responses often only include a preview. Fetch full content only for OpenAI candidates.
        if item["mail_id"] and _OPENAI_RE.search(candidate_text) and not item["body"]:
            detail_url = f"{api_url.rstrip('/')}/{item['mail_id']}"
            detail_resp = _mailbox_get(
                client,
                detail_url,
                params={"mailbox": mailbox},
                headers=headers,
                timeout=25,
            )
            if detail_resp.status_code == 200:
                detail_data = _response_json(detail_resp)
                detail_raw = detail_data.get("message") or detail_data.get("data") or detail_data
                if isinstance(detail_raw, dict):
                    detailed = _message(detail_raw, "flysms", index)
                    for key in ("subject", "from", "date", "preview", "body", "account_id"):
                        if detailed.get(key):
                            item[key] = detailed[key]
        result.append(item)
    return result


def _legacy_url(code_url: str, limit: int) -> str:
    parsed = urlparse(code_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["n"] = str(max(1, min(100, int(limit))))
    return urlunparse(parsed._replace(query=urlencode(query)))


def _wordck_message_urls(text: str, list_url: str, limit: int) -> list[str]:
    urls = []
    parsed_list = urlparse(list_url)
    message_prefix = parsed_list.path.rstrip("/") + "/"
    for tag, content in re.findall(r"(<a\b[^>]*>)(.*?)</a>", text or "", flags=re.I | re.S):
        class_match = re.search(r'''\bclass=["']([^"']*)["']''', tag, flags=re.I)
        if not class_match or "mail" not in class_match.group(1).split():
            continue
        if not _OPENAI_RE.search(_plain(content)):
            continue
        href_match = re.search(r'''\bhref=["']([^"']+)["']''', tag, flags=re.I)
        if not href_match:
            continue
        detail_url = urljoin(list_url, html.unescape(href_match.group(1)))
        parsed_detail = urlparse(detail_url)
        if (
            parsed_detail.scheme not in {"http", "https"}
            or parsed_detail.netloc != parsed_list.netloc
            or not parsed_detail.path.startswith(message_prefix)
        ):
            continue
        urls.append(detail_url)
        if len(urls) >= max(1, min(100, int(limit))):
            break
    return urls


def _parse_wordck_message_html(text: str, detail_url: str, index: int) -> dict:
    article_match = re.search(
        r'''<div\b[^>]*\bclass=["'][^"']*\barticle\b[^"']*["'][^>]*>(.*)</div>''',
        text or "",
        flags=re.I | re.S,
    )
    article = article_match.group(1) if article_match else text or ""
    subject_match = re.search(r"<h1\b[^>]*>(.*?)</h1>", article, flags=re.I | re.S)
    meta_values = [
        _plain(value)
        for value in re.findall(
            r'''<div\b[^>]*\bclass=["'][^"']*\bmeta\b[^"']*["'][^>]*>(.*?)</div>''',
            article,
            flags=re.I | re.S,
        )
    ]
    sender = next((value.split("：", 1)[1] for value in meta_values if value.startswith("发件人：")), "")
    mail_date = next((value.split("：", 1)[1] for value in meta_values if value.startswith("时间：")), "")
    srcdoc_match = re.search(r'''\bsrcdoc=(["'])(.*?)\1''', article, flags=re.I | re.S)
    body = html.unescape(srcdoc_match.group(2)) if srcdoc_match else article
    raw = {
        "id": urlparse(detail_url).path.rstrip("/").rsplit("/", 1)[-1] or f"mail-{index}",
        "subject": subject_match.group(1) if subject_match else "",
        "from": sender,
        "date": mail_date,
        "body": body,
    }
    return _message(raw, "wordck", index)


def _fetch_wordck_messages(client, response, list_url: str, limit: int) -> list[dict]:
    detail_urls = _wordck_message_urls(response.text or "", list_url, limit)
    if not detail_urls:
        return []
    headers = {"Accept": "text/html,application/xhtml+xml", "User-Agent": "Mozilla/5.0"}
    messages = []
    for index, detail_url in enumerate(detail_urls):
        detail_resp = _mailbox_get(client, detail_url, headers=headers, timeout=25)
        if detail_resp.status_code != 200:
            logger.warning("邮箱详情页面 HTTP %s: %s", detail_resp.status_code, detail_url)
            continue
        messages.append(_parse_wordck_message_html(detail_resp.text or "", detail_url, index))
    return messages


def fetch_legacy_messages(code_url: str, *, session=None, limit: int = 50) -> list[dict]:
    client = session or requests.Session()
    list_url = _legacy_url(code_url, limit)
    resp = _mailbox_get(
        client,
        list_url,
        headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": "Mozilla/5.0"},
        timeout=25,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"邮箱页面 HTTP {resp.status_code}")
    messages = parse_legacy_mailbox_html(resp.text or "")
    if not messages:
        messages = _fetch_wordck_messages(client, resp, getattr(resp, "url", "") or list_url, limit)
    if not messages and (resp.text or "").strip():
        # Some compatible services return one unstructured HTML page.
        messages = [{
            "mail_id": "page", "subject": "", "from": "", "date": "",
            "preview": "", "body": _plain(resp.text), "source": "legacy",
        }]
    return messages


def fetch_mailbox_messages(code_url: str, *, session=None, limit: int = 50) -> list[dict]:
    if _parse_flysms_pickup_url(code_url):
        return fetch_flysms_messages(code_url, session=session, limit=limit)
    return fetch_legacy_messages(code_url, session=session, limit=limit)


_BANNED_PATTERNS = (
    r"access\s+(?:has\s+been\s+)?(?:deactivated|disabled|terminated|suspended)",
    r"account\s+(?:has\s+been\s+|was\s+)?(?:deactivated|disabled|terminated|suspended)",
    r"account\s+can\s+no\s+longer\s+be\s+used", r"initiate\s+(?:an\s+)?appeal",
    r"账号.{0,8}(?:已被|被|遭到)?(?:停用|禁用|封禁|冻结)", r"(?:访问|存取权).{0,8}(?:停用|禁用|撤销)",
    r"(?:无法|不能).{0,8}(?:继续|再).{0,8}使用", r"(?:发起|提出|提交).{0,4}申诉",
    r"(?:アクセス|アカウント).{0,12}(?:無効|停止|凍結)", r"(?:異議|不服).{0,6}(?:申し立て|申立)",
    r"cuenta.{0,12}(?:desactivada|suspendida)", r"compte.{0,12}(?:désactivé|suspendu)",
    r"konto.{0,12}(?:deaktiviert|gesperrt)",
)
_PLUS_PLAN_PATTERNS = (
    r"chatgpt\s*plus\s*subscription", r"chatgptplusplan", r"chatgpt\s*plus\s*に",
    r"chatgpt\s*plus.{0,12}(?:plan|プラン|套餐|方案|abonnement)",
)
_PLUS_SUCCESS_PATTERNS = (
    r"successfully\s+(?:subscribed|registered)", r"(?:subscribed|registered).{0,35}chatgpt\s*plus",
    r"chatgpt\s*plus\s*に正常に登録", r"chatgpt\s*plus.{0,16}(?:登録|購読).{0,10}(?:完了|成功|されました)",
    r"(?:成功订阅|已成功订阅|订阅成功|成功注册).{0,18}chatgpt\s*plus",
    r"chatgpt\s*plus.{0,18}(?:成功订阅|已开通|订阅成功)",
    r"(?:suscripci[oó]n|abonnement).{0,30}chatgpt\s*plus",
)
_PLUS_MANAGE_PATTERNS = (
    r"manage\s+(?:my\s+|your\s+)?subscription", r"サブスクリプションの管理", r"管理(?:我的|您的)?订阅",
)
_PLUS_TITLE_PATTERNS = (
    r"chatgpt\s*[-–:]?\s*your\s+new\s+plan", r"chatgpt\s*[-–:]?\s*新しいプラン",
    r"chatgpt\s*[-–:]?\s*(?:新套餐|您的新方案|你的新方案)",
)


def _matches_any(patterns, text: str) -> bool:
    return any(re.search(pattern, text, flags=re.I | re.S) for pattern in patterns)


def classify_mailbox(messages: list[dict]) -> dict:
    """Classify fetched messages. Deactivation always overrides older Plus evidence."""
    plus_match = None
    for item in messages:
        subject = _plain(item.get("subject"))
        sender = _plain(item.get("from"))
        raw_preview = str(item.get("preview") or "")
        raw_body = str(item.get("body") or "")
        body = _plain(" ".join((raw_preview, raw_body)))
        text = " ".join((subject, sender, body)).casefold()
        if not _OPENAI_RE.search(text):
            continue
        if _matches_any(_BANNED_PATTERNS, text):
            return {
                "status": "banned", "label": "账号被封禁", "evidence": "OpenAI 账号停用/申诉邮件",
                "subject": subject, "mail_date": item.get("date") or "", "mail_id": item.get("mail_id") or "",
                "mail_source": item.get("source") or "", "account_id": "",
            }
        has_plan = _matches_any(_PLUS_PLAN_PATTERNS, text)
        has_success = _matches_any(_PLUS_SUCCESS_PATTERNS, text)
        has_manage = _matches_any(_PLUS_MANAGE_PATTERNS, text)
        has_title = _matches_any(_PLUS_TITLE_PATTERNS, subject.casefold())
        has_sub_id = bool(_SUB_ID_RE.search(text))
        # Require multiple independent purchase/activation signals to reject marketing mail.
        if has_plan and (has_success or has_manage or has_title or has_sub_id):
            reasons = []
            if has_plan: reasons.append("Plus 套餐")
            if has_success: reasons.append("订阅成功")
            if has_sub_id: reasons.append("订阅订单号")
            if has_manage: reasons.append("订阅管理")
            if has_title: reasons.append("新套餐标题")
            plus_match = {
                "status": "plus", "label": "Plus", "evidence": "、".join(reasons),
                "subject": subject, "mail_date": item.get("date") or "", "mail_id": item.get("mail_id") or "",
                "mail_source": item.get("source") or "",
                "account_id": item.get("account_id") or _plus_confirmation_account_id(
                    subject, raw_preview, raw_body
                ),
            }
    if plus_match:
        return plus_match
    source = next((str(m.get("source") or "") for m in messages if isinstance(m, dict)), "")
    return {
        "status": "nonplus", "label": "非Plus", "evidence": "未找到 Plus 订阅成功邮件",
        "subject": "", "mail_date": "", "mail_id": "", "mail_source": source, "account_id": "",
    }


def detect_mailbox_status(code_url: str, *, session=None, limit: int = 50) -> dict:
    messages = fetch_mailbox_messages(code_url, session=session, limit=limit)
    result = classify_mailbox(messages)
    result["message_count"] = len(messages)
    return result
