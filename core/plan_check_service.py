# -*- coding: utf-8 -*-
"""套餐/Plus 资格查询后台队列。"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import proxy as proxy_cfg
from core import db, detection_proxy
from core.chatgpt_plan import check_account_plan

logger = logging.getLogger(__name__)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(proxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(proxy_cfg, name, default) or 0.0)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_MIN_WORKERS = 1
_DEFAULT_WORKERS = max(_MIN_WORKERS, int(getattr(proxy_cfg, "PLAN_CHECK_WORKERS", 10) or 10))
_QUEUE_LIMIT = _int_setting("PLAN_CHECK_QUEUE_LIMIT", 500, _DEFAULT_WORKERS, 5000)
_EXECUTOR_LOCK = threading.RLock()
_EXECUTOR_WORKERS = _DEFAULT_WORKERS
_EXECUTOR_GENERATION = 1
_EXECUTOR = ThreadPoolExecutor(
    max_workers=_EXECUTOR_WORKERS,
    thread_name_prefix=f"plan-check-{_EXECUTOR_GENERATION}",
)
_RETIRED_EXECUTORS: list[ThreadPoolExecutor] = []
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _normalize_workers(max_workers: int | None) -> int:
    if max_workers is None:
        return _DEFAULT_WORKERS
    try:
        value = int(max_workers)
    except (TypeError, ValueError):
        value = _DEFAULT_WORKERS
    return max(_MIN_WORKERS, value)


def get_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    """返回套餐查询线程池，并让新批次即时使用本次指定的并发数。

    已经提交到旧线程池的查询会继续完成；切换只影响后续提交，避免修改线程数时
    中断正在查询的账号。
    """
    global _EXECUTOR, _EXECUTOR_WORKERS, _EXECUTOR_GENERATION
    requested_workers = _normalize_workers(max_workers) if max_workers is not None else _EXECUTOR_WORKERS
    with _EXECUTOR_LOCK:
        if requested_workers != _EXECUTOR_WORKERS:
            old_executor = _EXECUTOR
            old_executor.shutdown(wait=False, cancel_futures=False)
            _RETIRED_EXECUTORS.append(old_executor)
            logger.info(
                "[Plan] 套餐查询线程池 workers 从 %s 切换为 %s；旧池继续处理已提交任务",
                _EXECUTOR_WORKERS,
                requested_workers,
            )
            _EXECUTOR_WORKERS = requested_workers
            _EXECUTOR_GENERATION += 1
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=requested_workers,
                thread_name_prefix=f"plan-check-{_EXECUTOR_GENERATION}",
            )
        return _EXECUTOR


def get_executor_workers() -> int:
    with _EXECUTOR_LOCK:
        return _EXECUTOR_WORKERS


def _wait_for_rate_slot() -> None:
    """为所有查询线程分配错开的请求启动时间。"""
    global _NEXT_REQUEST_AT
    min_interval = _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0)
    jitter = _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _resolve_plan_check_proxy(spec: str | None, account_id: int) -> str | None:
    del account_id
    if spec is None:
        # 显式空串让 check_account_plan 直连，避免空检测池回退到注册用动态 API。
        return ""
    return detection_proxy.resolve_static_detection_proxy(spec)


def _run_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None,
    timezone_offset_min: str,
) -> dict:
    try:
        if not db.mark_account_plan_check_running(account_id):
            return {"ok": False, "error": "账号已删除或套餐查询状态已被重置"}

        _wait_for_rate_slot()
        selected_proxy = proxy if proxy is not None else detection_proxy.configured_detection_proxy_spec("plan")
        resolved_proxy = _resolve_plan_check_proxy(selected_proxy, account_id)
        resolved_timezone = detection_proxy.infer_timezone_offset_min(selected_proxy, timezone_offset_min)
        result = check_account_plan(
            access_token,
            proxy=resolved_proxy,
            timezone_offset_min=resolved_timezone,
            max_attempts=0,
            fast_mode=True,
            continue_check=lambda: bool(
                (db.get_account(account_id) or {}).get("plan_check_status") == "running"
            ),
            retry_proxy_provider=lambda: _resolve_plan_check_proxy(
                detection_proxy.configured_detection_proxy_spec("plan") if proxy is None else selected_proxy,
                account_id,
            ),
        )
        if result.get("ok") and not result.get("plan_detection_source"):
            result["plan_detection_source"] = "account_access_token"

        db.update_account_plan_check(acc_id=account_id, result=result)
        if result.get("has_active_plus_subscription"):
            try:
                from core.gc_registration_service import close_plus_window_for_account
                close_result = close_plus_window_for_account(account_id)
                if close_result.get("closed") or close_result.get("deleted"):
                    logger.info("[Plan] Plus 已到账，已关闭账号 %s 的 GC 窗口", email)
            except Exception:
                # 套餐查询结果已经成功落库；关窗失败留给账号行的停止按钮重试。
                logger.exception("[Plan] Plus 已到账但关闭 GC 窗口失败：account_id=%s", account_id)
        if result.get("account_unusable_code"):
            db.update_account_liveness(account_id, {
                "ok": False,
                "status": "deactivated",
                "checked_at": result.get("checked_at"),
                "error": result.get("account_unusable_code"),
            })
        if result.get("ok"):
            logger.info(
                "[Plan] 后台查询成功: %s, plan=%s, plus_trial=%s, trigger=%s",
                email,
                result.get("current_plan_type") or "unknown",
                bool(result.get("plus_trial_eligible")),
                trigger,
            )
        else:
            logger.warning(
                "[Plan] 后台查询失败: %s, trigger=%s, error=%s",
                email,
                trigger,
                result.get("error") or "未知错误",
            )
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        try:
            db.update_account_plan_check(acc_id=account_id, result=result)
        except Exception:
            logger.exception("[Plan] 写入后台查询异常状态失败: account_id=%s", account_id)
        logger.exception("[Plan] 后台查询异常: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None = None,
    timezone_offset_min: str = "-",
    executor: ThreadPoolExecutor | None = None,
) -> dict:
    """把查询放入统一线程池；重复查询或队列满时不提交。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "套餐查询队列已满，请稍后重试"}

    if not db.claim_account_plan_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询套餐"}

    try:
        (executor or get_executor()).submit(
            _run_plan_check,
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=str(trigger or "manual"),
            proxy=proxy,
            timezone_offset_min=str(timezone_offset_min or "-"),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"套餐查询入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_plan_check(acc_id=account_id, result=result)
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {
        "workers": get_executor_workers(),
        "queue_limit": _QUEUE_LIMIT,
        "min_interval": _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0),
        "jitter": _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0),
    }
