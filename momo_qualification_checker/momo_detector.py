"""MoMo qualification adapter.

This module deliberately keeps AT values in memory only.  It delegates the
actual VN/VND qualification probe to the vendored PAY.153 detector and strips
credential-like fields before returning a result to the UI.
"""
from __future__ import annotations

import re
from typing import Any

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.integrated_runtime import get_pay153_module


def mask_token(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 12:
        return "*" * len(text)
    return f"{text[:6]}…{text[-4:]}"


def mask_proxy(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "直连"
    # Keep only scheme/host/port in diagnostics; never expose proxy auth.
    return re.sub(r"(://)([^/@]+):([^/@]+)@", r"\1***:***@", text)[:120]


def check_momo(access_token: str, proxy: str = "") -> dict[str, Any]:
    """Run exactly one non-payment MoMo probe and return a redacted result."""
    token = str(access_token or "").strip()
    if not token:
        return {"ok": False, "momo": False, "error": "AT 不能为空"}
    try:
        payload, status = get_pay153_module().detect_momo({"token": token, "proxy": str(proxy or "").strip()})
    except Exception as exc:
        return {
            "ok": False,
            "momo": False,
            "http_status": 502,
            "error": f"检测执行异常：{type(exc).__name__}",
            "token_preview": mask_token(token),
            "proxy_preview": mask_proxy(proxy),
        }
    result = dict(payload) if isinstance(payload, dict) else {"ok": False, "momo": False}
    # Never return raw AT, Checkout URLs, cookies, or backend session blobs.
    for key in ("token", "access_token", "checkout_url", "url", "source_checkout_url", "session_id"):
        result.pop(key, None)
    result["http_status"] = int(status)
    result["token_preview"] = mask_token(token)
    result["proxy_preview"] = mask_proxy(proxy)
    result["error"] = str(result.get("error") or "")[:300] or None
    return result
