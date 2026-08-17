"""Domain-mail adapter for HTML mailbox providers.

The importer accepts a provider account-list URL, ``email----password`` rows,
or ``email----pickup_url`` rows. Email domains are derived from each address;
the provider host is derived independently from the pasted URL.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlencode, urljoin, urlparse

import requests

from config import email as _email_cfg

_EMAIL_RE = re.compile(r"^[^\s@]+@([^\s@]+\.[^\s@]+)$")


class DomainApiMailError(RuntimeError):
    """Domain API source parsing or pickup configuration error."""


class _AccountRowsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._row_div_depth = 0
        self._row_values: list[str] = []
        self.rows: list[tuple[str, str]] = []
        self.account_page_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = dict(attrs)
        href = str(attr.get("href") or "").strip()
        if href and "10p.php" in href.lower():
            self.account_page_links.append(href)
        classes = set(str(attr.get("class") or "").split())
        if tag == "div" and "row" in classes and not self._row_div_depth:
            self._row_div_depth = 1
            self._row_values = []
        elif tag == "div" and self._row_div_depth:
            self._row_div_depth += 1
        if self._row_div_depth:
            value = str(attr.get("data-copy") or "").strip()
            if value:
                self._row_values.append(value)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "div" or not self._row_div_depth:
            return
        self._row_div_depth -= 1
        if self._row_div_depth:
            return
        email = next((value for value in self._row_values if _EMAIL_RE.fullmatch(value)), "")
        password = next((value for value in self._row_values if value != email), "")
        if email and password:
            self.rows.append((email, password))
        self._row_values = []


def email_domain(email: str) -> str:
    match = _EMAIL_RE.fullmatch(str(email or "").strip())
    return match.group(1).lower() if match else ""


def _http_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DomainApiMailError("域名邮箱来源必须是有效的 HTTP/HTTPS URL")
    return str(url).strip()


def build_code_url(email: str, password: str, api_base: str | None = None) -> str:
    base = str(
        api_base
        or getattr(_email_cfg, "DOMAIN_API_BASE", "")
        or ""
    ).strip()
    if not base:
        raise DomainApiMailError("邮箱----密码格式需要同时粘贴账户页面 URL，或配置 DOMAIN_API_BASE")
    base = _http_url(base).split("?", 1)[0]
    return f"{base}?{urlencode({'u': str(email).strip(), 'p': str(password).strip()})}"


def _parse_page(html: str) -> _AccountRowsParser:
    parser = _AccountRowsParser()
    parser.feed(str(html or ""))
    return parser


def _fetch_account_list(url: str) -> tuple[list[tuple[str, str]], str]:
    source_url = _http_url(url)
    response = requests.get(source_url, timeout=20)
    response.raise_for_status()
    parser = _parse_page(response.text or "")
    if not parser.rows and parser.account_page_links:
        detail_url = _http_url(urljoin(source_url, parser.account_page_links[0].replace("&amp;", "&")))
        detail_response = requests.get(detail_url, timeout=20)
        detail_response.raise_for_status()
        parser = _parse_page(detail_response.text or "")
        source_url = detail_url
    parsed = urlparse(source_url)
    api_base = f"{parsed.scheme}://{parsed.netloc}/m.php"
    return parser.rows, api_base


def parse_import_text(text: str | Iterable[str]) -> list[dict]:
    """Parse account-page URLs and mailbox credential/pickup rows."""
    lines = str(text).splitlines() if isinstance(text, str) else list(text)
    records: list[dict] = []
    inferred_api_base = ""

    for raw in lines:
        line = str(raw or "").strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith(("http://", "https://")) and "----" not in line and "====" not in line:
            rows, inferred_api_base = _fetch_account_list(line)
            for address, password in rows:
                records.append({
                    "email": address,
                    "email_domain": email_domain(address),
                    "code_url": build_code_url(address, password, inferred_api_base),
                    "provider": "domain_api",
                })

    for raw in lines:
        line = str(raw or "").strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith(("http://", "https://")) and "----" not in line and "====" not in line:
            continue
        parts = [part.strip() for part in (line.split("----") if "----" in line else line.split("===="))]
        if len(parts) < 2 or not email_domain(parts[0]):
            continue
        address, second = parts[0], parts[1]
        explicit_base = parts[2] if len(parts) > 2 and parts[2].lower().startswith(("http://", "https://")) else ""
        code_url = (
            _http_url(second)
            if second.lower().startswith(("http://", "https://"))
            else build_code_url(address, second, explicit_base or inferred_api_base)
        )
        records.append({
            "email": address,
            "email_domain": email_domain(address),
            "code_url": code_url,
            "provider": "domain_api",
        })

    unique: dict[str, dict] = {}
    for row in records:
        unique.setdefault(str(row["email"]).lower(), row)
    return list(unique.values())


def pick_account():
    from core.generic_api_mail_client import pick_account as _pick_account
    return _pick_account(provider="domain_api")


def get_account_context(email: str):
    from core.generic_api_mail_client import get_account_context as _get_context
    return _get_context(email)


def fetch_latest_otp(email: str, **kwargs):
    from core.generic_api_mail_client import fetch_latest_otp as _fetch_latest_otp
    return _fetch_latest_otp(email, **kwargs)


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    from core.generic_api_mail_client import release_account as _release_account
    _release_account(email, status=status, note=note)
