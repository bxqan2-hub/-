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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
from urllib.parse import urlencode, urlparse

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
    try:
        csrf_resp = session.get(csrf_url, headers=csrf_headers)
        csrf_resp.raise_for_status()
        csrf_token = str((csrf_resp.json() or {}).get("csrfToken") or "").strip()
    except Exception as exc:
        raise TwoFASetupError("totp_reauth", "totp_reauth_request_failed", "2FA 重认证 CSRF 请求失败") from exc
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
    try:
        resp = session.post(signin_url, headers=headers, data=body)
        resp.raise_for_status()
        auth_url = str((resp.json() or {}).get("url") or "").strip()
    except Exception as exc:
        raise TwoFASetupError("totp_reauth", "totp_reauth_start_failed", "2FA 重认证启动失败") from exc
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


def _wait_for_totp_window(min_remaining: float = 4.0) -> None:
    remaining = 30.0 - (time.time() % 30.0)
    if remaining < float(min_remaining):
        time.sleep(remaining + 0.25)


def _validate_2fa_token(session: BrowserSession, access_token: str) -> int:
    """激活后用一个只读 ChatGPT API 验证新 token 仍可用。"""
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    try:
        resp = session.get("https://chatgpt.com/backend-api/models", headers=headers)
    except Exception as exc:
        raise TwoFASetupError("totp_validate", "totp_token_validation_failed", "2FA 激活后 Token 校验请求失败") from exc
    if resp.status_code != 200:
        raise TwoFASetupError("totp_validate", "totp_token_validation_failed", "2FA 激活后 Token 校验失败", http_status=resp.status_code)
    return int(resp.status_code)


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


def _setup_2fa_result(session: BrowserSession, email: str, otp_code: str | None = None) -> TwoFASetupResult:
    """
    完整的 2FA 设置流程。
    会触发再发一份邮箱验证码：
        - USE_EMAIL_SERVICE=True 时自动从 Outlook 账号池拉取
        - 否则需要用户手动输入

    Args:
        session: 已完成注册的会话
        email: 账号邮箱（用作 login_hint）
        otp_code: 邮箱验证码（None 则按上述策略获取）

    Returns:
        TOTP secret（Base32 字符串），可直接用于 pyotp.TOTP() 生成 6 位动态码
    """
    # 用模块属性读，支持 WebUI 热加载
    from config import email as _email_cfg
    from config import twofa as _twofa_cfg

    logger.info("=" * 60)
    logger.info("开始设置 2FA")
    logger.info("=" * 60)

    # 阶段一：重认证。先读取一次 generic/inbox_mate 当前缓存的验证码，
    # 再记录时间边界并触发新邮件，避免把旧缓存当成本次 OTP。
    historical_otp_codes: set[str] = set()
    if otp_code is None and bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", False)):
        historical_otp_codes = _snapshot_otp_history(email, timeout=2.0)
    reauth_otp_after_ts = time.time()
    auth_url = _trigger_reauth(session, email)
    human_delay("api")
    _follow_reauth(session, auth_url)
    human_delay("navigate")

    if otp_code is None:
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
            )
        else:
            logger.info("")
            logger.info("[2FA] 请检查邮箱，输入新收到的 6 位验证码")
            otp_code = input(">>> 2FA 验证码: ").strip()

    human_delay("otp_input")
    continue_url = _validate_reauth_otp(session, otp_code)
    human_delay("api")
    new_token = _exchange_new_token(session, continue_url)
    human_delay("api")

    # 阶段二：enroll + activate
    secret, session_id = _enroll_totp(session, new_token)
    human_delay("form")
    _wait_for_totp_window()
    if _activate_totp(session, new_token, secret, session_id) is not True:
        raise TwoFASetupError("totp_activate", "totp_activate_failed", "2FA TOTP 激活未确认成功")

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
    )


def setup_2fa_result(session: BrowserSession, email: str, otp_code: str | None = None) -> TwoFASetupResult:
    """执行完整 2FA 流程并返回可落库的 Secret 与刷新后 Token。"""
    try:
        result = _setup_2fa_result(session, email, otp_code=otp_code)
        try:
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


def setup_2fa(session: BrowserSession, email: str, otp_code: str | None = None) -> str:
    """兼容旧调用方：执行完整流程并只返回规范化 Secret。"""
    return setup_2fa_result(session, email, otp_code=otp_code).secret


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


def maybe_setup_2fa_result(session: BrowserSession, email: str, driver=None) -> TwoFASetupResult | None:
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
        return setup_2fa_result(session, email)
    except TwoFASetupError as exc:
        _set_twofa_error(session, exc)
        logger.warning("[2FA] 设置失败 stage=%s code=%s http=%s（账号保留）", exc.stage, exc.code, exc.http_status or "-")
        return None
    except Exception as exc:
        _set_twofa_error(session, exc)
        logger.warning("[2FA] 设置失败 type=%s（账号保留）", type(exc).__name__)
        return None


def maybe_setup_2fa(session: BrowserSession, email: str, driver=None) -> str | None:
    """兼容旧调用方：返回 Secret 或 None。"""
    result = maybe_setup_2fa_result(session, email, driver=driver)
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
    from core.db import insert_account
    extra = extra or {}
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

    row_id = insert_account(
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
    batch_folder = _append_batch_archive(
        row_id=row_id,
        email=email,
        access_token=access_token,
        totp_secret=totp_secret,
        email_source=email_source,
        proxy_used=proxy_used,
        extra=extra,
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
            access_token=access_token,
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
