"""Protocol TOTP 2FA binding for ChatGPT/OpenAI accounts.

Captured HAR flow (all against ``chatgpt.com/backend-api/accounts/mfa`` with
``Authorization: Bearer <access_token>``):

    GET  /backend-api/accounts/mfa_info
        -> {"mfa_enabled": false, "factors": {"totp": [], ...}}

    POST /backend-api/accounts/mfa/enroll
        body: {"factor_type": "totp"}
        -> {"secret": "<base32-totp-secret>",
            "session_id": "<enroll-session>",
            "factor": {"id": "...", "factor_type": "totp"}}

    POST /backend-api/accounts/mfa/user/activate_enrollment
        body: {"code": "<6-digit-totp>", "factor_type": "totp",
               "session_id": "<enroll-session>"}
        -> {"success": true}

``enroll`` returns the TOTP secret the operator needs for an authenticator
app; the enrollment only becomes active once the current TOTP code is verified.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import time
from typing import Any, Optional

from .constants import CHATGPT_APP

MFA_INFO_URL = f"{CHATGPT_APP}/backend-api/accounts/mfa_info"
MFA_ENROLL_URL = f"{CHATGPT_APP}/backend-api/accounts/mfa/enroll"
MFA_ACTIVATE_URL = f"{CHATGPT_APP}/backend-api/accounts/mfa/user/activate_enrollment"

# Backwards-compatible private aliases for callers/tests that imported the old
# module constants directly.
_MFA_INFO_URL = MFA_INFO_URL
_MFA_ENROLL_URL = MFA_ENROLL_URL
_MFA_ACTIVATE_URL = MFA_ACTIVATE_URL


def totp_code(secret_b32: str, *, step: int = 30, digits: int = 6) -> str:
    """Generate the current TOTP code for a base32 secret (RFC 6238 / RFC 4226)."""
    key = base64.b32decode(secret_b32.strip().upper().replace(" ", ""))
    counter = int(time.time() // step)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def _api_get(session, url: str, access_token: str) -> dict[str, Any]:
    resp = session.get(
        url,
        headers={
            "Accept": "*/*",
            "Authorization": f"Bearer {access_token}",
            "Referer": f"{CHATGPT_APP}/",
        },
    )
    resp.raise_for_status()
    return resp.json()


def _api_post(
    session,
    url: str,
    access_token: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    resp = session.post(
        url,
        json=body,
        headers={
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Origin": CHATGPT_APP,
            "Referer": f"{CHATGPT_APP}/",
        },
    )
    resp.raise_for_status()
    return resp.json()


def get_mfa_status(session, access_token: str) -> dict[str, Any]:
    """Return the current MFA state for the account."""
    return _api_get(session, _MFA_INFO_URL, access_token)


def enroll_totp(session, access_token: str) -> dict[str, Any]:
    """Start TOTP enrollment; returns ``{"secret", "session_id", "factor"}``."""
    return _api_post(
        session,
        _MFA_ENROLL_URL,
        access_token,
        {"factor_type": "totp"},
    )


def activate_totp_enrollment(
    session,
    access_token: str,
    code: str,
    session_id: str,
) -> dict[str, Any]:
    """Verify the current TOTP code and activate the enrollment."""
    return _api_post(
        session,
        _MFA_ACTIVATE_URL,
        access_token,
        {"code": str(code), "factor_type": "totp", "session_id": session_id},
    )


def prepare_totp_activation(
    enrollment: dict[str, Any],
    *,
    code_provider=None,
) -> tuple[str, str, dict[str, str]]:
    """Validate an enrollment response and build the activation request.

    Browser registration uses the same payload builder while sending the HTTP
    requests through ``page.evaluate(fetch)`` so the MFA calls retain the exact
    browser cookies, TLS fingerprint, and proxy route that created the account.
    """
    secret = str(enrollment.get("secret") or "").strip()
    session_id = str(enrollment.get("session_id") or "").strip()
    if not secret or not session_id:
        raise RuntimeError(
            "TOTP 绑定失败：enroll 未返回 secret/session_id: "
            f"{json.dumps(enrollment)[:200]}"
        )
    if code_provider is None:
        code = totp_code(secret)
    else:
        code = str(code_provider(secret)).strip()
    if not code:
        raise RuntimeError("TOTP 绑定失败：无法生成验证码")
    return secret, session_id, {
        "code": code,
        "factor_type": "totp",
        "session_id": session_id,
    }


def bind_totp_2fa(
    session,
    access_token: str,
    *,
    code_provider=None,
) -> dict[str, Any]:
    """Bind TOTP 2FA to the account.

    ``code_provider(secret)`` returns the current 6-digit code; defaults to
    generating it from the secret directly (works when the caller can compute
    TOTP locally).
    """
    enroll = enroll_totp(session, access_token)
    secret, session_id, activation_body = prepare_totp_activation(
        enroll,
        code_provider=code_provider,
    )
    result = activate_totp_enrollment(
        session,
        access_token,
        activation_body["code"],
        session_id,
    )
    return {
        "secret": secret,
        "session_id": session_id,
        "activated": bool(result.get("success")),
        "result": result,
    }


__all__ = [
    "MFA_INFO_URL",
    "MFA_ENROLL_URL",
    "MFA_ACTIVATE_URL",
    "totp_code",
    "get_mfa_status",
    "enroll_totp",
    "activate_totp_enrollment",
    "prepare_totp_activation",
    "bind_totp_2fa",
]
