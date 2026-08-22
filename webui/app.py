# -*- coding: utf-8 -*-
"""
Flask 本地控制台。

复用现有后端：
    core.db                     —— 账号 / 邮箱池 / 任务的文件持久化与查询
    core.registration_service   —— 线程池批量注册 + 任务日志
    webui.config_editor         —— 安全读写 config/*.py

所有接口返回 JSON；前端是单文件 templates/index.html（原生 JS + fetch）。
默认绑定 127.0.0.1，仅本地访问。
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, redirect, render_template, request
from werkzeug.test import Client as WsgiClient
from werkzeug.wrappers import Response as WerkzeugResponse

from core import codex_retry_service, db, plan_check_service, codex_agent_service, live_check_service, gc_registration_service, checkout_kind_service, jp_trial_service, oaics_extract_service, gcash_service
from core import integrated_runtime
from webui.auth import init_auth, register_auth_routes
from core import registration_service as svc
from core.mail_status_detector import detect_mailbox_status
from webui import config_editor

logger = logging.getLogger(__name__)

_UPSTREAM_HEADER_EXCLUDES = {
    "connection", "content-encoding", "content-length", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade",
}


def _positive_worker_count(value, default: int = 10) -> int:
    raw = default if value in (None, "") else value
    workers = int(raw)
    if workers < 1:
        raise ValueError("workers 必须是正整数")
    return workers


def _rewrite_integrated_location(value: str, prefix: str) -> str:
    location = str(value or "")
    if location.startswith("/") and not location.startswith(prefix + "/"):
        return prefix + location
    return location


def _dispatch_integrated_request(service: str, prefix: str, subpath: str) -> Response:
    """Run the vendored service in this process instead of proxying to another port."""
    path = "/" + str(subpath or "").lstrip("/")
    query = request.query_string.decode("latin-1")
    target = path + (("?" + query) if query else "")
    headers = {
        key: value for key, value in request.headers.items()
        if key.lower() not in _UPSTREAM_HEADER_EXCLUDES and key.lower() != "host"
    }
    headers["Host"] = request.host
    try:
        upstream = WsgiClient(
            integrated_runtime.get_wsgi_app(service),
            WerkzeugResponse,
            use_cookies=False,
        ).open(
            target,
            method=request.method,
            headers=headers,
            data=request.get_data(cache=False),
            environ_overrides={"REMOTE_ADDR": request.remote_addr or "127.0.0.1"},
        )
    except Exception as exc:
        logger.exception("集成服务 %s 单端口调用失败", service)
        return jsonify({
            "ok": False,
            "error": f"集成服务 {service} 暂不可用：{type(exc).__name__}: {exc}",
        }), 503

    response = Response(upstream.get_data(), status=upstream.status_code)
    for key, value in upstream.headers.to_wsgi_list():
        lowered = key.lower()
        if lowered in _UPSTREAM_HEADER_EXCLUDES or lowered == "x-frame-options":
            continue
        if lowered == "location":
            value = _rewrite_integrated_location(value, prefix)
        elif lowered == "content-security-policy":
            value = re.sub(r"frame-ancestors\s+'none'\s*;?", "frame-ancestors 'self';", value, flags=re.I)
        if lowered == "set-cookie":
            response.headers.add(key, value)
        else:
            response.headers[key] = value
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Integrated-Service"] = service
    return response

def _pool_source_arg(default: str = "outlook") -> str:
    src = (request.args.get("source") or "").strip()
    if not src and request.method == "POST":
        data = request.get_json(silent=True) or {}
        src = (data.get("source") or data.get("type") or "").strip()
    return src if src in ("all", "outlook", "generic_api", "domain_api", "inbox_mate", "cloudflare_domain") else default


def _with_pool_source(rows: list[dict], source: str) -> list[dict]:
    out = []
    for r in rows:
        x = dict(r)
        x["source"] = source
        if not x.get("copy_line"):
            x["copy_line"] = x.get("email") or ""
        out.append(x)
    return out


def _is_domain_api_pool_row(row: dict) -> bool:
    return str(row.get("provider") or "").strip().lower() == "domain_api"


def _is_inbox_mate_pool_row(row: dict) -> bool:
    return str(row.get("provider") or "").strip().lower() == "inbox_mate"


def _list_pool_rows(*, source: str, status: str | None, fetch_limit: int) -> list[dict]:
    """统一读取邮箱池，供普通列表和账号关联精确筛选复用。"""
    if source == "all":
        rows = []
        rows += _with_pool_source(db.list_outlook_pool(status=status, limit=fetch_limit), "outlook")
        generic_rows = db.list_generic_api_email_pool(status=status, limit=fetch_limit)
        rows += _with_pool_source([row for row in generic_rows if not _is_domain_api_pool_row(row) and not _is_inbox_mate_pool_row(row)], "generic_api")
        rows += _with_pool_source([row for row in generic_rows if _is_domain_api_pool_row(row)], "domain_api")
        rows += _with_pool_source([row for row in generic_rows if _is_inbox_mate_pool_row(row)], "inbox_mate")
        rows += _with_pool_source(db.list_domain_email_pool(status=status, limit=fetch_limit), "cloudflare_domain")
        return sorted(
            rows,
            key=lambda x: str(x.get("created_at") or x.get("imported_at") or x.get("used_at") or ""),
            reverse=True,
        )
    if source == "generic_api":
        rows = db.list_generic_api_email_pool(status=status, limit=fetch_limit)
        return _with_pool_source([row for row in rows if not _is_domain_api_pool_row(row) and not _is_inbox_mate_pool_row(row)], "generic_api")
    if source == "domain_api":
        rows = db.list_generic_api_email_pool(status=status, limit=fetch_limit)
        return _with_pool_source([row for row in rows if _is_domain_api_pool_row(row)], "domain_api")
    if source == "inbox_mate":
        rows = db.list_generic_api_email_pool(status=status, limit=fetch_limit)
        return _with_pool_source([row for row in rows if _is_inbox_mate_pool_row(row)], "inbox_mate")
    if source == "cloudflare_domain":
        return _with_pool_source(db.list_domain_email_pool(status=status, limit=fetch_limit), "cloudflare_domain")
    return _with_pool_source(db.list_outlook_pool(status=status, limit=fetch_limit), "outlook")




def _matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _matches_pool_plan_filter(row: dict, plan_filter: str | None) -> bool:
    """保留邮箱池旧筛选契约；账号页的 Free 则只表示当前套餐。"""
    value = str(plan_filter or "").strip().lower()
    if not value or value in {"all", "any"}:
        return True
    if not row.get("registered_account_id"):
        return False
    if value == "plus":
        return db._account_matches_plan_filter(row, "plus")
    if value == "free":
        return db._account_matches_plan_filter(row, "trial")
    if value in {"nonfree", "non-free", "not-free"}:
        return db._account_matches_plan_filter(row, "no-trial")
    return False


def _paginate_items(items: list[dict], *, page: int, page_size: int) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(500, int(page_size or 50)))
    total = len(items)
    offset = (page - 1) * page_size
    return {
        "ok": True,
        "items": items[offset:offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "offset": offset,
        "limit": page_size,
    }


def _filter_account_rows_by_group(rows: list[dict], group_id: str) -> list[dict]:
    """按本地账号分组筛选；default 只显示尚未加入任何自建分组的账号。"""
    group_id = str(group_id or "").strip()
    if not group_id:
        return rows
    groups = db.list_account_groups()
    grouped_emails = {
        str(email or "").strip().lower()
        for group in groups
        for email in (group.get("emails") or [])
        if str(email or "").strip()
    }
    if group_id == "default":
        return [row for row in rows if str(row.get("email") or "").strip().lower() not in grouped_emails]
    group = next((item for item in groups if str(item.get("id") or "") == group_id), None)
    if group is None:
        return []
    emails = {str(email or "").strip().lower() for email in (group.get("emails") or []) if str(email or "").strip()}
    return [row for row in rows if str(row.get("email") or "").strip().lower() in emails]


def _compact_registration_traffic(row: dict) -> dict | None:
    """Expose only display-safe registration traffic totals in the account list."""
    raw_extra = row.get("extra_json")
    try:
        extra = json.loads(raw_extra) if isinstance(raw_extra, str) else (raw_extra or {})
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    traffic = extra.get("registration_traffic") if isinstance(extra, dict) else None
    if not isinstance(traffic, dict):
        return None

    def amount(key: str) -> int:
        try:
            return max(0, int(traffic.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    downloaded = amount("downloaded")
    saved = amount("cache_saved_bytes")
    # v1 stored Fetch replay bytes in downloaded; v2 excludes them at collection time.
    network_bytes = downloaded if traffic.get("downloaded_excludes_cache_replay") else max(0, downloaded - saved)
    return {
        "network_bytes": network_bytes,
        "cache_saved_bytes": saved,
        "cache_hits": amount("cache_hits"),
        "blocked": amount("blocked"),
        "within_budget": bool(traffic.get("within_budget")),
        "metrics_version": amount("metrics_version") or 1,
    }


def _compact_account_for_list(row: dict, gc_job: dict | None = None) -> dict:
    """账号列表轻量对象：只返回当前表格渲染和按钮判断必需字段。

    原则：
    - 不返回完整 Token / Token 预览 / TOTP Secret / Agent Token。
    - 时间戳、错误原因等只在前端确实要展示时返回；空值不返回。
    - 复制/下载敏感内容时再通过 /secret 接口按需读取。
    """
    out = {
        "id": row.get("id"),
        "email": row.get("email"),
        "has_access_token": bool(str(row.get("access_token") or "").strip()),
        "totp_enabled": bool(row.get("totp_secret")),
        "codex_agent_has_token": bool(str(row.get("codex_agent_token") or "").strip()),
    }

    # 这些是列表固定列直接展示字段。
    for key in (
        "user_name", "registration_name", "birth_date",
        "registration_exit_ip", "registration_exit_country", "openai_created_at",
        "email_source", "note", "archived", "created_at",
        "plan_type", "current_plan_type", "subscription_plan", "has_active_subscription",
        "has_active_plus_subscription", "is_free_plan", "plus_trial_eligible",
        "plan_check_status", "plan_detection_source", "plan_authority", "plan_confidence",
        "checkout_kind_status", "checkout_kind",
        "gcash_status", "gcash_eligible", "gcash_payment_method_id",
        "jp_trial_status", "jp_trial_eligible", "jp_trial_evidence", "jp_trial_error", "jp_trial_checked_at",
        "codex_status", "codex_agent_status",
    ):
        if key in row:
            out[key] = row.get(key)

    out.update({
        "jp_trial_status": row.get("jp_trial_status") or "unchecked",
        "jp_trial_eligible": row.get("jp_trial_eligible"),
        "jp_trial_evidence": row.get("jp_trial_evidence"),
        "jp_trial_error": row.get("jp_trial_error"),
        "jp_trial_checked_at": row.get("jp_trial_checked_at"),
    })

    if row.get("plan_check_status") in ("queued", "running") or row.get("plan_check_ok") is False:
        out["plan_check_ok"] = row.get("plan_check_ok")

    traffic = _compact_registration_traffic(row)
    if traffic:
        out["registration_traffic"] = traffic

    # 下面字段仅在有值时返回，避免每行堆满 null/空字符串/内部状态。
    optional_keys = (
        # 套餐展示补充：付费到期/折扣/失败原因。
        "plan_check_error", "plan_checked_at", "plan_last_success_at",
        "plan_check_queued_at", "plan_check_started_at",
        "plan_check_network_route", "plan_check_proxy_used", "plan_check_proxy_fallback_reason",
        "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        "token_expired", "token_expires_at",
        # Checkout 类型检测；不包含 AT 或完整 Checkout URL。
        "checkout_kind_ok", "checkout_kind_provider", "checkout_kind_processor",
        "checkout_kind_session_prefix", "checkout_kind_confirm_sent",
        "checkout_kind_checked_at", "checkout_kind_error",
        # GCash 资格检测。
        "gcash_ok", "gcash_checkout_country", "gcash_checkout_currency",
        "gcash_checked_at", "gcash_completed_at", "gcash_error",
        "oaics_extract_status", "oaics_extract_ok", "oaics_extract_error",
        "oaics_extract_stage", "oaics_extract_log", "oaics_extract_queued_at", "oaics_extract_started_at", "oaics_extract_completed_at", "oaics_link",
        # 邮箱检测结果：账号页直接展示并支持单账号刷新。
        "mail_plus_status", "mail_plus_promoted", "mail_plus_checked_at", "mail_plus_evidence",
        "mail_plus_subject", "mail_plus_date", "mail_plus_account_id",
        # 查活状态。
        "live_check_status", "live_check_error", "live_checked_at",
        # Codex / Agent 状态提示。
        "codex_error", "codex_agent_message", "codex_agent_runtime_id",
        "codex_agent_sub2api_url", "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
    )
    for key in optional_keys:
        value = row.get(key)
        if value is not None and value != "":
            out[key] = value
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
    if row.get("has_active_plus_subscription") or any(x in plan for x in ("plus", "pro", "team", "go")):
        expire = row.get("expires_at")
        if expire:
            out["expires_at"] = expire
    if gc_job:
        out.update({
            "gc_job_id": int(gc_job.get("id") or 0),
            "gc_window_state": gc_job.get("gc_window_state"),
            "gc_check_state": gc_job.get("gc_check_state"),
            "gc_check_message": gc_job.get("gc_check_message") or gc_job.get("error_message"),
            "gc_roxy_profile_id": gc_job.get("roxy_profile_id"),
            "gc_window_label": gc_job.get("gc_window_label"),
        })
    return out


def _compact_accounts_for_list(rows: list[dict]) -> list[dict]:
    jobs_by_account: dict[int, dict] = {}
    for job in db.list_jobs(limit=1_000_000):
        account_id = int(job.get("account_id") or 0)
        if not account_id or not bool(job.get("gc_mode")) or job.get("gc_window_state") == "deleted":
            continue
        if str(job.get("status") or "") not in {"pending", "running", "stopping", "gc_waiting", "gc_checking"}:
            continue
        if account_id not in jobs_by_account:
            jobs_by_account[account_id] = job
    return [_compact_account_for_list(row, jobs_by_account.get(int(row.get("id") or 0))) for row in rows]


def _account_secret_value(row: dict, field: str) -> str:
    """Return only the explicitly allow-listed account secret requested by the UI."""
    field = str(field or "").strip()
    if field == "access_token":
        return str(row.get("access_token") or "")
    if field == "copy_line":
        return str(row.get("copy_line") or "")
    if field == "codex_agent_token":
        return str(row.get("codex_agent_token") or "")
    if field == "totp_secret":
        return str(row.get("totp_secret") or "")
    if field == "oaics_link":
        return str(row.get("oaics_link") or "")
    raise ValueError("field 仅支持 access_token/copy_line/codex_agent_token/totp_secret/oaics_link")


def _compact_job_for_list(row: dict) -> dict:
    """注册任务列表轻量对象：保留表格和 GC 操作需要的字段，不下发敏感值。"""
    out = {
        "id": row.get("id"),
        "status": row.get("status"),
    }
    for key in (
        "parent_job_id", "retry_attempt", "email", "started_at", "completed_at",
        "display_status", "retryable", "retry_action", "retry_label",
        "manual_otp_required",
        # GC 任务列表操作和窗口状态。
        "gc_mode", "gc_window_state", "gc_check_state", "gc_check_message",
        "gc_window_label", "roxy_profile_id", "account_id",
    ):
        value = row.get(key)
        if value is not None and value != "" and value is not False:
            out[key] = value
    err = str(row.get("error_message") or "").strip()
    if err:
        # 完整错误和堆栈仍通过任务日志读取。
        out["error_message"] = err[:240] + ("…" if len(err) > 240 else "")
    return out


def _job_status_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts["active"] = sum(
        int(counts.get(status, 0) or 0)
        for status in ("pending", "running", "stopping", "gc_waiting", "gc_checking")
    )
    return counts


def _resolve_email_targets(data: dict) -> tuple[list[str], list[dict]]:
    """Resolve selections from every UI pool to unique email addresses."""
    found: dict[str, str] = {}
    skipped: list[dict] = []

    def add(value, *, source: str = "email", ref=None) -> None:
        email = str(value or "").strip()
        if email and "@" in email:
            found.setdefault(email.lower(), email)
        elif value:
            skipped.append({"source": source, "value": ref if ref is not None else value, "reason": "没有有效邮箱"})

    for raw in data.get("emails") or []:
        add(raw.get("email") if isinstance(raw, dict) else raw)
    for raw in data.get("account_ids") or data.get("ids") or []:
        try:
            account = db.get_account(int(raw))
        except (TypeError, ValueError):
            account = None
        if account:
            add(account.get("email"), source="account_id", ref=raw)
        else:
            skipped.append({"source": "account_id", "value": raw, "reason": "账号不存在"})
    for raw in data.get("job_ids") or []:
        try:
            job = db.get_job(int(raw))
        except (TypeError, ValueError):
            job = None
        if job:
            add(job.get("email"), source="job_id", ref=raw)
        else:
            skipped.append({"source": "job_id", "value": raw, "reason": "任务不存在或尚未分配邮箱"})
    codex_by_name = {row.get("filename"): row for row in db.list_codex_accounts()}
    for filename in data.get("filenames") or data.get("codex_filenames") or []:
        row = codex_by_name.get(str(filename or ""))
        if row:
            add(row.get("email"), source="filename", ref=filename)
        else:
            skipped.append({"source": "filename", "value": filename, "reason": "Codex 凭证不存在"})
    return list(found.values()), skipped

def create_app(auth_code: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    _prepared_downloads: dict[str, dict] = {}
    def _put_prepared_download(content: bytes, filename: str, mimetype: str = "application/zip") -> str:
        now = time.time()
        # 顺手清理 10 分钟前的临时下载，避免内存堆积。
        for k, v in list(_prepared_downloads.items()):
            if now - float(v.get("created_at") or 0) > 600:
                _prepared_downloads.pop(k, None)
        download_id = uuid.uuid4().hex
        _prepared_downloads[download_id] = {
            "content": bytes(content),
            "filename": filename,
            "mimetype": mimetype,
            "created_at": now,
        }
        return download_id

    @app.get("/api/downloads/<download_id>")
    def api_prepared_download(download_id: str):
        item = _prepared_downloads.pop(str(download_id or ""), None)
        if not item:
            return jsonify({"ok": False, "error": "下载已过期或不存在，请重新生成"}), 404
        content = item.get("content") or b""
        filename = item.get("filename") or "download.zip"
        mimetype = item.get("mimetype") or "application/octet-stream"
        return Response(
            content,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(content)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    init_auth(app, auth_code=auth_code)
    register_auth_routes(app)

    @app.route("/pay153/", defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    @app.route("/pay153/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def integrated_pay153(subpath: str):
        return _dispatch_integrated_request("pay153", "/pay153", subpath)

    @app.get("/checkout-link/")
    @app.get("/checkout-link/<path:subpath>")
    def legacy_checkout_link(subpath: str = ""):
        target = "/pay153/" + str(subpath or "").lstrip("/")
        if request.query_string:
            target += "?" + request.query_string.decode("utf-8", errors="ignore")
        return redirect(target, code=302)

    @app.route("/paypal-pay/", defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    @app.route("/paypal-pay/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def integrated_paypal_agreement(subpath: str):
        return _dispatch_integrated_request("paypal-agreement", "/paypal-pay", subpath)

    @app.get("/api/integrations/health")
    def api_integrations_health():
        return jsonify({"ok": True, "services": integrated_runtime.status()})


    @app.post("/api/emails/resolve")
    def api_resolve_email_targets():
        """把任意界面的选中项解析成邮箱，供复制和跨界面定位。"""
        data = request.get_json(silent=True) or {}
        emails, skipped = _resolve_email_targets(data)
        if len(emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多处理 5000 个邮箱"}), 400
        return jsonify({"ok": True, "emails": emails, "count": len(emails), "skipped": skipped})

    @app.post("/api/emails/copy-mailbox-lines")
    def api_copy_mailbox_lines():
        """Return selections from any list as: email----mailbox URL."""
        data = request.get_json(silent=True) or {}
        emails, skipped = _resolve_email_targets(data)
        if not emails:
            return jsonify({"ok": False, "error": "选中项没有可复制的邮箱", "skipped": skipped}), 400
        if len(emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多复制 5000 个邮箱"}), 400

        url_by_email = {
            str(row.get("email") or "").strip().lower(): str(row.get("code_url") or "").strip()
            for row in db.list_generic_api_email_pool(limit=1_000_000)
            if str(row.get("email") or "").strip()
        }
        lines: list[str] = []
        missing_url_count = 0
        for email in emails:
            code_url = url_by_email.get(email.lower(), "")
            if not code_url:
                missing_url_count += 1
            lines.append(f"{email}----{code_url}")
        return jsonify({
            "ok": True,
            "lines": lines,
            "count": len(lines),
            "missing_url_count": missing_url_count,
            "skipped": skipped,
        })

    @app.post("/api/emails/purge")
    def api_purge_emails_everywhere():
        """按邮箱从所有本地数据池和派生界面中彻底清理。"""
        data = request.get_json(silent=True) or {}
        emails, skipped = _resolve_email_targets(data)
        if not emails:
            return jsonify({"ok": False, "error": "选中项没有可清理的邮箱", "skipped": skipped}), 400
        if len(emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多清理 5000 个邮箱"}), 400
        result = db.purge_emails_everywhere(emails, protect_active_jobs=True)
        return jsonify({"ok": True, **result, "resolve_skipped": skipped})

    recovered_plan_checks = db.recover_interrupted_plan_checks()
    if recovered_plan_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的套餐查询状态", recovered_plan_checks)
    recovered_live_checks = db.recover_interrupted_live_checks()
    if recovered_live_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的查活状态", recovered_live_checks)
    recovered_codex_agents = db.recover_interrupted_codex_agents()
    if recovered_codex_agents:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 Codex Agent Token 状态", recovered_codex_agents)
    recovered_checkout_kind = db.recover_interrupted_checkout_kind_checks()
    if recovered_checkout_kind:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 Checkout 类型检测状态", recovered_checkout_kind)
    recovered_oaics_extracts = db.recover_interrupted_oaics_extracts()
    if recovered_oaics_extracts:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 OAICS 提链状态", recovered_oaics_extracts)
    recovered_gcash = db.recover_interrupted_gcash_checks()
    if recovered_gcash:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 GCash 资格检测状态", recovered_gcash)
    # ----------------------------------------------------------
    # 页面
    # ----------------------------------------------------------
    @app.get("/")
    def index():
        response = Response(render_template("index.html"), mimetype="text/html")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    # ----------------------------------------------------------
    # 统计概览
    # ----------------------------------------------------------
    @app.get("/api/summary")
    def api_summary():
        from config import email as _email_cfg
        from core.email_provider import parse_email_sources
        pool = {"total": 0, "available": 0, "used": 0, "failed": 0}
        for src in parse_email_sources(_email_cfg.EMAIL_SOURCE):
            # GPTMail/MailNest/CloudMail 地址按需生成，不属于本地邮箱池。
            if src in ("gptmail", "mailnest", "cloudmail", "cloudflare"):
                continue
            one = (
                db.generic_api_email_pool_summary(provider="generic_api") if src == "generic_api"
                else db.generic_api_email_pool_summary(provider="domain_api") if src == "domain_api"
                else db.generic_api_email_pool_summary(provider="inbox_mate") if src == "inbox_mate"
                else db.domain_email_pool_summary() if src == "cloudflare_domain"
                else db.outlook_pool_summary()
            )
            for k in pool:
                pool[k] += int(one.get(k, 0) or 0)
        domain_pool = db.domain_email_pool_summary()
        return jsonify({
            "accounts": db.count_accounts(),
            "outlook_total": pool.get("total", 0),
            "outlook_available": pool.get("available", 0),
            "outlook_used": pool.get("used", 0),
            "outlook_failed": pool.get("failed", 0),
            "domain_total": domain_pool.get("total", 0),
            "domain_available": domain_pool.get("available", 0),
            "domain_used": domain_pool.get("used", 0),
            "domain_failed": domain_pool.get("failed", 0),
        })

    # ----------------------------------------------------------
    # 已注册账号
    # ----------------------------------------------------------
    @app.get("/api/accounts")
    def api_accounts():
        limit = request.args.get("limit", default=500, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        checkout_kind = str(request.args.get("checkout_kind", default="") or "").strip().lower()
        gcash = str(request.args.get("gcash", default="") or "").strip().lower()
        q = str(request.args.get("q", default="") or "").strip()
        group_id = str(request.args.get("group", default="") or "").strip()
        # 新分页接口：传 page/page_size 或 paged=1 时返回 {items,total,page,page_size,...}
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        # 统一过滤函数：只显示已查询到 GCash 资格的账号。
        apply_gcash_filter = lambda rows: (
            [row for row in rows if row.get("gcash_eligible") is True]
            if gcash in {"1", "true", "yes"}
            else rows
        )
        if group_id:
            rows = _filter_account_rows_by_group(
                db.list_accounts(limit=1_000_000, archived=archived, plan_filter=plan_filter, q=q),
                group_id,
            )
            if checkout_kind:
                rows = [row for row in rows if str(row.get("checkout_kind") or "").strip().lower() == checkout_kind]
            rows = apply_gcash_filter(rows)
            if paged or page_arg is not None or page_size_arg is not None:
                page = max(1, int(page_arg or 1))
                page_size = max(1, min(500, int(page_size_arg or limit or 50)))
                result = _paginate_items(_compact_accounts_for_list(rows), page=page, page_size=page_size)
                result.update({"ok": True, "page": page, "page_size": page_size, "compact": True, "group": group_id})
                return jsonify(result)
            return jsonify(rows[:limit])
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            if checkout_kind or gcash in {"1", "true", "yes"}:
                rows = [row for row in db.list_accounts(limit=1_000_000, archived=archived, plan_filter=plan_filter, q=q)
                        if (not checkout_kind or str(row.get("checkout_kind") or "").strip().lower() == checkout_kind)
                        and (gcash not in {"1", "true", "yes"} or row.get("gcash_eligible") is True)]
                result = _paginate_items(_compact_accounts_for_list(rows), page=page, page_size=page_size)
            else:
                result = db.list_accounts_page(limit=page_size, offset=offset, archived=archived, plan_filter=plan_filter, q=q)
                result["items"] = _compact_accounts_for_list(result.get("items") or [])
            result.update({"ok": True, "page": page, "page_size": page_size, "compact": True})
            return jsonify(result)
        rows = db.list_accounts(limit=limit, archived=archived, plan_filter=plan_filter, q=q)
        if checkout_kind:
            rows = [row for row in rows if str(row.get("checkout_kind") or "").strip().lower() == checkout_kind]
        rows = apply_gcash_filter(rows)
        return jsonify(rows)

    @app.get("/api/account-groups")
    def api_account_groups():
        return jsonify({"ok": True, "items": db.list_account_groups()})

    @app.post("/api/account-groups")
    def api_account_groups_create():
        data = request.get_json(silent=True) or {}
        try:
            group = db.create_account_group(str(data.get("name") or ""))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "group": group}), 201

    @app.post("/api/account-groups/<group_id>/members")
    def api_account_group_add_members(group_id: str):
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多加入 5000 个账号"}), 400
        if str(group_id or "").strip().lower() == "default":
            group, skipped = db.move_accounts_to_default_group(ids)
        else:
            group, skipped = db.add_accounts_to_group(group_id, ids)
        if group is None:
            return jsonify({"ok": False, "error": "分组不存在"}), 404
        return jsonify({"ok": True, "group": group, "skipped": skipped, "added_count": group.get("count", 0)})

    @app.delete("/api/account-groups/<group_id>")
    def api_account_groups_delete(group_id: str):
        if not db.delete_account_group(group_id):
            return jsonify({"ok": False, "error": "分组不存在"}), 404
        return jsonify({"ok": True, "deleted": True})

    @app.patch("/api/account-groups/<group_id>")
    def api_account_groups_rename(group_id: str):
        data = request.get_json(silent=True) or {}
        try:
            group = db.rename_account_group(group_id, str(data.get("name") or ""))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if group is None:
            return jsonify({"ok": False, "error": "分组不存在"}), 404
        return jsonify({"ok": True, "group": group})

    @app.post("/api/accounts/filter-emails")
    def api_accounts_filter_emails():
        """按完整邮箱精确定位账号，同时保留账号页现有筛选和分页。"""
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"ok": False, "error": "emails 必须是非空数组"}), 400
        email_set = {str(value or "").strip().lower() for value in emails if str(value or "").strip()}
        archived = str(data.get("archived") or "all").lower()
        plan_filter = str(data.get("plan") or "").lower()
        checkout_kind = str(data.get("checkout_kind") or "").strip().lower()
        gcash = str(data.get("gcash") or "").strip().lower()
        q = str(data.get("q") or "").strip()
        page = max(1, int(data.get("page") or 1))
        page_size = max(1, min(500, int(data.get("page_size") or 50)))
        rows = db.list_accounts(limit=1_000_000, archived=archived, plan_filter=plan_filter, q=q)
        rows = [row for row in rows if str(row.get("email") or "").strip().lower() in email_set]
        if checkout_kind:
            rows = [row for row in rows if str(row.get("checkout_kind") or "").strip().lower() == checkout_kind]
        if gcash in {"1", "true", "yes"}:
            rows = [row for row in rows if row.get("gcash_eligible") is True]
        result = _paginate_items(_compact_accounts_for_list(rows), page=page, page_size=page_size)
        result["filter_email_count"] = len(email_set)
        return jsonify(result)

    @app.get("/api/accounts/free-delete-targets")
    def api_accounts_free_delete_targets():
        """返回最近一次套餐查询明确确认的未归档 Free 账号，供手动全局清理。"""
        rows = db.list_accounts(limit=1_000_000, archived="0", plan_filter="free")
        targets = []
        for row in rows:
            plan = str(row.get("current_plan_type") or "").strip().lower()
            subscription_plan = str(row.get("subscription_plan") or "").strip().lower()
            mail_status = str(row.get("mail_plus_status") or "").strip().lower()
            has_active_plus = bool(row.get("has_active_plus_subscription")) or bool(
                row.get("has_active_subscription")
                and "plus" in subscription_plan
                and "free" not in subscription_plan
            )
            confirmed_free = bool(row.get("plan_last_success_at")) and (
                plan == "free"
                or bool(row.get("is_free_plan"))
                or subscription_plan == "chatgptfreeplan"
            )
            if not confirmed_free or has_active_plus or mail_status == "plus":
                continue
            email = str(row.get("email") or "").strip()
            if email:
                targets.append({"id": int(row.get("id") or 0), "email": email})
        return jsonify({"ok": True, "items": targets, "count": len(targets)})

    @app.get("/api/accounts/plan-check-status")
    def api_account_plan_check_status():
        """套餐查询轻量状态，不返回 Token、邮箱密码等敏感字段。"""
        limit = request.args.get("limit", default=5000, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        q = str(request.args.get("q", default="") or "").strip()
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            snapshot = db.list_account_plan_check_statuses(limit=page_size, offset=offset, archived=archived, plan_filter=plan_filter, q=q)
            snapshot.update({"page": page, "page_size": page_size})
        else:
            snapshot = db.list_account_plan_check_statuses(limit=max(1, min(5000, limit)), archived=archived, plan_filter=plan_filter, q=q)
        snapshot["queue"] = plan_check_service.queue_settings()
        return jsonify(snapshot)


    @app.get("/api/accounts/<int:acc_id>/secret")
    def api_account_secret(acc_id: int):
        """按需读取单账号敏感值，避免账号列表一次性下发完整 Token/整行。"""
        field = str(request.args.get("field") or "").strip()
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            value = _account_secret_value(acc, field)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "id": acc_id, "field": field, "value": value})

    @app.post("/api/accounts/secret-bulk")
    def api_accounts_secret_bulk():
        """按需批量读取账号敏感值。Body {account_ids:[...], field}."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        field = str(data.get("field") or "").strip()
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多读取 5000 个账号"}), 400
        values = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            try:
                value = _account_secret_value(acc, field)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            if value:
                values.append({"id": acc_id, "email": acc.get("email"), "value": value})
            else:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "值为空"})
        return jsonify({"ok": True, "field": field, "values": values, "count": len(values), "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/archive")
    def api_account_archive(acc_id: int):
        """归档/取消归档一个账号。Body {archived: true|false}。"""
        data = request.get_json(silent=True) or {}
        archived = bool(data.get("archived", True))
        updated = db.archive_account(acc_id=acc_id, archived=archived)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "archived": archived})

    @app.post("/api/accounts/archive-bulk")
    def api_accounts_archive_bulk():
        """批量归档/取消归档账号。Body {account_ids:[...], archived:true|false}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        archived = bool(data.get("archived", True))
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多归档 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.archive_accounts(account_ids=account_ids, archived=archived)
        skipped.extend(db_skipped)
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/delete")
    def api_account_delete(acc_id: int):
        """删除一个已注册账号记录。只删除本地保存的账号/token记录，不改邮箱池状态。"""
        deleted = db.delete_account(acc_id=acc_id)
        if not deleted:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "deleted": True})

    @app.post("/api/accounts/delete-bulk")
    def api_accounts_delete_bulk():
        """批量删除已注册账号记录。Body {account_ids: [...]} 或 {ids: [...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        deleted, db_skipped = db.delete_accounts(account_ids=account_ids)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    @app.post("/api/accounts/<int:acc_id>/note")
    def api_account_note(acc_id: int):
        """更新单个已注册账号备注。Body {note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        note = str(data.get("note") or "")
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400
        updated = db.update_account_note(acc_id=acc_id, note=note)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "note": note})

    @app.post("/api/accounts/note-bulk")
    def api_accounts_note_bulk():
        """批量更新已注册账号备注。Body {account_ids: [...], note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        note = str(data.get("note") or "")
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多备注 5000 个账号"}), 400
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400

        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.update_accounts_note(account_ids=account_ids, note=note)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.post("/api/accounts/check-live-bulk")
    def api_accounts_check_live_bulk():
        """批量查活：加入后台队列；协议 BrowserSession 指纹环境重新登录并刷新最新 AT。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查活 500 个账号"}), 400

        account_ids: list[int] = []
        skipped: list[dict] = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)

        accounts = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            accounts.append(acc)

        started = []
        busy_count = 0
        failed = []
        for acc in accounts:
            acc_id = int(acc.get("id") or 0)
            email = str(acc.get("email") or "")
            queued = live_check_service.enqueue_account_live_check(
                account_id=acc_id,
                email=email,
                trigger="manual",
                # 查活按“查套餐”同一套网络选路：
                # PLAN_CHECK_PROXY_MODE / PLAN_CHECK_PROXY / PROXY_POOL。
                # 不复用账号注册时的 proxy_used，避免旧注册出口被 CF 403 后一直失败。
                proxy=None,
            )
            if queued.get("accepted"):
                started.append({"id": acc_id, "email": email, "status": "queued"})
            elif queued.get("busy"):
                busy_count += 1
                skipped.append({"id": acc_id, "email": email, "reason": queued.get("error") or "正在查活"})
            else:
                failed.append({"id": acc_id, "email": email, "error": queued.get("error") or "入队失败"})

        return jsonify({
            "ok": True,
            "message": f"已入队 {len(started)} 个查活任务",
            "started": started,
            "started_count": len(started),
            "busy_count": busy_count,
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "queue": live_check_service.queue_settings(),
        }), 202


    @app.post("/api/accounts/check-plan")
    def api_account_check_plan():
        """把单账号套餐查询加入后台队列。Body {account_id|email, proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        email = (data.get("email") or "").strip()
        acc = None
        if acc_id is not None:
            try:
                acc = db.get_account(int(acc_id))
            except Exception:
                acc = None
        if acc is None and email:
            acc = db.get_account_by_email(email)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "账号缺少 AT/access_token，无法查询套餐；请先查活刷新 AT"}), 400
        account_id = int(acc.get("id"))
        queued = plan_check_service.enqueue_account_plan_check(
            account_id=account_id,
            email=acc.get("email") or "",
            access_token=token,
            trigger="manual",
            proxy=None,
            timezone_offset_min=str(data.get("timezone_offset_min") or "-"),
        )
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **queued}), 202

    @app.post("/api/accounts/check-plan-bulk")
    def api_accounts_check_plan_bulk():
        """批量把套餐查询加入统一后台队列。Body {account_ids:[...], workers?, proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400
        # 与单账号查询保持一致：页面代理池可填写直接代理或返回代理的 API。
        timezone_offset_min = str(data.get("timezone_offset_min") or "-")
        try:
            workers = _positive_worker_count(data.get("workers"), plan_check_service.get_executor_workers())
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是正整数"}), 400
        # 本批固定使用同一个线程池，避免并发请求切换 workers 时把同一批拆到不同池。
        executor = plan_check_service.get_executor(max_workers=workers)

        items = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not str(acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 AT/access_token"})
                continue
            items.append(acc)

        started = []
        busy = []
        failed = []
        for acc in items:
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=acc.get("access_token") or "",
                trigger="manual_bulk",
                proxy=None,
                timezone_offset_min=timezone_offset_min,
                executor=executor,
            )
            item = {"id": acc.get("id"), "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "workers": workers,
            "queue": plan_check_service.queue_settings(),
        }), 202

    @app.post("/api/accounts/check-jp-trial-bulk")
    def api_accounts_check_jp_trial_bulk():
        """使用注册代理池中的已确认 JP 出口检测 Plus 一个月试用资格。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        selected_ids: list[int] = []
        seen: set[int] = set()
        for raw_id in ids:
            try:
                account_id = int(raw_id)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "请选择有效的注册账号"}), 400
            if account_id <= 0 or account_id > 9_007_199_254_740_991:
                return jsonify({"ok": False, "error": "请选择有效的注册账号"}), 400
            if isinstance(raw_id, float) and not raw_id.is_integer():
                return jsonify({"ok": False, "error": "请选择有效的注册账号"}), 400
            if account_id in seen:
                continue
            seen.add(account_id)
            selected_ids.append(account_id)
        if len(selected_ids) > 100:
            return jsonify({"ok": False, "error": "单次最多检测 100 个账号"}), 400
        selected = []
        for account_id in selected_ids:
            account = db.get_account(account_id)
            if not account:
                return jsonify({"ok": False, "error": "选择中包含不属于本注册页面的账号"}), 409
            selected.append(account)
        try:
            result = jp_trial_service.check_accounts_jp_trial(selected)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        return jsonify({"ok": True, **result, "promo_campaign": jp_trial_service.PROMO_CAMPAIGN})

    @app.post("/api/accounts/check-checkout-kind-bulk")
    def api_accounts_check_checkout_kind_bulk():
        """Create one Checkout per selected account and stop before any confirm."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多检测 500 个账号"}), 400
        try:
            workers = _positive_worker_count(data.get("workers"), 10)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是正整数"}), 400
        executor = checkout_kind_service.get_executor(workers)
        started, busy, failed, skipped = [], [], [], []
        seen: set[int] = set()
        for raw_id in ids:
            try:
                account_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if account_id in seen:
                continue
            seen.add(account_id)
            account = db.get_account(account_id)
            if not account:
                skipped.append({"id": account_id, "reason": "账号不存在"})
                continue
            token = str(account.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": account_id, "email": account.get("email"), "reason": "缺少 AT/access_token"})
                continue
            queued = checkout_kind_service.enqueue(
                account_id=account_id,
                access_token=token,
                trigger="manual_bulk",
                proxy=None,
                executor=executor,
            )
            item = {"id": account_id, "email": account.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "workers": workers,
            "confirm_sent": False,
            "message": "检测只创建 Checkout 并读取类型，不执行 promo update 或 confirm",
        }), 202

    @app.post("/api/accounts/check-gcash-bulk")
    def api_accounts_check_gcash_bulk():
        """Create one PH/PHP Checkout per selected account and read GCash
        availability without confirming or starting any payment method."""
        from core import detection_proxy
        from config import proxy as proxy_cfg
        import random

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多检测 500 个账号"}), 400
        max_workers = 100
        try:
            workers = _positive_worker_count(data.get("workers"), 50)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是正整数"}), 400
        workers = max(1, min(workers, max_workers, len(ids)))
        # GCash 探测必须走 PH 出口；代理池为设置在“gc查询代理池”的 PH 代理列表。
        pool_specs = detection_proxy.parse_detection_proxy_pool(
            getattr(proxy_cfg, "GC_CHECK_PROXY_PROFILES", []) or []
        )
        if not pool_specs:
            return jsonify({"ok": False, "error": "尚未配置 gc查询代理池（PH 出口代理）"}), 409
        if len(pool_specs) < workers:
            workers = len(pool_specs)

        executor = gcash_service.get_executor(workers)
        started, busy, failed, skipped = [], [], [], []
        seen: set[int] = set()
        for raw_id in ids:
            try:
                account_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if account_id in seen:
                continue
            seen.add(account_id)
            account = db.get_account(account_id)
            if not account:
                skipped.append({"id": account_id, "reason": "账号不存在"})
                continue
            token = str(account.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": account_id, "email": account.get("email"), "reason": "缺少 AT/access_token"})
                continue
            proxy_spec = random.choice(pool_specs)
            proxy_url = ""
            try:
                proxy_url = detection_proxy.resolve_detection_proxy(proxy_spec) or ""
            except Exception as exc:
                skipped.append({"id": account_id, "email": account.get("email"), "reason": f"代理解析失败：{exc}"})
                continue
            queued = gcash_service.enqueue(
                account_id=account_id,
                access_token=token,
                trigger="manual_gcash_bulk",
                proxy=proxy_url or None,
                executor=executor,
            )
            item = {"id": account_id, "email": account.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "workers": workers,
            "confirm_sent": False,
            "message": "每个账号创建一次 PH Checkout 并读取 GCash 支付方式，不执行 confirm/start",
        }), 202

    @app.post("/api/accounts/extract-oaics-bulk")
    def api_accounts_extract_oaics_bulk():
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多处理 5000 个账号"}), 400
        try:
            oaics_extract_service.configured_proxy_pool()
            workers = oaics_extract_service.configured_worker_count()
            executor = oaics_extract_service.get_executor(workers)
            started, busy, failed, skipped = [], [], [], []
            seen = set()
            for raw_id in ids:
                try:
                    account_id = int(raw_id)
                except (TypeError, ValueError):
                    skipped.append({"id": raw_id, "reason": "ID 非法"})
                    continue
                if account_id in seen:
                    continue
                seen.add(account_id)
                account = db.get_account(account_id)
                if not account:
                    skipped.append({"id": account_id, "reason": "账号不存在"})
                    continue
                if str(account.get("checkout_kind") or "").strip().lower() != "oaics":
                    skipped.append({
                        "id": account_id,
                        "email": account.get("email"),
                        "reason": "Checkout 类型尚未确认 OAICS",
                    })
                    continue
                token = str(account.get("access_token") or "").strip()
                if not token:
                    skipped.append({"id": account_id, "email": account.get("email"), "reason": "缺少 AT/access_token"})
                    continue
                queued = oaics_extract_service.enqueue(
                    account_id=account_id, access_token=token,
                    trigger="accounts_oaics_bulk", executor=executor,
                )
                item = {"id": account_id, "email": account.get("email"), **queued}
                if queued.get("accepted"):
                    started.append(item)
                elif queued.get("busy"):
                    busy.append(item)
                else:
                    failed.append(item)
                    db.update_account_oaics_extract(account_id, {
                        "ok": False,
                        "error": queued.get("error") or "OAICS 提链任务未能入队",
                    })
            logger.info(
                "OAICS账号提链入队：started=%s busy=%s failed=%s skipped=%s workers=%s",
                len(started), len(busy), len(failed), len(skipped), workers,
            )
            return jsonify({
                "ok": True, "started": started, "started_count": len(started),
                "busy": busy, "busy_count": len(busy), "failed": failed,
                "failed_count": len(failed), "skipped": skipped,
                "skipped_count": len(skipped), "workers": workers,
            }), 202
        except Exception as exc:
            return jsonify({"ok": False, "error": f"OAICS 提链队列初始化失败：{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/accounts/codex-agent")
    def api_account_codex_agent():
        """单账号生成 Codex Agent Token。Body {account_id|id, verify_task?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = codex_agent_service.enqueue_account_codex_agent(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                verify_task=bool(data.get("verify_task", True)),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/codex-agent-bulk")
    def api_accounts_codex_agent_bulk():
        """批量生成 Codex Agent Token。Body {account_ids:[...], verify_task?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = codex_agent_service.enqueue_account_codex_agent(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    verify_task=bool(data.get("verify_task", True)),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    def _codex_agent_auth_for_account(acc: dict) -> tuple[str, str]:
        """返回账号已生成的 Codex Agent auth.json 文本与下载文件名。"""
        import json as _json
        from pathlib import Path as _Path

        email = str(acc.get("email") or "").strip()
        safe_email = "".join(ch if ch.isalnum() or ch in ("@", ".", "-", "_") else "_" for ch in (email or f"account-{acc.get('id')}"))
        filename = f"codex-agent-{safe_email}.json"
        token_text = str(acc.get("codex_agent_token") or "").strip()
        if token_text:
            try:
                payload = _json.loads(token_text)
                token_text = _json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            except Exception:
                token_text = token_text + ("\n" if not token_text.endswith("\n") else "")
            return token_text, filename

        auth_path = str(acc.get("codex_agent_auth_path") or "").strip()
        if auth_path:
            p = _Path(auth_path)
            if p.exists() and p.is_file():
                return p.read_text(encoding="utf-8"), p.name or filename

        raise RuntimeError("该账号还没有生成 Codex Agent Token")

    def _join_sub2_url(base: str, path: str) -> str:
        base = str(base or "").strip().rstrip("/")
        path = str(path or "").strip()
        if not base or not path:
            return ""
        parsed = urlparse(path)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return path
        return f"{base}/{path.lstrip('/')}"

    def _sub2_codex_session_import_url() -> str:
        from config import sub2api as sub2api_cfg
        api_base = str(getattr(sub2api_cfg, "SUB2API_API_BASE", "") or "").strip()
        if api_base:
            return _join_sub2_url(api_base, "/api/v1/admin/accounts/import/codex-session")
        # 兼容旧配置：之前 SUB2API_API_URL 是完整上传接口 URL。
        return str(getattr(sub2api_cfg, "SUB2API_API_URL", "") or "").strip()

    def _upload_account_codex_agent_to_sub2(acc: dict) -> dict:
        """把账号已生成的 Codex Agent auth.json 上传到 sub2api。"""
        import json as _json
        from config import sub2api as sub2api_cfg
        from core.codex_agent import upload_sub2api_account

        text, _filename = _codex_agent_auth_for_account(acc)
        try:
            auth_json = _json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"Agent Token JSON 无效: {exc}") from exc

        api_url = _sub2_codex_session_import_url()
        api_token = str(getattr(sub2api_cfg, "SUB2API_API_KEY", "") or getattr(sub2api_cfg, "SUB2API_API_TOKEN", "") or "").strip()
        auth_header = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key") or "x-api-key").strip()
        auth_prefix = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_PREFIX", "") or "").strip()
        payload_mode = "codex_session_import"
        proxy_key = str(getattr(sub2api_cfg, "SUB2API_PROXY_KEY", "") or "").strip() or None
        timeout = float(getattr(sub2api_cfg, "SUB2API_API_TIMEOUT", 20) or 20)

        result = upload_sub2api_account(
            auth_json,
            api_url,
            api_token=api_token,
            auth_header=auth_header,
            auth_prefix=auth_prefix,
            payload_mode=payload_mode,
            proxy_key=proxy_key,
            timeout=timeout,
        )
        try:
            db.update_account_codex_agent(int(acc.get("id")), {
                "ok": True,
                "status": "success",
                "message": "Agent Token 已上传 sub2api",
                "sub2api_url": result.get("url"),
                "sub2api_mode": result.get("payload_mode"),
                "sub2api_total": result.get("total"),
            })
        except Exception:
            logger.exception("更新账号 sub2api 上传状态失败: account_id=%s", acc.get("id"))
        return result

    @app.post("/api/accounts/<int:acc_id>/codex-agent/upload-sub2")
    def api_account_codex_agent_upload_sub2(acc_id: int):
        """单账号把已生成的 Codex Agent Token 上传到 sub2api。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            result = _upload_account_codex_agent_to_sub2(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify({"ok": True, "account_id": acc_id, "email": acc.get("email"), "result": result})

    @app.post("/api/accounts/codex-agent/upload-sub2-bulk")
    def api_accounts_codex_agent_upload_sub2_bulk():
        """批量把已生成的 Codex Agent Token 上传到 sub2api。Body {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        uploaded, failed, skipped = [], [], []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if (acc.get("codex_agent_status") or "") != "success" and not (acc.get("codex_agent_token") or acc.get("codex_agent_auth_path")):
                skipped.append({"id": acc_id, "email": email, "reason": "未生成 Agent Token"})
                continue
            try:
                result = _upload_account_codex_agent_to_sub2(acc)
                uploaded.append({"id": acc_id, "email": email, "url": result.get("url"), "status_code": result.get("status_code")})
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "uploaded": uploaded,
            "uploaded_count": len(uploaded),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.get("/api/accounts/<int:acc_id>/codex-agent/download")
    def api_account_codex_agent_download(acc_id: int):
        """下载单个账号的 Codex Agent auth.json。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            content, filename = _codex_agent_auth_for_account(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 404
        data = content.encode("utf-8")
        return Response(
            data,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/codex-agent/download-bulk")
    def api_accounts_codex_agent_download_bulk():
        """下载选中账号已生成的 Codex Agent Token，打包 ZIP。"""
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        added = []
        errors = []
        used_names = set()
        seen = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw in ids:
                try:
                    acc_id = int(raw)
                except Exception:
                    errors.append({"id": raw, "error": "ID 非法"})
                    continue
                if acc_id in seen:
                    continue
                seen.add(acc_id)
                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                try:
                    content, filename = _codex_agent_auth_for_account(acc)
                    arcname = filename
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, content)
                    added.append({"id": acc_id, "email": acc.get("email"), "filename": arcname})
                except Exception as exc:
                    errors.append({"id": acc_id, "email": acc.get("email"), "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-codex-agent",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有可下载的 Codex Agent Token", "errors": errors}), 404
        now = _dt.now()
        dl_name = f"accounts-codex-agent-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/download-cpa-bulk")
    def api_accounts_download_cpa_bulk():
        """
        从账号列表选中的账号直接到 CPA auth-files 下载 Codex CPA JSON，并打包为 ZIP。
        Body: {"account_ids": [1,2,...]} 或 {"ids": [...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text, list_cpa_codex_auth_files

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        try:
            cpa_files = list_cpa_codex_auth_files()
        except Exception as exc:
            return jsonify({"ok": False, "error": f"读取 CPA auth-files 失败: {type(exc).__name__}: {exc}"}), 502

        def _match_cpa_file(email: str, local_filename: str = "") -> dict | None:
            """在已缓存的 CPA 文件列表中匹配，避免每个账号都重新请求 auth-files。"""
            email_l = str(email or "").strip().lower()
            local_name_l = str(local_filename or "").strip().lower()
            local_stem_l = local_name_l[:-5] if local_name_l.endswith(".json") else local_name_l

            def score(item: dict) -> int:
                name_l = str(item.get("name") or "").lower()
                item_email_l = str(item.get("email") or "").lower()
                s = 0
                if local_name_l and name_l == local_name_l:
                    s = max(s, 100)
                if local_stem_l and name_l.startswith(local_stem_l):
                    s = max(s, 80)
                if email_l and item_email_l == email_l:
                    s = max(s, 70)
                if email_l and email_l in name_l:
                    s = max(s, 60)
                if local_stem_l.endswith("-cpa-callback"):
                    base = local_stem_l[:-len("-cpa-callback")]
                    if base and name_l.startswith(base + "-"):
                        s = max(s, 75)
                return s

            ranked = sorted(((score(item), item) for item in cpa_files), key=lambda x: x[0], reverse=True)
            return ranked[0][1] if ranked and ranked[0][0] > 0 else None

        # 建立 email -> 本地 codex 文件名索引；有本地文件名时传给 CPA 匹配逻辑可提升命中率。
        local_by_email: dict[str, str] = {}
        try:
            for item in db.list_codex_accounts():
                email_key = str(item.get("email") or "").strip().lower()
                fname = str(item.get("filename") or "").strip()
                if email_key and fname and email_key not in local_by_email:
                    local_by_email[email_key] = fname
        except Exception:
            local_by_email = {}

        errors = []
        added = []
        used_names = set()
        seen_ids = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw_id in ids:
                try:
                    acc_id = int(raw_id)
                except (TypeError, ValueError):
                    errors.append({"id": raw_id, "error": "ID 非法"})
                    continue
                if acc_id in seen_ids:
                    continue
                seen_ids.add(acc_id)

                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                email = str(acc.get("email") or "").strip()
                if not email:
                    errors.append({"id": acc_id, "error": "账号缺少 email"})
                    continue

                local_filename = local_by_email.get(email.lower(), "")
                try:
                    meta = _match_cpa_file(email=email, local_filename=local_filename)
                    cpa_name_hint = str((meta or {}).get("name") or "").strip()
                    if not cpa_name_hint:
                        raise RuntimeError(f"[Codex][CPA] 未在 CPA auth-files 中找到匹配的 Codex 凭证: {email}")
                    cpa_text, cpa_name, meta = download_cpa_codex_auth_text(
                        cpa_name=cpa_name_hint,
                    )
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({
                        "id": acc_id,
                        "email": email,
                        "local_filename": local_filename,
                        "cpa_filename": cpa_name,
                        "cpa_meta": meta,
                    })
                    if local_filename:
                        try:
                            db.mark_codex_exported(local_filename)
                        except Exception:
                            pass
                except Exception as exc:
                    errors.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})

            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"accounts-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        if isinstance(data, dict) and data.get("prepare"):
            download_id = _put_prepared_download(zip_bytes, dl_name, "application/zip")
            return jsonify({
                "ok": True,
                "prepared": True,
                "download_id": download_id,
                "download_url": f"/api/downloads/{download_id}",
                "filename": dl_name,
                "added_count": len(added),
                "error_count": len(errors),
            })
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    # ----------------------------------------------------------
    # 邮箱池
    # ----------------------------------------------------------
    @app.get("/api/outlook")
    def api_outlook():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        source = _pool_source_arg()
        q = str(request.args.get("q", default="") or "").strip()
        plan_filter = str(request.args.get("plan", default="") or "").strip().lower()
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        fetch_limit = 1_000_000 if (paged or q or plan_filter) else limit
        rows = _list_pool_rows(source=source, status=status, fetch_limit=fetch_limit)
        if plan_filter:
            rows = [r for r in rows if _matches_pool_plan_filter(r, plan_filter)]
        if q:
            rows = [r for r in rows if _matches_query(r, q)]
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            return jsonify(_paginate_items(rows, page=page, page_size=page_size))
        return jsonify(rows[:limit])

    @app.post("/api/accounts/pool-emails")
    def api_account_pool_emails():
        """把账号 ID 精确解析为邮箱，供账号页联动邮箱池筛选。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多关联 5000 个账号"}), 400
        emails = []
        skipped = []
        seen_ids = set()
        seen_emails = set()
        for raw in ids:
            try:
                account_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if account_id in seen_ids:
                continue
            seen_ids.add(account_id)
            account = db.get_account(account_id)
            email = str((account or {}).get("email") or "").strip()
            email_key = email.lower()
            if not account:
                skipped.append({"id": account_id, "reason": "账号不存在"})
            elif not email:
                skipped.append({"id": account_id, "reason": "账号没有邮箱"})
            elif email_key not in seen_emails:
                seen_emails.add(email_key)
                emails.append(email)
        return jsonify({"ok": True, "emails": emails, "count": len(emails), "skipped": skipped})

    @app.post("/api/outlook/filter-emails")
    def api_outlook_filter_emails():
        """按完整邮箱地址精确筛选多个邮箱池，并保留分页、来源和文本搜索。"""
        data = request.get_json(silent=True) or {}
        raw_emails = data.get("emails") or []
        if not isinstance(raw_emails, list) or not raw_emails:
            return jsonify({"ok": False, "error": "emails 必须是非空数组"}), 400
        if len(raw_emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多筛选 5000 个邮箱"}), 400
        email_set = {str(email or "").strip().lower() for email in raw_emails if str(email or "").strip()}
        source = str(data.get("source") or "all").strip()
        if source not in {"all", "outlook", "generic_api", "domain_api", "inbox_mate", "cloudflare_domain"}:
            source = "all"
        status = str(data.get("status") or "").strip() or None
        q = str(data.get("q") or "").strip()
        plan_filter = str(data.get("plan") or "").strip().lower()
        page = max(1, int(data.get("page") or 1))
        page_size = max(1, min(500, int(data.get("page_size") or 50)))
        rows = _list_pool_rows(source=source, status=status, fetch_limit=1_000_000)
        rows = [r for r in rows if str(r.get("email") or "").strip().lower() in email_set]
        if plan_filter:
            rows = [r for r in rows if _matches_pool_plan_filter(r, plan_filter)]
        if q:
            rows = [r for r in rows if _matches_query(r, q)]
        result = _paginate_items(rows, page=page, page_size=page_size)
        result["filter_email_count"] = len(email_set)
        return jsonify(result)

    @app.post("/api/outlook/copy-selected")
    def api_outlook_copy_selected():
        """Return selected mailbox records as: email----email URL----AT."""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or []
        if not isinstance(items, list) or not items:
            return jsonify({"ok": False, "error": "items 必须是非空数组"}), 400
        if len(items) > 5000:
            return jsonify({"ok": False, "error": "单次最多复制 5000 个邮箱"}), 400

        requested: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        allowed_sources = {"outlook", "generic_api", "domain_api", "inbox_mate", "cloudflare_domain"}
        for item in items:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            email = str(item.get("email") or "").strip()
            key = (source, email.lower())
            if source in allowed_sources and email and key not in seen:
                seen.add(key)
                requested.append(key)

        rows_by_key: dict[tuple[str, str], dict] = {}
        for source in allowed_sources:
            for row in _list_pool_rows(source=source, status=None, fetch_limit=1_000_000):
                email = str(row.get("email") or "").strip()
                if email:
                    rows_by_key[(source, email.lower())] = row

        lines: list[str] = []
        missing: list[dict] = []
        for source, email_key in requested:
            row = rows_by_key.get((source, email_key))
            if not row:
                missing.append({"source": source, "email": email_key})
                continue
            if source == "inbox_mate":
                lines.append("----".join([
                    str(row.get("email") or "").strip(),
                    str(row.get("password") or "").strip(),
                    str(row.get("api_base") or "").strip(),
                ]))
            else:
                # Outlook/domain-pool entries have no mailbox read URL; keep the
                # delimiter so every copied line still follows the same 3-column format.
                lines.append("----".join([
                    str(row.get("email") or "").strip(),
                    str(row.get("code_url") or "").strip(),
                    str(row.get("access_token") or "").strip(),
                ]))
        return jsonify({"ok": True, "lines": lines, "count": len(lines), "missing": missing})

    @app.post("/api/outlook/import")
    def api_outlook_import():
        """
        粘贴文本导入邮箱素材。
        Outlook：email----password----clientId----refreshToken
        通用 API：email----code_url
        域名 API：账户列表 URL、email----password 或 email----code_url
        Inbox Mate：账号: email | 密码: password
        分隔符兼容 ---- 与 ====。
        """
        data = request.get_json(silent=True) or {}
        text = data.get("text") or ""
        source = (data.get("source") or data.get("type") or "").strip().lower()
        source = {
            "inboxmate": "inbox_mate",
            "mail.com": "inbox_mate",
            "mailcom": "inbox_mate",
            "domain": "domain_api",
            "api": "generic_api",
        }.get(source, source)
        if source not in ("outlook", "generic_api", "domain_api", "inbox_mate"):
            from core.inbox_mate_mail_client import looks_like_labeled_import
            if looks_like_labeled_import(text):
                source = "inbox_mate"
        if source not in ("outlook", "generic_api", "domain_api", "inbox_mate"):
            return jsonify({"ok": False, "error": "导入时请选择具体类型：Outlook、通用 API、域名 API 或 Inbox Mate"}), 400
        as_registered = bool(data.get("as_registered", False))
        records = []
        if source == "domain_api":
            try:
                from core.domain_api_mail_client import parse_import_text
                records = parse_import_text(text)
            except Exception as exc:
                return jsonify({"ok": False, "error": f"域名 API 素材读取失败：{type(exc).__name__}: {exc}"}), 400
        elif source == "inbox_mate":
            try:
                from core.inbox_mate_mail_client import parse_import_text
                records = parse_import_text(text)
            except Exception as exc:
                return jsonify({"ok": False, "error": f"Inbox Mate 素材读取失败：{type(exc).__name__}: {exc}"}), 400
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("----") if "----" in line else line.split("====")
                parts = [p.strip() for p in parts]
                if source == "generic_api":
                    if len(parts) < 2:
                        continue
                    records.append({
                        "email": parts[0],
                        "code_url": parts[1],
                        "access_token": parts[2] if len(parts) > 2 else "",
                        "totp_secret": parts[3] if len(parts) > 3 else "",
                    })
                    continue
                if len(parts) < 4:
                    continue
                records.append({
                    "email": parts[0],
                    "password": parts[1],
                    "client_id": parts[2],
                    "refresh_token": parts[3],
                    "access_token": parts[4] if len(parts) > 4 else "",
                    "totp_secret": parts[5] if len(parts) > 5 else "",
                })
        if not records:
            need = (
                "账户列表 URL、邮箱----密码或邮箱----取码地址"
                if source == "domain_api"
                else "账号: email | 密码: password"
                if source == "inbox_mate"
                else "2 段：邮箱----取码地址"
                if source == "generic_api"
                else "4 段：email----password----clientId----refreshToken"
            )
            return jsonify({"ok": False, "error": f"未解析到有效邮箱行（需 {need}，---- 或 ==== 分隔）"}), 400
        if as_registered:
            inserted, skipped = db.import_registered_email_accounts(records, source=source)
        elif source in ("generic_api", "domain_api", "inbox_mate"):
            inserted, skipped = db.import_generic_api_emails(records)
        else:
            inserted, skipped = db.import_outlook_accounts(records)
        return jsonify({
            "ok": True,
            "inserted": inserted,
            "skipped": skipped,
            "parsed": len(records),
            "domains": sorted({str(row.get("email_domain") or "") for row in records if row.get("email_domain")}),
            "as_registered": as_registered,
            "source": source,
        })

    @app.post("/api/outlook/status")
    def api_outlook_status():
        """手动改邮箱状态：body {email, status, note?, source?}。status ∈ available/used/failed/disabled。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        if status == "available" and db.get_account_by_email(email) is not None:
            return jsonify({"ok": False, "error": "邮箱已在账号池中，不能恢复为可用"}), 409
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        if source in ("generic_api", "domain_api", "inbox_mate"):
            db.release_generic_api_email(email, status=status, note=data.get("note"))
        elif source == "cloudflare_domain":
            db.release_domain_email(email, status=status, note=data.get("note"))
        else:
            db.release_outlook(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/outlook/status-bulk")
    def api_outlook_status_bulk():
        """批量修改邮箱状态。Body {items:[{email,source}], status, note?}。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or data.get("emails") or []
        status = (data.get("status") or "").strip()
        note = data.get("note")
        default_source = (data.get("source") or _pool_source_arg()).strip()
        if status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "status 非法"}), 400
        if not isinstance(items, list) or not items:
            return jsonify({"ok": False, "error": "items/emails 必须是非空数组"}), 400
        if len(items) > 5000:
            return jsonify({"ok": False, "error": "单次最多操作 5000 个邮箱"}), 400

        updated = []
        skipped = []
        seen = set()
        for raw_item in items:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or default_source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = default_source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            if status == "available" and db.get_account_by_email(email) is not None:
                skipped.append({"email": email, "source": item_source, "reason": "邮箱已在账号池中"})
                continue
            try:
                if item_source in ("generic_api", "domain_api", "inbox_mate"):
                    db.release_generic_api_email(email, status=status, note=note)
                elif item_source == "cloudflare_domain":
                    db.release_domain_email(email, status=status, note=note)
                else:
                    db.release_outlook(email, status=status, note=note)
                updated.append({"email": email, "source": item_source, "status": status})
            except Exception as exc:
                skipped.append({"email": email, "source": item_source, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
        })

    @app.post("/api/outlook/delete")
    def api_outlook_delete():
        """从邮箱池彻底删除一个邮箱：body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        deleted = (
            db.delete_generic_api_email(email)
            if source in ("generic_api", "domain_api", "inbox_mate")
            else db.delete_domain_email(email)
            if source == "cloudflare_domain"
            else db.delete_outlook(email)
        )
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/outlook/delete-bulk")
    def api_outlook_delete_bulk():
        """从邮箱池批量彻底删除邮箱：body {emails: [...]}。"""
        data = request.get_json(silent=True) or {}
        source = _pool_source_arg()
        emails = data.get("items") or data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"ok": False, "error": "emails/items 必须是非空数组"}), 400
        if len(emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个邮箱"}), 400

        deleted: list[str] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        for raw_item in emails:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            deleted_ok = (
                db.delete_generic_api_email(email)
                if item_source in ("generic_api", "domain_api", "inbox_mate")
                else db.delete_domain_email(email)
                if item_source == "cloudflare_domain"
                else db.delete_outlook(email)
            )
            if deleted_ok:
                deleted.append({"email": email, "source": item_source})
            else:
                skipped.append({"email": email, "reason": "邮箱不存在"})

        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    # ----------------------------------------------------------
    # FlySMS / iCloud 邮件状态检测池
    # ----------------------------------------------------------
    @app.get("/api/mail-status")
    def api_mail_status_list():
        status = str(request.args.get("status") or "").strip().lower()
        q = str(request.args.get("q") or "").strip().lower()
        page = max(1, request.args.get("page", default=1, type=int) or 1)
        page_size = max(1, min(500, request.args.get("page_size", default=50, type=int) or 50))
        rows = db.list_mail_status_pool(status=status or None, limit=100000)
        if q:
            rows = [row for row in rows if q in "\n".join(str(v) for v in row.values()).lower()]
        return jsonify(_paginate_items(rows, page=page, page_size=page_size))

    @app.post("/api/mail-status/filter-emails")
    def api_mail_status_filter_emails():
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"ok": False, "error": "emails 必须是非空数组"}), 400
        email_set = {str(value or "").strip().lower() for value in emails if str(value or "").strip()}
        status = str(data.get("status") or "").strip().lower()
        q = str(data.get("q") or "").strip().lower()
        page = max(1, int(data.get("page") or 1))
        page_size = max(1, min(500, int(data.get("page_size") or 50)))
        rows = db.list_mail_status_pool(status=status or None, limit=100000)
        rows = [row for row in rows if str(row.get("email") or "").strip().lower() in email_set]
        if q:
            rows = [row for row in rows if q in "\n".join(str(v) for v in row.values()).lower()]
        result = _paginate_items(rows, page=page, page_size=page_size)
        result["filter_email_count"] = len(email_set)
        return jsonify(result)

    @app.post("/api/mail-status/add")
    def api_mail_status_add():
        data = request.get_json(silent=True) or {}
        raw_emails = data.get("emails") or []
        account_ids = data.get("account_ids") or data.get("ids") or []
        all_accounts = bool(data.get("all_accounts"))
        if not isinstance(raw_emails, list) or not isinstance(account_ids, list):
            return jsonify({"ok": False, "error": "emails/account_ids 必须是数组"}), 400
        emails = [str(value or "").strip() for value in raw_emails]
        if all_accounts:
            for account in db.list_accounts(limit=1_000_000, archived=False):
                if account.get("email"):
                    emails.append(str(account.get("email")).strip())
        for raw_id in account_ids:
            try:
                account = db.get_account(int(raw_id))
            except (TypeError, ValueError):
                account = None
            if account and account.get("email"):
                emails.append(str(account.get("email")).strip())
        emails = list(dict.fromkeys(email for email in emails if email))
        if not emails:
            return jsonify({"ok": False, "error": "请先选择或输入邮箱"}), 400
        if len(emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多添加 5000 个邮箱"}), 400
        added, skipped = db.add_mail_status_emails(emails)
        return jsonify({
            "ok": True, "added": added, "added_count": len(added),
            "skipped": skipped, "skipped_count": len(skipped),
            "target_emails": emails, "target_count": len(emails),
        })

    @app.post("/api/mail-status/check")
    def api_mail_status_check():
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"ok": False, "error": "emails 必须是非空数组"}), 400
        emails = list(dict.fromkeys(str(email or "").strip() for email in emails if str(email or "").strip()))
        if len(emails) > 500:
            return jsonify({"ok": False, "error": "单次最多检测 500 个邮箱"}), 400
        try:
            workers = _positive_worker_count(data.get("workers"), 10)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是正整数"}), 400

        entries = []
        missing = []
        for email in emails:
            entry = db.mark_mail_status_checking(email)
            if entry and entry.get("code_url"):
                entries.append(entry)
            else:
                missing.append({"email": email, "error": "邮件检测池中不存在或没有读取链接"})

        def check_one(entry: dict) -> dict:
            email = entry.get("email") or ""
            try:
                result = detect_mailbox_status(entry.get("code_url") or "", limit=50)
            except Exception as exc:
                result = {
                    "status": "error", "label": "检测失败", "evidence": "",
                    "error": f"{type(exc).__name__}: {exc}", "message_count": 0,
                    "subject": "", "mail_date": "", "mail_id": "", "mail_source": "",
                }
            updated = db.update_mail_status_result(email, result) or {"email": email, **result}
            if result.get("status") in {"plus", "nonplus", "banned"}:
                db.sync_account_mail_status(email, result)
            account = db.get_account_by_email(email)
            if account and account.get("mail_plus_promoted"):
                try:
                    gc_registration_service.close_plus_window_for_account(int(account["id"]))
                except Exception:
                    logger.exception("邮箱确认 Plus 后关闭 GC 窗口失败：account_id=%s", account.get("id"))
            return updated

        results = []
        if entries:
            with ThreadPoolExecutor(max_workers=min(workers, len(entries)), thread_name_prefix="mail-status") as pool:
                futures = [pool.submit(check_one, entry) for entry in entries]
                for future in as_completed(futures):
                    results.append(future.result())
        return jsonify({
            "ok": True, "items": results, "checked_count": len(results),
            "errors": missing, "error_count": len(missing) + sum(1 for row in results if row.get("status") == "error"),
        })

    @app.post("/api/mail-status/delete")
    def api_mail_status_delete():
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"ok": False, "error": "emails 必须是非空数组"}), 400
        removed = db.delete_mail_status_emails(emails)
        return jsonify({"ok": True, "deleted_count": removed})

    # ----------------------------------------------------------
    # 域名邮箱池（Cloudflare 域名邮箱模式）
    # ----------------------------------------------------------
    @app.get("/api/domain-pool")
    def api_domain_pool():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        return jsonify(db.list_domain_email_pool(status=status, limit=limit))

    @app.post("/api/domain-pool/status")
    def api_domain_pool_status():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        db.release_domain_email(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/domain-pool/delete")
    def api_domain_pool_delete():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        deleted = db.delete_domain_email(email)
        return jsonify({"ok": True, "deleted": deleted})

    # ----------------------------------------------------------
    # Codex 授权账号（CPA 兼容凭证）
    # ----------------------------------------------------------
    @app.get("/api/codex")
    def api_codex_list():
        rows = db.list_codex_accounts()
        q = str(request.args.get("q", default="") or "").strip()
        if q:
            rows = [r for r in rows if _matches_query(r, q)]
        limit = request.args.get("limit", default=500, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = _paginate_items(rows, page=page, page_size=page_size)
            result["accounts"] = result.pop("items")
            result["summary"] = db.codex_accounts_summary()
            return jsonify(result)
        return jsonify({
            "summary": db.codex_accounts_summary(),
            "accounts": rows[:limit],
        })

    @app.get("/api/codex/download/<path:filename>")
    def api_codex_download(filename: str):
        """
        下载一个 CPA 兼容的 codex-*.json 文件，下载即标记为已导出（计数+1）。
        前端通过浏览器原生下载触发（a 标签 / window.location）。
        """
        try:
            content, fname = db.read_codex_credential(filename)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        db.mark_codex_exported(fname)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/codex/download-from-cpa/<path:filename>")
    def api_codex_download_from_cpa(filename: str):
        """按本地 codex 文件/回执匹配 CPA auth-files，并从 CPA 下载实际 Codex JSON。"""
        try:
            content, fname = db.read_codex_credential(filename)
            import json as _json
            try:
                local = _json.loads(content)
            except Exception:
                local = {}
            email = str(local.get("email") or "").strip()
            from core.codex_oauth import download_cpa_codex_auth_text
            cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=fname)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
        db.mark_codex_exported(fname)
        return Response(
            cpa_text,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{cpa_name}"'},
        )

    @app.post("/api/codex/download-bulk-from-cpa")
    def api_codex_download_bulk_from_cpa():
        """
        批量从 CPA 下载选中的 Codex 凭证，打包成 zip；zip 内每个文件都是 CPA 原始 JSON。
        Body: {"filenames": ["codex-xxx-cpa-callback.json", ...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        errors = []
        added = []
        used_names = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fname in filenames:
                if not isinstance(fname, str):
                    errors.append({"filename": str(fname), "error": "非字符串"})
                    continue
                try:
                    content, real_fname = db.read_codex_credential(fname)
                    try:
                        local = _json.loads(content)
                    except Exception:
                        local = {}
                    email = str(local.get("email") or "").strip()
                    cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=real_fname)
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({"local_filename": real_fname, "cpa_filename": cpa_name})
                    db.mark_codex_exported(real_fname)
                except Exception as exc:
                    errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"codex-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        return Response(
            buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/download-bulk")
    def api_codex_download_bulk():
        """
        批量下载选中的 codex 凭证，打包到一个 JSON 文件里。

        Body: {"filenames": ["codex-xxx.json", ...]}
        响应：聚合 JSON（attachment 触发浏览器下载），结构：
            {
              "exported_at": "...",
              "count": N,
              "credentials": [{"filename": "...", "data": {...原始凭证内容...}}, ...],
              "errors": [...]   // 仅当部分失败时出现
            }
        注意：聚合格式**不能直接被 CPA 读**，CPA 是按单文件加载 auths/ 目录的。
              本接口主要用途是备份 / 跨机迁移 / 二次处理。
        每个成功的凭证会自动标记 mark_exported（计数+1）。
        """
        import json as _json
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        bundle = []
        errors = []
        for fname in filenames:
            if not isinstance(fname, str):
                errors.append({"filename": str(fname), "error": "非字符串"})
                continue
            try:
                content, real_fname = db.read_codex_credential(fname)
                parsed = _json.loads(content)
                bundle.append({"filename": real_fname, "data": parsed})
                db.mark_codex_exported(real_fname)
            except Exception as exc:
                errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})

        now = _dt.now()
        result = {
            "exported_at": now.isoformat(timespec="seconds"),
            "count": len(bundle),
            "credentials": bundle,
        }
        if errors:
            result["errors"] = errors

        dl_name = f"codex-bulk-{now.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            _json.dumps(result, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/reset-export")
    def api_codex_reset_export():
        """清掉某个 codex 凭证的导出状态（重新标为未导出）。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            db.reset_codex_exported(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/codex/delete")
    def api_codex_delete():
        """删除一个 codex 凭证文件。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            deleted = db.delete_codex_credential(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not deleted:
            return jsonify({"ok": False, "error": "凭证文件不存在"}), 404
        return jsonify({"ok": True, "deleted": fname})

    @app.post("/api/codex/delete-bulk")
    def api_codex_delete_bulk():
        """批量删除 codex 凭证文件。body {filenames:[...]}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个"}), 400
        deleted = []
        skipped = []
        seen = set()
        for fname in filenames:
            fname = str(fname or "").strip()
            if not fname or fname in seen:
                continue
            seen.add(fname)
            try:
                ok = db.delete_codex_credential(fname)
                if ok:
                    deleted.append(fname)
                else:
                    skipped.append({"filename": fname, "reason": "文件不存在"})
            except Exception as exc:
                skipped.append({"filename": fname, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    def _reserve_codex_retry(email: str) -> bool:
        """进程内防重复占位；成功返回 True。"""
        return codex_retry_service.reserve(email)

    def _release_codex_retry(email: str) -> None:
        codex_retry_service.release(email)

    def _run_codex_retry_worker(email: str, *, batch_label: str | None = None, clear_log: bool = True) -> None:
        """执行一个账号的 Codex 补跑。调用前必须已经 reserve。"""
        codex_retry_service.run_worker(email, batch_label=batch_label, clear_log=clear_log)


    @app.post("/api/codex/stop")
    def api_codex_stop():
        """停止单个 Codex 补跑。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        result = codex_retry_service.request_stop(email)
        status = int(result.pop("status", 200) or 200)
        return jsonify(result), status

    @app.post("/api/codex/stop-bulk")
    def api_codex_stop_bulk():
        """批量停止 Codex 补跑。Body {emails:[...]} 或 {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        ids = data.get("account_ids") or data.get("ids") or []
        targets = []
        if isinstance(emails, list) and emails:
            targets = [str(x or "").strip() for x in emails]
        elif isinstance(ids, list) and ids:
            for raw in ids:
                try:
                    acc = db.get_account(int(raw))
                except Exception:
                    acc = None
                if acc and acc.get("email"):
                    targets.append(str(acc.get("email") or "").strip())
        else:
            return jsonify({"ok": False, "error": "emails 或 account_ids 必须是非空数组"}), 400
        if len(targets) > 500:
            return jsonify({"ok": False, "error": "单次最多停止 500 个"}), 400
        stopped = []
        skipped = []
        seen = set()
        for email in targets:
            key = email.lower()
            if not email or key in seen:
                continue
            seen.add(key)
            acc = db.get_account_by_email(email)
            if acc is None:
                skipped.append({"email": email, "reason": "账号不存在"})
                continue
            if (acc.get("codex_status") or "") != "retrying" and not codex_retry_service.is_retrying(email):
                skipped.append({"email": email, "reason": "未处于补跑中"})
                continue
            r = codex_retry_service.request_stop(email)
            if r.get("ok"):
                stopped.append({"email": email, "injected": r.get("injected"), "running": r.get("running")})
            else:
                skipped.append({"email": email, "reason": r.get("error") or "停止失败"})
        return jsonify({"ok": True, "stopped": stopped, "stopped_count": len(stopped), "skipped": skipped})

    @app.post("/api/codex/reset-retrying")
    def api_codex_reset_retrying():
        """手动重置某账号的 Codex 补跑中状态。Body {email, status?}。"""
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        raw_status = (data.get("status") or "failed").strip().lower()
        if raw_status in ("", "none", "null", "clear"):
            raw_status = "empty"
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        if raw_status not in ("failed", "skipped", "empty"):
            return jsonify({"ok": False, "error": "status 仅支持 failed/skipped/empty"}), 400

        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        new_status = "" if raw_status == "empty" else raw_status
        err = None if raw_status == "empty" else "用户手动重置补跑中状态"
        ok = db.update_account_codex_status(email, new_status, err)
        if not ok:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        _release_codex_retry(email)

        try:
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                ts = _dt.now().strftime("%H:%M:%S")
                shown = new_status or "空"
                f.write(f"{ts} [WARNING] [Codex 补跑] 用户手动重置补跑中状态，当前状态={shown}\n")
        except Exception:
            logger.exception("写入 Codex 补跑重置日志失败")

        return jsonify({"ok": True, "message": "已重置补跑中状态", "status": new_status})

    @app.post("/api/codex/retry")
    def api_codex_retry():
        """手动补跑某账号的 Codex 授权。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        if (acc.get("codex_status") or "") == "deactivated":
            return jsonify({"ok": False, "error": "账号已废号，不能补跑 Codex"}), 409
        if not _reserve_codex_retry(email):
            return jsonify({"ok": False, "error": "该账号正在补跑中，请稍候"}), 409

        db.update_account_codex_status(email, "retrying", None)
        threading.Thread(
            target=_run_codex_retry_worker,
            kwargs={"email": email, "clear_log": True},
            name=f"codex-retry-{email}",
            daemon=True,
        ).start()
        return jsonify({"ok": True, "message": "已在后台开始补跑，~1-2 分钟后刷新查看"})

    @app.post("/api/codex/retry-bulk")
    def api_codex_retry_bulk():
        """批量补跑 Codex。Body {account_ids:[...], workers: 正整数}。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        workers = data.get("workers", 10)
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        try:
            workers = _positive_worker_count(workers, 10)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是正整数"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多选择 500 个账号"}), 400

        selected = []
        skipped = []
        seen_ids = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = (acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            if (acc.get("codex_status") or "") == "deactivated":
                skipped.append({"id": acc_id, "email": email, "reason": "账号已废号"})
                continue
            if not _reserve_codex_retry(email):
                skipped.append({"id": acc_id, "email": email, "reason": "正在补跑中"})
                continue
            selected.append({"id": acc_id, "email": email})

        if not selected:
            return jsonify({"ok": False, "error": "没有可补跑的账号", "skipped": skipped}), 409

        batch_id = _dt.now().strftime("%Y%m%d-%H%M%S")
        for item in selected:
            email = item["email"]
            db.update_account_codex_status(email, "retrying", None)
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"{_dt.now().strftime('%H:%M:%S')} [INFO] [Codex 批量补跑] 已加入批量任务 batch={batch_id} workers={workers}，等待线程执行\n",
                encoding="utf-8",
            )

        def _bulk_runner(items: list[dict], max_workers: int, batch: str):
            logger.info(f"[Codex 批量补跑] 启动 batch={batch} count={len(items)} workers={max_workers}")
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"codex-bulk-{batch}") as ex:
                futures = [ex.submit(_run_codex_retry_worker, it["email"], batch_label=f"{batch} #{idx}/{len(items)}", clear_log=False) for idx, it in enumerate(items, 1)]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception:
                        logger.exception(f"[Codex 批量补跑] 子任务异常 batch={batch}")
            logger.info(f"[Codex 批量补跑] 完成 batch={batch}")

        threading.Thread(
            target=_bulk_runner,
            args=(selected, workers, batch_id),
            name=f"codex-bulk-dispatch-{batch_id}",
            daemon=True,
        ).start()
        return jsonify({
            "ok": True,
            "message": f"已开始批量补跑 {len(selected)} 个账号，并发 {workers}",
            "started": selected,
            "started_count": len(selected),
            "skipped": skipped,
            "batch_id": batch_id,
        })

    @app.get("/api/codex/retry-log")
    def api_codex_retry_log():
        """读取某邮箱最近一次补跑的日志。?email=xxx"""
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = codex_retry_service.log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": False})
        max_bytes = 50_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": codex_retry_service.is_retrying(email),
        })

    @app.get("/api/accounts/live-check-log")
    def api_account_live_check_log():
        """读取某邮箱最近一次查活日志。?email=xxx"""
        from core import account_liveness
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = account_liveness.log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": live_check_service.is_checking(email)})
        max_bytes = 80_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": live_check_service.is_checking(email),
        })

    # ----------------------------------------------------------
    # 注册任务
    # ----------------------------------------------------------
    @app.get("/api/jobs")
    def api_jobs():
        limit = request.args.get("limit", default=100, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        fetch_limit = 1_000_000 if (paged or page_arg is not None or page_size_arg is not None) else limit
        from config import email as _email_cfg
        manual_otp_required = not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True))
        rows = db.list_jobs(limit=fetch_limit)
        for row in rows:
            row["manual_otp_required"] = manual_otp_required
            row.update(svc.get_retry_info(row))
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = _paginate_items(rows, page=page, page_size=page_size)
            result["items"] = [_compact_job_for_list(r) for r in (result.get("items") or [])]
            result["status_counts"] = _job_status_counts(rows)
            result["compact"] = True
            return jsonify(result)
        return jsonify(rows)

    @app.post("/api/jobs")
    def api_jobs_create():
        """启动批量注册，或将邮箱池选中的指定邮箱直接推送为注册任务。"""
        data = request.get_json(silent=True) or {}
        selected_email_items = data.get("email_items") or []
        if selected_email_items and not isinstance(selected_email_items, list):
            return jsonify({"ok": False, "error": "email_items 必须是数组"}), 400
        if isinstance(selected_email_items, list) and len(selected_email_items) > 200:
            return jsonify({"ok": False, "error": "单次最多推送 200 个邮箱"}), 400
        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "count 非法"}), 400
        if count < 1 or count > 200:
            return jsonify({"ok": False, "error": "count 需在 1~200 之间"}), 400

        # workers 控制本次新提交任务使用的线程池；若和上次不同，服务层会为新任务切换到新池。
        try:
            workers = _positive_worker_count(data.get("workers"), 1)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是正整数"}), 400

        from config import roxybrowser as _roxy_cfg
        gc_mode = bool(getattr(_roxy_cfg, "GC_REGISTRATION_MODE", False))
        driver_mode = str(getattr(_roxy_cfg, "REGISTRATION_DRIVER", "") or "").strip().lower()
        if gc_mode and driver_mode not in {"roxy", "roxybrowser", "fingerprint", "browser"}:
            return jsonify({
                "ok": False,
                "error": "GC 注册模式只支持 Roxy 注册驱动；请在配置中把注册驱动设为 roxy",
            }), 409

        # 提交前先确认池里有足够可用邮箱，给前端一个温和提示（不阻断）
        from config import email as _email_cfg
        from config import register as _register_cfg
        from core.email_provider import parse_email_sources
        requested_proxy_mode = str(data.get("proxy_mode") or "").strip().lower()
        if requested_proxy_mode not in {"", "local"}:
            return jsonify({"ok": False, "error": "proxy_mode 只支持 local"}), 400
        if selected_email_items:
            allowed_sources = {"outlook", "generic_api", "domain_api", "inbox_mate", "cloudflare_domain"}
            normalized_items = []
            seen_items = set()
            for item in selected_email_items:
                if not isinstance(item, dict):
                    return jsonify({"ok": False, "error": "email_items 元素必须包含 source 和 email"}), 400
                source = str(item.get("source") or "").strip().lower()
                email = str(item.get("email") or "").strip()
                key = (source, email.lower())
                if source not in allowed_sources or not email or "@" not in email:
                    return jsonify({"ok": False, "error": "选中邮箱或来源非法"}), 400
                if key not in seen_items:
                    seen_items.add(key)
                    normalized_items.append({"source": source, "email": email})
            if not normalized_items:
                return jsonify({"ok": False, "error": "没有可推送的邮箱"}), 400
            submit_kwargs = {"workers": workers, "email_items": normalized_items}
            if requested_proxy_mode:
                submit_kwargs["proxy_mode"] = requested_proxy_mode
            jobs = svc.submit_registration(**submit_kwargs)
            submitted_keys = {
                (
                    str(job.get("email_source") or "").strip().lower(),
                    str(job.get("email") or "").strip().lower(),
                )
                for job in jobs
                if isinstance(job, dict) and str(job.get("email") or "").strip()
            }
            skipped = [
                {
                    **item,
                    "reason": "邮箱已被领取、不可用或来源不匹配",
                }
                for item in normalized_items
                if (item["source"], item["email"].lower()) not in submitted_keys
            ]
            warning = f"{len(skipped)} 个邮箱未提交，仍保留选中状态" if skipped else ""
            return jsonify({
                "ok": True,
                "submitted": len(jobs),
                "requested_count": len(normalized_items),
                "jobs": jobs,
                "skipped": skipped,
                "warning": warning,
                "workers": workers,
                "gc_mode": gc_mode,
            })
        if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True)):
            reg_email = str(getattr(_register_cfg, "REGISTER_EMAIL", "") or "").strip()
            if not reg_email:
                return jsonify({
                    "ok": False,
                    "error": "手动模式未配置 REGISTER_EMAIL。请到配置页填写「手动注册邮箱」，或开启自动取邮箱+收码。",
                }), 400
            if count > 1:
                return jsonify({
                    "ok": False,
                    "error": "手动模式建议每次只跑 1 个任务（同一 REGISTER_EMAIL）。请把数量设为 1。",
                }), 400
            submit_kwargs = {"count": count, "workers": workers}
            if requested_proxy_mode:
                submit_kwargs["proxy_mode"] = requested_proxy_mode
            jobs = svc.submit_registration(**submit_kwargs)
            return jsonify({
                "ok": True,
                "submitted": len(jobs),
                "jobs": jobs,
                "warning": f"手动 OTP 模式：将使用 {reg_email}；验证码请在任务页提交",
                "workers": workers,
                "gc_mode": gc_mode,
            })
        requested_source = str(data.get("email_source") or "").strip()
        if requested_source:
            sources = parse_email_sources(requested_source)
            if len(sources) != 1 or sources[0] != requested_source:
                return jsonify({"ok": False, "error": "email_source 只支持一个有效邮箱来源"}), 400
        else:
            sources = parse_email_sources(_email_cfg.EMAIL_SOURCE)
        if "gptmail" in sources:
            api_key = str(getattr(_email_cfg, "GPTMAIL_API_KEY", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 gptmail 邮箱来源，请填写 GPTMail API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudflare" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDFLARE_API_BASE", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudflare 邮箱来源，请填写 Cloudflare API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            auth_mode = str(getattr(_email_cfg, "CLOUDFLARE_AUTH_MODE", "none") or "none").strip().lower()
            accounts_path = str(getattr(_email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address") or "").strip().lower()
            api_key = str(getattr(_email_cfg, "CLOUDFLARE_API_KEY", "") or "").strip()
            needs_key = auth_mode in ("x-admin-auth", "bearer", "x-api-key", "query-key") or accounts_path.rstrip("/").endswith("/admin/new_address")
            if needs_key and not api_key:
                return jsonify({
                    "ok": False,
                    "error": "Cloudflare admin/鉴权模式需要填写 Cloudflare API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "mailnest" in sources:
            api_key = str(getattr(_email_cfg, "MAIL_NEST_API_KEY", "") or "").strip()
            project_code = str(getattr(_email_cfg, "MAIL_NEST_PROJECT_CODE", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest API Key（配置 → 邮箱 / OTP）。",
                }), 400
            if not project_code:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest 项目代码（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudmail" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDMAIL_API_BASE", "") or "").strip()
            token = str(getattr(_email_cfg, "CLOUDMAIL_AUTH_TOKEN", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            if not token:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail Token（配置 → 邮箱 / OTP）。",
                }), 400
        if "gptmail" in sources or "mailnest" in sources or "cloudmail" in sources or "cloudflare" in sources:
            # 临时邮箱在任务开始时动态生成，不需要本地邮箱池容量提示。
            warning = ""
        elif "cloudflare_domain" in sources:
            pool = db.domain_email_pool_summary()
            warning = ""
            if sources == ["cloudflare_domain"] and pool.get("available", 0) < count:
                warning = f"域名邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会自动生成"
        elif sources == ["generic_api"]:
            pool = db.generic_api_email_pool_summary(provider="generic_api")
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"通用 API 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif sources == ["domain_api"]:
            pool = db.generic_api_email_pool_summary(provider="domain_api")
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"域名 API 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif sources == ["inbox_mate"]:
            pool = db.generic_api_email_pool_summary(provider="inbox_mate")
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"Inbox Mate 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif len(sources) > 1:
            available = 0
            if "outlook" in sources:
                available += db.outlook_pool_summary().get("available", 0)
            if "generic_api" in sources:
                available += db.generic_api_email_pool_summary(provider="generic_api").get("available", 0)
            if "domain_api" in sources:
                available += db.generic_api_email_pool_summary(provider="domain_api").get("available", 0)
            if "inbox_mate" in sources:
                available += db.generic_api_email_pool_summary(provider="inbox_mate").get("available", 0)
            warning = ""
            if available < count:
                warning = f"多个邮箱池合计仅 {available} 个可用，少于任务数 {count}，不足的会失败"
        else:
            pool = db.outlook_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"可用邮箱仅 {pool.get('available', 0)} 个，少于任务数 {count}，不足的会失败"
        submit_kwargs = {"count": count, "workers": workers}
        if requested_source:
            submit_kwargs["email_source"] = requested_source
        if requested_proxy_mode:
            submit_kwargs["proxy_mode"] = requested_proxy_mode
        jobs = svc.submit_registration(**submit_kwargs)
        return jsonify({"ok": True, "submitted": len(jobs), "jobs": jobs, "warning": warning, "workers": workers, "gc_mode": gc_mode})

    @app.get("/api/manual-otp/waiting")
    def api_manual_otp_waiting():
        """列出当前正在等待手动验证码的邮箱。"""
        from core.manual_otp import list_waiting
        return jsonify({"ok": True, "waiting": list_waiting()})

    @app.post("/api/manual-otp")
    def api_manual_otp_submit():
        """提交手动邮箱验证码。Body: {email, code} 或 {job_id, code}。"""
        from core.manual_otp import submit_manual_otp
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or data.get("otp") or "").strip()
        email = (data.get("email") or "").strip()
        job_id = data.get("job_id")
        if not email and job_id is not None:
            job = db.get_job(int(job_id))
            email = (job or {}).get("email") or ""
        if not email:
            return jsonify({"ok": False, "error": "email/job_id 缺失"}), 400
        try:
            result = submit_manual_otp(email, code)
            return jsonify(result)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/jobs/cancel-pending")
    def api_jobs_cancel_pending():
        """取消所有还在排队（status=pending）的任务。已在 running 的不动。"""
        cancelled = svc.cancel_pending_jobs()
        return jsonify({"ok": True, "cancelled": cancelled})

    @app.post("/api/jobs/<int:job_id>/stop")
    def api_job_stop(job_id: int):
        """手动停止单个注册任务。pending 取消；running 发送停止信号。"""
        result = svc.request_stop_job(job_id)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error") or "停止失败"}), int(result.get("status") or 400)
        return jsonify(result)

    @app.get("/api/jobs/<int:job_id>/gc/access-token")
    def api_job_gc_access_token(job_id: int):
        result = gc_registration_service.access_token_for_job(job_id)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error")}), int(result.get("status") or 400)
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    @app.post("/api/jobs/<int:job_id>/gc/check-plus/start")
    def api_job_gc_check_plus_start(job_id: int):
        result = gc_registration_service.start_plan_poll(job_id)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error")}), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/gc/check-plus/start-all")
    def api_jobs_gc_check_plus_start_all():
        return jsonify(gc_registration_service.start_all_plan_polls())

    @app.post("/api/jobs/<int:job_id>/gc/check-plus/stop")
    def api_job_gc_check_plus_stop(job_id: int):
        result = gc_registration_service.stop_plan_poll(job_id)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error")}), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/<int:job_id>/gc/window/close")
    def api_job_gc_window_close(job_id: int):
        result = gc_registration_service.close_job_window(job_id, reason="manual")
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error")}), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/<int:job_id>/retry")
    def api_job_retry(job_id: int):
        """重试失败/停止/取消任务；服务端自动判断完整注册或 Codex 补跑。"""
        data = request.get_json(silent=True) or {}
        try:
            workers = _positive_worker_count(data.get("workers"), svc.get_executor_workers())
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是正整数"}), 400
        result = svc.retry_job(job_id, workers=workers)
        if not result.get("ok"):
            return jsonify(result), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/retry-bulk")
    def api_jobs_retry_bulk():
        """批量重试任务；不支持项逐条跳过并返回原因。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 500:
            return jsonify({"ok": False, "error": "单次最多重试 500 个任务"}), 400
        try:
            workers = _positive_worker_count(data.get("workers"), svc.get_executor_workers())
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是正整数"}), 400

        started: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                one_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if one_id in seen:
                continue
            seen.add(one_id)
            result = svc.retry_job(one_id, workers=workers)
            if not result.get("ok"):
                skipped.append({"id": one_id, "reason": result.get("error") or "不能重试"})
            elif result.get("reused"):
                reused.append(result)
            else:
                started.append(result)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "reused": reused,
            "reused_count": len(reused),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "workers": workers,
        })

    @app.post("/api/jobs/<int:job_id>/delete")
    def api_job_delete(job_id: int):
        """删除一个任务记录。运行中的任务不允许删除；排队任务删除后执行前会自动跳过。"""
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if job.get("status") in ("running", "stopping"):
            return jsonify({"ok": False, "error": "运行中的任务不能删除，请等待完成后再删"}), 409
        if job.get("gc_window_state") == "open":
            return jsonify({"ok": False, "error": "该 GC 任务窗口仍打开，请先点“关闭并删除窗口”"}), 409
        deleted = db.delete_job(job_id, delete_log=True, allow_running=False)
        if not deleted:
            return jsonify({"ok": False, "error": "任务不存在或已开始运行"}), 409
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/jobs/delete-bulk")
    def api_jobs_delete_bulk():
        """直接删除选中的任务记录和日志，不按任务状态或窗口状态区分。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个任务"}), 400

        deleted: list[int] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                job_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if job_id in seen:
                continue
            seen.add(job_id)

            if db.delete_job(job_id, delete_log=True, allow_running=True):
                deleted.append(job_id)
            else:
                skipped.append({"id": job_id, "reason": "任务不存在"})

        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    @app.get("/api/jobs/<int:job_id>/log")
    def api_job_log(job_id: int):
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        return jsonify({
            "ok": True,
            "job": job,
            "log": svc.read_job_log(job_id),
        })

    # ----------------------------------------------------------
    # RoxyBrowser 辅助接口
    # ----------------------------------------------------------
    @app.get("/api/roxy/workspaces")
    def api_roxy_workspaces():
        try:
            from core.roxybrowser_client import RoxyBrowserClient
            result = RoxyBrowserClient().list_workspaces()
            return jsonify(result)
        except Exception as exc:
            logger.exception("获取 Roxy 团队/工作区失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    # ----------------------------------------------------------
    # 配置读写
    # ----------------------------------------------------------
    @app.post("/api/proxy-api/test")
    def api_proxy_api_test():
        """获取一条 API 代理并验证 SOCKS/HTTP 出口，不保存前端尚未提交的配置。"""
        data = request.get_json(silent=True) or {}
        try:
            from config import proxy as proxy_cfg
            from curl_cffi.requests import Session as CurlSession

            api_url = str(data.get("api_url") or getattr(proxy_cfg, "PROXY_API_URL", "") or "").strip()
            protocol = str(data.get("protocol") or getattr(proxy_cfg, "PROXY_API_PROTOCOL", "socks5h") or "socks5h").strip()
            timeout = float(data.get("timeout") or getattr(proxy_cfg, "PROXY_API_TIMEOUT", 15) or 15)
            proxy_url = proxy_cfg.fetch_proxy_from_api(
                api_url=api_url,
                protocol=protocol,
                timeout=timeout,
                max_attempts=max(1, int(getattr(proxy_cfg, "PROXY_API_MAX_ATTEMPTS", 3) or 3)),
                force=True,
            )

            session = CurlSession()
            session.proxies = {"http": proxy_url, "https": proxy_url}
            response = session.get("https://ipinfo.io/json", timeout=max(5.0, timeout))
            if int(response.status_code) != 200:
                raise RuntimeError(f"代理出口检测返回 HTTP {response.status_code}")
            geo = response.json() if response.content else {}
            if not isinstance(geo, dict):
                geo = {}
            return jsonify({
                "ok": True,
                "proxy": proxy_cfg.mask_proxy_url(proxy_url),
                "exit_ip": geo.get("ip") or "",
                "country": geo.get("country") or "",
                "region": geo.get("region") or "",
                "city": geo.get("city") or "",
                "org": geo.get("org") or "",
            })
        except Exception as exc:
            logger.warning("API代理测试失败: %s: %s", type(exc).__name__, str(exc)[:300])
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.get("/api/config")
    def api_config_get():
        return jsonify(config_editor.get_config())

    @app.post("/api/cloudmail/gen-token")
    def api_cloudmail_gen_token():
        """手动生成 CloudMail Authorization Token，并把本次填写的 CloudMail 配置一并写入 .env。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import gen_token
            from config.env_loader import write_env_values

            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            path = (data.get("path") or "/api/public/genToken").strip() or "/api/public/genToken"
            token = gen_token(
                email=admin_email,
                password=password,
                path=path,
                base_url=api_base,
            )
            updates = {"CLOUDMAIL_AUTH_TOKEN": token}
            # 生成 Token 时用户通常尚未点“保存配置”；这里同步保存本次填写的字段，
            # 避免 loadConfig() 后 API 地址/账号/密码被旧 .env 值覆盖。
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if path:
                updates["CLOUDMAIL_TOKEN_PATH"] = path
            written = write_env_values(updates)
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail Token 写入后热加载失败")
            return jsonify({
                "ok": True,
                "token": token,
                "written": written,
                "message": "CloudMail Token 已生成，且当前 CloudMail 配置已保存",
            })
        except Exception as exc:
            logger.exception("生成 CloudMail Token 失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/cloudmail/domains")
    def api_cloudmail_domains():
        """从 CloudMail 平台获取域名列表，并可写入 .env 作为本地缓存。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import fetch_domains
            from config.env_loader import write_env_values

            updates = {}
            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            token = (data.get("token") or "").strip()
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if token:
                updates["CLOUDMAIL_AUTH_TOKEN"] = token
            if updates:
                write_env_values(updates)
                import config as _config_pkg
                _config_pkg.reload_all()

            domains = fetch_domains(force=True)
            written = write_env_values({"CLOUDMAIL_DOMAINS": "\n".join(domains)})
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail 域名写入后热加载失败")
            return jsonify({
                "ok": True,
                "domains": domains,
                "count": len(domains),
                "written": written,
                "message": f"已获取 {len(domains)} 个 CloudMail 可用域名并保存",
            })
        except Exception as exc:
            logger.exception("获取 CloudMail 域名失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/config")
    def api_config_set():
        data = request.get_json(silent=True) or {}
        updates = data.get("updates") if isinstance(data.get("updates"), dict) else data
        if not isinstance(updates, dict) or not updates:
            return jsonify({"ok": False, "error": "无更新内容"}), 400
        try:
            result = config_editor.update_config(updates)
        except Exception as exc:
            logger.exception("配置写入失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

        # 写盘成功后立即热加载所有 config 子模块，让运行时代码看到新值。
        reload_ok = True
        reload_err = ""
        try:
            import config as _config_pkg
            _config_pkg.reload_all()
        except Exception as exc:
            reload_ok = False
            reload_err = f"{type(exc).__name__}: {exc}"
            logger.exception("配置热加载失败")

        return jsonify({
            "ok": True,
            "updated": result["updated"],
            "ignored": result["ignored"],
            "runtime_file_updated": result.get("runtime_file_updated", []),
            "reloaded": reload_ok,
            "note": (
                "✅ 已保存并热加载，新值立即生效"
                if reload_ok
                else f"⚠️ 已写入文件但热加载失败（{reload_err}），需重启 Web 服务才能生效"
            ),
        })

    return app
