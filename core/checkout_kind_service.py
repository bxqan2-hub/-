# -*- coding: utf-8 -*-
"""Background OAICS/CSLIVE detection backed by the bundled PAY.153 service."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from core import db, detection_proxy
from core.chatgpt_plan import resolve_plan_check_route
from core.integrated_runtime import get_pay153_module


_MIN_WORKERS = 1
_DEFAULT_WORKERS = 10
_QUEUE_LIMIT = 500
_EXECUTOR_LOCK = threading.RLock()
_EXECUTOR_WORKERS = _DEFAULT_WORKERS
_EXECUTOR = ThreadPoolExecutor(max_workers=_DEFAULT_WORKERS, thread_name_prefix="checkout-kind")
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
            _EXECUTOR = ThreadPoolExecutor(max_workers=requested, thread_name_prefix="checkout-kind")
        return _EXECUTOR


def check_checkout_kind(access_token: str, *, proxy: str | None = None) -> dict:
    body = {"token": str(access_token or "").strip()}
    try:
        selected_proxy = proxy if proxy is not None else (
            detection_proxy.configured_detection_proxy_spec("checkout") or ""
        )
        route = resolve_plan_check_route(detection_proxy.resolve_detection_proxy(selected_proxy))
    except Exception as exc:
        return {
            "ok": False,
            "kind": "unknown",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "confirm_sent": False,
            "error": f"Checkout 检测网络配置错误：{exc}",
        }
    if route.get("proxy"):
        body["proxy"] = str(route["proxy"]).strip()
    try:
        result, status_code = get_pay153_module().detect_checkout_kind(body)
    except Exception as exc:
        return {
            "ok": False,
            "kind": "unknown",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "confirm_sent": False,
            "error": f"PAY.153 检测执行异常：{type(exc).__name__}: {str(exc)[:240]}",
        }
    if not isinstance(result, dict):
        result = {"ok": False, "error": "PAY.153 返回格式不正确"}
    result["checked_at"] = datetime.now().isoformat(timespec="seconds")
    result["network_route"] = route.get("network_route")
    result["proxy_source"] = route.get("proxy_source") or result.get("proxy_source")
    result.setdefault("kind", "unknown")
    result.setdefault("confirm_sent", False)
    if status_code >= 400:
        result["ok"] = False
        result.setdefault("error", f"PAY.153 检测失败（状态 {status_code}）")
    return result


def _run(*, account_id: int, access_token: str, proxy: str | None) -> dict:
    try:
        if not db.mark_account_checkout_kind_running(account_id):
            return {"ok": False, "error": "账号已删除或检测状态已重置"}
        result = check_checkout_kind(access_token, proxy=proxy)
        db.update_account_checkout_kind(account_id, result)
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "kind": "unknown",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "confirm_sent": False,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
        db.update_account_checkout_kind(account_id, result)
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
        return {"accepted": False, "busy": False, "error": "Checkout 类型检测队列已满"}
    if not db.claim_account_checkout_kind(account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在检测 Checkout 类型"}
    try:
        (executor or get_executor()).submit(
            _run, account_id=account_id, access_token=access_token, proxy=proxy,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False, "kind": "unknown", "confirm_sent": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"检测任务入队失败：{type(exc).__name__}: {str(exc)[:220]}",
        }
        db.update_account_checkout_kind(account_id, result)
        return {"accepted": False, "busy": False, "error": result["error"]}
    return {"accepted": True, "busy": False, "account_id": account_id, "status": "queued"}


def queue_settings() -> dict:
    return {"workers": _EXECUTOR_WORKERS, "queue_limit": _QUEUE_LIMIT}


__all__ = ["check_checkout_kind", "enqueue", "get_executor", "queue_settings"]
