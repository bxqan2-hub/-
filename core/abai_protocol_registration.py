"""Entry point for the vendored aBaiFreeGPT protocol registration flow."""
from __future__ import annotations

import logging
import re
import time
from typing import Callable

from config import email as email_cfg
from config.proxy import mask_proxy_url
from core.account_export import save_account_data
from core.email_provider import resolve_email_source, wait_for_otp
from core.flow_trigger import trigger_flow
from core.abais_protocol.local_browser_session import LocalBrowserSession
from core.profile_utils import generate_random_birthday
from core.registration_password import (
    persist_confirmed_registration_password,
    registration_password,
)

logger = logging.getLogger(__name__)
def _pick_protocol_proxy(proxy: str | None) -> str:
    selected = str(proxy or "").strip()
    if selected:
        return selected
    from config.proxy import pick_proxy

    return str(pick_proxy(strict=True) or "").strip()


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
    registration_name = str(name or "").strip()
    if not registration_name:
        raise RuntimeError("协议注册缺少姓名")
    registration_birthdate = str(birthday or "").strip() or generate_random_birthday()
    from core.registration_service import check_stop_requested, is_stop_requested

    with LocalBrowserSession(proxy=selected_proxy, email=email) as transport:
        profile = dict(transport.profile)
        worker = ChatGPTProtocolRegister(
            proxy=selected_proxy,
            otp_callback=_otp_callback(email, otp_code),
            session=transport,
            cancel_check=is_stop_requested,
            # Upstream messages may contain auth URLs or remote response bodies.
            log_fn=lambda _message: logger.info("[aBaiFreeGPT协议] stage=%s http=%s", transport.stage, transport.http_status),
        )
        # Reuse the worker's existing borrowed-session and Sentinel interfaces.
        # No curl session/profile or proxy-rotation factory enters registration.
        worker.user_agent = profile["user_agent"]
        worker.sentinel = transport
        try:
            result = worker.run(
                email=email,
                password=password,
                name=registration_name,
                birthdate=registration_birthdate,
            )
        except Exception as exc:
            check_stop_requested()
            stage = re.search(r"\bstage=(protocol_[a-z_]+)\b", str(exc))
            code = getattr(exc, "code", "")
            if code in ("account_deactivated", "account_suspended", "account_banned"):
                from core.openai_auth import AccountUnusableError

                raise AccountUnusableError(
                    f"stage={transport.stage} http_status={transport.http_status or '-'} code={code}",
                    error_code=code,
                ) from None
            raise RuntimeError(
                f"stage={stage.group(1) if stage else transport.stage} "
                f"http_status={transport.http_status or '-'} type={type(exc).__name__}"
            ) from None

    access_token = str(result.get("access_token") or "").strip()
    totp = result.get("totp_2fa") if isinstance(result.get("totp_2fa"), dict) else {}
    totp_secret = str(totp.get("secret") or "").strip() or None
    if not access_token:
        raise RuntimeError("aBaiFreeGPT 协议注册未返回 access_token")
    if not totp_secret or totp.get("bound") is not True:
        raise RuntimeError("aBaiFreeGPT 协议注册未确认 TOTP 激活")
    if result.get("password_registered") is not True:
        raise RuntimeError("协议注册未确认密码提交成功")

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
        "browser_profile": profile,
        "registration_traffic": transport.traffic,
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
        proxy_used=mask_proxy_url(selected_proxy) or None,
        registration_exit_ip=transport.exit_geo.get("ip"),
        registration_exit_country=transport.exit_geo.get("country"),
        batch_dir=batch_dir,
        registration_name=registration_name,
        birth_date=registration_birthdate,
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
        "traffic": transport.traffic,
        "error": None,
    }


__all__ = ["run_abai_protocol_registration"]
