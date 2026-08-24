"""Direct runtime bridge to the pinned GPT-utral-platform registration engine."""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_ROOT = PROJECT_ROOT / "vendor" / "GPT-utral-platform"
VENDOR_ROOT = UPSTREAM_ROOT / "vendor" / "turb_gpt_free_register"
BRIDGE_RUNNER = VENDOR_ROOT / "bridge_runner.py"
EXPECTED_PLATFORM_COMMIT = "68a1f8faede7e41f10ac5f9af267465fa61d0e3d"
EXPECTED_SOURCE_COMMIT = "48aefea978136c3bbcf75ec20cb07ae95932ce80"
RUNTIME_ROOT = PROJECT_ROOT / "data" / "legacy" / "turb-gpt-free-register"
SITE_CUSTOMIZE_ROOT = PROJECT_ROOT / "upstream_runtime"


def _windows_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)) | int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    )


def verify_checkout() -> dict[str, str]:
    if not BRIDGE_RUNNER.is_file():
        raise RuntimeError(f"上游注册 Bridge 不存在：{BRIDGE_RUNNER}")
    source_commit_file = VENDOR_ROOT / "SOURCE_COMMIT"
    source_commit = source_commit_file.read_text(encoding="utf-8-sig").strip()
    if source_commit != EXPECTED_SOURCE_COMMIT:
        raise RuntimeError(
            f"上游注册核心提交不匹配：expected={EXPECTED_SOURCE_COMMIT} actual={source_commit}"
        )
    return {
        "platform_commit": EXPECTED_PLATFORM_COMMIT,
        "source_commit": source_commit,
        "bridge": str(BRIDGE_RUNNER),
    }


def _sync_runtime_materials() -> None:
    """Expose current mailbox/config material to the upstream worker's runtime tree."""
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    for name in (
        ".env",
        "用于注册的邮箱.json",
        "用于注册的邮箱.txt",
        "用于注册的API邮箱.json",
        "用于注册的API邮箱.txt",
        "用于注册的域名邮箱.json",
    ):
        source = PROJECT_ROOT / name
        if not source.is_file():
            continue
        target = RUNTIME_ROOT / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)


def _bridge_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["APP_DATA_DIR"] = str(PROJECT_ROOT / "data")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    existing = str(env.get("PYTHONPATH") or "").strip()
    env["PYTHONPATH"] = str(SITE_CUSTOMIZE_ROOT) + (os.pathsep + existing if existing else "")
    return env


def run_bridge(
    operation: str,
    *,
    run_item_id: int,
    input_data: dict[str, Any],
    config: dict[str, Any],
    on_event: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    timeout: float = 1800,
) -> dict[str, Any]:
    verify_checkout()
    request_id = uuid.uuid4().hex
    request = {
        "protocol_version": 1,
        "request_id": request_id,
        "operation": operation,
        "run_item_id": int(run_item_id),
        "input": dict(input_data),
        "config": dict(config),
    }
    process = subprocess.Popen(
        [sys.executable, "-u", str(BRIDGE_RUNNER)],
        cwd=str(VENDOR_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_bridge_environment(),
        creationflags=_windows_creation_flags(),
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    process.stdin.close()

    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def drain(kind: str, stream) -> None:
        for line in stream:
            output_queue.put((kind, line))
        output_queue.put((kind, None))

    assert process.stdout is not None and process.stderr is not None
    threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True).start()
    threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True).start()
    started = time.monotonic()
    streams_done: set[str] = set()
    result: dict[str, Any] | None = None
    errors: list[str] = []
    try:
        while len(streams_done) < 2 or process.poll() is None:
            if should_cancel and should_cancel():
                process.terminate()
                raise RuntimeError("上游注册任务已停止")
            if time.monotonic() - started > max(1.0, float(timeout)):
                process.terminate()
                raise TimeoutError(f"上游注册 Bridge 超时：{operation}")
            try:
                kind, line = output_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                streams_done.add(kind)
                continue
            if kind == "stderr":
                errors.append(line.rstrip())
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.info("[UpstreamBridge] %s", line.rstrip())
                continue
            if str(event.get("request_id") or request_id) != request_id:
                continue
            if on_event:
                on_event(event)
            if event.get("type") == "result":
                result = dict(event.get("result") or {})
            elif event.get("type") == "error":
                errors.append(str(event.get("message") or event.get("error") or ""))
        code = process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
    if code == 0 and result is not None:
        return result
    raise RuntimeError((errors[-1] if errors else f"上游注册 Bridge 退出码 {code}")[:1000])


def _registration_config() -> dict[str, Any]:
    from config import email as email_config
    from config import proxy as proxy_config
    from config import roxybrowser as roxy_config
    from config import twofa as twofa_config

    config: dict[str, Any] = {
        "registration_driver": "roxy",
        "enable_2fa": bool(getattr(twofa_config, "ENABLE_2FA", False)),
        "legacy_use_email_service": bool(getattr(email_config, "USE_EMAIL_SERVICE", True)),
        "legacy_email_source": str(getattr(email_config, "EMAIL_SOURCE", "") or ""),
        "registration_proxy_pool": "\n".join(str(item) for item in (getattr(proxy_config, "PROXY_POOL", []) or [])),
        "low_traffic": bool(getattr(roxy_config, "ROXY_LOW_TRAFFIC", True)),
        "static_cache": bool(getattr(roxy_config, "ROXY_STATIC_CACHE", True)),
        "headless": bool(getattr(roxy_config, "ROXY_OPEN_HEADLESS", False)),
        "roxy_profile_create_payload": dict(getattr(roxy_config, "ROXY_PROFILE_CREATE_PAYLOAD", {}) or {}),
    }
    for source, target in {
        "ROXY_API_BASE": "roxy_api_base",
        "ROXY_API_TOKEN": "roxy_api_token",
        "ROXY_CHROMEDRIVER_PATH": "roxy_chromedriver_path",
        "ROXY_PROFILE_ID": "roxy_profile_id",
        "ROXY_WORKSPACE_ID": "roxy_workspace_id",
        "ROXY_PROJECT_ID": "roxy_project_id",
        "ROXY_DEFAULT_OS": "roxy_default_os",
        "ROXY_DEFAULT_OS_VERSION": "roxy_default_os_version",
        "ROXY_PROXY_CHECK_CHANNEL": "roxy_proxy_check_channel",
        "ROXY_PROXY_EXIT_CHECK_URL": "roxy_proxy_exit_check_url",
        "ROXY_ONE_PROFILE_PER_ACCOUNT": "roxy_one_profile_per_account",
        "ROXY_DELETE_PROFILE_AFTER_RUN": "roxy_delete_profile_after_run",
        "ROXY_KEEP_BROWSER_OPEN": "roxy_keep_browser_open",
        "ROXY_CREATE_USE_PROXY_POOL": "roxy_create_use_proxy_pool",
        "ROXY_SELENIUM_TIMEOUT": "roxy_selenium_timeout",
        "ROXY_API_RETRIES": "roxy_api_retries",
        "ROXY_CHALLENGE_WAIT_SECONDS": "roxy_challenge_wait_seconds",
        "ROXY_CHALLENGE_MAX_PROFILE_ATTEMPTS": "roxy_challenge_max_profile_attempts",
        "ROXY_PROXY_PREFLIGHT_TIMEOUT": "roxy_proxy_preflight_timeout",
    }.items():
        value = getattr(roxy_config, source, None)
        if value not in (None, ""):
            config[target] = value
    for source, target in {
        "GPTMAIL_API_KEY": "legacy_gptmail_api_key",
        "MAIL_NEST_API_KEY": "legacy_mailnest_api_key",
        "MAIL_NEST_PROJECT_CODE": "legacy_mailnest_project_code",
        "CLOUDMAIL_API_BASE": "legacy_cloudmail_api_base",
        "CLOUDMAIL_ADMIN_EMAIL": "legacy_cloudmail_admin_email",
        "CLOUDMAIL_PASSWORD": "legacy_cloudmail_password",
        "CLOUDMAIL_AUTH_TOKEN": "legacy_cloudmail_auth_token",
        "EMAIL_DOMAIN": "legacy_email_domain",
        "QQ_EMAIL": "legacy_qq_email",
        "QQ_IMAP_PASSWORD": "legacy_qq_imap_password",
    }.items():
        value = getattr(email_config, source, None)
        if value not in (None, ""):
            config[target] = value
    return config


def run_roxy_registration(
    *,
    email: str,
    name: str,
    birthday: str,
    proxy: str | None = None,
    batch_dir: Path | None = None,
) -> dict[str, Any]:
    from core.account_export import save_account_data
    from core.email_provider import resolve_email_source
    from core.registration_service import bind_roxy_profile, check_stop_requested, current_job_id

    _sync_runtime_materials()

    def handle_event(event: dict[str, Any]) -> None:
        message = str(event.get("message") or "").strip()
        if message:
            level = logging.WARNING if event.get("level") == "warning" else logging.INFO
            logger.log(level, "[UpstreamBridge][%s] %s", event.get("stage") or event.get("type"), message)

    response = run_bridge(
        "register.roxy",
        run_item_id=int(current_job_id() or 0),
        input_data={"email": email, "name": name, "birthday": birthday, "proxy": proxy or ""},
        config=_registration_config(),
        on_event=handle_event,
        should_cancel=lambda: _stop_requested(check_stop_requested),
    )
    registration = dict(response.get("registration") or {})
    access_token = str(registration.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("上游注册完成但未返回 access_token")
    extra = dict(registration.get("extra") or {})
    profile_id = str((extra.get("roxybrowser") or {}).get("profile_id") or "").strip()
    account_id = save_account_data(
        email=str(registration.get("email") or email),
        access_token=access_token,
        totp_secret=str(registration.get("totp_secret") or "").strip() or None,
        extra=extra,
        email_source=resolve_email_source(email),
        proxy_used=str(extra.get("proxy_used") or proxy or "").strip() or None,
        batch_dir=batch_dir,
        registration_name=name,
        birth_date=birthday,
        registration_exit_ip=str(registration.get("registration_exit_ip") or "").strip() or None,
        registration_exit_country=str(registration.get("registration_exit_country") or "").strip() or None,
    )
    if profile_id and bool(_registration_config().get("roxy_keep_browser_open")):
        bind_roxy_profile(profile_id)
    return {**registration, "success": True, "account_id": account_id, "roxy_profile_id": profile_id or None}


def _stop_requested(checker: Callable[[], None]) -> bool:
    try:
        checker()
        return False
    except Exception:
        return True
