"""Inbox Mate API adapter for provider accounts pasted as email/password rows."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import requests

from config import email as _email_cfg
from core.generic_api_mail_client import _extract_code, _extract_yangyang_openai_code

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^\s@]+@([^\s@]+\.[^\s@]+)$")
_ACCOUNT_RE = re.compile(
    r"(?:账号|帳號|account|email)\s*[:：=]\s*([^|\s]+@[^|\s]+)\s*\|\s*(?:密码|密碼|password|pass)\s*[:：=]\s*(\S+)",
    re.IGNORECASE,
)
_MAILCOM_DOMAINS = {
    "mail.com", "cheerful.com", "email.com", "usa.com", "myself.com", "post.com",
    "consultant.com", "dr.com", "engineer.com", "techie.com", "writeme.com",
    "catlover.com", "doglover.com", "solution4u.com", "iname.com", "alumni.com",
    "columnist.com", "deliveryman.com", "diplomats.com", "instruction.com",
    "accountant.com", "monarchy.com", "realtyagent.com", "registerednurses.com",
    "repairman.com", "representative.com", "sanfranmail.com", "sociologist.com",
    "teachers.org", "technologist.com", "uniforms.com", "worker.com", "workmail.com",
    "elvis.com", "optician.com", "pediatrician.com", "presidency.com", "crew22.net",
}


class InboxMateMailError(RuntimeError):
    """Inbox Mate session, task, or OTP extraction failure."""


@dataclass(frozen=True)
class InboxMateAccount:
    email: str
    code_url: str
    access_token: str = ""
    totp_secret: str | None = None


def email_domain(email: str) -> str:
    match = _EMAIL_RE.fullmatch(str(email or "").strip())
    return match.group(1).lower() if match else ""


def provider_for_email(email: str) -> str:
    return "mailcom" if email_domain(email) in _MAILCOM_DOMAINS else "custom"


def _base_url(value: str | None = None) -> str:
    base = str(value or getattr(_email_cfg, "INBOX_MATE_BASE", "") or "").strip().rstrip("/")
    if not re.match(r"^https?://[^/]+(?:/[^/]*)?$", base, re.IGNORECASE):
        raise InboxMateMailError("Inbox Mate 地址必须是有效的 HTTP/HTTPS URL")
    return base


def _split_row(line: str) -> tuple[str, str] | None:
    match = _ACCOUNT_RE.search(line)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    parts = re.split(r"----|====|\s*\|\s*", line, maxsplit=1)
    if len(parts) == 2 and _EMAIL_RE.fullmatch(parts[0].strip()):
        return parts[0].strip(), parts[1].strip()
    return None


def looks_like_labeled_import(text: str | Iterable[str]) -> bool:
    """Return whether the payload contains an Inbox Mate account/password label row."""
    lines = str(text).splitlines() if isinstance(text, str) else list(text)
    return any(_ACCOUNT_RE.search(str(line or "")) for line in lines)


def parse_import_text(text: str | Iterable[str]) -> list[dict]:
    """Parse ``账号: email | 密码: secret`` and common delimiter variants."""
    lines = str(text).splitlines() if isinstance(text, str) else list(text)
    records: list[dict] = []
    base = _base_url()
    for raw in lines:
        line = str(raw or "").strip()
        if not line or line.startswith("#"):
            continue
        pair = _split_row(line)
        if not pair:
            continue
        address, password = pair
        domain = email_domain(address)
        if not domain or not password:
            continue
        records.append({
            "email": address,
            "password": password,
            "email_domain": domain,
            "provider": "inbox_mate",
            "mail_provider": provider_for_email(address),
            "api_base": base,
            "code_url": f"{base}/api/v1/jobs",
        })
    unique: dict[str, dict] = {}
    for row in records:
        unique.setdefault(row["email"].lower(), row)
    return list(unique.values())


def pick_account() -> InboxMateAccount:
    from core.db import claim_next_generic_api_email, generic_api_email_pool_summary

    row = claim_next_generic_api_email(provider="inbox_mate")
    if row is None:
        summary = generic_api_email_pool_summary(provider="inbox_mate")
        raise InboxMateMailError(f"Inbox Mate 邮箱池没有可用账号：{summary}")
    return InboxMateAccount(
        email=row["email"],
        code_url=row.get("code_url") or "",
        access_token=row.get("access_token") or "",
        totp_secret=row.get("totp_secret"),
    )


def get_account_context(email: str) -> InboxMateAccount | None:
    from core.db import get_generic_api_email_by_email

    row = get_generic_api_email_by_email(email)
    if not row or str(row.get("provider") or "").lower() != "inbox_mate":
        return None
    return InboxMateAccount(email=row["email"], code_url=row.get("code_url") or "")


def _row_for_email(email: str) -> dict:
    from core.db import get_generic_api_email_by_email

    row = get_generic_api_email_by_email(email)
    if not row or str(row.get("provider") or "").lower() != "inbox_mate":
        raise InboxMateMailError(f"Inbox Mate 邮箱不存在或未导入：{email}")
    if not str(row.get("password") or "").strip():
        raise InboxMateMailError(f"Inbox Mate 邮箱未保存密码：{email}")
    return row


def _code_from_payload(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("verificationCode", "verification_code", "otp", "code"):
            candidate = str(value.get(key) or "").strip()
            if re.fullmatch(r"\d{6}", candidate):
                return candidate
        text_parts = []
        for key in ("subject", "body", "text", "html", "snippet", "content"):
            if value.get(key) is not None:
                text_parts.append(str(value[key]))
        if text_parts:
            text = "\n".join(text_parts)
            return _extract_yangyang_openai_code(str(value.get("subject") or ""), text) or _extract_code(text)
        for child in value.values():
            found = _code_from_payload(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _code_from_payload(child)
            if found:
                return found
    return None


def snapshot_current_otp(email: str, timeout: float = 8.0) -> str | None:
    try:
        return _run_job(
            email,
            max_wait=max(8, int(timeout)),
            settle_seconds=0,
            rescan_completed=False,
        )
    except Exception as exc:
        logger.debug("[InboxMate] 历史 OTP 快照失败：%s: %s", type(exc).__name__, exc)
        return None


def _run_job(
    email: str,
    *,
    after_ts: float | None = None,
    max_wait: int | None = None,
    settle_seconds: int = 0,
    exclude_codes: set[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    rescan_completed: bool = True,
) -> str:
    row = _row_for_email(email)
    base = _base_url(row.get("api_base"))
    provider = str(row.get("mail_provider") or provider_for_email(email)).strip() or "custom"
    deadline = time.time() + int(max_wait or getattr(_email_cfg, "OTP_MAX_WAIT", 180) or 180)
    lookback = 1440
    if after_ts:
        lookback = max(15, min(10080, int((time.time() - float(after_ts)) / 60) + 15))
    account_payload = {
        "email": email,
        "provider": provider,
        "auth": {"type": "app_password", "secret": str(row["password"])},
    }
    if provider == "custom":
        account_payload.update({
            "customHost": str(row.get("custom_host") or ""),
            "customPort": int(row.get("custom_port") or 993),
            "customProtocol": str(row.get("custom_protocol") or "imap"),
        })
    body = {
        "accounts": [account_payload],
        "lookbackMinutes": lookback,
        "maxMessagesPerAccount": 20,
    }
    excluded = {str(code).strip() for code in (exclude_codes or set()) if str(code).strip()}
    best = None
    last_transport_error: Exception | None = None
    scan_attempt = 0
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    try:
        while time.time() < deadline:
            if should_stop and should_stop():
                raise InboxMateMailError("验证码页面已进入下一步，停止等待新验证码")
            try:
                csrf_resp = session.get(f"{base}/api/v1/session", timeout=20)
                csrf_resp.raise_for_status()
                csrf = str((csrf_resp.json() or {}).get("csrfToken") or "").strip()
                if not csrf:
                    raise InboxMateMailError("Inbox Mate 未返回 CSRF 会话令牌")
                scan_attempt += 1
                account_payload["clientAccountId"] = (
                    f"codex-{email}-{int(time.time() * 1000)}-{scan_attempt}"
                )
                response = session.post(
                    f"{base}/api/v1/jobs",
                    headers={"X-Inbox-Mate-CSRF": csrf, "Content-Type": "application/json"},
                    json=body,
                    timeout=20,
                )
                response.raise_for_status()
                job_id = str((response.json() or {}).get("jobId") or "").strip()
                if not job_id:
                    raise InboxMateMailError("Inbox Mate 未返回任务 ID")
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_transport_error = exc
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                logger.warning("[InboxMate] 创建扫描任务暂时失败，将重试：%s", exc)
                time.sleep(min(2, remaining))
                continue

            completed = False
            while time.time() < deadline and not completed:
                try:
                    with session.get(
                        f"{base}/api/v1/jobs/{job_id}/events",
                        headers={"Accept": "text/event-stream"},
                        stream=True,
                        timeout=(20, max(10, min(20, int(deadline - time.time())))),
                    ) as stream:
                        for raw in stream.iter_lines(decode_unicode=True):
                            if time.time() >= deadline:
                                break
                            if should_stop and should_stop():
                                raise InboxMateMailError("验证码页面已进入下一步，停止等待新验证码")
                            if not raw or not raw.startswith("data:"):
                                continue
                            try:
                                payload = json.loads(raw.split(":", 1)[1].strip())
                            except (TypeError, ValueError):
                                continue
                            code = _code_from_payload(payload)
                            if code and code not in excluded:
                                best = code
                            if best and settle_seconds <= 0:
                                return best
                            if isinstance(payload, dict) and payload.get("state") == "completed":
                                completed = True
                                break
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                    last_transport_error = exc
                    continue

            if best:
                return best
            if not completed:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(1, remaining))
                continue
            if not rescan_completed:
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            logger.info("[InboxMate] 本轮扫描未找到新验证码，创建新任务继续扫描：%s", email)
            time.sleep(min(3, remaining))
    finally:
        session.close()
    if best:
        return best
    if last_transport_error:
        logger.warning("[InboxMate] 扫描截止前最后一次网络错误：%s", last_transport_error)
    raise InboxMateMailError(f"Inbox Mate 未找到新验证码：{email}")


def fetch_latest_otp(email: str, **kwargs) -> str:
    return _run_job(
        email,
        after_ts=kwargs.get("after_ts"),
        max_wait=kwargs.get("max_wait"),
        settle_seconds=int(kwargs.get("settle_seconds") or 0),
        exclude_codes={str(code) for code in (kwargs.get("exclude_codes") or [])},
        should_stop=kwargs.get("should_stop"),
    )


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    from core.db import release_generic_api_email

    release_generic_api_email(email, status=status, note=note)
