# -*- coding: utf-8 -*-
"""
注册后处理模块：
    1. 拉取 /api/auth/session，从中抽取 accessToken / user 信息
    2. 设置 2FA（TOTP），返回 secret
    3. 把账号信息（邮箱 + accessToken + TOTP secret）落盘成 JSON

整体复用注册阶段的 BrowserSession（同一 cookie jar / 同一 IP / 同一 UA），
避免再起新会话被风控关联或缺失登录态。
"""
import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlencode, urlparse

import pyotp

from core.session import BrowserSession
from core.humanize import delay as human_delay
from core.otp_utils import redact_otp_text

logger = logging.getLogger(__name__)


class TwoFASetupError(RuntimeError):
    """带阶段和稳定错误码的 2FA 设置错误。"""

    def __init__(self, stage: str, code: str, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.stage = str(stage)
        self.code = str(code)
        self.http_status = int(http_status) if http_status else None


@dataclass(frozen=True)
class TwoFASetupResult:
    """可落库的 2FA 结果。

    ``activate_enrollment`` 成功后，服务端的 TOTP 已经生效。后续的只读
    ``/models`` 校验只是健康检查，不能因为它超时/被限流就丢掉 secret。
    因此结果始终携带 secret/token，并用 ``validation`` 描述健康检查状态。
    旧调用方继续读取 ``validation_status``（成功时为 HTTP 状态码）。
    """

    secret: str
    access_token: str
    activated_at: str
    validation_status: int | None
    validation_ok: bool = True
    validation_code: str | None = None
    validation_message: str | None = None
    expires: str | None = None
    # ENABLE_2FA=True 时补设的 OpenAI 账号密码；补密码流程在 TOTP 激活后执行。
    password: str | None = None
    # 补密码结果状态（供 WebUI/日志读取）
    password_setup: dict | None = None
    # TOTP activate 成功后的一次性 Secret 是否已提前 durable checkpoint。
    totp_checkpoint_persisted: bool = False

    @property
    def validation(self) -> dict[str, object]:
        """返回可直接写入账号 ``extra`` 的结构化校验状态。"""
        return {
            "ok": bool(self.validation_ok),
            "status": "passed" if self.validation_ok else "failed",
            "http_status": self.validation_status,
            "code": self.validation_code,
            "message": self.validation_message,
        }

    @property
    def validation_state(self) -> dict[str, object]:
        """``validation`` 的兼容别名，便于 WebUI/旧脚本读取。"""
        return self.validation

    @property
    def password_configured(self) -> bool:
        """注册密码是否已确认提交或在初始注册页完成。"""
        return bool(
            self.password
            and str(self.password).strip()
            and self.password_setup
            and self.password_setup.get("ok")
        )

    @property
    def security_ok(self) -> bool:
        """服务端已确认 TOTP 激活且密码完成；只读健康检查失败不回滚已生效 MFA。"""
        return bool(self.secret and self.activated_at and self.password_configured)

    @property
    def checkpoint(self) -> dict[str, bool]:
        """暴露早持久化结果；最终正式账号落库仍由调用方完成。"""
        return {
            "totp_persisted": bool(self.totp_checkpoint_persisted),
            "password_persisted": bool(
                self.password_setup and self.password_setup.get("checkpoint_persisted")
            ),
        }


def normalize_totp_secret(value: str) -> str:
    """规范化并校验服务端返回的 Base32 TOTP secret。"""
    normalized = "".join(str(value or "").split()).upper().rstrip("=")
    if not normalized:
        raise TwoFASetupError("totp_enroll", "totp_enroll_response_invalid", "2FA enroll 响应缺少 Secret")
    try:
        padded = normalized + "=" * (-len(normalized) % 8)
        base64.b32decode(padded, casefold=True)
    except (ValueError, TypeError) as exc:
        raise TwoFASetupError("totp_enroll", "totp_enroll_response_invalid", "2FA enroll Secret 不是有效 Base32") from exc
    return normalized


def _assert_authenticated_account(email: str, authenticated_email: str = "") -> None:
    """在密码/MFA 写操作前绑定注册目标与浏览器登录账号。

    参考项目会在浏览器 MFA 请求前比较目标邮箱和当前 session 邮箱；这里把
    同一边界提升到完整 2FA 流程入口，避免复用浏览器时把凭据写到别的账号。
    """
    expected = str(email or "").strip().casefold()
    authenticated = str(authenticated_email or "").strip().casefold()
    if not expected:
        raise TwoFASetupError(
            "totp_session",
            "totp_session_email_missing",
            "2FA 设置缺少目标账号邮箱",
        )
    if authenticated and authenticated != expected:
        raise TwoFASetupError(
            "totp_session",
            "totp_session_account_mismatch",
            "当前浏览器登录账号与注册目标不一致",
        )


def _validate_trusted_openai_url(value: str, *, stage: str, code: str, message: str) -> str:
    """只允许 OpenAI/ChatGPT HTTPS 回调，避免把重认证结果当成任意跳转地址。"""
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except (TypeError, ValueError):
        port = -1
        parsed = None
    hostname = str(getattr(parsed, "hostname", "") or "").lower() if parsed else ""
    trusted_host = (
        hostname == "chatgpt.com"
        or hostname.endswith(".chatgpt.com")
        or hostname == "openai.com"
        or hostname.endswith(".openai.com")
    )
    if (
        not parsed
        or parsed.scheme.lower() != "https"
        or parsed.username
        or parsed.password
        or not trusted_host
        or port not in (None, 443)
    ):
        raise TwoFASetupError(stage, code, message)
    return raw

# 输出目录（与项目根 .claude/ 工作区分离，单独放在 accounts/）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_DIR = _PROJECT_ROOT / "accounts"
_BATCH_ARCHIVE_LOCK = threading.RLock()


def _account_material_line(email: str, row: dict | None = None) -> str:
    """优先输出 Outlook 原始素材；没有素材时退回邮箱地址。"""
    if row:
        return row.get("original_email_line") or row.get("email") or email
    return email


def _account_copy_line(material_line: str, access_token: str, totp_secret: str | None = None) -> str:
    """生成包含 token 的整行归档，方便从批次汇总文件里复制。"""
    return f"{material_line}----{access_token}----{totp_secret}" if totp_secret else f"{material_line}----{access_token}"


def create_batch_archive_dir(count: int, workers: int = 1) -> Path:
    """为一次运行创建批次归档目录，例如 accounts/20260509-10个-3线程。"""
    day = datetime.now().strftime("%Y%m%d")
    base_name = f"{day}-{count}个" if workers <= 1 else f"{day}-{count}个-{workers}线程"
    folder = _ACCOUNTS_DIR / base_name
    suffix = 2
    while folder.exists():
        folder = _ACCOUNTS_DIR / f"{base_name}-{suffix}"
        suffix += 1
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "注册成功的邮箱.txt").write_text("", encoding="utf-8")
    (folder / "注册成功的token.txt").write_text("", encoding="utf-8")
    (folder / "注册成功整行.txt").write_text("", encoding="utf-8")
    (folder / "注册成功账号.json").write_text("[]\n", encoding="utf-8")
    return folder


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def _append_batch_archive(
    *,
    row_id: int,
    email: str,
    access_token: str,
    totp_secret: str | None,
    email_source: str | None,
    proxy_used: str | None,
    extra: dict,
    batch_dir: Path | None,
) -> Path:
    """把注册成功账号追加到本次批次目录的 TXT/JSON 文件中。"""
    from core import db

    folder = batch_dir or create_batch_archive_dir(count=1)
    row = db.get_account(row_id) or {}
    folder.mkdir(parents=True, exist_ok=True)
    material_line = _account_material_line(email, row)
    copy_line = _account_copy_line(material_line, access_token, totp_secret)
    archive = {
        "id": row_id,
        "email": email,
        "email_source": email_source,
        "proxy_used": proxy_used,
        "access_token": access_token,
        "totp_secret": totp_secret,
        "material_line": material_line,
        "copy_line": copy_line,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "row": row,
        "extra": extra,
    }

    with _BATCH_ARCHIVE_LOCK:
        _append_line(folder / "注册成功的邮箱.txt", material_line)
        _append_line(folder / "注册成功的token.txt", access_token)
        _append_line(folder / "注册成功整行.txt", copy_line)

        json_path = folder / "注册成功账号.json"
        try:
            rows = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else []
        except Exception:
            rows = []
        if not isinstance(rows, list):
            rows = []
        rows.append(archive)
        json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return folder


def follow_oauth_callback(session: BrowserSession, continue_url: str, referer: str = "https://auth.openai.com/about-you") -> str:
    """
    步骤12.5: 跟随 create_account 返回的 continue_url，完成 OAuth 回调。

    create_account 成功后返回的 continue_url 一般指向
        https://auth.openai.com/authorize/continue?...
    它会再 302 到
        https://chatgpt.com/api/auth/callback/openai?code=...&state=...
    回调请求会让 chatgpt.com 设置 `__Secure-next-auth.session-token` cookie，
    之后 /api/auth/session 才能返回 accessToken。

    Returns:
        重定向链最终落点 URL（一般是 chatgpt.com 站内地址）
    """
    if not continue_url:
        raise ValueError("continue_url 为空，无法完成 OAuth 回调")

    # continue_url 通常是 auth.openai.com/authorize/continue；
    # OTP 后 external_url 分支也可能直接给 chatgpt.com 回调地址。
    # 按目标域名选择导航头，避免 auth step 正确但请求头语义不一致。
    if str(continue_url).startswith("https://chatgpt.com"):
        headers = session.get_chatgpt_navigate_headers(referer=referer)
    else:
        headers = session.get_auth_navigate_headers(referer=referer)

    logger.info(f"[OAuth回调] 跟随 continue_url 完成 OAuth 回调...")
    resp = session.get(continue_url, headers=headers, allow_redirects=True)
    logger.info(f"[OAuth回调] 完成, 最终落点: {resp.url}")
    return resp.url


def fetch_session(session: BrowserSession) -> dict:
    """
    GET https://chatgpt.com/api/auth/session
    注册成功后立刻调用，拿到 accessToken / user / account / expires。

    Returns:
        完整 session JSON，包含字段:
            - accessToken: str (Bearer token, 用于 backend-api 调用)
            - user: {id, name, email, idp, iat, mfa}
            - account: {id, planType, structure, ...}
            - expires: ISO 时间字符串
    """
    url = "https://chatgpt.com/api/auth/session"
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/")

    logger.info("[Session] 拉取 ChatGPT session 信息...")
    resp = session.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("accessToken"):
        logger.error(f"[Session] 响应中没有 accessToken: {data}")
        raise RuntimeError("未拿到 accessToken，登录态可能未建立")

    user = data.get("user") or {}
    account = data.get("account") or {}
    logger.info(
        f"[Session] 成功，user_id={user.get('id')}, email={user.get('email')}, "
        f"plan={account.get('planType')}, mfa={user.get('mfa')}"
    )
    return data


def _trigger_reauth(session: BrowserSession, email: str) -> str:
    """
    步骤2-3: 发起密码重认证，返回 OpenAI authorize URL。
    重定向链会自动触发邮箱发送一份新的 OTP（用于 2FA 重认证）。
    """
    # 重新拿一次 csrf（旧的可能已过期）
    csrf_url = "https://chatgpt.com/api/auth/csrf"
    csrf_headers = session.get_nextauth_headers(referer="https://chatgpt.com/")
    # GET /api/auth/csrf 是页面读取接口，不发送 JSON Content-Type。
    csrf_headers.pop("content-type", None)
    csrf_resp = None
    try:
        csrf_resp = session.get(csrf_url, headers=csrf_headers)
        csrf_resp.raise_for_status()
        csrf_token = str((csrf_resp.json() or {}).get("csrfToken") or "").strip()
    except Exception as exc:
        status = getattr(csrf_resp, "status_code", None)
        if not status:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        raise TwoFASetupError(
            "totp_reauth",
            "totp_reauth_request_failed",
            "2FA 重认证 CSRF 请求失败",
            http_status=status,
        ) from exc
    if not csrf_token:
        raise TwoFASetupError("totp_reauth", "totp_reauth_start_failed", "2FA 重认证响应缺少 CSRF Token")

    # POST /api/auth/signin/openai 带 reauth 参数
    query = {
        "connection": "password",
        "login_hint": email,
        "reauth": "password",
        "max_age": "0",
        "ext-oai-did": session.device_id,
    }
    signin_url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query)

    headers = session.get_nextauth_headers(referer="https://chatgpt.com/")
    headers["content-type"] = "application/x-www-form-urlencoded"
    headers["origin"] = "https://chatgpt.com"

    body = urlencode({
        "callbackUrl": "https://chatgpt.com/?action=enable&factor=totp",
        "csrfToken": csrf_token,
        "json": "true",
    })

    logger.info("[2FA] 发起重认证 signin/openai...")
    resp = None
    try:
        resp = session.post(signin_url, headers=headers, data=body)
        resp.raise_for_status()
        auth_url = str((resp.json() or {}).get("url") or "").strip()
    except Exception as exc:
        status = getattr(resp, "status_code", None)
        if not status:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        raise TwoFASetupError(
            "totp_reauth",
            "totp_reauth_start_failed",
            "2FA 重认证启动失败",
            http_status=status,
        ) from exc
    return _validate_trusted_openai_url(
        auth_url,
        stage="totp_reauth",
        code="totp_reauth_url_untrusted",
        message="2FA 重认证返回了非可信地址",
    )


def _follow_reauth(session: BrowserSession, auth_url: str) -> str:
    """
    步骤3: 跟随 authorize URL 触发邮箱 OTP 发送。
    auth.openai.com 会重定向到 /email-verification 页面，期间发送 OTP 邮件。
    """
    headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")
    logger.info("[2FA] 跟随 authorize URL，触发 OTP 发送...")
    trusted_auth_url = _validate_trusted_openai_url(
        auth_url,
        stage="totp_reauth",
        code="totp_reauth_url_untrusted",
        message="2FA 重认证返回了非可信地址",
    )
    try:
        resp = session.get(trusted_auth_url, headers=headers, allow_redirects=True)
        status = getattr(resp, "status_code", None)
        if status is not None and not 200 <= int(status) < 400:
            raise TwoFASetupError(
                "totp_reauth",
                "totp_reauth_navigation_failed",
                "2FA 重认证页面返回非成功状态",
                http_status=int(status),
            )
        # curl_cffi/requests responses expose raise_for_status; keep the
        # explicit status check above so light-weight test doubles work too.
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
    except Exception as exc:
        if isinstance(exc, TwoFASetupError):
            raise
        raise TwoFASetupError("totp_reauth", "totp_reauth_navigation_failed", "2FA 重认证页面加载失败") from exc
    final_url = str(getattr(resp, "url", "") or "")
    _validate_trusted_openai_url(
        final_url,
        stage="totp_reauth",
        code="totp_reauth_navigation_failed",
        message="2FA 重认证落点不可信",
    )
    logger.info("[2FA] 重认证页面已到达")
    return final_url


def _validate_reauth_otp(session: BrowserSession, code: str) -> str:
    """
    步骤4: 提交邮箱 OTP 验证。
    返回 continue_url（带 code 参数的 callback URL，用于跳回 chatgpt.com）。
    """
    url = "https://auth.openai.com/api/accounts/email-otp/validate"
    headers = session.get_auth_headers(referer="https://auth.openai.com/email-verification")
    body = json.dumps({"code": code})

    logger.info("[2FA] 提交重认证邮箱验证码")
    try:
        resp = session.post(url, headers=headers, data=body)
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as exc:
        raise TwoFASetupError("totp_reauth", "totp_reauth_email_code_failed", "2FA 重认证邮箱验证码提交失败") from exc
    continue_url = str(data.get("continue_url") or "").strip()
    if not continue_url:
        raise TwoFASetupError("totp_reauth", "totp_reauth_email_code_failed", "邮箱验证码响应缺少 continue_url")
    return _validate_trusted_openai_url(
        continue_url,
        stage="totp_reauth",
        code="totp_reauth_continue_url_untrusted",
        message="邮箱验证码返回了非可信回调地址",
    )


def _exchange_new_token(session: BrowserSession, continue_url: str) -> str:
    """
    步骤5: 跟随 continue_url 完成回调，再次拉 /api/auth/session 拿到新 accessToken
    （此时 token 内嵌的 pwd_auth_time 是新鲜的，2FA enroll 才会接受）。
    """
    trusted_continue_url = _validate_trusted_openai_url(
        continue_url,
        stage="totp_session",
        code="totp_reauth_continue_url_untrusted",
        message="2FA 回调地址不可信",
    )
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/email-verification")
    logger.info("[2FA] 跟随 continue_url，刷新 session-token cookie...")
    try:
        callback_resp = session.get(trusted_continue_url, headers=headers, allow_redirects=True)
        status = getattr(callback_resp, "status_code", None)
        if status is not None and not 200 <= int(status) < 400:
            raise TwoFASetupError(
                "totp_session",
                "totp_session_refresh_failed",
                "2FA 重认证回调返回非成功状态",
                http_status=int(status),
            )
        if hasattr(callback_resp, "raise_for_status"):
            callback_resp.raise_for_status()
        callback_url = str(getattr(callback_resp, "url", "") or "").strip()
        if callback_url:
            _validate_trusted_openai_url(
                callback_url,
                stage="totp_session",
                code="totp_session_refresh_failed",
                message="2FA 重认证回调落点不可信",
            )
    except Exception as exc:
        if isinstance(exc, TwoFASetupError):
            raise
        raise TwoFASetupError("totp_session", "totp_session_refresh_failed", "2FA 重认证回调失败") from exc

    # 拿新的 accessToken
    try:
        new_session = fetch_session(session)
        new_token = str(new_session.get("accessToken") or "").strip()
    except Exception as exc:
        raise TwoFASetupError("totp_session", "totp_session_refresh_failed", "2FA 重认证后未取得新的 Session Token") from exc
    if not new_token:
        raise TwoFASetupError("totp_session", "totp_session_refresh_failed", "2FA 重认证后 Session Token 为空")
    try:
        setattr(session, "_twofa_session_expires", str(new_session.get("expires") or "").strip() or None)
    except Exception:
        pass
    logger.info("[2FA] 已取得新的重认证 Session Token")
    return new_token


def _enroll_totp(session: BrowserSession, access_token: str) -> tuple[str, str]:
    """
    步骤6: 注册 TOTP，返回 (secret, session_id)
    """
    url = "https://chatgpt.com/backend-api/accounts/mfa/enroll"
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()

    body = json.dumps({"factor_type": "totp"})

    logger.info("[2FA] 注册 TOTP...")
    try:
        resp = session.post(url, headers=headers, data=body)
        if resp.status_code != 200:
            raise TwoFASetupError("totp_enroll", "totp_enroll_failed", "2FA TOTP enroll 失败", http_status=resp.status_code)
        data = resp.json() or {}
    except TwoFASetupError:
        raise
    except Exception as exc:
        raise TwoFASetupError("totp_enroll", "totp_api_request_failed", "2FA enroll 请求失败") from exc
    secret = normalize_totp_secret(str(data.get("secret") or ""))
    session_id = str(data.get("session_id") or "").strip()
    if not session_id:
        raise TwoFASetupError("totp_enroll", "totp_enroll_failed", "2FA enroll 响应缺少 session_id")
    logger.info("[2FA] TOTP enroll 已返回有效 Secret")
    return secret, session_id


def _activate_totp(
    session: BrowserSession,
    access_token: str,
    secret: str,
    session_id: str,
) -> bool:
    """
    步骤7: 用 secret 生成 6 位 TOTP 码，激活 2FA。
    """
    url = "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()

    normalized_secret = normalize_totp_secret(secret)
    totp_code = pyotp.TOTP(normalized_secret).now()
    body = json.dumps({
        "code": totp_code,
        "factor_type": "totp",
        "session_id": session_id,
    })

    logger.info("[2FA] 激活 TOTP enrollment")
    try:
        resp = session.post(url, headers=headers, data=body)
        if resp.status_code != 200:
            raise TwoFASetupError("totp_activate", "totp_activate_failed", "2FA TOTP 激活失败", http_status=resp.status_code)
        data = resp.json() or {}
    except TwoFASetupError:
        raise
    except Exception as exc:
        raise TwoFASetupError("totp_activate", "totp_api_request_failed", "2FA 激活请求失败") from exc
    if data.get("success") is not True:
        raise TwoFASetupError("totp_activate", "totp_activate_failed", "2FA 激活返回 success=false")
    return True


def _persist_activated_totp_checkpoint(email: str, secret: str, access_token: str) -> bool:
    """activate 明确 success=true 后立即保存不可回取的 TOTP Secret。"""
    from core import db

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            db.save_security_checkpoint(
                email,
                totp_secret=normalize_totp_secret(secret),
                access_token=str(access_token or "").strip(),
            )
            logger.info("[2FA] 已保存激活 TOTP 的安全凭据检查点：%s", email)
            return True
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.05 * attempt)
    # TOTP 已在服务端生效，落盘失败不能让调用方丢掉仍在内存中的 Secret。
    logger.error(
        "[2FA] 安全凭据检查点连续 3 次写入失败：%s",
        type(last_error).__name__ if last_error else "UnknownError",
    )
    return False


def _wait_for_totp_window(min_remaining: float = 4.0) -> None:
    remaining = 30.0 - (time.time() % 30.0)
    if remaining < float(min_remaining):
        time.sleep(remaining + 0.25)


_PASSWORD_CODE_SELECTOR = (
    'input[autocomplete="one-time-code"], input[name="code"], '
    'input[name="otp"], input[inputmode="numeric"]'
)
_PASSWORD_INPUT_SELECTOR = 'input[type="password"], input[autocomplete="new-password"]'
_PASSWORD_AUTHENTICATOR_RE = re.compile(
    r"authenticator|verification app|two[- ]factor|2fa|动态口令|身份验证器|认证器|認証アプリ|인증 앱",
    re.IGNORECASE,
)
_PASSWORD_EMAIL_RE = re.compile(
    r"email[- ]verification|email code|verification code sent|邮箱|郵箱|メール|이메일",
    re.IGNORECASE,
)
_PASSWORD_REJECTION_RE = re.compile(
    r"incorrect|invalid|rejected|failed|error|try again|could not|unable|错误|无效|不匹配|失败|拒绝|重试",
    re.IGNORECASE,
)
_PASSWORD_SUCCESS_RE = re.compile(
    r"password\s+(?:updated|changed|added|created)|密码(?:已更新|已更改|已添加|设置成功)|"
    r"パスワード.*更新|비밀번호.*업데이트",
    re.IGNORECASE,
)
_BROWSER_CHALLENGE_RE = re.compile(
    r"cloudflare|managed challenge|checking your browser|verify (?:you are )?human|"
    r"just a moment|正在验证|验证您是真人|セキュリティチェック|보안 확인",
    re.IGNORECASE,
)

_PASSWORD_REAUTH_JS = r"""
async () => {
  const sessionResponse = await fetch('/api/auth/session', {
    credentials: 'include', headers: {'accept': 'application/json'},
  });
  const session = await sessionResponse.json().catch(() => ({}));
  const email = String(session?.user?.email || '');
  if (!sessionResponse.ok || !session?.accessToken || !email)
    return {ok:false, stage:'session', status:sessionResponse.status};
  const csrfResponse = await fetch('/api/auth/csrf', {
    credentials: 'include', headers: {'accept': 'application/json'},
  });
  const csrf = await csrfResponse.json().catch(() => ({}));
  const csrfToken = String(csrf.csrfToken || '');
  if (!csrfResponse.ok || !csrfToken)
    return {ok:false, stage:'csrf', status:csrfResponse.status};
  const deviceCookie = document.cookie.split(';').map(v => v.trim())
    .find(v => v.startsWith('oai-did='));
  const deviceId = deviceCookie
    ? decodeURIComponent(deviceCookie.slice('oai-did='.length)) : crypto.randomUUID();
  const query = new URLSearchParams({
    post_login_add_password: 'true', prompt: 'login', max_age: '0',
    login_hint: email, 'ext-oai-did': deviceId,
  });
  const body = new URLSearchParams({
    csrfToken, callbackUrl: 'https://chatgpt.com/?tm_action=password&tm_stage=password_done',
    json: 'true',
  });
  const signinResponse = await fetch(`/api/auth/signin/openai?${query.toString()}`, {
    method: 'POST', credentials: 'include',
    headers: {'accept':'application/json', 'content-type':'application/x-www-form-urlencoded'},
    body: body.toString(),
  });
  const signin = await signinResponse.json().catch(() => ({}));
  return {ok: signinResponse.ok && !!signin.url, stage:'signin',
    status: signinResponse.status, url: String(signin.url || '')};
}
"""

_PASSWORD_REAUTH_SELENIUM_JS = r"""
const done = arguments[arguments.length - 1];
(async () => {
  try {
    const result = await (async () => {
      const sessionResponse = await fetch('/api/auth/session', {
        credentials: 'include', headers: {'accept': 'application/json'},
      });
      const session = await sessionResponse.json().catch(() => ({}));
      const email = String(session?.user?.email || '');
      if (!sessionResponse.ok || !session?.accessToken || !email)
        return {ok:false, stage:'session', status:sessionResponse.status};
      const csrfResponse = await fetch('/api/auth/csrf', {
        credentials: 'include', headers: {'accept': 'application/json'},
      });
      const csrf = await csrfResponse.json().catch(() => ({}));
      const csrfToken = String(csrf.csrfToken || '');
      if (!csrfResponse.ok || !csrfToken)
        return {ok:false, stage:'csrf', status:csrfResponse.status};
      const deviceCookie = document.cookie.split(';').map(v => v.trim())
        .find(v => v.startsWith('oai-did='));
      const deviceId = deviceCookie
        ? decodeURIComponent(deviceCookie.slice('oai-did='.length)) : crypto.randomUUID();
      const query = new URLSearchParams({post_login_add_password:'true', prompt:'login',
        max_age:'0', login_hint:email, 'ext-oai-did':deviceId});
      const body = new URLSearchParams({csrfToken,
        callbackUrl:'https://chatgpt.com/?tm_action=password&tm_stage=password_done', json:'true'});
      const signinResponse = await fetch(`/api/auth/signin/openai?${query.toString()}`, {
        method:'POST', credentials:'include',
        headers:{'accept':'application/json','content-type':'application/x-www-form-urlencoded'},
        body:body.toString(),
      });
      const signin = await signinResponse.json().catch(() => ({}));
      return {ok:signinResponse.ok && !!signin.url, stage:'signin', status:signinResponse.status,
        url:String(signin.url || '')};
    })();
    done(result);
  } catch (error) {
    done({ok:false, stage:'exception', status:null, error:String(error || '')});
  }
})();
"""

_PASSWORD_RESEND_JS = r"""
() => {
  const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
    && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
  const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
  const target = [...document.querySelectorAll('button,a,[role="button"],[role="link"]')]
    .filter(el => visible(el) && enabled(el)).find(el =>
      /resend|send again|send a new|new code|重新发送|再次发送|重发|重發|再送信|もう一度送信|다시\s*보내|재전송/i
        .test([el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title')]
          .filter(Boolean).join(' ')));
  if (!target) return false;
  target.scrollIntoView({block:'center'}); target.click(); return true;
}
"""


def _is_playwright_page(driver) -> bool:
    return callable(getattr(driver, "evaluate", None)) and callable(getattr(driver, "locator", None))


_TOTP_BROWSER_POST_JS = r"""
async request => {
  try {
    const path = String(request?.path || '');
    const payload = request?.payload || {};
    let accessToken = String(request?.accessToken || '');
    let email = '';
    let expires = '';
    if (!accessToken) {
      const sessionResponse = await fetch('/api/auth/session', {
        cache: 'no-store', credentials: 'include',
        headers: {'accept': 'application/json', 'cache-control': 'no-cache'},
      });
      const session = await sessionResponse.json().catch(() => ({}));
      accessToken = String(session?.accessToken || '');
      email = String(session?.user?.email || '');
      expires = String(session?.expires || '');
      if (!sessionResponse.ok || !accessToken) {
        return {status:sessionResponse.status, stage:'session', email, expires,
          accessToken:'', body:{}, json:true};
      }
    }
    const response = await fetch(path, {
      method: 'POST', credentials: 'include',
      headers: {
        'accept': 'application/json',
        'authorization': `Bearer ${accessToken}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify(payload),
    });
    const contentType = String(response.headers.get('content-type') || '');
    const text = await response.text();
    let body = {};
    let json = true;
    try { body = text ? JSON.parse(text) : {}; } catch (_) { json = false; }
    return {status:response.status, stage:'request', contentType, email, expires,
      accessToken, body, json};
  } catch (error) {
    return {status:0, stage:'exception', error:String(error || ''), body:{}, json:false};
  }
}
"""


_TOTP_BROWSER_POST_SELENIUM_JS = r"""
const path = String(arguments[0] || '');
const payload = arguments[1] || {};
const suppliedAccessToken = String(arguments[2] || '');
const done = arguments[arguments.length - 1];
(async () => {
  try {
    let accessToken = suppliedAccessToken;
    let email = '';
    let expires = '';
    if (!accessToken) {
      const sessionResponse = await fetch('/api/auth/session', {
        cache:'no-store', credentials:'include',
        headers:{'accept':'application/json','cache-control':'no-cache'},
      });
      const session = await sessionResponse.json().catch(() => ({}));
      accessToken = String(session?.accessToken || '');
      email = String(session?.user?.email || '');
      expires = String(session?.expires || '');
      if (!sessionResponse.ok || !accessToken) {
        done({status:sessionResponse.status, stage:'session', email, expires,
          accessToken:'', body:{}, json:true});
        return;
      }
    }
    const response = await fetch(path, {
      method:'POST', credentials:'include',
      headers:{
        'accept':'application/json',
        'authorization':`Bearer ${accessToken}`,
        'content-type':'application/json',
      },
      body:JSON.stringify(payload),
    });
    const contentType = String(response.headers.get('content-type') || '');
    const text = await response.text();
    let body = {};
    let json = true;
    try { body = text ? JSON.parse(text) : {}; } catch (_) { json = false; }
    done({status:response.status, stage:'request', contentType, email, expires,
      accessToken, body, json});
  } catch (error) {
    done({status:0, stage:'exception', error:String(error || ''), body:{}, json:false});
  }
})();
"""


def _driver_supports_authenticated_fetch(driver) -> bool:
    return bool(
        driver is not None
        and (
            _is_playwright_page(driver)
            or callable(getattr(driver, "execute_async_script", None))
        )
    )


def _browser_authenticated_json_post(
    driver,
    path: str,
    payload: dict,
    *,
    access_token: str = "",
    stage: str,
    code: str,
    message: str,
) -> dict:
    """在当前已登录浏览器网络栈内发送 MFA 请求，避免协议会话被 CF 单独挑战。"""
    try:
        if _is_playwright_page(driver):
            result = driver.evaluate(
                _TOTP_BROWSER_POST_JS,
                {"path": path, "payload": payload, "accessToken": access_token},
            )
        else:
            result = driver.execute_async_script(
                _TOTP_BROWSER_POST_SELENIUM_JS,
                path,
                payload,
                access_token,
            )
    except Exception as exc:
        # Preserve a short, redacted transport diagnostic.  Previously a
        # Selenium execute_async_script timeout/renderer disconnect was
        # collapsed to the generic message, making activate failures
        # indistinguishable from HTTP rejection.
        detail = redact_otp_text(f"{type(exc).__name__}: {str(exc)[:160]}")
        suffix = f" stage=exception detail={detail}" if detail else " stage=exception"
        raise TwoFASetupError(stage, code, f"{message}{suffix}") from exc

    if not isinstance(result, dict):
        raise TwoFASetupError(stage, code, message)
    status = int(result.get("status") or 0)
    if status != 200:
        # Keep the stable error code for callers, but include the browser
        # response stage/status so the registration log explains whether the
        # failure was a missing session, a rejected token, or a transport error.
        detail = str(result.get("error") or "").strip()
        if not detail and isinstance(result.get("body"), dict):
            body = result.get("body") or {}
            for key in ("error_code", "error", "code", "message", "detail"):
                value = str(body.get(key) or "").strip()
                if value:
                    detail = value
                    break
        suffix = f" stage={result.get('stage') or 'request'}"
        if detail:
            suffix += f" detail={redact_otp_text(detail[:180])}"
        raise TwoFASetupError(stage, code, f"{message} HTTP {status or 0}{suffix}", http_status=status or None)
    if not bool(result.get("json", True)):
        raise TwoFASetupError(stage, code, f"{message}（响应不是 JSON）", http_status=status)
    body = result.get("body")
    if not isinstance(body, dict):
        raise TwoFASetupError(stage, code, f"{message}（响应结构异常）", http_status=status)
    return {
        "body": body,
        "email": str(result.get("email") or "").strip(),
        "access_token": str(result.get("accessToken") or access_token or "").strip(),
        "expires": str(result.get("expires") or "").strip() or None,
    }


def _setup_totp_with_driver(
    driver,
    email: str,
    *,
    authenticated_email: str = "",
    access_token: str = "",
) -> tuple[str, str, str | None]:
    """按参考项目的实时浏览器方案直接 enroll/activate TOTP。"""
    _assert_authenticated_account(email, authenticated_email)
    current_url = _password_page_url(driver)
    try:
        current_host = str(urlparse(current_url).hostname or "").lower() if current_url else ""
    except ValueError:
        current_host = ""
    if current_host and not (current_host == "chatgpt.com" or current_host.endswith(".chatgpt.com")):
        logger.info("[2FA] MFA 浏览器请求前返回 ChatGPT 同源页面")
        try:
            if _is_playwright_page(driver):
                driver.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=30_000)
            else:
                driver.get("https://chatgpt.com/")
        except Exception as exc:
            raise TwoFASetupError(
                "totp_session",
                "totp_browser_origin_failed",
                "浏览器无法返回 ChatGPT 同源页面",
            ) from exc
    logger.info("[2FA] 使用当前浏览器登录态直接执行 MFA enroll/activate，跳过 CSRF 重认证")
    enroll_result = _browser_authenticated_json_post(
        driver,
        "/backend-api/accounts/mfa/enroll",
        {"factor_type": "totp"},
        access_token=access_token,
        stage="totp_enroll",
        code="totp_browser_enroll_failed",
        message="浏览器 MFA enroll 请求失败",
    )
    response_email = str(enroll_result.get("email") or "").casefold()
    expected_email = str(email or "").strip().casefold()
    if response_email and response_email != expected_email:
        raise TwoFASetupError(
            "totp_session",
            "totp_session_account_mismatch",
            "浏览器登录账号与待设置 2FA 的邮箱不一致",
        )
    enroll = enroll_result["body"]
    secret = normalize_totp_secret(str(enroll.get("secret") or ""))
    session_id = str(enroll.get("session_id") or "").strip()
    access_token = str(enroll_result.get("access_token") or "").strip()
    if not session_id:
        raise TwoFASetupError(
            "totp_enroll",
            "totp_enroll_response_invalid",
            "浏览器 MFA enroll 响应缺少 session_id",
            http_status=200,
        )
    if not access_token:
        raise TwoFASetupError(
            "totp_session",
            "totp_session_refresh_failed",
            "浏览器登录态没有返回 Access Token",
            http_status=200,
        )

    activation = None
    activation_error: TwoFASetupError | None = None
    # A browser renderer/network interruption or a boundary-time TOTP code
    # can fail the activate POST even though enroll succeeded. Retry once in
    # the same browser session with a fresh 30-second code; never re-enroll,
    # switch proxy, or persist the Secret before success=true.
    for activate_attempt in (1, 2):
        if activate_attempt == 1:
            _wait_for_totp_window()
        else:
            try:
                _wait_for_totp_window(min_remaining=8.0)
            except TypeError as exc:
                # Keep compatibility with test/integration adapters that
                # expose the historical zero-argument wait hook.
                if "min_remaining" not in str(exc):
                    raise
                _wait_for_totp_window()
        try:
            logger.info("[2FA] 浏览器 MFA activate attempt=%s/2", activate_attempt)
            activation = _browser_authenticated_json_post(
                driver,
                "/backend-api/accounts/mfa/user/activate_enrollment",
                {
                    "code": pyotp.TOTP(secret).now(),
                    "factor_type": "totp",
                    "session_id": session_id,
                },
                access_token=access_token,
                stage="totp_activate",
                code="totp_browser_activate_failed",
                message="浏览器 MFA activate 请求失败",
            )["body"]
            activation_error = None
            break
        except TwoFASetupError as exc:
            activation_error = exc
            retryable = (
                exc.http_status is None
                or exc.http_status in {408, 425, 429}
                or exc.http_status >= 500
                or (exc.http_status == 200 and exc.code == "totp_activate_failed")
            )
            if activate_attempt == 1 and retryable:
                logger.warning(
                    "[2FA] 浏览器 MFA activate 可重试失败，等待下一 TOTP 窗口：status=%s code=%s",
                    exc.http_status or "-",
                    exc.code,
                )
                continue
            raise
    if activation is None and activation_error is not None:
        raise activation_error
    if activation.get("success") is not True:
        raise TwoFASetupError(
            "totp_activate",
            "totp_activate_failed",
            "2FA TOTP 激活未确认成功",
            http_status=200,
        )
    logger.info("[2FA] 浏览器 MFA enroll/activate 已完成")
    return secret, access_token, enroll_result.get("expires")


def _password_log_prefix(driver) -> str:
    try:
        from core.roxy_registration import _log_prefix
        return _log_prefix(driver)
    except Exception:
        return "[2FA]"


def _password_page_url(driver) -> str:
    try:
        value = getattr(driver, "url", None)
        if callable(value):
            value = value()
        if value:
            return str(value)
        return str(getattr(driver, "current_url", "") or "")
    except Exception:
        return ""


def _password_done_callback(url: str) -> bool:
    """确认已回到预先指定的可信密码完成回调。"""
    try:
        parsed = urlparse(str(url or ""))
        host = str(parsed.hostname or "").lower().rstrip(".")
        query = parse_qs(parsed.query)
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and (host == "chatgpt.com" or host.endswith(".chatgpt.com"))
        and str((query.get("tm_action") or [""])[0]).lower() == "password"
        and str((query.get("tm_stage") or [""])[0]).lower() == "password_done"
    )


def _password_body_text(driver) -> str:
    if _is_playwright_page(driver):
        try:
            return str(driver.locator("body").inner_text(timeout=1000) or "")
        except Exception:
            try:
                return str(driver.evaluate("() => document.body?.innerText || ''") or "")
            except Exception:
                return ""
    try:
        return str(driver.execute_script("return document.body ? document.body.innerText : ''; ") or "")
    except Exception:
        return ""


def _password_visible_playwright(driver, selector: str) -> list:
    try:
        locator = driver.locator(selector)
        count = min(int(locator.count()), 20)
    except Exception:
        return []
    result = []
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible(timeout=500):
                result.append(candidate)
        except Exception:
            continue
    return result


def _password_visible_selenium(driver, selector: str) -> list:
    try:
        from selenium.webdriver.common.by import By
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
    except Exception:
        try:
            elements = driver.find_elements("css selector", selector)
        except Exception:
            try:
                elements = driver.find_elements_by_css_selector(selector)
            except Exception:
                elements = []
    visible = []
    for candidate in elements[:20]:
        try:
            if candidate.is_displayed() and candidate.is_enabled():
                visible.append(candidate)
        except Exception:
            continue
    return visible


def _password_visible_inputs(driver, selector: str) -> list:
    return _password_visible_playwright(driver, selector) if _is_playwright_page(driver) else _password_visible_selenium(driver, selector)


def _password_click_playwright(driver, field, selectors: str) -> bool:
    try:
        scope = field.locator("xpath=ancestor::form[1]")
        button = scope.locator(selectors)
        if button.count() and button.first.is_visible(timeout=500):
            button.first.click(timeout=5000)
            return True
    except Exception:
        pass
    try:
        button = driver.locator(selectors)
        if button.count() and button.first.is_visible(timeout=500):
            button.first.click(timeout=5000)
            return True
    except Exception:
        pass
    try:
        driver.keyboard.press("Enter")
        return True
    except Exception:
        return False


def _password_find_submit_selenium(field):
    try:
        scope = field.find_element("xpath", "ancestor::form[1]")
        submit = scope.find_element(
            "css selector",
            'button[type="submit"], input[type="submit"], button[name="intent"]',
        )
        if submit.is_displayed() and submit.is_enabled():
            return submit
    except Exception:
        pass
    return None


def _password_click_selenium(driver, field) -> bool:
    submit = _password_find_submit_selenium(field)
    if submit is not None:
        try:
            submit.click()
            return True
        except Exception:
            pass
    try:
        clicked = bool(driver.execute_script(
            """
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
            const target = [...document.querySelectorAll('button[type="submit"]:not([disabled]), input[type="submit"]:not([disabled]), button[name="intent"]:not([disabled])')]
              .find(visible); if (!target) return false; target.click(); return true;
            """
        ))
        if clicked:
            return True
    except Exception:
        pass
    try:
        from selenium.webdriver.common.keys import Keys

        field.send_keys(Keys.ENTER)
        return True
    except Exception:
        return False


def _password_submit_code(driver, code: str) -> bool:
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return False
    fields = _password_visible_inputs(driver, _PASSWORD_CODE_SELECTOR)
    if not fields:
        return False
    initial_url = _password_page_url(driver)

    def _submitted_or_advanced(clicked: bool) -> bool:
        if clicked:
            return True
        time.sleep(0.35)
        return bool(
            _password_page_url(driver) != initial_url
            or not _password_visible_inputs(driver, _PASSWORD_CODE_SELECTOR)
        )

    try:
        if _is_playwright_page(driver):
            if len(fields) >= 6:
                for field, char in zip(fields[-6:], code):
                    field.fill(char, timeout=3000)
                return _submitted_or_advanced(_password_click_playwright(driver, fields[-6], 'button[type="submit"], input[type="submit"], button[name="intent"]'))
            fields[0].fill(code, timeout=5000)
            return _submitted_or_advanced(_password_click_playwright(driver, fields[0], 'button[type="submit"], input[type="submit"], button[name="intent"]'))
        if len(fields) >= 6:
            for field, char in zip(fields[-6:], code):
                field.clear(); field.send_keys(char)
            return _submitted_or_advanced(_password_click_selenium(driver, fields[-6]))
        fields[0].clear(); fields[0].send_keys(code)
        return _submitted_or_advanced(_password_click_selenium(driver, fields[0]))
    except Exception as exc:
        logger.debug("[2FA][密码] 验证码输入异常：%s", type(exc).__name__)
        return False


def _password_submit_new_password(driver, fields: list, password: str) -> bool:
    try:
        if _is_playwright_page(driver):
            for field in fields:
                field.fill(password, timeout=5000)
            return _password_click_playwright(
                driver,
                fields[0],
                'button[type="submit"], input[type="submit"], button[name="intent"], '
                'button:has-text("Save"), button:has-text("Continue"), button:has-text("Update"), '
                'button:has-text("保存"), button:has-text("继续"), button:has-text("更新")',
            )
        for field in fields:
            field.clear(); field.send_keys(password)
        return _password_click_selenium(driver, fields[0])
    except Exception as exc:
        logger.debug("[2FA][密码] 密码提交异常：%s", type(exc).__name__)
        return False


def _password_click_resend(driver) -> bool:
    try:
        if _is_playwright_page(driver):
            return bool(driver.evaluate(_PASSWORD_RESEND_JS))
        return bool(driver.execute_script("return (" + _PASSWORD_RESEND_JS + ")();"))
    except Exception:
        return False


def _password_screenshot(driver) -> None:
    path = str(_PROJECT_ROOT / "run" / "password_setup_failed.png")
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if _is_playwright_page(driver):
            driver.screenshot(path=path)
        else:
            driver.save_screenshot(path)
        logger.info("[2FA][密码] 失败截图已保存 %s", path)
    except Exception:
        pass


def _setup_password_with_driver(
    *,
    driver,
    session: BrowserSession,
    email: str,
    password: str,
    totp_secret: str | None = None,
    timeout_seconds: float = 120.0,
    password_mode: str = "add",
    console_compat: bool = False,
) -> dict:
    """在 Selenium/Cloak 或同步 Playwright 页面中完成注册后密码设置。

    该流程对齐 maile456 项目的 ``add_password_in_settings``：先走
    ``post_login_add_password`` 重认证，再按页面实际挑战选择邮箱 OTP 或 TOTP，
    最后填写密码。Selenium 必须用 ``execute_async_script``，Cloak 适配器会把
    最后一个参数映射到其异步完成回调；Playwright 则直接 ``evaluate`` 异步函数。

    ``password_mode``/``console_compat`` 只供账号页的独立安全扩展使用。默认值
    保持原注册流程完全不变；扩展可以选择 reset 模式，并补齐控制台脚本使用的
    connection/reauth 参数。
    """
    from core.registration_password import validate_registration_password
    from core.email_provider import wait_for_otp

    valid, reason = validate_registration_password(password)
    if not valid:
        return {
            "ok": False, "status": "failed", "stage": "password_validation",
            "code": "generated_password_invalid", "message": reason or "密码强度不符合要求",
            "http_status": None,
        }
    prefix = _password_log_prefix(driver)
    try:
        otp_history = _snapshot_otp_history(email, timeout=2.0)
    except Exception:
        otp_history = set()
    otp_message_ids = _snapshot_otp_message_ids(email, timeout=2.0)
    requested_at = time.time()
    normalized_mode = str(password_mode or "add").strip().lower()
    if normalized_mode not in {"add", "reset"}:
        return {
            "ok": False, "status": "failed", "stage": "password_validation",
            "code": "password_mode_invalid", "message": "password_mode 仅支持 add/reset",
            "http_status": None,
        }
    mode_key = (
        "post_login_password_reset"
        if normalized_mode == "reset"
        else "post_login_add_password"
    )
    reauth_js = _PASSWORD_REAUTH_JS.replace("post_login_add_password: 'true'", f"{mode_key}: 'true'")
    reauth_selenium_js = _PASSWORD_REAUTH_SELENIUM_JS.replace(
        "post_login_add_password:'true'",
        f"{mode_key}:'true'",
    )
    if console_compat:
        reauth_js = reauth_js.replace(
            "  const body = new URLSearchParams({",
            "  query.set('connection', 'password');\n"
            "  query.set('reauth', 'password');\n"
            "  const body = new URLSearchParams({",
            1,
        )
        reauth_selenium_js = reauth_selenium_js.replace(
            "      const body = new URLSearchParams({csrfToken,",
            "      query.set('connection','password'); query.set('reauth','password');\n"
            "      const body = new URLSearchParams({csrfToken,",
            1,
        )
    try:
        if _is_playwright_page(driver):
            reauth = driver.evaluate(reauth_js)
        else:
            if not callable(getattr(driver, "execute_async_script", None)):
                raise RuntimeError("driver_missing_execute_async_script")
            reauth = driver.execute_async_script(reauth_selenium_js)
    except Exception as exc:
        logger.warning("%s[2FA][密码] 重认证请求失败：%s", prefix, type(exc).__name__)
        return {
            "ok": False, "status": "failed", "stage": "password_reauth",
            "code": "password_reauth_request_failed", "message": f"{type(exc).__name__}: {str(exc)[:160]}",
            "http_status": None,
        }
    if not isinstance(reauth, dict) or not reauth.get("ok"):
        status = reauth.get("status") if isinstance(reauth, dict) else None
        return {
            "ok": False, "status": "failed", "stage": "password_reauth",
            "code": "password_reauth_start_failed",
            "message": f"重认证启动失败 stage={reauth.get('stage') if isinstance(reauth, dict) else '?'} status={status}",
            "http_status": int(status) if status else None,
        }
    try:
        auth_url = _validate_trusted_openai_url(
            str(reauth.get("url") or ""), stage="password_setup",
            code="password_reauth_url_untrusted", message="补设密码重认证返回了非可信地址",
        )
        if _is_playwright_page(driver):
            driver.goto(auth_url, wait_until="domcontentloaded", timeout=30_000)
        else:
            driver.get(auth_url)
    except TwoFASetupError:
        raise
    except Exception as exc:
        return {
            "ok": False, "status": "failed", "stage": "password_reauth",
            "code": "password_reauth_navigation_failed", "message": f"{type(exc).__name__}: {str(exc)[:160]}",
            "http_status": None,
        }

    deadline = time.monotonic() + max(30.0, float(timeout_seconds))
    totp_used = False
    email_used = False
    totp_submitted_at = None
    email_submitted_at = None
    resend_attempted = False
    password_submitted = False
    last_url = ""
    while time.monotonic() < deadline:
        body = _password_body_text(driver)
        last_url = _password_page_url(driver)
        password_fields = _password_visible_inputs(driver, _PASSWORD_INPUT_SELECTOR)
        code_fields = _password_visible_inputs(driver, _PASSWORD_CODE_SELECTOR)
        if code_fields and not password_fields:
            path = last_url.lower()
            is_email_challenge = "email-verification" in path or bool(_PASSWORD_EMAIL_RE.search(body))
            is_authenticator = bool(_PASSWORD_AUTHENTICATOR_RE.search(body))
            if is_authenticator and not is_email_challenge:
                if totp_used:
                    if _PASSWORD_REJECTION_RE.search(body):
                        return {
                            "ok": False, "status": "failed", "stage": "password_totp",
                            "code": "password_totp_reauth_rejected", "message": "TOTP 重认证验证码被拒绝",
                            "http_status": None,
                        }
                    if totp_submitted_at and time.monotonic() - totp_submitted_at >= 8:
                        return {
                            "ok": False, "status": "failed", "stage": "password_totp",
                            "code": "password_totp_reauth_not_advanced",
                            "message": "TOTP 重认证提交后页面未继续",
                            "http_status": None,
                        }
                    time.sleep(0.5)
                    continue
                if not totp_secret:
                    return {
                        "ok": False, "status": "failed", "stage": "password_totp",
                        "code": "password_totp_secret_missing",
                        "message": "重认证要求认证器动态码，但没有可用 TOTP Secret",
                        "http_status": None,
                    }
                if totp_secret:
                    _wait_for_totp_window(min_remaining=5.0)
                    code = pyotp.TOTP(normalize_totp_secret(totp_secret)).now()
                    if not _password_submit_code(driver, code):
                        return {
                            "ok": False, "status": "failed", "stage": "password_totp",
                            "code": "password_totp_reauth_submit_failed", "message": "TOTP 重认证验证码提交失败",
                            "http_status": None,
                        }
                    totp_used = True
                    totp_submitted_at = time.monotonic()
                    time.sleep(1.5)
                    continue
            if is_email_challenge or not is_authenticator:
                if email_used:
                    if _PASSWORD_REJECTION_RE.search(body):
                        return {
                            "ok": False, "status": "failed", "stage": "password_email",
                            "code": "password_email_reauth_rejected", "message": "邮箱重认证验证码被拒绝",
                            "http_status": None,
                        }
                    if email_submitted_at and time.monotonic() - email_submitted_at >= 8:
                        return {
                            "ok": False, "status": "failed", "stage": "password_email",
                            "code": "password_email_reauth_not_advanced",
                            "message": "邮箱重认证验证码提交后页面未继续",
                            "http_status": None,
                        }
                    time.sleep(0.5)
                    continue
                code_after = requested_at
                if not resend_attempted:
                    resend_attempted = True
                    if _password_click_resend(driver):
                        code_after = time.time()
                try:
                    from config import twofa as twofa_cfg
                    code = wait_for_otp(
                        email,
                        after_ts=code_after,
                        max_wait=max(10, int(getattr(twofa_cfg, "TWOFA_OTP_MAX_WAIT", 120) or 120)),
                        poll_interval=max(1, int(getattr(twofa_cfg, "TWOFA_OTP_POLL_INTERVAL", 2) or 2)),
                        settle_seconds=max(0, int(getattr(twofa_cfg, "TWOFA_OTP_SETTLE_SECONDS", 1) or 0)),
                        exclude_codes=otp_history,
                        exclude_message_ids=otp_message_ids,
                    )
                except Exception as exc:
                    return {
                        "ok": False, "status": "failed", "stage": "password_email",
                        "code": "password_email_code_wait_failed", "message": f"{type(exc).__name__}: {str(exc)[:160]}",
                        "http_status": None,
                    }
                if not _password_submit_code(driver, code):
                    return {
                        "ok": False, "status": "failed", "stage": "password_email",
                        "code": "password_email_reauth_submit_failed", "message": "邮箱重认证验证码提交失败",
                        "http_status": None,
                    }
                email_used = True
                email_submitted_at = time.monotonic()
                time.sleep(1.5)
                continue

        if password_fields and not password_submitted:
            if not _password_submit_new_password(driver, password_fields, password):
                return {
                    "ok": False, "status": "failed", "stage": "password_setup",
                    "code": "password_settings_submit_failed", "message": "新密码填写或提交失败",
                    "http_status": None,
                }
            password_submitted = True
            time.sleep(1.5)
            continue

        if password_submitted:
            if _PASSWORD_REJECTION_RE.search(body) and not _PASSWORD_SUCCESS_RE.search(body):
                return {
                    "ok": False, "status": "failed", "stage": "password_setup",
                    "code": "password_settings_rejected", "message": "新密码被服务端拒绝",
                    "http_status": None,
                }
            if _PASSWORD_SUCCESS_RE.search(body) or _password_done_callback(last_url):
                return {
                    "ok": True, "status": "success", "stage": "password_done",
                    "code": "password_setup_success", "message": "密码已补设", "http_status": None,
                    "password": password, "email_reauth_used": email_used,
                    "totp_reauth_used": totp_used,
                }
        time.sleep(0.5)

    _password_screenshot(driver)
    return {
        "ok": False, "status": "failed", "stage": "password_setup",
        "code": "password_settings_timeout", "message": f"补设密码流程超时 url={last_url}",
        "http_status": None,
    }


def _follow_reauth_with_driver(
    session: BrowserSession,
    driver,
    auth_url: str,
    *,
    password: str | None,
    timeout_seconds: float = 45.0,
) -> tuple[str, bool]:
    """协议导航被 Cloudflare 拦截时，改用当前真实浏览器完成重认证导航。

    返回 ``(final_url, completed_in_browser)``。通常浏览器会落到邮箱验证码页，
    此时只同步 Cookie 并继续既有 OTP API；若浏览器已直接完成回调，则返回
    ``completed_in_browser=True``，调用方直接从同步后的会话读取新 Token。
    """
    if driver is None:
        raise TwoFASetupError(
            "totp_reauth",
            "totp_reauth_browser_required",
            "2FA 重认证被风控拦截，且没有可复用的注册浏览器",
        )
    trusted_auth_url = _validate_trusted_openai_url(
        auth_url,
        stage="totp_reauth",
        code="totp_reauth_url_untrusted",
        message="2FA 浏览器重认证返回了非可信地址",
    )
    logger.warning("[2FA] 协议重认证被 HTTP 403 拦截，切换当前注册浏览器继续")
    try:
        if _is_playwright_page(driver):
            driver.goto(trusted_auth_url, wait_until="domcontentloaded", timeout=30_000)
        else:
            driver.get(trusted_auth_url)
    except Exception as exc:
        raise TwoFASetupError(
            "totp_reauth",
            "totp_reauth_browser_navigation_failed",
            "2FA 浏览器重认证页面加载失败",
        ) from exc

    def sync_and_resume_protocol() -> None:
        imported = import_browser_cookies(session, driver, require_auth=True)
        if int(imported or 0) <= 0:
            raise TwoFASetupError(
                "totp_reauth",
                "totp_reauth_browser_cookie_sync_failed",
                "浏览器通过重认证后没有同步到可用 Cookie",
            )
        # BrowserSession 会在协议 403 后熔断 900 秒。只有真实浏览器已经离开
        # challenge 并同步登录 Cookie 后，才恢复同一会话的后续 OTP/enroll 请求。
        try:
            session.blocked_until = 0.0
            session.blocked_reason = ""
        except Exception:
            pass

    deadline = time.monotonic() + max(15.0, float(timeout_seconds or 45.0))
    submitted_password = False
    last_url = ""
    while time.monotonic() < deadline:
        last_url = _password_page_url(driver)
        body = _password_body_text(driver)
        if last_url:
            _validate_trusted_openai_url(
                last_url,
                stage="totp_reauth",
                code="totp_reauth_browser_url_untrusted",
                message="2FA 浏览器重认证落点不可信",
            )
        code_fields = _password_visible_inputs(driver, _PASSWORD_CODE_SELECTOR)
        password_fields = _password_visible_inputs(driver, _PASSWORD_INPUT_SELECTOR)
        parsed = urlparse(last_url) if last_url else None
        host = str(getattr(parsed, "hostname", "") or "").lower()
        path = str(getattr(parsed, "path", "") or "").lower()
        query = parse_qs(str(getattr(parsed, "query", "") or "")) if parsed else {}

        if "email-verification" in path or (
            code_fields and not password_fields and bool(_PASSWORD_EMAIL_RE.search(body))
        ):
            sync_and_resume_protocol()
            logger.info("[2FA] 浏览器已通过风控并到达邮箱验证码页")
            return last_url, False

        if password_fields and not submitted_password:
            confirmed_password = str(password or "").strip()
            if not confirmed_password:
                raise TwoFASetupError(
                    "totp_reauth",
                    "totp_reauth_password_missing",
                    "2FA 浏览器重认证要求密码，但本次注册没有已确认密码",
                )
            if not _password_submit_new_password(driver, password_fields, confirmed_password):
                raise TwoFASetupError(
                    "totp_reauth",
                    "totp_reauth_password_submit_failed",
                    "2FA 浏览器重认证密码提交失败",
                )
            submitted_password = True
            time.sleep(1.0)
            continue

        if submitted_password and _PASSWORD_REJECTION_RE.search(body):
            raise TwoFASetupError(
                "totp_reauth",
                "totp_reauth_password_rejected",
                "2FA 浏览器重认证密码被拒绝",
            )

        completed_callback = bool(
            (host == "chatgpt.com" or host.endswith(".chatgpt.com"))
            and (
                path.startswith("/api/auth/callback/openai")
                or (
                    str((query.get("action") or [""])[0]).lower() == "enable"
                    and str((query.get("factor") or [""])[0]).lower() == "totp"
                )
            )
        )
        if completed_callback and not _BROWSER_CHALLENGE_RE.search(body):
            sync_and_resume_protocol()
            logger.info("[2FA] 浏览器已完成重认证回调并同步登录 Cookie")
            return last_url, True

        time.sleep(0.5 if not _BROWSER_CHALLENGE_RE.search(body) else 1.0)

    raise TwoFASetupError(
        "totp_reauth",
        "totp_reauth_browser_timeout",
        f"2FA 浏览器重认证超时 url={last_url}",
    )


def _validate_2fa_token(session: BrowserSession, access_token: str) -> int:
    """激活后用一个只读 ChatGPT API 验证新 token 仍可用。"""
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    last_exc: Exception | None = None
    last_status: int | None = None
    for attempt in range(1, 4):
        try:
            resp = session.get("https://chatgpt.com/backend-api/models", headers=headers)
            last_status = int(resp.status_code)
            if last_status == 200:
                return last_status
            # 401/403 是确定性鉴权结果；限流和服务端错误才值得短重试。
            if last_status not in {408, 425, 429, 500, 502, 503, 504}:
                break
        except Exception as exc:
            last_exc = exc
        if attempt < 3:
            logger.warning("[2FA] Token 只读校验暂时失败，短暂等待后重试（%s/3）", attempt)
            time.sleep(float(attempt))
    if last_status is not None:
        raise TwoFASetupError(
            "totp_validate",
            "totp_token_validation_failed",
            "2FA 激活后 Token 校验失败",
            http_status=last_status,
        )
    logger.debug("[2FA] Token 只读校验请求异常", exc_info=last_exc)
    raise TwoFASetupError("totp_validate", "totp_token_validation_failed", "2FA 激活后 Token 校验请求失败") from last_exc


def _snapshot_otp_history(email: str, *, timeout: float = 2.0) -> set[str]:
    """在触发重认证前抓取一次旧 OTP，避免取码接口返回缓存验证码。

    只有 generic_api / inbox_mate 能稳定提供轻量历史快照；其它邮箱实现
    继续使用各自的 ``after_ts`` 逻辑，不额外发起请求。快照失败属于可恢复
    情况，由正常 OTP 轮询继续处理。
    """
    try:
        from core.email_provider import resolve_email_source

        source = str(resolve_email_source(email) or "").strip().lower()
    except Exception as exc:
        logger.debug("[2FA] 无法解析邮箱来源，跳过历史 OTP 快照：%s", type(exc).__name__)
        return set()

    snapshot_fn = None
    if source in {"generic_api", "domain_api"}:
        try:
            from core.generic_api_mail_client import snapshot_current_otp

            snapshot_fn = snapshot_current_otp
        except Exception:
            snapshot_fn = None
    elif source == "inbox_mate":
        try:
            from core.inbox_mate_mail_client import snapshot_current_otp

            snapshot_fn = snapshot_current_otp
        except Exception:
            snapshot_fn = None

    if snapshot_fn is None:
        return set()

    try:
        code = str(snapshot_fn(email, timeout=max(1.0, float(timeout or 2.0))) or "").strip()
    except Exception as exc:
        logger.debug(
            "[2FA] 历史 OTP 快照失败，继续正常取码：%s: %s",
            type(exc).__name__,
            redact_otp_text(str(exc)[:160]),
        )
        return set()
    if len(code) == 6 and code.isdigit():
        # OTP 本身不写入日志；只记录是否成功，避免凭据出现在运行日志中。
        logger.info("[2FA] 已记录重认证前的历史 OTP 快照（source=%s）", source)
        return {code}
    return set()


def _snapshot_otp_message_ids(email: str, *, timeout: float = 2.0) -> set[str]:
    """记录触发前稳定邮件卡片 ID，解决同一分钟内 OpenAI 复用 OTP。"""
    try:
        from core.email_provider import resolve_email_source

        source = str(resolve_email_source(email) or "").strip().lower()
    except Exception:
        return set()
    if source not in {"generic_api", "domain_api"}:
        return set()
    try:
        from core.generic_api_mail_client import snapshot_current_message_ids

        message_ids = snapshot_current_message_ids(email, timeout=max(1.0, float(timeout or 2.0)))
    except Exception as exc:
        logger.debug("[2FA] 历史邮件 ID 快照失败，继续正常取码：%s", type(exc).__name__)
        return set()
    result = {str(value) for value in (message_ids or set()) if str(value)}
    if result:
        logger.info("[2FA] 已记录重认证前的历史邮件卡片快照：%s 条", len(result))
    return result


def _setup_2fa_result(
    session: BrowserSession,
    email: str,
    otp_code: str | None = None,
    driver=None,
    existing_password: str | None = None,
    desired_password: str | None = None,
    authenticated_email: str = "",
    access_token: str = "",
) -> TwoFASetupResult:
    """
    完整的 2FA 设置流程。
    会触发再发一份邮箱验证码：
        - USE_EMAIL_SERVICE=True 时自动从 Outlook 账号池拉取
        - 否则需要用户手动输入

    Args:
        session: 已完成注册的会话
        email: 账号邮箱（用作 login_hint）
        otp_code: 邮箱验证码（None 则按上述策略获取）
        driver: 可选的已登录浏览器页面/驱动。
        existing_password: 注册初始密码；已有密码时不会重复调用补设接口。
        desired_password: 本注册任务预先生成的目标密码；补设时复用，避免重试改成另一密码。

    Returns:
        TOTP secret（Base32 字符串），可直接用于 pyotp.TOTP() 生成 6 位动态码
    """
    # 用模块属性读，支持 WebUI 热加载
    from config import email as _email_cfg
    from config import twofa as _twofa_cfg

    logger.info("=" * 60)
    logger.info("开始设置 2FA")
    logger.info("=" * 60)
    _assert_authenticated_account(email, authenticated_email)

    # 阶段一：开启安全设置后必须先确认密码，再进行 MFA 重认证。
    # OTP-only 注册不会出现 create-account/password；旧顺序把补设密码放在
    # TOTP activate 之后，导致 MFA 重认证先失败时密码永远没有执行。
    password_setup: dict | None = None
    configured_password = str(existing_password or "").strip() or None
    try:
        from core.registration_password import registration_password_required
        require_password = registration_password_required()
    except Exception:
        require_password = True
    if not require_password:
        password_setup = {
            "ok": True,
            "status": "skipped",
            "stage": "password_setup",
            "code": "password_setup_disabled",
            "message": "注册密码要求已关闭",
            "http_status": None,
        }
    elif configured_password:
        password_setup = {
            "ok": True,
            "status": "already_configured",
            "stage": "password_setup",
            "code": "password_already_configured",
            "message": "注册密码页已完成，无需重复设置",
            "http_status": None,
        }
        from core.registration_password import persist_confirmed_registration_password
        password_setup["checkpoint_persisted"] = persist_confirmed_registration_password(
            email,
            configured_password,
        )
    else:
        if driver is None:
            raise TwoFASetupError(
                "password_setup",
                "password_driver_required",
                "当前注册驱动没有可用浏览器页面，不能在 MFA 前补设密码",
            )
        try:
            try:
                from core.roxy_registration import _registration_password
            except Exception:
                from core.registration_password import registration_password as _registration_password
            configured_password = str(desired_password or "").strip() or _registration_password()
            logger.info("[2FA] 注册页未设置密码，先执行 post_login_add_password，再启用 MFA")
            password_setup = _setup_password_with_driver(
                driver=driver,
                session=session,
                email=email,
                password=configured_password,
                totp_secret=None,
            )
        except TwoFASetupError:
            configured_password = None
            raise
        except Exception as exc:
            configured_password = None
            raise TwoFASetupError(
                "password_setup",
                "password_setup_failed",
                f"补设密码异常：{type(exc).__name__}: {str(exc)[:160]}",
            ) from exc
        if not isinstance(password_setup, dict) or not bool(password_setup.get("ok")):
            failure = password_setup if isinstance(password_setup, dict) else {}
            configured_password = None
            raise TwoFASetupError(
                str(failure.get("stage") or "password_setup"),
                str(failure.get("code") or "password_setup_failed"),
                str(failure.get("message") or "补设密码未确认成功")[:180],
                http_status=failure.get("http_status"),
            )
        logger.info("[2FA] 账号密码已先行补设成功")
        from core.registration_password import persist_confirmed_registration_password
        password_setup["checkpoint_persisted"] = persist_confirmed_registration_password(
            email,
            configured_password,
        )
        # 浏览器重认证可能刷新 session-token/Cloudflare Cookie；MFA 协议阶段
        # 必须重新同步，而不是继续使用补设密码之前的旧 Cookie 快照。
        import_browser_cookies(session, driver, require_auth=True)

    # 参考项目当前 Roxy 实现已不再为 TOTP 重走 NextAuth CSRF/邮箱 OTP：
    # 直接在已登录浏览器网络栈中调用 enroll/activate，可复用真实浏览器指纹、
    # Cookie 和代理出口，避免独立 BrowserSession 在 /api/auth/csrf 被 CF 403。
    if _driver_supports_authenticated_fetch(driver):
        secret, new_token, browser_expires = _setup_totp_with_driver(
            driver,
            email,
            authenticated_email=authenticated_email,
            access_token=access_token,
        )
        setattr(session, "_twofa_session_expires", browser_expires)
        totp_checkpoint_persisted = _persist_activated_totp_checkpoint(email, secret, new_token)
    else:
        # 无浏览器的兼容入口保留原协议流程。先读取当前验证码快照，随后再触发
        # 重认证邮件，避免把旧缓存误当成本次 OTP。
        historical_otp_codes: set[str] = set()
        historical_message_ids: set[str] = set()
        if otp_code is None and bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", False)):
            historical_otp_codes = _snapshot_otp_history(email, timeout=2.0)
            historical_message_ids = _snapshot_otp_message_ids(email, timeout=2.0)
        reauth_otp_after_ts = time.time()
        auth_url = _trigger_reauth(session, email)
        human_delay("api")
        browser_reauth_completed = False
        try:
            _follow_reauth(session, auth_url)
        except TwoFASetupError as exc:
            if exc.http_status != 403 or driver is None:
                raise
            _, browser_reauth_completed = _follow_reauth_with_driver(
                session,
                driver,
                auth_url,
                password=configured_password,
            )
        human_delay("navigate")

        if not browser_reauth_completed and otp_code is None:
            if _email_cfg.USE_EMAIL_SERVICE:
                from core.email_provider import wait_for_otp
                logger.info("[2FA] 自动等待邮箱重认证 OTP...")
                otp_code = wait_for_otp(
                    email,
                    after_ts=reauth_otp_after_ts,
                    max_wait=int(getattr(_twofa_cfg, "TWOFA_OTP_MAX_WAIT", 120) or 120),
                    poll_interval=int(getattr(_twofa_cfg, "TWOFA_OTP_POLL_INTERVAL", 2) or 2),
                    settle_seconds=max(
                        0,
                        int(
                            getattr(_twofa_cfg, "TWOFA_OTP_SETTLE_SECONDS", 1)
                            if getattr(_twofa_cfg, "TWOFA_OTP_SETTLE_SECONDS", None) is not None
                            else 1
                        ),
                    ),
                    request_timeout=float(getattr(_twofa_cfg, "TWOFA_GENERIC_API_REQUEST_TIMEOUT", 12) or 12),
                    retry_timeout=float(getattr(_twofa_cfg, "TWOFA_GENERIC_API_RETRY_TIMEOUT", 8) or 8),
                    max_consecutive_errors=int(getattr(_twofa_cfg, "TWOFA_GENERIC_API_MAX_CONSECUTIVE_ERRORS", 2) or 2),
                    exclude_codes=historical_otp_codes,
                    exclude_message_ids=historical_message_ids,
                )
            else:
                logger.info("")
                logger.info("[2FA] 请检查邮箱，输入新收到的 6 位验证码")
                otp_code = input(">>> 2FA 验证码: ").strip()

        if browser_reauth_completed:
            try:
                new_session = fetch_session(session)
                new_token = str(new_session.get("accessToken") or "").strip()
                setattr(session, "_twofa_session_expires", str(new_session.get("expires") or "").strip() or None)
            except Exception as exc:
                raise TwoFASetupError(
                    "totp_session",
                    "totp_session_refresh_failed",
                    "浏览器重认证完成后未取得新的 Session Token",
                ) from exc
            if not new_token:
                raise TwoFASetupError(
                    "totp_session",
                    "totp_session_refresh_failed",
                    "浏览器重认证完成后 Session Token 为空",
                )
            logger.info("[2FA] 已从浏览器重认证会话取得新 Token")
        else:
            human_delay("otp_input")
            continue_url = _validate_reauth_otp(session, otp_code)
            human_delay("api")
            new_token = _exchange_new_token(session, continue_url)
            human_delay("api")

        secret, session_id = _enroll_totp(session, new_token)
        human_delay("form")
        _wait_for_totp_window()
        if _activate_totp(session, new_token, secret, session_id) is not True:
            raise TwoFASetupError("totp_activate", "totp_activate_failed", "2FA TOTP 激活未确认成功")
        totp_checkpoint_persisted = _persist_activated_totp_checkpoint(email, secret, new_token)

    # 激活成功后 secret 必须保留。models 只是只读健康检查，可能因 401/429
    # 或瞬时网络错误失败；把失败编码到结果中而不是抛出并让调用方丢 secret。
    validation_status: int | None = None
    validation_ok = False
    validation_code: str | None = None
    validation_message: str | None = None
    try:
        validation_status = _validate_2fa_token(session, new_token)
        validation_ok = True
        logger.info("[2FA] 设置完成并通过 Token 校验")
    except TwoFASetupError as exc:
        validation_status = exc.http_status
        validation_code = exc.code
        validation_message = redact_otp_text(str(exc)[:240])
        logger.warning(
            "[2FA] TOTP 已激活，但 Token 校验未通过；保留 Secret 供落库：code=%s http=%s",
            exc.code,
            exc.http_status or "-",
        )
    except Exception as exc:
        validation_code = "totp_token_validation_failed"
        validation_message = redact_otp_text(str(exc)[:240])
        logger.warning("[2FA] TOTP 已激活，但 Token 校验异常；保留 Secret：%s", type(exc).__name__)

    return TwoFASetupResult(
        secret=secret,
        access_token=new_token,
        activated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        validation_status=validation_status,
        validation_ok=validation_ok,
        validation_code=validation_code,
        validation_message=validation_message,
        expires=getattr(session, "_twofa_session_expires", None),
        password=configured_password,
        password_setup=password_setup,
        totp_checkpoint_persisted=totp_checkpoint_persisted,
    )


def setup_2fa_result(
    session: BrowserSession,
    email: str,
    otp_code: str | None = None,
    driver=None,
    existing_password: str | None = None,
    desired_password: str | None = None,
    authenticated_email: str = "",
    access_token: str = "",
) -> TwoFASetupResult:
    """执行完整 2FA 流程并返回可落库的 Secret 与刷新后 Token。"""
    try:
        result = _setup_2fa_result(
            session,
            email,
            otp_code=otp_code,
            driver=driver,
            existing_password=existing_password,
            desired_password=desired_password,
            authenticated_email=authenticated_email,
            access_token=access_token,
        )
        try:
            setup_status = result.password_setup or {}
            if setup_status and not bool(setup_status.get("ok")):
                setattr(session, "_twofa_last_error", {
                    "stage": setup_status.get("stage") or "password_setup",
                    "code": setup_status.get("code") or "password_setup_failed",
                    "http_status": setup_status.get("http_status"),
                    "message": str(setup_status.get("message") or "密码设置未完成")[:240],
                })
            elif not bool(result.validation_ok):
                setattr(session, "_twofa_last_error", {
                    "stage": "totp_validate",
                    "code": result.validation_code or "totp_token_validation_failed",
                    "http_status": result.validation_status,
                    "message": str(result.validation_message or "TOTP Token 校验未通过")[:240],
                })
            else:
                setattr(session, "_twofa_last_error", None)
        except Exception:
            pass
        return result
    except TwoFASetupError as exc:
        # 直接调用 setup_2fa_result 的主流程也能读取结构化失败原因；
        # maybe_setup_2fa_result 会再次记录但不会覆盖阶段/错误码。
        _set_twofa_error(session, exc)
        raise
    except Exception as exc:
        wrapped = TwoFASetupError("totp_setup", "totp_setup_failed", "2FA 设置失败")
        _set_twofa_error(session, wrapped)
        raise wrapped from exc


def setup_2fa(
    session: BrowserSession,
    email: str,
    otp_code: str | None = None,
    driver=None,
    existing_password: str | None = None,
    desired_password: str | None = None,
    authenticated_email: str = "",
    access_token: str = "",
) -> str:
    """兼容旧调用方：执行完整流程并只返回规范化 Secret。"""
    return setup_2fa_result(
        session,
        email,
        otp_code=otp_code,
        driver=driver,
        existing_password=existing_password,
        desired_password=desired_password,
        authenticated_email=authenticated_email,
        access_token=access_token,
    ).secret


_AUTH_COOKIE_NAME_MARKERS = (
    "__secure-next-auth.session-token",
    "__host-next-auth.session-token",
    "next-auth.session-token",
    "session-token",
)


def _session_has_auth_cookie(session: BrowserSession) -> bool:
    """判断 HTTP 会话是否已拿到 ChatGPT NextAuth 登录 Cookie。"""
    try:
        for cookie in session.session.cookies.jar:
            name = str(getattr(cookie, "name", "") or "").strip().lower()
            if any(marker in name for marker in _AUTH_COOKIE_NAME_MARKERS):
                value = str(getattr(cookie, "value", "") or "").strip()
                if value:
                    return True
    except Exception:
        return False
    return False


def _set_twofa_error(session: BrowserSession, exc: Exception) -> dict[str, object]:
    """把最近一次 2FA 失败写入会话，供注册调用方落到 extra。"""
    if isinstance(exc, TwoFASetupError):
        info: dict[str, object] = {
            "stage": exc.stage,
            "code": exc.code,
            "http_status": exc.http_status,
            "message": redact_otp_text(str(exc)[:240]),
        }
    else:
        info = {
            "stage": "totp_setup",
            "code": "totp_setup_failed",
            "http_status": None,
            "message": redact_otp_text(str(exc)[:240]),
        }
    try:
        setattr(session, "_twofa_last_error", info)
    except Exception:
        pass
    return info


def import_browser_cookies(session: BrowserSession, driver, *, require_auth: bool = False) -> int:
    """把 Selenium/Playwright 当前登录态 Cookie 导入同代理 HTTP 会话。

    ``require_auth=True`` 用于 2FA 辅助会话：没有可验证的 NextAuth
    session-token 时立即返回结构化错误，不再进入最长 120 秒的 OTP 轮询。
    默认仍保持旧行为（导入失败返回 0），兼容其它调用方/测试。
    """
    if driver is None:
        if require_auth and not _session_has_auth_cookie(session):
            raise TwoFASetupError(
                "cookie_import",
                "cookie_auth_missing",
                "当前浏览器没有可用于 2FA 重认证的登录 Cookie",
            )
        return 0
    cookies: list[dict] = []
    try:
        if hasattr(driver, "get_cookies"):
            cookies = driver.get_cookies() or []
        if not cookies and hasattr(driver, "context"):
            context = driver.context
            if hasattr(context, "cookies"):
                cookies = context.cookies() or []
    except Exception as exc:
        logger.warning("[2FA] 导入浏览器 Cookie 失败：%s", type(exc).__name__)
        if require_auth:
            raise TwoFASetupError(
                "cookie_import",
                "cookie_import_failed",
                "读取浏览器登录 Cookie 失败",
            ) from exc
        return 0
    imported = 0
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "").strip()
        path = str(cookie.get("path") or "/")
        if not name or not domain:
            continue
        try:
            session.session.cookies.set(name, value, domain=domain, path=path)
            imported += 1
        except Exception:
            continue
    try:
        session._sync_device_id_from_cookie()
    except Exception:
        pass
    if imported:
        logger.info("[2FA] 已导入 %s 个浏览器 Cookie", imported)
    if require_auth and not _session_has_auth_cookie(session):
        raise TwoFASetupError(
            "cookie_import",
            "cookie_auth_missing",
            f"已读取 {imported} 个 Cookie，但没有发现 ChatGPT 登录 session-token",
        )
    return imported


def maybe_setup_2fa_result(
    session: BrowserSession,
    email: str,
    driver=None,
    existing_password: str | None = None,
    desired_password: str | None = None,
    authenticated_email: str = "",
    access_token: str = "",
) -> TwoFASetupResult | None:
    """按开关执行 2FA；失败只降级为 None，保留已注册账号。"""
    try:
        try:
            setattr(session, "_twofa_last_error", None)
        except Exception:
            pass
        from config import twofa as _twofa_cfg
        from config import email as _email_cfg
        if not bool(getattr(_twofa_cfg, "ENABLE_2FA", False)):
            logger.info("[2FA] ENABLE_2FA=False，跳过")
            return None
        if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", False)):
            logger.warning("[2FA] USE_EMAIL_SERVICE=False，无法自动收取重认证 OTP，跳过")
            return None
        import_browser_cookies(session, driver, require_auth=True)
        return setup_2fa_result(
            session,
            email,
            driver=driver,
            existing_password=existing_password,
            desired_password=desired_password,
            authenticated_email=authenticated_email,
            access_token=access_token,
        )
    except TwoFASetupError as exc:
        _set_twofa_error(session, exc)
        logger.warning("[2FA] 设置失败 stage=%s code=%s http=%s（账号保留）", exc.stage, exc.code, exc.http_status or "-")
        return None
    except Exception as exc:
        _set_twofa_error(session, exc)
        logger.warning("[2FA] 设置失败 type=%s（账号保留）", type(exc).__name__)
        return None


def maybe_setup_2fa(
    session: BrowserSession,
    email: str,
    driver=None,
    existing_password: str | None = None,
    desired_password: str | None = None,
    authenticated_email: str = "",
    access_token: str = "",
) -> str | None:
    """兼容旧调用方：返回 Secret 或 None。"""
    result = maybe_setup_2fa_result(
        session,
        email,
        driver=driver,
        existing_password=existing_password,
        desired_password=desired_password,
        authenticated_email=authenticated_email,
        access_token=access_token,
    )
    return result.secret if result else None


def save_account_data(
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    extra: dict | None = None,
    output_path: Path | None = None,  # 兼容老接口，已废弃
    email_source: str | None = None,
    proxy_used: str | None = None,
    batch_dir: Path | None = None,
    registration_name: str | None = None,
    birth_date: str | None = None,
    registration_exit_ip: str | None = None,
    registration_exit_country: str | None = None,
    openai_created_at: str | int | float | None = None,
) -> int:
    """
    将账号信息保存到本地 JSON/TXT 文件存储。
    返回新插入/更新的 row id。
    """
    from core import db

    extra = dict(extra or {})
    user = extra.get("user") or {}
    account = extra.get("account") or {}
    created_value = openai_created_at if openai_created_at is not None else account.get("createdTime")
    if created_value is not None:
        try:
            if isinstance(created_value, (int, float)) or str(created_value).strip().replace(".", "", 1).isdigit():
                created_value = datetime.fromtimestamp(float(created_value), tz=timezone.utc)
            else:
                created_value = datetime.fromisoformat(str(created_value).strip().replace("Z", "+00:00"))
                if created_value.tzinfo is None:
                    created_value = created_value.replace(tzinfo=timezone.utc)
                created_value = created_value.astimezone(timezone.utc)
            openai_created_at = created_value.isoformat(timespec="seconds").replace("+00:00", "Z")
        except (OverflowError, TypeError, ValueError):
            openai_created_at = str(created_value).strip() or None
    # 从 extra.codex 抽出顶层 codex 状态/错误，方便 WebUI 直接读账号字段
    codex = extra.get("codex") or {}
    codex_status = codex.get("status")  # success / failed / skipped
    codex_error = None
    if codex_status == "failed":
        codex_error = codex.get("message")

    row_id = db.insert_account(
        email=email,
        access_token=access_token,
        totp_secret=totp_secret,
        user_id=user.get("id"),
        user_name=user.get("name") or registration_name,
        registration_name=registration_name,
        birth_date=birth_date,
        registration_exit_ip=registration_exit_ip,
        registration_exit_country=registration_exit_country,
        openai_created_at=openai_created_at,
        plan_type=account.get("planType"),
        expires_at=extra.get("expires"),
        device_id=extra.get("device_id"),
        proxy_used=proxy_used,
        email_source=email_source,
        extra=extra,
        codex_status=codex_status,
        codex_error=codex_error,
    )
    # checkpoint 的 merge + consume 在 db.insert_account 的同一临界区完成。
    # 归档和后续任务必须读取 durable row，不能复用锁外的旧快照。
    durable_row = db.get_account(row_id) or {}
    durable_access_token = str(durable_row.get("access_token") or access_token or "").strip()
    durable_totp_secret = str(durable_row.get("totp_secret") or totp_secret or "").strip() or None
    durable_extra = dict(extra)
    raw_extra = durable_row.get("extra_json")
    if isinstance(raw_extra, dict):
        durable_extra = dict(raw_extra)
    elif isinstance(raw_extra, str) and raw_extra.strip():
        try:
            decoded_extra = json.loads(raw_extra)
            if isinstance(decoded_extra, dict):
                durable_extra = decoded_extra
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("[Save] durable extra_json 解析失败，批次归档使用本次终态：id=%s", row_id)
    batch_folder = _append_batch_archive(
        row_id=row_id,
        email=email,
        access_token=durable_access_token,
        totp_secret=durable_totp_secret,
        email_source=email_source,
        proxy_used=proxy_used,
        extra=durable_extra,
        batch_dir=batch_dir,
    )
    logger.info(f"[Save] 账号已写入 DB, id={row_id}, email={email}")
    logger.info(f"[Save] 批次归档目录: {batch_folder}")
    if str(email_source or "").strip().lower() in {"domain_api", "inbox_mate"}:
        try:
            from core import db

            domain = email.rsplit("@", 1)[-1].strip().lower()
            group = db.ensure_account_group(f"域名邮箱 · {domain}")
            db.add_accounts_to_group(str(group["id"]), [row_id])
            logger.info(f"[Save] 域名 API 账号已加入分组 {group['name']}: id={row_id}, email={email}")
        except Exception as exc:
            logger.warning(
                f"[Save] 域名 API 账号加入域名邮箱分组失败（不影响注册结果）: "
                f"{email}, {type(exc).__name__}: {str(exc)[:180]}"
            )
    # session 中的 account.planType 不能说明 Plus 试用资格。账号落库后只负责
    # 入队，由专用线程池异步查询并回写，避免占用注册工作线程。
    try:
        from core.plan_check_service import enqueue_account_plan_check

        queued = enqueue_account_plan_check(
            account_id=row_id,
            email=email,
            access_token=durable_access_token,
            trigger="registration_auto",
        )
        if queued.get("accepted"):
            logger.info(f"[Plan] 注册后自动查询已入队: id={row_id}, email={email}")
        elif queued.get("busy"):
            logger.info(f"[Plan] 账号已有套餐查询，注册流程不重复入队: id={row_id}, email={email}")
        else:
            logger.warning(f"[Plan] 注册后自动查询入队失败（不影响注册结果）: {email}, {queued.get('error')}")
    except Exception as exc:
        logger.warning(
            f"[Plan] 注册后自动查询入队异常（不影响注册结果）: "
            f"{email}, {type(exc).__name__}: {str(exc)[:180]}"
        )
    return row_id
