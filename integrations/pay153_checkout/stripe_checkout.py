"""纯协议 Stripe Hosted Checkout（无浏览器）。

把 ChatGPT 的 ``cs_live`` checkout session 通过 Stripe HTTP API 推进到「选择 PayPal
作为 funding source」并 confirm，拿到 Stripe→PayPal 的跳转地址（``pm-redirects.stripe.com
/authorize/...`` → ``paypal.com/agreements/approve?ba_token=BA-...``）。

移植自 Gpt-Agreement-Payment/CTF-pay/card/_monolith.py 的 Stripe 部分：
  fetch_publishable_key / init_checkout / fetch_elements_session /
  create_paypal_payment_method / confirm_payment / poll_result

全程用 curl_cffi（Firefox 144 TLS 指纹）走支付代理；api.stripe.com 不在 Cloudflare 挑战后，
纯 HTTP 即可。ChatGPT 下单（/backend-api/payments/checkout）也尝试纯 HTTP（带 Bearer）。
"""

from __future__ import annotations

import json
from pathlib import Path
import random
import re
import string
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION_BASE = "2025-03-31.basil"
STRIPE_VERSION_FULL = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"
PAYPAL_STRIPE_VERSION = (
    "2020-08-27;custom_checkout_beta=v1; "
    "checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
)


def _load_paypal_fingerprint() -> dict:
    path = Path(__file__).with_name("paypal_fingerprint.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _paypal_return_url(session_id: str, init_resp: dict, ctx: dict) -> str:
    """Build the same custom-checkout return URL used by Stripe.js for PayPal."""
    hosted = str(ctx.get("stripe_hosted_url") or init_resp.get("stripe_hosted_url") or "").strip()
    if hosted.startswith("https://pay.openai.com"):
        hosted = "https://checkout.stripe.com" + hosted[len("https://pay.openai.com"):]
    if not hosted:
        hosted = f"https://checkout.stripe.com/c/pay/{session_id}"
    parsed = urlsplit(hosted)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["redirect_pm_type"] = "paypal"
    query["lid"] = str(uuid.uuid4())
    query["ui_mode"] = "custom"
    return urlunsplit((parsed.scheme or "https", parsed.netloc or "checkout.stripe.com", parsed.path, urlencode(query), parsed.fragment))

FIREFOX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) "
    "Gecko/20100101 Firefox/144.0"
)
# Compatibility alias used by the existing provider modules.
CHROME_UA = FIREFOX_UA

OPENAI_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"

# In-memory only: native Checkout passes this secret to Stripe Elements so
# methods saved on the Checkout Customer can be redisplayed.
CHECKOUT_CUSTOMER_SESSION_SECRETS: dict[str, str] = {}

# OpenAI / OpenAI LLC 的 Stripe publishable key（账户分片 → pk）。
KNOWN_PUBLISHABLE_KEYS = {
    "1Pj377KslHRdbaPg": "pk_live_51Pj377KslHRdbaPgTJYjThzH3f5dt1N1vK7LUp0qh0yNSarhfZ6nfbG7FFlh8KLxVkvdMWN5o6Mc4Vda6NHaSnaV00C2Sbl8Zs",
    "1HOrSwC6h1nxGoI3": "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
}

LOCALE_PROFILES = {
    "US": {"browser_locale": "en-US", "browser_timezone": "America/Chicago", "browser_language": "en-US"},
    "JP": {"browser_locale": "ja-JP", "browser_timezone": "Asia/Tokyo", "browser_language": "ja-JP"},
    "GB": {"browser_locale": "en-GB", "browser_timezone": "Europe/London", "browser_language": "en-GB"},
    "DE": {"browser_locale": "de-DE", "browser_timezone": "Europe/Berlin", "browser_language": "de-DE"},
    "FR": {"browser_locale": "fr-FR", "browser_timezone": "Europe/Paris", "browser_language": "fr-FR"},
    "NL": {"browser_locale": "nl-NL", "browser_timezone": "Europe/Amsterdam", "browser_language": "nl-NL"},
    "CH": {"browser_locale": "de-CH", "browser_timezone": "Europe/Zurich", "browser_language": "de-CH"},
    "IN": {"browser_locale": "en-IN", "browser_timezone": "Asia/Kolkata", "browser_language": "en-IN"},
    "BR": {"browser_locale": "pt-BR", "browser_timezone": "America/Sao_Paulo", "browser_language": "pt-BR"},
    "VN": {"browser_locale": "vi-VN", "browser_timezone": "Asia/Ho_Chi_Minh", "browser_language": "vi-VN"},
    "AU": {"browser_locale": "en-AU", "browser_timezone": "Australia/Sydney", "browser_language": "en-AU"},
    "CA": {"browser_locale": "en-CA", "browser_timezone": "America/Toronto", "browser_language": "en-CA"},
}

# PayPal 作为 funding source 只在特定地区/币种/processor_entity 下开放（EU→openai_ie）。
# 下单币种必须匹配地区，否则 checkout 只返回 card，confirm 会 payment_method_types_mismatch。
COUNTRY_CURRENCY = {
    "US": "USD", "GB": "GBP", "JP": "JPY", "CN": "CNY", "HK": "HKD",
    "TW": "TWD", "KR": "KRW", "IN": "INR", "BR": "BRL", "AU": "AUD",
    "CA": "CAD", "NZ": "NZD", "SG": "SGD", "MY": "MYR", "TH": "THB",
    "ID": "IDR", "PH": "PHP", "VN": "VND", "TR": "TRY", "IL": "ILS",
    "AE": "AED", "SA": "SAR", "QA": "QAR", "KW": "KWD", "BH": "BHD",
    "OM": "OMR", "ZA": "ZAR", "EG": "EGP", "NG": "NGN", "KE": "KES",
    "MX": "MXN", "AR": "ARS", "CL": "CLP", "CO": "COP", "PE": "PEN",
    "UY": "UYU", "PY": "PYG", "BO": "BOB", "CR": "CRC", "DO": "DOP",
    "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK", "PL": "PLN",
    "CZ": "CZK", "HU": "HUF", "RO": "RON", "BG": "BGN", "IS": "ISK",
    "RS": "RSD", "UA": "UAH", "GE": "GEL", "KZ": "KZT",
    "DE": "EUR", "FR": "EUR", "IE": "EUR", "NL": "EUR", "ES": "EUR",
    "IT": "EUR", "AT": "EUR", "BE": "EUR", "FI": "EUR", "PT": "EUR",
    "GR": "EUR", "LU": "EUR", "SK": "EUR", "SI": "EUR", "EE": "EUR",
    "LV": "EUR", "LT": "EUR", "CY": "EUR", "MT": "EUR", "HR": "EUR",
}

# 支持 PayPal 的下单地区（EU/EUR）。US/USD 只有 card。
PAYPAL_ORDER_COUNTRIES = ["US", "GB", "DE", "FR", "IE", "NL", "ES", "IT", "AT"]


def currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get((country or "US").upper(), "USD")


def _profile(country: str) -> dict:
    return LOCALE_PROFILES.get((country or "US").upper(), LOCALE_PROFILES["US"])


def _locale_short(profile: dict) -> str:
    return profile["browser_locale"].split("-")[0]


def _stripe_headers() -> dict:
    return {
        "User-Agent": CHROME_UA,
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }


def _gen_fingerprint() -> tuple[str, str, str]:
    def _id() -> str:
        return str(uuid.uuid4()).replace("-", "") + uuid.uuid4().hex[:6]

    return _id(), _id(), _id()


def _gen_elements_session_id() -> str:
    chars = string.ascii_letters + string.digits
    return "elements_session_" + "".join(random.choices(chars, k=11))


def _elements_options_client_payload() -> dict:
    return {
        # Match native Checkout so Customer-saved methods may be redisplayed.
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }


def _stripe_unknown_param(resp) -> str:
    try:
        payload = resp.json()
    except Exception:
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return ""
    if str(err.get("code") or "").lower() != "parameter_unknown":
        return ""
    return str(err.get("param") or "").strip()


def _extract_payment_method_types(payload: dict) -> list[str]:
    pmt = payload.get("payment_method_types")
    if isinstance(pmt, list) and pmt:
        return [pm for pm in pmt if isinstance(pm, str)]
    specs = payload.get("payment_method_specs")
    if isinstance(specs, list):
        out = [s["type"] for s in specs if isinstance(s, dict) and s.get("type")]
        if out:
            return out
    return ["card"]


def _paypal_setup_diagnostic(payload: dict) -> dict:
    """Return a compact, secret-free summary of PayPal setup/mandate fields."""
    if not isinstance(payload, dict):
        return {}

    summary: dict[str, Any] = {}
    for key in (
        "mode",
        "setup_future_usage",
        "setup_future_usage_for_payment_method_type",
        "payment_method_options",
        "subscription_data",
        "deferred_intent",
    ):
        value = payload.get(key)
        if value not in (None, {}, []):
            if isinstance(value, dict):
                summary[key] = {str(k): type(v).__name__ for k, v in value.items()}
            else:
                summary[key] = value

    specs = payload.get("payment_method_specs") or []
    if isinstance(specs, list):
        for spec in specs:
            if not isinstance(spec, dict) or str(spec.get("type") or "").lower() != "paypal":
                continue
            summary["paypal_spec_keys"] = sorted(str(k) for k in spec.keys())
            for key in (
                "setup_future_usage",
                "payment_method_options",
                "mandate_options",
                "requirements",
            ):
                value = spec.get(key)
                if value not in (None, {}, []):
                    summary[f"paypal_{key}"] = (
                        sorted(str(k) for k in value.keys()) if isinstance(value, dict) else value
                    )
            break
    return summary


def build_http(proxy: Optional[str]):
    """curl_cffi Session（Firefox 144 TLS 指纹），跟随支付代理。"""
    from curl_cffi.requests import Session as CffiSession

    http = CffiSession(impersonate="firefox144")
    try:
        http.trust_env = False
    except Exception:
        pass
    if proxy:
        try:
            http.proxies = {"http": proxy, "https": proxy}
        except Exception:
            pass
    return http


# ---------------------------------------------------------------------------
# ChatGPT 下单（纯 HTTP）
# ---------------------------------------------------------------------------

def create_chatgpt_order(
    http,
    access_token: str,
    *,
    country: str = "US",
    currency: Optional[str] = None,
    with_promo: bool = True,
    log: Callable[[str], None] = lambda m: None,
) -> tuple[Optional[str], Optional[str]]:
    """POST /backend-api/payments/checkout 创建订单，返回 (cs_live_session_id, error)。

    ``currency`` 缺省时按 ``country`` 推导；PayPal funding 只在币种匹配地区时开放。
    """
    currency = (currency or currency_for_country(country)).upper()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": CHROME_UA,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "OAI-Language": "en-US",
    }
    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country or "US", "currency": currency},
        "checkout_ui_mode": "custom",
        "check_card_proxy": True,
    }
    if with_promo:
        body["promo_campaign"] = {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }

    # 暖身：拿 __cf_bm / _cfuvid，提高 /backend-api 通过率
    try:
        http.get("https://chatgpt.com/", headers={"User-Agent": CHROME_UA,
                 "Accept-Language": "en-US,en;q=0.9"}, timeout=30)
    except Exception as e:
        log(f"[order] 暖身 chatgpt.com 失败（忽略）: {e}")

    try:
        resp = http.post(OPENAI_CHECKOUT_URL, json=body, headers=headers, timeout=30)
    except Exception as e:
        return None, f"request_error: {type(e).__name__}: {e}"

    status = getattr(resp, "status_code", 0)
    text = getattr(resp, "text", "") or ""
    if status != 200:
        return None, f"HTTP {status}: {text[:300]}"
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return None, f"invalid_json: {text[:200]}"
    sid = data.get("checkout_session_id")
    if not sid:
        mm = re.search(r"cs_live_[A-Za-z0-9]+", text)
        sid = mm.group(0) if mm else None
    if sid:
        customer_session_secret = str(
            data.get("customer_session_client_secret") or ""
        ).strip()
        if customer_session_secret.startswith("cuss_secret_"):
            CHECKOUT_CUSTOMER_SESSION_SECRETS[str(sid)] = customer_session_secret
            log("[order] CustomerSession ?????????????????")
        return sid, None
    return None, f"no_session_id: {text[:200]}"


def create_order_with_fallback(
    http,
    access_token: str,
    *,
    country: str = "US",
    currency: Optional[str] = None,
    with_promo: bool = True,
    log: Callable[[str], None] = lambda m: None,
) -> tuple[Optional[str], bool, Optional[str]]:
    """返回 (session_id, used_promo, error)。带 promo 失败且为资格类错误时回退无 promo。"""
    sid, err = create_chatgpt_order(http, access_token, country=country, currency=currency, with_promo=with_promo, log=log)
    if sid:
        return sid, with_promo, None
    low = (err or "").lower()
    if with_promo and any(k in low for k in ("already paid", "promo", "coupon", "not_eligible", "offer not found")):
        log(f"[order] 带 promo 失败（{err[:80]}），回退无 promo 重试")
        sid2, err2 = create_chatgpt_order(http, access_token, country=country, currency=currency, with_promo=False, log=log)
        if sid2:
            return sid2, False, None
        return None, False, err2 or err
    return None, with_promo, err


# ---------------------------------------------------------------------------
# Stripe Hosted Checkout（纯 HTTP）
# ---------------------------------------------------------------------------

def verify_pk(http, session_id: str, log: Callable[[str], None]) -> str:
    """用已知 OpenAI Stripe pk 探测 /init，返回有效 pk。

    代理偶发 TLS 握手错误(curl 35 / OPENSSL_internal)时对每个 pk 做几次重试；
    若探测整体因网络抖动失败，回退到首个已知 pk 让下游继续（下游会再校验）。
    """
    last = ""
    tls_flaky = False
    keys = list(KNOWN_PUBLISHABLE_KEYS.values())
    for pk in keys:
        for attempt in range(3):
            try:
                resp = http.post(
                    f"{STRIPE_API}/v1/payment_pages/{session_id}/init",
                    data={"key": pk, "_stripe_version": STRIPE_VERSION_BASE, "browser_locale": "en-US"},
                    headers=_stripe_headers(),
                    timeout=15,
                )
                if getattr(resp, "status_code", 0) == 200:
                    log(f"[stripe] publishable_key OK: {pk[:24]}…")
                    return pk
                last = f"{getattr(resp, 'status_code', '?')}: {(getattr(resp, 'text', '') or '')[:120]}"
                break  # 非网络错误（如 400）无需重试该 pk
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                low = str(e).lower()
                if any(k in low for k in ("tls", "ssl", "curl: (35)", "curl: (56)", "curl: (28)", "timed out", "connection")):
                    tls_flaky = True
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break  # 非网络错误，换下一个 pk
    if tls_flaky and keys:
        log(f"[stripe] pk 探测网络抖动，回退首个已知 pk（last={last[:80]}）")
        return keys[0]
    raise RuntimeError(f"无法确认 Stripe publishable_key（{last}）")


def init_checkout(http, session_id: str, pk: str, profile: dict, log) -> tuple[dict, str, dict]:
    if not re.fullmatch(r"cs_(?:live|test)_[A-Za-z0-9]+", str(session_id or "")):
        raise RuntimeError(f"Stripe init requires a resolved cs_* session, got {session_id or '-'}")
    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/init"
    stripe_js_id = str(uuid.uuid4())
    elements_session_id = _gen_elements_session_id()
    elements_options = _elements_options_client_payload()

    for version in (STRIPE_VERSION_BASE, STRIPE_VERSION_FULL):
        data = {
            "browser_locale": profile["browser_locale"],
            "browser_timezone": profile["browser_timezone"],
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": stripe_js_id,
            "elements_session_client[locale]": profile["browser_locale"],
            "elements_session_client[is_aggregation_expected]": "false",
            "key": pk,
            "_stripe_version": version,
        }
        data.update(elements_options)
        if version == STRIPE_VERSION_FULL:
            data["elements_session_client[client_betas][0]"] = "custom_checkout_server_updates_1"
            data["elements_session_client[client_betas][1]"] = "custom_checkout_manual_approval_1"

        resp = http.post(url, data=data, headers=_stripe_headers(), timeout=30)
        dropped: set[str] = set()
        while getattr(resp, "status_code", 0) == 400:
            param = _stripe_unknown_param(resp)
            if not param or param not in data or param in dropped:
                break
            dropped.add(param)
            data.pop(param, None)
            elements_options.pop(param, None)
            resp = http.post(url, data=data, headers=_stripe_headers(), timeout=30)

        sc = getattr(resp, "status_code", 0)
        if sc == 200:
            init_data = resp.json()
            total = init_data.get("total_summary") or {}
            amount = total.get("due")
            if amount is None:
                amount = (init_data.get("invoice") or {}).get("amount_due")
            checkout_customer = init_data.get("customer") or {}
            if not isinstance(checkout_customer, dict):
                checkout_customer = {}
            visible_saved_methods = checkout_customer.get("payment_methods") or []
            if not isinstance(visible_saved_methods, list):
                visible_saved_methods = []
            ctx = {
                "stripe_js_id": stripe_js_id,
                "elements_session_id": elements_session_id,
                "elements_options_client": dict(elements_options),
                "browser_locale": profile["browser_locale"],
                "locale": init_data.get("locale") or _locale_short(profile),
                "currency": (init_data.get("currency") or "usd").lower(),
                "checkout_amount": amount,
                "payment_method_types": _extract_payment_method_types(init_data),
                "config_id": init_data.get("config_id", ""),
                "init_checksum": init_data.get("init_checksum", ""),
                "return_url": init_data.get("return_url") or "",
                "stripe_hosted_url": init_data.get("stripe_hosted_url") or "",
                "customer_has_fallback_payment_method": bool(
                    checkout_customer.get("has_fallback_payment_method")
                ),
                "visible_saved_payment_method_count": len(visible_saved_methods),
            }
            log(f"[stripe] init ok version={version[:24]} amount={amount} currency={ctx['currency']} pm={ctx['payment_method_types']}")
            log(
                "[stripe] saved methods "
                f"fallback={ctx['customer_has_fallback_payment_method']} "
                f"visible={ctx['visible_saved_payment_method_count']}"
            )
            if str(amount).strip() in {"0", "0.0", "0.00"}:
                # Keep the fields that decide whether Checkout is confirming a
                # PaymentIntent or a SetupIntent.  These are deliberately
                # compact and contain no client secret / publishable key.
                debug_fields = {}
                for key_name in (
                    "mode",
                    "setup_future_usage_for_payment_method_type",
                    "payment_method_options",
                    "setup_intent",
                    "payment_intent",
                    "subscription_data",
                    "customer",
                ):
                    value = init_data.get(key_name)
                    if value not in (None, "", {}, []):
                        debug_fields[key_name] = value
                if debug_fields:
                    raw = json.dumps(debug_fields, ensure_ascii=False, separators=(",", ":"))
                    raw = re.sub(r'("(?:client_secret|publishable_key|key)"\s*:\s*")[^"]+', r'\1***', raw)
                    log(f"[stripe] zero-due intent fields: {raw[:1800]}")
            return init_data, version, ctx
        if sc == 400 and "beta" in (getattr(resp, "text", "") or "").lower():
            continue
        raise RuntimeError(f"init 失败 [{sc}]: {(getattr(resp, 'text', '') or '')[:300]}")
    raise RuntimeError("init 失败：所有 Stripe 版本均不可用")


def fetch_elements_session(http, pk: str, session_id: str, ctx: dict, version: str, profile: dict, log) -> dict:
    locale_short = ctx.get("locale") or _locale_short(profile)
    currency = (ctx.get("currency") or "usd").lower()
    amount = ctx.get("checkout_amount")
    amount = 0 if amount is None else int(amount)
    pmt = ctx.get("payment_method_types") or ["card"]
    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": currency,
        "deferred_intent[setup_future_usage]": "off_session",
        "currency": currency,
        "key": pk,
        "_stripe_version": version,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": ctx.get("stripe_js_id", str(uuid.uuid4())),
        "locale": locale_short,
        "type": "deferred_intent",
        "checkout_session_id": session_id,
    }
    customer_session_secret = CHECKOUT_CUSTOMER_SESSION_SECRETS.get(session_id, "")
    if customer_session_secret:
        params["customer_session_client_secret"] = customer_session_secret
    for idx, pm in enumerate(pmt):
        params[f"deferred_intent[payment_method_types][{idx}]"] = pm
    try:
        resp = http.get(f"{STRIPE_API}/v1/elements/sessions", params=params, headers=_stripe_headers(), timeout=30)
    except Exception as e:
        log(f"[stripe] elements/sessions 异常（忽略）: {e}")
        return {}
    if getattr(resp, "status_code", 0) != 200:
        log(f"[stripe] elements/sessions [{getattr(resp, 'status_code', '?')}]（忽略，用本地 id）")
        return {}
    data = resp.json()
    real = data.get("session_id") or data.get("id")
    if real:
        ctx["elements_session_id"] = real
    if data.get("config_id"):
        ctx["elements_session_config_id"] = data["config_id"]
    types = [s["type"] for s in data.get("payment_method_specs", []) if isinstance(s, dict) and s.get("type")]
    if types:
        ctx["payment_method_types"] = types
    diagnostic = _paypal_setup_diagnostic(data)
    ctx["paypal_elements_diagnostic"] = diagnostic
    elements_customer = data.get("customer") or data.get("legacy_customer") or {}
    if not isinstance(elements_customer, dict):
        elements_customer = {}
    saved_methods = elements_customer.get("payment_methods") or []
    if not isinstance(saved_methods, list):
        saved_methods = []
    ctx["customer_session_present"] = bool(customer_session_secret)
    ctx["elements_saved_payment_method_count"] = len(saved_methods)
    log(
        "[stripe] CustomerSession "
        f"present={ctx['customer_session_present']} "
        f"saved_methods={ctx['elements_saved_payment_method_count']}"
    )
    if "paypal" in types or "paypal" in pmt:
        log(
            "[stripe] PayPal Elements setup diagnostic: "
            + json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":"))[:1600]
        )
    return data


def _entity_from_return_url(url: str) -> str:
    m = re.search(r"processor_entity=([A-Za-z_]+)", url or "")
    return m.group(1) if m else ""


def update_tax_region(http, session_id: str, pk: str, version: str, ctx: dict, billing: dict, profile: dict, log) -> dict:
    """POST /v1/payment_pages/{cs} 更新买家地址（tax_region），否则税费停留在
    ``requires_location_inputs``，confirm 后拿不到 PayPal 跳转。"""
    addr = billing.get("address", {}) or {}
    data = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": ctx.get("stripe_js_id", str(uuid.uuid4())),
        "elements_session_client[session_id]": ctx.get("elements_session_id", _gen_elements_session_id()),
        "elements_session_client[locale]": ctx.get("locale") or _locale_short(profile),
        "elements_session_client[is_aggregation_expected]": "false",
        "key": pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    # 只带非空的地址字段，避免 parameter_invalid_empty
    for field, value in (
        ("country", addr.get("country", "")),
        ("line1", addr.get("line1", "")),
        ("city", addr.get("city", "")),
        ("postal_code", addr.get("postal_code", "")),
        ("state", addr.get("state", "")),
    ):
        if value:
            data[f"tax_region[{field}]"] = value
    data.update(ctx.get("elements_options_client") or _elements_options_client_payload())

    try:
        resp = http.post(f"{STRIPE_API}/v1/payment_pages/{session_id}", data=data, headers=_stripe_headers(), timeout=30)
    except Exception as e:  # noqa: BLE001
        log(f"[stripe] tax_region 更新异常（忽略）: {e}")
        return {}
    sc = getattr(resp, "status_code", 0)
    if sc != 200:
        log(f"[stripe] tax_region 更新 [{sc}]（忽略）: {(getattr(resp, 'text', '') or '')[:160]}")
        return {}
    try:
        data_resp = resp.json()
    except Exception:
        return {}
    total = data_resp.get("total_summary") or {}
    amount = total.get("total")
    if amount is None:
        amount = total.get("due")
    if amount is not None:
        ctx["checkout_amount"] = amount
    tax = data_resp.get("tax") or {}
    log(f"[stripe] tax_region 更新 ok amount={ctx.get('checkout_amount')} tax_status={tax.get('status')}")
    return data_resp


def snapshot_billing(chatgpt_http, access_token: str, session_id: str, processor_entity: str, billing: dict, log) -> None:
    """confirm 前把账单地址快照给 ChatGPT 后端（manual_approval 流程的一环）。best-effort。"""
    if chatgpt_http is None or not access_token:
        return
    addr = billing.get("address", {}) or {}
    payload = {
        "snapshot": {
            "billing_address": {
                "name": billing.get("name", ""),
                "address": {
                    "line1": addr.get("line1", ""),
                    "city": addr.get("city", ""),
                    "country": addr.get("country", "US"),
                    "postal_code": addr.get("postal_code", ""),
                },
            }
        }
    }
    if addr.get("state"):
        payload["snapshot"]["billing_address"]["address"]["state"] = addr["state"]
    headers = {
        "content-type": "application/json",
        "accept": "*/*",
        "authorization": f"Bearer {access_token}",
        "origin": "https://chatgpt.com",
        "referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
        "User-Agent": CHROME_UA,
        "OAI-Language": "en-US",
    }
    try:
        resp = chatgpt_http.post(
            "https://chatgpt.com/backend-api/payments/checkout/snapshot",
            json=payload, headers=headers, timeout=20,
        )
        log(f"[stripe] snapshot billing: {getattr(resp, 'status_code', '?')}")
    except Exception as e:  # noqa: BLE001
        log(f"[stripe] snapshot billing 异常（忽略）: {e}")


def create_paypal_payment_method(http, pk: str, billing: dict, session_id: str, version: str, ctx: dict, log) -> str:
    guid, muid, sid = _gen_fingerprint()
    guid = ctx.get("guid") or guid
    muid = ctx.get("muid") or muid
    sid = ctx.get("sid") or sid
    ctx["guid"], ctx["muid"], ctx["sid"] = guid, muid, sid
    addr = billing.get("address", {}) or {}
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))
    elements_session_id = ctx.get("elements_session_id", _gen_elements_session_id())
    elements_session_config_id = ctx.get("elements_session_config_id") or str(uuid.uuid4())
    checkout_config_id = ctx.get("config_id") or ""
    runtime_version = ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION

    data = {
        "type": "paypal",
        "billing_details[name]": billing.get("name", ""),
        "billing_details[email]": billing.get("email", ""),
        "billing_details[address][country]": addr.get("country", "US"),
        "billing_details[address][line1]": addr.get("line1", ""),
        "billing_details[address][city]": addr.get("city", ""),
        "billing_details[address][postal_code]": addr.get("postal_code", ""),
        "billing_details[address][state]": addr.get("state", ""),
        "payment_user_agent": (
            f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
            "payment-element; deferred-intent"
        ),
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": session_id,
        "client_attribution_metadata[checkout_config_id]": checkout_config_id,
        "client_attribution_metadata[elements_session_id]": elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": elements_session_config_id,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": pk,
        "_stripe_version": STRIPE_VERSION_BASE,
    }
    resp = http.post(f"{STRIPE_API}/v1/payment_methods", data=data, headers=_stripe_headers(), timeout=30)
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"创建 PayPal payment_method 失败 [{getattr(resp, 'status_code', '?')}]: {(getattr(resp, 'text', '') or '')[:300]}")
    pm_id = resp.json()["id"]
    log(f"[stripe] paypal payment_method: {pm_id}")
    return pm_id


def confirm_payment(
    http,
    pk: str,
    session_id: str,
    pm_id: str,
    init_resp: dict,
    version: str,
    ctx: dict,
    profile: dict,
    log,
    *,
    inline_payment_method: bool = True,
) -> dict:
    guid, muid, sid = ctx.get("guid"), ctx.get("muid"), ctx.get("sid")
    if not (guid and muid and sid):
        guid, muid, sid = _gen_fingerprint()
    runtime_version = ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION
    locale_short = ctx.get("locale") or _locale_short(profile)
    top_checkout_config_id = ctx.get("config_id", "")
    elements_session_config_id = ctx.get("elements_session_config_id") or str(uuid.uuid4())
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))
    elements_session_id = ctx.get("elements_session_id", _gen_elements_session_id())
    init_checksum = init_resp.get("init_checksum", "")

    total = init_resp.get("total_summary") or {}
    expected_amount = "0"
    if ctx.get("checkout_amount") is not None:
        # tax_region 更新后的实付总额（含税），必须与服务端一致，否则停在 requires_approval
        expected_amount = str(ctx["checkout_amount"])
    elif total.get("due") is not None:
        expected_amount = str(total["due"])
    elif (init_resp.get("invoice") or {}).get("amount_due") is not None:
        expected_amount = str(init_resp["invoice"]["amount_due"])

    checkout_url = _paypal_return_url(session_id, init_resp, ctx)

    data = {
        "guid": guid, "muid": muid, "sid": sid,
        "expected_amount": expected_amount,
        "expected_payment_method_type": "paypal",
        "key": pk,
        "_stripe_version": STRIPE_VERSION_FULL,
        "init_checksum": init_checksum,
        "version": runtime_version,
        "return_url": checkout_url,
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": locale_short,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[session_id]": elements_session_id,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": session_id,
        "client_attribution_metadata[checkout_config_id]": top_checkout_config_id,
        "client_attribution_metadata[elements_session_id]": elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": elements_session_config_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
    }
    consent_collection = init_resp.get("consent_collection", {}) or {}
    if consent_collection.get("terms_of_service") not in (None, "", "none"):
        data["consent[terms_of_service]"] = "accepted"
    data.update(ctx.get("elements_options_client") or _elements_options_client_payload())

    billing = ctx.get("billing") or {}
    addr = billing.get("address", {}) or {}
    if inline_payment_method:
        # 第一次 confirm 必须 inline，Stripe 才会创建真正绑定本次
        # submission 的 PayPal PaymentMethod。
        pm_meta = {
        "payment_method_data[type]": "paypal",
        "payment_method_data[billing_details][name]": billing.get("name", ""),
        "payment_method_data[billing_details][email]": billing.get("email", ""),
        "payment_method_data[billing_details][address][country]": addr.get("country", "US"),
        "payment_method_data[billing_details][address][line1]": addr.get("line1", ""),
        "payment_method_data[billing_details][address][city]": addr.get("city", ""),
        "payment_method_data[billing_details][address][postal_code]": addr.get("postal_code", ""),
        "payment_method_data[client_attribution_metadata][client_session_id]": stripe_js_id,
        "payment_method_data[client_attribution_metadata][checkout_session_id]": session_id,
        "payment_method_data[client_attribution_metadata][checkout_config_id]": top_checkout_config_id,
        "payment_method_data[client_attribution_metadata][elements_session_id]": elements_session_id,
        "payment_method_data[client_attribution_metadata][elements_session_config_id]": elements_session_config_id,
        "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
        "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
        "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
        "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
        "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
        "payment_method_data[payment_user_agent]": (
            f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
            "payment-element; deferred-intent"
        ),
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(random.randint(25000, 55000)),
        }
        if addr.get("state"):
            pm_meta["payment_method_data[billing_details][address][state]"] = addr["state"]
        data.update(pm_meta)
    else:
        # Stripe Hosted Checkout 浏览器的 PayPal confirm 是精简的 23 字段
        # 结构；额外塞入 Elements/deferred-intent 字段会让 PayPal setup 在
        # merchant approve 后落入 generic_decline。
        fingerprint = _load_paypal_fingerprint()
        client_session_id = ctx.get("client_session_id") or str(uuid.uuid4())
        ctx["client_session_id"] = client_session_id
        runtime_version = fingerprint.get("version") or runtime_version
        data = {
            "eid": "NA",
            "payment_method": pm_id,
            "expected_amount": expected_amount,
            "expected_payment_method_type": "paypal",
            "return_url": checkout_url,
            "_stripe_version": fingerprint.get("_stripe_version") or PAYPAL_STRIPE_VERSION,
            "guid": guid,
            "muid": muid,
            "sid": sid,
            "key": pk,
            "version": runtime_version,
            "init_checksum": init_checksum,
            "client_attribution_metadata[client_session_id]": client_session_id,
            "client_attribution_metadata[checkout_session_id]": session_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[checkout_config_id]": top_checkout_config_id,
            "link_brand": "link",
        }
        for key in (
            "js_checksum", "passive_captcha_token", "passive_captcha_ekey", "rv_timestamp",
        ):
            if key in fingerprint:
                data[key] = str(fingerprint.get(key) or "")

    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/confirm"
    resp = http.post(url, data=data, headers=_stripe_headers(), timeout=30)
    if (
        getattr(resp, "status_code", 0) == 400
        and "consent[terms_of_service]" not in data
        and "terms of service" in (getattr(resp, "text", "") or "").lower()
    ):
        data["consent[terms_of_service]"] = "accepted"
        resp = http.post(url, data=data, headers=_stripe_headers(), timeout=30)
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"confirm 失败 [{getattr(resp, 'status_code', '?')}]: {(getattr(resp, 'text', '') or '')[:400]}")
    return resp.json()


def _find_next_action(confirm_data: dict) -> dict:
    na = confirm_data.get("next_action")
    if isinstance(na, dict) and na:
        return na
    action = confirm_data.get("action")
    if isinstance(action, dict) and action:
        return action
    elements_session = confirm_data.get("elements_session") or {}
    if isinstance(elements_session, dict):
        action = elements_session.get("action") or elements_session.get("next_action")
        if isinstance(action, dict) and action:
            return action
    submission = confirm_data.get("submission_attempt") or {}
    if isinstance(submission, dict):
        action = submission.get("next_action") or submission.get("action")
        if isinstance(action, dict) and action:
            return action
    for key in ("payment_intent", "setup_intent"):
        obj = confirm_data.get(key)
        if isinstance(obj, dict):
            na = obj.get("next_action")
            if isinstance(na, dict) and na:
                return na
    pmo = confirm_data.get("payment_method_object")
    if isinstance(pmo, dict):
        si = pmo.get("setup_intent")
        if isinstance(si, dict):
            na = si.get("next_action")
            if isinstance(na, dict) and na:
                return na
    # Stripe occasionally nests the action one level deeper after a Payment
    # Page server update.  Search for a non-empty next_action/action object but
    # avoid returning unrelated scalar metadata.
    def walk(value, depth=0):
        if depth > 7:
            return {}
        if isinstance(value, dict):
            for key in ("next_action", "action"):
                item = value.get(key)
                if isinstance(item, dict) and item:
                    return item
            for item in value.values():
                found = walk(item, depth + 1)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item, depth + 1)
                if found:
                    return found
        return {}

    return walk(confirm_data)


def _find_payment_method_id(payload: dict) -> str:
    """Find the pm_* created by the first inline PayPal confirm."""
    preferred = [
        payload.get("payment_method"),
        (payload.get("submission_attempt") or {}).get("payment_method"),
        (payload.get("setup_intent") or {}).get("payment_method"),
        (payload.get("payment_intent") or {}).get("payment_method"),
        (payload.get("payment_method_object") or {}).get("id"),
    ]
    for value in preferred:
        if isinstance(value, dict):
            value = value.get("id")
        value = str(value or "")
        if value.startswith("pm_"):
            return value

    def walk(value, depth=0):
        if depth > 8:
            return ""
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"payment_method", "payment_method_id", "id"}:
                    candidate = item.get("id") if isinstance(item, dict) else item
                    candidate = str(candidate or "")
                    if candidate.startswith("pm_"):
                        return candidate
            for item in value.values():
                found = walk(item, depth + 1)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item, depth + 1)
                if found:
                    return found
        return ""

    return walk(payload)


def extract_redirect_url(confirm_data: dict) -> str:
    na = _find_next_action(confirm_data)
    rt = na.get("redirect_to_url") or {}
    url = str(rt.get("url") or "")
    if url:
        return url
    # 兜底：整个 confirm JSON 里正则找 PayPal/pm-redirects 跳转地址
    raw = json.dumps(confirm_data)
    m = re.search(r"https?://pm-redirects\.stripe\.com/authorize/[^\s\"'<>\\]+", raw)
    if m:
        return m.group(0).replace("\\u0026", "&").replace("\\/", "/")
    m = re.search(r"https?://(?:www\.)?paypal\.com/agreements/approve\?[^\s\"'<>\\]+", raw)
    if m:
        return m.group(0).replace("\\u0026", "&").replace("\\/", "/")
    return ""


def approve_submission(chatgpt_http, access_token: str, session_id: str, processor_entity: str, log) -> dict:
    """manual_approval beta：调 ChatGPT /backend-api/payments/checkout/approve 批准提交。"""
    headers = {
        "content-type": "application/json",
        "accept": "*/*",
        "authorization": f"Bearer {access_token}",
        "origin": "https://chatgpt.com",
        "referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
        "x-openai-target-path": "/backend-api/payments/checkout/approve",
        "x-openai-target-route": "/backend-api/payments/checkout/approve",
        "User-Agent": CHROME_UA,
        "OAI-Language": "en-US",
    }
    body = {"checkout_session_id": session_id, "processor_entity": processor_entity}
    ar = chatgpt_http.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json=body, headers=headers, timeout=20,
    )
    sc_code = getattr(ar, "status_code", 0)
    text = getattr(ar, "text", "") or ""
    log(f"[stripe] manual_approval approve: {sc_code} {text[:160]}")
    if sc_code != 200:
        raise RuntimeError(f"ChatGPT approve 失败: {sc_code} {text[:200]}")
    try:
        payload = ar.json() or {}
    except Exception:
        payload = {}
    result = str(payload.get("result") or "").lower()
    if result and result != "approved":
        raise RuntimeError(f"manual_approval approve blocked: result={result}")
    return payload


def poll_paypal_redirect_light(
    http,
    pk: str,
    session_id: str,
    log,
    *,
    max_attempts: int = 1,
    stage: str = "pre-approve",
) -> str:
    """Read Stripe's compact /poll view without creating a new submission.

    Some zero-amount PayPal SetupIntents expose the buyer authorization URL
    before merchant approval.  Keep this probe short: if the first submission
    has no redirect, the outer provider loop should rebuild the Checkout rather
    than repeatedly polling a stale submission.
    """
    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/poll"
    params = {"key": pk, "_stripe_version": STRIPE_VERSION_BASE}
    for attempt in range(max(1, max_attempts)):
        try:
            resp = http.get(url, params=params, headers=_stripe_headers(), timeout=20)
        except Exception as exc:  # noqa: BLE001
            log(f"[stripe] PayPal {stage} /poll exception: {type(exc).__name__}")
            continue
        if getattr(resp, "status_code", 0) != 200:
            log(f"[stripe] PayPal {stage} /poll HTTP {getattr(resp, 'status_code', '?')}")
            continue
        try:
            payload = resp.json() or {}
        except Exception:
            payload = {}
        redirect = extract_redirect_url(payload)
        if redirect:
            log(f"[stripe] PayPal {stage} /poll 已取得 buyer redirect")
            return redirect
        log(
            f"[stripe] PayPal {stage} /poll 未返回 redirect："
            f"state={payload.get('state') or ''}; "
            f"payment_object_status={payload.get('payment_object_status') or ''}"
        )
        if attempt + 1 < max_attempts:
            time.sleep(0.8)
    return ""


def poll_redirect_after_approve(http, pk: str, session_id: str, log, *, ctx: dict | None = None, max_attempts: int = 15) -> str:
    """approve 后 GET /payment_pages/<id> 轮询，拿 next_action.redirect_to_url。"""
    ctx = ctx or {}
    params = {
        "key": pk,
        "_stripe_version": STRIPE_VERSION_FULL,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": ctx.get("elements_session_id") or _gen_elements_session_id(),
        "elements_session_client[stripe_js_id]": ctx.get("stripe_js_id") or str(uuid.uuid4()),
        "elements_session_client[locale]": ctx.get("locale") or "en",
        "elements_session_client[is_aggregation_expected]": "false",
        **_elements_options_client_payload(),
    }
    for i in range(max_attempts):
        try:
            gr = http.get(f"{STRIPE_API}/v1/payment_pages/{session_id}", params=params, headers=_stripe_headers(), timeout=20)
        except Exception as e:
            log(f"[stripe] approve 后 GET 异常: {e}")
            time.sleep(1)
            continue
        if getattr(gr, "status_code", 0) != 200:
            time.sleep(1)
            continue
        try:
            gj = gr.json()
        except Exception:
            time.sleep(1)
            continue
        url = extract_redirect_url(gj)
        if url:
            return url
        sa = (gj.get("submission_attempt") or {}).get("state")
        payment_intent = gj.get("payment_intent") or {}
        setup_intent = gj.get("setup_intent") or {}
        decline = payment_intent.get("last_payment_error") or setup_intent.get("last_setup_error") or {}
        if i == 0:
            diagnostic = _paypal_setup_diagnostic(gj)
            log(
                "[stripe] PayPal post-approve setup diagnostic: "
                + json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":"))[:1600]
            )
        log(
            f"[stripe] approve 后 poll {i + 1}: sub_state={sa} "
            f"payment_status={payment_intent.get('status') or ''} "
            f"setup_status={setup_intent.get('status') or ''} "
            f"decline_code={decline.get('decline_code') or decline.get('code') or ''} "
            f"decline_message={decline.get('message') or ''}"
        )
        time.sleep(1)
    return ""


def poll_payment_page_after_approve(
    http,
    pk: str,
    session_id: str,
    log,
    *,
    ctx: dict | None = None,
    max_attempts: int = 3,
) -> dict:
    """Poll the already-confirmed Payment Page after merchant approval.

    Local payment methods submit their PaymentMethod on the first confirm.  A
    second inline confirm creates another setup attempt and can replace the
    approved attempt with ``last_setup_error``.  Poll the approved submission
    first so PIX/UPI can expose their QR/redirect from the original attempt.
    """
    ctx = ctx or {}
    params = {
        "key": pk,
        "_stripe_version": STRIPE_VERSION_FULL,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": ctx.get("elements_session_id") or _gen_elements_session_id(),
        "elements_session_client[stripe_js_id]": ctx.get("stripe_js_id") or str(uuid.uuid4()),
        "elements_session_client[locale]": ctx.get("locale") or "en",
        "elements_session_client[is_aggregation_expected]": "false",
        **_elements_options_client_payload(),
    }
    last: dict = {}
    for attempt in range(max(1, max_attempts)):
        try:
            resp = http.get(
                f"{STRIPE_API}/v1/payment_pages/{session_id}",
                params=params,
                headers=_stripe_headers(),
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"[stripe] approval poll {attempt + 1}/{max_attempts} exception: {type(exc).__name__}")
            time.sleep(1)
            continue
        if getattr(resp, "status_code", 0) != 200:
            log(f"[stripe] approval poll {attempt + 1}/{max_attempts}: HTTP {getattr(resp, 'status_code', '?')}")
            time.sleep(1)
            continue
        try:
            last = resp.json() or {}
        except Exception:
            last = {}
        submission = last.get("submission_attempt") or {}
        next_action = _find_next_action(last)
        payment_intent = last.get("payment_intent") or {}
        setup_intent = last.get("setup_intent") or {}
        decline = payment_intent.get("last_payment_error") or setup_intent.get("last_setup_error") or {}
        log(
            "[stripe] approval poll {}/{}: state={} next_action={} decline={}".format(
                attempt + 1,
                max_attempts,
                submission.get("state") or "",
                next_action.get("type") or "",
                decline.get("code") or decline.get("message") or "",
            )
        )
        if next_action or decline:
            return last
        time.sleep(1)
    return last


def poll_result(http, pk: str, session_id: str, log, max_attempts: int = 60) -> dict:
    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/poll"
    params = {"key": pk, "_stripe_version": STRIPE_VERSION_BASE}
    for attempt in range(max_attempts):
        time.sleep(2)
        try:
            resp = http.get(url, params=params, headers=_stripe_headers(), timeout=30)
        except Exception as e:
            log(f"[stripe] poll 异常: {e}")
            continue
        if getattr(resp, "status_code", 0) != 200:
            continue
        data = resp.json()
        state = data.get("state", "unknown")
        if state == "succeeded":
            log(f"[stripe] 支付成功 state={state} status={data.get('payment_object_status')}")
            return data
        if state in ("failed", "expired", "canceled"):
            log(f"[stripe] 支付失败 state={state}")
            return data
        log(f"[stripe] poll {attempt + 1}/{max_attempts} state={state}")
    raise TimeoutError("Stripe 轮询超时")


def _processor_entity(confirm_data: dict) -> str:
    ret = confirm_data.get("return_url") or ""
    m = re.search(r"processor_entity=([A-Za-z_]+)", ret)
    return m.group(1) if m else ""


def resolve_paypal_approval_url(http, redirect_url: str, log: Callable[[str], None]) -> str:
    current = str(redirect_url or "")
    headers = {
        "User-Agent": CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for _ in range(6):
        if re.search(r"paypal\.com/agreements/approve\?.*\bba_token=", current, re.I):
            return current
        try:
            resp = http.get(current, headers=headers, allow_redirects=False, timeout=30)
        except Exception as exc:
            log(f"[paypal] pm-redirects 解析提示：{type(exc).__name__}")
            return redirect_url
        location = str((getattr(resp, "headers", {}) or {}).get("location") or "")
        if location:
            current = urljoin(current, location)
            continue
        text = getattr(resp, "text", "") or ""
        match = re.search(r"https?://(?:www\.)?paypal\.com/agreements/approve\?[^\s\"'<>]+", text, re.I)
        if match:
            return match.group(0).replace("&amp;", "&")
        break
    return current or redirect_url


def stripe_to_paypal_redirect(
    http,
    session_id: str,
    *,
    billing: dict,
    promotion_billing: dict | None = None,
    payment_billing: dict | None = None,
    payment_http=None,
    country: str = "US",
    chatgpt_http=None,
    access_token: str = "",
    publishable_key: str = "",
    processor_entity: str = "",
    approve_callback=None,
    apply_promo_callback=None,
    require_zero_due: bool = False,
    log: Callable[[str], None] = lambda m: None,
) -> tuple[str, str, dict]:
    """init→elements→create paypal pm→confirm(→manual approve)，返回 (redirect_url, pk, ctx)。"""
    profile = _profile(country)
    payment_billing = payment_billing or billing
    payment_http = payment_http or http
    ctx_payment_country = str((payment_billing.get("address") or {}).get("country") or country).upper()
    pk = publishable_key or verify_pk(http, session_id, log)
    log("[paypal] 第 3/7 步：Stripe init")
    init_data, version, ctx = init_checkout(http, session_id, pk, profile, log)
    if "paypal" not in (ctx.get("payment_method_types") or []):
        raise RuntimeError(f"当前支付线路未开放 PayPal，可用方式：{', '.join(ctx.get('payment_method_types') or []) or 'card'}")
    fetch_elements_session(http, pk, session_id, ctx, version, profile, log)
    processor_entity = processor_entity or _entity_from_return_url(ctx.get("return_url") or init_data.get("return_url") or "") or "openai_llc"
    initial_amount = ctx.get("checkout_amount")
    initial_zero = str(initial_amount).strip() in {"0", "0.0", "0.00"}
    if apply_promo_callback and not initial_zero:
        apply_promo_callback(processor_entity)
        log("[promo] PayPal Stage1 金额未归零，已补充优惠更新并等待 Stripe 同步")
        for sync_attempt in range(6):
            time.sleep(1.5 if sync_attempt else 0.8)
            init_data, version, ctx = init_checkout(http, session_id, pk, profile, log)
            if "paypal" not in (ctx.get("payment_method_types") or []):
                raise RuntimeError(f"应用优惠后 checkout 未开放 paypal，可用方式：{', '.join(ctx.get('payment_method_types') or []) or 'card'}")
            synced_amount = ctx.get("checkout_amount")
            log(f"[promo] PayPal 优惠同步检查 {sync_attempt + 1}/6：amount={synced_amount}")
            if str(synced_amount).strip() in {"0", "0.0", "0.00"}:
                break
        fetch_elements_session(http, pk, session_id, ctx, version, profile, log)
    ctx["billing"] = billing  # confirm 内联 payment_method_data 需要账单身份

    # 买家地址 → tax_region（否则税费停留在 requires_location_inputs，confirm 拿不到跳转）
    update_tax_region(http, session_id, pk, version, ctx, billing, profile, log)
    checkout_amount = ctx.get("checkout_amount")
    if require_zero_due:
        if checkout_amount is None:
            raise RuntimeError("优惠金额校验失败：Stripe 未返回今日应付金额")
        try:
            promo_applied = int(str(checkout_amount)) == 0
        except ValueError:
            promo_applied = str(checkout_amount).strip() in {"0", "0.0", "0.00"}
        if not promo_applied:
            raise RuntimeError(f"Plus 首月免费优惠未生效：Stripe 今日应付 amount={checkout_amount}")
        log("[paypal] 第 4/7 步：金额校验通过，Stripe 今日应付 amount=0")
        ctx["promo_applied"] = True
    else:
        log(f"[paypal] 第 4/7 步：金额校验 amount={checkout_amount}")

    # confirm 前把账单地址快照给 ChatGPT 后端（manual_approval 流程需要）
    ctx["processor_entity"] = processor_entity
    # merchant approval snapshot must match the PayPal/Stripe billing country.
    # The BR promotion is already applied through checkout/update; writing BR
    # into this approval snapshot while the PaymentMethod is US makes the
    # approved setup fall into generic_decline.
    snapshot_payload = billing
    snapshot_billing(
        chatgpt_http,
        access_token,
        session_id,
        processor_entity,
        snapshot_payload,
        log,
    )
    if promotion_billing:
        log(
            "[paypal] BR 优惠已由 checkout/update 应用；merchant 快照与 "
            f"Stripe/PayPal 账单统一为 {(billing.get('address') or {}).get('country') or country}"
        )

    # One-shot browser capture hook used to refresh Stripe.js fingerprint
    # fields. It is inactive unless the local flag file is explicitly present.
    try:
        import pathlib
        capture_flag = pathlib.Path("/tmp/paypal_browser_capture.flag")
        if capture_flag.exists():
            seed = {
                "session_id": session_id,
                "stripe_hosted_url": _paypal_return_url(session_id, init_data, ctx),
                "billing": billing,
                "publishable_key": pk,
            }
            pathlib.Path("/tmp/paypal_browser_seed.json").write_text(
                json.dumps(seed, ensure_ascii=False), encoding="utf-8"
            )
            capture_flag.unlink(missing_ok=True)
            log("[paypal] 浏览器指纹采集窗口已准备，等待 150 秒")
            time.sleep(150)
            raise RuntimeError("PayPal 浏览器指纹采集窗口结束")
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"[paypal] 浏览器指纹采集钩子提示：{type(exc).__name__}")

    log("[paypal] 第 5/7 步：创建 PayPal PaymentMethod")
    ctx["billing"] = payment_billing
    ctx["paypal_billing_country"] = ctx_payment_country
    pm_id = create_paypal_payment_method(payment_http, pk, payment_billing, session_id, version, ctx, log)
    log("[paypal] 第 6/7 步：confirm → approve → poll")
    # 首轮采用已创建的 pm_*，避免 inline confirm 再创建一个匿名 PayPal PM。
    # merchant approve 后直接 poll 同一个 submission。
    confirm_data = confirm_payment(
        payment_http, pk, session_id, pm_id, init_data, version, ctx, profile, log,
        inline_payment_method=False,
    )
    redirect_url = extract_redirect_url(confirm_data)
    approved_payment_method = _find_payment_method_id(confirm_data)
    log(
        "[stripe] 首次 inline confirm 已提取 approved submission PM="
        f"{bool(approved_payment_method)}"
    )

    # manual_approval beta：confirm 返回 requires_approval 后，由 merchant approve
    # 首次 submission，再直接 poll 该 submission 的 redirect。
    if not redirect_url:
        sub = confirm_data.get("submission_attempt") or {}
        if sub.get("state") == "requires_approval" and chatgpt_http is not None and access_token:
            pe = _processor_entity(confirm_data) or processor_entity
            # Preserve the first confirmed submission.  Probe both Stripe's
            # compact /poll view and one complete payment_pages snapshot before
            # merchant approval; neither request creates a second submission.
            pre_approve_redirect = poll_paypal_redirect_light(
                payment_http, pk, session_id, log,
                max_attempts=1, stage="pre-approve",
            )
            if not pre_approve_redirect:
                pre_approve_redirect = poll_redirect_after_approve(
                    payment_http, pk, session_id, log, ctx=ctx, max_attempts=1,
                )
            log(f"[stripe] manual_approval（requires_approval）→ 调 ChatGPT approve（processor_entity={pe}）…")
            if approve_callback:
                approve_callback(pe)
            else:
                approve_submission(chatgpt_http, access_token, session_id, pe, log)
            # Reuse an early redirect if Stripe already exposed one. Otherwise
            # read the approved submission once through /poll, then once through
            # the full payment_pages representation. Never re-confirm here.
            redirect_url = pre_approve_redirect or poll_paypal_redirect_light(
                payment_http, pk, session_id, log,
                max_attempts=1, stage="post-approve",
            )
            if not redirect_url:
                redirect_url = poll_redirect_after_approve(
                    payment_http, pk, session_id, log, ctx=ctx, max_attempts=1,
                )
            if not redirect_url:
                raise RuntimeError("PayPal buyer redirect 未生成，正在废弃当前 Checkout 并更换代理重试")

    if not redirect_url:
        try:
            import pathlib
            pathlib.Path("_confirm_dump.json").write_text(
                json.dumps(confirm_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log("[stripe] confirm 响应已 dump 到 _confirm_dump.json（供排查）")
        except Exception:
            pass
        raise RuntimeError(f"confirm 未返回 PayPal 跳转地址: {json.dumps(confirm_data)[:300]}")
    ctx["stripe_redirect_url"] = redirect_url
    paypal_url = resolve_paypal_approval_url(payment_http, redirect_url, log)
    if paypal_url != redirect_url:
        log(f"[paypal] 已解析 agreements/approve 链接: {paypal_url[:110]}")
    else:
        log(f"[stripe] PayPal 跳转: {redirect_url[:120]}")
    return paypal_url, pk, ctx
