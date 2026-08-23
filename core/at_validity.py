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
    """选择 AT 检测路径；优先静态检测池，也支持已配置的本地专用代理。

    显式空串表示直连。没有静态检测池和 ``PLAN_CHECK_PROXY`` 时也直连，避免
    AT 周期任务为了取代理而调用动态代理 API。
    """
    if explicit_proxy is not None:
        resolved = detection_proxy.resolve_static_detection_proxy(explicit_proxy)
        route = resolve_plan_check_route(explicit_proxy=resolved or "")
        route["proxy_source"] = "request" if resolved else None
        return route

    static_spec = detection_proxy.configured_detection_proxy_spec("plan")
    if static_spec is not None:
        resolved = detection_proxy.resolve_static_detection_proxy(static_spec)
        route = resolve_plan_check_route(explicit_proxy=resolved or "")
        route["proxy_source"] = "plan_static_pool"
        return route

    configured = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY", "") or "").strip()
    mode = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto") or "auto").strip().lower()
    if mode not in {"auto", "proxy", "direct"}:
        raise ValueError(f"PLAN_CHECK_PROXY_MODE={mode!r} 无效，可选 auto / proxy / direct")
    if configured:
        # 只验证这是普通静态/本地代理；禁止定时检测间接调用代理 API。
        detection_proxy.resolve_static_detection_proxy(configured)
    if configured or mode == "direct":
        # 这里不会落到代理池或动态 API：configured 非空时解析器会直接使用它，
        # direct 则明确直连。本地端口不可用时沿用 auto 的直连回退行为。
        return resolve_plan_check_route()
    if mode == "proxy":
        raise ValueError("AT 检测网络模式为 proxy，但未配置本地/静态检测代理")
    return resolve_plan_check_route(explicit_proxy="")


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
    max_attempts: int = 2,
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

    attempts = max(1, min(3, int(max_attempts or 1)))
    env: BrowserSession | None = None
    last_error = ""
    last_status: int | None = None
    attempt_count = 0
    try:
        env = BrowserSession(proxy=str(route.get("proxy") or ""), detect_exit_geo=False)
        headers = _request_headers(env, token)
        for attempt in range(1, attempts + 1):
            attempt_count = attempt
            try:
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
                if last_status not in _RETRYABLE_HTTP_STATUSES or attempt >= attempts:
                    break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                if attempt >= attempts:
                    break
            time.sleep(0.25)

        error_code = f"http_{last_status}" if last_status else "request_error"
        return _result(
            "check_error",
            http_status=last_status,
            error_code=error_code,
            error=last_error or "AT 检测未得到确定结论",
            attempt_count=attempt_count,
            route=route,
        )
    except Exception as exc:
        return _result(
            "check_error",
            http_status=last_status,
            error_code="request_error",
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
            attempt_count=attempt_count,
            route=route,
        )
    finally:
        if env is not None:
            env.close()
