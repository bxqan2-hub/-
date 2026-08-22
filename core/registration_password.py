# -*- coding: utf-8 -*-
"""统一的 OpenAI 注册密码策略。

注册入口有多个浏览器适配器；密码生成和“是否需要设置密码”统一跟随
``ENABLE_2FA``，避免出现一个开关开 MFA、另一个开密码的分裂状态。
"""
from __future__ import annotations

import logging
import secrets
import string
import time


PASSWORD_MIN_LENGTH = 12
PASSWORD_DEFAULT_LENGTH = 14
PASSWORD_SYMBOLS = "!@#$%^&*?_-+="
logger = logging.getLogger(__name__)


def registration_password_required() -> bool:
    """只有设置页开启 2FA 时，才要求新账号同时拥有 OpenAI 密码。"""
    try:
        from config import twofa as twofa_cfg

        return bool(getattr(twofa_cfg, "ENABLE_2FA", False))
    except Exception:
        return False


def configured_registration_password() -> str:
    """读取 WebUI/.env 中的固定密码；空值表示按号生成。"""
    try:
        from config import register as register_cfg

        return str(getattr(register_cfg, "REGISTER_PASSWORD", "") or "").strip()
    except Exception:
        return ""


def generate_registration_password(length: int = PASSWORD_DEFAULT_LENGTH) -> str:
    """生成满足 OpenAI 强度要求的密码。

    每个字符组至少出现一次，剩余字符使用 ``secrets`` 生成，避免多个并发
    注册线程复用同一随机序列。
    """
    length = max(PASSWORD_MIN_LENGTH, int(length or PASSWORD_DEFAULT_LENGTH))
    groups = (string.ascii_uppercase, string.ascii_lowercase, string.digits, PASSWORD_SYMBOLS)
    chars = [secrets.choice(group) for group in groups]
    pool = "".join(groups)
    chars.extend(secrets.choice(pool) for _ in range(length - len(chars)))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def registration_password() -> str:
    """返回固定密码或本次注册生成的新密码。"""
    return configured_registration_password() or generate_registration_password()


def persist_confirmed_registration_password(email: str, password: str) -> bool:
    """密码页明确进入成功终态后，立即写入独立 pending checkpoint。"""
    normalized_email = str(email or "").strip().lower()
    confirmed_password = str(password or "").strip()
    if not normalized_email or not confirmed_password:
        return False
    from core import db

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            db.save_security_checkpoint(
                normalized_email,
                registration_password=confirmed_password,
            )
            logger.info("[Password] 已保存确认密码的安全凭据检查点：%s", normalized_email)
            return True
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.05 * attempt)
    # 服务端密码已经生效；本地写盘异常不能把远端状态误判成“未设置密码”。
    logger.error(
        "[Password] 安全凭据检查点连续 3 次写入失败：%s",
        type(last_error).__name__ if last_error else "UnknownError",
    )
    return False


def validate_registration_password(password: str) -> tuple[bool, str | None]:
    """校验固定/生成密码，返回 ``(ok, reason)``，不回显密码内容。"""
    value = str(password or "")
    if len(value) < PASSWORD_MIN_LENGTH:
        return False, "password_too_short"
    if not any(ch.isupper() for ch in value):
        return False, "password_missing_uppercase"
    if not any(ch.islower() for ch in value):
        return False, "password_missing_lowercase"
    if not any(ch.isdigit() for ch in value):
        return False, "password_missing_digit"
    if not any(not ch.isalnum() for ch in value):
        return False, "password_missing_symbol"
    return True, None


__all__ = [
    "PASSWORD_MIN_LENGTH",
    "PASSWORD_DEFAULT_LENGTH",
    "PASSWORD_SYMBOLS",
    "registration_password_required",
    "configured_registration_password",
    "generate_registration_password",
    "registration_password",
    "persist_confirmed_registration_password",
    "validate_registration_password",
]
