# -*- coding: utf-8 -*-
"""Background MoMo trial/payment-method qualification checks."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from core import account_operation_control, db
from core.integrated_runtime import get_pay153_module

DEFAULT_WORKERS = 8
MAX_WORKERS = 32
MAX_PROXY_RETRIES: int | None = None
PROBE_TOTAL_TIMEOUT = 90.0
_QUEUE_LIMIT = 500
_EXECUTOR_LOCK = threading.RLock()
_EXECUTOR_WORKERS = DEFAULT_WORKERS
_EXECUTOR = ThreadPoolExecutor(max_workers=DEFAULT_WORKERS, thread_name_prefix="momo-check")
_RETIRED_EXECUTORS: list[ThreadPoolExecutor] = []
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RETRYABLE_HINTS = (
    "ssl", "proxyerror", "connection", "timeout", "timed out", "failed to perform",
    "http 400", "http 429", "rate limit", "too many requests", "unusual activity",
    "sentinel", "502", "5xx",
)


def get_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    global _EXECUTOR, _EXECUTOR_WORKERS
    try:
        requested = max(1, min(MAX_WORKERS, int(max_workers or _EXECUTOR_WORKERS)))
    except (TypeError, ValueError):
        requested = DEFAULT_WORKERS
    with _EXECUTOR_LOCK:
        if requested != _EXECUTOR_WORKERS:
            previous = _EXECUTOR
            previous.shutdown(wait=False, cancel_futures=False)
            _RETIRED_EXECUTORS.append(previous)
            _EXECUTOR_WORKERS = requested
            _EXECUTOR = ThreadPoolExecutor(max_workers=requested, thread_name_prefix="momo-check")
        return _EXECUTOR


def _transport_candidates(proxy: str | None) -> list[str | None]:
    value = str(proxy or "").strip()
    if not value:
        return [None]
    candidates: list[str | None] = [value]
    parsed = urlsplit(value)
    if parsed.scheme.lower() == "http":
        candidates.append(urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment)))
    return candidates


def _transport_failed(result: dict) -> bool:
    error = str(result.get("error") or "").lower()
    return any(value in error for value in ("connect tunnel failed", "curl: (7)", "could not connect to proxy", "could not resolve proxy", "proxyerror", "connection refused"))


def check_momo(access_token: str, *, proxy: str | None = None, trial_days: int = 30) -> dict:
    result: dict = {"ok": False, "momo": False}
    status_code = 502
    used_transport = ""
    attempts = 0
    for candidate in _transport_candidates(proxy):
        attempts += 1
        body = {"token": str(access_token or "").strip(), "trial_days": int(trial_days or 30)}
        if candidate:
            body["proxy"] = candidate
            used_transport = urlsplit(candidate).scheme.lower()
        try:
            result, status_code = get_pay153_module().detect_momo(body)
        except Exception as exc:
            result = {"ok": False, "momo": False, "error": f"MoMo 检测执行异常：{type(exc).__name__}: {str(exc)[:240]}"}
            status_code = 502
        if not isinstance(result, dict):
            result = {"ok": False, "momo": False, "error": "PAY.153 返回格式不正确"}
        if status_code < 400 or not _transport_failed(result):
            break
    result["checked_at"] = result.get("checked_at") or datetime.now().isoformat(timespec="seconds")
    result["proxy_source"] = "request" if proxy else result.get("proxy_source")
    result["proxy_transport"] = used_transport or ("direct" if not proxy else "")
    result["transport_attempt_count"] = attempts
    result["http_status"] = status_code
    result.setdefault("momo", False)
    result.setdefault("confirm_sent", False)
    if status_code >= 400:
        result["ok"] = False
        result.setdefault("error", f"PAY.153 MoMo 检测失败（状态 {status_code}）")
    return result


def _retryable(result: dict) -> bool:
    if result.get("ok") or result.get("momo"):
        return False
    if result.get("detection_outcome") in {"no_momo_in_stripe_init", "account_trial_ineligible", "unsupported_custom_checkout"}:
        return False
    error = str(result.get("error") or "").lower()
    return bool(error and any(hint in error for hint in _RETRYABLE_HINTS))


def _run_with_proxy_retry(*, account_id: int, access_token: str, proxies: list[str], max_retries: int | None = None, total_timeout: float | None = PROBE_TOTAL_TIMEOUT, generation: int | None = None) -> dict:
    operation_generation = account_operation_control.snapshot() if generation is None else int(generation)
    if not db.mark_account_momo_running(account_id):
        return {"ok": False, "momo": False, "error": "账号已删除或检测状态已重置"}
    pool = [str(value).strip() for value in proxies if str(value).strip()]
    total = len(pool) if max_retries is None else min(len(pool), max(1, int(max_retries)))
    deadline = time.monotonic() + float(total_timeout) if total_timeout else None
    attempted: list[str] = []
    result: dict = {"ok": False, "momo": False, "error": "没有可用的 VN 代理"}
    try:
        for index, proxy in enumerate(pool[:total]):
            account_operation_control.raise_if_cancelled(operation_generation)
            if deadline is not None and time.monotonic() >= deadline:
                result["error"] = f"MoMo 检测总耗时超过 {int(total_timeout or 0)} 秒"
                break
            result = check_momo(access_token, proxy=proxy)
            account_operation_control.raise_if_cancelled(operation_generation)
            attempted.append(proxy[:64])
            if not _retryable(result):
                break
        result["retried_proxies"] = attempted
        result["attempt_count"] = len(attempted)
    except account_operation_control.AccountOperationStopped:
        result = {"ok": False, "momo": False, "checked_at": datetime.now().isoformat(timespec="seconds"), "error": "MoMo 检测已停止", "stopped": True, "retried_proxies": attempted, "attempt_count": len(attempted)}
    except Exception as exc:
        result = {"ok": False, "momo": False, "checked_at": datetime.now().isoformat(timespec="seconds"), "error": f"{type(exc).__name__}: {str(exc)[:300]}", "retried_proxies": attempted, "attempt_count": len(attempted)}
    db.update_account_momo(account_id, result)
    return result


def _queued_run(**kwargs) -> dict:
    try:
        return _run_with_proxy_retry(**kwargs)
    finally:
        _QUEUE_SLOTS.release()


def enqueue(*, account_id: int, access_token: str, proxies: list[str], trigger: str = "manual", max_retries: int | None = MAX_PROXY_RETRIES, executor: ThreadPoolExecutor | None = None) -> dict:
    account_id = int(account_id)
    token = str(access_token or "").strip()
    candidates = [str(value).strip() for value in proxies if str(value).strip()]
    if not token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not candidates:
        return {"accepted": False, "busy": False, "error": "MoMo 检测没有可用的 VN 代理"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "MoMo 检测队列已满"}
    if not db.claim_account_momo(account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在检测 MoMo 资格"}
    try:
        (executor or get_executor()).submit(_queued_run, account_id=account_id, access_token=token, proxies=candidates, max_retries=max_retries, generation=account_operation_control.snapshot())
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {"ok": False, "momo": False, "error": f"MoMo 检测任务入队失败：{type(exc).__name__}: {str(exc)[:220]}"}
        db.update_account_momo(account_id, result)
        return {"accepted": False, "busy": False, "error": result["error"]}
    return {"accepted": True, "busy": False, "account_id": account_id, "status": "queued"}


__all__ = ["DEFAULT_WORKERS", "MAX_WORKERS", "MAX_PROXY_RETRIES", "check_momo", "enqueue", "get_executor"]
