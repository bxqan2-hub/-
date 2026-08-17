"""Background PayPal OAICS extraction for registered accounts."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import proxy as proxy_cfg
from core import db
from core.integrated_runtime import get_pay153_module


_DEFAULT_WORKERS = 5
_QUEUE_LIMIT = 500
_LOCK = threading.RLock()
_EXECUTOR = ThreadPoolExecutor(max_workers=_DEFAULT_WORKERS, thread_name_prefix="paypal-oaics-account")
_EXECUTOR_WORKERS = _DEFAULT_WORKERS
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _proxy_entries() -> list[str]:
    entries = getattr(proxy_cfg, "PAYPAL_OAICS_PROXY_PROFILES", []) or []
    if isinstance(entries, str):
        entries = entries.splitlines()
    values = [str(item or "").strip() for item in entries if str(item or "").strip()]
    active = str(getattr(proxy_cfg, "PAYPAL_OAICS_PROXY_ACTIVE", "") or "").strip()
    if active:
        selected = []
        for item in values:
            label, _, endpoint = item.partition("|")
            if item == active or label.strip() == active:
                selected.append(endpoint.strip() if endpoint else item)
        if selected:
            return selected
    return [item.split("|", 1)[1].strip() if "|" in item else item for item in values]


def configured_proxy_pool() -> list[str]:
    pool = _proxy_entries()
    if not pool:
        raise ValueError("请先在设置→代理池填写 PayPal OAICS 专用代理池")
    module = get_pay153_module()
    normalized = module.normalize_paypal_oaics_proxies(pool)
    if not normalized:
        raise ValueError("PayPal OAICS 专用代理池为空")
    return normalized


def get_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    global _EXECUTOR, _EXECUTOR_WORKERS
    try:
        requested = int(max_workers if max_workers is not None else getattr(proxy_cfg, "PAYPAL_OAICS_WORKERS", _EXECUTOR_WORKERS))
    except (TypeError, ValueError):
        requested = _DEFAULT_WORKERS
    requested = max(1, min(32, requested))
    with _LOCK:
        if requested != _EXECUTOR_WORKERS:
            previous = _EXECUTOR
            previous.shutdown(wait=False, cancel_futures=False)
            _EXECUTOR = ThreadPoolExecutor(max_workers=requested, thread_name_prefix="paypal-oaics-account")
            _EXECUTOR_WORKERS = requested
        return _EXECUTOR


def configured_worker_count() -> int:
    try:
        workers = int(getattr(proxy_cfg, "PAYPAL_OAICS_WORKERS", _DEFAULT_WORKERS))
    except (TypeError, ValueError):
        workers = _DEFAULT_WORKERS
    return max(1, min(32, workers))


def _progress_label(stage: str, detail: str) -> str:
    text = str(detail or "").strip()
    if text.startswith("生成 Checkout "):
        return text.split("（", 1)[0].strip()
    if text.startswith("提取 PayPal 链接 "):
        return text.replace("链接 ", "", 1).replace(" · ", " / ")[:80]
    if text.startswith("0 元已确认"):
        return "\u96f6\u5143 Checkout \u5df2\u786e\u8ba4"
    if "更换代理" in text or "代理出口" in text:
        return "\u66f4\u6362\u4ee3\u7406\u91cd\u8bd5"
    if "重新生成 Checkout" in text:
        return "\u91cd\u65b0\u751f\u6210 Checkout"
    if "已取得 PayPal BA" in text:
        return "\u63d0\u94fe\u5b8c\u6210"
    return stage


def _run(account_id: int, access_token: str, proxies: list[str]) -> dict:
    try:
        if not db.mark_account_oaics_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已重置"}
        module = get_pay153_module()
        def on_log(message: str) -> None:
            raw = str(message or "").strip()
            if not raw:
                return
            stage = "\u63d0\u94fe\u5904\u7406\u4e2d"
            detail = raw
            if raw.startswith("[") and "]" in raw:
                key, _, detail = raw[1:].partition("]")
                stage = {
                    "checkout": "\u751f\u6210 Checkout",
                    "extract": "\u63d0\u53d6 PayPal \u94fe\u63a5",
                    "error": "\u63d0\u94fe\u5f02\u5e38",
                }.get(key.lower(), "\u63d0\u94fe\u5904\u7406\u4e2d")
                detail = detail.strip()
            db.update_account_oaics_extract_progress(
                account_id,
                _progress_label(stage, detail),
                detail,
            )
        result = module.run_paypal_oaics(
            access_token=access_token,
            proxies=proxies,
            checkout_attempts=5,
            provider_attempts=10,
            log=on_log,
            is_cancelled=lambda: False,
        )
        result = result if isinstance(result, dict) else {}
        result["ok"] = bool(result.get("paypal_link") or result.get("url"))
        if not result["ok"]:
            result["error"] = result.get("error") or "PayPal OAICS 未返回提链地址"
        db.update_account_oaics_extract(account_id, result)
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": _now(),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
        db.update_account_oaics_extract(account_id, result)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue(*, account_id: int, access_token: str, trigger: str = "manual", executor=None) -> dict:
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "OAICS 提链队列已满"}
    try:
        proxies = configured_proxy_pool()
        if not db.claim_account_oaics_extract(account_id, trigger=trigger):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链"}
        db.update_account_oaics_extract_progress(
            int(account_id),
            "\u4ee3\u7406\u5df2\u89e3\u6790",
            f"\u5df2\u52a0\u8f7d {len(proxies)} \u6761 PayPal OAICS \u4e13\u7528\u4ee3\u7406",
        )
        offset = int(account_id) % len(proxies)
        rotated_proxies = proxies[offset:] + proxies[:offset]
        (executor or get_executor()).submit(_run, int(account_id), str(access_token), rotated_proxies)
        return {
            "accepted": True,
            "busy": False,
            "account_id": int(account_id),
            "status": "queued",
            "proxy_count": len(proxies),
        }
    except Exception as exc:
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": False, "error": str(exc)}


__all__ = ["configured_proxy_pool", "configured_worker_count", "enqueue", "get_executor"]
