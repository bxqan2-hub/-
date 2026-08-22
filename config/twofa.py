# -*- coding: utf-8 -*-
"""
2FA（TOTP）配置

是否在注册成功后自动设置 2FA：
    True:  注册时设置 OpenAI 密码，并在注册完成后 enroll/activate TOTP；
           若注册密码页未出现，TOTP 激活后再用重认证补设密码
    False: 保持默认 OTP-only 注册，不主动设置密码或 MFA

ENABLE_2FA 是密码 + MFA 联动的唯一开关，默认关闭。
"""
from config.env_loader import apply_env_overrides

ENABLE_2FA = False

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
