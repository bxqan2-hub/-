# -*- coding: utf-8 -*-
"""
2FA（TOTP）配置

是否在注册成功后自动设置 2FA：
    True:  注册完成 → 拉新 OTP 邮件 → enroll TOTP → activate → 把 secret 写入 DB
    False: 跳过整个 2FA 流程，只保存 邮箱 + accessToken

关掉 2FA 不会影响账号可用性，仅意味着账号没有动态口令保护，且少收一封 OTP 邮件。
"""
from config.env_loader import apply_env_overrides

ENABLE_2FA = True

# 2FA 只在注册完成后的重认证阶段使用这些专用参数；不改变普通注册 OTP
# 的等待预算。通用 API 邮箱偶发 5~9 秒才返回，本阶段保留一次短重试。
TWOFA_OTP_MAX_WAIT = 120
TWOFA_OTP_POLL_INTERVAL = 2
TWOFA_OTP_SETTLE_SECONDS = 1
TWOFA_GENERIC_API_REQUEST_TIMEOUT = 12
TWOFA_GENERIC_API_RETRY_TIMEOUT = 8
TWOFA_GENERIC_API_MAX_CONSECUTIVE_ERRORS = 2

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'ENABLE_2FA': 'bool',
    'TWOFA_OTP_MAX_WAIT': 'int',
    'TWOFA_OTP_POLL_INTERVAL': 'int',
    'TWOFA_OTP_SETTLE_SECONDS': 'int',
    'TWOFA_GENERIC_API_REQUEST_TIMEOUT': 'float',
    'TWOFA_GENERIC_API_RETRY_TIMEOUT': 'float',
    'TWOFA_GENERIC_API_MAX_CONSECUTIVE_ERRORS': 'int',
})
