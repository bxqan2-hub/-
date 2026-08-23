# -*- coding: utf-8 -*-
"""Background GCash eligibility detection for account rows.

Each task creates one minimal PH/PHP Plus Checkout through the vendored
PAY.153 service and reads explicit GCash evidence from that creation response.
It does not classify the Checkout backend, poll session state, update taxes,
confirm, or start a payment method.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from core import account_operation_control, db
from core.integrated_runtime import get_pay153_module


logger = logging.getLogger(__name__)

_MIN_WORKERS = 1
DEFAULT_WORKERS = 8
MAX_WORKERS = 32
_DEFAULT_WORKERS = DEFAULT_WORKERS
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
    "OpenAI Checkout HTTP 429", "HTTP 429", "rate limit", "too many requests",
    "unusual activity", "Sentinel", "SENTINEL_INIT_BLOCKED", "502", "5xx",
)
MAX_PROXY_RETRIES: int | None = None
# 单个账号整个探测的硬性总耗时上限（秒）。超过即中止并释放线程，
# 防止 100 条短命代理（-t-15）配合无上限重试把 worker 线程全部占死。
PROBE_TOTAL_TIMEOUT = 90.0


def _should_retry_with_next_proxy(result: dict) -> bool:
    error = str(result.get("error") or "")
    # 明确成功或有资格的探测不需要重试。
    if result.get("ok") or result.get("gcash"):
        return False
    # 完整探测后确认无 GCash 资格（只出现在 200 success 分支），不重试。
    if result.get("detection_outcome") in {
        "no_cpmt_after_full_probe", "no_gcash_in_create_response",
    }:
        return False
    if not error:
        return False
    return any(hint.lower() in error.lower() for hint in PROXY_RETRYABLE_HINTS)


def proxy_transport_candidates(proxy: str | None) -> list[str | None]:
    """Try HTTPS proxy transport after the same HTTP proxy tunnel fails."""
    value = str(proxy or "").strip()
    if not value:
        return [None]
    candidates: list[str | None] = [value]
    parsed = urlsplit(value)
    if parsed.scheme.lower() == "http":
        candidates.append(urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment)))
    return candidates


def _is_proxy_transport_failure(result: dict) -> bool:
    error = str(result.get("error") or "").lower()
    return any(marker in error for marker in (
        "connect tunnel failed", "curl: (7)", "could not connect to proxy",
        "could not resolve proxy", "proxyerror", "connection refused",
    ))


def _retry_delay_seconds(result: dict, retry_index: int) -> float:
    error = str(result.get("error") or "").lower()
    upstream_status = result.get("upstream_http_status")
    if upstream_status == 429 or "http 429" in error or "rate limit" in error or "too many requests" in error:
        return min(4.0, 0.75 * (2 ** max(0, int(retry_index))))
    if "unusual activity" in error or "http 400" in error:
        return min(1.5, 0.35 * (max(0, int(retry_index)) + 1))
    return 0.0


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
    result: dict = {"ok": False, "gcash": False}
    status_code = 502
    used_transport = ""
    transport_attempts = 0
    for candidate in proxy_transport_candidates(proxy):
        transport_attempts += 1
        body = {"token": str(access_token or "").strip()}
        if candidate:
            body["proxy"] = candidate
            used_transport = urlsplit(candidate).scheme.lower()
        try:
            result, status_code = get_pay153_module().detect_gcash(body)
        except Exception as exc:
            result = {
                "ok": False,
                "gcash": False,
                "confirm_sent": False,
                "error": f"GCash 检测执行异常：{type(exc).__name__}: {str(exc)[:240]}",
            }
            status_code = 502
        if not isinstance(result, dict):
            result = {"ok": False, "gcash": False, "error": "PAY.153 返回格式不正确"}
        if status_code < 400 or not _is_proxy_transport_failure(result):
            break
    result["checked_at"] = datetime.now().isoformat(timespec="seconds")
    result["proxy_source"] = "request" if proxy else result.get("proxy_source")
    result["proxy_transport"] = used_transport or ("direct" if not proxy else "")
    result["transport_attempt_count"] = transport_attempts
    result["http_status"] = status_code
    result.setdefault("gcash", False)
    result.setdefault("confirm_sent", False)
    if status_code >= 400:
        result["ok"] = False
        result.setdefault("error", f"PAY.153 检测失败（状态 {status_code}）")
    return result


def _run(*, account_id: int, access_token: str, proxy: str | None, generation: int | None = None) -> dict:
    operation_generation = account_operation_control.snapshot() if generation is None else int(generation)
    try:
        account_operation_control.raise_if_cancelled(operation_generation)
        if not db.mark_account_gcash_running(account_id):
            return {"ok": False, "gcash": False, "error": "账号已删除或检测状态已重置"}
        result = check_gcash(access_token, proxy=proxy)
        account_operation_control.raise_if_cancelled(operation_generation)
        db.update_account_gcash(account_id, result)
        return result
    except account_operation_control.AccountOperationStopped:
        result = {
            "ok": False,
            "gcash": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "confirm_sent": False,
            "error": "GCash 检测已停止",
            "stopped": True,
        }
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


def _run_with_proxy_retry_impl(
    *,
    account_id: int,
    access_token: str,
    proxies: list[str],
    max_retries: int | None = None,
    total_timeout: float | None = PROBE_TOTAL_TIMEOUT,
    generation: int | None = None,
) -> dict:
    """Try candidate proxies until GCash detection reaches a definitive answer.

    ``max_retries=None`` walks the entire proxy pool once (each proxy tried at
    most once) — effectively "unlimited", bounded only by pool size. Proxy /
    connection / risk-control class failures advance to the next proxy; a
    definitive result (eligible / confirmed no-eligible) or an account-level
    failure (e.g. bad access token) stops immediately.

    A hard total-time watchdog (``total_timeout`` seconds) aborts the loop so a
    long-lived proxy pool of short-lived sessions (e.g. ``-t-15``) cannot pin
    worker threads forever.
    """
    operation_generation = account_operation_control.snapshot() if generation is None else int(generation)
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
    deadline = time.monotonic() + total_timeout if total_timeout and total_timeout > 0 else None
    last_result: dict = {"ok": False, "gcash": False}
    timed_out = False
    for idx in range(total):
        account_operation_control.raise_if_cancelled(operation_generation)
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break
        proxy = pool[idx] if pool else None
        try:
            result = check_gcash(access_token, proxy=proxy)
            account_operation_control.raise_if_cancelled(operation_generation)
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
        delay = _retry_delay_seconds(result, idx)
        if delay > 0 and idx + 1 < total:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                delay = min(delay, remaining)
            # Direct callers (including the legacy synchronous API) do not
            # provide a generation and retain the original single sleep call;
            # queued account-page workers pass a generation and get a
            # stop-aware wait instead.
            if generation is None:
                time.sleep(delay)
            elif not account_operation_control.wait(delay, operation_generation):
                account_operation_control.raise_if_cancelled(operation_generation)
    # 整个池子都试完 / 总时长超时仍失败：记录尝试过的代理，方便用户看。
    result = dict(last_result)
    result.setdefault("ok", False)
    result["gcash"] = False
    result["retried_proxies"] = attempts
    result["attempt_count"] = len(attempts)
    if timed_out:
        result["error"] = (f"检测总耗时超过 {int(total_timeout or 0)} 秒中止（已换 {len(attempts)} 个代理）；"
                           + str(result.get("error") or "未知"))
    else:
        result["error"] = (f"已尝试 {len(attempts)} 个代理仍失败；最后一个错误："
                           + str(result.get("error") or "未知"))
    db.update_account_gcash(account_id, result)
    return result


def _run_with_proxy_retry(**kwargs) -> dict:
    """带全局停止结果落库的代理轮换包装器。"""
    try:
        return _run_with_proxy_retry_impl(**kwargs)
    except account_operation_control.AccountOperationStopped:
        account_id = int(kwargs.get("account_id") or 0)
        result = {
            "ok": False,
            "gcash": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": "GCash 检测已停止",
            "stopped": True,
        }
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
    operation_generation = account_operation_control.snapshot()
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
                generation=operation_generation,
            )
        else:
            (executor or get_executor()).submit(
                _run, account_id=account_id, access_token=access_token, proxy=None,
                generation=operation_generation,
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
    return {
        "workers": _EXECUTOR_WORKERS,
        "default_workers": DEFAULT_WORKERS,
        "max_workers": MAX_WORKERS,
        "queue_limit": _QUEUE_LIMIT,
    }


__all__ = [
    "DEFAULT_WORKERS", "MAX_WORKERS", "check_gcash", "enqueue",
    "get_executor", "proxy_transport_candidates", "queue_settings",
]
