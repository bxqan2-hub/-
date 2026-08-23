# -*- coding: utf-8 -*-
"""Access Token 有效性结果归一化。

区分三种结论，避免把代理、限流或网络错误误判成 AT 已失效：
``valid``、``invalid_confirmed``、``check_error``。
"""
from __future__ import annotations

from typing import Any


VALIDITY_OUTCOMES = {"valid", "invalid_confirmed", "check_error"}


def classify_plan_check_result(result: dict[str, Any] | None) -> dict[str, Any]:
    """把套餐接口结果归一化成独立 AT 有效性结果。"""
    value = result if isinstance(result, dict) else {}
    checked_at = value.get("checked_at")
    http_status = value.get("http_status")
    try:
        http_status = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        http_status = None

    if value.get("ok") is True:
        return {
            "outcome": "valid",
            "valid": True,
            "checked_at": checked_at,
            "http_status": http_status,
            "error_code": "",
            "error": "",
        }

    credential_code = str(value.get("credential_unusable_code") or "").strip()
    account_code = str(value.get("account_unusable_code") or "").strip()
    token_expired = value.get("token_expired") is True
    if token_expired or http_status == 401 or credential_code or account_code:
        error_code = credential_code or account_code or ("http_401" if http_status == 401 else "token_expired")
        return {
            "outcome": "invalid_confirmed",
            "valid": False,
            "checked_at": checked_at,
            "http_status": http_status,
            "error_code": error_code,
            "error": str(value.get("error") or "AT 已确认失效")[:500],
        }

    return {
        "outcome": "check_error",
        "valid": None,
        "checked_at": checked_at,
        "http_status": http_status,
        "error_code": str(value.get("error_code") or "check_error")[:100],
        "error": str(value.get("error") or "AT 检测未得到确定结论")[:500],
    }
