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
# 代理轮换重试：遇到代理/连接/风控类失败换下一个代理。
# None = 无上限（遍历整个代理池，每条代理至多一次）；直到有确定结果为止。
PROXY_RETRYABLE_HINTS = (
    "SSL", "ProxyError", "Connection", "timeout", "timed out",
    "Failed to perform", "OpenAI Checkout HTTP 400", "HTTP 400",
    "unusual activity", "502", "5xx",
)
MAX_PROXY_RETRIES: int | None = None


def _should_retry_with_next_proxy(result: dict) -> bool:
    error = str(result.get("error") or "")
    # 明确成功或有资格的探测不需要重试。
    if result.get("ok") or result.get("gcash"):
        return False
    # 完整探测后确认无 GCash 资格（只出现在 200 success 分支），不重试。
    if result.get("detection_outcome") == "no_cpmt_after_full_probe":
        return False
    if not error:
        return False
    return any(hint.lower() in error.lower() for hint in PROXY_RETRYABLE_HINTS)


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


def _run_with_proxy_retry(
    *,
    account_id: int,
    access_token: str,
    proxies: list[str],
    max_retries: int | None = None,
) -> dict:
    """Try candidate proxies until GCash detection reaches a definitive answer.

    ``max_retries=None`` walks the entire proxy pool once (each proxy tried at
    most once) — effectively "unlimited", bounded only by pool size. Proxy /
    connection / risk-control class failures advance to the next proxy; a
    definitive result (eligible / confirmed no-eligible) or an account-level
    failure (e.g. bad access token) stops immediately.
    """
    attempts: list[str] = []
    pool = [p for p in (proxies or []) if p]
    # None → 遍历整个池（每条代理至多一次）；指定数字 → 试探 min(n, 池长) 条。
    if max_retries is None:
        total = len(pool)
    else:
        try:
            total = max(1, min(int(max_retries), len(pool) or 1))
        except (TypeError, ValueError):
            total = len(pool)
    if total <= 0:
        total = 1
    last_result: dict = {"ok": False, "gcash": False}
    for idx in range(total):
        proxy = pool[idx] if pool else None
        try:
            result = check_gcash(access_token, proxy=proxy)
        except Exception as exc:
            result = {
                "ok": False,
                "gcash": False,
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "confirm_sent": False,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
        attempts.append(str(proxy)[:64] if proxy else "direct")
        last_result = result
        if not _should_retry_with_next_proxy(result):
            # 成功 / 确定无资格 / 账号级失败（不可换代理解决）→ 结束
            if result.get("gcash") or result.get("ok"):
                result["retried_proxies"] = attempts
                result["attempt_count"] = idx + 1
            return db.update_account_gcash(account_id, result) and result
    # 整个池子都试完仍有代理/风控类失败：记录尝试过的代理，方便用户看。
    result = dict(last_result)
    result.setdefault("ok", False)
    result["gcash"] = False
    result["retried_proxies"] = attempts
    result["attempt_count"] = total
    result["error"] = (f"已尝试 {total} 个代理仍失败；最后一个错误："
                       + str(result.get("error") or "未知"))
    db.update_account_gcash(account_id, result)
    return result


def enqueue(
    *,
    account_id: int,
    access_token: str,
    trigger: str = "manual",
    proxy: str | None = None,
    proxies: list[str] | None = None,
    max_retries: int | None = MAX_PROXY_RETRIES,
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
    proxy_candidates = [p for p in (proxies or []) if p]
    if proxy and not proxy_candidates:
        proxy_candidates = [proxy]
    try:
        if proxy_candidates:
            (executor or get_executor()).submit(
                _run_with_proxy_retry,
                account_id=account_id, access_token=access_token,
                proxies=proxy_candidates, max_retries=max_retries,
            )
        else:
            (executor or get_executor()).submit(
                _run, account_id=account_id, access_token=access_token, proxy=None,
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