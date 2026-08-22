# -*- coding: utf-8 -*-
"""
通用 API 取码邮箱客户端。

邮箱池导入格式：
    email----code_url

注册时领取 email；取码时直接 GET code_url，并从响应中提取 6 位验证码。
响应可以是纯文本、HTML 或 JSON，只要其中包含 6 位验证码即可。
"""
import json
import logging
import re
import time
import base64
import ast
import html as html_lib
from datetime import datetime
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, quote, unquote, urlparse, urlunparse

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp, mask_otp, redact_otp_text

logger = logging.getLogger(__name__)

_CODE_REGEX = re.compile(r"\b(\d{6})\b")
_CONTEXT_WORDS = ("code", "verify", "verification", "验证码", "代码", "确认码", "認証", "コード")
_CONTEXT_CACHE: dict[str, "GenericApiEmailAccount"] = {}
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_FILE = _PROJECT_ROOT / "用于注册的API邮箱.txt"
_YANGYANG_MESSAGES_RE = re.compile(r"/messages/([^/]+)/([^/?#]+)", re.IGNORECASE)
_YANGYANG_OPENAI_SUBJECT_HINTS = (
    "temporary chatgpt",
    "chatgpt verification code",
    "chatgpt login code",
    "临时 chatgpt",
    "chatgpt 登录代码",
    "chatgpt 验证码",
    "一時的な認証コード",
    "一時ログインコード",
)


class GenericApiMailError(RuntimeError):
    """通用 API 取码邮箱错误。"""


class GenericApiTransportError(GenericApiMailError):
    """取码端点经主请求+短重试后仍不可达，上层应停止盲目重发 OTP。"""


@dataclass
class GenericApiEmailAccount:
    email: str
    code_url: str


def _flatten_json(obj) -> str:
    parts: list[str] = []
    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x is not None:
            parts.append(str(x))
    walk(obj)
    return "\n".join(parts)


def _decode_data_uri(text: str) -> str:
    """把 data:text/html;base64,... 正文解码成可抽取 OTP 的 HTML/文本。"""
    if not isinstance(text, str):
        return ""
    if not text.startswith("data:"):
        return text
    try:
        _meta, payload = text.split(",", 1)
    except ValueError:
        return text
    if ";base64" in _meta.lower():
        try:
            return base64.b64decode(payload).decode("utf-8", errors="replace")
        except Exception:
            return text
    try:
        from urllib.parse import unquote_to_bytes
        return unquote_to_bytes(payload).decode("utf-8", errors="replace")
    except Exception:
        return text


def _extract_script_embedded_html(text: str) -> list[str]:
    """提取取码页脚本字符串中的邮件 HTML。

    api798 等取码页把真实邮件放在 ``htmlContent = "..."`` 的 JavaScript
    字符串里，再通过 iframe ``srcdoc`` 渲染。直接删除 ``<script>`` 会把
    邮件正文连同验证码一起删掉，所以先解码这些常见的 HTML 变量。
    """
    if not isinstance(text, str) or not text:
        return []

    values: list[str] = []
    assignment = r"(?:htmlContent|emailHtml|mailHtml|bodyHtml|srcdoc)\s*=\s*"
    patterns = (
        re.compile(assignment + r'"((?:\\.|[^"\\])*)"', flags=re.DOTALL | re.IGNORECASE),
        re.compile(assignment + r"'((?:\\.|[^'\\])*)'", flags=re.DOTALL | re.IGNORECASE),
    )
    for pattern in patterns:
        for match in pattern.finditer(text):
            raw = match.group(1)
            try:
                # JSON decoding handles the JavaScript escapes used by the
                # endpoint (``\\r\\n``, ``\\uXXXX``, escaped quotes, etc.).
                decoded = json.loads('"' + raw + '"')
            except (TypeError, ValueError, json.JSONDecodeError):
                try:
                    decoded = ast.literal_eval("'" + raw.replace("'", "\\'") + "'")
                except (SyntaxError, ValueError):
                    continue
            if isinstance(decoded, str) and decoded.strip():
                values.append(decoded)
    return values


def _extract_code(text: str) -> str | None:
    """从纯文本/HTML/JSON 文本中提取 6 位 OTP。"""
    if not text:
        return None

    # 兼容 JSON：优先把所有 value 拉平再抽取。部分取码页把邮件正文
    # 放进 JavaScript 的 htmlContent/srcdoc 字符串，必须在移除 script
    # 标签前先恢复正文，否则 HTTP 200 页面里虽然有验证码也会取不到。
    candidates_text = [*_extract_script_embedded_html(text), _decode_data_uri(text), text]
    try:
        parsed = json.loads(text)
        candidates_text.insert(0, _decode_data_uri(_flatten_json(parsed)))
    except Exception:
        pass

    for body in candidates_text:
        # 通用取码页经常把邮件和整页 CSS 一起返回。颜色值（例如
        # ``#171717``）不能参与 OTP 提取，否则会稳定覆盖正文中的真实验证码。
        body = re.sub(r"<style\b[^>]*>.*?</style>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<script\b[^>]*>.*?</script>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = html_lib.unescape(re.sub(r"<[^>]+>", " ", body))
        body = re.sub(r"\s+", " ", body).strip()
        # 复用邮件 OTP 抽取逻辑。
        code = extract_otp({"text": body, "content": body, "subject": body[:200]})
        if code:
            return code

        codes = _CODE_REGEX.findall(body)
        if not codes:
            continue
        lower = body.lower()
        for code in codes:
            idx = lower.find(code)
            window = lower[max(0, idx - 80): idx + 86]
            if any(w.lower() in window for w in _CONTEXT_WORDS):
                return code
        return codes[-1]
    return None


def _extract_yangyang_openai_code(subject: str, body: str) -> str | None:
    """
    yangyang 邮件详情里 OpenAI 模板常混入多个 6 位数字：
    - 202123 / 353740 这类 CSS/模板数字
    - 真正 OTP 在 “Your code is / code:” 附近，通常是正文最后一个业务 6 位数
    所以不能直接复用通用 _extract_code 的“第一个上下文命中”。
    """
    body = _decode_data_uri(body or "")
    subject_l = (subject or "").lower()
    text = "\n".join([subject or "", body])

    # 去掉 style/script，减少 CSS 颜色、宽高等 6 位数字干扰。
    clean = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<script[^>]*>.*?</script>", " ", clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"#[0-9a-fA-F]{6}\b", " ", clean)
    clean = re.sub(r"(?:color|background|border|width|height|font-size|line-height)\s*:\s*[^;\"']+", " ", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    codes = _CODE_REGEX.findall(clean)
    if not codes:
        return None

    # 过滤已知模板噪声；保留其它 6 位候选。
    noise = {"000000", "202123", "353740"}
    candidates = [c for c in codes if c not in noise]
    if not candidates:
        candidates = codes

    lower = clean.lower()
    patterns = (
        r"(?:code is|code:|verification code is|login code is|your code is)\D{0,80}(\d{6})",
        r"(?:验证码|驗證碼|登录代码|登入代碼|確認コード|認証コード|ログインコード)\D{0,80}(\d{6})",
        r"(\d{6})\D{0,80}(?:code|验证码|驗證碼|確認コード|認証コード)",
    )
    for pat in patterns:
        matches = re.findall(pat, clean, flags=re.IGNORECASE)
        matches = [m for m in matches if m not in noise]
        if matches:
            return matches[-1]

    # OpenAI 临时代码邮件：清理噪声后最后一个业务 6 位数最稳定。
    if any(h in subject_l for h in _YANGYANG_OPENAI_SUBJECT_HINTS) or "openai" in lower or "chatgpt" in lower:
        return candidates[-1]

    return _extract_code(clean)


def _parse_yangyang_code_url(code_url: str) -> tuple[str, str, str] | None:
    """
    解析 yangyang.website 这类邮箱页面：
        /messages/{token}/{email}
    返回 (origin, token, email)。
    """
    try:
        parsed = urlparse(code_url)
    except Exception:
        return None
    m = _YANGYANG_MESSAGES_RE.search(parsed.path or "")
    if not m:
        return None
    origin = urlunparse((parsed.scheme or "http", parsed.netloc, "", "", "", ""))
    token = unquote(m.group(1))
    email = unquote(m.group(2))
    if not origin or not token or not email:
        return None
    return origin.rstrip("/"), token, email



def _parse_flysms_pickup_url(code_url: str) -> tuple[str, str, str] | None:
    """解析 flysms 类 iCloud 网页取件链接。

    支持：
      https://flysms.xyz/icloud/pickup#email=xx&key=tok_xx
      https://flysms.xyz/icloud/pickup?email=xx&key=tok_xx

    仅在 path 命中 /icloud/pickup 时生效，不影响 yangyang / 普通 GET 取码。
    返回 (api_messages_url, email, token)。
    """
    try:
        parsed = urlparse(str(code_url or "").strip())
    except Exception:
        return None
    path = (parsed.path or "").rstrip("/")
    if not path.lower().endswith("/icloud/pickup") and path.lower() != "icloud/pickup":
        # 兼容带前缀路径：/xxx/icloud/pickup
        if "/icloud/pickup" not in path.lower():
            return None

    # 参数可能在 fragment（SPA）或 query
    params = {}
    if parsed.fragment:
        params.update(dict(parse_qsl(parsed.fragment, keep_blank_values=True)))
    if parsed.query:
        params.update(dict(parse_qsl(parsed.query, keep_blank_values=True)))

    email = (
        params.get("email")
        or params.get("mail")
        or params.get("mailbox")
        or ""
    ).strip()
    token = (
        params.get("key")
        or params.get("token")
        or params.get("auth")
        or params.get("access_token")
        or ""
    ).strip()
    email = unquote(email)
    token = unquote(token)
    if not email or "@" not in email or not token:
        return None

    # 网页路径 /icloud/pickup -> API /icloud/api/pickup/messages
    # 若 path 形如 /prefix/icloud/pickup，则保留 prefix。
    lower = path.lower()
    idx = lower.rfind("/icloud/pickup")
    if idx < 0:
        base_path = "/icloud"
    else:
        base_path = path[:idx] + "/icloud"
    origin = urlunparse((parsed.scheme or "https", parsed.netloc, "", "", "", "")).rstrip("/")
    if not origin or not parsed.netloc:
        return None
    api_url = f"{origin}{base_path}/api/pickup/messages"
    return api_url, email, token


def _fetch_flysms_otp(
    session: requests.Session,
    code_url: str,
    headers: dict,
    after_ts: float | None = None,
    expected_email: str | None = None,
    request_timeout: float | None = None,
) -> tuple[str, dict] | None:
    """从 flysms pickup messages API 提取最新 6 位验证码。"""
    parsed = _parse_flysms_pickup_url(code_url)
    if not parsed:
        return None
    api_url, mail_email, token = parsed
    if expected_email and mail_email.lower() != str(expected_email).strip().lower():
        logger.warning(
            "[GenericAPI] flysms 链接邮箱与任务邮箱不一致: link=%s task=%s",
            mail_email,
            expected_email,
        )

    req_headers = {
        **headers,
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Mailbox-Email": mail_email,
    }
    timeout = max(1.0, min(20.0, float(request_timeout if request_timeout is not None else 20.0)))
    try:
        resp = session.get(api_url, headers=req_headers, timeout=timeout, verify=False)
    except Exception as exc:
        logger.debug("[GenericAPI] flysms 取码请求失败: %s: %s", type(exc).__name__, redact_otp_text(exc))
        return None
    if resp.status_code != 200:
        logger.debug(
            "[GenericAPI] flysms 取码 HTTP %s: %s",
            resp.status_code,
            redact_otp_text((resp.text or "")[:160]),
        )
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    messages = data.get("messages") or data.get("items") or []
    if not isinstance(messages, list):
        return None

    items: list[dict] = []
    for idx, item in enumerate(messages):
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "")
        preview = str(item.get("preview") or item.get("snippet") or "")
        body = str(item.get("body") or item.get("text") or item.get("html") or "")
        received_at = (
            item.get("date")
            or item.get("received_at")
            or item.get("receivedAt")
            or item.get("time")
        )
        msg_ts = _parse_generic_api_ts(received_at) or 0.0
        mail_id = item.get("uid") or item.get("id") or f"flysms-{idx}"
        items.append({
            "mail_id": mail_id,
            "subject": subject,
            "preview": preview,
            "body": body,
            "received_at": received_at,
            "msg_ts": msg_ts,
            "from": item.get("from") or item.get("fromAddress") or "",
        })

    # 新邮件优先
    items.sort(key=lambda x: float(x.get("msg_ts") or 0.0), reverse=True)
    for item in items:
        msg_ts = float(item.get("msg_ts") or 0.0)
        if after_ts and msg_ts and msg_ts + 2 < after_ts:
            logger.debug(
                "[GenericAPI] flysms 跳过旧邮件: id=%s ts=%s after=%s subject=%r",
                item.get("mail_id"),
                item.get("received_at"),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(after_ts)),
                item.get("subject") or "",
            )
            continue
        subject = str(item.get("subject") or "")
        text_blob = "\n".join([
            subject,
            str(item.get("preview") or ""),
            str(item.get("body") or ""),
            str(item.get("from") or ""),
        ])
        code = _extract_yangyang_openai_code(subject, text_blob) or _extract_code(text_blob)
        if code:
            logger.info(
                "[GenericAPI] flysms 提取到 OTP=%s, mail_id=%s, ts=%s, subject=%r",
                mask_otp(code),
                item.get("mail_id"),
                item.get("received_at"),
                subject[:80],
            )
            return code, {
                "mail_id": item.get("mail_id"),
                "received_at": item.get("received_at"),
                "subject": subject,
                "msg_ts": msg_ts,
                "source": "flysms",
            }
    return None


def _parse_yangyang_ts(value: str | None) -> float | None:
    if not value:
        return None
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw[:19], fmt).timestamp()
        except Exception:
            pass
    return None


def _parse_generic_api_ts(value) -> float | None:
    """解析通用 API 返回的时间字段，兼容 ISO8601/Z 和常见本地时间格式。"""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    # 数字时间戳：秒 / 毫秒
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        try:
            ts = float(raw)
            return ts / 1000.0 if ts > 10_000_000_000 else ts
        except Exception:
            return None
    # ISO8601: 2026-08-05T01:10:17.000Z
    try:
        iso = raw
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            return dt.timestamp()
        return dt.timestamp()
    except Exception:
        pass
    # 常见字符串格式
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw[:19], fmt).timestamp()
        except Exception:
            pass
    # 邮件 HTML 常见 RFC 2822 日期：Mon, 10 Aug 2026 04:57:49 +0000 (UTC)
    try:
        return parsedate_to_datetime(raw).timestamp()
    except Exception:
        pass
    return None


def _extract_structured_api_code(text: str, after_ts: float | None = None) -> tuple[str, dict] | None:
    """
    兼容 newzoe 这类直接返回 JSON 的取码接口：
      {"code":"784207","from":"...","subject":"Your temporary ChatGPT login code","time":"2026-08-05T01:10:17.000Z"}

    如果响应里有 time/date/received_at，会按 after_ts 过滤旧码，避免拿到上一次缓存验证码。
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    # 常见字段优先级：code / otp / verification_code；没有再回退从拉平文本提取。
    raw_code = (
        data.get("code")
        or data.get("otp")
        or data.get("verification_code")
        or data.get("verificationCode")
        or data.get("email_code")
        or data.get("emailCode")
    )
    code = None
    if raw_code is not None:
        m = _CODE_REGEX.search(str(raw_code))
        if m:
            code = m.group(1)
    if not code:
        code = _extract_code(_flatten_json(data))
    if not code:
        return None

    ts_raw = (
        data.get("time")
        or data.get("date")
        or data.get("received_at")
        or data.get("receivedAt")
        or data.get("created_at")
        or data.get("createdAt")
        or data.get("timestamp")
    )
    msg_ts = _parse_generic_api_ts(ts_raw)
    if after_ts and msg_ts and msg_ts + 2 < after_ts:
        logger.debug(
            "[GenericAPI] structured API 跳过旧验证码: code=%s ts=%s after=%s subject=%r",
            mask_otp(code),
            ts_raw,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(after_ts)),
            str(data.get("subject") or "")[:80],
        )
        return None

    return code, {
        "source": "structured_api",
        "received_at": ts_raw,
        "msg_ts": msg_ts,
        "subject": data.get("subject"),
        "from": data.get("from") or data.get("fromAddress") or data.get("sender"),
    }


def _fetch_yangyang_otp(
    session: requests.Session,
    code_url: str,
    headers: dict,
    after_ts: float | None = None,
    request_timeout: float | None = None,
) -> tuple[str, dict] | None:
    """从 yangyang 邮箱页面的列表 API + 详情 API 中抽取最新 6 位验证码。"""
    parsed = _parse_yangyang_code_url(code_url)
    if not parsed:
        return None
    origin, token, email = parsed
    token_q = quote(token, safe="")
    email_q = quote(email, safe="@._+-")
    api_url = f"{origin}/api/messages/{token_q}/{email_q}"
    timeout = max(1.0, min(20.0, float(request_timeout if request_timeout is not None else 20.0)))

    items: list[dict] = []
    cursor: str | None = None
    # 一般第一页足够；保守支持最多翻 5 页。
    for _ in range(5):
        url = api_url if not cursor else f"{api_url}?cursor={quote(str(cursor), safe='')}"
        resp = session.get(url, headers={**headers, "Accept": "application/json"}, timeout=timeout, verify=False)
        if resp.status_code != 200:
            if resp.status_code == 404:
                # 兼容 mail.ai1998.xyz 这类同样是 /messages/{token}/{email}，
                # 但没有 /api/messages，邮件直接内嵌在 HTML 页面中的实现。
                return _fetch_inline_messages_page_otp(
                    session=session,
                    code_url=code_url,
                    headers=headers,
                    after_ts=after_ts,
                    request_timeout=timeout,
                )
            logger.debug(
                "[GenericAPI] yangyang 邮件列表 HTTP %s: %s",
                resp.status_code,
                redact_otp_text((resp.text or "")[:160]),
            )
            return None
        data = resp.json()
        page_items = data.get("items") or []
        if isinstance(page_items, list):
            items.extend([x for x in page_items if isinstance(x, dict)])
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = str(data.get("next_cursor"))

    # API 默认新邮件在前；再次按时间倒序，尽量取最新验证码。
    items.sort(key=lambda x: _parse_yangyang_ts(x.get("received_at") or x.get("receivedAt")) or 0, reverse=True)
    for item in items:
        msg_ts_raw = item.get("received_at") or item.get("receivedAt")
        msg_ts = _parse_yangyang_ts(msg_ts_raw)
        if after_ts and msg_ts and msg_ts + 2 < after_ts:
            logger.debug(
                "[GenericAPI] yangyang 跳过旧邮件: id=%s ts=%s after=%s subject=%r",
                item.get("id"), msg_ts_raw, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(after_ts)),
                item.get("subject") or "",
            )
            continue
        msg_id = item.get("id")
        if not msg_id:
            continue
        detail_url = f"{origin}/message/{quote(str(msg_id), safe='')}/{token_q}/{email_q}"
        try:
            detail_resp = session.get(detail_url, headers={**headers, "Accept": "application/json"}, timeout=timeout, verify=False)
            if detail_resp.status_code != 200:
                continue
            detail = detail_resp.json()
        except Exception as exc:
            logger.debug(
                "[GenericAPI] yangyang 邮件详情读取失败: %s: %s",
                type(exc).__name__,
                redact_otp_text(exc),
            )
            continue

        raw_body = str(detail.get("body") or "")
        body = _decode_data_uri(raw_body)
        subject = str(detail.get("subject") or item.get("subject") or "")
        text = "\n".join([
            subject,
            str(detail.get("fromAddress") or item.get("from_address") or ""),
            str(detail.get("receivedAt") or item.get("received_at") or ""),
            body,
        ])
        code = _extract_yangyang_openai_code(subject, body)
        if code:
            logger.info(
                f"[GenericAPI] yangyang 页面提取到 OTP={mask_otp(code)}, "
                f"mail_id={msg_id}, ts={detail.get('receivedAt') or item.get('received_at')}, subject={subject[:80]!r}"
            )
            return code, {
                "mail_id": msg_id,
                "received_at": detail.get("receivedAt") or item.get("received_at"),
                "subject": subject,
                "msg_ts": msg_ts,
            }
    return None


def _strip_html_fragment(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html_lib.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s+", "\n", value)
    return value.strip()


def _extract_inline_messages_html_otp(
    html: str,
    after_ts: float | None = None,
) -> tuple[str, dict] | None:
    """从邮件列表 HTML 中提取发送时间之后的新 OpenAI 验证码。

    同时兼容 ``article.mail-card``、``details``，以及 icloud-api.top 使用的
    ``div.card / .su / .dt / .bd`` 结构。识别出邮件卡片后不会再退回整页正则，
    从而避免把发送前的旧邮件验证码误当成新码。
    """
    html = str(html or "")
    cards = re.findall(
        r"<article\b[^>]*class=[\"'][^\"']*mail-card[^\"']*[\"'][^>]*>(.*?)</article>",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not cards:
        cards = re.findall(r"<details\b[^>]*>(.*?)</details>", html, flags=re.DOTALL | re.IGNORECASE)
    if not cards:
        card_starts = list(re.finditer(
            r"<div\b[^>]*class=[\"'][^\"']*\bcard\b[^\"']*[\"'][^>]*>",
            html,
            flags=re.IGNORECASE,
        ))
        for idx, match in enumerate(card_starts):
            end = card_starts[idx + 1].start() if idx + 1 < len(card_starts) else len(html)
            cards.append(html[match.end():end])

    items: list[dict] = []
    for idx, card in enumerate(cards):
        subject_m = re.search(
            r"<(?:span|div)\b[^>]*class=[\"'][^\"']*(?:subject|\bsu\b)[^\"']*[\"'][^>]*>(.*?)</(?:span|div)>",
            card,
            flags=re.DOTALL | re.IGNORECASE,
        )
        date_m = re.search(
            r"<(?:span|div)\b[^>]*class=[\"'][^\"']*(?:date|\bdt\b)[^\"']*[\"'][^>]*>(.*?)</(?:span|div)>",
            card,
            flags=re.DOTALL | re.IGNORECASE,
        )
        from_m = re.search(
            r"<div\b[^>]*class=[\"'][^\"']*(?:meta|\bfr\b)[^\"']*[\"'][^>]*>(.*?)</div>",
            card,
            flags=re.DOTALL | re.IGNORECASE,
        )
        subject = _strip_html_fragment(subject_m.group(1) if subject_m else "")
        received_at = _strip_html_fragment(date_m.group(1) if date_m else "")
        from_addr = _strip_html_fragment(from_m.group(1) if from_m else "")
        body = _strip_html_fragment(card)
        msg_ts = _parse_generic_api_ts(received_at)
        items.append({
            "mail_id": f"inline-{idx}",
            "subject": subject,
            "received_at": received_at,
            "from": from_addr,
            "body": body,
            "msg_ts": msg_ts or 0.0,
        })

    items.sort(key=lambda x: float(x.get("msg_ts") or 0.0), reverse=True)
    for item in items:
        msg_ts = float(item.get("msg_ts") or 0.0)
        if after_ts and msg_ts and msg_ts + 2 < after_ts:
            logger.debug(
                "[GenericAPI] inline messages 跳过发送前旧邮件: id=%s ts=%s after=%s subject=%r",
                item.get("mail_id"), item.get("received_at"),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(after_ts)),
                item.get("subject") or "",
            )
            continue
        code = _extract_yangyang_openai_code(str(item.get("subject") or ""), str(item.get("body") or ""))
        if code:
            return code, {
                "mail_id": item.get("mail_id"),
                "received_at": item.get("received_at"),
                "subject": item.get("subject"),
                "msg_ts": msg_ts,
                "source": "inline_html",
            }
    return None


def _fetch_inline_messages_page_otp(
    *,
    session: requests.Session,
    code_url: str,
    headers: dict,
    after_ts: float | None = None,
    request_timeout: float | None = None,
) -> tuple[str, dict] | None:
    """解析无 JSON API、直接把邮件卡片渲染在 HTML 里的 /messages 页面。"""
    timeout = max(1.0, min(20.0, float(request_timeout if request_timeout is not None else 20.0)))
    try:
        resp = session.get(
            code_url,
            headers={**headers, "Accept": "text/html,application/xhtml+xml,text/plain,*/*"},
            timeout=timeout,
            verify=False,
        )
        if resp.status_code != 200:
            logger.debug(
                "[GenericAPI] inline messages 页面 HTTP %s: %s",
                resp.status_code,
                redact_otp_text((resp.text or "")[:160]),
            )
            return None
        html = resp.text or ""
    except Exception as exc:
        logger.debug("[GenericAPI] inline messages 页面读取失败: %s: %s", type(exc).__name__, redact_otp_text(exc))
        return None

    result = _extract_inline_messages_html_otp(html, after_ts=after_ts)
    if result:
        code, meta = result
        logger.info(
            "[GenericAPI] inline messages 页面提取到新 OTP=%s, mail_id=%s, ts=%s, subject=%r",
            mask_otp(code), meta.get("mail_id"), meta.get("received_at"), str(meta.get("subject") or "")[:80],
        )
    return result


def pick_account(provider: str | None = "generic_api") -> GenericApiEmailAccount:
    """领取一个可用通用 API 邮箱。"""
    from core.db import (
        claim_next_generic_api_email,
        generic_api_email_pool_summary,
        quarantine_exhausted_generic_api_emails,
    )

    inserted, skipped = import_from_file()
    if inserted:
        logger.info(f"[GenericAPI] 已自动从 {_ACCOUNTS_FILE.name} 导入 {inserted} 个邮箱（跳过 {skipped} 个）")

    quarantined = quarantine_exhausted_generic_api_emails(
        max(1, int(getattr(_email_cfg, "GENERIC_API_REGISTRATION_FAILURE_LIMIT", 2) or 2)),
        provider=provider,
    )
    if quarantined:
        logger.warning("[GenericAPI] 已隔离 %s 个达到邮箱/OTP 失败上限的历史条目", quarantined)

    row = claim_next_generic_api_email(provider=provider)
    if row is None:
        summary = generic_api_email_pool_summary(provider=provider)
        raise GenericApiMailError(
            f"通用 API 邮箱池没有可用账号: {summary}. 请在 WebUI 邮箱池导入：邮箱----取码地址"
        )
    account = GenericApiEmailAccount(email=row["email"], code_url=row["code_url"])
    _CONTEXT_CACHE[account.email] = account
    logger.info(f"[GenericAPI] 选中邮箱: {account.email}（DB id={row.get('id')}）")
    return account


def import_from_file(path: str | Path | None = None) -> tuple[int, int]:
    """从文本文件导入通用 API 邮箱，每行：email----code_url 或 email====code_url。"""
    from core.db import import_generic_api_emails
    p = Path(path) if path else _ACCOUNTS_FILE
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    if not p.exists():
        return 0, 0
    records = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----") if "----" in line else line.split("====")
        parts = [x.strip() for x in parts]
        if len(parts) < 2:
            continue
        records.append({"email": parts[0], "code_url": parts[1]})
    return import_generic_api_emails(records)


def get_account_context(email: str) -> GenericApiEmailAccount | None:
    if email in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[email]
    from core.db import get_generic_api_email_by_email
    row = get_generic_api_email_by_email(email)
    if row is None:
        return None
    account = GenericApiEmailAccount(email=row["email"], code_url=row["code_url"])
    _CONTEXT_CACHE[email] = account
    return account


def snapshot_current_otp(email: str, timeout: float = 8.0) -> str | None:
    """读取取码地址当前已经存在的 OTP，供新注册轮次排除历史验证码。

    这里只做一次只读请求，不等待新邮件。部分通用取码页没有邮件时间字段，
    单靠 ``after_ts`` 无法识别缓存内容；在触发新验证码前记录当前值，后续把它
    放进 ``exclude_codes``，可以避免上一轮验证码被再次提交。
    """
    account = get_account_context(email)
    if account is None:
        return None

    headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (compatible; gpt-register/1.0)",
    }
    session = requests.Session()
    request_timeout = max(1.0, min(2.0, float(timeout or 2.0)))
    try:
        if _parse_yangyang_code_url(account.code_url) is not None:
            result = _fetch_yangyang_otp(
                session,
                account.code_url,
                headers,
                after_ts=None,
                request_timeout=request_timeout,
            )
            return result[0] if result else None
        if _parse_flysms_pickup_url(account.code_url) is not None:
            result = _fetch_flysms_otp(
                session,
                account.code_url,
                headers,
                after_ts=None,
                expected_email=email,
                request_timeout=request_timeout,
            )
            return result[0] if result else None

        resp = session.get(
            account.code_url,
            headers=headers,
            timeout=request_timeout,
            verify=False,
        )
        if resp.status_code != 200:
            return None
        text = resp.text or ""
        structured = _extract_structured_api_code(text, after_ts=None)
        inline = _extract_inline_messages_html_otp(text, after_ts=None)
        return structured[0] if structured else (inline[0] if inline else _extract_code(text))
    except Exception as exc:
        logger.debug("[GenericAPI] 读取历史 OTP 快照失败，继续注册：%s: %s", type(exc).__name__, redact_otp_text(exc))
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    from core.db import release_generic_api_email
    release_generic_api_email(email, status=status, note=note)
    _CONTEXT_CACHE.pop(email, None)


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    request_timeout: float | None = None,
    retry_timeout: float | None = None,
    max_consecutive_errors: int | None = None,
    exclude_codes: set[str] | list[str] | tuple[str, ...] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """
    轮询该邮箱配置的 code_url，直到提取到 6 位验证码或超时。

    settle 机制：首次拿到验证码后不立刻返回，而是继续等 OTP_SETTLE_SECONDS 秒。
    如果期间取码地址返回了不同验证码，则替换候选并重置 settle 倒计时；
    连续 settle 秒没有变化后才返回，避免取到接口缓存中的旧码。
    """
    account = get_account_context(email)
    if account is None:
        raise GenericApiMailError(f"通用 API 邮箱不存在或未导入: {email}")

    deadline = time.time() + (max_wait or _email_cfg.OTP_MAX_WAIT)
    interval = poll_interval or _email_cfg.OTP_POLL_INTERVAL
    settle = settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS
    excluded = {str(code).strip() for code in (exclude_codes or []) if str(code).strip()}
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (compatible; gpt-register/1.0)",
    }
    last_error = ""
    best_otp: str | None = None
    best_seen_at: float = 0.0
    settle_until: float | None = None
    consecutive_transport_errors = 0
    max_transport_errors = max(
        1,
        int(
            max_consecutive_errors
            if max_consecutive_errors is not None
            else getattr(_email_cfg, "GENERIC_API_MAX_CONSECUTIVE_ERRORS", 2)
            or 2
        ),
    )
    logger.info(
        f"[GenericAPI] 开始轮询取码地址: {email}，"
        f"最长 {max_wait or _email_cfg.OTP_MAX_WAIT}s, settle={settle}s"
    )
    is_yangyang = _parse_yangyang_code_url(account.code_url) is not None
    is_flysms = _parse_flysms_pickup_url(account.code_url) is not None
    if is_flysms:
        logger.info("[GenericAPI] 识别为 flysms iCloud pickup 链接，走 Bearer API 取码")

    def stop_requested() -> bool:
        if should_stop is None:
            return False
        try:
            return bool(should_stop())
        except Exception as exc:
            logger.debug("[GenericAPI] 检查浏览器 OTP 状态失败，继续取码：%s", redact_otp_text(exc))
            return False

    def excluded_code_is_stale(code: str | None, meta: dict | None = None) -> bool:
        """只有旧邮件/无时间戳响应里的排除码才继续排除。

        部分 iCloud 取件服务会在 OpenAI 重发后返回相同的 6 位码。只按数字永久
        排除会把本次请求之后的新邮件也丢掉，导致页面有可用码而 worker 空等。
        """
        code = str(code or "").strip()
        if not code or code not in excluded:
            return False
        meta = meta or {}
        try:
            msg_ts = float(meta.get("msg_ts") or 0.0)
        except (TypeError, ValueError):
            msg_ts = 0.0
        if not msg_ts:
            msg_ts = float(_parse_generic_api_ts(meta.get("received_at")) or 0.0)
        if after_ts and msg_ts and msg_ts + 2 >= float(after_ts):
            logger.info(
                "[GenericAPI] OTP=%s 虽与历史/拒绝码相同，但来自本次请求后的新邮件，允许重新提交",
                mask_otp(code),
            )
            return False
        return True

    # 一轮 OTP 等待只复用一个长连接；避免并发 worker 每次轮询都重做 TLS 握手。
    session = requests.Session()
    session.trust_env = False
    while time.time() < deadline:
        if stop_requested():
            raise GenericApiMailError("验证码页面已进入下一步，停止等待新验证码")
        # settle 已满足就直接返回，不再发起下一个可能占满 8s+5s 的请求。
        now = time.time()
        if best_otp and settle_until is not None and now >= settle_until:
            logger.info(
                f"[GenericAPI] settle 完成，返回 OTP={mask_otp(best_otp)}, "
                f"候选锁定时间={time.strftime('%H:%M:%S', time.localtime(best_seen_at))}"
            )
            return best_otp
        try:
            yy_result = _fetch_yangyang_otp(session, account.code_url, headers, after_ts=after_ts) if is_yangyang else None
            fly_result = None
            if (not yy_result) and is_flysms:
                fly_result = _fetch_flysms_otp(
                    session,
                    account.code_url,
                    headers,
                    after_ts=after_ts,
                    expected_email=email,
                )
            if yy_result and excluded_code_is_stale(yy_result[0], yy_result[1]):
                last_error = f"取码接口仍返回已被拒绝的旧验证码 {mask_otp(yy_result[0])}"
                yy_result = None
            if fly_result and excluded_code_is_stale(fly_result[0], fly_result[1]):
                last_error = f"取码接口仍返回已被拒绝的旧验证码 {mask_otp(fly_result[0])}"
                fly_result = None
            if yy_result:
                code, yy_meta = yy_result
                now_seen = time.time()
                if not best_otp:
                    best_otp = code
                    best_seen_at = now_seen
                    settle_until = now_seen + settle
                    logger.info(
                        f"[GenericAPI] 首次锁定 OTP={mask_otp(code)}, source=yangyang mail_id={yy_meta.get('mail_id')} ts={yy_meta.get('received_at')}, "
                        f"等 {settle}s 看取码接口是否出现更新验证码..."
                    )
                elif code != best_otp:
                    logger.info(
                        f"[GenericAPI] 发现更新 OTP={mask_otp(code)}, source=yangyang mail_id={yy_meta.get('mail_id')} ts={yy_meta.get('received_at')}，"
                        f"替换之前的 {mask_otp(best_otp)}, 重置 settle 计时"
                    )
                    best_otp = code
                    best_seen_at = now_seen
                    settle_until = now_seen + settle
                else:
                    logger.debug(f"[GenericAPI] 取码接口仍返回候选 OTP={mask_otp(best_otp)}")
                resp = None
                text = ""
            elif fly_result:
                code, fly_meta = fly_result
                now_seen = time.time()
                if not best_otp:
                    best_otp = code
                    best_seen_at = now_seen
                    settle_until = now_seen + settle
                    logger.info(
                        f"[GenericAPI] 首次锁定 OTP={mask_otp(code)}, source=flysms mail_id={fly_meta.get('mail_id')} ts={fly_meta.get('received_at')}, "
                        f"等 {settle}s 看取码接口是否出现更新验证码..."
                    )
                elif code != best_otp:
                    logger.info(
                        f"[GenericAPI] 发现更新 OTP={mask_otp(code)}, source=flysms mail_id={fly_meta.get('mail_id')} ts={fly_meta.get('received_at')}，"
                        f"替换之前的 {mask_otp(best_otp)}, 重置 settle 计时"
                    )
                    best_otp = code
                    best_seen_at = now_seen
                    settle_until = now_seen + settle
                else:
                    logger.debug(f"[GenericAPI] 取码接口仍返回候选 OTP={mask_otp(best_otp)}")
                resp = None
                text = ""
            else:
                if is_yangyang:
                    last_error = "yangyang 列表中尚未出现 after_ts 之后的新验证码邮件"
                    resp = None
                    text = ""
                elif is_flysms:
                    last_error = "flysms 列表中尚未出现 after_ts 之后的新验证码邮件"
                    resp = None
                    text = ""
                else:
                    request_budget = max(
                        1.0,
                        min(
                            float(
                                request_timeout
                                if request_timeout is not None
                                else getattr(_email_cfg, "GENERIC_API_REQUEST_TIMEOUT", 8)
                                or 8
                            ),
                            deadline - time.time(),
                        ),
                    )
                    try:
                        resp = session.get(account.code_url, headers=headers, timeout=request_budget, verify=False)
                    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                        # iCloud 取件页偶发一两秒的读超时；在本轮立即重试一次，
                        # 不消耗完整轮询间隔，也不会把旧 OTP 当作新 OTP。
                        remaining_after_error = deadline - time.time()
                        if remaining_after_error <= 2:
                            raise
                        retry_budget = max(
                            1.0,
                            min(
                                float(
                                    retry_timeout
                                    if retry_timeout is not None
                                    else getattr(_email_cfg, "GENERIC_API_RETRY_TIMEOUT", 5)
                                    or 5
                                ),
                                remaining_after_error,
                            ),
                        )
                        logger.warning(
                            "[GenericAPI] 取码接口瞬时网络失败，短间隔重试一次：%s: %s",
                            type(exc).__name__,
                            redact_otp_text(exc),
                        )
                        time.sleep(min(0.8, max(0.1, remaining_after_error / 10)))
                        resp = session.get(account.code_url, headers=headers, timeout=retry_budget, verify=False)
                    consecutive_transport_errors = 0
                    text = resp.text or ""
            if resp is None:
                pass
            elif resp.status_code == 200:
                no_code_reason = ""
                structured = _extract_structured_api_code(text, after_ts=after_ts)
                inline_html = bool(re.search(
                    r"<(?:article|div)\b[^>]*class=[\"'][^\"']*\b(?:mail-card|card)\b",
                    text,
                    flags=re.IGNORECASE,
                ))
                inline = _extract_inline_messages_html_otp(text, after_ts=after_ts) if inline_html else None
                structured_meta = structured[1] if structured else (inline[1] if inline else {})
                if structured:
                    code = structured[0]
                elif inline:
                    code = inline[0]
                elif inline_html:
                    # 已识别为邮件列表，但当前列表没有发送时间之后的新邮件。
                    # 绝不能再用整页正则取出旧邮件里的 6 位数字。
                    code = None
                    no_code_reason = "邮件列表中尚未出现本次发送之后的新验证码邮件"
                else:
                    code = _extract_code(text)
                if excluded_code_is_stale(code, structured_meta):
                    last_error = f"取码接口仍返回已被拒绝的旧验证码 {mask_otp(code)}"
                    logger.debug("[GenericAPI] 跳过已被 OpenAI 拒绝的旧 OTP=%s", mask_otp(code))
                    code = None
                if code:
                    now_seen = time.time()
                    if not best_otp:
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                        if structured_meta:
                            logger.info(
                                f"[GenericAPI] 首次锁定 OTP={mask_otp(code)}, source={structured_meta.get('source') or 'structured_api'} "
                                f"ts={structured_meta.get('received_at')} subject={str(structured_meta.get('subject') or '')[:80]!r}, "
                                f"等 {settle}s 看取码接口是否出现更新验证码..."
                            )
                        else:
                            logger.info(
                                f"[GenericAPI] 首次锁定 OTP={mask_otp(code)}, "
                                f"等 {settle}s 看取码接口是否出现更新验证码..."
                            )
                    elif code != best_otp:
                        if structured_meta:
                            logger.info(
                                f"[GenericAPI] 发现更新 OTP={mask_otp(code)}, source=structured_api "
                                f"ts={structured_meta.get('received_at')} subject={str(structured_meta.get('subject') or '')[:80]!r}，"
                                f"替换之前的 {mask_otp(best_otp)}, 重置 settle 计时"
                            )
                        else:
                            logger.info(
                                f"[GenericAPI] 发现更新 OTP={mask_otp(code)}，"
                                f"替换之前的 {mask_otp(best_otp)}, 重置 settle 计时"
                            )
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                    else:
                        logger.debug(f"[GenericAPI] 取码接口仍返回候选 OTP={mask_otp(best_otp)}")
                else:
                    last_error = no_code_reason or (
                        "HTTP 200 但未提取到 6 位验证码，响应预览: "
                        f"{redact_otp_text(text[:160])}"
                    )
            else:
                last_error = f"HTTP {resp.status_code}: {redact_otp_text(text[:160])}"
        except GenericApiTransportError:
            raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = f"{type(exc).__name__}: {redact_otp_text(exc)}"
            consecutive_transport_errors += 1
            if not best_otp and consecutive_transport_errors >= max_transport_errors:
                raise GenericApiTransportError(
                    "取码接口连续网络失败，已快速结束本轮 OTP 等待: "
                    f"{email}; attempts={consecutive_transport_errors}; {last_error}"
                ) from exc
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {redact_otp_text(exc)}"

        if stop_requested():
            raise GenericApiMailError("验证码页面已进入下一步，停止等待新验证码")
        now = time.time()
        if best_otp and settle_until is not None and now >= settle_until:
            logger.info(
                f"[GenericAPI] settle 完成，返回 OTP={mask_otp(best_otp)}, "
                f"候选锁定时间={time.strftime('%H:%M:%S', time.localtime(best_seen_at))}"
            )
            return best_otp

        remaining = max(0, int(deadline - now))
        if best_otp and settle_until is not None:
            logger.info(
                f"[GenericAPI] 已锁定候选 OTP={mask_otp(best_otp)}，等 settle 中"
                f"（剩余 settle ~{max(0, int(settle_until - now))}s, 总剩余 {remaining}s）..."
            )
        else:
            logger.info(
                f"[GenericAPI] 暂未从取码接口拿到验证码，"
                f"{interval}s 后重试（剩余 {remaining}s）..."
            )
        sleep_seconds = min(float(interval), max(0.0, deadline - now))
        if sleep_seconds <= 0:
            break
        time.sleep(sleep_seconds)

    if best_otp:
        logger.warning(f"[GenericAPI] 总超时但已有候选，返回 OTP={mask_otp(best_otp)}")
        return best_otp

    raise GenericApiMailError(f"等待通用 API 验证码超时: {email}; {last_error}")
