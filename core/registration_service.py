# -*- coding: utf-8 -*-
"""
注册任务服务层：
    - 线程池并发执行 run_registration
    - 每个任务在 data/registration_jobs.json 里有一条记录
    - 每个任务的日志写到 data/logs/<job_uuid>.log，便于 Web UI 实时尾巴

使用：
    submit_registration(email_source="outlook", count=5)
    → 创建 5 个任务，丢入线程池，立即返回 [job_dict, ...]
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from core import codex_retry_service, db

logger = logging.getLogger(__name__)

# 全局线程池（WebUI 每次提交时可按最新 workers 重建）
_DEFAULT_MAX_WORKERS = 10
_MIN_MAX_WORKERS = 1
_executor: ThreadPoolExecutor | None = None
_executor_workers = _DEFAULT_MAX_WORKERS
_executor_generation = 0
_retired_executors: list[ThreadPoolExecutor] = []
_executor_lock = threading.RLock()

_STOP_EVENTS: dict[int, threading.Event] = {}
_ACTIVE_JOBS: set[int] = set()
_STOP_LOCK = threading.Lock()
_THREAD_CTX = threading.local()


class StopRequested(RuntimeError):
    """用户手动停止注册任务。"""


def _activate_job(job_id: int) -> None:
    _THREAD_CTX.job_id = int(job_id)
    job = db.get_job(int(job_id)) or {}
    _THREAD_CTX.gc_mode = bool(job.get("gc_mode"))
    with _STOP_LOCK:
        _STOP_EVENTS.setdefault(int(job_id), threading.Event())
        _ACTIVE_JOBS.add(int(job_id))


def _deactivate_job(job_id: int) -> None:
    with _STOP_LOCK:
        _STOP_EVENTS.pop(int(job_id), None)
        _ACTIVE_JOBS.discard(int(job_id))
    try:
        delattr(_THREAD_CTX, "job_id")
    except Exception:
        pass
    try:
        delattr(_THREAD_CTX, "gc_mode")
    except Exception:
        pass


def current_job_id() -> int | None:
    value = getattr(_THREAD_CTX, "job_id", None)
    return int(value) if value else None


def current_gc_mode() -> bool:
    return bool(getattr(_THREAD_CTX, "gc_mode", False))


def bind_roxy_profile(profile_id: str) -> None:
    """把当前注册线程创建的唯一 Roxy Profile 绑定到对应任务。"""
    job_id = current_job_id()
    profile_id = str(profile_id or "").strip()
    if job_id and profile_id:
        db.update_job(job_id, roxy_profile_id=profile_id, gc_window_state="open")
        job = db.get_job(job_id) or {}
        if job.get("status") in ("stopping", "stopped", "cancelled"):
            if not _cleanup_roxy_on_stop(job_id, profile_id):
                raise RuntimeError(f"Roxy 环境删除失败：{profile_id}")
            raise StopRequested(f"任务 #{job_id} 已停止，Roxy 环境已删除")


def is_stop_requested(job_id: int | None = None) -> bool:
    if job_id is None:
        job_id = getattr(_THREAD_CTX, "job_id", None)
    if not job_id:
        return False
    with _STOP_LOCK:
        ev = _STOP_EVENTS.get(int(job_id))
        if ev and ev.is_set():
            return True
    job = db.get_job(int(job_id))
    return bool(job and job.get("status") in ("stopping", "stopped", "cancelled"))


def check_stop_requested() -> None:
    job_id = getattr(_THREAD_CTX, "job_id", None)
    if is_stop_requested(job_id):
        raise StopRequested(f"任务 #{job_id} 已被用户手动停止")


def _cleanup_roxy_on_stop(job_id: int, profile_id: str | None) -> bool:
    """Synchronously close and delete a manually stopped task's Roxy environment."""
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        return False
    from core.roxybrowser_client import RoxyBrowserClient

    client = RoxyBrowserClient()
    closed = client.close_profile(profile_id)
    deleted = client.delete_profile(profile_id)
    db.update_job(
        job_id,
        gc_window_state="deleted" if deleted else ("closed" if closed else "cleanup_failed"),
    )
    if not deleted:
        logger.error("[Service] 停止任务 #%s 的 Roxy 环境删除失败：%s", job_id, profile_id)
        return False
    logger.warning("[Service] 停止任务 #%s 后已关闭并删除 Roxy 环境：%s", job_id, profile_id)
    return True


def _append_job_log(job_id: int, message: str) -> None:
    try:
        job = db.get_job(job_id)
        log_file = job.get("log_file") if job else None
        if not log_file:
            return
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H:%M:%S")
        with Path(log_file).open("a", encoding="utf-8") as f:
            f.write(f"{ts} [WARNING] [manual-stop] {message}\n")
    except Exception:
        pass


def _random_display_name() -> str:
    """生成符合 OpenAI 限制的英文字母显示名。"""
    from core.name_samples import random_display_name

    return random_display_name()


def _prepare_registration_args(email_source=None, email_override: str | None = None) -> tuple[str, str, str]:
    """复用 CLI 的默认规则，为旧 Web 任务入口补齐注册参数。"""
    # 用模块属性读，支持 WebUI 热加载
    from config import register as _r, email as _e
    from core.email_provider import acquire_email
    from core.profile_utils import generate_random_birthday

    email = str(email_override or getattr(_r, "REGISTER_EMAIL", "") or "").strip()
    name = str(getattr(_r, "REGISTER_NAME", "") or "").strip()
    # WebUI/配置里有时会把空值存成 "-"，这不是合法 OpenAI 显示名，按空处理并自动生成
    if name in {"-", "—", "无", "空", "none", "None", "null", "NULL"}:
        name = ""

    if not name:
        # 手动模式也自动生成显示名，减少配置负担
        name = _random_display_name()

    birthday = generate_random_birthday()

    # 邮箱领取会把池状态置为 used，因此放在所有其他准备逻辑之后。
    if not email:
        if _e.USE_EMAIL_SERVICE:
            email = acquire_email(email_source=email_source)
        else:
            raise RuntimeError(
                "手动模式未配置邮箱。请在 WebUI 配置页设置 REGISTER_EMAIL，"
                "或开启 USE_EMAIL_SERVICE 并从邮箱池领取。"
            )

    return email, name, birthday


def _resolve_registration_proxy(proxy_mode: str | None) -> str | None:
    """Resolve a task's proxy mode without allowing local tasks to call the API pool."""
    if str(proxy_mode or "").strip().lower() != "local":
        return None
    from config.proxy import pick_local_proxy

    proxy = str(pick_local_proxy() or "").strip()
    if not proxy:
        raise RuntimeError(
            "本地代理模式未检测到可用的系统代理或静态代理。请开启系统代理，或在 PROXY_POOL 填写本地代理地址。"
        )
    return proxy


def _release_unconsumed_job_email(
    email: str | None,
    reason: str,
    *,
    count_failure: bool = False,
) -> None:
    """可恢复失败：回收未生成账号的邮箱，并把它放到重试队列末尾。"""
    if not email:
        return
    try:
        from core.email_provider import release_email_if_unconsumed

        release_email_if_unconsumed(
            email,
            note=f"可重试，已移至队尾: {reason[:180]}",
            count_failure=count_failure,
        )
    except Exception:
        logger.exception("[Service] 回收未消耗邮箱失败: %s", email)


def _is_final_session_access_token_timeout(error: object) -> bool:
    """
    识别注册最后一步已经返回 /api/auth/session 200 但没有 accessToken 的失败。
    这种邮箱后续继续注册通常会卡在同一状态，按要求直接停用邮箱池条目。
    """
    text = str(error or "")
    if not text:
        return False
    return (
        "等待 /api/auth/session accessToken 超时" in text
        and "WARNING_BANNER" in text
        and "'_http_status': 200" in text
    )


def _should_disable_failed_registration_email(error: object) -> bool:
    """只识别明确不可再次注册的永久失败；其他失败一律允许排队重试。"""
    text = str(error or "")
    if not text:
        return False
    return (
        _is_final_session_access_token_timeout(text)
        or "账号已创建但 AT 恢复失败" in text
        or "邮箱提交后进入登录密码页" in text
        or "auth.openai.com/log-in/password" in text
        or "/log-in/password" in text
        or "account_deactivated" in text.lower()
        or "account_disabled" in text.lower()
        or "account_banned" in text.lower()
        or "账号已删除或停用" in text
        or "账号已被封禁" in text
    )


def _should_count_registration_email_failure(error: object) -> bool:
    """Count only mailbox/OTP failures; proxy or browser failures must not poison a mailbox."""
    text = str(error or "")
    lowered = text.lower()
    return any(marker in lowered for marker in (
        "genericapimailerror",
        "genericapitransporterror",
        "otp",
        "验证码",
        "取码接口",
    ))


def _disable_job_email(email: str | None, reason: str) -> bool:
    """把本次任务邮箱停用，避免后续再次领取。"""
    if not email:
        return False
    try:
        from core.email_provider import release_email

        source = release_email(email, status="disabled", note=f"自动停用: {reason[:180]}")
        logger.warning("[Service] 已自动停用邮箱: source=%s email=%s reason=%s", source, email, reason[:220])
        return True
    except Exception:
        logger.exception("[Service] 自动停用邮箱失败: %s", email)
        return False


def _normalize_workers(max_workers: int | None) -> int:
    if max_workers is None:
        return _DEFAULT_MAX_WORKERS
    try:
        value = int(max_workers)
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_WORKERS
    return max(_MIN_MAX_WORKERS, value)


def get_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    """返回注册线程池。

    旧逻辑只在首次创建线程池时使用 max_workers，后续 WebUI 改线程数再提交仍会复用
    上一次的池。这里改成：每次传入的 max_workers 和当前池不一致时，立即创建新池供
    新提交任务使用；旧池不接收新任务，但会继续把已经排队/运行的任务跑完。
    """
    global _executor, _executor_workers, _executor_generation
    requested_workers = _normalize_workers(max_workers) if max_workers is not None else _executor_workers
    with _executor_lock:
        if _executor is None or requested_workers != _executor_workers:
            old_executor = _executor
            if old_executor is not None:
                # 不取消旧池里已提交的任务，只是不再往旧池追加新任务。
                old_executor.shutdown(wait=False, cancel_futures=False)
                _retired_executors.append(old_executor)
                logger.info(
                    "[Service] 注册线程池 workers 从 %s 切换为 %s；旧池继续处理已排队任务",
                    _executor_workers,
                    requested_workers,
                )
            _executor_workers = requested_workers
            _executor_generation += 1
            _executor = ThreadPoolExecutor(
                max_workers=requested_workers,
                thread_name_prefix=f"reg-worker-{_executor_generation}",
            )
    return _executor


def get_executor_workers() -> int:
    """当前新提交注册任务会使用的线程数。"""
    with _executor_lock:
        return _executor_workers


def shutdown_executor(wait: bool = True) -> None:
    global _executor
    with _executor_lock:
        executors = []
        if _executor is not None:
            executors.append(_executor)
            _executor = None
        executors.extend(_retired_executors)
        _retired_executors.clear()
    for ex in executors:
        ex.shutdown(wait=wait, cancel_futures=False)


# ============================================================
# 单任务执行：日志重定向到任务专属文件
# ============================================================

class _JobLogContext:
    """让本线程的根 logger 多一个 FileHandler，结束后移除。"""

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.handler: logging.FileHandler | None = None

    def __enter__(self):
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self.handler = logging.FileHandler(self.log_path, encoding="utf-8")
        self.handler.setLevel(logging.INFO)
        self.handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        # 仅给本线程过滤 —— 用 thread name 做区分，避免污染其他任务的日志
        thread_name = threading.current_thread().name
        self.handler.addFilter(lambda r: r.threadName == thread_name)
        logging.getLogger().addHandler(self.handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handler is not None:
            self.handler.close()
            logging.getLogger().removeHandler(self.handler)


def _enqueue_checkout_kind_after_registration(
    result: dict[str, Any],
    *,
    job_id: int,
    log_logger: logging.Logger | None = None,
) -> dict[str, Any] | None:
    account_id = result.get("account_id")
    access_token = str(result.get("access_token") or "").strip()
    if account_id is None or not access_token:
        return None
    active_logger = log_logger or logger
    try:
        from core import checkout_kind_service

        queued = checkout_kind_service.enqueue(
            account_id=int(account_id),
            access_token=access_token,
            trigger="registration_success",
        )
        if queued.get("accepted"):
            active_logger.info("[Job %s] AT 已落库，已自动开始 Checkout 类型查询", job_id)
        else:
            active_logger.warning("[Job %s] Checkout 类型自动查询未入队：%s", job_id, queued.get("error"))
        return queued
    except Exception:
        active_logger.exception("[Job %s] 自动开始 Checkout 类型查询失败", job_id)
        return None


def _run_one_job(job_id: int, log_file: str) -> None:
    """单任务入口（线程池里跑这个）。"""
    log_logger = logging.getLogger(__name__)
    _activate_job(job_id)

    # 取消检查：用户可能在任务排队期间点了"取消排队"，把 status 改成了 cancelled。
    # 因为 Future 已经 submit 进线程池无法撤回，只能在真正执行前自检一下，跳过 cancelled 的。
    current = db.start_pending_job(job_id, started_at=datetime.now().isoformat(timespec="seconds"))
    if current is None:
        current = db.get_job(job_id)
        if not current:
            log_logger.info(f"[Job {job_id}] 任务记录已删除，跳过执行")
        elif current.get("status") == "cancelled":
            _release_unconsumed_job_email(current.get("email"), "排队任务已取消")
            log_logger.info(f"[Job {job_id}] 已被用户取消，跳过执行")
        else:
            log_logger.info(f"[Job {job_id}] 当前状态为 {current.get('status')}，跳过重复执行")
        _deactivate_job(job_id)
        return

    email: str | None = None
    try:
        with _JobLogContext(log_file):
            from main import run_registration
            log_logger.info(f"[Job {job_id}] 开始注册任务")
            email, name, birthday = _prepare_registration_args(
                email_source=current.get("email_source"),
                email_override=current.get("email"),
            )
            registration_proxy = _resolve_registration_proxy(current.get("proxy_mode"))
            db.update_job(job_id, email=email)
            check_stop_requested()
            result = run_registration(
                email=email,
                name=name,
                birthday=birthday,
                proxy=registration_proxy,
                proxy_mode=current.get("proxy_mode"),
            )
            if is_stop_requested(job_id):
                _release_unconsumed_job_email(email, "用户手动停止")
                db.update_job(
                    job_id,
                    status="stopped",
                    error="用户手动停止",
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                log_logger.warning(f"[Job {job_id}] 已按用户请求停止")
                return
            if isinstance(result, dict) and result.get("success"):
                gc_mode = current_gc_mode()
                account_id = result.get("account_id")
                if gc_mode and account_id is not None and result.get("access_token"):
                    try:
                        gc_group = db.ensure_account_group("GC")
                        db.add_accounts_to_group(str(gc_group["id"]), [int(account_id)])
                        log_logger.info("[Job %s][GC] AT 已就绪，账号已加入 GC 分组", job_id)
                    except Exception:
                        log_logger.exception("[Job %s][GC] 加入 GC 分组失败", job_id)
                db.update_job(
                    job_id,
                    status="gc_waiting" if gc_mode else "success",
                    email=result.get("email"),
                    account_id=account_id,
                    roxy_profile_id=result.get("roxy_profile_id"),
                    gc_window_state="open" if gc_mode else None,
                    gc_check_state="idle" if gc_mode else None,
                    gc_check_message="AT 已就绪，等待支付后开始查询 Plus" if gc_mode else None,
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                log_logger.info(f"[Job {job_id}] 成功: {result.get('email')}")
            else:
                # 注意：失败也可能伴随 account_id（如 Codex 失败但账号已注册成功）
                err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
                result_email = (result or {}).get("email") if isinstance(result, dict) else None
                db.update_job(
                    job_id,
                    status="failed",
                    email=result_email or email,
                    account_id=(result or {}).get("account_id") if isinstance(result, dict) else None,
                    error=str(err)[:500],
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                email_to_handle = str(result_email or email or "").strip()
                if _should_disable_failed_registration_email(err):
                    _disable_job_email(email_to_handle, str(err))
                else:
                    _release_unconsumed_job_email(
                        email_to_handle,
                        str(err),
                        count_failure=_should_count_registration_email_failure(err),
                    )
                log_logger.error(f"[Job {job_id}] 失败: {err}")
    except StopRequested as exc:
        _release_unconsumed_job_email(email, str(exc))
        log_logger.warning(f"[Job {job_id}] 已停止: {exc}")
        db.update_job(
            job_id,
            status="stopped",
            error="用户手动停止",
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
    except Exception as exc:
        err_text = f"{type(exc).__name__}: {exc}"
        if _should_disable_failed_registration_email(err_text):
            _disable_job_email(email, err_text)
        else:
            _release_unconsumed_job_email(
                email,
                err_text,
                count_failure=_should_count_registration_email_failure(err_text),
            )
        if is_stop_requested(job_id):
            log_logger.warning(f"[Job {job_id}] 停止中捕获异常，按停止处理: {type(exc).__name__}: {exc}")
            db.update_job(
                job_id,
                status="stopped",
                error="用户手动停止",
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            return
        log_logger.exception(f"[Job {job_id}] 异常")
        db.update_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
        _append_job_log(job_id, f"[Job {job_id}] 终止异常: {type(exc).__name__}: {str(exc)[:500]}")
    finally:
        _deactivate_job(job_id)


def _reconcile_finished_registration_future(job_id: int, future: Any) -> None:
    """Future 结束后保证 DB 也已进入终态，防止 worker 无声消失。"""
    job = db.get_job(int(job_id)) or {}
    if str(job.get("status") or "").lower() not in {"pending", "running", "stopping"}:
        return
    try:
        exc = future.exception()
    except BaseException as future_exc:
        exc = future_exc
    if exc is None:
        detail = "执行线程已结束，但任务未写入终态"
    else:
        detail = f"执行线程异常结束：{type(exc).__name__}: {exc}"
    profile_id = str(job.get("roxy_profile_id") or "").strip()
    if profile_id:
        _cleanup_roxy_on_stop(int(job_id), profile_id)
    _release_unconsumed_job_email(job.get("email"), detail)
    db.update_job(
        int(job_id),
        status="failed",
        error=detail[:500],
        completed_at=datetime.now().isoformat(timespec="seconds"),
    )
    _append_job_log(int(job_id), f"任务终态看门狗已收敛异常任务：{detail}")
    logger.error("[Service] 任务 #%s 的 Future 已结束但 DB 仍非终态，已标记 failed", job_id)


def _run_codex_retry_job(job_id: int, log_file: str, email: str, account_id: int) -> None:
    """把 Codex 补跑作为标准任务执行，并复用任务状态、日志和停止入口。"""
    _activate_job(job_id)
    current = db.get_job(job_id)
    if not current or current.get("status") == "cancelled":
        codex_retry_service.release(email)
        _deactivate_job(job_id)
        return

    db.update_job(job_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"))
    try:
        result = codex_retry_service.run_worker(
            email,
            clear_log=False,
            target_log_path=log_file,
        )
        now_iso = datetime.now().isoformat(timespec="seconds")
        if is_stop_requested(job_id) or result.get("status") == "stopped":
            db.update_job(job_id, status="stopped", email=email, account_id=account_id, error=str(result.get("message") or "用户手动停止")[:500], completed_at=now_iso)
        elif result.get("ok"):
            db.update_job(
                job_id,
                status="success",
                email=email,
                account_id=account_id,
                completed_at=now_iso,
            )
        else:
            db.update_job(
                job_id,
                status="failed",
                email=email,
                account_id=account_id,
                error=str(result.get("message") or "Codex 补跑失败")[:500],
                completed_at=now_iso,
            )
    except Exception as exc:
        db.update_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
        codex_retry_service.release(email)
        logger.exception("[Job %s] Codex 补跑异常", job_id)
    finally:
        _deactivate_job(job_id)


# ============================================================
# 公共接口
# ============================================================

def submit_registration(
    count: int = 1,
    email_source: str | None = None,
    workers: int | None = None,
    proxy_mode: str | None = None,
    email_items: list[dict] | None = None,
) -> list[dict]:
    """
    创建 N 个注册任务并提交到线程池。
    email_source 会记录到 DB，并由执行线程用于选择对应的邮箱池。

    Returns:
        N 个新创建的 job dict
    """
    if email_source is None:
        from config import email as _email_cfg
        email_source = _email_cfg.EMAIL_SOURCE
    from config import roxybrowser as _roxy_cfg
    gc_mode = bool(getattr(_roxy_cfg, "GC_REGISTRATION_MODE", False))

    # 创建/切换线程池和提交本批任务必须整体串行化：否则另一请求在本批提交中途
    # 切换 workers 并 shutdown 旧池，会导致后续 submit 报 cannot schedule new futures after shutdown。
    with _executor_lock:
        executor = get_executor(max_workers=workers)
        effective_workers = get_executor_workers()
        jobs = []
        selected_items = email_items or []
        if selected_items:
            seen: set[tuple[str, str]] = set()
            job_specs: list[tuple[str, str]] = []
            for item in selected_items:
                if not isinstance(item, dict):
                    continue
                selected_source = str(item.get("source") or "").strip().lower()
                selected_email = str(item.get("email") or "").strip()
                key = (selected_source, selected_email.lower())
                if not selected_email or key in seen:
                    continue
                seen.add(key)
                if db.claim_email(selected_email, selected_source) is None:
                    logger.warning(
                        "[Service] 选中邮箱不可用，未创建注册任务: source=%s email=%s",
                        selected_source,
                        selected_email,
                    )
                    continue
                job_specs.append((selected_source, selected_email))
        else:
            job_specs = [(str(email_source), "") for _ in range(count)]

        for selected_source, selected_email in job_specs:
            job = db.create_job(
                email_source=selected_source,
                email=selected_email or None,
                gc_mode=gc_mode,
                proxy_mode=proxy_mode,
            )
            try:
                future = executor.submit(_run_one_job, job["id"], job["log_file"])
                future.add_done_callback(
                    lambda completed, jid=int(job["id"]): _reconcile_finished_registration_future(jid, completed)
                )
            except Exception as exc:
                db.update_job(
                    int(job["id"]),
                    status="failed",
                    error=f"队列提交失败：{type(exc).__name__}: {exc}"[:500],
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                if selected_email:
                    _release_unconsumed_job_email(selected_email, "任务未能提交到执行队列")
                logger.exception("[Service] 注册任务 #%s 提交线程池失败", job["id"])
            jobs.append(db.get_job(int(job["id"])) or job)
    logger.info(
        f"[Service] 已提交 {len(jobs)} 个注册任务，源={email_source}，代理模式={proxy_mode or 'config'}，workers={effective_workers}"
    )
    return jobs


def _account_for_job(job: dict) -> dict | None:
    account_id = job.get("account_id")
    if account_id is not None:
        try:
            account = db.get_account(int(account_id))
            if account is not None:
                return account
        except (TypeError, ValueError):
            pass
    email = str(job.get("email") or "").strip()
    return db.get_account_by_email(email) if email else None


def get_retry_info(job: dict) -> dict:
    """返回给 API/UI 的重试能力描述，不依赖前端猜测错误阶段。"""
    status = str(job.get("status") or "")
    info = {
        "retryable": False,
        "retry_action": None,
        "retry_label": None,
        "retry_reason": None,
        "display_status": status,
    }
    if status not in ("failed", "stopped", "cancelled"):
        return info

    successful_retry = db.get_successful_retry_for_job(int(job.get("id") or 0))
    if successful_retry is not None:
        info["retry_reason"] = f"后续重试任务 #{successful_retry.get('id')} 已成功"
        info["successful_retry_job_id"] = successful_retry.get("id")
        return info

    account = _account_for_job(job)
    if account and job.get("account_id") is not None and status in ("failed", "stopped"):
        info["display_status"] = "success" if (account.get("codex_status") or "") == "success" else "partial_success"

    if account:
        codex_status = str(account.get("codex_status") or "")
        if codex_status == "deactivated":
            info["retry_reason"] = "账号已废号，不能补跑 Codex"
            return info
        if codex_status == "success":
            info["retry_reason"] = "账号和 Codex 授权均已完成"
            return info
        info.update({
            "retryable": True,
            "retry_action": "codex",
            "retry_label": "补跑 Codex",
        })
        return info

    info.update({
        "retryable": True,
        "retry_action": "registration",
        "retry_label": "重试",
    })
    return info


def retry_job(job_id: int, workers: int | None = None) -> dict:
    """智能重试终态任务：未生成账号则重新注册，已有账号则仅补跑 Codex。"""
    source = db.get_job(job_id)
    if source is None:
        return {"ok": False, "error": "任务不存在", "status": 404}

    retry_info = get_retry_info(source)
    if not retry_info["retryable"]:
        reason = retry_info.get("retry_reason") or f"当前状态不支持重试：{source.get('status')}"
        return {"ok": False, "error": reason, "status": 409}

    action = str(retry_info["retry_action"])
    account = _account_for_job(source)
    email = str((account or {}).get("email") or source.get("email") or "").strip()
    account_id = int(account["id"]) if account and account.get("id") is not None else None
    reserved_codex = False
    if action == "codex":
        if not email or account_id is None:
            return {"ok": False, "error": "已注册账号信息不完整，无法补跑 Codex", "status": 409}
        if not codex_retry_service.reserve(email):
            return {"ok": False, "error": "该账号正在补跑 Codex，请稍候", "status": 409}
        reserved_codex = True

    try:
        job, created = db.create_retry_job(
            int(job_id),
            job_type="codex_retry" if action == "codex" else "registration",
            email_source=str(source.get("email_source") or "outlook"),
            proxy_mode=str(source.get("proxy_mode") or "") or None,
            email=email if action == "codex" else None,
            account_id=account_id if action == "codex" else None,
            gc_mode=bool(source.get("gc_mode")) if action == "registration" else False,
        )
    except LookupError as exc:
        if reserved_codex:
            codex_retry_service.release(email)
        return {"ok": False, "error": str(exc), "status": 404}
    except ValueError as exc:
        if reserved_codex:
            codex_retry_service.release(email)
        return {"ok": False, "error": str(exc), "status": 409}

    if not created:
        if reserved_codex:
            codex_retry_service.release(email)
        return {
            "ok": True,
            "created": False,
            "reused": True,
            "message": f"已有重试任务 #{job['id']} 在排队或运行中",
            "source_job_id": int(job_id),
            "retry_action": action,
            "job": job,
        }

    try:
        if action == "codex":
            db.update_account_codex_status(email, "retrying", None)
        with _executor_lock:
            executor = get_executor(max_workers=workers)
            if action == "codex":
                executor.submit(_run_codex_retry_job, job["id"], job["log_file"], email, int(account_id))
            else:
                executor.submit(_run_one_job, job["id"], job["log_file"])
    except Exception as exc:
        if reserved_codex:
            codex_retry_service.release(email)
            db.update_account_codex_status(email, "failed", f"队列提交失败：{type(exc).__name__}: {exc}"[:500])
        db.update_job(
            int(job["id"]),
            status="failed",
            error=f"队列提交失败：{type(exc).__name__}: {exc}"[:500],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
        logger.exception("[Service] 重试任务 #%s 提交线程池失败", job["id"])
        return {"ok": False, "error": "重试任务创建成功，但提交执行失败", "status": 500, "job": db.get_job(int(job["id"]))}

    return {
        "ok": True,
        "created": True,
        "reused": False,
        "message": f"已创建重试任务 #{job['id']}（{'Codex 补跑' if action == 'codex' else '完整注册'}）",
        "source_job_id": int(job_id),
        "retry_action": action,
        "job": job,
    }


def cancel_pending_jobs() -> int:
    """
    把所有 status=pending 的任务批量改成 cancelled，避免它们被执行。
    已经在 running 的任务不动（线程池中无法中途打断）。
    返回成功取消的数量。

    实际"不执行"的保证在 _run_one_job 开头——它真要跑起来时会先看 status 决定是否跳过。
    """
    jobs = db.list_jobs(limit=1000)
    cancelled = 0
    now_iso = datetime.now().isoformat(timespec="seconds")
    for job in jobs:
        if job.get("status") == "pending":
            cancelled_job = db.cancel_pending_job(
                int(job["id"]),
                completed_at=now_iso,
                error="用户手动取消",
            )
            if cancelled_job is not None:
                _release_unconsumed_job_email(cancelled_job.get("email"), "取消所有排队任务")
                cancelled += 1
    logger.info(f"[Service] 已取消 {cancelled} 个排队任务")
    return cancelled


def request_stop_job(job_id: int) -> dict:
    """手动停止单个注册任务。运行中立即落库、设置停止事件并关闭浏览器。"""
    job = db.get_job(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在", "status": 404}
    status = job.get("status")
    now_iso = datetime.now().isoformat(timespec="seconds")
    if status == "pending":
        cancelled_job = db.cancel_pending_job(
            job_id,
            completed_at=now_iso,
            error="用户手动停止/取消排队",
        )
        if cancelled_job is not None:
            _release_unconsumed_job_email(cancelled_job.get("email"), "用户手动停止/取消排队")
            _append_job_log(job_id, "用户手动停止：任务尚未运行，已取消排队。")
            return {"ok": True, "message": "排队任务已取消", "job_id": job_id, "state": "cancelled"}
        job = db.get_job(job_id) or job
        status = job.get("status")
    if status in ("success", "failed", "cancelled", "stopped"):
        return {
            "ok": True,
            "message": f"任务已结束：{status}",
            "job_id": job_id,
            "state": status,
        }
    if status in ("running", "stopping"):
        with _STOP_LOCK:
            active = int(job_id) in _ACTIVE_JOBS
            ev = _STOP_EVENTS.get(int(job_id)) if active else None
            if ev is not None:
                ev.set()
        if not active or ev is None:
            # Web 服务重启、线程异常退出、历史残留 stopping，或之前手动停止时只创建了 stop event
            # 但没有真实线程实例：直接落为 stopped，避免永远卡在“停止中”。
            with _STOP_LOCK:
                _STOP_EVENTS.pop(int(job_id), None)
                _ACTIVE_JOBS.discard(int(job_id))
            db.update_job(
                job_id,
                status="stopped",
                completed_at=now_iso,
                error="用户手动停止（任务实例不存在）",
            )
            _release_unconsumed_job_email(
                str(job.get("email") or "").strip() or None,
                "任务实例不存在，确认未继续执行",
            )
            _append_job_log(job_id, "用户手动停止：未找到运行中的任务实例，已直接标记为已停止。")
            cleanup_completed = _cleanup_roxy_on_stop(job_id, job.get("roxy_profile_id"))
            logger.warning("[Service] 用户停止任务 #%s：任务实例不存在，已直接标记 stopped", job_id)
            if job.get("roxy_profile_id") and not cleanup_completed:
                return {
                    "ok": False,
                    "error": "任务已停止，但 Roxy 环境删除失败",
                    "status": 500,
                    "job_id": job_id,
                    "state": "stopped",
                }
            return {
                "ok": True,
                "message": "任务实例不存在，已停止并清理浏览器环境",
                "job_id": job_id,
                "state": "stopped",
                "browser_cleanup": cleanup_completed,
            }
        # UI 和任务记录立即进入终态；worker 后续即使从网络/Selenium 调用返回，
        # stop event 与 stopped 状态也会阻止其把任务覆盖成 success/failed。
        db.update_job(
            job_id,
            status="stopped",
            completed_at=now_iso,
            error="用户手动停止",
        )
        cleanup_completed = _cleanup_roxy_on_stop(job_id, job.get("roxy_profile_id"))
        _append_job_log(
            job_id,
            f"用户手动停止：任务已立即标记停止；浏览器已删除={cleanup_completed}。",
        )
        logger.warning(
            "[Service] 用户立即停止任务 #%s：stop_event=true cleanup=%s",
            job_id,
            cleanup_completed,
        )
        if job.get("roxy_profile_id") and not cleanup_completed:
            return {
                "ok": False,
                "error": "任务已停止，但 Roxy 环境删除失败",
                "status": 500,
                "job_id": job_id,
                "state": "stopped",
            }
        return {
            "ok": True,
            "message": "任务已立即停止",
            "job_id": job_id,
            "state": "stopped",
            "browser_cleanup": cleanup_completed,
        }
    return {"ok": False, "error": f"当前状态不支持停止：{status}", "status": 409}


def read_job_log(job_id: int, max_bytes: int = 50_000) -> str:
    """读取任务日志文件最后 max_bytes 字节，给 Web UI 显示。"""
    job = db.get_job(job_id)
    if not job or not job.get("log_file"):
        return ""
    p = Path(job["log_file"])
    if not p.exists():
        return ""
    size = p.stat().st_size
    with p.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    return data.decode("utf-8", errors="replace")
