# -*- coding: utf-8 -*-
"""独立 AT 会话有效性后台队列。"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from config import at_validity as validity_cfg
from core import account_operation_control, db
from core.at_validity import check_access_token_validity


logger = logging.getLogger(__name__)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(validity_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(validity_cfg, name, default) or 0.0)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("AT_VALIDITY_WORKERS", 5, 1, 32)
_QUEUE_LIMIT = _int_setting("AT_VALIDITY_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="at-validity")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING_LOCK = threading.Lock()
_RUNNING: set[int] = set()
_AUTO_RETRY_LOCK = threading.Lock()
_AUTO_RETRY_COUNTS: dict[int, int] = {}
_AUTO_RETRY_MAX_ROUNDS = 2
_AUTO_RETRY_DELAY_SECONDS = 5.0
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _is_transient_result(result: dict) -> bool:
    """Return whether a failed probe should be retried as a transport event."""
    if str(result.get("outcome") or "") != "check_error":
        return False
    code = str(result.get("error_code") or "").strip().lower()
    return code == "request_error" or code in {"http_408", "http_425", "http_429", "http_500", "http_502", "http_503", "http_504"}


def _clear_auto_retry(account_id: int) -> None:
    with _AUTO_RETRY_LOCK:
        _AUTO_RETRY_COUNTS.pop(int(account_id), None)


def _schedule_transient_retry(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None,
    result: dict,
    generation: int,
) -> bool:
    """Requeue transient AT failures before exposing ``check_error`` in the UI.

    The probe itself already retries a request several times. This second layer
    handles a whole short outage (for example the local VPN/TUN being
    temporarily unreachable) without leaving a stale “网络错误” result as the
    final state. It is bounded so a persistent outage still becomes visible.
    """
    if not _is_transient_result(result):
        return False
    account_id = int(account_id)
    with _AUTO_RETRY_LOCK:
        round_no = _AUTO_RETRY_COUNTS.get(account_id, 0)
        if round_no >= _AUTO_RETRY_MAX_ROUNDS:
            _AUTO_RETRY_COUNTS.pop(account_id, None)
            return False
        _AUTO_RETRY_COUNTS[account_id] = round_no + 1

    def retry_later() -> None:
        if account_operation_control.is_cancelled(generation):
            _clear_auto_retry(account_id)
            return
        queued = enqueue_account_at_validity_check(
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=f"{trigger}-retry{round_no + 1}",
            proxy=proxy,
        )
        if not queued.get("accepted"):
            _clear_auto_retry(account_id)
            try:
                db.update_account_at_validity(account_id, result, trigger=trigger)
            except Exception:
                logger.exception("[AT检测] 自动重试入队失败后写回结果失败: account_id=%s", account_id)

    timer = threading.Timer(_AUTO_RETRY_DELAY_SECONDS, retry_later)
    timer.daemon = True
    timer.start()
    logger.warning(
        "[AT检测] 临时网络错误，%s 秒后自动重新查询 account_id=%s round=%s/%s",
        _AUTO_RETRY_DELAY_SECONDS,
        account_id,
        round_no + 1,
        _AUTO_RETRY_MAX_ROUNDS,
    )
    return True


def _wait_for_rate_slot(generation: int | None = None) -> None:
    global _NEXT_REQUEST_AT
    min_interval = _float_setting("AT_VALIDITY_MIN_INTERVAL", 0.4, 0.0, 30.0)
    jitter = _float_setting("AT_VALIDITY_JITTER", 0.2, 0.0, 30.0)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + min_interval
    if scheduled > now:
        delay = scheduled - now
        if generation is None:
            time.sleep(delay)
        elif not account_operation_control.wait(delay, generation):
            account_operation_control.raise_if_cancelled(generation)


def _run_at_validity_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None,
    generation: int | None = None,
) -> dict:
    operation_generation = account_operation_control.snapshot() if generation is None else int(generation)
    try:
        account_operation_control.raise_if_cancelled(operation_generation)
        _wait_for_rate_slot(operation_generation if generation is not None else None)
        account_operation_control.raise_if_cancelled(operation_generation)
        result = check_access_token_validity(
            access_token,
            proxy=proxy,
            max_attempts=_int_setting("AT_VALIDITY_REQUEST_ATTEMPTS", 5, 1, 10),
            retry_delay=_float_setting("AT_VALIDITY_RETRY_DELAY", 1.0, 0.0, 30.0),
        )
        account_operation_control.raise_if_cancelled(operation_generation)
        if _schedule_transient_retry(
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=trigger,
            proxy=proxy,
            result=result,
            generation=operation_generation,
        ):
            # Keep the last confirmed state while the short automatic retry
            # window is active; only the final exhausted result is persisted.
            return {**result, "auto_retry_scheduled": True}
        if str(result.get("outcome") or "") != "check_error":
            _clear_auto_retry(account_id)
        db.update_account_at_validity(account_id, result, trigger=trigger)
        logger.info(
            "[AT检测] 完成: email=%s outcome=%s route=%s trigger=%s",
            email,
            result.get("outcome"),
            result.get("network_route") or "unknown",
            trigger,
        )
        return result
    except account_operation_control.AccountOperationStopped:
        result = {
            "outcome": "check_error",
            "valid": None,
            "error_code": "stopped",
            "error": "AT 检测已停止",
            "stopped": True,
        }
        try:
            db.update_account_at_validity(account_id, result, trigger=trigger)
        except Exception:
            logger.exception("[AT检测] 写入停止状态失败: account_id=%s", account_id)
        return result
    except Exception as exc:
        result = {
            "outcome": "check_error",
            "valid": None,
            "error_code": "worker_error",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
        try:
            db.update_account_at_validity(account_id, result, trigger=trigger)
        except Exception:
            logger.exception("[AT检测] 写入后台异常状态失败: account_id=%s", account_id)
        logger.exception("[AT检测] 后台查询异常: %s", email)
        return result
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(account_id)
        _QUEUE_SLOTS.release()


def enqueue_account_at_validity_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None = None,
    executor: ThreadPoolExecutor | None = None,
) -> dict:
    """提交独立 AT 检测；不占用套餐队列，也不写套餐/试用字段。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    operation_generation = account_operation_control.snapshot()
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "AT 检测队列已满，请稍后重试"}
    with _RUNNING_LOCK:
        if account_id in _RUNNING:
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在检测 AT"}
        _RUNNING.add(account_id)

    try:
        (executor or _EXECUTOR).submit(
            _run_at_validity_check,
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=str(trigger or "manual-at"),
            proxy=proxy,
            generation=operation_generation,
        )
    except Exception as exc:
        with _RUNNING_LOCK:
            _RUNNING.discard(account_id)
        _QUEUE_SLOTS.release()
        return {
            "accepted": False,
            "busy": False,
            "error": f"AT 检测入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual-at"),
    }


def queue_settings() -> dict:
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "request_attempts": _int_setting("AT_VALIDITY_REQUEST_ATTEMPTS", 5, 1, 10),
        "retry_delay": _float_setting("AT_VALIDITY_RETRY_DELAY", 1.0, 0.0, 30.0),
        "min_interval": _float_setting("AT_VALIDITY_MIN_INTERVAL", 0.4, 0.0, 30.0),
        "jitter": _float_setting("AT_VALIDITY_JITTER", 0.2, 0.0, 30.0),
        "auto_retry_rounds": _AUTO_RETRY_MAX_ROUNDS,
        "auto_retry_delay": _AUTO_RETRY_DELAY_SECONDS,
    }
