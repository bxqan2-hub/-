"""Kakao Pay adapter for ChatGPT-owned ``oaics_*`` Checkout sessions.

Adapted from https://github.com/m1243808154/kakao_oaics_source (commit
60e4201470ea58c11f840e0da9ccf71835eea12c).  This module deliberately stays
inside the PAY.153 runtime: the source project is attribution and protocol
reference, not a third bundled service.

The important safety property is the one-shot boundary.  Everything through
ConfirmationToken creation is retryable.  Once the ChatGPT confirm request is
sent, the caller receives ``confirm_sent=True`` on any failure and must never
replay that Checkout.
"""
from __future__ import annotations

import html
import os
import re
import threading
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlsplit

import stripe_checkout as sc
from provider_checkout import flatten_stripe_params


LogFn = Callable[[str], None]
OAICS_SOURCE_RUNTIME = "c00af4ce81"
_WORKERS = max(1, min(8, int(os.getenv("PAY153_KAKAO_OAICS_WORKERS") or "1")))
_SLOTS = threading.BoundedSemaphore(_WORKERS)
_URL_RE = re.compile(r"https://[^\s\"'<>\\]+", re.IGNORECASE)


class KakaoOaicsError(RuntimeError):
    def __init__(self, message: str, *, state: str, confirm_sent: bool = False):
        super().__init__(message)
        self.state = state
        self.confirm_sent = confirm_sent


def _json(response: Any, stage: str) -> dict[str, Any]:
    try:
        value = response.json()
    except Exception as exc:
        text = str(getattr(response, "text", "") or "")[:400]
        raise KakaoOaicsError(
            f"{stage} returned non-JSON: {text}", state=stage,
        ) from exc
    if not isinstance(value, dict):
        raise KakaoOaicsError(f"{stage} returned non-object JSON", state=stage)
    return value


def _first_string(value: Any, names: set[str]) -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in names and isinstance(nested, str) and nested.strip():
                return nested.strip()
        for nested in value.values():
            found = _first_string(nested, names)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _first_string(nested, names)
            if found:
                return found
    return ""


def _amount_minor(value: Any) -> int | None:
    preferred = (
        "amount_due", "total_due", "total_amount", "amount_total",
        "checkout_amount", "amount",
    )
    if isinstance(value, dict):
        summary = value.get("total_summary")
        if isinstance(summary, dict):
            for name in ("due", "total", "amount_due"):
                raw = summary.get(name)
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    return int(raw)
        invoice = value.get("invoice")
        if isinstance(invoice, dict):
            raw = invoice.get("amount_due")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return int(raw)
        for name in preferred:
            raw = value.get(name)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return int(raw)
        for nested in value.values():
            found = _amount_minor(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _amount_minor(nested)
            if found is not None:
                return found
    return None


def _payment_methods(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if str(key).lower() in {
                    "payment_method_types", "ordered_payment_method_types",
                    "available_payment_method_types",
                } and isinstance(nested, list):
                    for method in nested:
                        name = str(method or "").strip().lower()
                        if name and name not in found:
                            found.append(name)
                if str(key).lower() == "payment_method_specs" and isinstance(nested, list):
                    for spec in nested:
                        if isinstance(spec, dict):
                            name = str(spec.get("type") or "").strip().lower()
                            if name and name not in found:
                                found.append(name)
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return found


def extract_kakao_redirect_url(value: Any) -> str:
    """Return only an allow-listed Kakao/NicePay/Stripe HTTPS redirect."""
    candidates: list[tuple[int, str]] = []

    def add(raw: Any) -> None:
        if not isinstance(raw, str):
            return
        text = html.unescape(raw).replace("\\u0026", "&").replace("\\/", "/").strip()
        try:
            parsed = urlsplit(text)
        except ValueError:
            return
        host = str(parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host:
            return
        if host == "nicepay.co.kr" or host.endswith(".nicepay.co.kr"):
            candidates.append((0, text))
        elif host in {"kakaopay.com", "kakaopay.co.kr", "kakao.com"} or host.endswith(
            (".kakaopay.com", ".kakaopay.co.kr", ".kakao.com")
        ):
            candidates.append((1, text))
        elif host in {"pm-redirects.stripe.com", "hooks.stripe.com"}:
            candidates.append((2, text))

    def visit(item: Any) -> None:
        if isinstance(item, str):
            add(item)
            for match in _URL_RE.findall(item):
                add(match.rstrip(".,);]"))
        elif isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return min(candidates, default=(99, ""))[1]


def _client_headers(page_html: str) -> dict[str, str]:
    source = str(page_html or "")

    def match(pattern: str) -> str:
        found = re.search(pattern, source, flags=re.IGNORECASE)
        return html.unescape(str(found.group(1) or "")).strip() if found else ""

    attestation = match(r'"webDeploymentAttestation"\s*:\s*"([^\"]+)"')
    session_id = match(r'"sessionId"\s*:\s*"([^\"]+)"')
    headers = {
        "oai-client-version": match(r"\bdata-build=[\"']([^\"']+)[\"']"),
        "oai-client-build-number": match(r"\bdata-seq=[\"']([^\"']+)[\"']"),
        "oai-web-deployment-attestation": attestation,
        "oai-session-id": session_id,
    }
    if any(headers.values()):
        headers["x-oai-is-client-observation"] = f"v1.r.d.{uuid.uuid4().hex[:16]}"
    return {key: value for key, value in headers.items() if value}


def _stripe_headers(accept_language: str = "ko-KR,ko;q=0.9,en;q=0.8") -> dict[str, str]:
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": sc.CHROME_UA,
        "Accept-Language": accept_language,
    }


def _runtime_version(stripe_http: Any, snapshots: list[Any], log: LogFn) -> tuple[str, str]:
    for snapshot in snapshots:
        value = _first_string(snapshot, {"runtime_version", "stripe_runtime_version"})
        if re.fullmatch(r"[A-Za-z0-9]{10,64}", value):
            return value, "checkout_snapshot"
    configured = str(os.getenv("PAY153_KAKAO_STRIPE_RUNTIME") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9]{10,64}", configured):
        return configured, "environment"
    try:
        response = stripe_http.get(
            "https://js.stripe.com/v3/", headers={"User-Agent": sc.CHROME_UA}, timeout=25,
        )
        source = str(getattr(response, "text", "") or "")
        patterns = (
            r"stripe\.js/([A-Za-z0-9]{10,64})",
            r'\b(?:runtimeVersion|runtime_version|rv)\s*[:=]\s*[\"\']([A-Za-z0-9]{10,64})',
        )
        for pattern in patterns:
            found = re.search(pattern, source)
            if found:
                return found.group(1), "live_stripe_js"
    except Exception as exc:
        log(f"[kakao-oaics] Stripe JS runtime discovery failed before confirm: {type(exc).__name__}")
    # This is the exact runtime pinned by the attributed OAICS source commit,
    # not a randomly fabricated fallback. Operators can override it via env.
    return OAICS_SOURCE_RUNTIME, "oaics_source_lock"


def _bootstrap(
    chatgpt_http: Any,
    *,
    token: str,
    session_id: str,
    processor: str,
    device_id: str,
    initial: dict[str, Any],
    log: LogFn,
) -> tuple[dict[str, Any], dict[str, str], str]:
    checkout_url = f"https://chatgpt.com/checkout/{processor}/{session_id}"
    page = chatgpt_http.get(
        checkout_url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            "User-Agent": sc.CHROME_UA,
        },
        timeout=35,
        allow_redirects=True,
    )
    page_status = int(getattr(page, "status_code", 0) or 0)
    if page_status <= 0 or page_status >= 400:
        raise KakaoOaicsError(
            f"checkout route bootstrap HTTP {page_status or '?'}",
            state="checkout_bootstrap",
        )
    page_html = str(getattr(page, "text", "") or "")
    live_token = token
    try:
        auth = chatgpt_http.get(
            "https://chatgpt.com/api/auth/session",
            headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json",
                "Referer": checkout_url, "User-Agent": sc.CHROME_UA,
            },
            timeout=30,
        )
        if int(getattr(auth, "status_code", 0) or 0) == 200:
            auth_data = _json(auth, "auth_session")
            live_token = str(auth_data.get("accessToken") or auth_data.get("access_token") or token)
    except KakaoOaicsError:
        pass
    response = chatgpt_http.get(
        f"https://chatgpt.com/backend-api/payments/checkout/{processor}/{session_id}",
        headers={
            "Authorization": f"Bearer {live_token}", "Accept": "application/json",
            "Referer": checkout_url, "User-Agent": sc.CHROME_UA,
            "OAI-Device-Id": device_id,
        },
        timeout=40,
    )
    if int(getattr(response, "status_code", 0) or 0) != 200:
        raise KakaoOaicsError(
            f"checkout snapshot HTTP {getattr(response, 'status_code', '?')}: "
            f"{str(getattr(response, 'text', '') or '')[:300]}",
            state="checkout_bootstrap",
        )
    snapshot = _json(response, "checkout_snapshot")
    merged = dict(initial)
    merged.update(snapshot)
    log(f"[kakao-oaics] bootstrap page={page_status} snapshot=200 client_headers={len(_client_headers(page_html))}")
    return merged, _client_headers(page_html), live_token


def _elements_session(
    stripe_http: Any,
    *,
    publishable_key: str,
    session_id: str,
    amount: int,
    methods: list[str],
    log: LogFn,
) -> dict[str, Any]:
    stripe_js_id = str(uuid.uuid4())
    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": "krw",
        "deferred_intent[setup_future_usage]": "off_session",
        "currency": "krw",
        "key": publishable_key,
        "_stripe_version": sc.STRIPE_VERSION_FULL,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": stripe_js_id,
        "locale": "ko",
        "type": "deferred_intent",
        "checkout_session_id": session_id,
    }
    normalized = list(dict.fromkeys(["kakao_pay", *methods, "link", "card"]))
    for index, method in enumerate(normalized):
        params[f"deferred_intent[payment_method_types][{index}]"] = method
    last = ""
    for attempt in range(1, 4):
        response = stripe_http.get(
            f"{sc.STRIPE_API}/v1/elements/sessions",
            params=params,
            headers=_stripe_headers(),
            timeout=35,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        data = _json(response, "elements_session")
        real_id = str(data.get("session_id") or data.get("id") or "")
        config_id = str(data.get("config_id") or "")
        if status == 200 and real_id and config_id:
            data["session_id"] = real_id
            data["config_id"] = config_id
            data["stripe_js_id"] = stripe_js_id
            available = _payment_methods(data)
            if available and "kakao_pay" not in available:
                raise KakaoOaicsError(
                    f"real Elements Session has no kakao_pay: {available}",
                    state="elements_session",
                )
            log(f"[kakao-oaics] real Elements Session ready (attempt {attempt}/3)")
            return data
        last = f"HTTP {status}: {str(data.get('error') or data)[:300]}"
        if attempt < 3:
            time.sleep(attempt)
    raise KakaoOaicsError(
        f"Stripe elements/sessions incomplete after 3 attempts: {last}",
        state="elements_session",
    )


def _confirmation_token(
    stripe_http: Any,
    *,
    publishable_key: str,
    elements: dict[str, Any],
    billing: dict[str, Any],
    methods: list[str],
    runtime: str,
) -> str:
    address = dict(billing.get("address") or {})
    attribution = {
        "client_session_id": elements["stripe_js_id"],
        "elements_session_config_id": elements["config_id"],
        "elements_session_id": elements["session_id"],
        "merchant_integration_additional_elements": ["expressCheckout", "payment", "address"],
        "merchant_integration_source": "elements",
        "merchant_integration_subtype": "payment-element",
        "merchant_integration_version": "2021",
        "payment_intent_creation_flow": "deferred",
        "payment_method_selection_flow": "merchant_specified",
    }
    payload: dict[str, Any] = {
        "setup_future_usage": "off_session",
        "set_as_default_payment_method": False,
        "mandate_data": {"customer_acceptance": {"type": "online", "online": {"infer_from_client": True}}},
        "client_context": {
            "mode": "subscription", "currency": "krw",
            "payment_method_types": list(dict.fromkeys(["kakao_pay", *methods, "link", "card"])),
        },
        "client_attribution_metadata": attribution,
        "payment_method_data": {
            "type": "kakao_pay",
            "billing_details": {
                "address": {
                    "city": str(address.get("city") or "Seoul"),
                    "country": "KR",
                    "line1": str(address.get("line1") or "30 Eulji-ro"),
                    "postal_code": str(address.get("postal_code") or "04533"),
                    "state": str(address.get("state") or "Seoul"),
                },
                "email": str(billing.get("email") or ""),
                "name": str(billing.get("name") or "Minjun Kim"),
                "phone": str(billing.get("phone") or ""),
            },
            "client_attribution_metadata": attribution,
            "guid": uuid.uuid4().hex,
            "muid": uuid.uuid4().hex,
            "sid": uuid.uuid4().hex,
            "payment_user_agent": (
                f"stripe.js/{runtime}; stripe-js-v3/{runtime}; payment-element; deferred-intent"
            ),
            "referrer": "https://chatgpt.com",
            "time_on_page": "32000",
        },
        "key": publishable_key,
        "_stripe_version": sc.STRIPE_VERSION_FULL,
    }
    customer = str(elements.get("customer") or "")
    if customer:
        payload["client_context"]["customer"] = customer
    form = flatten_stripe_params(payload)
    removed: list[str] = []
    data: dict[str, Any] = {}
    status = 0
    for _ in range(5):
        response = stripe_http.post(
            f"{sc.STRIPE_API}/v1/confirmation_tokens",
            data=form,
            headers=_stripe_headers(),
            timeout=35,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        data = _json(response, "confirmation_token")
        token_id = str(data.get("id") or "")
        if status == 200 and token_id.startswith("ctoken_"):
            return token_id
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        if str(error.get("code") or "") != "parameter_unknown":
            break
        unknown = str(error.get("param") or "")
        if not unknown or unknown in {"payment_method_data", "key"} or unknown in removed:
            break
        reduced = {key: value for key, value in form.items() if key != unknown and not key.startswith(f"{unknown}[")}
        if len(reduced) == len(form):
            break
        removed.append(unknown)
        form = reduced
    raise KakaoOaicsError(
        f"Stripe confirmation_token HTTP {status}: {str(data.get('error') or data)[:350]}",
        state="confirmation_token",
    )


def _confirm_intent(
    stripe_http: Any,
    *,
    publishable_key: str,
    client_secret: str,
    confirmation_token: str,
    return_url: str,
) -> dict[str, Any]:
    intent_id, separator, _secret = str(client_secret).partition("_secret_")
    if not separator or not (intent_id.startswith("seti_") or intent_id.startswith("pi_")):
        raise KakaoOaicsError("invalid Stripe client_secret", state="stripe_intent", confirm_sent=True)
    resource = "setup_intents" if intent_id.startswith("seti_") else "payment_intents"
    response = stripe_http.post(
        f"{sc.STRIPE_API}/v1/{resource}/{intent_id}/confirm",
        data={
            "client_secret": client_secret,
            "confirmation_token": confirmation_token,
            "return_url": return_url,
            "use_stripe_sdk": "true",
            "key": publishable_key,
            "_stripe_version": sc.STRIPE_VERSION_FULL,
        },
        headers=_stripe_headers(),
        timeout=40,
    )
    data = _json(response, "stripe_intent")
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200 or data.get("error"):
        raise KakaoOaicsError(
            f"Stripe Intent confirm HTTP {status}: {str(data.get('error') or data)[:350]}",
            state="stripe_intent", confirm_sent=True,
        )
    return data


def run_kakao_oaics(
    *,
    chatgpt_http: Any,
    stripe_http: Any,
    token: str,
    session_id: str,
    processor: str,
    device_id: str,
    initial_checkout: dict[str, Any],
    billing: dict[str, Any],
    sentinel_headers: dict[str, str] | None,
    log: LogFn,
    update_taxes: Callable[[Any, str, str, str, dict[str, Any], str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Execute a complete, serialized OAICS Kakao state machine."""
    if not str(session_id).startswith("oaics_"):
        raise KakaoOaicsError("not an oaics_* Checkout", state="checkout_bootstrap")
    with _SLOTS:
        history = ["checkout_bootstrap"]
        snapshot, merchant_headers, effective_token = _bootstrap(
            chatgpt_http, token=token, session_id=session_id, processor=processor,
            device_id=device_id, initial=initial_checkout, log=log,
        )
        publishable_key = _first_string(
            [snapshot, initial_checkout], {"publishable_key", "stripe_publishable_key"},
        )
        if not publishable_key.startswith("pk_"):
            raise KakaoOaicsError(
                "OAICS checkout did not expose a Stripe publishable key",
                state="checkout_bootstrap",
            )
        amount = _amount_minor(snapshot)
        if amount is None:
            raise KakaoOaicsError("OAICS checkout amount is unknown", state="amount_check")
        if amount != 0:
            raise KakaoOaicsError(
                f"OAICS Kakao requires zero due, got amount={amount}", state="amount_check",
            )
        methods = _payment_methods(snapshot)
        history.append("elements_session")
        elements = _elements_session(
            stripe_http, publishable_key=publishable_key, session_id=session_id,
            amount=amount, methods=methods, log=log,
        )
        element_methods = _payment_methods(elements)
        if element_methods:
            methods = element_methods
        history.append("checkout_taxes")
        tax_checkout = update_taxes(
            chatgpt_http, effective_token, session_id, processor, billing, "KRW", device_id,
        )
        if isinstance(tax_checkout, dict) and tax_checkout:
            snapshot.update(tax_checkout)
        tax_amount = _amount_minor(snapshot)
        if tax_amount not in {None, 0}:
            raise KakaoOaicsError(
                f"OAICS Kakao became non-zero after taxes: amount={tax_amount}",
                state="checkout_taxes",
            )
        runtime, runtime_source = _runtime_version(stripe_http, [elements, snapshot], log)
        log(f"[kakao-oaics] Stripe runtime source={runtime_source}")
        history.append("confirmation_token")
        confirmation_token = _confirmation_token(
            stripe_http, publishable_key=publishable_key, elements=elements,
            billing=billing, methods=methods, runtime=runtime,
        )
        log("[kakao-oaics] ConfirmationToken ready; entering one-shot confirm boundary")
        history.append("chatgpt_confirm")
        confirm_sent = True
        try:
            response = chatgpt_http.post(
                "https://chatgpt.com/backend-api/payments/checkout/confirm",
                json={
                    "checkout_session_id": session_id,
                    "processor_entity": processor,
                    "selected_payment_method_type": "kakao_pay",
                    "confirm_token": confirmation_token,
                },
                headers={
                    "Authorization": f"Bearer {effective_token}",
                    "Content-Type": "application/json", "Accept": "application/json",
                    "Origin": "https://chatgpt.com",
                    "Referer": f"https://chatgpt.com/checkout/{processor}/{session_id}",
                    "User-Agent": sc.CHROME_UA, "OAI-Device-Id": device_id,
                    "x-openai-target-path": "/backend-api/payments/checkout/confirm",
                    "x-openai-target-route": "/backend-api/payments/checkout/confirm",
                    **merchant_headers, **(sentinel_headers or {}),
                },
                timeout=50,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            confirmed = _json(response, "chatgpt_confirm")
        except KakaoOaicsError as exc:
            raise KakaoOaicsError(str(exc), state=exc.state, confirm_sent=True) from exc
        except Exception as exc:
            raise KakaoOaicsError(
                f"ChatGPT confirm transport error: {type(exc).__name__}: {exc}",
                state="chatgpt_confirm", confirm_sent=True,
            ) from exc
        state = str(confirmed.get("status") or "").lower()
        if status_code != 200 or state in {"blocked", "denied", "expired", "error", "failed"}:
            raise KakaoOaicsError(
                f"ChatGPT Kakao confirm HTTP {status_code} status={state or '-'}: "
                f"{str(confirmed.get('error') or confirmed)[:350]}",
                state="chatgpt_confirm", confirm_sent=confirm_sent,
            )
        redirect = extract_kakao_redirect_url(confirmed)
        intent_data: dict[str, Any] = {}
        if not redirect:
            client_secret = _first_string(confirmed, {"client_secret"})
            if not client_secret:
                raise KakaoOaicsError(
                    "ChatGPT confirm returned neither redirect nor client_secret",
                    state="chatgpt_confirm", confirm_sent=True,
                )
            history.append("stripe_intent")
            intent_data = _confirm_intent(
                stripe_http, publishable_key=publishable_key,
                client_secret=client_secret, confirmation_token=confirmation_token,
                return_url=f"https://chatgpt.com/checkout/{processor}/{session_id}",
            )
            redirect = extract_kakao_redirect_url(intent_data)
        if not redirect:
            raise KakaoOaicsError(
                "Kakao confirm completed without an allow-listed redirect",
                state="stripe_intent", confirm_sent=True,
            )
        history.append("complete")
        return {
            "redirect_url": redirect,
            "checkout_amount": tax_amount if tax_amount is not None else amount,
            "amount_currency": "KRW",
            "runtime_source": runtime_source,
            "state_history": history,
            "confirm_sent": True,
            "elements_session_id": elements["session_id"],
            "elements_session_config_id": elements["config_id"],
            "response": intent_data or confirmed,
        }


__all__ = ["KakaoOaicsError", "extract_kakao_redirect_url", "run_kakao_oaics"]
