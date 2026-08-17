# -*- coding: utf-8 -*-
"""GC 注册任务的 Plus 轮询和 Roxy 独立窗口生命周期管理。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from core import db
from core.chatgpt_plan import check_account_plan
from core.mail_status_detector import detect_mailbox_status
from core.roxybrowser_client import RoxyBrowserClient

logger = logging.getLogger(__name__)

_POLL_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="gc-plus")
_POLL_EVENTS: dict[int, threading.Event] = {}
_POLL_LOCK = threading.RLock()
_POLL_INTERVAL_SECONDS = 20


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gc_job(job_id: int) -> tuple[dict | None, dict | None]:
    job = db.get_job(int(job_id))
    if not job:
        return None, {"ok": False, "status": 404, "error": "任务不存在"}
    if not bool(job.get("gc_mode")):
        return None, {"ok": False, "status": 409, "error": "该任务不是 GC 注册任务"}
    return job, None


def _account_for_job(job: dict) -> dict | None:
    account_id = job.get("account_id")
    if account_id is not None:
        account = db.get_account(int(account_id))
        if account:
            return account
    email = str(job.get("email") or "").strip()
    return db.get_account_by_email(email) if email else None


def access_token_for_job(job_id: int) -> dict:
    job, error = _gc_job(job_id)
    if error:
        return error
    account = _account_for_job(job)
    token = str((account or {}).get("access_token") or "").strip()
    if not token:
        return {"ok": False, "status": 409, "error": "该任务尚未提取到 AT"}
    return {
        "ok": True,
        "job_id": int(job_id),
        "email": str((account or {}).get("email") or job.get("email") or ""),
        "access_token": token,
    }


def _mailbox_plus_fallback(account: dict) -> dict:
    """用明确的购买成功邮件作 Plus 正向补充证据。"""
    email = str(account.get("email") or "").strip()
    if not email:
        return {"status": "unavailable", "error": "账号邮箱为空"}
    entry = db.get_mail_status_entry(email, include_secret=True)
    if not entry or not str(entry.get("code_url") or "").strip():
        db.add_mail_status_emails([email])
        entry = db.get_mail_status_entry(email, include_secret=True)
    if not entry or not str(entry.get("code_url") or "").strip():
        return {"status": "unavailable", "error": "邮箱没有可用的读取链接"}

    db.mark_mail_status_checking(email)
    try:
        result = detect_mailbox_status(str(entry.get("code_url") or ""), limit=50)
    except Exception as exc:
        result = {
            "status": "error",
            "label": "检测失败",
            "evidence": "",
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
            "message_count": 0,
            "subject": "",
            "mail_date": "",
            "mail_id": "",
            "mail_source": "",
        }
    db.update_mail_status_result(email, result)
    if result.get("status") in {"plus", "nonplus", "banned"}:
        db.sync_account_mail_status(email, result)
    refreshed = db.get_account(int(account.get("id") or 0)) if account.get("id") else db.get_account_by_email(email)
    result["plan_promoted"] = bool((refreshed or {}).get("mail_plus_promoted"))
    return result


def _profile_conflict(job: dict) -> dict | None:
    profile_id = str(job.get("roxy_profile_id") or "").strip()
    if not profile_id:
        return None
    for other in db.list_jobs(limit=1_000_000):
        if int(other.get("id") or 0) == int(job.get("id") or 0):
            continue
        if str(other.get("roxy_profile_id") or "").strip() != profile_id:
            continue
        if other.get("gc_window_state") == "open":
            return other
    return None


def close_job_window(job_id: int, *, reason: str = "manual") -> dict:
    """只关闭并删除绑定到指定任务的 Roxy Profile。"""
    job, error = _gc_job(job_id)
    if error:
        return error
    profile_id = str(job.get("roxy_profile_id") or "").strip()
    if not profile_id:
        return {"ok": False, "status": 409, "error": "该任务没有绑定 Roxy 窗口 ID"}
    if job.get("gc_window_state") == "deleted":
        return {"ok": True, "job_id": int(job_id), "profile_id": profile_id, "already_deleted": True}

    conflict = _profile_conflict(job)
    if conflict:
        return {
            "ok": False,
            "status": 409,
            "error": f"安全校验失败：Roxy #{profile_id} 还绑定到任务 #{conflict.get('id')}，已拒绝关闭",
        }

    stop_plan_poll(job_id, update_job=False)
    client = RoxyBrowserClient()
    close_ok = client.close_profile(profile_id)
    delete_ok = client.delete_profile(profile_id)
    if not delete_ok:
        db.update_job(
            int(job_id),
            # 保持 open，确保前端继续展示“关闭并删除窗口”按钮，允许用户重试。
            gc_window_state="open",
            gc_check_state="error",
            gc_check_message=f"Roxy #{profile_id} 删除失败，请重试关闭",
            gc_checked_at=_now(),
        )
        return {
            "ok": False,
            "status": 502,
            "error": f"Roxy #{profile_id} 删除失败；关闭结果={'成功' if close_ok else '失败'}",
        }

    final_status = "success" if reason == "plus" else "gc_closed"
    message = "Plus 已到账，窗口已自动关闭并删除" if reason == "plus" else "已手动关闭并删除该任务窗口"
    db.update_job(
        int(job_id),
        status=final_status,
        gc_window_state="deleted",
        gc_check_state="plus" if reason == "plus" else "stopped",
        gc_check_message=message,
        gc_checked_at=_now(),
        completed_at=_now(),
    )
    logger.info("[GC] 任务 #%s 精确关闭并删除 Roxy #%s，reason=%s", job_id, profile_id, reason)
    return {"ok": True, "job_id": int(job_id), "profile_id": profile_id, "closed": close_ok, "deleted": True}


def close_plus_window_for_account(account_id: int) -> dict:
    """只关闭绑定到该账号的一个活动 GC 窗口。"""
    matches = [
        job for job in db.list_jobs(limit=1_000_000)
        if bool(job.get("gc_mode"))
        and int(job.get("account_id") or 0) == int(account_id)
        and job.get("gc_window_state") == "open"
    ]
    if not matches:
        return {"ok": True, "account_id": int(account_id), "closed": False, "reason": "no_open_gc_window"}
    # 新任务在前；同一账号正常情况下只有一个活动窗口。
    target = max(matches, key=lambda item: int(item.get("id") or 0))
    result = close_job_window(int(target["id"]), reason="plus")
    return {**result, "account_id": int(account_id)}


def _poll(job_id: int, stop_event: threading.Event) -> None:
    try:
        while not stop_event.is_set():
            job, error = _gc_job(job_id)
            if error or job.get("gc_window_state") != "open":
                return
            account = _account_for_job(job)
            token = str((account or {}).get("access_token") or "").strip()
            if not account or not token:
                db.update_job(
                    job_id,
                    status="gc_waiting",
                    gc_check_state="error",
                    gc_check_message="缺少账号或 AT，无法查询 Plus",
                    gc_checked_at=_now(),
                )
                return

            db.update_job(
                job_id,
                status="gc_checking",
                gc_check_state="checking",
                gc_check_message="正在通过 AT 查询 Plus",
                gc_checked_at=_now(),
            )
            result = check_account_plan(
                token,
                max_attempts=0,
                continue_check=lambda: not stop_event.is_set(),
            )
            db.update_account_plan_check(acc_id=int(account["id"]), result=result)
            # 查询请求可能耗时；用户在请求期间点了停止时，返回结果只能落库，不能再触发自动关窗。
            if stop_event.is_set():
                return
            mailbox_result = None
            at_auth_expired = (
                not result.get("ok")
                and int(result.get("http_status") or 0) == 401
                and not result.get("account_unusable_code")
            )
            if result.get("ok"):
                db.update_job(
                    job_id,
                    gc_check_state="checking",
                    gc_check_message="AT 已查询 Free/试用资格，正在通过邮箱确认 Plus",
                    gc_checked_at=result.get("checked_at") or _now(),
                )
                mailbox_result = _mailbox_plus_fallback(account)
                if stop_event.is_set():
                    return
                if result.get("has_active_plus_subscription") or mailbox_result.get("plan_promoted"):
                    db.update_job(
                        job_id,
                        gc_check_state="plus",
                        gc_check_message="邮箱确认 Plus 已到账，正在关闭并删除对应窗口",
                        gc_checked_at=_now(),
                    )
                    close_job_window(job_id, reason="plus")
                    return
            elif at_auth_expired:
                # 购买 Plus 不会必然使 AT 失效，但 AT 可能独立到期或会话被撤销。
                # 这时邮箱只可提供“已到账”的正向证据；查不到邮件时继续保留窗口。
                db.update_job(
                    job_id,
                    gc_check_state="checking",
                    gc_check_message="AT 已过期/失效，正在查询邮箱中的 Plus 到账凭证",
                    gc_checked_at=result.get("checked_at") or _now(),
                )
                mailbox_result = _mailbox_plus_fallback(account)
                if stop_event.is_set():
                    return
                if mailbox_result.get("plan_promoted"):
                    db.update_job(
                        job_id,
                        gc_check_state="plus",
                        gc_check_message="AT 已失效，但邮箱确认 Plus 已到账，正在关闭并删除对应窗口",
                        gc_checked_at=_now(),
                    )
                    close_job_window(job_id, reason="plus")
                    return
            elif not result.get("account_unusable_code"):
                # Plus is an independent mailbox check.  An AT/network failure
                # only makes Free/trial capability unknown; it must not block a
                # positive purchase receipt from closing the paid account window.
                db.update_job(
                    job_id,
                    gc_check_state="checking",
                    gc_check_message="AT 暂时无法查询 Free/试用资格，正在通过邮箱确认 Plus",
                    gc_checked_at=result.get("checked_at") or _now(),
                )
                mailbox_result = _mailbox_plus_fallback(account)
                if stop_event.is_set():
                    return
                if mailbox_result.get("plan_promoted"):
                    db.update_job(
                        job_id,
                        gc_check_state="plus",
                        gc_check_message="邮箱确认 Plus 已到账，正在关闭并删除对应窗口",
                        gc_checked_at=_now(),
                    )
                    close_job_window(job_id, reason="plus")
                    return

            if result.get("ok"):
                plan = str(result.get("current_plan_type") or "free")
                if mailbox_result and mailbox_result.get("status") == "nonplus":
                    message = f"AT 仅返回试用资格；邮箱未发现 Plus 到账，{_POLL_INTERVAL_SECONDS} 秒后继续查询"
                elif mailbox_result and mailbox_result.get("status") == "unavailable":
                    message = f"AT 仅返回试用资格；{mailbox_result.get('error')}，{_POLL_INTERVAL_SECONDS} 秒后继续查询"
                elif mailbox_result and mailbox_result.get("status") == "error":
                    message = f"邮箱查询失败：{str(mailbox_result.get('error') or '未知错误')[:120]}，稍后重试"
                else:
                    message = f"AT 明确套餐：{plan}，{_POLL_INTERVAL_SECONDS} 秒后继续查询"
                state = "checking"
            else:
                if at_auth_expired and mailbox_result:
                    if mailbox_result.get("status") == "unavailable":
                        detail = str(mailbox_result.get("error") or "邮箱不可查询")[:120]
                    elif mailbox_result.get("status") == "error":
                        detail = f"邮箱查询失败：{str(mailbox_result.get('error') or '未知错误')[:100]}"
                    else:
                        detail = "邮箱未发现 Plus 到账凭证"
                    message = f"AT 已过期/失效；{detail}，窗口保持开启并稍后重试"
                else:
                    # 封禁/停用与普通网络错误不能靠邮箱推断当前套餐状态。
                    message = f"AT 查询失败（未使用邮箱兜底）：{str(result.get('error') or '未知错误')[:140]}，稍后重试"
                state = "error"
            db.update_job(
                job_id,
                status="gc_checking",
                gc_check_state=state,
                gc_check_message=message,
                gc_checked_at=result.get("checked_at") or _now(),
            )
            stop_event.wait(_POLL_INTERVAL_SECONDS)
    except Exception as exc:
        logger.exception("[GC] 任务 #%s Plus 轮询异常", job_id)
        db.update_job(
            job_id,
            status="gc_waiting",
            gc_check_state="error",
            gc_check_message=f"查询异常：{type(exc).__name__}: {str(exc)[:160]}",
            gc_checked_at=_now(),
        )
    finally:
        with _POLL_LOCK:
            if _POLL_EVENTS.get(int(job_id)) is stop_event:
                _POLL_EVENTS.pop(int(job_id), None)
        latest = db.get_job(int(job_id)) or {}
        if stop_event.is_set() and latest.get("gc_window_state") == "open":
            db.update_job(
                int(job_id),
                status="gc_waiting",
                gc_check_state="stopped",
                gc_check_message="Plus 查询已停止，窗口仍保留",
                gc_checked_at=_now(),
            )


def start_plan_poll(job_id: int) -> dict:
    job, error = _gc_job(job_id)
    if error:
        return error
    if job.get("gc_window_state") != "open":
        return {"ok": False, "status": 409, "error": "该任务窗口未打开或已删除"}
    token_result = access_token_for_job(job_id)
    if not token_result.get("ok"):
        return token_result
    with _POLL_LOCK:
        existing = _POLL_EVENTS.get(int(job_id))
        if existing and not existing.is_set():
            return {"ok": True, "job_id": int(job_id), "already_running": True}
        stop_event = threading.Event()
        _POLL_EVENTS[int(job_id)] = stop_event
        _POLL_EXECUTOR.submit(_poll, int(job_id), stop_event)
    return {"ok": True, "job_id": int(job_id), "started": True, "interval_seconds": _POLL_INTERVAL_SECONDS}


def start_all_plan_polls() -> dict:
    """一键启动所有已拿到 AT、窗口仍打开的 GC 任务。"""
    started: list[int] = []
    reused: list[int] = []
    skipped: list[dict] = []
    for job in db.list_jobs(limit=1_000_000):
        if not bool(job.get("gc_mode")) or job.get("gc_window_state") != "open":
            continue
        job_id = int(job.get("id") or 0)
        if not job_id:
            continue
        result = start_plan_poll(job_id)
        if result.get("started"):
            started.append(job_id)
        elif result.get("already_running"):
            reused.append(job_id)
        else:
            skipped.append({"job_id": job_id, "error": result.get("error") or "无法开始查询"})
    return {
        "ok": True,
        "started": started,
        "started_count": len(started),
        "already_running": reused,
        "already_running_count": len(reused),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "interval_seconds": _POLL_INTERVAL_SECONDS,
    }


def stop_plan_poll(job_id: int, *, update_job: bool = True) -> dict:
    job, error = _gc_job(job_id)
    if error:
        return error
    with _POLL_LOCK:
        event = _POLL_EVENTS.get(int(job_id))
        if event:
            event.set()
    if update_job and job.get("gc_window_state") == "open":
        db.update_job(
            int(job_id),
            status="gc_waiting",
            gc_check_state="stopped",
            gc_check_message="Plus 查询已停止，窗口仍保留",
            gc_checked_at=_now(),
        )
    return {"ok": True, "job_id": int(job_id), "stopped": bool(event)}


def is_polling(job_id: int) -> bool:
    with _POLL_LOCK:
        event = _POLL_EVENTS.get(int(job_id))
        return bool(event and not event.is_set())
