# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from config import cloakbrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data
from core.browser_exit_geo import probe_playwright_context_exit_geo
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay
from core.twofa_proxy import build_twofa_session, resolve_twofa_proxy, twofa_failure_payload
from core.otp_utils import mask_otp, redact_otp_text

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _click_resend_email_otp, _complete_profile_page, _fetch_chatgpt_session, _check_manual_stop,
)

logger = logging.getLogger(__name__)


def run_cloak_registration(email: str, name: str, birthday: str, proxy: str = None, otp_code: str = None, batch_dir: Path | None = None) -> dict:
    """CloakBrowser 自动化注册入口。"""
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    registration_exit_geo: dict = {}
    try:
        driver, opened = build_cloak_driver(proxy=proxy)
        registration_exit_geo = probe_playwright_context_exit_geo(
            driver.context,
            label="Cloak注册",
        )
        logger.info("[Cloak注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        logger.info("[Cloak注册] 打开登录页：https://chatgpt.com/auth/login")
        driver.get("https://chatgpt.com/auth/login")
        human_delay("navigate")
        _maybe_accept(driver)
        _check_manual_stop()

        next_state = _submit_email_and_wait_next(driver, email, attempts=3)
        _check_manual_stop()

        openai_password = None if next_state == "otp" else _fill_password_page_if_present(driver, email, timeout=25)
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Cloak注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        redact_otp_text(str(exc)[:180]),
                    )
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Cloak注册][OTP] 收到验证码：%s", mask_otp(current_otp))
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            human_delay("otp_input")
            try:
                _click_continue(driver)
            except Exception as exc:
                logger.info(
                    "[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s",
                    redact_otp_text(str(exc)[:120]),
                )

            outcome = _wait_after_email_otp_submit(driver, timeout=10)
            if outcome == "accepted":
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            otp_after_ts = time.time()
            _click_resend_email_otp(driver, timeout=25)
            human_delay("api")
            current_otp = None

        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        logger.info("[Cloak注册] 已拿到 accessToken：%s", email)

        totp_secret = None
        twofa_result = None
        twofa_session = None
        twofa_status = "skipped"
        twofa_error = None
        twofa_validation = None
        twofa_proxy_continuity = False
        twofa_proxy_source = None
        if _twofa_cfg.ENABLE_2FA:
            twofa_status = "failed"
            logger.info("[Cloak注册][2FA] ENABLE_2FA=True，复用当前浏览器会话设置 2FA")
            try:
                from core.account_export import maybe_setup_2fa_result
                # Prefer the original input so a Cloak socks5h route is not
                # downgraded to local-DNS socks5 by the browser adapter.
                used_proxy = proxy or ((opened.raw or {}).get("proxy") if opened else None)
                used_proxy = resolve_twofa_proxy(used_proxy, source="CloakBrowser")
                twofa_session = build_twofa_session(used_proxy, source="CloakBrowser")
                twofa_proxy_continuity = True
                twofa_proxy_source = "registration_argument" if proxy else "cloak_profile"
                twofa_result = maybe_setup_2fa_result(twofa_session, email, driver=driver)
                twofa_error = getattr(twofa_session, "_twofa_last_error", None)
                if twofa_result:
                    totp_secret = twofa_result.secret
                    access_token = twofa_result.access_token
                    twofa_validation = getattr(twofa_result, "validation", None)
                    twofa_status = "success" if bool(getattr(twofa_result, "validation_ok", True)) else "partial_success"
                    logger.info("[Cloak注册][2FA] 已完成，Token 校验=%s", twofa_status == "success")
                else:
                    if not twofa_error:
                        twofa_error = {
                            "stage": "totp_setup",
                            "code": "totp_setup_failed",
                            "http_status": None,
                            "message": "2FA 未完成",
                        }
                    logger.warning("[Cloak注册][2FA] 未完成，账号仍保存")
            except Exception as exc:
                twofa_error = twofa_failure_payload(exc, default_stage="totp_proxy")
                logger.warning("[Cloak注册][2FA] 执行失败，账号仍保存：%s", type(exc).__name__)
            finally:
                if twofa_session is not None:
                    twofa_session.close()

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                from core.codex_oauth import run_codex_oauth
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=True，注册代理与 Codex 本地代理分离执行")
                _check_manual_stop()
                codex_result = run_codex_oauth(email, force=True)
            else:
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy") if opened else None) or proxy or None,
            batch_dir=batch_dir,
            registration_name=name,
            birth_date=birthday,
            registration_exit_ip=registration_exit_geo.get("ip"),
            registration_exit_country=registration_exit_geo.get("country"),
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": (twofa_result.expires if twofa_result and twofa_result.expires else session_info.get("expires")),
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "twofa": {
                    "status": twofa_status,
                    "validated": bool(twofa_result and getattr(twofa_result, "validation_ok", True)),
                    "validation_status": getattr(twofa_result, "validation_status", None) if twofa_result else None,
                    "validation": twofa_validation,
                    "activated_at": getattr(twofa_result, "activated_at", None) if twofa_result else None,
                    "proxy_continuity": twofa_proxy_continuity,
                    "proxy_source": twofa_proxy_source,
                    "error": twofa_error,
                },
                "codex": codex_result,
            },
        )
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {"success": bool(codex_ok), "email": email, "account_id": account_id, "access_token": access_token, "totp_secret": totp_secret, "codex": codex_result, "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}"}
    except Exception as exc:
        logger.error("[Cloak注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Cloak注册] 失败详情", exc_info=True)
        try:
            from core.email_provider import release_email
            release_email(email, status="failed" if create_acknowledged else "available", note=f"Cloak注册失败: {str(exc)[:180]}")
        except Exception:
            pass
        return {"success": False, "email": email, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        if driver and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
