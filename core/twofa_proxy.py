# -*- coding: utf-8 -*-
"""Helpers for keeping post-registration 2FA HTTP traffic on the same route.

Browser drivers must not call ``BrowserSession(proxy=None)`` for the 2FA
follow-up: ``None`` means "pick another proxy from the local pool" and can
silently move the account to a different exit.  The browser integrations pass
their known proxy (when one exists) through these helpers.  Cloud browser
providers that only expose a country selector therefore fail with a structured
diagnostic instead of making an untracked local hop.
"""
from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote, urlparse

from core.account_export import TwoFASetupError
from core.otp_utils import redact_otp_text
from core.session import BrowserSession


_EMPTY_PROXY_VALUES = {"", "-", "—", "none", "null", "undefined", "未设置", "未配置"}
_PROXY_KEYS = (
    "proxy",
    "proxy_url",
    "proxyUrl",
    "proxy_server",
    "proxyServer",
    "proxy_endpoint",
    "proxyEndpoint",
)
_NESTED_KEYS = ("data", "result", "latest", "session", "browser_session")


def twofa_failure_payload(error: BaseException, *, default_stage: str = "totp_setup") -> dict[str, object]:
    """Serialize a 2FA exception without copying arbitrary transport details."""
    if isinstance(error, TwoFASetupError):
        return {
            "stage": error.stage,
            "code": error.code,
            "http_status": error.http_status,
            "message": redact_otp_text(str(error)[:240]),
        }
    return {
        "stage": default_stage,
        "code": "totp_setup_failed",
        "http_status": None,
        "message": type(error).__name__,
    }


def _candidate_values(value, *, depth: int = 0):
    """Yield only proxy-shaped values from provider response objects."""
    if depth > 3 or value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if text and text.lower() not in _EMPTY_PROXY_VALUES:
            yield text
        return
    if isinstance(value, Mapping):
        for key in _PROXY_KEYS:
            if key in value:
                yield from _candidate_values(value.get(key), depth=depth + 1)
        for key in _NESTED_KEYS:
            if key in value:
                yield from _candidate_values(value.get(key), depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _candidate_values(item, depth=depth + 1)


def normalize_twofa_proxy(value: str, *, source: str = "registration") -> str:
    """Validate a proxy URL and use remote DNS for SOCKS routes."""
    text = str(value or "").strip()
    if not text or text.lower() in _EMPTY_PROXY_VALUES:
        raise TwoFASetupError(
            "totp_proxy",
            "totp_proxy_unavailable",
            f"{source} 未提供可复用的 2FA 代理",
        )
    # Accept the provider's compact ``host:port:user:password`` form too;
    # config.proxy expands the same form when it selects a registration route.
    if "://" not in text:
        parts = text.split(":", 3)
        if len(parts) == 4:
            host, port, username, password = parts
            text = (
                f"socks5h://{quote(username, safe='')}:{quote(password, safe='')}"
                f"@{host}:{port}"
            )
        elif len(parts) == 2:
            text = f"http://{text}"
    # CloakBrowser accepts socks5:// but the HTTP side must use socks5h:// so
    # DNS resolution stays at the proxy endpoint rather than on this host.
    if text.lower().startswith("socks5://"):
        text = "socks5h://" + text[len("socks5://") :]
    try:
        parsed = urlparse(text)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise TwoFASetupError(
            "totp_proxy",
            "totp_proxy_invalid",
            f"{source} 的 2FA 代理地址无效",
        ) from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https", "socks5h"} or not hostname or not port:
        raise TwoFASetupError(
            "totp_proxy",
            "totp_proxy_invalid",
            f"{source} 的 2FA 代理地址无效",
        )
    return text


def resolve_twofa_proxy(*values, source: str = "registration") -> str:
    """Resolve the first explicit provider proxy; never pick a local fallback."""
    for value in values:
        for candidate in _candidate_values(value):
            return normalize_twofa_proxy(candidate, source=source)
    raise TwoFASetupError(
        "totp_proxy",
        "totp_proxy_unavailable",
        f"{source} 没有返回可复用的 2FA 代理",
    )


def build_twofa_session(proxy: str, *, source: str = "registration") -> BrowserSession:
    """Create an auxiliary session bound to an explicit proxy endpoint."""
    normalized = normalize_twofa_proxy(proxy, source=source)
    try:
        # ``proxy`` is explicit, so BrowserSession cannot fall back to
        # ``pick_proxy``.  Keep geo probing off: the registration browser has
        # already verified this route and the extra probe only adds latency.
        session = BrowserSession(proxy=normalized, detect_exit_geo=False)
    except Exception as exc:
        raise TwoFASetupError(
            "totp_proxy",
            "totp_proxy_session_failed",
            f"{source} 2FA 代理会话创建失败",
        ) from exc
    actual = str(getattr(session, "proxy", "") or "").strip()
    if actual != normalized:
        try:
            session.close()
        except Exception:
            pass
        raise TwoFASetupError(
            "totp_proxy",
            "totp_proxy_continuity_failed",
            f"{source} 2FA 会话未保持注册代理",
        )
    return session
