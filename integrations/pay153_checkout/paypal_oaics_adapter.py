from __future__ import annotations

import hashlib
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from paypal_oaics_link_pp.countries import (
    checkout_country_for_proxy,
    get_country,
    list_countries,
)
from paypal_oaics_link_pp.engine import HandoffEngine, RunSpec
from paypal_oaics_link_pp.gateway import LiveProtocolGateway
from paypal_oaics_link_pp.proxies import ProxyPool, parse_proxy_lines
from paypal_oaics_link_pp.protocol import stripe_checkout as stripe
from paypal_oaics_link_pp.security import normalize_access_token, token_profile


def _normalize_paypal_oaics_proxy_line(raw: str) -> str:
    line = str(raw or "").strip()
    if "://" not in line and line.count("@") == 1:
        host_port, credentials = line.split("@", 1)
        host, separator, port = host_port.rpartition(":")
        username, credential_separator, password = credentials.partition(":")
        if separator and port.isdigit() and credential_separator and host and username and password:
            line = f"{username}:{password}@{host}:{port}"
    return line


def _use_remote_dns_for_socks(proxy_url: str) -> str:
    parsed = urlsplit(proxy_url)
    if parsed.scheme.lower() != "socks5":
        return proxy_url
    return urlunsplit(("socks5h", parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def normalize_paypal_oaics_proxies(raw: Any) -> list[str]:
    values = raw if isinstance(raw, (list, tuple)) else str(raw or "").replace("\r", "").split("\n")
    lines = [_normalize_paypal_oaics_proxy_line(value) for value in values if str(value or "").strip()]
    if len(lines) > 500:
        raise ValueError("入口代理最多填写 500 条")
    return [
        _use_remote_dns_for_socks(endpoint.url)
        for endpoint in parse_proxy_lines("\n".join(lines), default_scheme="socks5h")
    ]


def paypal_oaics_country_options() -> list[dict[str, str]]:
    return list_countries()


def resolve_paypal_oaics_countries(
    proxy_country: str = "BR",
    billing_country: str | None = None,
):
    proxy_profile = get_country(proxy_country or "BR")
    billing_profile = (
        get_country(billing_country)
        if str(billing_country or "").strip()
        else checkout_country_for_proxy(proxy_profile)
    )
    return proxy_profile, billing_profile


def probe_paypal_oaics_proxy(proxy_url: str, *, timeout: float = 3.5) -> dict[str, Any]:
    """Perform a bounded exit-IP check without creating or confirming Checkout."""
    endpoint = parse_proxy_lines(
        normalize_paypal_oaics_proxies([proxy_url])[0],
        default_scheme="socks5h",
    )[0]
    failures: list[str] = []
    http = stripe.build_http(endpoint.url)
    try:
        for source, url in stripe.IP_CHECK_SOURCES[:2]:
            try:
                response = http.get(
                    url,
                    headers={"User-Agent": stripe.CHROME_UA, "Accept": "application/json"},
                    timeout=timeout,
                )
                status = int(getattr(response, "status_code", 599) or 599)
                if status >= 400:
                    failures.append(f"{source} HTTP {status}")
                    continue
                payload = response.json() or {}
                if not isinstance(payload, dict):
                    failures.append(f"{source} invalid response")
                    continue
                ip, country = stripe._extract_ip_country(source, payload)
                if not country:
                    failures.append(f"{source} no country")
                    continue
                return {
                    "reachable": True,
                    "country": country,
                    "br_compatible": country == "BR",
                    "source": source,
                    "exit_id": "exit#" + hashlib.sha256(ip.encode()).hexdigest()[:10],
                }
            except Exception as exc:
                failures.append(f"{source} {type(exc).__name__}")
    finally:
        try:
            http.close()
        except Exception:
            pass
    return {
        "reachable": False,
        "country": "",
        "br_compatible": False,
        "error": " / ".join(failures[:2]) or "no response",
    }


def run_paypal_oaics(
    *,
    access_token: str,
    proxies: list[str],
    checkout_attempts: int,
    provider_attempts: int,
    log: Callable[[str], None],
    is_cancelled: Callable[[], bool],
    proxy_country: str = "BR",
    billing_country: str | None = None,
) -> dict[str, Any]:
    token = normalize_access_token(access_token)
    proxy_profile, checkout_country = resolve_paypal_oaics_countries(
        proxy_country,
        billing_country,
    )
    normalized_proxies = normalize_paypal_oaics_proxies(proxies)
    proxy_pool = ProxyPool(parse_proxy_lines("\n".join(normalized_proxies), default_scheme="socks5h"))
    spec = RunSpec(
        access_token=token,
        token_profile=token_profile(token),
        proxy_country=proxy_profile,
        checkout_country=checkout_country,
        proxies=proxy_pool,
        checkout_attempts=checkout_attempts,
        provider_attempts=provider_attempts,
    )

    def emit(level: str, stage: str, message: str) -> None:
        log(f"[{stage}] {message}")

    try:
        result = HandoffEngine(LiveProtocolGateway()).run(
            spec,
            emit=emit,
            is_cancelled=is_cancelled,
        )
        payload = result.to_dict()
        payload.update(
            {
                "plan": "plus",
                "requested_link_type": "paypal_oaics",
                "link_type": "paypal_oaics",
                "provider": "paypal_oaics",
                "checkout_session_id": result.session_id,
                "checkout_country": result.country,
                "checkout_currency": result.currency,
                "account_email": spec.token_profile.email,
                "account_id": spec.token_profile.account_id,
                "entry_country": result.proxy_country,
                "payment_proxy_country": result.proxy_country,
                "promo_requested": True,
                "promo_applied": True,
                "checkout_amount": 0,
                "amount_currency": result.currency,
                "amount_verification": "verified_zero",
                "extractor": "eatWhitePorridge/link-pp",
                "url": result.paypal_approve_url,
                "paypal_link": result.paypal_approve_url,
            }
        )
        return payload
    finally:
        spec.clear_secrets()
