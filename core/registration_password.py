# -*- coding: utf-8 -*-
"""统一的 OpenAI 注册密码策略。

注册入口有多个浏览器适配器；密码生成和“是否需要设置密码”统一跟随
``ENABLE_2FA``，避免出现一个开关开 MFA、另一个开密码的分裂状态。
"""
from __future__ import annotations

import secrets
import string


PASSWORD_MIN_LENGTH = 12
PASSWORD_DEFAULT_LENGTH = 14
PASSWORD_SYMBOLS = "!@#$%^&*?_-+="


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
    "validate_registration_password",
]
