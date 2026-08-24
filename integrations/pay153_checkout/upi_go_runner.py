from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
DEFAULT_BINARY = ROOT / "tools" / "upi_go" / "pix_extract_slot"


def binary_path() -> Path:
    configured = str(os.getenv("PAY153_UPI_GO_BINARY") or "").strip()
    return Path(configured) if configured else DEFAULT_BINARY


def available() -> bool:
    path = binary_path()
    return path.is_file() and os.access(path, os.X_OK)


def _billing_payload(billing: dict[str, Any]) -> dict[str, Any]:
    address = billing.get("address") or {}
    return {
        "name": str(billing.get("name") or "").strip(),
        "email": str(billing.get("email") or "").strip(),
        "tax_id": "",
        "line1": str(address.get("line1") or "").strip(),
        "line2": str(address.get("line2") or "").strip(),
        "city": str(address.get("city") or "").strip(),
        "state": str(address.get("state") or "").strip(),
        "postal_code": str(address.get("postal_code") or "").strip(),
        "country": "IN",
    }


def _last_json_line(stdout: str) -> dict[str, Any]:
    for line in reversed(str(stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("UPI Go 未返回有效 JSON")


def _error_text(result: dict[str, Any], stderr: str) -> str:
    for key in (
        "error",
        "lastSetupErrorMessage",
        "terminalFailureCode",
        "declineCode",
        "checkoutDeclineCode",
        "paymentErrorCode",
    ):
        value = str(result.get(key) or "").strip()
        if value:
            return value
    tail = str(stderr or "").strip().splitlines()
    return tail[-1][:800] if tail else "UPI Go 提取未命中"


def run_upi(
    *,
    token: str,
    proxy: str,
    billing: dict[str, Any],
    promotion_country: str = "VN",
    timeout_seconds: int = 45,
    cancelled: Callable[[], bool] | None = None,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    binary = binary_path()
    if not available():
        raise RuntimeError(f"UPI Go 二进制未就绪：{binary}")

    env = os.environ.copy()
    env.update(
        {
            "PIX_CHANNEL": "upi",
            "UPI_SLOT": "1",
            "UPI_PROMOTION_COUNTRY": str(promotion_country or "VN").upper(),
            "PIX_BILLING_JSON": json.dumps(_billing_payload(billing), ensure_ascii=False),
            # Keep credentials out of the process command line.
            "PIX_SMOKE_TOKEN": str(token or "").strip(),
            "UPI_DEFAULT_PROXY": str(proxy or "").strip(),
            # The Flask job wrapper owns full Checkout retries. Go keeps only
            # one effective attempt, while allowing a few transport/SID swaps.
            "UPI_MAX_RETRY": str(os.getenv("PAY153_UPI_GO_EFFECTIVE_RETRIES", "1")),
            "UPI_ATTEMPT_MAX": str(os.getenv("PAY153_UPI_GO_EFFECTIVE_RETRIES", "1")),
            "UPI_OUTER_LOOP_MAX": str(os.getenv("PAY153_UPI_GO_OUTER_LOOP_MAX", "3")),
            "UPI_INTERNAL_SID_RETRY_MAX": str(os.getenv("PAY153_UPI_GO_SID_RETRIES", "3")),
            "UPI_APPROVE_RETRY_MAX": str(os.getenv("PAY153_UPI_GO_APPROVE_RETRIES", "3")),
            "UPI_MAX_APPROVE_BLOCKED": str(os.getenv("PAY153_UPI_GO_BLOCKED_RETRIES", "1")),
            "UPI_POLL_TIMEOUT": str(os.getenv("PAY153_UPI_GO_POLL_TIMEOUT", "8")),
            "UPI_POLL_MAX_QUERIES": str(os.getenv("PAY153_UPI_GO_POLL_QUERIES", "8")),
            "UPI_FULLPAGE_FALLBACK_ENABLED": str(os.getenv("PAY153_UPI_GO_FULLPAGE", "1")),
        }
    )
    command = [
        str(binary),
        "-slot",
        "-full",
        "-timeout",
        str(max(15, min(120, int(timeout_seconds)))),
    ]
    log("[upi-go] 启动 IN → VN → IN Elements/B 提取流程")
    # The final slot JSON can exceed an OS pipe buffer when retry diagnostics
    # are included. Temporary files keep the child from blocking on stdout.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stdout_file, tempfile.TemporaryFile(
        mode="w+", encoding="utf-8", errors="replace"
    ) as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=str(binary.parent),
            env=env,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        started = time.monotonic()
        while process.poll() is None:
            if cancelled and cancelled():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise InterruptedError("任务已停止")
            if time.monotonic() - started > max(90, int(timeout_seconds) * 18):
                process.kill()
                process.wait(timeout=3)
                raise RuntimeError("UPI Go 完整流程超时")
            time.sleep(0.25)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()
    result = _last_json_line(stdout)
    result["go_exit_code"] = process.returncode
    if process.returncode != 0 or not bool(result.get("ok")):
        raise RuntimeError("UPI Go 未命中：" + _error_text(result, stderr))

    url = str(
        result.get("url")
        or result.get("long_url")
        or result.get("pixInstructionUrl")
        or result.get("pixQrPngUrl")
        or result.get("pixQrSvgUrl")
        or ""
    ).strip()
    qr_data = str(result.get("pixPayload") or result.get("pixInstructionUrl") or url).strip()
    if not url and not qr_data:
        raise RuntimeError("UPI Go 已完成但未返回支付材料")

    amount = result.get("amount", result.get("finalAmount", result.get("postPromoAmount")))
    currency = str(result.get("currency") or result.get("finalCurrency") or "INR").upper()
    try:
        promo_applied = int(str(amount)) == 0
    except (TypeError, ValueError):
        promo_applied = str(amount).strip() in {"0", "0.0", "0.00"}

    if not promo_applied:
        raise RuntimeError(
            f"UPI Plus 优惠未生效：amount={amount} currency={currency}"
        )

    log(
        "[upi-go] 提取完成：amount={} currency={} approve={} material={}".format(
            amount,
            currency,
            result.get("approveResult") or "-",
            "ready" if (url or qr_data) else "missing",
        )
    )
    return {
        "provider": "upi",
        "provider_redirect_url": url,
        "qr_data": qr_data,
        "qr_image_png": str(result.get("pixQrPngUrl") or ""),
        "qr_image_svg": str(result.get("pixQrSvgUrl") or ""),
        "checkout_session_id": str(result.get("checkoutSessionId") or ""),
        "processor_entity": str(result.get("processorEntity") or ""),
        "payment_method_types": result.get("paymentMethodTypes") or result.get("finalMethods") or ["upi"],
        "checkout_amount": amount,
        "checkout_currency": currency,
        "promo_applied": promo_applied,
        "upi_engine": "go-elements-b",
        "upi_route": str(result.get("routeChain") or "IN → VN → IN"),
        "upi_approve_result": str(result.get("approveResult") or ""),
        "upi_go_elapsed_ms": result.get("elapsedMs"),
        "upi_go_attempt": result.get("goOuterAttempt"),
    }
