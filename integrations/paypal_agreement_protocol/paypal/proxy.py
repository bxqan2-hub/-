"""Proxy helpers for outbound HTTP requests.

Supports 1024proxy lines in the form:
    host:port:username:password
and converts them to httpx-compatible proxy URLs.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote

from config import PROXY_POOL, PROXY_ENABLED

_TRUE_VALUES = {"1", "true", "yes", "on", "enable", "enabled", "y"}
_FALSE_VALUES = {"0", "false", "no", "off", "disable", "disabled", "n", ""}


@dataclass(frozen=True)
class ProxyEntry:
    host: str
    port: int
    username: str
    password: str
    scheme: str = "http"

    @classmethod
    def parse(cls, raw: str) -> "ProxyEntry":
        value = (raw or "").strip()
        if not value:
            raise ValueError("代理配置为空")

        # Already a URL.  This path is mainly for env overrides such as
        # PAYPAL_PROXY_URL=http://user:pass@host:port.
        if "://" in value:
            from urllib.parse import urlsplit, unquote

            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
                raise ValueError(f"不支持的代理协议：{parsed.scheme}")
            if not parsed.hostname or not parsed.port:
                raise ValueError("代理 URL 必须包含 host 和 port")
            return cls(
                host=parsed.hostname,
                port=int(parsed.port),
                username=unquote(parsed.username or ""),
                password=unquote(parsed.password or ""),
                scheme=parsed.scheme,
            )

        parts = value.split(":", 3)
        if len(parts) != 4:
            raise ValueError("代理格式应为 host:port:username:password")
        host, port_text, username, password = [part.strip() for part in parts]
        if not host:
            raise ValueError("代理 host 不能为空")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError("代理 port 必须是数字") from exc
        if not (1 <= port <= 65535):
            raise ValueError("代理 port 超出范围")
        if not username or not password:
            raise ValueError("代理 username/password 不能为空")
        scheme = (
            "socks5h"
            if host.lower().endswith(".1024proxy.io") and port == 3000
            else "http"
        )
        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            scheme=scheme,
        )

    @property
    def url(self) -> str:
        user = quote(self.username, safe="")
        password = quote(self.password, safe="")
        auth = f"{user}:{password}@" if self.username or self.password else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def masked(self) -> str:
        auth = "***:***@" if self.username or self.password else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"


@dataclass(frozen=True)
class ProxyConfig:
    enabled: bool
    entry: ProxyEntry | None = None

    @property
    def url(self) -> str | None:
        return self.entry.url if self.enabled and self.entry else None

    @property
    def label(self) -> str:
        if not self.enabled or not self.entry:
            return "代理关闭"
        return self.entry.masked


def parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default


def _split_pool(raw: str) -> list[str]:
    lines: list[str] = []
    for item in (raw or "").replace(",", "\n").splitlines():
        item = item.strip()
        if item and not item.startswith("#"):
            lines.append(item)
    return lines


def load_proxy_pool() -> list[str]:
    """Load proxy lines from env first, then config.PROXY_POOL."""
    env_url = os.getenv("PAYPAL_PROXY_URL", "").strip()
    if env_url:
        return [env_url]

    env_pool = _split_pool(os.getenv("PAYPAL_PROXY_POOL", ""))
    if env_pool:
        return env_pool

    return [line.strip() for line in PROXY_POOL if str(line).strip()]


def choose_proxy_entry(pool: Iterable[str] | None = None, index: int | None = None) -> ProxyEntry:
    entries = list(pool if pool is not None else load_proxy_pool())
    if not entries:
        raise ValueError("未配置代理池")
    if index is not None:
        if index < 0 or index >= len(entries):
            raise ValueError(f"代理序号超出范围：{index}，可用范围 0-{len(entries) - 1}")
        raw = entries[index]
    else:
        raw = random.choice(entries)
    return ProxyEntry.parse(raw)


def build_proxy_config(enabled: bool | None = None, index: int | None = None) -> ProxyConfig:
    """Return a selected proxy config.

    enabled=None means use config/env default.  If disabled, no proxy is selected.
    """
    if enabled is None:
        # Env can override the default at process startup without code changes.
        should_enable = parse_bool(os.getenv("PAYPAL_PROXY_ENABLED"), PROXY_ENABLED)
    else:
        # Explicit CLI/API choices must win so the proxy can be toggled dynamically.
        should_enable = bool(enabled)
    if not should_enable:
        return ProxyConfig(enabled=False)
    return ProxyConfig(enabled=True, entry=choose_proxy_entry(index=index))
