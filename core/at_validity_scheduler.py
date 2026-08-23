# -*- coding: utf-8 -*-
"""账号 AT 有效性定时调度器。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from config import at_validity as validity_cfg
from core import at_validity_service, db

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_RUN_LOCK = threading.Lock()
_WAKE = threading.Event()
_STARTED = False
_THREAD: threading.Thread | None = None
_NEXT_RUN_TS: float | None = None
_LAST_RUN_AT: str | None = None
_LAST_RESULT: dict = {}
_LAST_INTERVAL_MINUTES: int | None = None
_LAST_ENABLED: bool | None = None


def _interval_minutes() -> int:
    try:
        value = int(getattr(validity_cfg, "AT_VALIDITY_CHECK_INTERVAL_MINUTES", 360) or 360)
    except (TypeError, ValueError):
        value = 360
    return max(1, min(43_200, value))


def _recheck_interval_minutes() -> int:
    """返回已检测账号的复查周期（分钟）。"""
    try:
        value = int(getattr(validity_cfg, "AT_VALIDITY_RECHECK_INTERVAL_MINUTES", 1_440) or 1_440)
    except (TypeError, ValueError):
        value = 1_440
    return max(1, min(43_200, value))


def _account_due_for_recheck(account: dict, *, now: datetime | None = None) -> bool:
    """判断账号是否尚未检测，或已经达到复查周期。"""
    raw_checked_at = str(account.get("at_validity_checked_at") or "").strip()
    if not raw_checked_at:
        return True
    try:
        checked_at = datetime.fromisoformat(raw_checked_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        # 无法解析的历史值不能阻塞账号，下一次调度会重新建立标准时间戳。
        return True
    current = now or datetime.now(tz=checked_at.tzinfo)
    if checked_at.tzinfo is not None and current.tzinfo is None:
        current = current.replace(tzinfo=checked_at.tzinfo)
    elapsed_seconds = (current - checked_at).total_seconds()
    return elapsed_seconds >= _recheck_interval_minutes() * 60


def _enabled() -> bool:
    return bool(getattr(validity_cfg, "AT_VALIDITY_AUTO_CHECK_ENABLED", True))


def _iso_from_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")


def enqueue_accounts(*, trigger: str = "scheduled-at") -> dict:
    """把所有未归档且有 AT 的账号放入独立 AT 会话检测队列。"""
    global _LAST_RUN_AT, _LAST_RESULT
    if not _RUN_LOCK.acquire(blocking=False):
        return {"accepted": False, "busy": True, "error": "AT 定时检测正在入队"}
    try:
        accounts = db.list_accounts(limit=1_000_000, archived="0")
        result = {
            "accepted": True,
            "busy": False,
            "trigger": str(trigger or "scheduled-at"),
            "scanned_count": len(accounts),
            "started_count": 0,
            "busy_count": 0,
            "failed_count": 0,
            "skipped_no_token_count": 0,
            "skipped_recheck_count": 0,
        }
        for account in accounts:
            # 手动“立即检查 AT”保持原有的强制全量行为；自动调度才按
            # 首次检测/后续复查两个周期筛选账号。
            if str(trigger or "scheduled-at").strip().lower() == "scheduled-at" and not _account_due_for_recheck(account):
                result["skipped_recheck_count"] += 1
                continue
            token = str(account.get("access_token") or "").strip()
            if not token:
                result["skipped_no_token_count"] += 1
                continue
            queued = at_validity_service.enqueue_account_at_validity_check(
                account_id=int(account.get("id") or 0),
                email=str(account.get("email") or ""),
                access_token=token,
                trigger=str(trigger or "scheduled-at"),
            )
            if queued.get("accepted"):
                result["started_count"] += 1
            elif queued.get("busy"):
                result["busy_count"] += 1
            else:
                result["failed_count"] += 1
        _LAST_RUN_AT = datetime.now().isoformat(timespec="seconds")
        _LAST_RESULT = dict(result)
        logger.info(
            "[AT定时检测] 入队完成：started=%s busy=%s failed=%s no_token=%s skipped_recheck=%s",
            result["started_count"],
            result["busy_count"],
            result["failed_count"],
            result["skipped_no_token_count"],
            result["skipped_recheck_count"],
        )
        return result
    finally:
        _RUN_LOCK.release()


def _loop() -> None:
    global _NEXT_RUN_TS, _LAST_INTERVAL_MINUTES, _LAST_ENABLED
    while True:
        enabled = _enabled()
        interval = _interval_minutes()
        now = time.time()
        with _LOCK:
            config_changed = interval != _LAST_INTERVAL_MINUTES or enabled != _LAST_ENABLED
            if config_changed:
                _LAST_INTERVAL_MINUTES = interval
                _LAST_ENABLED = enabled
                _NEXT_RUN_TS = now + interval * 60 if enabled else None
            elif enabled and _NEXT_RUN_TS is None:
                _NEXT_RUN_TS = now + interval * 60
            next_run = _NEXT_RUN_TS

        if enabled and next_run is not None and now >= next_run:
            try:
                enqueue_accounts(trigger="scheduled-at")
            except Exception:
                logger.exception("[AT定时检测] 调度失败")
            finally:
                with _LOCK:
                    _NEXT_RUN_TS = time.time() + _interval_minutes() * 60
            continue

        wait_seconds = 30.0
        if enabled and next_run is not None:
            wait_seconds = max(0.2, min(30.0, next_run - now))
        _WAKE.wait(wait_seconds)
        _WAKE.clear()


def ensure_started() -> None:
    global _STARTED, _THREAD, _NEXT_RUN_TS, _LAST_INTERVAL_MINUTES, _LAST_ENABLED
    with _LOCK:
        if _STARTED and _THREAD is not None and _THREAD.is_alive():
            return
        _LAST_INTERVAL_MINUTES = _interval_minutes()
        _LAST_ENABLED = _enabled()
        _NEXT_RUN_TS = time.time() + _LAST_INTERVAL_MINUTES * 60 if _LAST_ENABLED else None
        _STARTED = True
        _THREAD = threading.Thread(target=_loop, name="at-validity-scheduler", daemon=True)
        _THREAD.start()


def wakeup() -> None:
    """配置热加载后唤醒调度线程，重新计算下一次执行时间。"""
    global _NEXT_RUN_TS, _LAST_INTERVAL_MINUTES, _LAST_ENABLED
    with _LOCK:
        _LAST_INTERVAL_MINUTES = _interval_minutes()
        _LAST_ENABLED = _enabled()
        _NEXT_RUN_TS = time.time() + _LAST_INTERVAL_MINUTES * 60 if _LAST_ENABLED else None
    _WAKE.set()


def status() -> dict:
    with _LOCK:
        next_run_at = _iso_from_timestamp(_NEXT_RUN_TS) if _enabled() else None
        last_result = dict(_LAST_RESULT)
    return {
        "enabled": _enabled(),
        "interval_minutes": _interval_minutes(),
        "recheck_interval_minutes": _recheck_interval_minutes(),
        "running": _RUN_LOCK.locked(),
        "last_run_at": _LAST_RUN_AT,
        "next_run_at": next_run_at,
        "last_result": last_result,
        "queue": at_validity_service.queue_settings(),
    }
