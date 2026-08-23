# -*- coding: utf-8 -*-
"""账号页独立补密码 + TOTP 2FA 扩展。

该服务只由账号页按钮触发。它创建独立 Roxy 环境完成邮箱 OTP 登录，然后
复用 account_export 中已经验证的浏览器脚本执行密码重认证与 MFA
enroll/activate；不会进入或修改注册任务主流程。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from core import db

logger = logging.getLogger(__name__)

_WORKERS = 1
_QUEUE_LIMIT = 50
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="account-security")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"security-setup-{safe}.log"


def _append_log(email: str, message: str, *, clear: bool = False) -> None:
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if clear else "a"
    stamp = datetime.now().strftime("%H:%M:%S")
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(f"{stamp} [INFO] {str(message or '')[:1000]}\n")


def _proxy_label(value: str) -> str:
    """只记录代理 host/port，避免把 Roxy 代理凭据写进账号日志。"""
    try:
        parsed = urlparse(str(value or ""))
        if parsed.hostname and parsed.port:
            return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        if parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}"
    except Exception:
        pass
    return "direct" if not str(value or "").strip() else "configured"


def _stored_password(account: dict) -> str:
    try:
        extra = json.loads(str(account.get("extra_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        extra = {}
    if not isinstance(extra, dict):
        return ""
    return str(extra.get("registration_password") or extra.get("chatgpt_password") or "").strip()


def _run_security_setup(*, account_id: int, password_mode: str, trigger: str) -> dict:
    client = None
    opened = None
    driver = None
    session = None
    email = ""
    password_done = False
    totp_done = False
    confirmed_password = ""
    confirmed_secret = ""
    access_token = ""
    try:
        account = db.get_account(int(account_id))
        if not account:
            return {"ok": False, "status": "failed", "error": "账号不存在"}
        email = str(account.get("email") or "").strip()
        if not email:
            raise RuntimeError("账号邮箱为空")
        confirmed_password = _stored_password(account)
        confirmed_secret = str(account.get("totp_secret") or "").strip()
        access_token = str(account.get("access_token") or "").strip()
        password_done = bool(confirmed_password)
        totp_done = bool(confirmed_secret)
        if password_done and totp_done:
            result = {
                "ok": True, "status": "success", "stage": "already_complete",
                "message": "账号密码和 2FA 已完整保存，无需重复设置",
                "password_done": True, "totp_done": True, "checked_at": _now(),
            }
            db.update_account_security_setup(account_id, result)
            return result

        _append_log(email, f"[安全扩展] 开始 account_id={account_id} trigger={trigger} mode={password_mode}")
        from core.email_provider import wait_for_otp
        from core.roxybrowser_client import RoxyBrowserClient
        from core.roxy_registration import (
            _build_driver,
            _center_browser_window,
            _fetch_chatgpt_session,
        )
        from core.roxy_codex_oauth import _fill_email_and_otp
        from core.account_export import (
            _setup_password_with_driver,
            _setup_totp_with_driver,
            _validate_2fa_token,
            import_browser_cookies,
        )
        from core.registration_password import registration_password
        from core.session import BrowserSession
        from config import roxybrowser as roxy_cfg

        client = RoxyBrowserClient()
        session_info = None
        browser_attempts = max(1, min(3, int(getattr(roxy_cfg, "ROXY_CREATE_API_ATTEMPTS", 2) or 2) + 1))
        for browser_attempt in range(1, browser_attempts + 1):
            try:
                # 先做真实出口预检，再创建 Roxy 环境；这样浏览器不会直接吃到刚失效的粘性代理。
                opened = client.open_profile(headless=False, require_proxy_exit_ip=True)
                if not db.mark_account_security_setup_running(account_id, profile_id=opened.profile_id):
                    raise RuntimeError("账号已删除或安全设置状态已重置")
                driver = _build_driver(opened)
                _center_browser_window(driver)
                driver.set_page_load_timeout(int(roxy_cfg.ROXY_SELENIUM_TIMEOUT))
                try:
                    driver.set_script_timeout(max(120, int(roxy_cfg.ROXY_SELENIUM_TIMEOUT)))
                except Exception:
                    pass

                _append_log(
                    email,
                    f"[安全扩展] Roxy 尝试 {browser_attempt}/{browser_attempts}："
                    f"profile={opened.profile_id} proxy={_proxy_label(client.profile_proxy)}，开始邮箱 OTP 登录",
                )
                _fill_email_and_otp(driver, email, wait_for_otp, "https://chatgpt.com/auth/login")
                _append_log(email, "[安全扩展] 邮箱 OTP 登录步骤结束，开始读取 ChatGPT session")
                session_info = _fetch_chatgpt_session(
                    driver,
                    timeout=max(15, int(getattr(roxy_cfg, "ROXY_SESSION_WAIT_TIMEOUT", 25) or 25)),
                    auto_jump_wait=max(3, int(getattr(roxy_cfg, "ROXY_SESSION_AUTO_JUMP_WAIT", 8) or 8)),
                    refresh_attempts=1,
                )
                break
            except Exception as exc:
                _append_log(
                    email,
                    f"[安全扩展] Roxy 尝试 {browser_attempt}/{browser_attempts} 失败："
                    f"{type(exc).__name__}: {str(exc)[:240]}",
                )
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = None
                if client is not None and opened is not None:
                    try:
                        client.cleanup_profile(opened)
                    except Exception:
                        logger.exception("[安全扩展] Roxy 重试前清理失败 account_id=%s", account_id)
                    opened = None
                if browser_attempt >= browser_attempts:
                    raise
                # 丢弃旧粘性代理；下一轮 open_profile 会重新预检并重新抽取代理。
                client.profile_proxy = None
                client.profile_proxy_source = None
                time.sleep(1.0)
        if not isinstance(session_info, dict):
            raise RuntimeError("浏览器登录后没有返回 ChatGPT session")
        authenticated_email = str((session_info.get("user") or {}).get("email") or "").strip()
        if authenticated_email.casefold() != email.casefold():
            raise RuntimeError("浏览器登录账号与目标账号不一致，已停止安全设置")
        access_token = str(session_info.get("accessToken") or access_token).strip()
        if not access_token:
            raise RuntimeError("浏览器登录成功但没有取得 Access Token")
        _append_log(email, "[安全扩展] 浏览器登录账号已严格匹配")
        db.update_account_security_setup(
            account_id,
            {
                "ok": False,
                "status": "running",
                "stage": "browser_authenticated",
                "message": "浏览器账号已匹配，正在请求补设密码验证码",
                "profile_id": opened.profile_id,
                "password_done": password_done,
                "totp_done": totp_done,
            },
            access_token=access_token,
        )

        # 空字符串表示与直连 Roxy 一致；显式代理则固定复用同一端点。
        session = BrowserSession(proxy=str(client.profile_proxy or ""), detect_exit_geo=False)
        import_browser_cookies(session, driver, require_auth=True)

        if not password_done:
            desired_password = registration_password()
            password_result = _setup_password_with_driver(
                driver=driver,
                session=session,
                email=email,
                password=desired_password,
                totp_secret=confirmed_secret or None,
                password_mode=password_mode,
                console_compat=True,
            )
            if not bool(password_result.get("ok")):
                raise RuntimeError(str(password_result.get("message") or "补设密码未确认成功"))
            confirmed_password = desired_password
            password_done = True
            db.save_security_checkpoint(
                email,
                registration_password=confirmed_password,
                access_token=access_token,
            )
            db.update_account_security_setup(
                account_id,
                {
                    "ok": False, "status": "running", "stage": "password_done",
                    "message": "密码已补设，正在开启 2FA", "profile_id": opened.profile_id,
                    "password_done": True, "totp_done": totp_done,
                },
                registration_password=confirmed_password,
                access_token=access_token,
            )
            _append_log(email, f"[安全扩展] 密码已完成 mode={password_mode}")

        if not totp_done:
            confirmed_secret, refreshed_token, _expires = _setup_totp_with_driver(
                driver,
                email,
                authenticated_email=authenticated_email,
            )
            access_token = str(refreshed_token or access_token).strip()
            totp_done = True
            db.save_security_checkpoint(
                email,
                registration_password=confirmed_password,
                totp_secret=confirmed_secret,
                access_token=access_token,
            )
            db.update_account_security_setup(
                account_id,
                {
                    "ok": False, "status": "running", "stage": "totp_activated",
                    "message": "2FA 已激活，正在校验最新 Token", "profile_id": opened.profile_id,
                    "password_done": password_done, "totp_done": True,
                },
                registration_password=confirmed_password,
                totp_secret=confirmed_secret,
                access_token=access_token,
            )
            _append_log(email, "[安全扩展] TOTP enroll/activate 已完成")

        validation_note = ""
        try:
            _validate_2fa_token(session, access_token)
        except Exception as exc:
            # 激活是远端写操作终态；只读 Token 校验失败不回滚已保存 Secret。
            validation_note = f"；Token 只读校验未通过（{type(exc).__name__}）"
        result = {
            "ok": True,
            "status": "success",
            "stage": "complete",
            "message": f"密码和 2FA 已完成{validation_note}",
            "profile_id": opened.profile_id,
            "password_done": password_done,
            "totp_done": totp_done,
            "checked_at": _now(),
        }
        db.update_account_security_setup(
            account_id,
            result,
            registration_password=confirmed_password,
            totp_secret=confirmed_secret,
            access_token=access_token,
        )
        _append_log(email, "[安全扩展] 完成：密码和 2FA 凭据已保存")
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "partial" if password_done or totp_done else "failed",
            "stage": "failed",
            "message": "密码/2FA 设置未全部完成",
            "error": f"{type(exc).__name__}: {str(exc)[:400]}",
            "profile_id": getattr(opened, "profile_id", None),
            "password_done": password_done,
            "totp_done": totp_done,
            "checked_at": _now(),
        }
        try:
            db.update_account_security_setup(
                account_id,
                result,
                registration_password=confirmed_password or None,
                totp_secret=confirmed_secret or None,
                access_token=access_token or None,
            )
        except Exception:
            logger.exception("[安全扩展] 写入失败状态异常 account_id=%s", account_id)
        if email:
            _append_log(email, f"[安全扩展] 失败：{result['error']}")
        logger.exception("[安全扩展] 任务失败 account_id=%s", account_id)
        return result
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if client is not None and opened is not None:
            try:
                client.cleanup_profile(opened)
            except Exception:
                logger.exception("[安全扩展] 清理 Roxy 环境失败")
        _QUEUE_SLOTS.release()


def enqueue_account_security_setup(
    *,
    account_id: int,
    password_mode: str = "add",
    trigger: str = "manual",
) -> dict:
    account_id = int(account_id)
    normalized_mode = str(password_mode or "add").strip().lower()
    if normalized_mode not in {"add", "reset"}:
        return {"accepted": False, "busy": False, "error": "password_mode 仅支持 add/reset"}
    account = db.get_account(account_id)
    if not account:
        return {"accepted": False, "busy": False, "error": "账号不存在"}
    email = str(account.get("email") or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "账号邮箱为空"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "安全设置队列已满"}
    if not db.claim_account_security_setup(account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在执行补密码/2FA"}
    _append_log(email, f"[安全扩展] 已入队 account_id={account_id} mode={normalized_mode}", clear=True)
    try:
        _EXECUTOR.submit(
            _run_security_setup,
            account_id=account_id,
            password_mode=normalized_mode,
            trigger=str(trigger or "manual"),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False, "status": "failed", "stage": "queue",
            "message": "安全设置入队失败", "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "checked_at": _now(),
        }
        db.update_account_security_setup(account_id, result)
        return {"accepted": False, "busy": False, "error": result["error"]}
    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "password_mode": normalized_mode,
    }


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


__all__ = ["enqueue_account_security_setup", "log_path", "queue_settings"]
