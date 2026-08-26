from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import random
import threading
import time
import unicodedata
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from flask import Flask, jsonify, redirect, request, send_from_directory
from curl_cffi import requests

import stripe_checkout as sc
from kakao_oaics import KakaoOaicsError, run_kakao_oaics
from provider_checkout import PROVIDER_DEFAULTS, default_billing, stripe_to_provider
from billing_address_resolver import resolve_cached_country_address
from sentinel_token import SentinelTokenProvider as BaseSentinel
from upi_go_runner import available as upi_go_available, run_upi as run_upi_go
from ph_short_extractor import (
    CheckoutExtractor as PhShortCheckoutExtractor,
    ExtractorConfig as PhShortExtractorConfig,
    checkout_amount_minor as custom_checkout_amount_minor,
    checkout_currency as custom_checkout_currency,
    checkout_state_from_html as custom_checkout_state_from_html,
    parse_credentials as parse_ph_short_credentials,
)
from paypal_oaics_adapter import (
    normalize_paypal_oaics_proxies,
    paypal_oaics_country_options,
    probe_paypal_oaics_proxy,
    resolve_paypal_oaics_countries,
    run_paypal_oaics,
)

ROOT = Path(__file__).resolve().parent
BACKEND_LOG_DIR = Path(os.getenv("PAY153_LOG_DIR", str(ROOT / "logs")))
RUST_ALIAS_FILE = ROOT / "data" / "rust_job_aliases.json"
RUST_ALIAS_LOCK = threading.RLock()
LEGACY_SERVICE_BASE = str(os.getenv("PAY153_LEGACY_BASE", "")).rstrip("/")
UPI_ENABLED = str(os.getenv("PAY153_UPI_ENABLED", "0")).strip().lower() in {
    "1", "true", "yes", "on",
}
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
app.config["JSON_AS_ASCII"] = False

ROTATING_PAYPAL_ADDRESS_COUNTRIES = {"NL", "GB", "TH", "BR", "US"}
DYNAMIC_PROXY_API_URL = str(
    os.getenv("PAY153_DYNAMIC_PROXY_API")
    or "https://white.1024proxy.com/white/api"
).strip()
DYNAMIC_PROXY_API_LOCK = threading.Lock()
DYNAMIC_PROXY_API_LAST_AT = 0.0
DYNAMIC_PROXY_API_MIN_INTERVAL = max(
    0.1, float(os.getenv("PAY153_DYNAMIC_PROXY_MIN_INTERVAL") or 0.35)
)
PAYPAL_OAICS_PROXY_CHECK_LIMIT = 5
PAYPAL_OAICS_PROXY_CHECK_CACHE_TTL = 120.0
_PAYPAL_OAICS_PROXY_CHECK_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PAYPAL_OAICS_PROXY_CHECK_CACHE_LOCK = threading.Lock()


def _cached_paypal_oaics_proxy_probe(proxy_url: str) -> dict[str, Any]:
    cache_key = hashlib.sha256(proxy_url.encode("utf-8")).hexdigest()
    now = time.time()
    with _PAYPAL_OAICS_PROXY_CHECK_CACHE_LOCK:
        cached = _PAYPAL_OAICS_PROXY_CHECK_CACHE.get(cache_key)
        if cached and now - cached[0] <= PAYPAL_OAICS_PROXY_CHECK_CACHE_TTL:
            return dict(cached[1])
    result = probe_paypal_oaics_proxy(proxy_url)
    with _PAYPAL_OAICS_PROXY_CHECK_CACHE_LOCK:
        _PAYPAL_OAICS_PROXY_CHECK_CACHE[cache_key] = (now, dict(result))
    return result


def _ascii_key(value: Any) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(character)
    ).strip().lower()


def normalize_rotating_paypal_address(country: str, value: dict[str, Any]) -> dict[str, str] | None:
    country = str(country or "").upper()
    line1 = str(value.get("line1") or "").strip()
    city = str(value.get("city") or "").strip()
    postal = str(value.get("postal_code") or "").strip().upper()
    state = str(value.get("state") or "").strip()
    if not line1 or not city or not postal:
        return None
    if country == "NL":
        compact = re.sub(r"\s+", "", postal)
        if not re.fullmatch(r"[1-9][0-9]{3}[A-Z]{2}", compact):
            return None
        postal, state = f"{compact[:4]} {compact[4:]}", ""
    elif country == "GB":
        compact = re.sub(r"\s+", "", postal)
        if not re.fullmatch(r"[A-Z0-9]{5,7}", compact):
            return None
        postal, state = f"{compact[:-3]} {compact[-3:]}", ""
    elif country == "TH":
        digits = re.sub(r"\D", "", postal)
        if len(digits) != 5:
            return None
        postal, state = digits, ""
    elif country == "BR":
        digits = re.sub(r"\D", "", postal)
        if len(digits) != 8:
            return None
        br_states = {
            "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM",
            "bahia": "BA", "ceara": "CE", "distrito federal": "DF",
            "espirito santo": "ES", "goias": "GO", "maranhao": "MA",
            "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG",
            "para": "PA", "paraiba": "PB", "parana": "PR", "pernambuco": "PE",
            "piaui": "PI", "rio de janeiro": "RJ", "rio grande do norte": "RN",
            "rio grande do sul": "RS", "rondonia": "RO", "roraima": "RR",
            "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE",
            "tocantins": "TO",
        }
        state = state.upper() if re.fullmatch(r"[A-Z]{2}", state.upper()) else br_states.get(_ascii_key(state), "")
        if not state:
            return None
        postal = digits
    elif country == "US":
        match = re.search(r"\b[0-9]{5}(?:-[0-9]{4})?\b", postal)
        if not match:
            return None
        us_states = {
            "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
            "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
            "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
            "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
            "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
            "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
            "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
            "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
            "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
            "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
            "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
            "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
            "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
        }
        state = state.upper() if re.fullmatch(r"[A-Z]{2}", state.upper()) else us_states.get(_ascii_key(state), "")
        if not state:
            return None
        postal = match.group(0)
    return {
        "country": country,
        "line1": line1,
        "line2": "",
        "city": city,
        "postal_code": postal,
        "state": state,
    }


def remember_rust_job_alias(public_job_id: str, rust_job_id: str, metadata: dict[str, Any]) -> None:
    now = time.time()
    with RUST_ALIAS_LOCK:
        try:
            rows = json.loads(RUST_ALIAS_FILE.read_text(encoding="utf-8"))
            if not isinstance(rows, dict):
                rows = {}
        except Exception:
            rows = {}
        rows = {
            str(key): value for key, value in rows.items()
            if isinstance(value, dict) and now - float(value.get("created_at") or now) < 86400
        }
        rows[str(public_job_id)] = {
            "rust_job_id": str(rust_job_id),
            "created_at": now,
            **{str(key): value for key, value in metadata.items() if key in {
                "plan", "link_type", "country", "currency", "use_promo", "promo_campaign",
            }},
        }
        RUST_ALIAS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = RUST_ALIAS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(RUST_ALIAS_FILE)


def get_rust_job_alias(public_job_id: str) -> dict[str, Any] | None:
    with RUST_ALIAS_LOCK:
        try:
            rows = json.loads(RUST_ALIAS_FILE.read_text(encoding="utf-8"))
            value = rows.get(str(public_job_id)) if isinstance(rows, dict) else None
            return dict(value) if isinstance(value, dict) else None
        except Exception:
            return None


def rust_job_public_snapshot(public_job_id: str) -> dict[str, Any] | None:
    alias = get_rust_job_alias(public_job_id)
    rust_base = str(os.getenv("PAY153_RUST_URL") or "").strip().rstrip("/")
    if not alias or not rust_base:
        return None
    rust_job_id = str(alias.get("rust_job_id") or "")
    if not rust_job_id:
        return None
    try:
        response = requests.get(f"{rust_base}/api/v1/jobs/{rust_job_id}", timeout=12)
        if response.status_code != 200:
            return None
        job = (response.json() or {}).get("job") or {}
    except Exception:
        return None
    status_map = {
        "queued": "queued",
        "running": "running",
        "succeeded": "done",
        "failed": "error",
        "cancelled": "cancelled",
    }
    result = dict(job.get("result") or {})
    explicit_promo_requested = result.get("promo_requested")
    if explicit_promo_requested is None:
        explicit_promo_requested = alias.get("use_promo")
    if explicit_promo_requested is None:
        # Older Rust aliases did not persist use_promo.  A Rust result only
        # contains promo_applied after the promotion branch was requested.
        explicit_promo_requested = result.get("promo_applied") is not None
    result.update({
        "plan": alias.get("plan") or result.get("plan"),
        "link_type": alias.get("link_type") or result.get("link_type"),
        "country": alias.get("country") or result.get("country"),
        "currency": str(result.get("currency") or alias.get("currency") or "").upper(),
        "promo_requested": bool(explicit_promo_requested),
        "promo_campaign_used": result.get("promo_campaign_used") or alias.get("promo_campaign") or "",
        "rust_workflow": True,
    })
    if result.get("link_type") == "paypal":
        result["paypal_link"] = result.get("paypal_url") or result.get("paypal_link") or ""
        result["provider_redirect_url"] = result.get("paypal_link") or result.get("stripe_redirect_url") or ""
    return {
        "id": public_job_id,
        "status": status_map.get(str(job.get("status") or ""), "running"),
        "percent": int(job.get("progress") or 0),
        "text": str(job.get("step") or "Rust 工作流运行中"),
        "logs": [],
        "result": result if job.get("status") == "succeeded" else None,
        "error": str(job.get("error") or ""),
        "queue_position": 0,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }

STRIPE_CHECKOUT_FRAGMENT = (
    "#fidnandhYHdWcXxpYCc%2FJ2FgY2RwaXEnKSdpamZkaWAnPyd%2FbScpJ3ZwZ3Zmd2x1cWxqa1Brb"
    "HRwYGtgdnZAa2RnaWBhJz9jZGl2YCknYnBkZmRoamlgU2R3bGRrcSc%2FJ2Zqa3F3amknKSdkdWxO"
    "YHwnPyd1blppbHNgWjA0TUp3VnJGM200a31Cakw2aVFEYldvXFN3fzFhUDZjU0pkZ3xGZk5XNnVnQ"
    "E9icEZTRGl0Rn1hfUZQc2pXbTRdUnJXZGZTbGpzUDZuSU5zdW5vbTJMdG5SNTVsXVR2b2o2aycpJ2"
    "N3amhWYHdzYHcnP3F3cGApJ2dkZm5id2pwa2FGamlqdyc%2FJyZjY2NjY2MnKSdpZHxqcHFRfHVgJ"
    "z8ndmxrYmlgWmxxYGgnKSdga2RnaWBVaWRmYG1qaWFgd3YnP3F3cGB4JSUl"
 )


def normalize_hosted_checkout_url(url: str, session_id: str = "") -> str:
    """Return an OpenAI hosted Checkout URL that can be opened directly."""
    value = str(url or "").strip()
    if not value and session_id:
        value = f"https://pay.openai.com/c/pay/{session_id}"
    if value.startswith("https://checkout.stripe.com/c/pay/"):
        value = "https://pay.openai.com" + value[len("https://checkout.stripe.com"):]
    return value


PLANS = {
    "plus": "chatgptplusplan",
    "pro": "chatgptpro",
    "team": "chatgptteamplan",
    "codex_low": "chatgptbusiness_usage_based",
}

OPENAI_CHECKOUT_CURRENCIES = {
    "USD", "AUD", "CAD", "GBP", "EUR", "CLP", "JPY", "INR", "IDR", "PKR",
    "THB", "MYR", "TWD", "VND", "PHP", "NGN", "ZAR", "KZT", "TZS", "EGP",
    "BRL", "SEK", "CZK", "PLN", "DKK", "NOK", "KRW", "COP", "MXN", "PEN",
    "HUF", "QAR", "RON", "ILS", "AED", "SGD", "NZD", "CHF", "SAR",
}

# 国家接口可能返回 OpenAI Checkout 尚未接受的本地币种，例如 BA/BAM。
# 欧洲非欧元国家遇到未开放币种时优先使用 EUR，其余地区回退 USD。
EURO_CURRENCY_FALLBACK_COUNTRIES = {
    "AL", "AD", "AM", "BA", "BG", "BY", "CY", "EE", "GE", "HR", "IS", "LI",
    "LT", "LV", "MC", "MD", "ME", "MK", "MT", "RS", "SM", "SK", "SI", "TR",
    "UA", "VA", "XK",
}


def normalize_checkout_currency(country: str, currency: str = "") -> tuple[str, str]:
    country = str(country or "US").strip().upper()
    detected = str(currency or "").strip().upper()
    if detected in OPENAI_CHECKOUT_CURRENCIES:
        return detected, "代理地区接口"
    mapped = str(sc.currency_for_country(country) or "").upper()
    if country in EURO_CURRENCY_FALLBACK_COUNTRIES and detected not in OPENAI_CHECKOUT_CURRENCIES:
        return "EUR", f"OpenAI币种回退（{detected or mapped or '未知'}→EUR）"
    if mapped in OPENAI_CHECKOUT_CURRENCIES:
        return mapped, "国家币种映射"
    return "USD", f"OpenAI币种回退（{detected or mapped or '未知'}→USD）"


COUNTRY_CURRENCY = {
    country: normalize_checkout_currency(country, currency)[0]
    for country, currency in sc.COUNTRY_CURRENCY.items()
}

_TOKEN_JOB_LOCKS: dict[str, threading.Lock] = {}
_TOKEN_JOB_LOCKS_GUARD = threading.Lock()


def checkout_token_lock(raw_token: str) -> threading.Lock:
    key = hashlib.sha256(str(raw_token or "").strip().encode("utf-8")).hexdigest()
    with _TOKEN_JOB_LOCKS_GUARD:
        return _TOKEN_JOB_LOCKS.setdefault(key, threading.Lock())

PAYPAL_CHECKOUT_REGIONS = {
    country: currency
    for country, currency in sc.COUNTRY_CURRENCY.items()
    if currency in OPENAI_CHECKOUT_CURRENCIES
}


def normalize_paypal_checkout_region(country: str, detected_currency: str = "") -> tuple[str, str, str]:
    # Prefer the proxy country native PayPal Checkout; otherwise use DE/EUR.
    country = str(country or "US").strip().upper()
    detected = str(detected_currency or "").strip().upper()
    direct_countries = {str(item).upper() for item in getattr(sc, "PAYPAL_ORDER_COUNTRIES", [])}
    if country in direct_countries:
        currency, source = normalize_checkout_currency(country, detected)
        return country, currency, f"\u5f53\u524d\u56fd\u5bb6\u652f\u6301 PayPal\uff08{source}\uff09"
    return "DE", "EUR", f"\u5f53\u524d\u56fd\u5bb6 {country} \u672a\u5217\u5165 PayPal \u8d26\u5355\u5730\u533a\uff0c\u56de\u9000 DE/EUR"


class ProxySentinel(BaseSentinel):
    def __init__(self, proxy: str | None, cookies: dict[str, str]):
        super().__init__(impersonate="firefox144", cookies=cookies)
        self.proxy = proxy

    async def _get_session(self):
        if not self._session:
            kwargs: dict[str, Any] = {"impersonate": "firefox144", "timeout": 70}
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            self._session = requests.AsyncSession(**kwargs)
        return self._session


def _decode_jwt(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()).decode())
    except Exception:
        return {}


def extract_access_token(raw: str) -> tuple[str, dict]:
    raw = str(raw or "").strip()
    if not raw:
        raise ValueError("请填写 Access Token 或 Session JSON")
    token = ""
    meta: dict[str, Any] = {}
    if raw.startswith("{"):
        data = json.loads(raw)
        token = str(data.get("accessToken") or data.get("access_token") or "")
        account = data.get("account") or {}
        if isinstance(account, dict):
            meta.update(account)
    if not token:
        match = re.search(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", raw)
        token = match.group(0) if match else raw.splitlines()[0].strip()
    if token.count(".") < 2:
        raise ValueError("Access Token 格式未识别")
    claims = _decode_jwt(token)
    meta.update({
        "email": claims.get("email") or meta.get("email") or "",
        "exp": claims.get("exp"),
        "account_id": (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
            or meta.get("id") or "",
    })
    if meta.get("exp") and int(meta["exp"]) <= int(time.time()):
        raise ValueError("Access Token 已过期")
    return token, meta


def normalize_proxy(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""

    def host_port(text: str) -> tuple[str, int]:
        text = text.strip()
        if text.startswith("[") and "]:" in text:
            host, port_text = text[1:].split("]:", 1)
            host = f"[{host}]"
        else:
            if ":" not in text:
                raise ValueError("代理缺少端口")
            host, port_text = text.rsplit(":", 1)
        if not host or not port_text.isdigit():
            raise ValueError("代理主机或端口格式不正确")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError("代理端口超出范围")
        return host, port

    def credentials(text: str) -> tuple[str, str]:
        if ":" not in text:
            raise ValueError("代理凭据格式应为 username:password")
        username, password = text.split(":", 1)
        if not username or not password:
            raise ValueError("代理用户名和密码为空")
        return username, password

    def build(scheme: str, host: str, port: int, username: str = "", password: str = "") -> str:
        auth = ""
        if username or password:
            auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
        return f"{scheme}://{auth}{host}:{port}"

    if "://" in value:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError(f"代理协议 {scheme} 暂未支持")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("代理端口格式不正确") from exc
        if not parsed.hostname or port is None:
            raise ValueError("代理 URL 缺少主机或端口")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        return build(scheme, host, port, unquote(parsed.username or ""), unquote(parsed.password or ""))

    if value.count("@") == 1:
        left, right = value.split("@", 1)
        try:
            username, password = credentials(left)
            host, port = host_port(right)
            return build("http", host, port, username, password)
        except ValueError:
            host, port = host_port(left)
            username, password = credentials(right)
            return build("http", host, port, username, password)

    parts = value.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        host, port = host_port(f"{parts[0]}:{parts[1]}")
        return build("http", host, port, parts[2], ":".join(parts[3:]))
    if len(parts) >= 4 and parts[-1].isdigit():
        host, port = host_port(f"{parts[-2]}:{parts[-1]}")
        return build("http", host, port, parts[0], ":".join(parts[1:-2]))

    host, port = host_port(value)
    return build("http", host, port)


def normalize_proxy_pool(raw: Any, label: str) -> list[str]:
    if isinstance(raw, (list, tuple)):
        values = [str(item or "").strip() for item in raw]
    else:
        values = [line.strip() for line in str(raw or "").replace("\r", "").split("\n")]
    values = [value for value in values if value]
    if len(values) > 500:
        raise ValueError(f"{label}最多填写 500 条")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values, 1):
        try:
            proxy = normalize_proxy(value)
        except ValueError as exc:
            raise ValueError(f"{label}第 {index} 条：{exc}") from exc
        if proxy not in seen:
            normalized.append(proxy)
            seen.add(proxy)
    return normalized


def fetch_dynamic_attempt_proxy(country: str, session_time: int = 10) -> str:
    """Fetch exactly one fresh regional proxy for the current outer attempt."""

    country = str(country or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError(f"Invalid dynamic proxy country: {country or '-'}")
    session_time = min(120, max(1, int(session_time or 10)))
    global DYNAMIC_PROXY_API_LAST_AT
    last_error = ""
    for api_attempt in range(1, 5):
        try:
            with DYNAMIC_PROXY_API_LOCK:
                wait_seconds = DYNAMIC_PROXY_API_MIN_INTERVAL - (
                    time.monotonic() - DYNAMIC_PROXY_API_LAST_AT
                )
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                response = requests.get(
                    DYNAMIC_PROXY_API_URL,
                    params={
                        "region": country,
                        "num": 1,
                        "time": session_time,
                        "format": "1",
                        "type": "txt",
                    },
                    timeout=25,
                    impersonate="firefox144",
                )
                DYNAMIC_PROXY_API_LAST_AT = time.monotonic()
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}")
            proxies = normalize_proxy_pool(response.text, f"{country} dynamic proxy")
            if not proxies:
                raise RuntimeError("empty response")
            return proxies[0]
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if api_attempt < 4:
                time.sleep(0.25 * api_attempt + random.random() * 0.2)
    raise RuntimeError(
        f"Dynamic proxy API did not return a valid {country} proxy after 4 attempts: {last_error}"
    )


def generate_cpf() -> str:
    digits = [secrets.randbelow(10) for _ in range(9)]
    for weights in (range(10, 1, -1), range(11, 1, -1)):
        value = 11 - sum(number * weight for number, weight in zip(digits, weights)) % 11
        digits.append(0 if value >= 10 else value)
    return "".join(map(str, digits))


def generate_cnpj() -> str:
    digits = [secrets.randbelow(10) for _ in range(12)]
    for weights in ((5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)):
        value = 11 - sum(number * weight for number, weight in zip(digits, weights)) % 11
        digits.append(0 if value >= 10 else value)
    return "".join(map(str, digits))


def generate_pix_identity(kind: str) -> dict[str, str]:
    first_names = ("Lucas", "Gabriel", "Rafael", "Matheus", "Mariana", "Beatriz", "Camila", "Larissa")
    last_names = ("Silva", "Santos", "Oliveira", "Souza", "Pereira", "Costa", "Rodrigues", "Almeida")
    locations = (
        ("Avenida Paulista 1000", "Sao Paulo", "SP", "01310-100"),
        ("Rua da Assembleia 10", "Rio de Janeiro", "RJ", "20011-901"),
        ("Avenida Afonso Pena 1500", "Belo Horizonte", "MG", "30130-005"),
        ("Rua XV de Novembro 500", "Curitiba", "PR", "80020-310"),
        ("Avenida Sete de Setembro 800", "Salvador", "BA", "40060-001"),
    )
    first, last = secrets.choice(first_names), secrets.choice(last_names)
    line1, city, state, postal_code = secrets.choice(locations)
    if kind == "cnpj":
        name = f"{first.upper()} {last.upper()} COMERCIO E SERVICOS LTDA"
        source = "generated_cnpj"
    else:
        name = f"{first} {last}"
        source = "generated_cpf"
    return {
        "name": name,
        "email": f"{first.lower()}.{last.lower()}{secrets.randbelow(9000) + 1000}@outlook.com",
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "source": source,
    }


def lookup_cnpj_identity(cnpj: str) -> dict[str, str]:
    value = re.sub(r"\D", "", cnpj or "")
    if len(value) != 14:
        return {}
    resp = requests.get(
        f"https://brasilapi.com.br/api/cnpj/v1/{value}",
        headers={"Accept": "application/json", "User-Agent": sc.CHROME_UA},
        timeout=25,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"CNPJ 登记信息查询 HTTP {resp.status_code}")
    data = resp.json() or {}
    street = " ".join(filter(None, [str(data.get("logradouro") or "").strip(), str(data.get("numero") or "").strip()]))
    complement = str(data.get("complemento") or "").strip()
    if complement:
        street = f"{street}, {complement}" if street else complement
    return {
        "name": str(data.get("razao_social") or data.get("nome_fantasia") or "").strip(),
        "line1": street,
        "city": str(data.get("municipio") or "").strip(),
        "state": str(data.get("uf") or "").strip(),
        "postal_code": str(data.get("cep") or "").strip(),
        "status": str(data.get("descricao_situacao_cadastral") or "").strip(),
        "source": "brasilapi_cnpj",
    }


async def sentinel_headers(
    proxy: str,
    flow: str,
    device_id: str,
    cookie: str,
    *,
    use_sen: bool = True,
    use_so: bool = True,
) -> dict[str, str]:
    if not use_sen and not use_so:
        return {}
    last_error = "empty token"
    for attempt in range(2):
        provider = ProxySentinel(proxy or None, {"oai-did": cookie})
        try:
            token, so, diag = await provider.get_token_pair(flow, device_id)
            init_error = str(diag.get("init_error") or getattr(provider, "_last_init_error", "") or "")
            if use_sen and not token:
                last_error = init_error or "empty token"
                if "SENTINEL_INIT_BLOCKED" in last_error:
                    break
            elif use_sen and diag.get("turnstile_required") and not diag.get("has_t"):
                last_error = str(diag.get("vm_error") or "required t proof was not generated")
            elif use_so and diag.get("so_required") and not diag.get("has_so"):
                last_error = str(diag.get("vm_error") or "required so proof was not generated")
            else:
                out: dict[str, str] = {}
                if use_sen and token:
                    out["OpenAI-Sentinel-Token"] = json.dumps(token, separators=(",", ":"))
                if use_so and so:
                    out["OpenAI-Sentinel-SO-Token"] = json.dumps(so, separators=(",", ":"))
                return out
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        finally:
            await provider.close()
        if attempt == 0:
            await asyncio.sleep(0.6)
    raise RuntimeError(f"Sentinel token generation failed after fresh-session retry: {last_error[:320]}")


def checkout_payload(options: dict, meta: dict) -> dict[str, Any]:
    plan = options["plan"]
    country = options.get("checkout_country") or options["country"]
    requested_currency = options.get("checkout_currency") or options["currency"]
    currency, _currency_source = normalize_checkout_currency(country, requested_currency)
    options["currency"] = currency
    options["checkout_currency"] = currency
    billing = {"country": country, "currency": currency}
    common: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": PLANS[plan],
        "billing_details": billing,
        "cancel_url": "https://chatgpt.com/",
        "checkout_ui_mode": "custom" if options["link_type"] != "hosted" else "redirect",
        "check_card_proxy": True,
    }
    promo = options.get("promo_campaign", "").strip()
    if plan == "team":
        common["entry_point"] = "team_workspace_purchase_modal"
        team_data = {
            "workspace_name": options.get("workspace_name") or "Codex Workspace",
            "price_interval": options.get("price_interval") or "month",
            "seat_quantity": int(options.get("seat_quantity") or 5),
        }
        if options.get("workspace_id"):
            team_data["existing_workspace_id"] = options["workspace_id"]
        common["team_plan_data"] = team_data
        if options.get("promo_code"):
            common["promo_code"] = options["promo_code"]
    elif plan == "codex_low":
        common["entry_point"] = "codex_team_start"
        common["usage_based_workspace_credit_purchase_data"] = {
            "quantity": int(options.get("credit_quantity") or 13),
            "unit": "credit",
            "workspace_name": options.get("workspace_name") or "Codex Space",
            "plan_type": "team",
            "auto_top_up_enabled": True,
        }
    elif plan == "plus" and options.get("use_promo") and (
        options.get("link_type") not in {"pix", "momo", "gcash", "paypal", "upi", "ideal", "twint", "kakao"}
        or options.get("promo_on_create")
    ):
        common["promo_campaign"] = {
            "promo_campaign_id": promo or "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }
    return common


def create_checkout(
    token: str,
    payload: dict,
    proxy: str,
    device_id: str,
    did: str,
    log,
    *,
    use_sen: bool = True,
    use_so: bool = True,
) -> dict:
    http = sc.build_http(proxy or None)
    try:
        http.cookies.set("oai-did", did, domain="chatgpt.com")
    except Exception:
        pass
    try:
        http.get(
            "https://chatgpt.com/api/auth/csrf",
            headers={"User-Agent": sc.CHROME_UA, "Accept": "application/json,text/plain,*/*"},
            timeout=20,
        )
    except Exception as exc:
        log(f"ChatGPT 暖身提示：{type(exc).__name__}")
    s_headers = asyncio.run(sentinel_headers(
        proxy, "chatgpt_checkout", device_id, did, use_sen=use_sen, use_so=use_so,
    ))
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": sc.CHROME_UA,
        "OAI-Language": "zh-CN",
        "OAI-Device-Id": device_id,
        **s_headers,
    }
    resp = http.post(sc.OPENAI_CHECKOUT_URL, json=payload, headers=headers, timeout=60)
    text = resp.text or ""
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI Checkout HTTP {resp.status_code}: {text[:500]}")
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"OpenAI Checkout 返回非 JSON：{text[:300]}")
    raw_sid = str(data.get("checkout_session_id") or "")
    sid_match = re.search(r"cs_(?:live|test)_[A-Za-z0-9]+", raw_sid)
    sid = sid_match.group(0) if sid_match else ""
    url = data.get("url") or ""
    if not sid and url:
        match = re.search(r"cs_(?:live|test)_[A-Za-z0-9]+", url)
        sid = match.group(0) if match else ""
    if not sid:
        match = re.search(r"cs_(?:live|test)_[A-Za-z0-9]+", text)
        sid = match.group(0) if match else ""
    if not sid:
        custom_match = re.search(r"oaics_[A-Za-z0-9]+", "\n".join((raw_sid, str(url), text)))
        if custom_match:
            custom_id = custom_match.group(0)
            processor = str(data.get("processor_entity") or "openai_ie").strip() or "openai_ie"
            data["checkout_session_id"] = custom_id
            data["checkout_provider"] = str(data.get("checkout_provider") or "open_ai")
            data["processor_entity"] = processor
            data["is_custom_checkout"] = True
            data["source_checkout_url"] = str(url or "")
            data["checkout_url"] = f"https://chatgpt.com/checkout/{processor}/{custom_id}"
            return {"data": data, "http": http}
    data["checkout_session_id"] = sid
    data["checkout_url"] = normalize_hosted_checkout_url(url, sid)
    return {"data": data, "http": http}


def preflight_trial_eligibility(token: str, account_id: str, proxy: str, device_id: str, did: str, log) -> dict:
    rust_base = str(os.getenv("PAY153_RUST_URL") or "").strip().rstrip("/")
    if rust_base:
        try:
            rust_response = requests.post(
                f"{rust_base}/api/v1/offers/check",
                json={
                    "access_token": token,
                    "account_id": account_id,
                    "proxy": proxy,
                    "transport": str(os.getenv("PAY153_RUST_TRANSPORT") or "curl_cffi"),
                },
                timeout=50,
            )
            if rust_response.status_code == 200:
                rust_data = rust_response.json() or {}
                offer = rust_data.get("offer") or {}
                campaign_id = str(offer.get("campaign_id") or "").strip()
                normalized = {
                    "promotion_source": "pay153_rust",
                    "promotion_http_status": 200,
                    "one_click_trial_eligible": bool(offer.get("eligible")),
                    "promo_campaign_id": campaign_id,
                    "promotion_label": str(offer.get("label") or ""),
                    "promotion_title": str(offer.get("title") or ""),
                    "promotion_discount_percentage": offer.get("discount_percentage"),
                    "promotion_duration_months": (
                        offer.get("duration_periods")
                        if offer.get("duration_unit") == "month"
                        else None
                    ),
                    "promotion_duration_period": str(offer.get("duration_unit") or ""),
                    "promotion_processor": str(offer.get("processor") or ""),
                    "promotion_transport": str(offer.get("transport") or ""),
                }
                log(
                    f"Rust \u4f18\u60e0\u68c0\u6d4b\u5b8c\u6210\uff1a"
                    f"{campaign_id or '\u5f53\u524d\u65e0\u4f18\u60e0'}\uff08{normalized['promotion_transport']}\uff09"
                )
                return normalized
            log(f"Rust \u4f18\u60e0\u68c0\u6d4b HTTP {rust_response.status_code}\uff0c\u56de\u9000 Python")
        except Exception as rust_exc:
            log(f"Rust \u4f18\u60e0\u68c0\u6d4b\u5f02\u5e38\uff1a{type(rust_exc).__name__}\uff0c\u56de\u9000 Python")

    """Read the account campaign catalog instead of the stale payment-method marker."""
    if not account_id:
        return {}
    http = sc.build_http(proxy)
    try:
        http.cookies.set("oai-did", did, domain="chatgpt.com")
    except Exception:
        pass
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "OAI-Language": "zh-CN",
        "OAI-Device-Id": device_id,
        "ChatGPT-Account-ID": account_id,
    }
    try:
        resp = http.get(
            "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
            headers=headers,
            timeout=35,
        )
        if resp.status_code != 200:
            log(f"\u8d26\u53f7\u6d3b\u52a8\u76ee\u5f55\u8fd4\u56de HTTP {resp.status_code}")
            return {"promotion_source": "accounts_check", "promotion_http_status": resp.status_code}
        data = resp.json() or {}
        accounts = data.get("accounts") or {}
        account = accounts.get(account_id) or accounts.get("default") or {}
        campaigns = account.get("eligible_promo_campaigns") or {}
        plus = campaigns.get("plus") or {}
        metadata = plus.get("metadata") or {}
        discount_data = metadata.get("discount") or {}
        duration_data = metadata.get("duration") or {}
        campaign_id = str(plus.get("id") or plus.get("campaign_id") or "").strip()
        discount = discount_data.get("percentage")
        duration = duration_data.get("num_periods")
        duration_period = duration_data.get("period") or ""
        label = metadata.get("promotion_type_label") or metadata.get("title") or metadata.get("summary") or ""
        processor = metadata.get("processor") or ""
        normalized = {
            "promotion_source": "accounts_check",
            "promotion_http_status": resp.status_code,
            "one_click_trial_eligible": bool(campaign_id),
            "promo_campaign_id": campaign_id,
            "promotion_label": label,
            "promotion_title": metadata.get("title") or "",
            "promotion_discount_percentage": discount,
            "promotion_duration_months": duration if duration_period == "month" else None,
            "promotion_duration_period": duration_period,
            "promotion_processor": processor,
            "eligible_offers": account.get("eligible_offers") or {},
        }
        if campaign_id:
            log(f"\u8d26\u53f7\u6d3b\u52a8\u76ee\u5f55\u5df2\u5339\u914d\uff1a{campaign_id}\uff08{label or 'Plus \u6d3b\u52a8'}\uff09")
        else:
            log("\u8d26\u53f7\u6d3b\u52a8\u76ee\u5f55\u672a\u8fd4\u56de Plus \u4f18\u60e0")
        return normalized
    except Exception as exc:
        log(f"\u8d26\u53f7\u6d3b\u52a8\u76ee\u5f55\u8bfb\u53d6\u5931\u8d25\uff1a{type(exc).__name__}")
        return {}

def promo_campaign_from_payload(payload: Any) -> str:
    """Extract the account-specific campaign id returned by OpenAI.

    Campaign ids are not guaranteed to stay equal to the UI label.  The update
    endpoint may accept a stale/default id and still return ``success=true``,
    while final approval rejects it as ``invalid_promotion``.
    """
    candidates: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_lower = str(key).lower()
                if key_lower in {
                    "promo_campaign_id",
                    "promotion_campaign_id",
                    "campaign_id",
                } and isinstance(item, str):
                    candidate = item.strip()
                    if candidate:
                        candidates.append(candidate)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return candidates[0] if candidates else ""


def proxy_country_hint(proxy: str) -> str:
    value = unquote(str(proxy or ""))
    for pattern in (
        r"(?i)(?:^|[-_:])region[-_=]?([a-z]{2})(?:[-_:]|$)",
        r"(?i)(?:^|[-_:])country[-_=]?([a-z]{2})(?:[-_:]|$)",
        r"(?i)(?:^|[-_:])location[-_=]?([a-z]{2})(?:[-_:]|$)",
    ):
        match = re.search(pattern, value)
        if match:
            return match.group(1).upper()
    return ""


def proxy_geo(proxy: str) -> dict[str, str]:
    hinted_country = proxy_country_hint(proxy)
    if hinted_country:
        return {
            "country": hinted_country, "currency": "", "region": "代理参数",
            "city": "", "postal": "", "timezone": "", "source": "proxy_hint",
        }
    probes = (
        "https://ipapi.co/json/",
        "https://ipwho.is/",
        "https://api.country.is/",
        "https://api.ip.sb/geoip",
        "https://ipinfo.io/json",
    )
    errors: list[str] = []
    for url in probes:
        try:
            # A failed TLS tunnel can poison a keep-alive connection. Use a
            # fresh browser session for each independent geo provider.
            http = sc.build_http(proxy)
            resp = http.get(url, timeout=12)
            if resp.status_code != 200:
                errors.append(f"HTTP {resp.status_code}")
                continue
            data = resp.json() or {}
            if data.get("success") is False:
                errors.append("provider_failed")
                continue
            country = str(
                data.get("country_code") or data.get("countryCode")
                or data.get("country") or data.get("country_code2") or ""
            ).upper()
            if not re.fullmatch(r"[A-Z]{2}", country):
                continue
            currency_value = data.get("currency") or ""
            if isinstance(currency_value, dict):
                currency_value = currency_value.get("code") or ""
            currency = str(currency_value).strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                currency = ""
            return {
                "country": country,
                "currency": currency,
                "region": str(data.get("region") or data.get("region_name") or data.get("regionName") or ""),
                "city": str(data.get("city") or ""),
                "postal": str(data.get("postal") or data.get("zip") or ""),
                "timezone": str(data.get("timezone") or ""),
                "source": url,
            }
        except Exception as exc:
            errors.append(type(exc).__name__)
    raise RuntimeError(f"代理地区检测失败：{' / '.join(errors[-5:]) or 'no response'}")


_PROXY_GEO_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_PROXY_GEO_CACHE_LOCK = threading.Lock()


def proxy_geo_cached(proxy: str, ttl: int = 900) -> dict[str, str]:
    now = time.time()
    with _PROXY_GEO_CACHE_LOCK:
        cached = _PROXY_GEO_CACHE.get(proxy)
        if cached and now - cached[0] <= ttl:
            return dict(cached[1])
    data = proxy_geo(proxy)
    with _PROXY_GEO_CACHE_LOCK:
        _PROXY_GEO_CACHE[proxy] = (now, dict(data))
    return data


def select_paypal_exit_proxy(preferred: str, pool: list[str], scan_limit: int = 24) -> tuple[str, dict[str, str], list[str]]:
    """Pick a proxy whose detected country has an exact OpenAI billing pair."""
    rest = [proxy for proxy in dict.fromkeys(pool) if proxy and proxy != preferred]
    random.SystemRandom().shuffle(rest)
    candidates = ([preferred] if preferred else []) + rest
    candidates = candidates[:max(1, min(int(scan_limit), len(candidates)))]
    if not candidates:
        raise RuntimeError("代理池 2 为空")

    rejected: list[str] = []
    executor = ThreadPoolExecutor(max_workers=min(6, len(candidates)), thread_name_prefix="paypal-geo")
    future_map = {executor.submit(proxy_geo_cached, proxy): proxy for proxy in candidates}
    try:
        for future in as_completed(future_map):
            proxy = future_map[future]
            try:
                geo = future.result()
            except Exception:
                continue
            country = str(geo.get("country") or "").upper()
            if re.fullmatch(r"[A-Z]{2}", country):
                for pending in future_map:
                    if pending is not future:
                        pending.cancel()
                return proxy, geo, rejected
            if country and country not in rejected:
                rejected.append(country)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    summary = "/".join(rejected[:12]) or "未识别"
    raise RuntimeError(
        f"代理池 2 本轮未找到 OpenAI 支持的 PayPal 账单地区；已检测：{summary}。"
        "系统将更换代理继续尝试"
    )


def proxy_country(proxy: str, expected_country: str = "") -> tuple[str, str]:
    expected = str(expected_country or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", expected):
        return expected, "国家代理池"
    data = proxy_geo_cached(proxy)
    return data["country"], data["region"]


def update_checkout_promo(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    campaign_id: str,
    log,
    *,
    device_id: str = "",
) -> dict:
    body = {
        "checkout_session_id": session_id,
        "processor_entity": processor_entity,
        "plan_name": PLANS["plus"],
        "price_interval": "month",
        "seat_quantity": 1,
        "discount_code": None,
        "promo_campaign": {
            "promo_campaign_id": campaign_id or "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    resp = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/update",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
            "User-Agent": sc.CHROME_UA,
            "OAI-Language": "zh-CN",
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/update",
            "x-openai-target-route": "/backend-api/payments/checkout/update",
        },
        timeout=45,
    )
    text = resp.text or ""
    log(f"[promo] checkout/update: {resp.status_code} {text[:180]}")
    if resp.status_code != 200:
        raise RuntimeError(f"应用 Plus 优惠失败：HTTP {resp.status_code} {text[:300]}")
    try:
        return resp.json() or {}
    except Exception:
        return {}


def fetch_custom_checkout_session(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    device_id: str,
) -> dict[str, Any]:
    resp = http.get(
        f"https://chatgpt.com/backend-api/payments/checkout/{processor_entity}/{session_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
            "User-Agent": sc.CHROME_UA,
            "OAI-Device-Id": device_id,
        },
        timeout=45,
    )
    text = resp.text or ""
    if resp.status_code != 200:
        raise RuntimeError(f"读取自定义 Checkout 失败：HTTP {resp.status_code} {text[:300]}")
    try:
        return resp.json() or {}
    except Exception:
        raise RuntimeError(f"读取自定义 Checkout 返回非 JSON：{text[:300]}")


def submit_custom_checkout_taxes(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    billing: dict[str, Any],
    currency: str,
    device_id: str,
) -> dict[str, Any]:
    address = dict(billing.get("address") or {})
    clean_address = {
        "country": str(address.get("country") or "PH").upper(),
        "line1": str(address.get("line1") or ""),
        "line2": str(address.get("line2") or ""),
        "city": str(address.get("city") or ""),
        "state": str(address.get("state") or ""),
        "postal_code": str(address.get("postal_code") or ""),
    }
    resp = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/taxes",
        json={
            "checkout_session_id": session_id,
            "checkout_email": str(billing.get("email") or ""),
            "billing_country": clean_address["country"],
            "billing_name": str(billing.get("name") or ""),
            "currency": str(currency or "PHP").upper(),
            "tax_id": str(billing.get("tax_id") or "") or None,
            "processor_entity": processor_entity,
            "billing_address": clean_address,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
            "User-Agent": sc.CHROME_UA,
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/taxes",
            "x-openai-target-route": "/backend-api/payments/checkout/taxes",
        },
        timeout=50,
    )
    text = resp.text or ""
    if resp.status_code != 200:
        raise RuntimeError(
            f"提交 {clean_address['country']} 账单地址失败：HTTP {resp.status_code} {text[:300]}"
        )
    try:
        payload = resp.json() or {}
    except Exception as exc:
        raise RuntimeError(
            f"提交 {clean_address['country']} 账单地址返回非 JSON：{text[:300]}"
        ) from exc
    checkout = payload.get("checkout_session") or {}
    return checkout if isinstance(checkout, dict) else {}


def confirm_custom_checkout_method(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    custom_payment_method_id: str,
    proxy: str,
    device_id: str,
    did: str,
    *,
    use_sen: bool = True,
    use_so: bool = True,
    method_name: str = "GCash",
) -> dict[str, Any]:
    sentinel = asyncio.run(sentinel_headers(
        proxy, "checkout_session_approval", device_id, did,
        use_sen=use_sen, use_so=use_so,
    ))
    resp = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/confirm",
        json={
            "checkout_session_id": session_id,
            "selected_payment_method_type": custom_payment_method_id,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
            "User-Agent": sc.CHROME_UA,
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/confirm",
            "x-openai-target-route": "/backend-api/payments/checkout/confirm",
            **sentinel,
        },
        timeout=50,
    )
    text = resp.text or ""
    if resp.status_code != 200:
        raise RuntimeError(f"确认 {method_name} 支付方式失败：HTTP {resp.status_code} {text[:300]}")
    try:
        payload = resp.json() or {}
    except Exception:
        raise RuntimeError(f"确认 {method_name} 支付方式返回非 JSON：{text[:300]}")
    if str(payload.get("status") or "").lower() != "success":
        status = str(payload.get("status") or "unknown").lower()
        if status == "blocked":
            raise RuntimeError(f"CUSTOM_CONFIRM_BLOCKED: {method_name} 支付方式确认被上游拦截")
        raise RuntimeError(f"确认 {method_name} 支付方式失败：status={status}；{text[:300]}")
    return payload


def start_custom_checkout_method(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    custom_payment_method_id: str,
    device_id: str,
) -> dict[str, Any]:
    resp = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start",
        json={
            "checkout_session_id": session_id,
            "custom_payment_method_type_id": custom_payment_method_id,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
            "User-Agent": sc.CHROME_UA,
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/custom_payment_method/start",
            "x-openai-target-route": "/backend-api/payments/checkout/custom_payment_method/start",
        },
        timeout=60,
    )
    text = resp.text or ""
    if resp.status_code != 200:
        raise RuntimeError(f"启动 GCash 支付失败：HTTP {resp.status_code} {text[:300]}")
    try:
        payload = resp.json() or {}
    except Exception:
        raise RuntimeError(f"启动 GCash 支付返回非 JSON：{text[:300]}")
    action = payload.get("next_action") or {}
    if str(payload.get("status") or "").lower() != "requires_action" or not action.get("url"):
        raise RuntimeError(f"GCash 未返回跳转链接：{text[:300]}")
    return payload



def approve_checkout(
    token: str,
    session_id: str,
    processor: str,
    proxy: str,
    device_id: str,
    did: str,
    *,
    http=None,
    log=lambda _message: None,
) -> dict:
    headers = asyncio.run(sentinel_headers(proxy, "checkout_session_approval", device_id, did))
    http = http or sc.build_http(proxy or None)
    try:
        http.cookies.set("oai-did", did, domain="chatgpt.com")
    except Exception:
        pass
    body = {"checkout_session_id": session_id, "processor_entity": processor}
    resp = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor}/{session_id}",
            "OAI-Device-Id": device_id,
            "User-Agent": sc.CHROME_UA,
            "OAI-Language": "zh-CN",
            "x-openai-target-path": "/backend-api/payments/checkout/approve",
            "x-openai-target-route": "/backend-api/payments/checkout/approve",
            **headers,
        },
        timeout=40,
    )
    text = resp.text or ""
    log(f"[stripe] manual_approval approve+sentinel: {resp.status_code} {text[:160]}")
    if resp.status_code != 200:
        raise RuntimeError(f"Checkout approve HTTP {resp.status_code}: {text[:300]}")
    try:
        payload = resp.json() or {}
    except Exception:
        payload = {}
    result = str(payload.get("result") or "").lower()
    if result and result != "approved":
        raise RuntimeError(f"manual_approval approve blocked: result={result}")
    return payload


class JobStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.file_lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.worker_limit = max(1, int(os.getenv("PAY153_WORKERS", "20")))
        self.pool = ThreadPoolExecutor(max_workers=self.worker_limit)
        self.internal_worker_limit = max(1, int(os.getenv("PAY153_INTERNAL_WORKERS", "5")))
        self.internal_pool = ThreadPoolExecutor(max_workers=self.internal_worker_limit)
        self.pending: deque[tuple[str, dict]] = deque()
        self.active_workers = 0
        threading.Thread(target=self._dispatch_loop, name="pay153-dispatcher", daemon=True).start()

    @staticmethod
    def _is_major_log(message: str) -> bool:
        text = str(message or "")
        lowered = text.lower()
        return any(marker in text for marker in (
            "提链尝试", "代理池", "代理校验", "自动设置地区", "计划=",
            "优惠已", "优惠更新", "优惠同步", "金额校验", "今日应付",
            "Checkout 创建", "支付方式已创建", "二维码生成", "链接生成",
            "提交 Checkout approval", "PayPal OAICS", "[preflight]", "[checkout]",
            "[provider]", "错误：", "本次未成功", "轮未命中",
        )) or any(marker in lowered for marker in (
            "init ok", "payment_method:", "manual_approval approve", "checkout/update",
        ))

    def _append_backend_log(self, job_id: str, kind: str, message: str):
        safe_message = re.sub(r"eyJ[A-Za-z0-9_.-]{40,}", "[TOKEN]", str(message))
        day = time.strftime("%Y-%m-%d")
        path = BACKEND_LOG_DIR / day / f"{job_id}.log"
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{kind}] {safe_message}\n"
        try:
            with self.file_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except Exception:
            pass

    def _record_success(self, job_id: str, result: dict):
        """Persist successful link results so batch runs survive restarts."""
        try:
            record = {
                "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "job_id": job_id,
                "combination": "{}-{}".format(
                    str(result.get("entry_country") or "?").upper(),
                    str(result.get("payment_proxy_country") or result.get("checkout_country") or "?").upper(),
                ),
                "attempt": result.get("attempt"),
                "max_attempts": result.get("max_attempts"),
                "account_email": result.get("account_email") or "",
                "link_type": result.get("link_type") or "",
                "checkout_amount": result.get("checkout_amount"),
                "currency": result.get("checkout_currency") or result.get("currency") or "",
                "url": result.get("paypal_approve_url") or result.get("paypal_link") or result.get("url") or result.get("link") or result.get("provider_redirect_url") or result.get("checkout_url") or "",
            }
            path = ROOT / "data" / "success_links.jsonl"
            with self.file_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                path.chmod(0o600)
        except Exception:
            pass

    def _refresh_queue_locked(self):
        for position, (job_id, _options) in enumerate(self.pending, 1):
            job = self.jobs.get(job_id)
            if not job:
                continue
            job["queue_position"] = position
            job["text"] = f"正在排队，前方 {position - 1} 个任务" if position > 1 else "正在排队，等待执行"
            job["updated_at"] = time.time()

    def _worker_done(self, _future):
        with self.condition:
            self.active_workers = max(0, self.active_workers - 1)
            self.condition.notify_all()

    def _internal_worker_done(self, _future):
        # Private jobs use a separate executor and do not consume public
        # queue/RPM capacity.
        with self.condition:
            self.condition.notify_all()

    def _dispatch_loop(self):
        while True:
            with self.condition:
                now = time.time()
                if not self.pending or self.active_workers >= self.worker_limit:
                    self.condition.wait(timeout=1)
                    continue

                job_id, options = self.pending.popleft()
                job = self.jobs.get(job_id)
                if not job or job.get("cancel"):
                    if job:
                        job.update(status="cancelled", percent=100, text="任务已停止", queue_position=0)
                    self._refresh_queue_locked()
                    continue

                self.active_workers += 1
                job.update(text="排队完成，即将开始", queue_position=0, dispatched=True, updated_at=now)
                self._refresh_queue_locked()
                future = self.pool.submit(self._run, job_id, options)
                future.add_done_callback(self._worker_done)

    def create(self, options: dict, *, internal: bool = False) -> str:
        job_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self.lock:
            expired = [
                key for key, value in self.jobs.items()
                if now - float(value.get("updated_at") or now) > 7200
            ]
            for key in expired:
                self.jobs.pop(key, None)
            if len(self.jobs) >= 500:
                oldest = sorted(self.jobs, key=lambda key: self.jobs[key].get("updated_at", 0))
                for key in oldest[: len(self.jobs) - 499]:
                    self.jobs.pop(key, None)
            self.jobs[job_id] = {
                "id": job_id, "status": "queued", "percent": 2, "text": "任务已创建",
                "logs": [], "result": None, "error": "", "last_retry_error": "", "cancel": False,
                "created_at": now, "updated_at": now, "queue_position": 0, "dispatched": False,
            }
            options = dict(options)
            options["_internal_request"] = bool(internal)
            if internal:
                self.jobs[job_id].update(
                    internal=True,
                    dispatched=True,
                    queue_position=0,
                    text="内部任务已启动",
                )
                future = self.internal_pool.submit(self._run, job_id, options)
                future.add_done_callback(self._internal_worker_done)
            else:
                self.pending.append((job_id, options))
            self._refresh_queue_locked()
            self.condition.notify_all()
        self._append_backend_log(
            job_id,
            "SYSTEM",
            "内部任务已直接分发" if internal else "公开任务已入队",
        )
        return job_id

    def queue_position(self, job_id: str) -> int:
        with self.lock:
            return int((self.jobs.get(job_id) or {}).get("queue_position") or 0)

    def update(self, job_id: str, **fields):
        backend_line = ""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            # A running worker can still be inside a synchronous HTTP request
            # for a few seconds after the user presses stop.  Keep the public
            # state terminal immediately and do not let that worker overwrite
            # `cancelled` with another running/error progress update.
            if (
                job.get("cancel")
                and job.get("status") == "cancelled"
                and fields.get("status") != "cancelled"
            ):
                return
            job.update(fields)
            job["updated_at"] = time.time()
            if "text" in fields or "status" in fields:
                backend_line = f"status={job.get('status')} percent={job.get('percent')} text={job.get('text')}"
        if backend_line:
            self._append_backend_log(job_id, "STATUS", backend_line)

    def log(self, job_id: str, message: str):
        safe = re.sub(r"eyJ[A-Za-z0-9_.-]{40,}", "[TOKEN]", str(message))
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None:
                job["logs"].append({
                    "time": time.strftime("%H:%M:%S"),
                    "message": safe[:800],
                    "major": self._is_major_log(safe),
                })
                job["logs"] = job["logs"][-1000:]
                job["updated_at"] = time.time()
        self._append_backend_log(job_id, "DETAIL", safe)

    def get(self, job_id: str, public: bool = False) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            snapshot = json.loads(json.dumps(job, ensure_ascii=False)) if job else None
        if snapshot and public:
            snapshot["logs"] = [item for item in snapshot.get("logs") or [] if item.get("major")]
        return snapshot

    def cancel(self, job_id: str) -> bool:
        with self.condition:
            if job_id not in self.jobs:
                return False
            job = self.jobs[job_id]
            job["cancel"] = True
            if job.get("status") == "queued" and not job.get("dispatched"):
                self.pending = deque((jid, opts) for jid, opts in self.pending if jid != job_id)
                job.update(
                    status="cancelled", percent=100, text="任务已停止",
                    error="任务已停止", queue_position=0,
                )
                self._refresh_queue_locked()
                self._append_backend_log(job_id, "STATUS", "status=cancelled percent=100 text=任务已停止")
            else:
                # Report the terminal state at once.  Cooperative checks in
                # the worker stop the remaining stages at the next boundary.
                job.update(
                    status="cancelled", percent=100, text="任务已停止",
                    error="任务已停止", queue_position=0,
                )
                self._append_backend_log(job_id, "STATUS", "status=cancelled percent=100 text=任务已停止")
            job["updated_at"] = time.time()
            self.condition.notify_all()
            return True

    def cancelled(self, job_id: str) -> bool:
        with self.lock:
            return bool((self.jobs.get(job_id) or {}).get("cancel"))

    def ensure_not_cancelled(self, job_id: str) -> None:
        if self.cancelled(job_id):
            raise InterruptedError("任务已停止")

    def _run(self, job_id: str, options: dict):
        account_lock = checkout_token_lock(str(options.get("token_raw") or ""))
        if not account_lock.acquire(blocking=False):
            message = "同一账号已有提链任务正在运行；并发创建 Checkout 会让旧 Session 失效"
            self.log(job_id, f"错误：RuntimeError: {message}")
            self.update(job_id, status="error", percent=100, text="任务失败", error=message)
            return
        try:
            self._run_locked(job_id, options)
        finally:
            account_lock.release()

    def _run_locked(self, job_id: str, options: dict):
        if options.get("link_type") == "paypal_oaics":
            self._run_paypal_oaics(job_id, options)
            return
        max_attempts = min(50, max(1, int(options.get("retry_count") or 1)))
        used_pairs: set[tuple[str, str]] = set()
        last_error = ""
        oaics_hits = 0
        requested_paypal_country = str(
            options.get("checkout_country") or options.get("country") or "US"
        ).upper()
        direct_paypal_countries = {
            str(item).upper() for item in getattr(sc, "PAYPAL_ORDER_COUNTRIES", [])
        }
        paypal_force_de_fallback = bool(
            options.get("link_type") == "paypal"
            and requested_paypal_country not in direct_paypal_countries
        )
        if paypal_force_de_fallback:
            self.log(
                job_id,
                f"PayPal billing country {requested_paypal_country} uses DE/EUR fallback from attempt 1",
            )
        for attempt in range(1, max_attempts + 1):
            if self.cancelled(job_id):
                self.update(job_id, status="cancelled", percent=100, text="任务已停止", error="任务已停止")
                return
            current = dict(options)
            current["retry_wrapper"] = True
            if current.get("dynamic_proxy_api"):
                try:
                    entry_country = str(
                        current.get("entry_proxy_country") or current.get("country") or "US"
                    ).upper()
                    exit_country = str(
                        current.get("exit_proxy_country") or current.get("country") or entry_country
                    ).upper()
                    proxy_session_time = int(current.get("proxy_session_time") or 10)
                    entry_proxy = fetch_dynamic_attempt_proxy(entry_country, proxy_session_time)
                    if current.get("link_type") in {"pix", "momo"}:
                        exit_proxy = entry_proxy
                    else:
                        exit_proxy = fetch_dynamic_attempt_proxy(exit_country, proxy_session_time)
                    pair = (entry_proxy, exit_proxy)
                    current["entry_proxies"] = [entry_proxy]
                    current["exit_proxies"] = [exit_proxy]
                    self.log(
                        job_id,
                        f"Attempt {attempt}/{max_attempts}: proxy API issued fresh {entry_country}/{exit_country} routes",
                    )
                except Exception as exc:
                    last_error = f"Dynamic proxy fetch failed: {type(exc).__name__}: {exc}"
                    self.update(
                        job_id,
                        status="running" if attempt < max_attempts else "error",
                        percent=4 if attempt < max_attempts else 100,
                        text=("正在重新获取代理" if attempt < max_attempts else "任务失败"),
                        error=last_error[:1200],
                        last_retry_error=last_error[:500],
                    )
                    self.log(job_id, f"第 {attempt}/{max_attempts} 轮代理获取失败：{last_error[:260]}")
                    if attempt >= max_attempts:
                        return
                    time.sleep(min(4, 1 + attempt * 0.35))
                    continue
            else:
                entry_pool = current["entry_proxies"]
                exit_pool = current.get("exit_proxies") or entry_pool
                if current.get("paired_proxy_rotation"):
                    entry_proxy = entry_pool[(attempt - 1) % len(entry_pool)]
                    exit_proxy = entry_proxy if current.get("link_type") in {"pix", "momo"} else exit_pool[(attempt - 1) % len(exit_pool)]
                    pair = (entry_proxy, exit_proxy)
                else:
                    pair = None
                    for _ in range(40):
                        if current.get("link_type") in {"pix", "momo"}:
                            proxy = secrets.choice(entry_pool)
                            candidate = (proxy, proxy)
                        else:
                            candidate = (secrets.choice(entry_pool), secrets.choice(exit_pool))
                        if candidate not in used_pairs or len(used_pairs) >= len(entry_pool) * len(exit_pool):
                            pair = candidate
                            break
                    if pair is None:
                        pair = (secrets.choice(entry_pool), secrets.choice(exit_pool))
            used_pairs.add(pair)
            current["fixed_entry_proxy"], current["fixed_exit_proxy"] = pair
            if current.get("link_type") == "paypal":
                current["force_paypal_de_fallback"] = paypal_force_de_fallback
                # Strategy A creates the Checkout with the campaign already
                # attached.  This preserves the merchant's native zero-due
                # PayPal SetupIntent configuration.  Strategy B keeps the
                # existing cross-entry checkout/update flow as a fallback.
                current["promo_on_create"] = bool(
                    (attempt - 1) % 2 == 0 and not paypal_force_de_fallback
                )
            if current.get("link_type") in {"pix", "upi", "momo", "gcash"}:
                # Alternate both Stripe submission shapes across outer retries.
                # Some Checkout revisions accept a pre-created pm_* while
                # others only complete the local mandate with inline data.
                strategy_cycle = (
                    ("standalone", "late_promo", "inline")
                    if current.get("link_type") == "pix"
                    else (("late_promo", "inline", "standalone") if current.get("link_type") == "momo" else (
                        ("go_b", "go_b", "inline", "late_promo")
                        if current.get("use_promo", False)
                        else ("standalone", "inline")
                    ))
                )
                current["local_method_strategy"] = strategy_cycle[(attempt - 1) % len(strategy_cycle)]
                # Creating the Checkout at zero due removes PIX/UPI from this
                # merchant's payment_method_types, so local methods keep the
                # mid-flight promotion flow.
                current["promo_on_create"] = False
            if current.get("link_type") == "pix" and current.get("pix_tax_id_auto"):
                auto_kind = current.get("pix_auto_kind") or "cpf"
                kind = ("cpf" if attempt % 2 else "cnpj") if auto_kind == "mixed" else auto_kind
                current["pix_tax_id"] = generate_cnpj() if kind == "cnpj" else generate_cpf()
                current["pix_identity"] = generate_pix_identity(kind)
            self.update(
                job_id, status="running", percent=4,
                text=f"第 {attempt}/{max_attempts} 次尝试：正在准备任务",
                error="",
            )
            self.log(job_id, f"========== 提链尝试 {attempt}/{max_attempts} ==========")
            if current.get("link_type") == "paypal" and current.get("use_promo"):
                strategy = "Checkout 创建时原生带优惠" if current.get("promo_on_create") else "创建后通过入口线路更新优惠"
                self.log(job_id, f"PayPal 优惠策略：{strategy}")
            self._run_single(job_id, current)
            state = self.get(job_id) or {}
            if state.get("status") in {"done", "cancelled"}:
                if state.get("status") == "done" and isinstance(state.get("result"), dict):
                    result = state["result"]
                    result["attempt"] = attempt
                    result["max_attempts"] = max_attempts
                    self.update(job_id, result=result)
                    self._record_success(job_id, result)
                return
            last_error = str(state.get("error") or "")
            if last_error:
                self.update(job_id, last_retry_error=last_error[:500])
            lowered = last_error.lower()
            if "custom_checkout_rebuild_required" in lowered or (
                "oaics_" in lowered and current.get("link_type") != "kakao"
            ):
                oaics_hits += 1
                self.log(job_id, f"OAICS Checkout 命中 {oaics_hits}/3；PayPal/本地支付将重建 Checkout")
                if oaics_hits >= 3:
                    threshold_error = (
                        "OAICS_THRESHOLD_REACHED: selected payment channel requires Stripe cs_*; "
                        "use Official Checkout for this account"
                    )
                    self.update(
                        job_id, status="error", percent=100,
                        text="当前账号仅返回 OAICS，请改用官方 Checkout",
                        error=threshold_error,
                        last_retry_error=last_error[:500],
                    )
                    return
            non_retryable = any(marker in lowered for marker in (
                "access token", "token_invalidated", "token_expired", "token_revoked", "jwt expired",
                "计划类型", "提取方式", "任务已停止", "kakao_confirm_already_sent",
            ))
            if non_retryable or attempt >= max_attempts:
                self.update(job_id, status="error", percent=100, text="任务失败", error=last_error[:1200])
                return
            if (
                current.get("link_type") == "paypal"
                and not paypal_force_de_fallback
                and (
                    "\u672a\u5f00\u653e paypal" in lowered
                    or "\u672a\u5f00\u653epaypal" in lowered
                    or "does not expose paypal" in lowered
                    or "paypal is not available" in lowered
                    or "paypal unavailable" in lowered
                )
                and str(current.get("checkout_country") or current.get("country") or "").upper() != "DE"
            ):
                paypal_force_de_fallback = True
                self.log(job_id, "\u5f53\u524d\u56fd\u5bb6 Checkout \u672a\u8fd4\u56de PayPal\uff1b\u540e\u7eed\u5c1d\u8bd5\u81ea\u52a8\u5207\u6362\u5fb7\u56fd DE/EUR \u8d26\u5355")
            self.log(job_id, f"第 {attempt}/{max_attempts} 轮未命中：{last_error[:260] or '上游未返回可用链接'}")
            if options.get("link_type") == "pix":
                self.log(job_id, "正在更换代理与 PIX 资料后重新尝试")
            else:
                self.log(job_id, "正在更换代理后重新尝试")
            time.sleep(min(4, 1 + attempt * 0.35))

    def _run_paypal_oaics(self, job_id: str, options: dict) -> None:
        try:
            self.update(job_id, status="running", percent=5, text="正在启动 PayPal OAICS 提链", error="")
            proxy_country = str(options.get("proxy_country") or "BR").upper()
            billing_country = str(options.get("billing_country") or "DE").upper()
            billing_currency = str(options.get("checkout_currency") or "EUR").upper()
            self.log(
                job_id,
                "PayPal OAICS 来源：eatWhitePorridge/link-pp；"
                f"{proxy_country} 出口，{billing_country}/{billing_currency} 零元账单",
            )
            result = run_paypal_oaics(
                access_token=str(options.get("token_raw") or ""),
                proxies=list(options.get("entry_proxies") or []),
                checkout_attempts=min(50, max(1, int(options.get("retry_count") or 5))),
                provider_attempts=min(50, max(1, int(options.get("provider_attempts") or 10))),
                log=lambda message: self.log(job_id, message),
                is_cancelled=lambda: self.cancelled(job_id),
                proxy_country=proxy_country,
                billing_country=billing_country,
            )
            self.ensure_not_cancelled(job_id)
            approval_url = str(result.get("paypal_approve_url") or "").strip()
            final_url = approval_url or str(result.get("provider_redirect_url") or "").strip()
            result["url"] = final_url
            result["paypal_link"] = final_url
            self.update(job_id, status="done", percent=100, text="PayPal OAICS BA 提链完成", result=result)
            self._record_success(job_id, result)
        except InterruptedError as exc:
            self.update(job_id, status="cancelled", percent=100, text=str(exc), error=str(exc))
        except Exception as exc:
            error_text = str(exc)
            if self.cancelled(job_id):
                self.update(job_id, status="cancelled", percent=100, text="任务已取消", error="任务已取消")
                return
            self.log(job_id, f"错误：{type(exc).__name__}: {error_text}")
            self.update(job_id, status="error", percent=100, text="PayPal OAICS 提链失败", error=error_text[:1200])

    def _run_rust_workflow(self, job_id: str, options: dict, rust_base: str):
        """Prepare one existing outer retry, then execute the payment stages in Rust."""
        try:
            self.update(job_id, status="running", percent=6, text="解析账号与 Rust 任务参数", error="")
            provider = str(options.get("link_type") or "").lower()
            entry_proxy = str(options.get("fixed_entry_proxy") or "").strip()
            payment_proxy = str(options.get("fixed_exit_proxy") or entry_proxy).strip()
            if provider == "pix":
                payment_proxy = entry_proxy
            if not entry_proxy or not payment_proxy:
                raise RuntimeError("Rust 工作流缺少本轮固定代理")

            country = str(options.get("checkout_country") or options.get("country") or "US").upper()
            payment_geo: dict[str, str] = {}
            if provider == "paypal":
                # reg153 prepares country-specific paired pools.  Re-probing as
                # many as 24 entries for every batch task creates hundreds of
                # simultaneous helper processes and used to surface as HTTP 408.
                # Trust the requested checkout country for paired pools; public
                # free-form pools still keep a bounded probe with a soft fallback.
                if options.get("paired_proxy_rotation"):
                    payment_geo = {
                        "country": country,
                        "currency": str(COUNTRY_CURRENCY.get(country) or options.get("checkout_currency") or options.get("currency") or ""),
                        "region": "",
                        "city": "",
                        "postal": "",
                        "timezone": "",
                        "source": "paired_pool",
                    }
                    self.log(job_id, f"已使用配对代理池地区 {country}，跳过重复代理探测")
                else:
                    exit_pool = list(options.get("exit_proxies") or [payment_proxy])
                    proxy_response = requests.post(
                        f"{rust_base}/api/v1/proxies/select",
                        json={
                            "proxies": exit_pool,
                            "preferred": payment_proxy,
                            "scan_limit": min(8, max(1, int(os.getenv("PAYPAL_PROXY_SCAN_LIMIT", "6") or 6))),
                            "transport": str(os.getenv("PAY153_RUST_TRANSPORT") or "curl_cffi"),
                        },
                        timeout=45,
                    )
                    if proxy_response.status_code == 200:
                        proxy_selection = proxy_response.json() or {}
                        payment_proxy = str(proxy_selection.get("selected") or payment_proxy).strip()
                        payment_geo = dict(proxy_selection.get("geo") or {})
                    elif proxy_response.status_code in {408, 429, 500, 502, 503, 504}:
                        payment_geo = {
                            "country": country,
                            "currency": str(options.get("checkout_currency") or options.get("currency") or ""),
                            "region": "",
                            "city": "",
                            "postal": "",
                            "timezone": "",
                            "source": "probe_timeout_fallback",
                        }
                        self.log(job_id, f"代理探测 HTTP {proxy_response.status_code}，沿用本轮固定代理继续")
                    else:
                        raise RuntimeError(
                            f"Rust 代理选择失败 HTTP {proxy_response.status_code}: "
                            f"{(proxy_response.text or '')[:500]}"
                        )
                    if not payment_proxy or not payment_geo.get("country"):
                        payment_geo = {
                            "country": country,
                            "currency": str(options.get("checkout_currency") or options.get("currency") or ""),
                            "region": "",
                            "city": "",
                            "postal": "",
                            "timezone": "",
                            "source": "empty_geo_fallback",
                        }
                payment_country = str(payment_geo.get("country") or country).upper()
                detected_currency = str(payment_geo.get("currency") or "").upper()
                if options.get("force_paypal_de_fallback"):
                    country, currency = "DE", "EUR"
                else:
                    country, currency, _source = normalize_paypal_checkout_region(
                        payment_country, detected_currency,
                    )
                options["checkout_country"] = country
                options["checkout_currency"] = currency
                options["country"] = country
                options["currency"] = currency
            elif provider == "ideal":
                country, options["currency"] = "NL", "EUR"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "EUR"
            elif provider == "twint":
                country, options["currency"] = "CH", "CHF"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "CHF"
            elif provider == "upi":
                country, options["currency"] = "IN", "INR"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "INR"
            elif provider == "pix":
                country, options["currency"] = "BR", "BRL"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "BRL"
            elif provider == "momo":
                country, options["currency"] = "VN", "VND"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "VND"
            elif provider == "gcash":
                country, options["currency"] = "PH", "PHP"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "PHP"
            elif provider == "kakao":
                country, options["currency"] = "KR", "KRW"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "KRW"

            prepare_response = requests.post(
                f"{rust_base}/api/v1/legacy/prepare",
                json={
                    "token_raw": str(options.get("token_raw") or ""),
                    "options": options,
                },
                timeout=20,
            )
            if prepare_response.status_code != 200:
                raise RuntimeError(
                    f"Rust 参数准备失败 HTTP {prepare_response.status_code}: "
                    f"{(prepare_response.text or '')[:500]}"
                )
            prepared = dict((prepare_response.json() or {}).get("prepared") or {})
            token = str(prepared.get("access_token") or "")
            meta = dict(prepared.get("meta") or {})
            prepared_payload = prepared.get("payload") or {}
            country = str(prepared.get("country") or country).upper()
            options["currency"] = options["checkout_currency"] = str(
                prepared.get("currency") or options.get("currency") or ""
            ).upper()
            if not token or not meta.get("account_id") or not prepared_payload:
                raise RuntimeError("Rust 参数准备结果不完整")

            device_id, did = str(uuid.uuid4()), str(uuid.uuid4())
            self.update(job_id, status="running", percent=12, text="准备 Rust Checkout 任务")
            billing_geo = payment_geo if str(payment_geo.get("country") or "").upper() == country else None
            billing_response = requests.post(
                f"{rust_base}/api/v1/billing/generate",
                json={
                    "country": country,
                    "email": str(meta.get("email") or ""),
                    "tax_id": str(options.get("pix_tax_id") or ""),
                    "geo": billing_geo,
                    "rotate_public_address": provider == "paypal",
                },
                timeout=20,
            )
            if billing_response.status_code != 200:
                raise RuntimeError(
                    f"Rust 账单生成失败 HTTP {billing_response.status_code}: "
                    f"{(billing_response.text or '')[:500]}"
                )
            billing = dict(((billing_response.json() or {}).get("profile") or {}).get("billing") or {})
            if not billing.get("address"):
                raise RuntimeError("Rust 账单生成未返回地址")
            if provider == "paypal" and country in ROTATING_PAYPAL_ADDRESS_COUNTRIES:
                normalized_address = None
                for _ in range(8):
                    cached_address = resolve_cached_country_address(country)
                    normalized_address = normalize_rotating_paypal_address(country, cached_address or {})
                    if normalized_address:
                        break
                if normalized_address:
                    address = billing.setdefault("address", {})
                    address.update(normalized_address)
            if provider == "pix":
                identity = dict(options.get("pix_identity") or {})
                if identity:
                    billing["name"] = identity.get("name") or billing.get("name")
                    billing["email"] = identity.get("email") or billing.get("email")
                    address = billing.setdefault("address", {})
                    for key in ("line1", "city", "state", "postal_code"):
                        if identity.get(key):
                            address[key] = identity[key]
            address = dict(billing.get("address") or {})
            address.setdefault("line2", "")
            rust_billing = {
                "name": str(billing.get("name") or ""),
                "email": str(billing.get("email") or ""),
                "tax_id": str(billing.get("tax_id") or ""),
                "address": {
                    "country": str(address.get("country") or country),
                    "line1": str(address.get("line1") or ""),
                    "line2": str(address.get("line2") or ""),
                    "city": str(address.get("city") or ""),
                    "postal_code": str(address.get("postal_code") or ""),
                    "state": str(address.get("state") or ""),
                },
            }
            profile = sc._profile(country)
            self.log(
                job_id,
                "令牌字段：SEN={}，SO={}".format(
                    "ON" if options.get("use_sen", True) else "OFF",
                    "ON" if options.get("use_so", True) else "OFF",
                ),
            )
            common = {
                "access_token": token,
                "account_id": str(meta.get("account_id") or ""),
                "payload": prepared_payload,
                "billing": rust_billing,
                "browser_locale": str(profile.get("browser_locale") or "en-US"),
                "browser_timezone": str(profile.get("browser_timezone") or "America/Chicago"),
                "use_sen": bool(options.get("use_sen", True)),
                "use_so": bool(options.get("use_so", True)),
                "attempts": [{
                    "chatgpt_proxy": payment_proxy,
                    "stripe_proxy": payment_proxy,
                    "promotion_proxy": entry_proxy,
                    "device_id": device_id,
                    "oai_did": did,
                    "checkout_sentinel_token": None,
                    "checkout_sentinel_so_token": None,
                    "approval_sentinel_token": None,
                    "approval_sentinel_so_token": None,
                }],
                "transport": str(os.getenv("PAY153_RUST_TRANSPORT") or "curl_cffi"),
            }
            if options.get("use_promo") and options.get("plan") == "plus":
                common["promo"] = {
                    "campaign_id": str(options.get("promo_campaign") or "plus-1-month-free"),
                    "plan_name": PLANS["plus"],
                    "price_interval": "month",
                    "seat_quantity": 1,
                    "require_zero_due": True,
                    "always_update": provider == "kakao",
                }
            if provider == "paypal":
                try:
                    common["fingerprint"] = json.loads(
                        Path(__file__).with_name("paypal_fingerprint.json").read_text(encoding="utf-8")
                    )
                    if "_stripe_version" in common["fingerprint"]:
                        common["fingerprint"]["stripe_version"] = common["fingerprint"].pop("_stripe_version")
                except Exception:
                    common["fingerprint"] = {}
                endpoint = "/api/v1/jobs/paypal-workflow"
            elif provider == "hosted":
                endpoint = "/api/v1/jobs/hosted-workflow"
            else:
                common["provider"] = provider
                endpoint = "/api/v1/jobs/local-workflow"

            response = requests.post(
                f"{rust_base}{endpoint}", json=common, timeout=90,
            )
            if response.status_code != 202:
                raise RuntimeError(
                    f"Rust 工作流创建失败 HTTP {response.status_code}: {(response.text or '')[:500]}"
                )
            rust_job_id = str((response.json() or {}).get("job", {}).get("id") or "")
            if not rust_job_id:
                raise RuntimeError("Rust 工作流未返回任务 ID")
            remember_rust_job_alias(job_id, rust_job_id, {
                "plan": options.get("plan"),
                "link_type": provider,
                "country": country,
                "currency": options.get("currency"),
                "use_promo": bool(options.get("use_promo")),
                "promo_campaign": str(options.get("promo_campaign") or ""),
            })
            step_labels = {
                "creating_checkout": "创建 OpenAI Checkout",
                "stripe_bootstrap": "初始化 Stripe 支付方式",
                "applying_promotion": "应用优惠并同步金额",
                "syncing_billing": "同步账单地址",
                "creating_paypal_payment_method": "创建 PayPal PaymentMethod",
                "creating_local_payment_method": f"创建 {provider.upper()} PaymentMethod",
                "preconfirming_kakao": "准备 Kakao Pay 支付会话",
                "creating_kakao_payment_method": "创建 Kakao Pay PaymentMethod",
                "confirming_kakao": "提交 Kakao Pay confirm",
                "polling_kakao_redirect": "读取 Kakao / Nicepay 跳转",
                "confirming_paypal": "提交 PayPal confirm",
                "confirming_local_payment": f"提交 {provider.upper()} confirm",
                "approving_checkout": "提交 Checkout approval",
                "polling_paypal_redirect": "读取 PayPal 跳转",
                "polling_local_result": f"读取 {provider.upper()} 支付结果",
                "retrying_with_fresh_checkout": "更换参数并重建 Checkout",
            }
            while True:
                if self.cancelled(job_id):
                    try:
                        requests.post(f"{rust_base}/api/v1/jobs/{rust_job_id}/cancel", timeout=8)
                    except Exception:
                        pass
                    self.update(job_id, status="cancelled", percent=100, text="任务已停止", error="任务已停止")
                    return
                progress_response = requests.get(
                    f"{rust_base}/api/v1/jobs/{rust_job_id}", timeout=15,
                )
                if progress_response.status_code != 200:
                    raise RuntimeError(f"Rust 任务状态 HTTP {progress_response.status_code}")
                rust_job = (progress_response.json() or {}).get("job") or {}
                rust_status = str(rust_job.get("status") or "")
                rust_step = str(rust_job.get("step") or "")
                if rust_status not in {"succeeded", "failed", "cancelled"}:
                    self.update(
                        job_id,
                        status="running",
                        percent=int(rust_job.get("progress") or 0),
                        text=step_labels.get(rust_step, rust_step or "Rust 工作流运行中"),
                        error=str(rust_job.get("error") or "")[:1200],
                    )
                if rust_status == "succeeded":
                    result = dict(rust_job.get("result") or {})
                    result.update({
                        "plan": options.get("plan"),
                        "link_type": provider,
                        "account_email": str(meta.get("email") or ""),
                        "account_id": str(meta.get("account_id") or ""),
                        "country": country,
                        "currency": str(result.get("currency") or options.get("currency") or "").upper(),
                        "entry_country": str(proxy_country(entry_proxy)[0] or "").upper(),
                        "payment_proxy_country": str(proxy_country(payment_proxy)[0] or "").upper(),
                        "rust_workflow": True,
                        "sen_requested": bool(options.get("use_sen", True)),
                        "so_requested": bool(options.get("use_so", True)),
                    })
                    if provider == "paypal":
                        result["paypal_link"] = result.get("paypal_url") or ""
                        result["provider_redirect_url"] = result.get("paypal_url") or result.get("stripe_redirect_url") or ""
                    self.update(job_id, status="done", percent=100, text="提取完成", error="", result=result)
                    return
                if rust_status == "failed":
                    rust_error = str(rust_job.get("error") or "Rust 工作流失败")[:1200]
                    if options.get("retry_wrapper"):
                        self.update(
                            job_id,
                            status="running",
                            percent=8,
                            text="本轮未成功，正在更换代理重试",
                            error=rust_error,
                        )
                    else:
                        self.update(job_id, status="error", percent=100, text="任务失败", error=rust_error)
                    return
                if rust_status == "cancelled":
                    self.update(job_id, status="cancelled", percent=100, text="任务已停止", error="任务已停止")
                    return
                time.sleep(0.5)
        except InterruptedError as exc:
            self.update(job_id, status="cancelled", percent=100, text="任务已停止", error=str(exc))
        except Exception as exc:
            self.log(job_id, f"Rust 工作流异常：{type(exc).__name__}: {exc}")
            if options.get("retry_wrapper"):
                self.update(
                    job_id,
                    status="running",
                    percent=8,
                    text="本轮未成功，正在更换代理重试",
                    error=str(exc)[:1200],
                )
            else:
                self.update(job_id, status="error", percent=100, text="任务失败", error=str(exc)[:1200])

    def _run_single(self, job_id: str, options: dict):
        rust_base = str(os.getenv("PAY153_RUST_URL") or "").strip().rstrip("/")
        rust_execute = str(os.getenv("PAY153_RUST_WORKFLOWS") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if rust_execute and rust_base and options.get("link_type") in {"paypal", "pix", "upi", "ideal", "kakao"}:
            return self._run_rust_workflow(job_id, options, rust_base)
        try:
            self.update(job_id, status="running", percent=6, text="解析 Access Token")
            raw_token = options.pop("token_raw")
            token, meta = extract_access_token(raw_token)
            self.ensure_not_cancelled(job_id)
            provider = options["link_type"]
            country = options["country"]
            entry_pool = options["entry_proxies"]
            exit_pool = entry_pool if provider in {"pix", "momo"} else (options.get("exit_proxies") or entry_pool)
            entry_proxy = options.get("fixed_entry_proxy") or secrets.choice(entry_pool)
            exit_proxy = entry_proxy if provider in {"pix", "momo"} else (options.get("fixed_exit_proxy") or secrets.choice(exit_pool))
            payment_geo: dict[str, str] = {}
            if provider == "hosted":
                self.log(job_id, f"代理池共 {len(entry_pool)} 条，本次已自动选择 1 条")
            elif provider in {"pix", "momo"}:
                self.log(job_id, f"代理池 1 共 {len(entry_pool)} 条，本次已自动选择 1 条")
            else:
                self.log(job_id, f"代理池 1 共 {len(entry_pool)} 条，代理池 2 共 {len(exit_pool)} 条，本次已分别自动选择")
            # Every outer retry creates a brand-new Checkout, so it must also
            # use a fresh browser/device identity.  Within this single attempt
            # the same ids are kept for create -> update -> approve.
            device_id, did = str(uuid.uuid4()), str(uuid.uuid4())

            if provider == "ph_short":
                short_country = country if country in {"PH", "GB", "US"} else "PH"
                short_currency = {"PH": "PHP", "GB": "GBP", "US": "USD"}[short_country]
                checkout_proxy_country = str(options.get("entry_proxy_country") or short_country).upper()
                update_proxy_country = str(options.get("exit_proxy_country") or (options.get("promo_country") if options.get("use_promo") else short_country) or short_country).upper()
                self.update(job_id, percent=9, text=f"Validate {checkout_proxy_country} Checkout and {update_proxy_country} promotion proxy")
                credentials = parse_ph_short_credentials(raw_token)
                extractor = PhShortCheckoutExtractor(
                    credentials,
                    PhShortExtractorConfig(
                        billing_country=short_country,
                        currency=short_currency,
                        payment_locale="en",
                        checkout_proxy_country=checkout_proxy_country,
                        update_proxy_country=update_proxy_country,
                        checkout_proxy=entry_proxy,
                        update_proxy=exit_proxy,
                        plan_name="chatgptplusplan",
                        promo_campaign_id=options.get("promo_campaign") or "plus-1-month-free",
                        apply_promo=bool(options.get("use_promo")),
                        checkout_attempts=10,
                        update_attempts=15,
                        full_attempts=1,
                        cf_same_identity_attempts=5,
                        verify_proxy_country=True,
                        allow_missing_customer_session=bool(options.get("allow_missing_customer_session")),
                    ),
                    logger=lambda message: self.log(job_id, message),
                )
                self.update(job_id, percent=24, text=f"Create {short_country}/{short_currency} Checkout")
                extracted = extractor.extract()
                verification = str(extracted.amount_verification or "pending")
                promo_requested = bool(options.get("use_promo"))
                promo_applied = (verification == "verified_zero") if promo_requested and verification != "pending" else None
                result = {
                    "plan": options["plan"],
                    "link_type": "ph_short",
                    "checkout_session_id": extracted.cs_id,
                    "checkout_url": extracted.long_url,
                    "short_link": extracted.long_url,
                    "processor_entity": extracted.processor_entity,
                    "account_email": meta.get("email") or "",
                    "account_id": meta.get("account_id") or "",
                    "country": short_country,
                    "currency": short_currency,
                    "checkout_country": short_country,
                    "checkout_currency": short_currency,
                    "entry_country": checkout_proxy_country,
                    "payment_proxy_country": update_proxy_country,
                    "proxy_mode": f"{checkout_proxy_country.lower()}_checkout_{update_proxy_country.lower()}_update",
                    "entry_proxy_pool_size": len(entry_pool),
                    "exit_proxy_pool_size": len(exit_pool),
                    "promo_requested": promo_requested,
                    "promo_applied": promo_applied,
                    "promo_campaign_used": (options.get("promo_campaign") or "plus-1-month-free") if promo_requested else "",
                    "amount_verification": verification,
                    "checkout_amount": extracted.amount_minor,
                    "amount_currency": extracted.amount_currency,
                    "checkout_device_id": extracted.device_id,
                    "checkout_chatgpt_session_id": extracted.chatgpt_session_id,
                    "checkout_user_agent": extracted.user_agent,
                    "stripe_publishable_key": extracted.publishable_key,
                    "extractor": "simon_short_link",
                }
                done_text = "菲律宾短链生成完成"
                if verification == "pending":
                    done_text += "（金额待页面复核）"
                self.update(job_id, percent=100, text=done_text, status="done", result=result)
                return

            if provider == "pix":
                self.update(job_id, percent=9, text="第 1/7 步：选择并检测代理")
                main_country, main_region = proxy_country(entry_proxy, options.get("entry_proxy_country"))
                stripe_country, stripe_region = proxy_country(exit_proxy, options.get("exit_proxy_country"))
                self.log(job_id, f"PIX 代理校验：代理池 1={main_country}/{main_region}")
                if main_country != "BR" or stripe_country != "BR":
                    self.log(
                        job_id,
                        f"PIX 当前代理为 {main_country or '?'} + {stripe_country or '?'}；不限制国家，继续由上游判断支付方式",
                    )
                self.ensure_not_cancelled(job_id)
            elif provider == "momo":
                self.update(job_id, percent=9, text="第 1/7 步：选择并检测越南代理")
                main_country, main_region = proxy_country(entry_proxy, options.get("entry_proxy_country"))
                self.log(job_id, f"MoMo 代理校验：代理池 1={main_country}/{main_region}")
                if main_country != "VN":
                    self.log(job_id, f"MoMo 当前代理为 {main_country or '?'}；继续由上游判断支付方式")
                country = options["country"] = options["checkout_country"] = "VN"
                options["currency"] = options["checkout_currency"] = "VND"
                self.ensure_not_cancelled(job_id)

            promo_requested = options["plan"] == "plus" and options.get("use_promo", False)
            if provider == "gcash":
                main_country, main_region = proxy_country(entry_proxy, options.get("entry_proxy_country"))
                promo_proxy_country, promo_proxy_region = proxy_country(exit_proxy, options.get("exit_proxy_country"))
                self.update(job_id, percent=9, text="校验 US Checkout 与优惠代理")
                self.log(job_id, f"GCash 路由：Checkout={main_country}/{main_region}，账单=PH/PHP，优惠更新={promo_proxy_country}/{promo_proxy_region}")
                if main_country != "US":
                    self.log(job_id, f"GCash Checkout 代理当前为 {main_country or '?'}；目标为 US，继续由上游校验")
                self.ensure_not_cancelled(job_id)
            if provider == "paypal":
                self.update(job_id, percent=9, text="第 1/7 步：校验 PayPal 优惠识别代理与支付代理")
                main_country, main_region = proxy_country(entry_proxy)
                exit_proxy, payment_geo, rejected_countries = select_paypal_exit_proxy(
                    exit_proxy,
                    exit_pool,
                    scan_limit=int(os.getenv("PAYPAL_PROXY_SCAN_LIMIT", "24") or 24),
                )
                payment_country = payment_geo.get("country") or ""
                payment_region = payment_geo.get("region") or ""
                if not payment_country:
                    raise RuntimeError("代理池 2 未检测到国家地区")
                if rejected_countries:
                    self.log(job_id, f"PayPal 已跳过不兼容地区：{'/'.join(rejected_countries[:8])}")
                detected_currency = str(payment_geo.get("currency") or "").upper()
                if options.get("force_paypal_de_fallback"):
                    checkout_country, checkout_currency, currency_source = (
                        "DE", "EUR", f"\u5f53\u524d\u56fd\u5bb6 {payment_country} \u5b9e\u6d4b\u672a\u5f00\u653e PayPal\uff0c\u4f7f\u7528 DE/EUR \u56de\u9000",
                    )
                else:
                    checkout_country, checkout_currency, currency_source = normalize_paypal_checkout_region(
                        payment_country, detected_currency,
                    )
                country = checkout_country
                options["country"] = checkout_country
                options["currency"] = checkout_currency
                options["checkout_country"] = checkout_country
                options["checkout_currency"] = checkout_currency
                options["payment_proxy_country"] = payment_country
                self.log(
                    job_id,
                    f"PayPal 代理池 2 地区：{payment_country}/{payment_region}；"
                    f"Checkout={checkout_country}/{checkout_currency}（{currency_source}）",
                )
                if promo_requested and main_country not in {"TR", "JP"}:
                    self.log(job_id, f"PayPal 优惠识别代理当前为 {main_country or '?'}；不限制国家，继续尝试")
                self.ensure_not_cancelled(job_id)
            if provider == "upi":
                self.update(job_id, percent=9, text="第 1/7 步：校验 UPI 优惠识别代理与印度支付代理")
                main_country, main_region = proxy_country(entry_proxy, options.get("entry_proxy_country"))
                payment_country, payment_region = proxy_country(exit_proxy, options.get("exit_proxy_country"))
                self.log(job_id, f"UPI 代理校验：优惠识别={main_country}/{main_region}，UPI 支付={payment_country}/{payment_region}，账单=IN/INR")
                if promo_requested and main_country not in {"TR", "JP"}:
                    self.log(job_id, f"UPI 优惠识别代理当前为 {main_country or '?'}；不限制国家，继续尝试")
                if payment_country != "IN":
                    self.log(job_id, f"UPI 支付代理当前为 {payment_country or '?'}；不限制国家，继续由上游判断支付方式")
                self.ensure_not_cancelled(job_id)
            if provider == "ideal":
                self.update(job_id, percent=9, text="校验 iDEAL 荷兰支付代理")
                main_country, main_region = proxy_country(entry_proxy, options.get("entry_proxy_country"))
                payment_country, payment_region = proxy_country(exit_proxy, options.get("exit_proxy_country"))
                self.log(
                    job_id,
                    f"iDEAL 代理校验：入口={main_country}/{main_region}，"
                    f"支付={payment_country}/{payment_region}，账单=NL/EUR",
                )
                if payment_country != "NL":
                    raise RuntimeError(
                        f"iDEAL 支付代理出口为 {payment_country or '未知'}，需要 NL 荷兰出口"
                    )
                self.ensure_not_cancelled(job_id)
            if provider == "twint":
                self.update(job_id, percent=9, text="校验 TWINT 瑞士支付代理")
                payment_country, payment_region = proxy_country(exit_proxy, options.get("exit_proxy_country"))
                self.log(job_id, f"TWINT 代理校验：支付={payment_country}/{payment_region}，账单=CH/CHF")
                if payment_country != "CH":
                    raise RuntimeError(f"TWINT 支付代理出口为 {payment_country or '未知'}，需要 CH 瑞士出口")
                country = options["country"] = options["checkout_country"] = "CH"
                options["currency"] = options["checkout_currency"] = "CHF"
                self.ensure_not_cancelled(job_id)
            preflight = {}
            if promo_requested:
                self.update(job_id, percent=12, text="读取入口支付与活动标记")
                preflight = preflight_trial_eligibility(
                    token, meta.get("account_id") or "", (exit_proxy if provider == "gcash" else entry_proxy), device_id, did,
                    lambda m: self.log(job_id, m),
                )
                detected_campaign = promo_campaign_from_payload(preflight)
                if preflight.get("one_click_trial_eligible") is True:
                    options["promo_marker_eligible"] = True
                if detected_campaign:
                    options["promo_campaign"] = detected_campaign
                    options["promo_campaign_verified"] = True
                    self.log(job_id, f"优惠预检已匹配账号活动：{detected_campaign}")
                self.ensure_not_cancelled(job_id)

            if (
                provider == "upi"
                and promo_requested
                and options.get("local_method_strategy") == "go_b"
            ):
                if not upi_go_available():
                    raise RuntimeError("UPI Go Elements/B 引擎未安装")
                self.update(job_id, percent=22, text="UPI Go：准备印度账单与代理路由")
                upi_billing = default_billing("IN", meta.get("email") or "")
                upi_address = upi_billing.get("address") or {}
                self.log(
                    job_id,
                    "UPI Go 账单：城市={}，州={}，邮编={}".format(
                        upi_address.get("city") or "-",
                        upi_address.get("state") or "-",
                        upi_address.get("postal_code") or "-",
                    ),
                )
                self.update(job_id, percent=34, text="UPI Go：创建零元 Checkout")
                go_result = run_upi_go(
                    token=token,
                    proxy=exit_proxy,
                    billing=upi_billing,
                    promotion_country=str(os.getenv("PAY153_UPI_GO_PROMO_COUNTRY") or "VN"),
                    timeout_seconds=int(os.getenv("PAY153_UPI_GO_REQUEST_TIMEOUT", "45") or 45),
                    cancelled=lambda: self.cancelled(job_id),
                    log=lambda message: self.log(job_id, message),
                )
                self.ensure_not_cancelled(job_id)
                result: dict[str, Any] = {
                    "plan": options["plan"],
                    "link_type": "upi",
                    "account_email": meta.get("email") or "",
                    "account_id": meta.get("account_id") or "",
                    "country": "IN",
                    "currency": str(go_result.get("checkout_currency") or "INR").upper(),
                    "checkout_country": "IN",
                    "checkout_currency": str(go_result.get("checkout_currency") or "INR").upper(),
                    "entry_proxy_pool_size": len(entry_pool),
                    "exit_proxy_pool_size": len(exit_pool),
                    "proxy_mode": "go_region_route",
                    "promo_requested": True,
                    "promo_applied": go_result.get("promo_applied"),
                    "promo_campaign_used": options.get("promo_campaign") or "plus-1-month-free",
                    "entry_trial_eligible": preflight.get("one_click_trial_eligible"),
                    "entry_country": str(main_country or "").upper(),
                    "payment_proxy_country": str(payment_country or "").upper(),
                }
                result.update(go_result)
                self.update(job_id, percent=100, text="UPI 提取完成", status="done", result=result)
                return

            self.update(job_id, percent=18, text="生成 Sentinel 校验")
            payload = checkout_payload(options, meta)
            if provider == "paypal":
                self.log(job_id, f"计划={options['plan']}，方式=paypal，账单={country}/{options['currency']}，PayPal订单={options.get('checkout_country')}/{options.get('checkout_currency')}")
            else:
                self.log(job_id, f"计划={options['plan']}，方式={provider}，地区={country}/{options['currency']}")
            stage2_text = "第 2/7 步：BR 创建 Checkout（首段不带优惠）" if provider == "pix" else (
                (f"第 2/7 步：使用 {country} 代理创建 PayPal Checkout"
                 + ("（原生携带优惠）" if options.get("promo_on_create") else "（稍后更新优惠）"))
                if provider == "paypal" and promo_requested else (
                    "第 2/7 步：使用 IN 代理创建 UPI Checkout" if provider == "upi" else (
                        "第 2/7 步：使用 VN 代理创建 MoMo Checkout" if provider == "momo" else "创建 OpenAI Checkout"
                    )
                )
            )
            self.update(job_id, percent=34, text=stage2_text)
            checkout_proxy = exit_proxy if provider in {"paypal", "upi", "ideal", "twint", "kakao"} else entry_proxy
            if provider in {"pix", "momo"}:
                self.log(
                    job_id,
                    f"Stage1 Checkout、优惠更新、Stripe 和 approval 使用同一条 {'BR' if provider == 'pix' else 'VN'} 代理"
                    + ("；本轮优惠随 Checkout 创建" if options.get("promo_on_create") else ""),
                )
            elif provider == "gcash":
                self.log(job_id, f"GCash 设置：代理池 1 使用 US 创建 PH/PHP Checkout，代理池 2 使用用户选择的 {options.get('promo_country') or '优惠国家'} 更新优惠")
            elif provider == "paypal" and promo_requested:
                self.log(job_id, f"PayPal 设置：代理池 1 用于优惠检查，代理池 2 创建 {country}/{options['currency']} Checkout")
            elif provider == "upi":
                self.log(job_id, "UPI 设置：代理池 1 用于优惠检查，代理池 2 创建 IN/INR Checkout")
            elif provider == "ideal":
                self.log(job_id, "iDEAL 设置：代理池 2 创建 NL/EUR Checkout，并贯穿 Stripe 支付处理")
            elif provider == "twint":
                self.log(job_id, "TWINT 设置：代理池 2 使用 CH 创建 CHF Checkout；可在支付方式确认后应用首月优惠")
            elif provider == "kakao":
                self.log(job_id, "Kakao 设置：代理池 1 用于优惠检查/更新，代理池 2 用于 KR Checkout、Stripe 与 OAICS confirm")
            elif provider != "hosted":
                self.log(job_id, f"Checkout 将使用所选的 {country} 地区代理")
            created = create_checkout(
                token,
                payload,
                checkout_proxy,
                device_id,
                did,
                lambda m: self.log(job_id, m),
                use_sen=(True if provider == "gcash" else bool(options.get("use_sen", True))),
                use_so=(True if provider == "gcash" else bool(options.get("use_so", True))),
            )
            self.ensure_not_cancelled(job_id)
            self.update(job_id, percent=44, text="Checkout 创建完成，正在准备支付方式")
            checkout_data = created["data"]
            chatgpt_http = created["http"]
            stage1_campaign = promo_campaign_from_payload(checkout_data)
            if checkout_data.get("one_click_trial_eligible") is True:
                options["promo_marker_eligible"] = True
            if stage1_campaign:
                options["promo_campaign"] = stage1_campaign
                options["promo_campaign_verified"] = True
                self.log(job_id, f"Checkout 已返回活动标识：{stage1_campaign}")
            provider_chatgpt_http = chatgpt_http
            promo_chatgpt_http = chatgpt_http
            if provider in {"paypal", "upi", "ideal", "twint", "gcash", "kakao"}:
                promo_chatgpt_http = sc.build_http(exit_proxy if provider == "gcash" else entry_proxy)
                try:
                    promo_chatgpt_http.cookies.set("oai-did", did, domain="chatgpt.com")
                    for cookie_name, cookie_value in chatgpt_http.cookies.get_dict().items():
                        promo_chatgpt_http.cookies.set(cookie_name, cookie_value, domain="chatgpt.com")
                    promo_chatgpt_http.get(
                        "https://chatgpt.com/api/auth/csrf",
                        headers={"User-Agent": sc.CHROME_UA, "Accept": "application/json,text/plain,*/*"},
                        timeout=20,
                    )
                except Exception as exc:
                    self.log(job_id, f"{provider.upper()} 优惠线路暖身提示：{type(exc).__name__}")
                if provider == "gcash":
                    self.log(job_id, f"GCash 优惠更新使用代理池 2（{options.get('promo_country') or '用户选择地区'}），Checkout 与确认使用代理池 1（US）")
                elif provider == "paypal":
                    self.log(job_id, f"PayPal 支付处理使用代理池 2（{country}）")
                elif provider == "upi":
                    self.log(job_id, "UPI 支付处理使用代理池 2（IN）")
                elif provider == "ideal":
                    self.log(job_id, "iDEAL 优惠更新使用代理池 1，NL/EUR Checkout 与 Stripe 使用代理池 2")
                elif provider == "kakao":
                    self.log(job_id, "Kakao 优惠更新使用代理池 1，KR/OAICS 支付处理使用代理池 2")
                else:
                    self.log(job_id, "TWINT 支付处理使用代理池 2（CH/CHF）")
            session_id = checkout_data.get("checkout_session_id") or ""
            if not session_id and provider != "hosted":
                raise RuntimeError("Checkout 未返回 Stripe Session ID")
            if self.cancelled(job_id):
                raise InterruptedError("任务已停止")

            result: dict[str, Any] = {
                "plan": options["plan"],
                "requested_link_type": str(options.get("requested_link_type") or provider),
                "link_type": provider,
                "checkout_session_id": session_id,
                "checkout_url": checkout_data.get("checkout_url") or "",
                "account_email": meta.get("email") or "",
                "account_id": meta.get("account_id") or "",
                "country": country,
                "currency": options["currency"],
                "checkout_country": options.get("checkout_country") or country,
                "checkout_currency": options.get("checkout_currency") or options["currency"],
                "entry_proxy_pool_size": len(entry_pool),
                "exit_proxy_pool_size": len(exit_pool) if provider not in {"hosted", "pix", "momo"} else 0,
                "proxy_mode": ("us_checkout_promo_update" if provider == "gcash" else ("single_chain" if provider in {"pix", "momo"} else ("entry_only" if provider == "hosted" else "dual_chain"))),
                "promo_requested": promo_requested,
                "promo_applied": None,
                "promo_campaign_used": options.get("promo_campaign") or "plus-1-month-free",
                "entry_trial_eligible": preflight.get("one_click_trial_eligible"),
                "checkout_trial_eligible": checkout_data.get("one_click_trial_eligible"),
                "entry_one_click_marker": preflight.get("one_click_trial_eligible"),
                "checkout_one_click_marker": checkout_data.get("one_click_trial_eligible"),
                "promotion_eligibility_decided_by": "checkout_approve",
                "entry_country": str(locals().get("main_country") or "").upper(),
                "promo_country": str(options.get("promo_country") or "").upper(),
                "payment_proxy_country": str(options.get("payment_proxy_country") or locals().get("payment_country") or "").upper(),
            }
            if promo_requested:
                checkout_trial = checkout_data.get("one_click_trial_eligible")
                self.log(
                    job_id,
                    "支付标记（仅供诊断）：入口 one_click={}，Stage1 one_click={}".format(
                        preflight.get("one_click_trial_eligible"), checkout_trial
                    ),
                )
                if checkout_trial is False:
                    self.log(
                        job_id,
                        "Stage1 one_click 标记为 false；该字段不代表活动资格，继续以金额与 approval 结果判定",
                    )
            if str(session_id).startswith("oaics_"):
                custom_processor = (
                    str(checkout_data.get("processor_entity") or "").strip()
                    or ("openai_llc" if country == "US" else "openai_ie")
                )
                if provider == "kakao":
                    self.update(job_id, percent=56, text="正在准备 Kakao OAICS Checkout")
                    custom_state = fetch_custom_checkout_session(
                        chatgpt_http, token, session_id, custom_processor, device_id,
                    )
                    custom_amount = custom_checkout_amount_minor(custom_state)
                    if promo_requested and custom_amount not in {None, 0}:
                        self.update(job_id, percent=63, text="正在通过优惠线路更新 Kakao Checkout")
                        update_checkout_promo(
                            promo_chatgpt_http, token, session_id, custom_processor,
                            options.get("promo_campaign") or "plus-1-month-free",
                            lambda m: self.log(job_id, m), device_id=device_id,
                        )
                        custom_state = fetch_custom_checkout_session(
                            chatgpt_http, token, session_id, custom_processor, device_id,
                        )
                        custom_amount = custom_checkout_amount_minor(custom_state)
                    if custom_amount is None:
                        raise RuntimeError("KAKAO_OAICS_AMOUNT_UNKNOWN: 无法确认当前 OAICS Checkout 的应付金额")
                    if custom_amount != 0:
                        raise RuntimeError(
                            f"KAKAO_OAICS_NONZERO: 优惠未生效，amount={custom_amount} "
                            f"{custom_checkout_currency(custom_state) or 'KRW'}"
                        )
                    kakao_billing = default_billing("KR", meta.get("email") or "")
                    self.update(job_id, percent=70, text="正在创建真实 Kakao Elements Session")
                    approval_headers = asyncio.run(sentinel_headers(
                        exit_proxy, "checkout_session_approval", device_id, did,
                        use_sen=bool(options.get("use_sen", True)),
                        use_so=bool(options.get("use_so", True)),
                    ))
                    stripe_http = sc.build_http(exit_proxy)
                    try:
                        oaics_result = run_kakao_oaics(
                            chatgpt_http=chatgpt_http,
                            stripe_http=stripe_http,
                            token=token,
                            session_id=session_id,
                            processor=custom_processor,
                            device_id=device_id,
                            initial_checkout={**checkout_data, **custom_state},
                            billing=kakao_billing,
                            sentinel_headers=approval_headers,
                            log=lambda message: self.log(job_id, message),
                            update_taxes=submit_custom_checkout_taxes,
                        )
                    except KakaoOaicsError as exc:
                        marker = "KAKAO_CONFIRM_ALREADY_SENT: " if exc.confirm_sent else ""
                        raise RuntimeError(
                            f"{marker}Kakao OAICS 在 {exc.state} 阶段失败：{exc}"
                        ) from exc
                    redirect_url = str(oaics_result.get("redirect_url") or "")
                    result.update({
                        "link_type": "kakao",
                        "checkout_provider": "open_ai_oaics",
                        "processor_entity": custom_processor,
                        "payment_method_type": "kakao_pay",
                        "provider_redirect_url": redirect_url,
                        "short_link": redirect_url,
                        "checkout_url": redirect_url,
                        "checkout_amount": oaics_result.get("checkout_amount"),
                        "amount_currency": oaics_result.get("amount_currency") or "KRW",
                        "amount_verification": "verified_zero",
                        "promo_applied": True if promo_requested else None,
                        "oaics_kakao": True,
                        "oaics_state_history": oaics_result.get("state_history") or [],
                        "oaics_runtime_source": oaics_result.get("runtime_source") or "",
                        "expires_at": int(time.time()) + 1800,
                    })
                    self.update(job_id, percent=100, text="Kakao OAICS 跳转链接生成完成", status="done", result=result)
                    return
                if provider == "gcash":
                    self.update(job_id, percent=58, text="正在读取 GCash 自定义支付方式")
                    custom_state = fetch_custom_checkout_session(
                        chatgpt_http, token, session_id, custom_processor, device_id,
                    )
                    initial_custom_methods = custom_state.get("custom_payment_methods") or []
                    initial_custom_method_id = next(
                        (str(item.get("id") or "") for item in initial_custom_methods
                         if str(item.get("id") or "").startswith("cpmt_")), "",
                    )
                    # OAICS can publish custom methods a moment after the checkout
                    # object itself becomes readable. Poll briefly before rebuilding.
                    for method_poll in range(1, 4):
                        if initial_custom_method_id:
                            break
                        time.sleep(0.8 * method_poll)
                        custom_state = fetch_custom_checkout_session(
                            chatgpt_http, token, session_id, custom_processor, device_id,
                        )
                        initial_custom_methods = custom_state.get("custom_payment_methods") or []
                        initial_custom_method_id = next(
                            (str(item.get("id") or "") for item in initial_custom_methods
                             if str(item.get("id") or "").startswith("cpmt_")), "",
                        )
                        method_state = "已获取" if initial_custom_method_id else "待同步"
                        self.log(job_id, f"GCash 支付方式同步检查 {method_poll}/3：{method_state}")
                    custom_amount = custom_checkout_amount_minor(custom_state)
                    custom_currency = custom_checkout_currency(custom_state) or "PHP"
                    if promo_requested and custom_amount not in {None, 0}:
                        self.update(job_id, percent=66, text="正在应用优惠并刷新 GCash Checkout")
                        update_checkout_promo(
                            promo_chatgpt_http, token, session_id, custom_processor,
                            options.get("promo_campaign") or "plus-1-month-free",
                            lambda m: self.log(job_id, m), device_id=device_id,
                        )
                        custom_state = fetch_custom_checkout_session(
                            chatgpt_http, token, session_id, custom_processor, device_id,
                        )
                        custom_amount = custom_checkout_amount_minor(custom_state)
                        custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    gcash_billing = default_billing("PH", meta.get("email") or "", real_random=True)
                    gcash_address = gcash_billing.get("address") or {}
                    self.update(job_id, percent=72, text="正在提交 PH 账单地址")
                    self.log(
                        job_id,
                        "GCash PH 账单：name={}，city={}，state={}，postal={}，source={}，place={}".format(
                            gcash_billing.get("name") or "-",
                            gcash_address.get("city") or "-",
                            gcash_address.get("state") or "-",
                            gcash_address.get("postal_code") or "-",
                            gcash_billing.get("_address_source") or "fallback",
                            gcash_billing.get("_place_name") or "-",
                        ),
                    )
                    tax_checkout = submit_custom_checkout_taxes(
                        chatgpt_http, token, session_id, custom_processor,
                        gcash_billing, custom_currency, device_id,
                    )
                    if tax_checkout:
                        custom_state = tax_checkout
                    else:
                        custom_state = fetch_custom_checkout_session(
                            chatgpt_http, token, session_id, custom_processor, device_id,
                        )
                    custom_amount = custom_checkout_amount_minor(custom_state)
                    custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    custom_methods = custom_state.get("custom_payment_methods") or []
                    custom_method_id = next(
                        (str(item.get("id") or "") for item in custom_methods
                         if str(item.get("id") or "").startswith("cpmt_")),
                        initial_custom_method_id,
                    )
                    if not custom_method_id:
                        raise RuntimeError("GCASH_METHOD_UNAVAILABLE: 当前 PH Checkout 尚未返回 GCash 支付方式，将更换代理重建")
                    self.update(job_id, percent=76, text="正在确认 GCash 支付方式")
                    try:
                        confirmed = confirm_custom_checkout_method(
                            chatgpt_http, token, session_id, custom_processor,
                            custom_method_id, entry_proxy, device_id, did,
                            use_sen=True, use_so=True, method_name="GCash",
                        )
                    except RuntimeError as confirm_error:
                        if "CUSTOM_CONFIRM_BLOCKED" not in str(confirm_error):
                            raise
                        self.log(job_id, "GCash confirm 首次被拦截，正在更新 SEN/SO 后重试")
                        time.sleep(1.2)
                        confirmed = confirm_custom_checkout_method(
                            chatgpt_http, token, session_id, custom_processor,
                            custom_method_id, entry_proxy, device_id, did,
                            use_sen=True, use_so=True, method_name="GCash",
                        )
                    self.update(job_id, percent=88, text="正在生成 GCash 跳转链接")
                    started = start_custom_checkout_method(
                        chatgpt_http, token, session_id, custom_processor,
                        custom_method_id, device_id,
                    )
                    action = started.get("next_action") or {}
                    redirect_url = str(action.get("url") or "").strip()
                    result.update({
                        "link_type": "gcash",
                        "checkout_provider": "open_ai",
                        "processor_entity": custom_processor,
                        "custom_payment_method_id": custom_method_id,
                        "payment_method_type": str(action.get("paymentMethodType") or "gcash"),
                        "provider_redirect_url": redirect_url,
                        "short_link": redirect_url,
                        "checkout_url": redirect_url,
                        "verification_url": str(confirmed.get("confirm_return_url") or ""),
                        "checkout_amount": custom_amount,
                        "amount_currency": custom_currency,
                        "amount_verification": (
                            "verified_zero" if custom_amount == 0
                            else ("pending" if custom_amount is None else "nonzero")
                        ),
                        "promo_applied": (
                            (custom_amount == 0)
                            if promo_requested and custom_amount is not None else None
                        ),
                        "expires_at": int(time.time()) + 1800,
                    })
                    if promo_requested and custom_amount not in {None, 0}:
                        raise RuntimeError(
                            f"GCash 优惠未生效：今日应付 amount={custom_amount} {custom_currency}"
                        )
                    self.update(job_id, percent=100, text="GCash 跳转链接生成完成", status="done", result=result)
                    return
                if provider != "hosted":
                    raise RuntimeError(
                        f"CUSTOM_CHECKOUT_REBUILD_REQUIRED: received {session_id}; "
                        f"{provider} requires a Stripe cs_* checkout"
                    )
                custom_processor = (
                    str(checkout_data.get("processor_entity") or "").strip()
                    or ("openai_llc" if country == "US" else "openai_ie")
                )
                custom_url = (
                    str(checkout_data.get("checkout_url") or "").strip()
                    or f"https://chatgpt.com/checkout/{custom_processor}/{session_id}"
                )
                custom_update: dict[str, Any] = {}
                if promo_requested:
                    self.update(job_id, percent=68, text="正在为 OAICS Checkout 应用优惠")
                    custom_update = update_checkout_promo(
                        promo_chatgpt_http,
                        token,
                        session_id,
                        custom_processor,
                        options.get("promo_campaign") or "plus-1-month-free",
                        lambda m: self.log(job_id, m),
                        device_id=device_id,
                    )
                custom_amount = custom_checkout_amount_minor(custom_update)
                custom_currency = custom_checkout_currency(custom_update) or options["currency"]
                if custom_amount is None:
                    try:
                        custom_page = chatgpt_http.get(
                            custom_url,
                            headers={
                                "Authorization": f"Bearer {token}",
                                "User-Agent": sc.CHROME_UA,
                                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                "Referer": "https://chatgpt.com/",
                            },
                            timeout=35,
                        )
                        custom_state = custom_checkout_state_from_html(custom_page.text or "")
                        custom_amount = custom_checkout_amount_minor(custom_state)
                        custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    except Exception as exc:
                        self.log(job_id, f"OAICS 金额页面读取提示：{type(exc).__name__}")
                custom_verification = (
                    "verified_zero" if custom_amount == 0
                    else ("pending" if custom_amount is None else "nonzero")
                )
                result.update({
                    "requested_link_type": str(options.get("requested_link_type") or provider),
                    "link_type": "oaics",
                    "checkout_provider": "open_ai",
                    "checkout_url": custom_url,
                    "short_link": custom_url,
                    "processor_entity": custom_processor,
                    "checkout_ui_mode": "custom",
                    "checkout_amount": custom_amount,
                    "amount_currency": custom_currency,
                    "amount_verification": custom_verification,
                    "promo_applied": (custom_amount == 0) if promo_requested and custom_amount is not None else None,
                })
                if promo_requested and custom_amount not in {None, 0}:
                    raise RuntimeError(
                        f"OAICS 优惠未生效：今日应付 amount={custom_amount} {custom_currency}"
                    )
                done_text = "OAICS Checkout 提链完成"
                if custom_amount is None:
                    done_text += "（金额待页面复核）"
                self.update(job_id, percent=100, text=done_text, status="done", result=result)
                return
            if provider == "hosted":
                self.update(job_id, percent=56, text="正在检测官方长链金额")
                if not session_id:
                    if promo_requested:
                        raise RuntimeError("官方长链未返回 Stripe Session ID，优惠金额校验失败")
                    self.update(job_id, percent=100, text="支付长链生成完成", status="done", result=result)
                    return

                hosted_stripe_http = sc.build_http(entry_proxy)
                hosted_profile = sc._profile(country)
                hosted_pk = str(checkout_data.get("publishable_key") or "") or sc.verify_pk(
                    hosted_stripe_http, session_id, lambda m: self.log(job_id, m)
                )
                hosted_customer_session = str(
                    checkout_data.get("customer_session_client_secret") or ""
                ).strip()
                if hosted_customer_session.startswith("cuss_secret_"):
                    sc.CHECKOUT_CUSTOMER_SESSION_SECRETS[session_id] = hosted_customer_session
                    self.log(job_id, "???? CustomerSession ???")
                hosted_init, hosted_version, hosted_ctx = sc.init_checkout(
                    hosted_stripe_http, session_id, hosted_pk, hosted_profile, lambda m: self.log(job_id, m)
                )
                exact_hosted_url = normalize_hosted_checkout_url(
                    hosted_ctx.get("stripe_hosted_url") or "", session_id
                )
                hosted_processor = (
                    str(checkout_data.get("processor_entity") or "")
                    or sc._entity_from_return_url(hosted_ctx.get("return_url") or hosted_init.get("return_url") or "")
                    or "openai_llc"
                )
                if not exact_hosted_url or "#" not in exact_hosted_url:
                    raise RuntimeError("Stripe did not return a complete Hosted Checkout URL")
                source_hosted_url = str(checkout_data.get("checkout_url") or "").strip()
                if session_id in source_hosted_url and "#" in source_hosted_url:
                    result["checkout_url"] = source_hosted_url
                else:
                    result["checkout_url"] = exact_hosted_url
                result["stripe_hosted_url"] = exact_hosted_url
                if options["plan"] == "codex_low":
                    result["short_link"] = ""
                    result["checkout_ui_mode"] = "hosted"
                hosted_amount = hosted_ctx.get("checkout_amount")
                try:
                    hosted_zero = int(str(hosted_amount)) == 0
                except (TypeError, ValueError):
                    hosted_zero = str(hosted_amount).strip() in {"0", "0.0", "0.00"}

                if promo_requested and not hosted_zero:
                    self.update(job_id, percent=68, text="正在应用优惠并同步金额")
                    update_checkout_promo(
                        chatgpt_http,
                        token,
                        session_id,
                        hosted_processor,
                        options.get("promo_campaign") or "plus-1-month-free",
                        lambda m: self.log(job_id, m),
                        device_id=device_id,
                    )
                    for sync_attempt in range(6):
                        time.sleep(1.5 if sync_attempt else 0.8)
                        hosted_init, hosted_version, hosted_ctx = sc.init_checkout(
                            hosted_stripe_http, session_id, hosted_pk, hosted_profile, lambda m: self.log(job_id, m)
                        )
                        hosted_amount = hosted_ctx.get("checkout_amount")
                        self.log(job_id, f"官方长链优惠同步检查 {sync_attempt + 1}/6：amount={hosted_amount}")
                        try:
                            hosted_zero = int(str(hosted_amount)) == 0
                        except (TypeError, ValueError):
                            hosted_zero = str(hosted_amount).strip() in {"0", "0.0", "0.00"}
                        if hosted_zero:
                            break

                hosted_elements = sc.fetch_elements_session(
                    hosted_stripe_http,
                    hosted_pk,
                    session_id,
                    hosted_ctx,
                    hosted_version,
                    hosted_profile,
                    lambda m: self.log(job_id, m),
                )

                # Keep saved-card availability for the UI, without enforcing
                # or exposing the saved card billing country.
                saved_method_count = 0
                for saved_source in (hosted_init, hosted_elements):
                    if not isinstance(saved_source, dict):
                        continue
                    saved_customer = saved_source.get("customer") or saved_source.get("legacy_customer") or {}
                    if not isinstance(saved_customer, dict):
                        continue
                    saved_methods = saved_customer.get("payment_methods") or []
                    if isinstance(saved_methods, list):
                        saved_method_count = max(saved_method_count, len(saved_methods))
                result["saved_payment_method_count"] = saved_method_count

                hosted_billing = default_billing(country, meta.get("email") or "")
                sc.update_tax_region(
                    hosted_stripe_http,
                    session_id,
                    hosted_pk,
                    hosted_version,
                    hosted_ctx,
                    hosted_billing,
                    hosted_profile,
                    lambda m: self.log(job_id, m),
                )
                hosted_amount = hosted_ctx.get("checkout_amount")
                try:
                    hosted_zero = int(str(hosted_amount)) == 0
                except (TypeError, ValueError):
                    hosted_zero = str(hosted_amount).strip() in {"0", "0.0", "0.00"}
                result.update({
                    "checkout_amount": hosted_amount,
                    "promo_applied": hosted_zero if promo_requested else None,
                    "payment_method_types": hosted_ctx.get("payment_method_types") or [],
                    "processor_entity": hosted_processor,
                    "stripe_publishable_key": hosted_pk,
                })
                if promo_requested and not hosted_zero:
                    raise RuntimeError(f"官方长链优惠未生效：Stripe 今日应付 amount={hosted_amount}")
                if promo_requested:
                    self.log(job_id, "官方长链金额校验通过：Stripe 今日应付 amount=0")
                else:
                    self.log(job_id, f"官方长链金额检测完成：Stripe 今日应付 amount={hosted_amount}")
                self.update(job_id, percent=100, text="支付长链生成完成", status="done", result=result)
                return

            stage3_text = "第 3/7 步：正在初始化 PIX" if provider == "pix" else (
                "第 3/7 步：正在初始化 PayPal" if provider == "paypal" and promo_requested else f"正在初始化 {provider.upper()}"
            )
            self.update(job_id, percent=56, text=stage3_text)
            billing_geo = None
            if provider == "paypal" and str(options.get("payment_proxy_country") or "").upper() == country:
                billing_geo = payment_geo
            billing = default_billing(
                country,
                meta.get("email") or "",
                options.get("pix_tax_id") or "",
                billing_geo,
                real_random=(provider == "paypal"),
            )
            if provider == "paypal":
                selected_address = billing.get("address") or {}
                self.log(
                    job_id,
                    "PayPal 本轮随机真实账单：source={}，城市={}，邮编={}，地点={}".format(
                        billing.get("_address_source") or "unknown",
                        selected_address.get("city") or "-",
                        selected_address.get("postal_code") or "-",
                        billing.get("_place_name") or "公开场所",
                    ),
                )
            paypal_payment_billing = None
            if provider == "paypal":
                paypal_country = str(options.get("payment_proxy_country") or country).upper()
                if paypal_country != country:
                    paypal_payment_billing = default_billing(
                        paypal_country,
                        meta.get("email") or "",
                        geo=payment_geo,
                        real_random=True,
                    )
                    paypal_address = paypal_payment_billing.get("address") or {}
                    self.log(
                        job_id,
                        f"PayPal separated billing: OpenAI={country}/{options.get('currency')}, "
                        f"PayPal={paypal_country}, city={paypal_address.get('city') or '-'}, "
                        f"postal={paypal_address.get('postal_code') or '-'}",
                    )
            promotion_billing = None
            if provider == "paypal" and promo_requested:
                promotion_country = str(main_country or "BR").upper()
                promotion_billing = default_billing(
                    promotion_country,
                    meta.get("email") or "",
                )
                self.log(
                    job_id,
                    f"PayPal 地区：优惠更新={promotion_country}，Stripe/PayPal 账单与 merchant 快照={country}",
                )
            if provider == "pix":
                identity = options.get("pix_identity") or {}
                if identity:
                    billing["name"] = identity.get("name") or billing.get("name")
                    billing["email"] = identity.get("email") or billing.get("email")
                    address = billing.setdefault("address", {})
                    for key in ("line1", "city", "state", "postal_code"):
                        if identity.get(key):
                            address[key] = identity[key]
                    if identity.get("source") == "brasilapi_cnpj":
                        self.log(job_id, f"PIX 已匹配 CNPJ 登记主体：{billing.get('name')} / {address.get('state')}")
                    elif str(identity.get("source") or "").startswith("generated_"):
                        generated_kind = str(identity.get("source")).removeprefix("generated_").upper()
                        self.log(job_id, f"PIX 本轮已自动生成 {generated_kind}、持有人/企业名称及巴西地址")
            stripe_http = sc.build_http(exit_proxy)

            progress_mark = 62

            def advance_progress(percent: int, text: str):
                nonlocal progress_mark
                self.ensure_not_cancelled(job_id)
                if percent > progress_mark:
                    progress_mark = percent
                    self.update(job_id, percent=percent, text=text)

            def provider_log(message: str):
                self.log(job_id, message)
                lowered_message = message.lower()
                if "init ok" in lowered_message:
                    advance_progress(64, "支付方式初始化完成")
                elif "checkout/update" in lowered_message or "优惠更新完成" in message:
                    advance_progress(72, "优惠已应用，正在确认金额")
                elif "tax_region" in lowered_message:
                    advance_progress(78, "金额确认完成，正在提交账单信息")
                elif "snapshot billing" in lowered_message:
                    advance_progress(84, "账单信息已提交")
                elif "payment_method" in lowered_message:
                    advance_progress(88, "支付方式已创建")
                elif "manual_approval" in lowered_message or "approve:" in lowered_message:
                    advance_progress(92, "正在确认支付请求")
                elif "poll" in lowered_message:
                    advance_progress(96, "正在获取最终结果")

            def approve_cb(processor: str):
                self.ensure_not_cancelled(job_id)
                advance_progress(90, "正在确认支付请求")
                self.log(job_id, "提交 Checkout approval")
                approve_checkout(
                    token,
                    session_id,
                    processor,
                    checkout_proxy,
                    device_id,
                    did,
                    http=provider_chatgpt_http,
                    log=provider_log,
                )
                self.ensure_not_cancelled(job_id)

            def apply_promo_cb(processor: str):
                self.ensure_not_cancelled(job_id)
                if provider == "pix":
                    self.log(job_id, "第 4/7 步：初始化已确认 PIX，开始应用优惠")
                elif provider == "paypal":
                    self.log(job_id, "PayPal 已确认可用，正在应用优惠")
                elif provider == "upi":
                    self.log(job_id, "UPI 已确认可用，正在应用优惠")
                elif provider == "momo":
                    self.log(job_id, "MoMo 已确认可用，正在应用优惠")
                elif provider == "ideal":
                    self.log(job_id, "iDEAL 已确认可用，正在通过代理池 1 提交优惠；最终以 Stripe 今日应付金额为准")
                elif provider == "twint":
                    self.log(job_id, "TWINT 已确认可用，正在应用首月优惠并校验 CHF 今日应付金额")
                advance_progress(70, "正在应用优惠")
                campaign = options.get("promo_campaign") or "plus-1-month-free"
                response = update_checkout_promo(
                    promo_chatgpt_http,
                    token,
                    session_id,
                    processor,
                    campaign,
                    provider_log,
                    device_id=device_id,
                )
                self.ensure_not_cancelled(job_id)
                return response

            self.update(job_id, percent=62, text="正在生成支付结果")
            provider_result = stripe_to_provider(
                stripe_http,
                session_id,
                provider,
                billing=billing,
                promotion_billing=promotion_billing,
                payment_billing=paypal_payment_billing,
                payment_http=stripe_http if paypal_payment_billing else None,
                country=options.get("checkout_country") or country,
                chatgpt_http=provider_chatgpt_http,
                access_token=token,
                stage1=checkout_data,
                # PayPal 保持原协议的 Bearer approval；PIX/UPI 才使用带
                # Sentinel 的 callback。PayPal approval 返回 approved 后仍
                # 卡住时，额外 Sentinel 上下文会让批准结果与 Stripe
                # submission 不同步。
                approve_callback=None if provider == "paypal" else approve_cb,
                apply_promo_callback=apply_promo_cb if provider in {"pix", "momo", "gcash", "paypal", "upi", "ideal", "twint"} and promo_requested else None,
                ideal_bank=options.get("ideal_bank", ""),
                require_zero_due=promo_requested,
                local_method_strategy=options.get("local_method_strategy") or "standalone",
                log=provider_log,
            )
            self.ensure_not_cancelled(job_id)
            self.update(job_id, percent=98, text="结果已生成，正在整理页面")
            result.update(provider_result)
            # Display the currency Stripe actually returned instead of only
            # echoing the requested currency.  This also makes automatic
            # proxy-region adaptation observable in the result panel/API.
            if provider_result.get("checkout_currency"):
                result["currency"] = str(provider_result["checkout_currency"]).upper()
                result["checkout_currency"] = result["currency"]
            done_text = "第 7/7 步：PIX 二维码生成完成" if provider == "pix" else (
                "第 7/7 步：MoMo 支付结果生成完成" if provider == "momo" else (
                "第 7/7 步：PayPal agreements/approve 链接生成完成" if provider == "paypal" else f"{provider.upper()} 提取完成"
                )
            )
            self.update(job_id, percent=100, text=done_text, status="done", result=result)
        except InterruptedError as exc:
            self.update(job_id, status="cancelled", percent=100, text=str(exc), error=str(exc))
        except Exception as exc:
            raw_error = str(exc)
            error_text = raw_error
            lowered = raw_error.lower()
            if "token_invalidated" in lowered or "authentication token has been invalidated" in lowered:
                error_text = "Access Token 已失效，请重新登录 ChatGPT 获取新的 Session JSON 或 AT。"
            elif "token_expired" in lowered or "jwt expired" in lowered:
                error_text = "Access Token 已过期，请重新登录 ChatGPT 获取新的 Session JSON 或 AT。"
            elif "not_eligible" in lowered:
                error_text = "当前账号未开放所选套餐或支付通道。"
            elif "cannot combine currencies" in lowered:
                error_text = "该账号已有其他币种的活跃结账会话，请等待原会话释放，或更换账号后再生成当前币种链接。"
            elif "amount_too_small" in lowered:
                error_text = "当前地区换算后的结账金额低于支付提供商下限，请提高 Codex 积分数量后重试。"
            self.log(job_id, f"错误：{type(exc).__name__}: {error_text}")
            if options.get("retry_wrapper"):
                self.update(job_id, status="running", percent=8, text="本次未成功，正在更换代理重试", error=error_text[:1200])
            else:
                self.update(job_id, status="error", percent=100, text="任务失败", error=error_text[:1200])


STORE = JobStore()


@app.after_request
def security_headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def _internal_key_valid(value: str) -> bool:
    expected = str(os.getenv("PAY153_INTERNAL_KEY") or "").strip()
    supplied = str(value or "").strip()
    return bool(expected and supplied and hmac.compare_digest(supplied, expected))


def _private_page_key_valid(value: str) -> bool:
    expected = str(os.getenv("PAY153_PRIVATE_PAGE_KEY") or "").strip()
    supplied = str(value or "").strip()
    return bool(expected and supplied and hmac.compare_digest(supplied, expected))


def classify_checkout_kind(checkout: dict[str, Any]) -> str:
    """Classify a newly-created Checkout without advancing its payment state."""
    session_id = str(checkout.get("checkout_session_id") or "").strip().lower()
    provider = str(checkout.get("checkout_provider") or "").strip().lower()
    if session_id.startswith("oaics_") or provider in {"open_ai", "oaics", "open_ai_oaics"}:
        return "oaics"
    if session_id.startswith("cs_live_"):
        return "cs_live"
    return "unknown"


KNOWN_GCASH_METHOD_IDS = frozenset({
    # qualification-test upstream records this opaque OpenAI custom method as
    # GCash even when the Checkout response omits a human-readable label.
    "cpmt_1TOgstC6h1nxGoI3WUVEY2cJ",
})


def gcash_custom_payment_method_id(methods: Any) -> str:
    """Return only a published GCash ``cpmt_*`` method, not another wallet."""
    if not isinstance(methods, list):
        return ""
    for item in methods:
        if not isinstance(item, dict):
            continue
        method_id = str(item.get("id") or "")
        if not method_id.startswith("cpmt_"):
            continue
        if method_id in KNOWN_GCASH_METHOD_IDS:
            return method_id
        if "gcash" in json.dumps(item, ensure_ascii=False).lower():
            return method_id
    return ""


def gcash_checkout_method(checkout: Any) -> tuple[str, str]:
    """Read GCash evidence directly from a successful Checkout response.

    The fast account-page probe intentionally does not classify the Checkout
    session backend and does not perform a follow-up OAICS state request.  It
    only accepts explicit GCash evidence in payment-method fields returned by
    the PH/PHP Checkout creation call.
    """
    if not isinstance(checkout, dict):
        return "", ""
    containers: list[tuple[str, dict[str, Any]]] = [("checkout", checkout)]
    for container_key in ("checkout_session", "session", "payment_method_configuration"):
        nested = checkout.get(container_key)
        if isinstance(nested, dict):
            containers.append((container_key, nested))
    for container_name, container in containers:
        for field in (
            "payment_method_types",
            "custom_payment_methods",
            "available_payment_methods",
            "payment_methods",
        ):
            methods = container.get(field)
            custom_method_id = gcash_custom_payment_method_id(methods)
            if custom_method_id:
                return custom_method_id, f"{container_name}.{field}"
            if not isinstance(methods, list):
                continue
            for item in methods:
                if isinstance(item, str):
                    value = item.strip()
                    if value in KNOWN_GCASH_METHOD_IDS:
                        return value, f"{container_name}.{field}"
                    if value.lower() == "gcash":
                        return "gcash", f"{container_name}.{field}"
                if not isinstance(item, dict):
                    continue
                method_id = str(item.get("id") or "").strip()
                labels = (
                    item.get("type"), item.get("name"), item.get("display_name"),
                    item.get("label"), item.get("payment_method_type"),
                )
                if any(str(label or "").strip().lower() == "gcash" for label in labels):
                    return method_id if method_id.startswith("cpmt_") else "gcash", f"{container_name}.{field}"
    return "", ""


@app.get("/private-checkout")
def private_checkout_page():
    bootstrap_key = str(request.args.get("key") or "").strip()
    if _private_page_key_valid(bootstrap_key):
        response = redirect("/private-checkout", code=302)
        response.set_cookie(
            "pay153_private_lane",
            bootstrap_key,
            max_age=30 * 24 * 60 * 60,
            secure=True,
            httponly=True,
            samesite="Strict",
        )
        return response
    if not _private_page_key_valid(request.cookies.get("pay153_private_lane") or ""):
        return "Not Found", 404
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/paypal-oaics/proxy-check")
def check_paypal_oaics_proxies():
    data = request.get_json(silent=True) or {}
    raw = data.get("proxies")
    try:
        normalized = normalize_paypal_oaics_proxies(raw)
        expected_country, _ = resolve_paypal_oaics_countries(
            str(data.get("expected_country") or "BR"),
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "kind": "format"}), 400

    candidates = normalized[:PAYPAL_OAICS_PROXY_CHECK_LIMIT]
    checked: list[dict[str, Any] | None] = [None] * len(candidates)
    executor = ThreadPoolExecutor(
        max_workers=max(1, len(candidates)),
        thread_name_prefix="paypal-oaics-proxy-check",
    )
    future_map = {
        executor.submit(_cached_paypal_oaics_proxy_probe, proxy): index
        for index, proxy in enumerate(candidates)
    }
    try:
        for future in as_completed(future_map):
            index = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "reachable": False,
                    "country": "",
                    "br_compatible": False,
                    "error": type(exc).__name__,
                }
            checked[index] = {"line": index + 1, **result}
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    results = [item for item in checked if item is not None]
    reachable = sum(item.get("reachable") is True for item in results)
    br_compatible = sum(item.get("br_compatible") is True for item in results)
    country_compatible = sum(
        str(item.get("country") or "").upper() == expected_country.code
        for item in results
    )
    return jsonify({
        "ok": True,
        "total": len(normalized),
        "checked": len(results),
        "check_limit": PAYPAL_OAICS_PROXY_CHECK_LIMIT,
        "truncated": len(normalized) > len(results),
        "reachable": reachable,
        "br_compatible": br_compatible,
        "expected_country": expected_country.code,
        "country_compatible": country_compatible,
        "failed": len(results) - reachable,
        "results": results,
    })


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "pay153",
        "capabilities": ["checkout_kind_v1", "gopay_qualification_v1"],
        "time": int(time.time()),
    })


def detect_gcash(data: dict[str, Any] | None) -> tuple[dict[str, Any], int]:
    """Create one PH/PHP Plus Checkout and read its direct GCash evidence.

    This fast path performs no OAICS type recognition, state polling, tax
    update, PaymentMethod creation, ctoken, start, or confirm request.
    """
    data = data if isinstance(data, dict) else {}
    token_raw = str(data.get("token") or "").strip()
    if not token_raw:
        return {"ok": False, "gcash": False, "error": "缺少 AT/access_token"}, 400
    try:
        token, meta = extract_access_token(token_raw)
    except Exception as exc:
        return {"ok": False, "gcash": False, "error": f"AT 解析失败：{exc}"}, 400
    proxy = str(data.get("proxy") or "").strip()
    proxy_source = "request" if proxy else "direct"
    if proxy:
        try:
            proxy = normalize_proxy(proxy)
        except ValueError as exc:
            return {"ok": False, "gcash": False, "error": f"代理格式错误：{exc}"}, 400
    device_id = str(uuid.uuid4())
    did = str(uuid.uuid4())
    options = {
        "plan": "plus",
        # "detection" selects the generic custom Checkout response; it does not
        # run Kakao/Stripe/PayPal processing and never confirms a method.
        "link_type": "detection",
        "country": "PH",
        "currency": "PHP",
        "checkout_country": "PH",
        "checkout_currency": "PHP",
        "use_promo": False,
    }
    try:
        # Checkout 创建本身也可能要求 Sentinel / SO。资格检测虽然不会
        # confirm/start，仍须和真实 Checkout 使用相同的挑战头，否则容易
        # 被上游判为 unusual activity 或直接返回 400。
        created = create_checkout(
            token,
            checkout_payload(options, meta),
            proxy,
            device_id,
            did,
            lambda _message: None,
            use_sen=True,
            use_so=True,
        )
        checkout = dict(created.get("data") or {})
        session_id = str(checkout.get("checkout_session_id") or "")
        if not session_id and not str(checkout.get("url") or checkout.get("checkout_url") or "").strip():
            return {
                "ok": False, "gcash": False,
                "error": "创建 PH/PHP Checkout 失败：未返回 Session",
            }, 502
        method_evidence, evidence_source = gcash_checkout_method(checkout)
        gcash = bool(method_evidence)
        return {
            "ok": True,
            "gcash": gcash,
            "detection_outcome": "qualified" if gcash else "no_gcash_in_create_response",
            "custom_payment_method_id": method_evidence if method_evidence.startswith("cpmt_") else "",
            "gcash_method": "gcash" if gcash else "",
            "gcash_evidence_source": evidence_source,
            "checkout_created": True,
            "confirm_sent": False,
            "promo_update_sent": False,
            "proxy_source": proxy_source,
            "checkout_country": "PH",
            "checkout_currency": str(checkout.get("checkout_currency") or checkout.get("currency") or "PHP").upper(),
            "checked_at": int(time.time()),
            "error": None if gcash else "当前 PH/PHP Checkout 创建响应未包含 GCash 支付方式",
        }, 200
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {str(exc)[:500]}"
        upstream_status = re.search(r"OpenAI Checkout HTTP\s+(\d{3})", error_text)
        return {
            "ok": False,
            "gcash": False,
            "confirm_sent": False,
            "promo_update_sent": False,
            "upstream_http_status": int(upstream_status.group(1)) if upstream_status else None,
            "error": error_text,
        }, 502


def _gopay_payment_method_available(methods: Any) -> bool:
    """Return whether Stripe published GoPay for the ID/IDR Checkout."""
    if not isinstance(methods, list):
        return False
    for method in methods:
        if isinstance(method, str):
            value = method.strip().lower()
            if value in {"gopay", "go_pay"} or "gopay" in value.replace("_", ""):
                return True
            continue
        if isinstance(method, dict):
            raw = " ".join(
                str(method.get(key) or "")
                for key in ("type", "name", "display_name", "payment_method_type", "provider", "id")
            ).lower()
            if "gopay" in raw.replace("_", ""):
                return True
    return False


def detect_gopay(data: dict[str, Any] | None) -> tuple[dict[str, Any], int]:
    """Create one zero-due ID/IDR Checkout and read GoPay eligibility only.

    This follows the public link-gp Checkout/Stripe boundary: create the
    Indonesia Checkout with the Plus promotion, initialize Stripe once, then
    stop after checking currency/payment methods.  The account qualification
    decision intentionally ignores the Checkout amount.  It deliberately does
    not create an Elements session, update taxes, attach a PaymentMethod,
    confirm, approve, or poll the Checkout.
    """
    data = data if isinstance(data, dict) else {}
    token_raw = str(data.get("token") or "").strip()
    if not token_raw:
        return {"ok": False, "gopay": False, "error": "缺少 AT/access_token"}, 400
    try:
        token, meta = extract_access_token(token_raw)
    except Exception as exc:
        return {"ok": False, "gopay": False, "error": f"AT 解析失败：{exc}"}, 400
    proxy = str(data.get("proxy") or "").strip()
    proxy_source = "request" if proxy else "direct"
    if proxy:
        try:
            proxy = normalize_proxy(proxy)
        except ValueError as exc:
            return {"ok": False, "gopay": False, "error": f"代理格式错误：{exc}"}, 400
    device_id = str(uuid.uuid4())
    did = str(uuid.uuid4())
    options = {
        "plan": "plus",
        "link_type": "detection",
        "country": "ID",
        "currency": "IDR",
        "checkout_country": "ID",
        "checkout_currency": "IDR",
        "use_promo": True,
        "promo_campaign": "plus-1-month-free",
    }
    try:
        created = create_checkout(
            token,
            checkout_payload(options, meta),
            proxy,
            device_id,
            did,
            lambda _message: None,
            use_sen=True,
            use_so=True,
        )
        checkout = dict(created.get("data") or {})
        http = created.get("http")
        session_id = str(checkout.get("checkout_session_id") or "")
        if not re.fullmatch(r"cs_(?:live|test)_[A-Za-z0-9]+", session_id):
            return {
                "ok": True,
                "gopay": False,
                "detection_outcome": "unsupported_custom_checkout",
                "checkout_created": bool(session_id or checkout.get("checkout_url") or checkout.get("url")),
                "confirm_sent": False,
                "proxy_source": proxy_source,
                "checkout_country": "ID",
                "checkout_currency": "IDR",
                "checked_at": int(time.time()),
                "error": "ID/IDR Checkout 未返回可用于 GoPay 资格判断的 Stripe cs_* 会话",
            }, 200
        stripe_pk = str(
            checkout.get("stripe_publishable_key")
            or checkout.get("publishable_key")
            or checkout.get("publishableKey")
            or checkout.get("key")
            or ""
        ).strip()
        profile = dict(getattr(sc, "LOCALE_PROFILES", {}).get("ID") or {})
        profile.setdefault("browser_locale", "id-ID")
        profile.setdefault("browser_timezone", "Asia/Jakarta")
        profile.setdefault("browser_language", "id-ID")
        if not stripe_pk:
            stripe_pk = sc.verify_pk(http, session_id, lambda _message: None)
        init_data, _version, ctx = sc.init_checkout(
            http, session_id, stripe_pk, profile, lambda _message: None,
        )
        amount = ctx.get("checkout_amount")
        currency = str(ctx.get("currency") or init_data.get("currency") or "").lower()
        methods = list(ctx.get("payment_method_types") or sc._extract_payment_method_types(init_data))
        gopay = currency == "idr" and _gopay_payment_method_available(methods)
        return {
            "ok": True,
            "gopay": gopay,
            "detection_outcome": "qualified" if gopay else "no_gopay_in_stripe_init",
            "payment_method_types": methods,
            "checkout_created": True,
            "confirm_sent": False,
            "proxy_source": proxy_source,
            "checkout_country": "ID",
            "checkout_currency": currency.upper() or "IDR",
            "checkout_amount": amount,
            "checked_at": int(time.time()),
            "error": None if gopay else f"ID/IDR Stripe Checkout 未发布 GoPay（currency={currency or 'missing'}）",
        }, 200
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {str(exc)[:500]}"
        upstream_status = re.search(r"OpenAI Checkout HTTP\s+(\d{3})", error_text)
        return {
            "ok": False,
            "gopay": False,
            "confirm_sent": False,
            "promo_update_sent": False,
            "upstream_http_status": int(upstream_status.group(1)) if upstream_status else None,
            "error": error_text,
        }, 502


def detect_checkout_kind(data: dict[str, Any] | None) -> tuple[dict[str, Any], int]:
    """Create the smallest DE/EUR Plus Checkout and classify its backend."""
    data = data if isinstance(data, dict) else {}
    token_raw = str(data.get("token") or "").strip()
    if not token_raw:
        return {"ok": False, "error": "缺少 AT/access_token"}, 400
    try:
        token, meta = extract_access_token(token_raw)
    except Exception as exc:
        return {"ok": False, "error": f"AT 解析失败：{exc}"}, 400
    proxy = str(data.get("proxy") or "").strip()
    proxy_source = "request" if proxy else "direct"
    if proxy:
        try:
            proxy = normalize_proxy(proxy)
        except ValueError as exc:
            return {"ok": False, "error": f"代理格式错误：{exc}"}, 400
    device_id = str(uuid.uuid4())
    did = str(uuid.uuid4())
    options = {
        "plan": "plus",
        # "detection" selects the generic custom Checkout response. It is not
        # a payment method and does not run Kakao/Stripe/PayPal processing.
        "link_type": "detection",
        "country": "DE",
        "currency": "EUR",
        "checkout_country": "DE",
        "checkout_currency": "EUR",
        "use_promo": False,
    }
    try:
        created = create_checkout(
            token,
            checkout_payload(options, meta),
            proxy,
            device_id,
            did,
            lambda _message: None,
            use_sen=True,
            use_so=True,
        )
        checkout = dict(created.get("data") or {})
        kind = classify_checkout_kind(checkout)
        session_id = str(checkout.get("checkout_session_id") or "")
        provider = str(checkout.get("checkout_provider") or "")
        processor = str(checkout.get("processor_entity") or "")
        return {
            "ok": kind in {"oaics", "cs_live"},
            "kind": kind,
            "checkout_provider": provider,
            "processor_entity": processor,
            "session_prefix": "oaics_" if kind == "oaics" else ("cs_live_" if kind == "cs_live" else session_id[:16]),
            "proxy_source": proxy_source,
            "checkout_country": "DE",
            "checkout_currency": "EUR",
            "confirm_sent": False,
            "promo_attached": False,
            "promo_update_sent": False,
            "checked_at": int(time.time()),
            "error": None if kind in {"oaics", "cs_live"} else "Checkout 返回了无法识别的 Session 类型",
        }, 200 if kind in {"oaics", "cs_live"} else 422
    except Exception as exc:
        return {
            "ok": False,
            "kind": "unknown",
            "confirm_sent": False,
            "promo_update_sent": False,
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }, 502


@app.post("/api/checkout-kind")
def checkout_kind():
    """HTTP compatibility wrapper for the single-port in-process detector."""
    payload, status = detect_checkout_kind(request.get_json(silent=True) or {})
    return jsonify(payload), status


@app.post("/api/gcash")
def gcash():
    """HTTP compatibility wrapper for the in-process GCash eligibility detector."""
    payload, status = detect_gcash(request.get_json(silent=True) or {})
    return jsonify(payload), status


@app.post("/api/gopay")
def gopay():
    """HTTP compatibility wrapper for the GoPay qualification detector."""
    payload, status = detect_gopay(request.get_json(silent=True) or {})
    return jsonify(payload), status


@app.get("/api/config")
def config():
    return jsonify({
        "plans": list(PLANS),
        "link_types": ["hosted", "ph_short", "paypal", "paypal_oaics", "ideal", "twint", "pix", "momo", "gcash", "kakao"]
            + (["upi"] if UPI_ENABLED else []),
        "disabled_link_types": [] if UPI_ENABLED else ["upi"],
        "country_currency": COUNTRY_CURRENCY,
        "provider_defaults": PROVIDER_DEFAULTS,
        "paypal_oaics_countries": paypal_oaics_country_options(),
        "proxy_policy": {
            "entry_required": True,
            "exit_required_for": ["ph_short", "paypal", "ideal", "twint", "upi", "gcash", "kakao"],
            "single_chain_for": ["paypal_oaics", "pix", "momo"],
            "max_per_pool": 500,
            "selection": "random_per_job",
        },
        "retry_policy": {"min": 1, "max": 50, "default_pix": 10, "default_other": 3},
        "pix_identity_policy": {"default": "cpf", "auto_kinds": ["cpf", "mixed", "cnpj"], "regenerate_each_attempt": True},
        "task_limits": {
            "global_rpm": 0,
            "per_ip_rpm": 0,
            "rate_limit_enabled": False,
            "queue_enabled": True,
            "workers": STORE.worker_limit,
        },
    })


@app.post("/api/checkout")
def start_checkout():
    data = request.get_json(silent=True) or {}
    internal_request = bool(
        _internal_key_valid(request.headers.get("X-Pay153-Internal-Key") or "")
        or _private_page_key_valid(request.cookies.get("pay153_private_lane") or "")
    )
    plan = str(data.get("plan") or "plus").lower()
    link_type = str(data.get("link_type") or "hosted").lower()
    if plan not in PLANS:
        return jsonify({"error": "计划类型不正确"}), 400
    if link_type not in {"hosted", "ph_short", "paypal", "paypal_oaics", "ideal", "twint", "upi", "pix", "momo", "gcash", "kakao"}:
        return jsonify({"error": "提取方式不正确"}), 400
    if link_type == "upi" and not UPI_ENABLED:
        return jsonify({"error": "UPI 提链已暂停维护"}), 503
    if link_type == "ph_short" and plan != "plus":
        return jsonify({"error": "菲律宾短链仅支持 Plus 计划"}), 400
    if link_type == "paypal_oaics" and plan != "plus":
        return jsonify({"error": "PayPal OAICS 仅支持 Plus 计划"}), 400
    defaults = PROVIDER_DEFAULTS.get(link_type, {})
    country = str(
        (data.get("proxy_country") or data.get("country") or "BR")
        if link_type == "paypal_oaics"
        else (data.get("country") or defaults.get("country") or "US")
    ).upper()
    checkout_country = country
    checkout_currency = ""
    paypal_proxy_country = ""
    paypal_billing_country = ""
    if link_type == "paypal_oaics":
        try:
            proxy_profile, billing_profile = resolve_paypal_oaics_countries(
                str(data.get("proxy_country") or country or "BR"),
                str(data.get("billing_country") or "DE"),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        paypal_proxy_country = country = proxy_profile.code
        paypal_billing_country = checkout_country = billing_profile.code
        currency = proxy_profile.currency
        checkout_currency = billing_profile.currency
    else:
        requested_currency = str(
            data.get("currency")
            or (COUNTRY_CURRENCY.get(country) if link_type == "paypal" else "")
            or defaults.get("currency")
            or COUNTRY_CURRENCY.get(country, "USD")
        ).upper()
        currency, _currency_source = normalize_checkout_currency(country, requested_currency)
        checkout_currency = currency
    entry_raw = data.get("entry_proxies")
    if entry_raw is None:
        entry_raw = data.get("entry_proxy") or data.get("api_proxy") or data.get("proxy") or ""
    exit_raw = data.get("exit_proxies")
    if exit_raw is None:
        exit_raw = data.get("exit_proxy") or data.get("payment_proxy") or ""
    dynamic_proxy_api = bool(data.get("dynamic_proxy_api")) and internal_request
    if not entry_raw and (not dynamic_proxy_api or link_type == "paypal_oaics"):
        return jsonify({"error": "请填写 Checkout 入口代理"}), 400
    if link_type not in {"hosted", "paypal_oaics", "pix", "momo"} and not exit_raw and not dynamic_proxy_api:
        return jsonify({"error": "当前支付路径需要填写支付出口代理"}), 400
    try:
        entry_proxies = (
            normalize_paypal_oaics_proxies(entry_raw)
            if link_type == "paypal_oaics" and entry_raw
            else (normalize_proxy_pool(entry_raw, "入口代理") if entry_raw else [])
        )
        exit_proxies = normalize_proxy_pool(exit_raw, "出口代理") if exit_raw else []
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not entry_proxies and (not dynamic_proxy_api or link_type == "paypal_oaics"):
        return jsonify({"error": "入口代理至少填写 1 条"}), 400
    if link_type not in {"hosted", "paypal_oaics", "pix", "momo"} and not exit_proxies and not dynamic_proxy_api:
        return jsonify({"error": "出口代理至少填写 1 条"}), 400
    raw_pix_tax_id = re.sub(r"\D", "", str(data.get("pix_tax_id") or ""))[:14] if link_type == "pix" else ""
    try:
        retry_limit = 50 if link_type == "paypal_oaics" else 10
        retry_count = min(retry_limit, max(1, int(data.get("retry_count") or (10 if link_type in {"pix", "momo", "gcash"} else 3))))
        provider_attempts = min(50, max(1, int(data.get("provider_attempts") or 10)))
    except (TypeError, ValueError):
        return jsonify({"error": "重试次数需要填写整数"}), 400
    pix_identity: dict[str, str] = {}
    if link_type == "pix":
        manual_identity = {
            "name": str(data.get("pix_name") or "").strip()[:160],
            "email": str(data.get("pix_email") or "").strip()[:200],
            "line1": str(data.get("pix_line1") or "").strip()[:180],
            "city": str(data.get("pix_city") or "").strip()[:100],
            "state": str(data.get("pix_state") or "").strip()[:40],
            "postal_code": str(data.get("pix_postal_code") or "").strip()[:30],
        }
        if len(raw_pix_tax_id) == 14:
            try:
                pix_identity.update(lookup_cnpj_identity(raw_pix_tax_id))
            except Exception as exc:
                if not manual_identity["name"]:
                    return jsonify({"error": f"CNPJ 登记信息查询失败：{exc}"}), 400
        pix_identity.update({key: value for key, value in manual_identity.items() if value})
    options = {
        "token_raw": str(data.get("token") or ""),
        "plan": plan,
        "link_type": link_type,
        "requested_link_type": link_type,
        "country": country,
        "currency": currency,
        "checkout_country": checkout_country,
        "checkout_currency": checkout_currency,
        "entry_proxies": entry_proxies,
        "exit_proxies": (exit_proxies or entry_proxies) if link_type in {"paypal_oaics", "pix", "momo"} else exit_proxies,
        "provider_attempts": provider_attempts,
        "use_promo": True if link_type == "paypal_oaics" else (bool(data.get("use_promo", True)) if plan == "plus" else False),
        "promo_campaign": "plus-1-month-free" if link_type == "paypal_oaics" else (str(data.get("promo_campaign") or "") if plan == "plus" else ""),
        "promo_country": str(
            data.get("promo_country") or ("VN" if link_type == "kakao" and plan == "plus" else "")
        ).strip().upper()[:2],
        "promo_code": str(data.get("promo_code") or "") if plan == "team" else "",
        "workspace_name": str(data.get("workspace_name") or "")[:80],
        "workspace_id": str(data.get("workspace_id") or "")[:120],
        "seat_quantity": min(999, max(2, int(data.get("seat_quantity") or 5))),
        "price_interval": "year" if data.get("price_interval") == "year" else "month",
        "credit_quantity": min(100000, max(1, int(data.get("credit_quantity") or 13))),
        "ideal_bank": str(data.get("ideal_bank") or "")[:40] if link_type == "ideal" else "",
        "pix_tax_id": raw_pix_tax_id,
        "pix_tax_id_auto": link_type == "pix" and not raw_pix_tax_id,
        "pix_auto_kind": str(data.get("pix_auto_kind") or "cpf").lower()
            if str(data.get("pix_auto_kind") or "cpf").lower() in {"mixed", "cpf", "cnpj"} else "cpf",
        "pix_identity": pix_identity,
        "retry_count": retry_count,
        "paired_proxy_rotation": bool(data.get("paired_proxy_rotation", False)),
        "use_sen": data.get("use_sen", True) is not False,
        "use_so": data.get("use_so", True) is not False,
        "dynamic_proxy_api": dynamic_proxy_api,
        "allow_missing_customer_session": bool(data.get("allow_missing_customer_session")) and internal_request,
        "entry_proxy_country": str(
            data.get("entry_proxy_country")
            or (str(data.get("promo_country") or "VN") if link_type == "kakao" else (
                "US" if link_type in {"gcash", "ph_short"} and country == "PH" else country
            ))
        ).upper(),
        "exit_proxy_country": str(
            data.get("exit_proxy_country")
            or (country if link_type == "kakao" else (
                str(data.get("promo_country") or "VN") if link_type == "gcash" else (
                    str(data.get("promo_country") or ("TR" if country == "PH" else country))
                    if link_type == "ph_short" and bool(data.get("use_promo", True)) else country
                )
            ))
        ).upper(),
        "proxy_session_time": min(120, max(1, int(data.get("proxy_session_time") or 10))),
    }
    if link_type == "paypal_oaics":
        options["proxy_country"] = paypal_proxy_country
        options["billing_country"] = paypal_billing_country
    if link_type == "ph_short":
        if country == "PH" and options["entry_proxy_country"] == "PH":
            options["entry_proxy_country"] = "US"
        if not options.get("use_promo"):
            options["exit_proxy_country"] = options["entry_proxy_country"]
        elif not str(data.get("exit_proxy_country") or "").strip() and not str(data.get("promo_country") or "").strip():
            options["exit_proxy_country"] = "TR" if country == "PH" else country
    if not options["token_raw"].strip():
        return jsonify({"error": "请填写 Access Token 或 Session JSON"}), 400
    if link_type == "pix" and options["pix_tax_id"] and len(options["pix_tax_id"]) not in {11, 14}:
        return jsonify({"error": "PIX 需要填写 11 位 CPF 或 14 位 CNPJ"}), 400
    job_id = STORE.create(options, internal=internal_request)
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "queue_position": STORE.queue_position(job_id),
        "global_rpm": 0,
        "ip_rpm": 0,
        "internal": internal_request,
    }), 202


@app.get("/api/checkout-progress")
def checkout_progress():
    job_id = str(request.args.get("job_id") or "")
    job = STORE.get(job_id, public=True)
    if not job:
        job = rust_job_public_snapshot(job_id)
    if not job:
        if LEGACY_SERVICE_BASE:
            try:
                legacy = requests.get(
                    f"{LEGACY_SERVICE_BASE}/api/checkout-progress",
                    params={"job_id": str(request.args.get("job_id") or "")},
                    timeout=8,
                )
                return app.response_class(
                    response=legacy.content,
                    status=legacy.status_code,
                    content_type=legacy.headers.get("content-type", "application/json"),
                )
            except Exception:
                pass
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job)


@app.post("/api/checkout-cancel")
def checkout_cancel():
    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id") or "")
    ok = STORE.cancel(job_id)
    if not ok:
        alias = get_rust_job_alias(job_id)
        rust_base = str(os.getenv("PAY153_RUST_URL") or "").strip().rstrip("/")
        rust_job_id = str((alias or {}).get("rust_job_id") or "")
        if rust_base and rust_job_id:
            try:
                response = requests.post(
                    f"{rust_base}/api/v1/jobs/{rust_job_id}/cancel",
                    timeout=8,
                )
                ok = response.status_code in {200, 202}
            except Exception:
                ok = False
    if not ok and LEGACY_SERVICE_BASE:
        try:
            legacy = requests.post(
                f"{LEGACY_SERVICE_BASE}/api/checkout-cancel",
                json={"job_id": job_id},
                timeout=8,
            )
            return app.response_class(
                response=legacy.content,
                status=legacy.status_code,
                content_type=legacy.headers.get("content-type", "application/json"),
            )
        except Exception:
            pass
    return jsonify({"ok": ok}), 200 if ok else 404


if __name__ == "__main__":
    app.run(host=os.getenv("PAY153_HOST", "127.0.0.1"), port=int(os.getenv("PAY153_PORT", "18082")), threaded=True)
