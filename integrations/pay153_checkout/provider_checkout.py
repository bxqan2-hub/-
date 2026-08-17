from __future__ import annotations

import base64
import io
import json
import random
import re
import time
import uuid
from urllib.parse import quote, urlsplit
from datetime import date, timedelta
from typing import Any, Callable

import stripe_checkout as sc
from billing_address_resolver import resolve_public_address


PROVIDER_DEFAULTS = {
    "paypal": {"country": "US", "currency": "USD"},
    "ideal": {"country": "NL", "currency": "EUR"},
    "upi": {"country": "IN", "currency": "INR"},
    "pix": {"country": "BR", "currency": "BRL"},
    "momo": {"country": "VN", "currency": "VND"},
    "gcash": {"country": "PH", "currency": "PHP"},
    "twint": {"country": "CH", "currency": "CHF"},
}

# PIX Automático / UPI AutoPay mandate_options were added after the Checkout
# Payment Page version currently returned by this merchant.  Use the current
# Stripe API train only for the direct SetupIntent fallback.
LOCAL_MANDATE_STRIPE_VERSION = "2026-06-24.dahlia"
LOCAL_MANDATE_STRIPE_VERSION_FULL = (
    f"{LOCAL_MANDATE_STRIPE_VERSION}; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
HOSTED_CHECKOUT_STRIPE_VERSION = (
    "2020-08-27;custom_checkout_beta=v1; "
    "checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
)
HOSTED_CHECKOUT_RUNTIME_VERSION = "e1fb22ad35"


def generate_cpf() -> str:
    digits = [random.randint(0, 9) for _ in range(9)]
    for weights in (range(10, 1, -1), range(11, 1, -1)):
        value = 11 - sum(number * weight for number, weight in zip(digits, weights)) % 11
        digits.append(0 if value >= 10 else value)
    return "".join(map(str, digits))


def _format_cpf(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return digits


def _hosted_provider_return_url(hosted_url: str, provider: str) -> str:
    if not hosted_url:
        return "https://checkout.stripe.com/"
    base, marker, fragment = hosted_url.partition("#")
    joiner = "&" if "?" in base else "?"
    base = (
        f"{base}{joiner}redirect_pm_type={provider}"
        f"&lid={uuid.uuid4()}&ui_mode=custom"
    )
    return f"{base}#{fragment}" if marker else base


def payment_decline(data: dict) -> dict:
    payment_intent = data.get("payment_intent") or {}
    setup_intent = data.get("setup_intent") or {}
    return payment_intent.get("last_payment_error") or setup_intent.get("last_setup_error") or {}


def provider_failure_detail(data: dict) -> str:
    """Return the most useful local-payment failure fields without secrets."""
    decline = payment_decline(data)
    submission = data.get("submission_attempt") or {}
    setup_intent = data.get("setup_intent") or {}
    payment_intent = data.get("payment_intent") or {}
    values: list[str] = []
    for label, value in (
        ("submission_state", submission.get("state")),
        ("submission_failure", submission.get("failure_code") or submission.get("failure_reason")),
        ("decline_code", decline.get("decline_code") or decline.get("code")),
        ("decline_message", decline.get("message")),
        ("setup_status", setup_intent.get("status")),
        ("payment_status", payment_intent.get("status")),
    ):
        text = str(value or "").strip()
        if text:
            values.append(f"{label}={text[:220]}")
    return "; ".join(values)


def confirm_local_setup_intent(
    http,
    pk: str,
    provider: str,
    payment_page: dict,
    payment_method_id: str,
    ctx: dict,
    log: Callable[[str], None],
) -> dict:
    """Retry the approved local mandate on its SetupIntent directly.

    Payment Pages can return an approved submission whose SetupIntent is still
    ``requires_payment_method``.  UPI AutoPay especially needs a direct
    SetupIntent confirm after merchant approval; Payment Page confirm often
    strips ``payment_method_options`` as unknown on this Checkout revision.
    """
    provider = provider.lower()
    setup_intent = payment_page.get("setup_intent") or {}
    setup_id = str(setup_intent.get("id") or "")
    client_secret = str(setup_intent.get("client_secret") or "")
    submission = payment_page.get("submission_attempt") or {}
    candidate_pm = (
        payment_method_id
        or ctx.get("payment_method_id")
        or setup_intent.get("payment_method")
        or submission.get("payment_method")
        or ""
    )
    if isinstance(candidate_pm, dict):
        candidate_pm = candidate_pm.get("id") or ""
    candidate_pm = str(candidate_pm)
    if not setup_id.startswith("seti_") or not client_secret or not candidate_pm.startswith("pm_"):
        log(
            f"[{provider}] SetupIntent 直连补交条件不足："
            f"setup_id={bool(setup_id)} client_secret={bool(client_secret)} "
            f"pm_id={bool(candidate_pm.startswith('pm_') and candidate_pm)}"
        )
        return payment_page

    original_amount = ctx.get("original_checkout_amount") or ctx.get("checkout_amount") or 0
    try:
        mandate_amount = max(1, int(str(original_amount)))
    except (TypeError, ValueError):
        mandate_amount = 1
    if mandate_amount <= 1:
        mandate_amount = 9990 if provider == "pix" else 199900

    base_options = dict(ctx.get("provider_payment_method_options") or {})
    base_mandate = dict(base_options.get("mandate_options") or {})
    return_url = str(ctx.get("stripe_hosted_url") or ctx.get("return_url") or "https://chatgpt.com/")
    headers_candidates = [
        LOCAL_MANDATE_STRIPE_VERSION,
        sc.STRIPE_VERSION_FULL,
        sc.STRIPE_VERSION_BASE,
    ]

    variants: list[tuple[str, dict]] = []
    if provider == "upi":
        end_unix = int(time.time()) + 365 * 24 * 60 * 60
        variants.extend(
            [
                (
                    "upi_max_1y",
                    {
                        "mandate_options": {
                            "amount": mandate_amount,
                            "amount_type": "maximum",
                            "description": base_mandate.get("description") or "Subscription payment",
                            "end_date": end_unix,
                        }
                    },
                ),
                (
                    "upi_fixed_1y",
                    {
                        "mandate_options": {
                            "amount": mandate_amount,
                            "amount_type": "fixed",
                            "description": base_mandate.get("description") or "Subscription payment",
                            "end_date": end_unix,
                        }
                    },
                ),
                (
                    "upi_max_no_end",
                    {
                        "mandate_options": {
                            "amount": mandate_amount,
                            "amount_type": "maximum",
                            "description": base_mandate.get("description") or "Subscription payment",
                        }
                    },
                ),
                ("upi_pm_only", {}),
            ]
        )
    elif provider == "pix":
        variants.extend(
            [
                (
                    "pix_monthly",
                    {
                        "mandate_options": {
                            "amount": mandate_amount,
                            "amount_type": "maximum",
                            "payment_schedule": "monthly",
                            "start_date": (date.today() + timedelta(days=3)).isoformat(),
                        }
                    },
                ),
                ("pix_pm_only", {}),
            ]
        )
    else:
        variants.append(("pm_only", {}))

    last_error = ""
    for variant_name, options in variants:
        body = {
            "client_secret": client_secret,
            "payment_method": candidate_pm,
            "return_url": return_url,
            "use_stripe_sdk": "true",
            "mandate_data[customer_acceptance][type]": "online",
            "mandate_data[customer_acceptance][online][infer_from_client]": "true",
            "key": pk,
        }
        if options:
            body.update(flatten_stripe_params(options, f"payment_method_options[{provider}]"))
        mandate_keys = sorted((options.get("mandate_options") or {}).keys())
        for api_version in headers_candidates:
            headers = dict(sc._stripe_headers())
            headers["Stripe-Version"] = api_version
            log(
                f"[{provider}] SetupIntent 直连补交：variant={variant_name} "
                f"amount={mandate_amount} mandate_keys={','.join(mandate_keys) or '-'} "
                f"api={api_version.split(';')[0]}"
            )
            try:
                resp = http.post(
                    f"{sc.STRIPE_API}/v1/setup_intents/{setup_id}/confirm",
                    data=body,
                    headers=headers,
                    timeout=40,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                log(f"[{provider}] SetupIntent 直连补交异常：{last_error}")
                continue
            resp_text = getattr(resp, "text", "") or ""
            status_code = getattr(resp, "status_code", 0)
            if status_code != 200:
                last_error = f"HTTP {status_code}: {resp_text[:360]}"
                log(f"[{provider}] SetupIntent 直连补交失败 [{status_code}]: {resp_text[:360]}")
                continue
            try:
                payload = resp.json() or {}
            except Exception:
                last_error = "invalid JSON from SetupIntent confirm"
                continue
            next_action = payload.get("next_action") or {}
            log(
                f"[{provider}] SetupIntent 直连补交返回：status={payload.get('status') or ''} "
                f"next_action={next_action.get('type') or ''}"
            )
            merged = dict(payment_page)
            merged["setup_intent"] = payload
            if next_action:
                merged["next_action"] = next_action
            if payload.get("status") in {"requires_action", "succeeded", "processing"}:
                submission = dict(merged.get("submission_attempt") or {})
                if submission.get("state") == "failed":
                    submission["state"] = "processing"
                    merged["submission_attempt"] = submission
            return merged
    if last_error:
        log(f"[{provider}] SetupIntent 直连补交全部变体失败：{last_error[:300]}")
    return payment_page


def flatten_stripe_params(value: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}[{key}]" if prefix else str(key)
            out.update(flatten_stripe_params(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.update(flatten_stripe_params(item, f"{prefix}[{index}]"))
    elif value is not None and prefix:
        if isinstance(value, bool):
            out[prefix] = "true" if value else "false"
        else:
            out[prefix] = str(value)
    return out


def default_billing(country: str, email: str = "", tax_id: str = "", geo: dict[str, str] | None = None, real_random: bool = False) -> dict[str, Any]:
    country = (country or "US").upper()
    rows = {
        "DE": ("Alex Meyer", "Friedrichstrasse 100", "Berlin", "10117", "BE"),
        "NL": ("Lars de Vries", "Damrak 1", "Amsterdam", "1012LG", "NH"),
        "IN": ("Arjun Sharma", "1 MG Road", "Bengaluru", "560001", "KA"),
        "BR": ("Lucas Silva", "Avenida Paulista 1000", "Sao Paulo", "01310-100", "SP"),
        "VN": ("Nguyen Minh Anh", "1 Nguyen Hue", "Ho Chi Minh City", "700000", "Ho Chi Minh"),
        "US": ("Alex Morgan", "1 Market Street", "San Francisco", "94105", "CA"),
        "GB": ("Alex Taylor", "10 King Street", "London", "SW1A 1AA", "London"),
        "FR": ("Alex Martin", "10 Rue de Rivoli", "Paris", "75001", "IDF"),
        "AU": ("Alex Wilson", "1 George Street", "Sydney", "2000", "NSW"),
        "JP": ("Haruto Sato", "1-1 Marunouchi", "Chiyoda", "100-0005", "Tokyo"),
        "KR": ("Minjun Kim", "30 Eulji-ro", "Seoul", "04533", "Seoul"),
        "PH": ("Sofia Torres", "1000 Roxas Boulevard", "Manila", "1000", "Metro Manila"),
        "BA": ("Adnan Hadzic", "Zmaja od Bosne 7", "Sarajevo", "71000", ""),
        "CH": ("Luca Meier", "Bahnhofstrasse 1", "Zurich", "8001", "ZH"),
    }
    geo = geo or {}
    name, line1, city, postal, state = rows.get(country, rows["US"])
    local_names = {
        "BA": ["Adnan Hadzic", "Amar Kovacevic", "Haris Basic", "Lejla Music", "Amina Softic", "Emir Delic"],
        "US": ["Alex Morgan", "Jordan Taylor", "Casey Wilson", "Taylor Reed"],
        "GB": ["Oliver Smith", "George Taylor", "Amelia Wilson", "Sophie Brown"],
        "BR": ["Lucas Silva", "Gabriel Santos", "Mariana Costa", "Ana Oliveira"],
        "IN": ["Arjun Sharma", "Rahul Verma", "Priya Patel", "Ananya Singh"],
        "KR": ["Minjun Kim", "Jihoon Lee", "Seo-yeon Kim", "Ji-woo Park"],
        "PH": ["Sofia Torres", "Maria Santos", "Angela Reyes", "Paolo Garcia", "Miguel Cruz"],
        "VN": ["Nguyen Minh Anh", "Tran Quoc Bao", "Le Thu Ha", "Pham Gia Huy"],
        "CH": ["Luca Meier", "Noah Keller", "Lea Schmid", "Mia Frei"],
    }
    if country in local_names:
        name = random.SystemRandom().choice(local_names[country])
    address_source = "country_profile"
    place_name = ""
    if geo:
        city = geo.get("city") or city
        postal = geo.get("postal") or postal
        state = geo.get("region") or state
    if real_random:
        resolved = resolve_public_address(
            country,
            str(geo.get("city") or city),
            str(geo.get("region") or state),
            str(geo.get("postal") or postal),
        )
        if resolved:
            line1 = resolved.get("line1") or line1
            city = resolved.get("city") or city
            postal = resolved.get("postal_code") or postal
            state = resolved.get("state") or state
            address_source = resolved.get("source") or "public_address_pool"
            place_name = resolved.get("name") or ""
    if country == "BA":
        postal_digits = re.sub(r"\D", "", str(postal or ""))
        if len(postal_digits) == 5:
            postal = postal_digits
        elif not real_random:
            postal = "71000"
        state = ""
    if country == "US":
        us_states = {
            "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA",
            "colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA",
            "hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS",
            "kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD","massachusetts":"MA",
            "michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO","montana":"MT",
            "nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ","new mexico":"NM",
            "new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK",
            "oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC",
            "south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT",
            "virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY",
            "district of columbia":"DC",
        }
        raw_state = str(state or "").strip()
        state = raw_state.upper() if re.fullmatch(r"[A-Za-z]{2}", raw_state) else us_states.get(raw_state.lower(), raw_state[:2].upper())
        postal_match = re.search(r"\b\d{5}(?:-\d{4})?\b", str(postal or ""))
        if postal_match:
            postal = postal_match.group(0)
    if country == "GB":
        state = ""
        postal = str(postal or "").strip().upper()
    if country == "BR":
        br_states = {
            "acre": "AC", "alagoas": "AL", "amapá": "AP", "amapa": "AP",
            "amazonas": "AM", "bahia": "BA", "ceará": "CE", "ceara": "CE",
            "distrito federal": "DF", "espírito santo": "ES", "espirito santo": "ES",
            "goiás": "GO", "goias": "GO", "maranhão": "MA", "maranhao": "MA",
            "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG",
            "pará": "PA", "para": "PA", "paraíba": "PB", "paraiba": "PB",
            "paraná": "PR", "parana": "PR", "pernambuco": "PE", "piauí": "PI",
            "piaui": "PI", "rio de janeiro": "RJ", "rio grande do norte": "RN",
            "rio grande do sul": "RS", "rondônia": "RO", "rondonia": "RO",
            "roraima": "RR", "santa catarina": "SC", "são paulo": "SP",
            "sao paulo": "SP", "sergipe": "SE", "tocantins": "TO",
        }
        raw_state = str(state or "").strip()
        state = raw_state.upper() if re.fullmatch(r"[A-Za-z]{2}", raw_state) else br_states.get(raw_state.lower(), "SP")
        postal = re.sub(r"\D", "", str(postal or ""))
        if len(postal) != 8:
            postal = "01310100"
    if country == "KR":
        postal_digits = re.sub(r"\D", "", str(postal or ""))
        if len(postal_digits) == 5:
            postal = postal_digits
        else:
            # Korean Stripe/PayPal billing requires the modern 5-digit postal code.
            name, line1, city, postal, state = rows["KR"]
            address_source = "country_profile_postal_fallback"
            place_name = "Lotte Hotel Seoul"
    if country == "PH":
        postal_digits = re.sub(r"\D", "", str(postal or ""))
        if len(postal_digits) == 4:
            postal = postal_digits
        else:
            postal = "1000"
        city = str(city or "Manila").strip() or "Manila"
        state = str(state or "Metro Manila").strip() or "Metro Manila"
    if country not in rows and address_source == "country_profile":
        line1 = "1 Main Street"
        postal = geo.get("postal") or "00000"
    billing = {
        "name": name,
        "email": email or f"checkout-{uuid.uuid4().hex[:10]}@outlook.com",
        "address": {
            "country": country,
            "line1": line1,
            "city": city,
            "postal_code": postal,
            "state": state,
        },
    }
    billing["_address_source"] = address_source
    if place_name:
        billing["_place_name"] = place_name
    if tax_id:
        billing["tax_id"] = re.sub(r"\D", "", tax_id)
    return billing


def _runtime_meta(ctx: dict, session_id: str) -> tuple[dict, dict]:
    guid, muid, sid = ctx.get("guid"), ctx.get("muid"), ctx.get("sid")
    if not (guid and muid and sid):
        guid, muid, sid = sc._gen_fingerprint()
        ctx["guid"], ctx["muid"], ctx["sid"] = guid, muid, sid
    runtime_version = ctx.get("runtime_version") or sc.DEFAULT_STRIPE_RUNTIME_VERSION
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))
    elements_session_id = ctx.get("elements_session_id", sc._gen_elements_session_id())
    elements_session_config_id = ctx.get("elements_session_config_id") or str(uuid.uuid4())
    checkout_config_id = ctx.get("config_id") or ""
    common = {
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "runtime_version": runtime_version,
        "stripe_js_id": stripe_js_id,
        "elements_session_id": elements_session_id,
        "elements_session_config_id": elements_session_config_id,
        "checkout_config_id": checkout_config_id,
    }
    attr = {
        "client_session_id": stripe_js_id,
        "checkout_session_id": session_id,
        "checkout_config_id": checkout_config_id,
        "elements_session_id": elements_session_id,
        "elements_session_config_id": elements_session_config_id,
    }
    return common, attr


def _hosted_checkout_init(http, session_id: str, pk: str, profile: dict, ctx: dict, log):
    """Refresh PIX using the same compact init shape as checkout.stripe.com."""
    resp = http.post(
        f"{sc.STRIPE_API}/v1/payment_pages/{session_id}/init",
        data={
            "key": pk,
            "eid": "NA",
            "browser_locale": profile.get("browser_locale") or "pt-BR",
            "browser_timezone": profile.get("browser_timezone") or "America/Sao_Paulo",
            "redirect_type": "url",
        },
        headers=sc._stripe_headers(),
        timeout=35,
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(
            f"PIX hosted init [{getattr(resp, 'status_code', '?')}]: "
            f"{(getattr(resp, 'text', '') or '')[:500]}"
        )
    payload = resp.json() or {}
    total = payload.get("total_summary") or {}
    amount = total.get("due")
    if amount is None:
        amount = (payload.get("invoice") or {}).get("amount_due")
    ctx.update({
        "runtime_version": HOSTED_CHECKOUT_RUNTIME_VERSION,
        "config_id": payload.get("config_id") or ctx.get("config_id") or "",
        "init_checksum": payload.get("init_checksum") or ctx.get("init_checksum") or "",
        "stripe_hosted_url": payload.get("stripe_hosted_url") or ctx.get("stripe_hosted_url") or "",
        "return_url": payload.get("return_url") or ctx.get("return_url") or "",
        "currency": str(payload.get("currency") or ctx.get("currency") or "brl").lower(),
        "checkout_amount": amount,
        "payment_method_types": sc._extract_payment_method_types(payload),
    })
    log(
        f"[stripe] PIX hosted init amount={amount} currency={ctx['currency']} "
        f"pm={ctx['payment_method_types']}"
    )
    return payload, HOSTED_CHECKOUT_STRIPE_VERSION, ctx


def create_provider_payment_method(
    http,
    pk: str,
    session_id: str,
    provider: str,
    version: str,
    ctx: dict,
    billing: dict,
    log: Callable[[str], None],
) -> str:
    """Create a local PaymentMethod before confirming the Payment Page.

    Submitting local-method billing details inline on every confirm can create
    a new setup attempt after manual approval.  A standalone ``pm_*`` keeps the
    approved submission bound to one PaymentMethod and lets the post-approval
    poll read the original QR/action.
    """
    provider = provider.lower()
    addr = billing.get("address") or {}
    common, attr = _runtime_meta(ctx, session_id)
    try:
        zero_due = int(str(ctx.get("checkout_amount") or 0)) == 0
    except (TypeError, ValueError):
        zero_due = str(ctx.get("checkout_amount")).strip() in {"0", "0.0", "0.00"}
    browser_exact = provider == "pix" and zero_due
    if browser_exact:
        # Captured from Stripe's own hosted Checkout PIX form.  The older
        # custom-checkout revision leaves mandate_options on the server-side
        # Checkout Session; forwarding them to /confirm makes Stripe discard
        # the valid recurring PIX setup attempt.
        data = {
            "type": "pix",
            "billing_details[name]": billing.get("name", ""),
            "billing_details[email]": billing.get("email", ""),
            "billing_details[address][country]": addr.get("country", "BR"),
            "billing_details[address][line1]": addr.get("line1", ""),
            "billing_details[address][line2]": addr.get("line2", ""),
            "billing_details[address][city]": addr.get("city", ""),
            "billing_details[address][postal_code]": re.sub(r"\D", "", str(addr.get("postal_code", ""))),
            "billing_details[address][state]": addr.get("state", "SP"),
            "billing_details[tax_id]": _format_cpf(billing.get("tax_id", "")),
            "_stripe_version": HOSTED_CHECKOUT_STRIPE_VERSION,
            "key": pk,
            "payment_user_agent": (
                f"stripe.js/{HOSTED_CHECKOUT_RUNTIME_VERSION}; "
                f"stripe-js-v3/{HOSTED_CHECKOUT_RUNTIME_VERSION}; checkout"
            ),
            "client_attribution_metadata[client_session_id]": attr["client_session_id"],
            "client_attribution_metadata[checkout_session_id]": attr["checkout_session_id"],
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[checkout_config_id]": attr["checkout_config_id"],
        }
        data = {key: value for key, value in data.items() if value not in (None, "")}
        resp = http.post(
            f"{sc.STRIPE_API}/v1/payment_methods",
            data=data,
            headers=sc._stripe_headers(),
            timeout=35,
        )
        if getattr(resp, "status_code", 0) != 200:
            raise RuntimeError(
                f"pix PaymentMethod 创建失败 [{getattr(resp, 'status_code', '?')}]: "
                f"{(getattr(resp, 'text', '') or '')[:500]}"
            )
        payment_method_id = str((resp.json() or {}).get("id") or "")
        if not payment_method_id.startswith("pm_"):
            raise RuntimeError("pix PaymentMethod 未返回 pm_id")
        ctx["payment_method_id"] = payment_method_id
        ctx["pix_hosted_exact"] = True
        log(f"[stripe] pix hosted-checkout payment_method: {payment_method_id}")
        return payment_method_id
    data = {
        "type": provider,
        "billing_details[name]": billing.get("name", ""),
        "billing_details[email]": billing.get("email", ""),
        "billing_details[address][country]": addr.get("country", ""),
        "billing_details[address][line1]": addr.get("line1", ""),
        "billing_details[address][city]": addr.get("city", ""),
        "billing_details[address][postal_code]": addr.get("postal_code", ""),
        "billing_details[address][state]": addr.get("state", ""),
        "payment_user_agent": (
            f"stripe.js/{common['runtime_version']}; stripe-js-v3/{common['runtime_version']}; "
            "payment-element; deferred-intent"
        ),
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[client_session_id]": attr["client_session_id"],
        "client_attribution_metadata[checkout_session_id]": attr["checkout_session_id"],
        "client_attribution_metadata[checkout_config_id]": attr["checkout_config_id"],
        "client_attribution_metadata[elements_session_id]": attr["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": attr["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "key": pk,
        "_stripe_version": sc.STRIPE_VERSION_FULL,
    }
    if billing.get("tax_id"):
        data["billing_details[tax_id]"] = billing["tax_id"]
    data = {key: value for key, value in data.items() if value not in (None, "")}
    resp = http.post(
        f"{sc.STRIPE_API}/v1/payment_methods",
        data=data,
        headers=sc._stripe_headers(),
        timeout=35,
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(
            f"{provider} PaymentMethod 创建失败 [{getattr(resp, 'status_code', '?')}]: "
            f"{(getattr(resp, 'text', '') or '')[:500]}"
        )
    payload = resp.json() or {}
    payment_method_id = str(payload.get("id") or "")
    if not payment_method_id.startswith("pm_"):
        raise RuntimeError(f"{provider} PaymentMethod 未返回 pm_id")
    ctx["payment_method_id"] = payment_method_id
    log(f"[stripe] {provider} payment_method: {payment_method_id}")
    return payment_method_id


def confirm_provider_payment(
    http,
    pk: str,
    session_id: str,
    provider: str,
    init_resp: dict,
    version: str,
    ctx: dict,
    profile: dict,
    log: Callable[[str], None],
    *,
    ideal_bank: str = "",
    payment_method_id: str = "",
) -> dict:
    provider = provider.lower()
    billing = ctx.get("billing") or {}
    addr = billing.get("address") or {}
    common, attr = _runtime_meta(ctx, session_id)
    locale_short = ctx.get("locale") or sc._locale_short(profile)
    total = init_resp.get("total_summary") or {}
    amount = ctx.get("checkout_amount")
    if amount is None:
        amount = total.get("due")
    if amount is None:
        amount = (init_resp.get("invoice") or {}).get("amount_due", 0)

    stripe_hosted_url = ctx.get("stripe_hosted_url") or init_resp.get("stripe_hosted_url") or ""
    success_return_url = ctx.get("return_url") or init_resp.get("return_url") or init_resp.get("url") or ""
    return_url = stripe_hosted_url or success_return_url or "https://chatgpt.com/"

    try:
        exact_zero_due = int(str(amount or 0)) == 0
    except (TypeError, ValueError):
        exact_zero_due = str(amount).strip() in {"0", "0.0", "0.00"}
    if provider == "pix" and payment_method_id and exact_zero_due and ctx.get("pix_hosted_exact"):
        data = {
            "eid": "NA",
            "payment_method": payment_method_id,
            "expected_amount": "0",
            "expected_payment_method_type": "pix",
            "return_url": _hosted_provider_return_url(stripe_hosted_url or return_url, "pix"),
            "_stripe_version": HOSTED_CHECKOUT_STRIPE_VERSION,
            "key": pk,
            "version": HOSTED_CHECKOUT_RUNTIME_VERSION,
            "init_checksum": init_resp.get("init_checksum") or ctx.get("init_checksum") or "",
            "client_attribution_metadata[client_session_id]": attr["client_session_id"],
            "client_attribution_metadata[checkout_session_id]": attr["checkout_session_id"],
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[checkout_config_id]": attr["checkout_config_id"],
            "link_brand": "link",
        }
        data = {key: value for key, value in data.items() if value not in (None, "")}
        log("[stripe] pix confirm strategy=hosted_exact zero_due=True")
        resp = http.post(
            f"{sc.STRIPE_API}/v1/payment_pages/{session_id}/confirm",
            data=data,
            headers=sc._stripe_headers(),
            timeout=40,
        )
        if getattr(resp, "status_code", 0) != 200:
            raise RuntimeError(
                f"pix hosted confirm [{getattr(resp, 'status_code', '?')}]: "
                f"{(getattr(resp, 'text', '') or '')[:500]}"
            )
        return resp.json()

    data = {
        "guid": common["guid"],
        "muid": common["muid"],
        "sid": common["sid"],
        "expected_amount": str(amount or 0),
        "expected_payment_method_type": provider,
        "key": pk,
        "_stripe_version": sc.STRIPE_VERSION_FULL,
        "init_checksum": init_resp.get("init_checksum", ""),
        "version": common["runtime_version"],
        "return_url": return_url,
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": common["stripe_js_id"],
        "elements_session_client[locale]": locale_short,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[session_id]": common["elements_session_id"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "client_attribution_metadata[client_session_id]": attr["client_session_id"],
        "client_attribution_metadata[checkout_session_id]": attr["checkout_session_id"],
        "client_attribution_metadata[checkout_config_id]": attr["checkout_config_id"],
        "client_attribution_metadata[elements_session_id]": attr["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": attr["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "payment_method_data[type]": provider,
        "payment_method_data[billing_details][name]": billing.get("name", ""),
        "payment_method_data[billing_details][email]": billing.get("email", ""),
        "payment_method_data[billing_details][address][country]": addr.get("country", "US"),
        "payment_method_data[billing_details][address][line1]": addr.get("line1", ""),
        "payment_method_data[billing_details][address][city]": addr.get("city", ""),
        "payment_method_data[billing_details][address][postal_code]": addr.get("postal_code", ""),
        "payment_method_data[payment_user_agent]": (
            f"stripe.js/{common['runtime_version']}; stripe-js-v3/{common['runtime_version']}; "
            "payment-element; deferred-intent"
        ),
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(random.randint(25000, 55000)),
    }
    if addr.get("state"):
        data["payment_method_data[billing_details][address][state]"] = addr["state"]
    for key, value in attr.items():
        data[f"payment_method_data[client_attribution_metadata][{key}]"] = value
    data["payment_method_data[client_attribution_metadata][merchant_integration_source]"] = "elements"
    data["payment_method_data[client_attribution_metadata][merchant_integration_subtype]"] = "payment-element"
    data["payment_method_data[client_attribution_metadata][merchant_integration_version]"] = "2021"
    data["payment_method_data[client_attribution_metadata][payment_intent_creation_flow]"] = "deferred"
    data["payment_method_data[client_attribution_metadata][payment_method_selection_flow]"] = "automatic"
    if provider == "ideal" and ideal_bank:
        # iDEAL 2.0 performs issuer selection on the hosted redirect page.
        # Sending the legacy `ideal[bank]` field can make newer PaymentIntent /
        # SetupIntent revisions reject the confirm payload.
        log(f"[ideal] 忽略旧版银行参数 {ideal_bank}，由 iDEAL 跳转页选择银行")
    if billing.get("tax_id"):
        data["payment_method_data[billing_details][tax_id]"] = billing["tax_id"]
    consent = init_resp.get("consent_collection") or {}
    if consent.get("terms_of_service") not in (None, "", "none"):
        data["consent[terms_of_service]"] = "accepted"
    data.update(ctx.get("elements_options_client") or sc._elements_options_client_payload())

    if payment_method_id:
        data["payment_method"] = payment_method_id
        for key in list(data):
            if key.startswith("payment_method_data["):
                data.pop(key, None)

    setup_future_usage = init_resp.get("setup_future_usage_for_payment_method_type") or {}
    if isinstance(setup_future_usage, dict) and provider in setup_future_usage:
        data[f"setup_future_usage_for_payment_method_type[{provider}]"] = setup_future_usage[provider]
    setup_intent = init_resp.get("setup_intent") or {}
    if isinstance(setup_intent, dict) and setup_intent.get("usage"):
        data["setup_intent[usage]"] = setup_intent["usage"]
    payment_method_options = init_resp.get("payment_method_options") or {}
    if isinstance(payment_method_options, dict) and isinstance(payment_method_options.get(provider), dict):
        ctx["provider_payment_method_options"] = dict(payment_method_options[provider])
        if payment_method_id:
            data.update(
                flatten_stripe_params(
                    payment_method_options[provider],
                    f"payment_method_options[{provider}]",
                )
            )

    # A zero-due promotional Checkout is a setup/mandate flow.  Keep the
    # provider-specific payment_method_options because PIX/UPI revisions may
    # require mandate_options to produce the QR/action.  The adaptive confirm
    # loop below removes only parameters Stripe explicitly reports as unknown.
    try:
        zero_due = int(str(amount or 0)) == 0
    except (TypeError, ValueError):
        zero_due = str(amount).strip() in {"0", "0.0", "0.00"}
    if zero_due:
        if provider == "ideal":
            setup_usage = (
                (init_resp.get("setup_future_usage_for_payment_method_type") or {}).get("ideal")
                if isinstance(init_resp.get("setup_future_usage_for_payment_method_type"), dict)
                else ""
            ) or ((init_resp.get("setup_intent") or {}).get("usage") if isinstance(init_resp.get("setup_intent"), dict) else "")
            log(
                "[ideal] 0 元 Checkout 使用 SetupIntent；"
                f"setup_usage={setup_usage or 'merchant-default'}，银行选择由 redirect_to_url 完成"
            )
        if provider == "upi":
            upi_options = (
                payment_method_options.get("upi")
                if isinstance(payment_method_options, dict)
                else None
            ) or {}
            mandate_options = upi_options.get("mandate_options") or {}
            ctx["server_upi_mandate_present"] = bool(mandate_options)
            log(
                "[upi] 0 元 Checkout 使用 SetupIntent；"
                f"服务端 mandate_options={'present' if mandate_options else 'missing'}。"
                "付费 UPI 使用 PaymentIntent，可由首笔支付生成授权；"
                "0 元授权需要 Checkout 创建阶段写入 UPI mandate 配置。"
            )
        data.pop("elements_options_client[saved_payment_method][enable_save]", None)
        data.pop("elements_options_client[saved_payment_method][enable_redisplay]", None)
        data.pop("client_attribution_metadata[payment_intent_creation_flow]", None)
        # PIX currently returns the recurring mandate structure needed by this
        # merchant. Do not synthesize UPI mandate fields locally: when the
        # Checkout session does not expose them, Stripe rejects the resulting
        # SetupIntent with generic_decline/requires_payment_method.
        if provider == "pix":
            data.setdefault(
                f"setup_future_usage_for_payment_method_type[{provider}]",
                "off_session",
            )
            data.setdefault("setup_intent[usage]", "off_session")
            try:
                mandate_amount = max(1, int(str(ctx.get("original_checkout_amount") or 1)))
            except (TypeError, ValueError):
                mandate_amount = 1
            if mandate_amount <= 1:
                mandate_amount = 9990 if provider == "pix" else 199900
            local_options = dict(ctx.get("provider_payment_method_options") or {})
            mandate = dict(local_options.get("mandate_options") or {})
            mandate.setdefault("amount", mandate_amount)
            mandate.setdefault("amount_type", "maximum")
            if provider == "pix":
                mandate.setdefault("payment_schedule", "monthly")
                mandate.setdefault("start_date", (date.today() + timedelta(days=3)).isoformat())
            else:
                mandate.setdefault("description", "Subscription payment")
                mandate.setdefault("end_date", int(time.time()) + 3650 * 24 * 60 * 60)
            local_options["mandate_options"] = mandate
            ctx["provider_payment_method_options"] = local_options
            if payment_method_id:
                data.update(flatten_stripe_params(local_options, f"payment_method_options[{provider}]"))
            else:
                # Checkout's Payment Page endpoint rejects root
                # payment_method_options on this merchant revision.  Browser
                # submissions carry local mandate fields with the inline
                # PaymentMethod data instead.
                data.update(flatten_stripe_params(local_options, f"payment_method_data[{provider}]"))
            data.pop("_stripe_version", None)
    option_keys = sorted(
        key for key in data
        if key.startswith("payment_method_options[")
        or key.startswith(f"payment_method_data[{provider}][mandate_options]")
    )
    log(
        f"[stripe] {provider} confirm strategy={'pm_id' if payment_method_id else 'inline'} "
        f"zero_due={zero_due} setup_usage={data.get('setup_intent[usage]', '')} "
        f"provider_options={len(option_keys)}"
    )

    resp = None
    confirm_headers = dict(sc._stripe_headers())
    if zero_due and provider in {"pix", "upi"}:
        confirm_headers["Stripe-Version"] = LOCAL_MANDATE_STRIPE_VERSION_FULL
    for _ in range(5):
        resp = http.post(
            f"{sc.STRIPE_API}/v1/payment_pages/{session_id}/confirm",
            data=data,
            headers=confirm_headers,
            timeout=40,
        )
        if getattr(resp, "status_code", 0) == 200:
            break
        unknown_param = sc._stripe_unknown_param(resp)
        if not unknown_param:
            break
        removed = False
        for key in list(data):
            if key == unknown_param or key.startswith(f"{unknown_param}["):
                data.pop(key, None)
                removed = True
        if not removed:
            break
        log(f"[stripe] confirm 移除当前版本不支持参数：{unknown_param}")
    if resp is None or getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(
            f"{provider} confirm [{getattr(resp, 'status_code', '?')}]: "
            f"{(getattr(resp, 'text', '') or '')[:500]}"
        )
    return resp.json()



def canonical_ideal_payment_url(value: str) -> str:
    """Return the public iDEAL payment page instead of the raw tx endpoint."""
    raw = str(value or "").strip()
    if not raw:
        return raw
    parsed = urlsplit(raw)
    host = parsed.netloc.lower().rstrip(".")
    if host == "pay.ideal.nl" and "/transactions/" in parsed.path:
        return raw
    if host == "tx.ideal.nl":
        transaction_url = f"{parsed.scheme or 'https'}://{parsed.netloc}{parsed.path}"
        wrapped = "https://pay.ideal.nl/transactions/" + quote(transaction_url, safe="")
        if parsed.query:
            wrapped += "?" + parsed.query
        return wrapped
    return raw

def enrich_ideal_redirect(http, redirect_url: str, log: Callable[[str], None]) -> dict[str, Any]:
    """Resolve the iDEAL hosted page and extract its Canvas QR payload."""
    if not redirect_url:
        return {}
    try:
        hosted_url = canonical_ideal_payment_url(redirect_url)
        if hosted_url != redirect_url:
            log("[ideal] raw transaction URL converted to pay.ideal.nl hosted page")
        page = http.get(
            hosted_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": sc.CHROME_UA,
            },
            timeout=45,
            allow_redirects=True,
        )
        final_url = canonical_ideal_payment_url(str(getattr(page, "url", "") or hosted_url))
        parsed = urlsplit(final_url)
        prefix = "/transactions/"
        if parsed.netloc.lower() != "pay.ideal.nl" or prefix not in parsed.path:
            log(f"[ideal] 跳转页已生成：{parsed.netloc or 'unknown-host'}")
            return {"provider_redirect_url": final_url}
        transaction_path = parsed.path.split(prefix, 1)[1]
        api_url = f"https://pay.ideal.nl/api/v1/transactions/{transaction_path}/initiate"
        init_resp = http.post(
            api_url,
            json={
                "deviceInfo": {
                    "language": "nl-NL",
                    "timeZone": "Europe/Amsterdam",
                    "screenWidth": 1280,
                    "screenHeight": 720,
                    "screenAvailableWidth": 1280,
                    "screenAvailableHeight": 720,
                    "colorDepth": 24,
                },
                "httpReferrer": "https://some-referrer",
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://pay.ideal.nl",
                "Referer": final_url,
                "User-Agent": sc.CHROME_UA,
            },
            timeout=45,
        )
        if getattr(init_resp, "status_code", 0) != 200:
            log(f"[ideal] 二维码初始化 HTTP {getattr(init_resp, 'status_code', '?')}，保留支付页链接")
            return {"provider_redirect_url": final_url}
        payload = init_resp.json() or {}
        qr_url = str(payload.get("qrCodeUrl") or "")
        issuers = payload.get("supportedIssuers") or []
        result: dict[str, Any] = {
            "provider_redirect_url": final_url,
            "ideal_qr_url": qr_url,
            "ideal_transaction_url": qr_url,
            "qr_data": qr_url,
            "ideal_creditor_name": str(payload.get("creditorName") or ""),
            "ideal_amount": payload.get("amount"),
            "ideal_transaction_flow": str(payload.get("transactionFlow") or ""),
            "ideal_supported_issuers": [
                {
                    "id": str(item.get("id") or ""),
                    "deeplink": str(item.get("deeplink") or ""),
                    "availability": str(item.get("availabilityStatus") or ""),
                }
                for item in issuers
                if isinstance(item, dict)
            ],
        }
        if qr_url:
            try:
                import qrcode
                from qrcode.constants import ERROR_CORRECT_L
                image = qrcode.QRCode(
                    version=None,
                    error_correction=ERROR_CORRECT_L,
                    box_size=10,
                    border=4,
                )
                image.add_data(qr_url)
                image.make(fit=True)
                image = image.make_image(fill_color="black", back_color="white")
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                result["qr_image_png"] = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
                try:
                    import qrcode.image.svg
                    svg_qr = qrcode.QRCode(
                        version=None,
                        error_correction=ERROR_CORRECT_L,
                        box_size=10,
                        border=4,
                    )
                    svg_qr.add_data(qr_url)
                    svg_qr.make(fit=True)
                    svg_buffer = io.BytesIO()
                    svg_qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(svg_buffer)
                    result["qr_image_svg"] = "data:image/svg+xml;base64," + base64.b64encode(svg_buffer.getvalue()).decode("ascii")
                except Exception:
                    pass
            except Exception as exc:
                log(f"[ideal] 二维码图片生成提示：{type(exc).__name__}")
            log(f"[ideal] 二维码已提取，银行入口 {len(result['ideal_supported_issuers'])} 个")
        else:
            log("[ideal] 支付页已打开，但初始化响应未包含 qrCodeUrl")
        return result
    except Exception as exc:
        log(f"[ideal] 二维码提取提示：{type(exc).__name__}: {str(exc)[:180]}")
        return {"provider_redirect_url": canonical_ideal_payment_url(redirect_url)}


def extract_provider_result(data: dict, provider: str) -> dict[str, Any]:
    provider = provider.lower()
    na = sc._find_next_action(data)
    redirect = (na.get("redirect_to_url") or {}).get("url") or ""
    if not redirect:
        redirect = sc.extract_redirect_url(data)
    result: dict[str, Any] = {
        "provider": provider,
        "provider_redirect_url": redirect,
        "next_action_type": na.get("type") or "",
    }
    if provider == "pix":
        qr = na.get("pix_display_qr_code") or {}
        result.update({
            "provider_redirect_url": qr.get("hosted_instructions_url") or redirect,
            "qr_data": qr.get("data") or "",
            "qr_image_png": qr.get("image_url_png") or "",
            "qr_image_svg": qr.get("image_url_svg") or "",
            "expires_at": qr.get("expires_at"),
        })
    elif provider == "upi":
        upi = na.get("upi_handle_redirect_or_display_qr_code") or {}
        qr = upi.get("qr_code") or {}
        result.update({
            "provider_redirect_url": upi.get("hosted_instructions_url") or redirect,
            "qr_image_png": qr.get("image_url_png") or "",
            "qr_image_svg": qr.get("image_url_svg") or "",
            "expires_at": qr.get("expires_at"),
        })
    elif provider == "momo":
        momo = (
            na.get("momo_handle_redirect_or_display_qr_code")
            or na.get("momo_display_qr_code")
            or na.get("momo")
            or {}
        )
        qr = momo.get("qr_code") if isinstance(momo, dict) else {}
        qr = qr if isinstance(qr, dict) else {}
        if isinstance(momo, dict):
            result.update({
                "provider_redirect_url": (
                    momo.get("hosted_instructions_url")
                    or momo.get("redirect_url")
                    or momo.get("url")
                    or redirect
                ),
                "qr_data": momo.get("data") or qr.get("data") or "",
                "qr_image_png": momo.get("image_url_png") or qr.get("image_url_png") or "",
                "qr_image_svg": momo.get("image_url_svg") or qr.get("image_url_svg") or "",
                "expires_at": momo.get("expires_at") or qr.get("expires_at"),
            })
    # Only accept Stripe next_action.redirect_to_url (or the generic redirect
    # extractor). A broad search for the word "ideal" also matches static
    # assets such as icon-pm-ideal@3x.png and creates a false success.
    return result


def stripe_to_provider(
    http,
    session_id: str,
    provider: str,
    *,
    billing: dict,
    country: str,
    promotion_billing: dict | None = None,
    payment_billing: dict | None = None,
    payment_http=None,
    chatgpt_http=None,
    access_token: str = "",
    stage1: dict | None = None,
    approve_callback=None,
    apply_promo_callback=None,
    ideal_bank: str = "",
    require_zero_due: bool = False,
    local_method_strategy: str = "standalone",
    log: Callable[[str], None] = lambda m: None,
) -> dict[str, Any]:
    provider = provider.lower()
    local_method_strategy = str(local_method_strategy or "standalone").lower()
    late_promo = provider in {"pix", "upi", "momo"} and local_method_strategy == "late_promo"
    stage1 = stage1 or {}
    if provider == "paypal":
        redirect, pk, paypal_ctx = sc.stripe_to_paypal_redirect(
            http,
            session_id,
            billing=billing,
            promotion_billing=promotion_billing,
            payment_billing=payment_billing,
            payment_http=payment_http,
            country=country,
            chatgpt_http=chatgpt_http,
            access_token=access_token,
            publishable_key=str(stage1.get("publishable_key") or ""),
            processor_entity=str(stage1.get("processor_entity") or ""),
            approve_callback=approve_callback,
            apply_promo_callback=apply_promo_callback,
            require_zero_due=require_zero_due,
            log=log,
        )
        return {
            "provider": provider,
            "provider_redirect_url": redirect,
            "stripe_redirect_url": paypal_ctx.get("stripe_redirect_url") or "",
            "payment_method_types": paypal_ctx.get("payment_method_types") or [],
            "processor_entity": paypal_ctx.get("processor_entity") or stage1.get("processor_entity") or "",
            "stripe_publishable_key": pk,
            "checkout_amount": paypal_ctx.get("checkout_amount"),
            "checkout_currency": str(paypal_ctx.get("currency") or "").upper(),
            "paypal_billing_country": str(paypal_ctx.get("paypal_billing_country") or "").upper(),
            "promo_requested": require_zero_due,
            "promo_applied": paypal_ctx.get("promo_applied") if require_zero_due else None,
        }

    profile = sc._profile(country)
    pk = str(stage1.get("publishable_key") or "") or sc.verify_pk(http, session_id, log)
    init_data, version, ctx = sc.init_checkout(http, session_id, pk, profile, log)
    methods = ctx.get("payment_method_types") or []
    if provider not in methods:
        raise RuntimeError(f"当前 checkout 未开放 {provider}，可用方式：{', '.join(methods) or 'card'}")
    sc.fetch_elements_session(http, pk, session_id, ctx, version, profile, log)
    processor = str(stage1.get("processor_entity") or "") or sc._entity_from_return_url(ctx.get("return_url") or init_data.get("return_url") or "") or "openai_llc"
    if apply_promo_callback and not late_promo:
        original_checkout_amount = ctx.get("checkout_amount")
        try:
            already_zero = int(str(original_checkout_amount or 0)) == 0
        except (TypeError, ValueError):
            already_zero = str(original_checkout_amount).strip() in {"0", "0.0", "0.00"}
        if already_zero:
            log("[promo] Checkout 创建时金额已为 0，保留 Stage1 原生 mandate 配置")
            ctx["original_checkout_amount"] = original_checkout_amount
        else:
            apply_promo_callback(processor)
            log("[promo] 优惠更新完成，重新初始化 Stripe 并获取新鲜 elements_session_id")
            init_data, version, ctx = sc.init_checkout(http, session_id, pk, profile, log)
            ctx["original_checkout_amount"] = original_checkout_amount
            methods = ctx.get("payment_method_types") or []
            if provider not in methods:
                raise RuntimeError(f"应用优惠后 checkout 未开放 {provider}，可用方式：{', '.join(methods) or 'card'}")
            sc.fetch_elements_session(http, pk, session_id, ctx, version, profile, log)
    if provider == "pix" and require_zero_due and not late_promo:
        original_checkout_amount = ctx.get("original_checkout_amount")
        init_data, version, ctx = _hosted_checkout_init(
            http, session_id, pk, profile, ctx, log
        )
        ctx["original_checkout_amount"] = original_checkout_amount
        methods = ctx.get("payment_method_types") or []
        if "pix" not in methods:
            raise RuntimeError(
                f"PIX hosted init 未开放 pix，可用方式：{', '.join(methods) or 'card'}"
            )
    ctx["billing"] = billing
    sc.update_tax_region(http, session_id, pk, version, ctx, billing, profile, log)
    checkout_amount = ctx.get("checkout_amount")
    if ctx.get("original_checkout_amount") in (None, "", 0, "0"):
        ctx["original_checkout_amount"] = checkout_amount
    promo_applied = None
    if require_zero_due:
        if late_promo:
            log(f"[promo] 本轮延后到 PaymentMethod 挂载后应用优惠，当前 amount={checkout_amount}")
        else:
            if checkout_amount is None:
                raise RuntimeError("优惠金额校验失败：Stripe 未返回今日应付金额")
            try:
                promo_applied = int(str(checkout_amount)) == 0
            except ValueError:
                promo_applied = str(checkout_amount).strip() in {"0", "0.0", "0.00"}
            if not promo_applied:
                raise RuntimeError(f"Plus 首月免费优惠未生效：Stripe 今日应付 amount={checkout_amount}")
            if provider == "upi":
                upi_options = (
                    (init_data.get("payment_method_options") or {}).get("upi")
                    if isinstance(init_data, dict)
                    else None
                ) or {}
                if not isinstance(upi_options, dict) or not upi_options.get("mandate_options"):
                    raise RuntimeError(
                        "当前 Checkout 未开放 UPI 0 元 mandate；已停止提交无效 SetupIntent。"
                        "UPI 仅在 Stripe 返回 mandate_options 时继续生成。"
                    )
            if provider == "pix":
                log("[promo] 第 5/7 步：返回 BR 主链路并校验通过，Stripe 今日应付 amount=0")
            else:
                log("[promo] Plus 首月免费校验通过：Stripe 今日应付 amount=0")
    sc.snapshot_billing(chatgpt_http, access_token, session_id, processor, billing, log)

    if provider == "pix":
        log("[pix] 第 6/7 步：创建独立 PIX PaymentMethod")
    elif provider == "upi":
        log("[upi] 正在创建独立 UPI PaymentMethod")
    elif provider == "momo":
        log("[momo] 正在创建 MoMo 支付请求")
    elif provider == "twint":
        log("[twint] 正在创建独立 TWINT PaymentMethod")
    payment_method_id = ""
    if provider in {"pix", "upi", "momo", "twint"} and local_method_strategy == "standalone":
        payment_method_id = create_provider_payment_method(
            http,
            pk,
            session_id,
            provider,
            version,
            ctx,
            billing,
            log,
        )
    elif provider in {"pix", "upi", "momo", "twint"}:
        log(f"[stripe] {provider} 本轮使用 {local_method_strategy} inline PaymentMethod 提交")
    confirm = confirm_provider_payment(
        http, pk, session_id, provider, init_data, version, ctx, profile, log,
        ideal_bank=ideal_bank,
        payment_method_id=payment_method_id,
    )
    initial_submission = confirm.get("submission_attempt") or {}
    initial_setup = confirm.get("setup_intent") or {}
    initial_action = sc._find_next_action(confirm)
    log(
        f"[{provider}] confirm 返回：submission_state={initial_submission.get('state') or ''} "
        f"setup_status={initial_setup.get('status') or ''} "
        f"next_action={initial_action.get('type') or ''} "
        f"failure={provider_failure_detail(confirm)}"
    )
    if not payment_method_id:
        for source in (initial_setup, initial_submission, confirm):
            if not isinstance(source, dict):
                continue
            cand = source.get("payment_method")
            if isinstance(cand, dict):
                cand = cand.get("id")
            cand = str(cand or "")
            if cand.startswith("pm_"):
                payment_method_id = cand
                ctx["payment_method_id"] = cand
                log(f"[{provider}] 从 confirm 回填 payment_method={cand}")
                break
    out = extract_provider_result(confirm, provider)
    if not out.get("provider_redirect_url") and not out.get("qr_image_png") and not out.get("qr_data"):
        sub = confirm.get("submission_attempt") or {}
        if sub.get("state") == "requires_approval" and approve_callback:
            if late_promo and apply_promo_callback:
                log(f"[{provider}] PaymentMethod 已挂载，开始延后应用优惠")
                apply_promo_callback(processor)
                promo_init, _promo_version, promo_ctx = sc.init_checkout(http, session_id, pk, profile, log)
                promo_amount = promo_ctx.get("checkout_amount")
                try:
                    promo_applied = int(str(promo_amount or 0)) == 0
                except (TypeError, ValueError):
                    promo_applied = str(promo_amount).strip() in {"0", "0.0", "0.00"}
                if not promo_applied:
                    raise RuntimeError(f"延后应用优惠未归零：Stripe 今日应付 amount={promo_amount}")
                checkout_amount = promo_amount
                log(f"[{provider}] 延后优惠金额校验通过：Stripe 今日应付 amount=0")
            approve_callback(processor)
            # The first confirm has already attached the local PaymentMethod.
            # Re-confirming with inline payment_method_data creates a second
            # setup attempt and commonly replaces the approved attempt with
            # `last_setup_error`. Poll the approved attempt first.
            confirm = sc.poll_payment_page_after_approve(
                http,
                pk,
                session_id,
                log,
                ctx=ctx,
                max_attempts=3,
            )
            out = extract_provider_result(confirm, provider)
            decline = payment_decline(confirm)
            failure_detail = provider_failure_detail(confirm)
            if failure_detail:
                log(f"[{provider}] approval 后失败详情：{failure_detail}")

            setup_status = str(((confirm.get("setup_intent") or {}).get("status") or "")).lower()
            need_setup_recover = (
                provider in {"upi", "pix", "momo"}
                and not out.get("provider_redirect_url")
                and not out.get("qr_image_png")
                and not out.get("qr_data")
                and (
                    setup_status in {"requires_payment_method", "requires_confirmation", ""}
                    or bool(decline)
                    or "requires_payment_method" in failure_detail
                    or "generic_decline" in failure_detail
                )
            )
            if need_setup_recover:
                recover_pm = payment_method_id or str(ctx.get("payment_method_id") or "")
                log(
                    f"[{provider}] approval 后 SetupIntent 未产出动作，"
                    f"尝试直连补交 pm={bool(recover_pm)} setup_status={setup_status or '-'}"
                )
                confirm = confirm_local_setup_intent(
                    http,
                    pk,
                    provider,
                    confirm,
                    recover_pm,
                    ctx,
                    log,
                )
                out = extract_provider_result(confirm, provider)
                decline = payment_decline(confirm)
                failure_detail = provider_failure_detail(confirm)
                if failure_detail:
                    log(f"[{provider}] SetupIntent 补交后详情：{failure_detail}")
            if decline and provider == "pix" and not (
                out.get("provider_redirect_url") or out.get("qr_image_png") or out.get("qr_data")
            ):
                log("[pix] approval 后原始 PaymentMethod 被支付通道拒绝，交给外层更换代理、CPF 并重建完整链路")
    out.update({
        "payment_method_types": ctx.get("payment_method_types") or methods,
        "processor_entity": processor,
        "stripe_publishable_key": pk,
        "checkout_amount": checkout_amount,
        "checkout_currency": str(ctx.get("currency") or "").upper(),
        "promo_requested": require_zero_due,
        "promo_applied": promo_applied,
    })
    if provider == "ideal" and out.get("provider_redirect_url"):
        out.update(enrich_ideal_redirect(http, str(out.get("provider_redirect_url") or ""), log))
    if not out.get("provider_redirect_url") and not out.get("qr_image_png") and not out.get("qr_data"):
        decline = payment_decline(confirm)
        failure_detail = provider_failure_detail(confirm)
        if decline or failure_detail:
            raise RuntimeError(
                f"{provider} 支付通道拒绝："
                f"{provider_failure_detail(confirm) or decline.get('message') or decline.get('decline_code') or 'provider decline'}"
            )
        if provider == "ideal":
            setup_status = str(((confirm.get("setup_intent") or {}).get("status") or ""))
            submission_state = str(((confirm.get("submission_attempt") or {}).get("state") or ""))
            raise RuntimeError(
                "iDEAL 未返回银行跳转地址："
                f"setup_status={setup_status or '-'}; submission_state={submission_state or '-'}"
            )
        raise RuntimeError(f"{provider} 未返回跳转或二维码：{json.dumps(confirm, ensure_ascii=False)[:500]}")
    return out
