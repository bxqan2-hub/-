"""Entry point for the vendored aBaiFreeGPT protocol registration flow."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from typing import Callable

from config import email as email_cfg
from core.account_export import save_account_data
from core.email_provider import resolve_email_source, wait_for_otp
from core.flow_trigger import trigger_flow
from core.registration_password import (
    persist_confirmed_registration_password,
    registration_password,
)

logger = logging.getLogger(__name__)
_PROFILE_POOL = None
_PROFILE_LOCK = threading.Lock()


def _next_profile():
    global _PROFILE_POOL
    from core.abais_protocol.environment_profile import FingerprintPool

    with _PROFILE_LOCK:
        if _PROFILE_POOL is None:
            _PROFILE_POOL = FingerprintPool.from_us_en_desktop()
        return next(_PROFILE_POOL)


def _pick_protocol_proxy(proxy: str | None) -> str:
    selected = str(proxy or "").strip()
    if selected:
        return selected
    from config.proxy import pick_proxy

    return str(pick_proxy(strict=True) or "").strip()


def _proxy_rotator(current: str) -> Callable[[], str | None]:
    def rotate() -> str | None:
        from config.proxy import pick_proxy

        try:
            candidate = str(pick_proxy(strict=True, excluded={current}) or "").strip()
        except Exception as exc:
            logger.warning("[协议注册] Cloudflare 后代理轮换失败: %s", exc)
            return None
        return candidate or None

    return rotate


def _otp_callback(email: str, initial_code: str | None) -> Callable[[], str]:
    state = {"initial": str(initial_code or "").strip(), "after_ts": time.time(), "attempt": 0}

    def callback() -> str:
        state["attempt"] += 1
        if state["initial"]:
            code = state["initial"]
            state["initial"] = ""
            return code
        if bool(getattr(email_cfg, "USE_EMAIL_SERVICE", False)):
            logger.info("[aBaiFreeGPT协议] 等待邮箱验证码：%s（第 %s 次）", email, state["attempt"])
            code = wait_for_otp(email, after_ts=state["after_ts"])
            state["after_ts"] = time.time()
            return str(code or "").strip()
        return input(f"[aBaiFreeGPT协议] 请输入 {email} 的 6 位验证码：").strip()

    return callback


def run_abai_protocol_registration(
    *,
    email: str,
    name: str,
    birthday: str | None,
    proxy: str | None = None,
    otp_code: str | None = None,
    batch_dir=None,
) -> dict:
    """Run the copied aBaiFreeGPT flow and persist its result locally."""
    from core.abais_protocol.protocol_register import ChatGPTProtocolRegister

    selected_proxy = _pick_protocol_proxy(proxy)
    password = registration_password()
    profile = _next_profile()
    logger.info(
        "[aBaiFreeGPT协议] 开始：%s，profile=%s，impersonate=%s",
        email,
        profile.name,
        profile.impersonate,
    )
    worker = ChatGPTProtocolRegister(
        proxy=selected_proxy,
        otp_callback=_otp_callback(email, otp_code),
        profile=profile,
        proxy_rotate_callback=_proxy_rotator(selected_proxy),
        log_fn=lambda message: logger.info("[aBaiFreeGPT协议] %s", message),
    )
    result = worker.run(email=email, password=password)

    access_token = str(result.get("access_token") or "").strip()
    totp = result.get("totp_2fa") if isinstance(result.get("totp_2fa"), dict) else {}
    totp_secret = str(totp.get("secret") or "").strip() or None
    if not access_token:
        raise RuntimeError("aBaiFreeGPT 协议注册未返回 access_token")
    if not totp_secret or not bool(totp.get("bound")):
        raise RuntimeError("aBaiFreeGPT 协议注册未确认 TOTP 激活")

    checkpoint_persisted = persist_confirmed_registration_password(email, password)
    refresh_token = str(result.get("refresh_token") or "").strip()
    codex_result = {
        "status": "success" if refresh_token else "skipped",
        "ok": bool(refresh_token),
        "message": "aBaiFreeGPT 同一注册事务已获取 refresh token" if refresh_token else "正常无 RT 状态",
        "refresh_token": refresh_token or None,
    }
    extra = {
        "account": result.get("profile") if isinstance(result.get("profile"), dict) else {},
        "workspace_id": result.get("workspace_id"),
        "session_token": result.get("session_token"),
        "refresh_token": refresh_token or None,
        "id_token": result.get("id_token"),
        "client_id": result.get("client_id"),
        "cookies": result.get("cookies") if isinstance(result.get("cookies"), dict) else {},
        "browser_profile": asdict(profile),
        "registration_password": password,
        "password_registered": bool(result.get("password_registered")),
        "password_setup": {
            "ok": True,
            "status": "success",
            "code": "aBaiFreeGPT_password_registered",
            "checkpoint_persisted": checkpoint_persisted,
        },
        "totp_2fa": {"status": "success", "requested": True, "bound": True, "activated": True, "secret": totp_secret},
        "codex": codex_result,
        "protocol_source": {
            "repository": "https://github.com/asz798838958/aBaiFreeGPT",
            "commit": "98e0ad6717566dcaec2a2d7feb7b3bea2458de1",
        },
    }
    account_id = save_account_data(
        email=email,
        access_token=access_token,
        totp_secret=totp_secret,
        email_source=resolve_email_source(email),
        proxy_used=selected_proxy or None,
        batch_dir=batch_dir,
        registration_name=name,
        birth_date=birthday,
        extra=extra,
    )
    flow_result = trigger_flow(access_token)
    if flow_result.get("ok"):
        logger.info("[aBaiFreeGPT协议] Flow 成功：%s", email)
    elif flow_result.get("status") != "skipped":
        logger.warning("[aBaiFreeGPT协议] Flow 失败：%s", flow_result.get("message"))
    return {
        "success": True,
        "email": email,
        "account_id": account_id,
        "access_token": access_token,
        "totp_secret": totp_secret,
        "flow": flow_result,
        "codex": codex_result,
        "error": None,
    }


__all__ = ["run_abai_protocol_registration"]
