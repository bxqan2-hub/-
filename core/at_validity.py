# -*- coding: utf-8 -*-
"""只验证 Access Token 会话是否仍有效。

这个模块只请求 ``/backend-api/me`` 并读取 HTTP 状态，不解析套餐、订阅、优惠或
0 元试用字段。结论分为 ``valid``、``invalid_confirmed``、``check_error``，代理、
限流和服务端错误不会被误记成 AT 已失效。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from config import proxy as proxy_cfg
from core import detection_proxy
from core.chatgpt_plan import ME_PATH, normalize_token, resolve_plan_check_route, token_claims
from core.session import BrowserSession


VALIDITY_OUTCOMES = {"valid", "invalid_confirmed", "check_error"}
_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def _checked_at() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _timeout_seconds() -> float:
    try:
        value = float(getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 30.0) or 30.0)
    except (TypeError, ValueError):
        value = 30.0
    return max(1.0, min(60.0, value))


def _result(
    outcome: str,
    *,
    http_status: int | None = None,
    error_code: str = "",
    error: str = "",
    attempt_count: int = 0,
    route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    route = route or {}
    return {
        "outcome": outcome,
        "valid": True if outcome == "valid" else False if outcome == "invalid_confirmed" else None,
        "checked_at": _checked_at(),
        "http_status": http_status,
        "error_code": str(error_code or "")[:100],
        "error": str(error or "")[:500],
        "attempt_count": int(attempt_count or 0),
        "network_route": route.get("network_route"),
        "proxy_used": route.get("proxy_used"),
        "proxy_source": route.get("proxy_source"),
        "proxy_fallback_reason": route.get("proxy_fallback_reason"),
    }


def _resolve_at_validity_route(explicit_proxy: str | None = None) -> dict[str, Any]:
    """选择 AT 检测路径；专属池非空时只用专属池，空池时走本机 VPN。

    显式空串表示调用方要求直连。空池回退只读取操作系统/环境代理；若系统没有
    显式代理则用直连交给本机 VPN/TUN 接管。不会读取任何代理池或动态代理 API。
    """
    if explicit_proxy is not None:
        resolved = detection_proxy.resolve_static_detection_proxy(explicit_proxy)
        route = resolve_plan_check_route(explicit_proxy=resolved or "")
        route["proxy_source"] = "request" if resolved else None
        return route

    static_spec = detection_proxy.configured_detection_proxy_spec("at")
    if static_spec is not None:
        resolved = detection_proxy.resolve_static_detection_proxy(static_spec)
        if not resolved:
            raise ValueError("AT 有效性检测专属代理池未解析到可用静态代理")
        route = resolve_plan_check_route(explicit_proxy=resolved or "")
        route["proxy_source"] = "at_validity_static_pool"
        return route

    system_proxy = str(proxy_cfg.detect_system_proxy() or "").strip()
    if system_proxy:
        resolved = detection_proxy.resolve_static_detection_proxy(system_proxy)
        if not resolved:
            raise ValueError("AT 有效性检测无法识别本机系统代理")
        route = resolve_plan_check_route(explicit_proxy=resolved)
        route["proxy_source"] = "local_vpn_system_proxy"
        return route
    return {
        "proxy": "",
        "proxy_mode": "local_vpn",
        "proxy_source": "local_vpn_tun",
        "network_route": "local_vpn",
        "proxy_used": None,
        "proxy_fallback_reason": None,
    }


def _request_headers(env: BrowserSession, token: str) -> dict[str, str]:
    headers = env._get_common_headers()
    headers.update({
        "accept": "application/json",
        "authorization": f"Bearer {token}",
        "oai-device-id": env.device_id,
        "oai-language": env.navigator_language(),
        "referer": "https://chatgpt.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-openai-target-path": ME_PATH,
        "x-openai-target-route": ME_PATH,
    })
    return headers


def check_access_token_validity(
    access_token: str,
    *,
    proxy: str | None = None,
    max_attempts: int = 5,
    retry_delay: float = 1.0,
) -> dict[str, Any]:
    """仅检查 AT 是否有效；不会执行任何套餐或 0 元试用判断。"""
    token = normalize_token(access_token)
    if not token:
        return _result(
            "invalid_confirmed",
            error_code="missing_access_token",
            error="账号缺少 Access Token",
        )

    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return _result(
            "invalid_confirmed",
            error_code="token_expired",
            error="AT 的本地过期时间已到",
        )

    try:
        route = _resolve_at_validity_route(proxy)
    except Exception as exc:
        return _result(
            "check_error",
            error_code="proxy_config_error",
            error=f"AT 检测代理配置错误: {type(exc).__name__}: {str(exc)[:300]}",
        )

    attempts = max(1, min(10, int(max_attempts or 1)))
    try:
        retry_delay = max(0.0, min(30.0, float(retry_delay or 0.0)))
    except (TypeError, ValueError):
        retry_delay = 1.0
    last_error = ""
    last_status: int | None = None
    attempt_count = 0
    for attempt in range(1, attempts + 1):
        attempt_count = attempt
        env: BrowserSession | None = None
        if attempt > 1:
            try:
                # 专属池会在这里轮换下一条；本机 VPN 则重新读取系统代理/TUN 状态。
                route = _resolve_at_validity_route(proxy)
            except Exception as exc:
                last_status = None
                last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
        try:
            env = BrowserSession(proxy=str(route.get("proxy") or ""), detect_exit_geo=False)
            headers = _request_headers(env, token)
            response = env.session.get(
                f"https://chatgpt.com{ME_PATH}",
                headers=headers,
                allow_redirects=False,
                timeout=_timeout_seconds(),
            )
            last_status = int(getattr(response, "status_code", 0) or 0)
            if 200 <= last_status < 300:
                return _result("valid", http_status=last_status, attempt_count=attempt, route=route)
            if last_status == 401:
                return _result(
                    "invalid_confirmed",
                    http_status=last_status,
                    error_code="http_401",
                    error="AT 会话接口返回 HTTP 401",
                    attempt_count=attempt,
                    route=route,
                )
            last_error = f"AT 会话接口返回 HTTP {last_status}"
            if last_status not in _RETRYABLE_HTTP_STATUSES:
                break
        except Exception as exc:
            last_status = None
            last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
        finally:
            if env is not None:
                env.close()
        if attempt < attempts:
            time.sleep(min(8.0, retry_delay * (2 ** (attempt - 1))))

    error_code = f"http_{last_status}" if last_status else "request_error"
    return _result(
        "check_error",
        http_status=last_status,
        error_code=error_code,
        error=last_error or "AT 检测未得到确定结论",
        attempt_count=attempt_count,
        route=route,
    )
