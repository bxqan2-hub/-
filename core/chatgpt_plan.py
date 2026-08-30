# -*- coding: utf-8 -*-
"""ChatGPT 账号套餐/试用资格查询。"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlparse

from core.openai_auth import detect_account_unusable_response_body
from core.session import BrowserSession

logger = logging.getLogger(__name__)

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
SUBSCRIPTIONS_PATH = "/backend-api/subscriptions"
WHAM_USAGE_PATH = "/backend-api/wham/usage"
ME_PATH = "/backend-api/me"
AUTHORITATIVE_STATUS_MAX_ATTEMPTS = 2
WHAM_USAGE_MAX_ATTEMPTS = 3
WHAM_USAGE_RETRY_DELAYS = (0.2, 0.5)
WHAM_USAGE_USER_AGENT = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"

_CONCLUSIVE_ACCOUNT_CODES = {
    "account_banned", "account_deactivated", "account_deleted", "account_disabled",
    "account_suspended", "user_banned", "user_deactivated", "user_deleted",
    "user_disabled", "user_suspended", "access_token_expired", "authentication_expired",
    "invalid_token", "session_expired", "token_expired", "auth_revoked",
    "authentication_revoked", "credentials_revoked", "session_revoked", "token_revoked",
}
_ACCOUNT_UNUSABLE_CODES = {
    code for code in _CONCLUSIVE_ACCOUNT_CODES
    if code.startswith(("account_", "user_"))
}
_CREDENTIAL_UNUSABLE_CODES = _CONCLUSIVE_ACCOUNT_CODES - _ACCOUNT_UNUSABLE_CODES


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_token(token: str) -> str:
    token = (token or "").strip().strip('"').strip("'")
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _mask_proxy(proxy: str) -> str:
    """返回可用于日志/API 结果的代理摘要，不泄露用户名和密码。"""
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        auth = "***:***@" if parsed.username or parsed.password else ""
        return f"{scheme}{auth}{host}{port}" or "***"
    except Exception:
        return "***"


def _local_proxy_status(proxy: str) -> tuple[bool, bool, str | None]:
    """检查回环代理端口；非本地代理不做预探测，避免额外网络请求。"""
    value = str(proxy or "").strip()
    if not value:
        return False, False, None
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        is_loopback = host.lower() == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            return False, True, None
        if not parsed.port:
            return True, False, "本地代理未配置端口"
        try:
            with socket.create_connection((host, parsed.port), timeout=0.5):
                return True, True, None
        except OSError as exc:
            return True, False, f"本地代理 {host}:{parsed.port} 未监听（{type(exc).__name__}）"
    except Exception as exc:
        return False, False, f"代理地址解析失败（{type(exc).__name__}）"


def resolve_plan_check_route(explicit_proxy: Optional[str] = None) -> dict:
    """解析套餐查询的实际网络路径。

    explicit_proxy 不是 None 时表示 API 调用方明确覆盖配置；空字符串代表直连。
    """
    if explicit_proxy is not None:
        selected = str(explicit_proxy or "").strip()
        return {
            "proxy": selected,
            "proxy_mode": "request",
            "proxy_source": "request" if selected else None,
            "network_route": "proxy" if selected else "direct",
            "proxy_used": _mask_proxy(selected) or None,
            "proxy_fallback_reason": None,
        }

    from config import proxy as proxy_cfg

    mode = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto") or "auto").strip().lower()
    if mode not in {"auto", "proxy", "direct"}:
        raise ValueError(f"PLAN_CHECK_PROXY_MODE={mode!r} 无效，可选 auto / proxy / direct")
    if mode == "direct":
        return {
            "proxy": "",
            "proxy_mode": mode,
            "proxy_source": None,
            "network_route": "direct",
            "proxy_used": None,
            "proxy_fallback_reason": None,
        }

    selected = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY", "") or "").strip()
    proxy_source = "configured" if selected else None
    if not selected:
        selected = str(proxy_cfg.pick_proxy() or "").strip()
        if selected:
            proxy_source = "api" if bool(getattr(proxy_cfg, "PROXY_API_ENABLED", False)) else "pool"
    if not selected:
        if mode == "proxy":
            raise ValueError("套餐查询网络模式为 proxy，但未配置 PLAN_CHECK_PROXY 或 PROXY_POOL")
        return {
            "proxy": "",
            "proxy_mode": mode,
            "proxy_source": None,
            "network_route": "direct",
            "proxy_used": None,
            "proxy_fallback_reason": "未配置套餐查询代理或代理池",
        }

    is_local, available, reason = _local_proxy_status(selected)
    if mode == "auto" and is_local and not available:
        return {
            "proxy": "",
            "proxy_mode": mode,
            "proxy_source": proxy_source,
            "network_route": "direct_fallback",
            "proxy_used": _mask_proxy(selected),
            "proxy_fallback_reason": reason,
        }
    return {
        "proxy": selected,
        "proxy_mode": mode,
        "proxy_source": proxy_source,
        "network_route": "proxy",
        "proxy_used": _mask_proxy(selected),
        "proxy_fallback_reason": None,
    }


def decode_jwt_payload_unverified(token: str) -> dict:
    """仅本地解析 JWT payload，不校验签名。"""
    token = normalize_token(token)
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def token_claims(token: str) -> dict:
    payload = decode_jwt_payload_unverified(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    profile = payload.get("https://api.openai.com/profile") or {}
    exp = payload.get("exp")
    exp_iso = None
    expired = None
    if isinstance(exp, (int, float)):
        exp_iso = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        expired = datetime.now(tz=timezone.utc).timestamp() >= float(exp)
    return {
        "payload": payload,
        "email": payload.get("email") or profile.get("email"),
        "user_name": payload.get("name") or profile.get("name"),
        "user_id": auth.get("chatgpt_user_id") or auth.get("user_id"),
        "account_id": auth.get("chatgpt_account_id"),
        "claim_plan_type": auth.get("chatgpt_plan_type"),
        "exp": exp,
        "token_expires_at": exp_iso,
        "token_expired": expired,
    }


def _common_headers(env: BrowserSession, token: str) -> dict[str, str]:
    headers = env._get_common_headers()
    headers.update({
        "accept": "*/*",
        "authorization": f"Bearer {normalize_token(token)}",
        "oai-device-id": env.device_id,
        "oai-language": env.navigator_language(),
        "referer": "https://chatgpt.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-openai-target-path": ACCOUNTS_CHECK_PATH,
        "x-openai-target-route": ACCOUNTS_CHECK_PATH,
    })
    return headers


def _workspace_headers(env: BrowserSession, token: str, account_id: str, path: str) -> dict[str, str]:
    headers = _common_headers(env, token)
    headers.update({
        "accept": "application/json",
        "chatgpt-account-id": account_id,
        "user-agent": WHAM_USAGE_USER_AGENT,
        "x-openai-target-path": path,
        "x-openai-target-route": path,
    })
    return headers


def _normalize_status_code(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")[:80]


def _conclusive_account_code(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    nodes: list[tuple[str, dict]] = [("response", data)]
    for parent_name in ("error", "detail", "account", "user", "auth", "authentication"):
        parent = data.get(parent_name)
        if isinstance(parent, dict):
            nodes.append((parent_name, parent))
            for child_name in ("account", "user", "auth", "authentication"):
                child = parent.get(child_name)
                if isinstance(child, dict):
                    nodes.append((f"{parent_name}.{child_name}", child))
    contextual_codes = {
        "banned": "account_banned",
        "deactivated": "account_deactivated",
        "deleted": "account_deleted",
        "disabled": "account_disabled",
        "expired": "authentication_expired",
        "revoked": "auth_revoked",
        "suspended": "account_suspended",
    }
    for node_name, node in nodes:
        account_context = node_name.rsplit(".", 1)[-1] in {"account", "user", "auth", "authentication"}
        for field_name in ("code", "error_code", "type", "reason", "status"):
            code = _normalize_status_code(node.get(field_name))
            if code in _CONCLUSIVE_ACCOUNT_CODES:
                return code
            if account_context and field_name in {"reason", "status"} and code in contextual_codes:
                return contextual_codes[code]
        for code in _CONCLUSIVE_ACCOUNT_CODES:
            if node.get(code) is True:
                return code
    return ""


def _terminal_plan_error(code: str, http_status: int) -> dict:
    result = {
        "ok": False,
        "checked_at": now_iso(),
        "http_status": http_status,
        "error": f"账号或登录凭据不可用（{code}）",
        "retryable": False,
        "needs_live_check": True,
        "plan_terminal_code": code,
    }
    if code in _ACCOUNT_UNUSABLE_CODES:
        result["account_unusable_code"] = code
    if code in _CREDENTIAL_UNUSABLE_CODES:
        result["credential_unusable_code"] = code
        result["token_expired"] = "expired" in code
    return result


def _raw_plan(plan: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(plan or "").strip().lower()).strip("_")[:64]


def _plan_family(plan: Any) -> str:
    raw = str(plan or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", raw)
    aliases = {
        "free": {"free", "basic", "starter", "hobby", "chatgptfree", "chatgptfreeplan"},
        "go": {"go", "goplan", "chatgptgo", "chatgptgoplan"},
        "plus": {"plus", "premium", "chatgptplus", "chatgptplusplan"},
        "pro": {"pro", "proplan", "chatgptpro", "chatgptproplan"},
        "team": {"team", "teamplan", "chatgptteam", "chatgptteamplan"},
        "business": {"business", "businessplan", "chatgptbusiness", "chatgptbusinessplan"},
        "enterprise": {
            "enterprise", "enterpriseplan", "chatgptenterprise", "chatgptenterpriseplan",
            "corporate", "corporateplan",
        },
        "edu": {"edu", "eduplan", "education", "chatgptedu", "chatgpteduplan"},
        "trial": {"trial", "trialing", "freetrial", "chatgpttrial"},
    }
    for family, values in aliases.items():
        if compact in values:
            return family
    tokens = set(re.findall(r"[a-z0-9]+", raw))
    for family, values in (
        ("enterprise", {"enterprise", "corporate"}), ("business", {"business"}),
        ("team", {"team"}), ("edu", {"edu", "education"}), ("pro", {"pro"}),
        ("plus", {"plus", "premium"}), ("go", {"go"}),
        ("trial", {"trial", "trialing"}), ("free", {"free", "basic", "starter", "hobby"}),
    ):
        if tokens & values:
            return family
    return "unknown" if compact else ""


def _plan_observation(plan: Any, *, source: str, scope: str, evidence_path: str) -> dict | None:
    raw = _raw_plan(plan)
    if not raw:
        return None
    family = _plan_family(plan) or "unknown"
    return {
        "plan_family": family,
        "plan_code_raw": raw,
        "subscription_state": "free" if family == "free" else ("trialing" if family == "trial" else "active"),
        "source": source,
        "scope": scope,
        "evidence_path": evidence_path,
    }


def _usage_account_id(data: dict) -> str:
    for key in ("account_id", "accountId", "chatgpt_account_id", "chatgptAccountId"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    account = data.get("account")
    return _usage_account_id(account) if isinstance(account, dict) else ""


def _response_json(resp: Any) -> dict | None:
    try:
        data = resp.json()
    except Exception:
        text = str(getattr(resp, "text", "") or "")
        try:
            data = json.loads(text) if text.strip().startswith("{") else None
        except Exception:
            data = None
    return data if isinstance(data, dict) else None


def _read_plan_endpoint(
    env: BrowserSession,
    token: str,
    account_id: str,
    *,
    path: str,
    timeout: float,
    params: dict | None = None,
) -> tuple[dict | None, dict | None]:
    request_kwargs = {
        "headers": _workspace_headers(env, token, account_id, path),
        "allow_redirects": False,
        "timeout": timeout,
    }
    if params is not None:
        request_kwargs["params"] = params
    resp = env.session.get(f"https://chatgpt.com{path}", **request_kwargs)
    data = _response_json(resp)
    http_status = int(resp.status_code)
    unusable_code = _conclusive_account_code(data)
    if unusable_code:
        return None, _terminal_plan_error(unusable_code, http_status)
    if not (200 <= http_status < 300):
        return None, {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": http_status,
            "error": f"HTTP {http_status}",
            "retryable": _retryable_plan_error(http_status),
            "response_data": data,
        }
    if data is None:
        return None, {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": http_status,
            "error": "响应不是 JSON 对象",
            "retryable": True,
        }
    return data, None


def _accounts_plan_signals(data: dict, account_id: str) -> dict:
    accounts = data.get("accounts")
    if not isinstance(accounts, dict) or not isinstance(accounts.get(account_id), dict):
        return {"error": "accounts/check 响应缺少当前 JWT workspace 精确节点"}
    node = accounts[account_id]
    account = node.get("account") if isinstance(node.get("account"), dict) else {}
    entitlement = node.get("entitlement") if isinstance(node.get("entitlement"), dict) else {}
    account_observation = _plan_observation(
        account.get("plan_type"), source="backend-api/accounts/check", scope="account",
        evidence_path="accounts[account_id].account.plan_type",
    )
    entitlement_observation = _plan_observation(
        entitlement.get("subscription_plan"), source="backend-api/accounts/check", scope="entitlement",
        evidence_path="accounts[account_id].entitlement.subscription_plan",
    )
    entitlement_active = entitlement.get("has_active_subscription")
    paid = None
    if entitlement_active is True and entitlement_observation and entitlement_observation["plan_family"] != "free":
        paid = entitlement_observation
    elif account_observation and account_observation["plan_family"] != "free":
        paid = account_observation
    elif entitlement_observation and entitlement_observation["plan_family"] != "free":
        paid = entitlement_observation
    if paid and entitlement_active is False:
        paid = dict(paid)
        paid["subscription_state"] = "expired"
    free_inactive = bool(
        account_observation and account_observation["plan_family"] == "free"
        and entitlement_observation and entitlement_observation["plan_family"] == "free"
        and entitlement_active is False
    )
    return {
        "paid": paid,
        "free": account_observation if free_inactive else None,
        "free_inactive": free_inactive,
    }


def _subscription_plan_signal(data: dict, account_id: str) -> tuple[dict | None, str]:
    if data.get("_no_subscription") is True:
        return None, ""
    response_account_id = str(data.get("account_id") or data.get("id") or "").strip()
    if response_account_id != account_id:
        return None, "subscriptions 响应与 JWT workspace 不匹配"
    observation = _plan_observation(
        data.get("plan_type"), source="backend-api/subscriptions", scope="subscription",
        evidence_path="response.plan_type",
    )
    if observation and data.get("is_delinquent") is True:
        observation = dict(observation)
        observation["subscription_state"] = "past_due"
    return observation, ""


def _result_from_observation(
    observation: dict,
    *,
    claims: dict,
    authority: str,
    confidence: str,
    raw_evidence: dict | None = None,
    preserve_trial: bool = True,
    plan_conflict: bool = False,
) -> dict:
    family = observation.get("plan_family") or "unknown"
    raw_plan = observation.get("plan_code_raw") or family
    subscription_state = str(observation.get("subscription_state") or "unknown")
    if family == "unknown" and raw_plan and subscription_state not in {"expired", "canceled", "past_due"}:
        family = "other"
    paid = family not in {"", "unknown", "free"} and subscription_state in {"active", "trialing"}
    result = {
        "ok": True,
        "checked_at": now_iso(),
        "account_id": claims.get("account_id"),
        "current_plan_type": family,
        "subscription_plan": raw_plan,
        "has_active_subscription": paid,
        "has_active_plus_subscription": family == "plus" and paid,
        "is_free_plan": family == "free",
        "subscription_state_available": True,
        "plan_detection_capability": "fallback_plan",
        "plan_detection_source": observation.get("source"),
        "plan_authority": authority,
        "plan_confidence": confidence,
        "plan_evidence_path": observation.get("evidence_path"),
        "plan_evidence_scope": observation.get("scope"),
        "subscription_status": subscription_state,
        "plan_evidence": raw_evidence,
        "plan_conflict": plan_conflict,
        "preserve_plus_trial_eligibility": preserve_trial,
        "retryable": False,
    }
    result.update({key: value for key, value in claims.items() if key != "payload" and value is not None})
    return result


def _check_plan_fallbacks(
    env: BrowserSession,
    token: str,
    claims: dict,
    *,
    timeout: float,
    accounts_data: dict | None = None,
) -> dict:
    account_id = str(claims.get("account_id") or "").strip()
    if not account_id:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "error": "JWT 缺少 workspace account_id，无法安全执行套餐兜底查询",
            "retryable": False,
            "plan_detection_source": "local_credentials",
            "plan_authority": "none",
        }

    errors: list[dict] = []
    accounts_signal = _accounts_plan_signals(accounts_data, account_id) if accounts_data else {}
    if accounts_signal.get("error"):
        errors.append({"source": "accounts_check", "error": accounts_signal["error"]})

    subscriptions_data = None
    subscriptions_observation = None
    for attempt in range(1, AUTHORITATIVE_STATUS_MAX_ATTEMPTS + 1):
        try:
            data, error = _read_plan_endpoint(
                env,
                token,
                account_id,
                path=SUBSCRIPTIONS_PATH,
                params={"account_id": account_id},
                timeout=timeout,
            )
        except Exception as exc:
            data = None
            error = {"error": f"{type(exc).__name__}: {exc}", "retryable": True}
        if data is not None:
            subscriptions_data = data
            break
        error = error or {"error": "subscriptions 查询失败", "retryable": True}
        status = int(error.get("http_status") or 0)
        preview = str(error.get("error") or "")
        if status == 404:
            body = error.get("response_data") or {}
            if "no subscription found for account" in str(body.get("detail") or "").lower():
                subscriptions_data = {"_no_subscription": True}
                break
        errors.append({"source": "subscriptions", "error": preview, "http_status": status})
        if error.get("plan_terminal_code"):
            error.update({key: value for key, value in claims.items() if key != "payload" and value is not None})
            return error
        if not error.get("retryable") or attempt >= AUTHORITATIVE_STATUS_MAX_ATTEMPTS:
            break
        time.sleep(WHAM_USAGE_RETRY_DELAYS[0])

    if subscriptions_data is not None:
        subscriptions_observation, identity_error = _subscription_plan_signal(subscriptions_data, account_id)
        if identity_error:
            errors.append({"source": "subscriptions", "error": identity_error})
            subscriptions_data = None
            subscriptions_observation = None

    accounts_paid = accounts_signal.get("paid")
    subscriptions_paid = (
        subscriptions_observation
        if subscriptions_observation and subscriptions_observation.get("plan_family") != "free"
        else None
    )
    if accounts_paid:
        return _result_from_observation(
            accounts_paid,
            claims=claims,
            authority="authoritative",
            confidence="high",
            raw_evidence=accounts_data,
            plan_conflict=bool(
                subscriptions_paid
                and subscriptions_paid.get("plan_family") != accounts_paid.get("plan_family")
            ),
        )
    if subscriptions_paid:
        return _result_from_observation(
            subscriptions_paid,
            claims=claims,
            authority="authoritative",
            confidence="high",
            raw_evidence=subscriptions_data,
            plan_conflict=bool(accounts_signal.get("free_inactive")),
        )
    if (
        accounts_signal.get("free_inactive")
        and isinstance(subscriptions_data, dict)
        and subscriptions_data.get("_no_subscription") is True
    ):
        observation = dict(accounts_signal["free"])
        observation.update({
            "source": "backend-api/accounts/check+subscriptions",
            "evidence_path": "account.plan_type+entitlement+subscriptions.404",
        })
        return _result_from_observation(
            observation,
            claims=claims,
            authority="authoritative",
            confidence="high",
            raw_evidence={"accounts_check": accounts_data, "subscriptions": subscriptions_data},
        )

    for attempt in range(1, WHAM_USAGE_MAX_ATTEMPTS + 1):
        try:
            usage_data, error = _read_plan_endpoint(
                env,
                token,
                account_id,
                path=WHAM_USAGE_PATH,
                timeout=timeout,
            )
        except Exception as exc:
            usage_data = None
            error = {"error": f"{type(exc).__name__}: {exc}", "retryable": True}
        if usage_data is not None:
            response_account_id = _usage_account_id(usage_data)
            if response_account_id != account_id:
                errors.append({
                    "source": "wham_usage",
                    "error": "wham/usage 响应缺少 JWT 当前 workspace 的精确身份",
                })
                break
            observation = _plan_observation(
                usage_data.get("plan_type"), source="backend-api/wham/usage", scope="workspace",
                evidence_path="response.plan_type",
            )
            if observation:
                return _result_from_observation(
                    observation,
                    claims=claims,
                    authority="verified",
                    confidence="medium",
                    raw_evidence=usage_data,
                )
            errors.append({"source": "wham_usage", "error": "响应未包含 plan_type"})
            break
        error = error or {"error": "wham/usage 查询失败", "retryable": True}
        errors.append({
            "source": "wham_usage",
            "error": error.get("error"),
            "http_status": error.get("http_status"),
        })
        if error.get("plan_terminal_code"):
            error.update({key: value for key, value in claims.items() if key != "payload" and value is not None})
            return error
        if not error.get("retryable") or attempt >= WHAM_USAGE_MAX_ATTEMPTS:
            break
        time.sleep(WHAM_USAGE_RETRY_DELAYS[min(attempt - 1, len(WHAM_USAGE_RETRY_DELAYS) - 1)])

    try:
        resp = env.session.get(
            f"https://chatgpt.com{ME_PATH}",
            headers={
                "authorization": f"Bearer {normalize_token(token)}",
                "accept": "application/json",
                "user-agent": WHAM_USAGE_USER_AGENT,
            },
            allow_redirects=False,
            timeout=timeout,
        )
        me_payload = _response_json(resp)
        me_data = me_payload if 200 <= int(resp.status_code) < 300 else None
        unusable_code = _conclusive_account_code(me_payload)
        error = None
        if unusable_code:
            error = _terminal_plan_error(unusable_code, int(resp.status_code))
        elif me_data is None:
            error = {
                "http_status": int(resp.status_code),
                "error": f"HTTP {int(resp.status_code)}" if int(resp.status_code) >= 400 else "响应不是 JSON 对象",
            }
    except Exception as exc:
        me_data = None
        error = {"http_status": None, "error": f"{type(exc).__name__}: {exc}"}
    if me_data is not None:
        observations = []
        top = _plan_observation(
            me_data.get("plan_type"), source="backend-api/me", scope="personal",
            evidence_path="response.plan_type",
        )
        if top:
            observations.append(top)
        org_container = me_data.get("orgs")
        orgs = org_container.get("data", []) if isinstance(org_container, dict) else (
            org_container if isinstance(org_container, list) else []
        )
        for org in orgs if isinstance(orgs, list) else []:
            if not isinstance(org, dict):
                continue
            workspace_id = str(org.get("id") or org.get("account_id") or "").strip()
            if workspace_id != account_id:
                continue
            settings = org.get("settings") if isinstance(org.get("settings"), dict) else {}
            observation = _plan_observation(
                settings.get("workspace_plan_type"), source="backend-api/me", scope="workspace",
                evidence_path="orgs[workspace_id].settings.workspace_plan_type",
            )
            if observation:
                observations.append(observation)
        rank = {
            "enterprise": 90, "business": 80, "team": 70, "edu": 60, "pro": 50,
            "plus": 40, "go": 30, "trial": 20, "free": 10, "unknown": 1,
        }
        observation = max(observations, key=lambda item: rank.get(item["plan_family"], 1), default=None)
        if observation:
            return _result_from_observation(
                observation,
                claims=claims,
                authority="weak",
                confidence="low",
                raw_evidence=me_data,
            )
        errors.append({"source": "me", "error": "响应未包含可识别套餐"})
    elif error:
        if error.get("plan_terminal_code"):
            error.update({key: value for key, value in claims.items() if key != "payload" and value is not None})
            return error
        errors.append({"source": "me", "error": error.get("error"), "http_status": error.get("http_status")})

    return {
        "ok": False,
        "checked_at": now_iso(),
        "error": _format_plan_probe_errors(errors),
        "retryable": True,
        "plan_detection_source": "chatgpt_status_check",
        "plan_authority": "none",
        "plan_evidence": errors,
        **{key: value for key, value in claims.items() if key != "payload" and value is not None},
    }


def _format_plan_probe_errors(errors: list[dict] | None) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    source_labels = {
        "accounts_check": "accounts/check",
        "subscriptions": "subscriptions",
        "wham_usage": "wham/usage",
        "me": "me",
    }
    for item in errors or []:
        if not isinstance(item, dict):
            continue
        source = source_labels.get(str(item.get("source") or "").strip(), str(item.get("source") or "套餐接口").strip())
        message = str(item.get("error") or "").strip()
        status = item.get("http_status")
        if not message and status:
            message = f"HTTP {status}"
        elif status and not re.search(rf"\bHTTP\s+{re.escape(str(status))}\b", message, re.I):
            message = f"HTTP {status} · {message}"
        if not message:
            continue
        text = f"{source}：{message}"
        if text in seen:
            continue
        seen.add(text)
        parts.append(text)
    return "；".join(parts) or "套餐检测失败，接口未返回具体错误"


def _normalize_offer_percentage(value: Any) -> float | None:
    """Normalize a campaign discount percentage without guessing missing data."""
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().replace("%", "").replace(",", "")
    if not raw:
        return None
    try:
        percentage = float(raw)
    except (TypeError, ValueError):
        return None
    # Some APIs serialize ratios (0.5) while accounts/check normally returns 50.
    if 0 < percentage < 1:
        percentage *= 100
    if percentage < 0 or percentage > 100:
        return None
    return round(percentage, 4)


def classify_plus_trial_offer(plus_campaign: Any) -> dict:
    """Classify an eligible Plus campaign as zero-price, half-price or other.

    The accounts/check campaign metadata is the source of truth. Text markers are
    only used when the response omits an explicit discount percentage.
    """
    if not isinstance(plus_campaign, dict) or not plus_campaign:
        return {
            "kind": "none",
            "label": "无试用资格",
            "percentage": None,
            "evidence": "eligible_promo_campaigns.plus absent",
        }

    metadata = plus_campaign.get("metadata") if isinstance(plus_campaign.get("metadata"), dict) else {}
    discount = metadata.get("discount") if isinstance(metadata.get("discount"), dict) else {}
    percentage = _normalize_offer_percentage(
        discount.get("percentage", plus_campaign.get("discount_percentage"))
    )
    searchable = " ".join(str(value or "") for value in (
        plus_campaign.get("id"),
        metadata.get("title"),
        metadata.get("summary"),
        metadata.get("promotion_type_label"),
    )).casefold()
    compact = re.sub(r"[\s_]+", "-", searchable)

    zero_markers = (
        "1-month-free", "one-month-free", "one month free", "month-free",
        "free-trial", "free trial", "100%-off", "100% off", "$0", "€0", "¥0",
        "0元", "零元", "免费试用", "無料", "一か月無料", "1か月無料",
    )
    half_markers = (
        "half-price", "half price", "50%-off", "50% off", "半价", "半價", "半額",
    )
    evidence = "metadata.discount.percentage" if percentage is not None else "campaign metadata"

    if percentage is not None and percentage >= 99.5 or any(marker in compact for marker in zero_markers):
        return {"kind": "free_trial", "label": "0元试用", "percentage": percentage, "evidence": evidence}
    if percentage is not None and 49.5 <= percentage <= 50.5 or any(marker in compact for marker in half_markers):
        return {"kind": "half_price", "label": "半价试用", "percentage": percentage, "evidence": evidence}
    if percentage is not None and percentage > 0:
        display = int(percentage) if percentage.is_integer() else percentage
        return {"kind": "discount", "label": f"{display}%优惠", "percentage": percentage, "evidence": evidence}
    return {"kind": "trial", "label": "可试用（优惠未知）", "percentage": percentage, "evidence": evidence}


def parse_accounts_check(data: dict, *, token: str = "") -> dict:
    """从 accounts/check 响应提取套餐和 Plus 试用资格。"""
    claims = token_claims(token) if token else {}
    claim_account_id = claims.get("account_id")
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        raise ValueError("响应缺少 accounts 对象")

    item = None
    account_key = None
    if claim_account_id:
        if not isinstance(accounts.get(claim_account_id), dict):
            raise ValueError("响应缺少 JWT 当前 workspace 的精确账号条目")
        item = accounts.get(claim_account_id)
        account_key = claim_account_id
    elif isinstance(accounts.get("default"), dict):
        item = accounts.get("default")
        account = item.get("account") or {}
        account_key = account.get("account_id") or "default"
    else:
        for k, v in accounts.items():
            if k != "default" and isinstance(v, dict):
                item = v
                account_key = k
                break
    if not isinstance(item, dict):
        raise ValueError("未找到可解析的账号条目")

    account = item.get("account") or {}
    entitlement = item.get("entitlement") or {}
    last_sub = item.get("last_active_subscription") or {}
    eligible_promo_campaigns = item.get("eligible_promo_campaigns") or {}
    raw_plus_campaign = eligible_promo_campaigns.get("plus") if isinstance(eligible_promo_campaigns, dict) else None
    plus_campaign = raw_plus_campaign if isinstance(raw_plus_campaign, dict) else None
    plus_meta = plus_campaign.get("metadata") if isinstance((plus_campaign or {}).get("metadata"), dict) else {}
    discount = plus_meta.get("discount") if isinstance(plus_meta.get("discount"), dict) else {}
    duration = plus_meta.get("duration") if isinstance(plus_meta.get("duration"), dict) else {}

    plan_type = account.get("plan_type") or claims.get("claim_plan_type") or ""
    subscription_plan = entitlement.get("subscription_plan") or ""
    has_active_subscription = bool(entitlement.get("has_active_subscription"))
    subscription_state_available = bool(
        "has_active_subscription" in entitlement
        or str(entitlement.get("subscription_plan") or "").strip()
    )
    subscription_plan_key = str(subscription_plan).strip().lower()
    # accounts/check 的 account.plan_type 可能仍为 free；实际订阅状态应以
    # entitlement 的 active subscription + subscription_plan 为准。试用资格不能
    # 作为已开通 Plus 的判断条件。
    has_active_plus_subscription = bool(
        has_active_subscription
        and "plus" in subscription_plan_key
        and "free" not in subscription_plan_key
    )
    has_active_paid_subscription = bool(
        has_active_subscription
        and _plan_family(subscription_plan) not in {"", "unknown", "free"}
    )
    is_free = bool(
        not has_active_paid_subscription
        and (
            str(plan_type).lower() == "free"
            or subscription_plan_key == "chatgptfreeplan"
        )
    )
    plus_trial_eligible = bool(is_free and plus_campaign)
    plus_trial_offer = classify_plus_trial_offer(plus_campaign if plus_trial_eligible else None)

    offers = ((item.get("eligible_offers") or {}).get("offers") or [])
    eligible_offer_ids = [o.get("id") for o in offers if isinstance(o, dict) and o.get("id")]

    result = {
        "ok": True,
        "checked_at": now_iso(),
        "account_id": account.get("account_id") or account_key or claim_account_id,
        "account_user_role": account.get("account_user_role"),
        "current_plan_type": plan_type,
        "subscription_plan": subscription_plan,
        "has_active_subscription": has_active_subscription,
        "subscription_state_available": subscription_state_available,
        "plan_detection_capability": "entitlement" if subscription_state_available else "promo_only",
        "plan_detection_source": "backend-api/accounts/check",
        "plan_authority": "authoritative",
        "plan_confidence": "high",
        "plan_evidence_path": (
            "accounts[account_id].entitlement.subscription_plan"
            if str(subscription_plan).strip()
            else "accounts[account_id].account.plan_type"
        ),
        "plan_evidence_scope": "workspace",
        "has_active_plus_subscription": has_active_plus_subscription,
        "is_free_plan": is_free,
        "is_active_subscription_gratis": bool(entitlement.get("is_active_subscription_gratis")),
        "expires_at": entitlement.get("expires_at"),
        "renews_at": entitlement.get("renews_at"),
        "cancels_at": entitlement.get("cancels_at"),
        "billing_period": entitlement.get("billing_period"),
        "billing_currency": entitlement.get("billing_currency"),
        "is_delinquent": bool(entitlement.get("is_delinquent")),
        "discount_type": (entitlement.get("discount") or {}).get("discount_type"),
        "discount_amount": (entitlement.get("discount") or {}).get("amount"),
        "discount_duration_num_periods": (entitlement.get("discount") or {}).get("duration_num_periods"),
        "discount_expires_at": (entitlement.get("discount") or {}).get("discount_expires_at"),
        "discount_cancellation_policy": (entitlement.get("discount") or {}).get("cancellation_policy"),
        "discount_promo_campaign_id": (entitlement.get("discount") or {}).get("promo_campaign_id"),
        "last_purchase_origin_platform": last_sub.get("purchase_origin_platform"),
        "last_will_renew": bool(last_sub.get("will_renew")),
        "plus_trial_eligible": plus_trial_eligible,
        "plus_trial_campaign_id": (plus_campaign or {}).get("id"),
        "plus_trial_title": plus_meta.get("title"),
        "plus_trial_summary": plus_meta.get("summary"),
        "plus_trial_discount_percentage": discount.get("percentage"),
        "plus_trial_duration_num_periods": duration.get("num_periods"),
        "plus_trial_duration_period": duration.get("period"),
        "plus_trial_promotion_type_label": plus_meta.get("promotion_type_label"),
        "plus_trial_offer_kind": plus_trial_offer["kind"],
        "plus_trial_offer_label": plus_trial_offer["label"],
        "plus_trial_offer_percentage": plus_trial_offer["percentage"],
        "plus_trial_offer_evidence": plus_trial_offer["evidence"],
        "eligible_offer_ids": eligible_offer_ids,
        "features_count": len(item.get("features") or []),
        "can_access_with_session": bool(item.get("can_access_with_session")),
        "raw_account_plan_type": account.get("plan_type"),
    }
    result.update({k: v for k, v in claims.items() if k != "payload" and v is not None})
    return result


def _plan_check_settings(
    timeout: float | None,
    max_attempts: int | None,
    retry_delay: float | None,
) -> tuple[float, int, float]:
    from config import proxy as proxy_cfg

    timeout_value = timeout if timeout is not None else getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 15.0)
    attempts_value = max_attempts if max_attempts is not None else getattr(proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 2)
    delay_value = retry_delay if retry_delay is not None else getattr(proxy_cfg, "PLAN_CHECK_RETRY_DELAY", 1.5)
    attempts_number = int(attempts_value if attempts_value is not None else 2)
    return (
        max(1.0, min(120.0, float(timeout_value or 30.0))),
        0 if attempts_number <= 0 else max(1, attempts_number),
        max(0.0, min(30.0, float(delay_value or 0.0))),
    )


def _format_plan_request_error(exc: Exception, timeout_seconds: float) -> str:
    message = str(exc or "").strip()
    if type(exc).__name__.lower().endswith("timeout") or "curl: (28)" in message.lower():
        return f"套餐查询超时（{timeout_seconds:g} 秒）：专用代理节点响应过慢，请重试"
    return f"{type(exc).__name__}: {message}"


def _retryable_plan_error(http_status: int | None) -> bool:
    if http_status is None:
        return True
    return http_status in {408, 409, 425, 429} or http_status >= 500


def _retry_wait_seconds(resp: Any, base_delay: float, attempt: int) -> float:
    try:
        retry_after = (getattr(resp, "headers", {}) or {}).get("retry-after")
        if retry_after is not None:
            return max(0.0, min(30.0, float(retry_after)))
    except (TypeError, ValueError):
        pass
    return max(0.0, min(30.0, base_delay * attempt))


def check_account_plan(
    token: str,
    *,
    proxy: Optional[str] = None,
    timezone_offset_min: str = "-",
    locale_country: str = "",
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
    fast_mode: bool = False,
    continue_check=None,
    retry_proxy_provider=None,
    locale_country_provider=None,
) -> dict:
    token = normalize_token(token)
    if not token:
        return {"ok": False, "checked_at": now_iso(), "error": "token 为空"}
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": "AT已过期/失效，请手动查活刷新",
            "needs_live_check": True,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    try:
        route = resolve_plan_check_route(proxy)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"套餐查询网络配置错误: {exc}",
            **{k: v for k, v in claims.items() if k != "payload"},
        }
    route_meta = {k: v for k, v in route.items() if k != "proxy"}
    normalized_locale_country = str(locale_country or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", normalized_locale_country):
        normalized_locale_country = ""
    request_locale_country = normalized_locale_country
    request_language = ""

    def current_locale_country() -> str:
        value = normalized_locale_country
        if callable(locale_country_provider):
            try:
                value = str(locale_country_provider() or value).strip().upper()
            except Exception:
                value = normalized_locale_country
        return value if re.fullmatch(r"[A-Z]{2}", value or "") else ""
    logger.info(
        "[Plan] 套餐查询网络路径：source=%s mode=%s route=%s proxy=%s",
        route.get("proxy_source") or "none",
        route.get("proxy_mode") or "-",
        route.get("network_route") or "-",
        route.get("proxy_used") or "-",
    )
    url = f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}?timezone_offset_min={quote(str(timezone_offset_min))}"
    try:
        timeout_seconds, attempts, base_delay = _plan_check_settings(timeout, max_attempts, retry_delay)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"套餐查询重试配置错误: {exc}",
            "retryable": False,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    last_result: dict | None = None
    attempt = 0
    while True:
        attempt += 1
        if callable(continue_check) and not continue_check():
            return {
                "ok": False,
                "checked_at": now_iso(),
                "error": "套餐查询已停止",
                "retryable": False,
                "stopped": True,
                "attempt_count": attempt - 1,
                "max_attempts": attempts or None,
                **route_meta,
            }
        env = None
        resp = None
        try:
            if attempt > 1 and callable(retry_proxy_provider):
                refreshed_proxy = retry_proxy_provider()
                route = resolve_plan_check_route(refreshed_proxy)
                route_meta = {k: v for k, v in route.items() if k != "proxy"}
            # 动态 API 模式下每次重试重新取节点，避免同一个坏代理连续失败。
            elif attempt > 1 and proxy is None and route.get("proxy_source") == "api":
                route = resolve_plan_check_route(None)
                route_meta = {k: v for k, v in route.items() if k != "proxy"}
            # 套餐查询只需要稳定的请求头，不需要额外访问 IP 地理信息接口。
            request_locale_country = current_locale_country()
            env = BrowserSession(
                proxy=route["proxy"],
                detect_exit_geo=False,
                profile_geo={"country": request_locale_country} if request_locale_country else {},
            )
            request_language = env.navigator_language()
            logger.info(
                "[Plan] 套餐查询语言画像：proxy_country=%s language=%s timezone=%s",
                request_locale_country or "default",
                request_language or "default",
                str((env.browser_profile or {}).get("timezone_iana") or "default"),
            )
            request_headers = _common_headers(env, token)
            if claims.get("account_id"):
                request_headers["chatgpt-account-id"] = str(claims["account_id"])
            resp = env.session.get(
                url,
                headers=request_headers,
                allow_redirects=False,
                timeout=timeout_seconds,
            )
            response_text = resp.text or ""
            http_status = int(resp.status_code)
            if not (200 <= http_status < 300):
                account_unusable_code = detect_account_unusable_response_body(response_text)
                terminal_code = _conclusive_account_code(_response_json(resp)) or account_unusable_code
                is_auth_expired = http_status == 401
                if terminal_code:
                    last_result = _terminal_plan_error(terminal_code, http_status)
                    last_result["response_preview"] = response_text[:500]
                else:
                    last_result = {
                        "ok": False,
                        "checked_at": now_iso(),
                        "http_status": http_status,
                        "error": "AT已过期/失效，请手动查活刷新" if is_auth_expired else f"HTTP {http_status}",
                        "response_preview": response_text[:500],
                        "retryable": _retryable_plan_error(http_status),
                        "token_expired": True if is_auth_expired else claims.get("token_expired"),
                        "needs_live_check": True if is_auth_expired else False,
                        "account_unusable_code": None,
                    }
            else:
                try:
                    data: Any = resp.json()
                except Exception:
                    data = json.loads(response_text) if response_text.strip().startswith(("{", "[")) else None
                if not isinstance(data, dict):
                    last_result = {
                        "ok": False,
                        "checked_at": now_iso(),
                        "http_status": http_status,
                        "error": "响应不是 JSON 对象",
                        "response_preview": response_text[:500],
                        "retryable": True,
                    }
                else:
                    parsed = parse_accounts_check(data, token=token)
                    parsed["http_status"] = http_status
                    parsed["attempt_count"] = attempt
                    parsed["max_attempts"] = attempts or None
                    parsed["retry_until_result"] = attempts == 0
                    parsed["request_timeout"] = timeout_seconds
                    parsed["retryable"] = False
                    parsed["plan_check_locale_country"] = request_locale_country or None
                    parsed["plan_check_request_language"] = request_language or None
                    parsed.update(route_meta)
                    if fast_mode:
                        parsed["plan_detection_capability"] = "accounts_check_fast"
                        parsed["plan_detection_source"] = "backend-api/accounts/check"
                        parsed["plan_authority"] = "authoritative"
                        parsed["plan_confidence"] = "high"
                        return parsed
                    detected = _check_plan_fallbacks(
                        env,
                        token,
                        claims,
                        timeout=timeout_seconds,
                        accounts_data=data,
                    )
                    if detected.get("ok"):
                        detected = {
                            **parsed,
                            **detected,
                            "plus_trial_eligible": (
                                bool(parsed.get("plus_trial_eligible"))
                                if detected.get("is_free_plan") else False
                            ),
                            "preserve_plus_trial_eligibility": False,
                        }
                    elif not detected.get("plan_terminal_code"):
                        detected = {
                            "ok": True,
                            "checked_at": parsed.get("checked_at") or now_iso(),
                            "account_id": parsed.get("account_id"),
                            "account_user_role": parsed.get("account_user_role"),
                            "plus_trial_eligible": bool(parsed.get("plus_trial_eligible")),
                            "plus_trial_campaign_id": parsed.get("plus_trial_campaign_id"),
                            "plus_trial_title": parsed.get("plus_trial_title"),
                            "plus_trial_summary": parsed.get("plus_trial_summary"),
                            "plus_trial_discount_percentage": parsed.get("plus_trial_discount_percentage"),
                            "plus_trial_duration_num_periods": parsed.get("plus_trial_duration_num_periods"),
                            "plus_trial_duration_period": parsed.get("plus_trial_duration_period"),
                            "plus_trial_promotion_type_label": parsed.get("plus_trial_promotion_type_label"),
                            "plus_trial_offer_kind": parsed.get("plus_trial_offer_kind"),
                            "plus_trial_offer_label": parsed.get("plus_trial_offer_label"),
                            "plus_trial_offer_percentage": parsed.get("plus_trial_offer_percentage"),
                            "plus_trial_offer_evidence": parsed.get("plus_trial_offer_evidence"),
                            "eligible_offer_ids": parsed.get("eligible_offer_ids") or [],
                            "plan_detection_capability": "promo_only",
                            "plan_detection_source": "backend-api/accounts/check",
                            "plan_authority": "promo_only",
                            "plan_fallback_error": detected.get("error"),
                            "plan_fallback_evidence": detected.get("plan_evidence"),
                            "preserve_plus_trial_eligibility": False,
                            "retryable": False,
                            **{key: value for key, value in claims.items() if key != "payload" and value is not None},
                        }
                    detected["http_status"] = http_status
                    detected["attempt_count"] = attempt
                    detected["max_attempts"] = attempts or None
                    detected["retry_until_result"] = attempts == 0
                    detected["request_timeout"] = timeout_seconds
                    detected["plan_check_locale_country"] = request_locale_country or None
                    detected["plan_check_request_language"] = request_language or None
                    detected.update(route_meta)
                    return detected
        except Exception as exc:
            logger.debug("套餐查询失败: %s: %s", type(exc).__name__, exc, exc_info=True)
            last_result = {
                "ok": False,
                "checked_at": now_iso(),
                "http_status": int(resp.status_code) if resp is not None and getattr(resp, "status_code", None) else None,
                "error": _format_plan_request_error(exc, timeout_seconds),
                "retryable": True,
            }
        finally:
            if env is not None:
                try:
                    env.session.close()
                except Exception:
                    pass

        last_result = last_result or {"ok": False, "checked_at": now_iso(), "error": "未知错误", "retryable": True}
        last_result.update({
            "attempt_count": attempt,
            "max_attempts": attempts or None,
            "retry_until_result": attempts == 0,
            "request_timeout": timeout_seconds,
            "plan_check_locale_country": request_locale_country or None,
            "plan_check_request_language": request_language or None,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        })
        if not last_result.get("retryable") or (attempts > 0 and attempt >= attempts):
            break

        wait_seconds = _retry_wait_seconds(resp, base_delay, attempt)
        logger.warning(
            "套餐查询临时失败，第 %s/%s 次，%.1fs 后重试: %s",
            attempt,
            attempts or "∞",
            wait_seconds,
            last_result.get("error"),
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    if not fast_mode and not (last_result or {}).get("plan_terminal_code"):
        env = None
        try:
            request_locale_country = current_locale_country()
            env = BrowserSession(
                proxy=route["proxy"],
                detect_exit_geo=False,
                profile_geo={"country": request_locale_country} if request_locale_country else {},
            )
            request_language = env.navigator_language()
            fallback_result = _check_plan_fallbacks(
                env,
                token,
                claims,
                timeout=timeout_seconds,
            )
            fallback_result.update({
                "accounts_check_error": (last_result or {}).get("error"),
                "accounts_check_http_status": (last_result or {}).get("http_status"),
                "attempt_count": attempt,
                "max_attempts": attempts or None,
                "retry_until_result": attempts == 0,
                "request_timeout": timeout_seconds,
                "plan_check_locale_country": request_locale_country or None,
                "plan_check_request_language": request_language or None,
                **route_meta,
            })
            return fallback_result
        except Exception as exc:
            logger.debug("套餐兜底查询失败: %s: %s", type(exc).__name__, exc, exc_info=True)
        finally:
            if env is not None:
                try:
                    env.session.close()
                except Exception:
                    pass

    return last_result or {
        "ok": False,
        "checked_at": now_iso(),
        "http_status": None,
        "error": "套餐查询未执行",
        "retryable": False,
        **route_meta,
        **{k: v for k, v in claims.items() if k != "payload"},
    }
