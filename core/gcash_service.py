# -*- coding: utf-8 -*-
"""Background GCash eligibility detection for account rows.

Each task creates one minimal PH/PHP Plus Checkout through the vendored
PAY.153 service and reads back whether a ``cpmt_*`` custom payment method
(GCash) is available. It never confirms or starts a payment method, so no
payment state is advanced.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from core import db
from core.integrated_runtime import get_pay153_module


logger = logging.getLogger(__name__)

_MIN_WORKERS = 1
_DEFAULT_WORKERS = 6
_QUEUE_LIMIT = 500
_EXECUTOR_LOCK = threading.RLock()
_EXECUTOR_WORKERS = _DEFAULT_WORKERS
_EXECUTOR = ThreadPoolExecutor(max_workers=_DEFAULT_WORKERS, thread_name_prefix="gcash-check")
_RETIRED_EXECUTORS: list[ThreadPoolExecutor] = []
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def _normalize_workers(value: int | None) -> int:
    try:
        parsed = int(value if value is not None else _EXECUTOR_WORKERS)
    except (TypeError, ValueError):
        parsed = _DEFAULT_WORKERS
    return max(_MIN_WORKERS, parsed)


def get_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    global _EXECUTOR, _EXECUTOR_WORKERS
    requested = _normalize_workers(max_workers)
    with _EXECUTOR_LOCK:
        if requested != _EXECUTOR_WORKERS:
            previous = _EXECUTOR
            previous.shutdown(wait=False, cancel_futures=False)
            _RETIRED_EXECUTORS.append(previous)
            _EXECUTOR_WORKERS = requested
            _EXECUTOR = ThreadPoolExecutor(max_workers=requested, thread_name_prefix="gcash-check")
        return _EXECUTOR


def check_gcash(access_token: str, *, proxy: str | None = None) -> dict:
    body = {"token": str(access_token or "").strip()}
    if proxy:
        body["proxy"] = str(proxy).strip()
    try:
        result, status_code = get_pay153_module().detect_gcash(body)
    except Exception as exc:
        return {
            "ok": False,
            "gcash": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "confirm_sent": False,
            "error": f"GCash 检测执行异常：{type(exc).__name__}: {str(exc)[:240]}",
        }
    if not isinstance(result, dict):
        result = {"ok": False, "gcash": False, "error": "PAY.153 返回格式不正确"}
    result["checked_at"] = datetime.now().isoformat(timespec="seconds")
    result["proxy_source"] = "request" if proxy else result.get("proxy_source")
    result.setdefault("gcash", False)
    result.setdefault("confirm_sent", False)
    if status_code >= 400:
        result["ok"] = False
        result.setdefault("error", f"PAY.153 检测失败（状态 {status_code}）")
    return result


def _run(*, account_id: int, access_token: str, proxy: str | None) -> dict:
    try:
        if not db.mark_account_gcash_running(account_id):
            return {"ok": False, "gcash": False, "error": "账号已删除或检测状态已重置"}
        result = check_gcash(access_token, proxy=proxy)
        db.update_account_gcash(account_id, result)
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "gcash": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "confirm_sent": False,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
        db.update_account_gcash(account_id, result)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue(
    *,
    account_id: int,
    access_token: str,
    trigger: str = "manual",
    proxy: str | None = None,
    executor: ThreadPoolExecutor | None = None,
) -> dict:
    account_id = int(account_id)
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "GCash 检测队列已满"}
    if not db.claim_account_gcash(account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在检测 GCash 资格"}
    try:
        (executor or get_executor()).submit(
            _run, account_id=account_id, access_token=access_token, proxy=proxy,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False, "gcash": False, "confirm_sent": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"GCash 检测任务入队失败：{type(exc).__name__}: {str(exc)[:220]}",
        }
        db.update_account_gcash(account_id, result)
        return {"accepted": False, "busy": False, "error": result["error"]}
    return {"accepted": True, "busy": False, "account_id": account_id, "status": "queued"}


def queue_settings() -> dict:
    return {"workers": _EXECUTOR_WORKERS, "queue_limit": _QUEUE_LIMIT}


__all__ = ["check_gcash", "enqueue", "get_executor", "queue_settings"]