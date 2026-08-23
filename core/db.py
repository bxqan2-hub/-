# -*- coding: utf-8 -*-
"""
本地文件持久化层。

根目录文件分工：
    - 用于注册的邮箱.txt      仅保留可继续注册的邮箱素材
    - 注册成功的邮箱.txt      仅保存注册成功的邮箱素材，不追加 token
    - 注册成功的token.txt     每行只保存一个 access token
    - 用于注册的邮箱.json     Outlook 账号池完整状态
    - 注册成功的邮箱.json     注册成功账号完整状态
"""
import copy
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT
_LEGACY_DATA_DIR = _PROJECT_ROOT / "data"
_LOG_DIR = _PROJECT_ROOT / "注册日志"
_PLAN_CHECK_STALE_SECONDS = 120
_PLAN_CHECK_QUEUE_STALE_SECONDS = 1800
_PLUS_MAIL_CONFIRMATION_MAX_AGE_SECONDS = 35 * 24 * 60 * 60
_PLUS_MAIL_CONFIRMATION_FUTURE_SKEW_SECONDS = 5 * 60
_SUBSCRIPTION_TERMINAL_CODES = {
    "subscription_expired", "subscription_canceled", "subscription_cancelled",
    "subscription_past_due",
}
_SUBSCRIPTION_TERMINAL_STATUSES = {"expired", "canceled", "cancelled", "past_due"}

_OUTLOOK_JSON = _PROJECT_ROOT / "用于注册的邮箱.json"
_OUTLOOK_TXT = _PROJECT_ROOT / "用于注册的邮箱.txt"
_GENERIC_API_EMAIL_JSON = _PROJECT_ROOT / "用于注册的API邮箱.json"
_GENERIC_API_EMAIL_TXT = _PROJECT_ROOT / "用于注册的API邮箱.txt"
_ACCOUNTS_JSON = _PROJECT_ROOT / "注册成功的邮箱.json"
# 密码/TOTP 在服务端确认生效后、完整注册结果落库前的独立检查点。
# 该文件只保存安全凭据，不进入正式账号列表，也不会改变邮箱池完成状态。
_SECURITY_CHECKPOINTS_JSON = _PROJECT_ROOT / "注册安全凭据待完成.json"
_SECURITY_CHECKPOINTS_LOCK = _PROJECT_ROOT / "注册安全凭据待完成.lock"
_ACCOUNT_GROUPS_JSON = _PROJECT_ROOT / "账号分组.json"
_ACCOUNTS_TXT = _PROJECT_ROOT / "注册成功的邮箱.txt"
_TOKENS_TXT = _PROJECT_ROOT / "注册成功的token.txt"
_JOBS_JSON = _PROJECT_ROOT / "注册任务.json"
_MAIL_STATUS_JSON = _PROJECT_ROOT / "邮件检测池.json"
_VIEWER_HTML = _PROJECT_ROOT / "accounts_viewer.html"
_CODEX_DIR = _PROJECT_ROOT / "codex_accounts"
# 导出状态单独存：{ "codex-邮箱-plan.json": {"exported_at": "...", "exported_count": N} }
# 不污染 CPA 兼容的原文件
_CODEX_EXPORT_STATE = _PROJECT_ROOT / "codex_导出状态.json"

_LEGACY_SQLITE = _LEGACY_DATA_DIR / "registrations.db"
_LEGACY_OUTLOOK_JSON = _LEGACY_DATA_DIR / "outlook_accounts.json"
_LEGACY_ACCOUNTS_JSON = _LEGACY_DATA_DIR / "registered_accounts.json"
_LEGACY_JOBS_JSON = _LEGACY_DATA_DIR / "registration_jobs.json"
_LOCK = threading.RLock()
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_storage() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    _ensure_storage()
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    _ensure_storage()
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _next_id(items: list[dict]) -> int:
    ids = [int(item.get("id") or 0) for item in items]
    return (max(ids) if ids else 0) + 1


def _outlook_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("password") or "",
        row.get("client_id") or "",
        row.get("refresh_token") or "",
    ])


def _generic_api_email_line(row: dict) -> str:
    if str(row.get("provider") or "").strip().lower() == "inbox_mate":
        return "----".join([
            row.get("email") or "",
            row.get("password") or "",
            row.get("api_base") or "",
        ])
    return "----".join([
        row.get("email") or "",
        row.get("code_url") or "",
    ])


def _account_line(row: dict) -> str:
    base = row.get("original_email_line") or row.get("email") or ""
    token = row.get("access_token") or ""
    totp = row.get("totp_secret") or ""
    return f"{base}----{token}----{totp}" if totp else f"{base}----{token}"


def _registered_email_line(row: dict) -> str:
    """生成注册成功邮箱 TXT 的行内容；token 由注册成功的token.txt 单独保存。"""
    return row.get("original_email_line") or row.get("email") or ""


def _sync_outlook_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_outlook_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _OUTLOOK_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_generic_api_email_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_generic_api_email_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _GENERIC_API_EMAIL_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_accounts_txt(rows: list[dict]) -> None:
    lines = [_registered_email_line(r) for r in sorted(rows, key=lambda x: int(x.get("id") or 0))]
    _ACCOUNTS_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_tokens_txt(rows: list[dict]) -> None:
    tokens = [
        r.get("access_token") or ""
        for r in sorted(rows, key=lambda x: int(x.get("id") or 0))
        if r.get("access_token")
    ]
    _TOKENS_TXT.write_text(("\n".join(tokens) + ("\n" if tokens else "")), encoding="utf-8")


def _viewer_snapshot(outlook_rows: list[dict], account_rows: list[dict]) -> dict:
    account_by_email = {
        (a.get("email") or "").lower(): a
        for a in account_rows
    }
    return {
        "generated_at": _now(),
        "accounts": [
            _decorate_account(r)
            for r in sorted(account_rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        ],
        "outlook": [
            _decorate_outlook(r, account_by_email)
            for r in sorted(outlook_rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        ],
        "summary": {
            "accounts": len(account_rows),
            "outlook_total": len(outlook_rows),
            "outlook_available": sum(1 for r in outlook_rows if r.get("status") == "available"),
            "outlook_used": sum(1 for r in outlook_rows if r.get("status") == "used"),
            "outlook_failed": sum(1 for r in outlook_rows if r.get("status") == "failed"),
        },
    }


def _render_static_viewer(outlook_rows: list[dict] | None = None, account_rows: list[dict] | None = None) -> Path:
    """生成可直接双击打开的静态账号查看页。"""
    outlook_rows = _load_outlook() if outlook_rows is None else outlook_rows
    account_rows = _load_accounts() if account_rows is None else account_rows
    snapshot = _viewer_snapshot(outlook_rows, account_rows)
    data_json = json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")
    title = escape(f"账号查看器 - {snapshot['generated_at']}")
    html_text = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; }}
    :root {{
      --bg: #eef3f8;
      --surface: #ffffff;
      --soft: #f7f9fc;
      --text: #172033;
      --muted: #667085;
      --line: #d9e2ec;
      --blue: #2563eb;
      --green: #16803c;
      --red: #c2413a;
      --amber: #b7791f;
    }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 22px 28px;
      background: #101827;
      color: #fff;
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: center;
      flex-wrap: wrap;
    }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 28px; }}
    .meta {{ margin-top: 6px; color: #b8c7d9; font-size: 13px; }}
    .stats {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .stat {{
      min-width: 116px;
      padding: 10px 12px;
      border: 1px solid rgba(255,255,255,.16);
      border-radius: 8px;
      background: rgba(255,255,255,.08);
    }}
    .stat span {{ display: block; color: #b8c7d9; font-size: 12px; }}
    .stat strong {{ display: block; margin-top: 4px; font-size: 18px; }}
    main {{ width: min(1500px, calc(100vw - 32px)); margin: 16px auto 30px; display: grid; gap: 16px; }}
    .toolbar, section {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 8px 22px rgba(15,23,42,.06);
    }}
    .toolbar {{ padding: 14px; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
    .search {{ min-width: min(520px, 100%); flex: 1; }}
    input {{
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
    }}
    .buttons {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    button {{
      min-height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 0 12px;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: var(--soft); }}
    button.primary {{ border-color: var(--blue); background: var(--blue); color: #fff; }}
    button.good {{ border-color: #2f855a; background: #edf8f1; color: #166534; }}
    button:disabled {{ color: #98a2b3; cursor: not-allowed; background: #f2f4f7; }}
    .head {{ padding: 14px 16px; border-bottom: 1px solid var(--line); background: var(--soft); }}
    .head p {{ margin-top: 4px; color: var(--muted); font-size: 12px; }}
    .table-wrap {{ overflow: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf1f5; text-align: left; white-space: nowrap; vertical-align: middle; }}
    th {{ position: sticky; top: 0; background: #fbfcfe; color: #475467; z-index: 1; font-size: 12px; }}
    tr:hover td {{ background: #fbfdff; }}
    .main-cell {{ font-weight: 700; }}
    .sub-cell {{ margin-top: 3px; color: var(--muted); font-size: 12px; }}
    .mono {{ font-family: ui-monospace, "JetBrains Mono", Consolas, monospace; font-size: 12px; }}
    .muted {{ color: var(--muted); }}
    .pill {{ display: inline-flex; min-width: 48px; justify-content: center; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
    .status-available {{ color: var(--blue); background: #eef4ff; }}
    .status-used {{ color: #475467; background: #f2f4f7; }}
    .status-failed {{ color: var(--red); background: #fff0ef; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    #toast {{
      position: fixed;
      right: 18px;
      bottom: 18px;
      padding: 10px 14px;
      border-radius: 8px;
      background: #101827;
      color: #fff;
      box-shadow: 0 14px 30px rgba(15,23,42,.24);
      opacity: 0;
      transform: translateY(8px);
      pointer-events: none;
      transition: opacity .18s ease, transform .18s ease;
    }}
    #toast.show {{ opacity: 1; transform: translateY(0); }}
    @media (max-width: 820px) {{
      header {{ align-items: flex-start; }}
      .stats {{ width: 100%; }}
      .stat {{ flex: 1; }}
    }}
  </style>
</head>
<body>
<header>
  <div>
    <h1>账号查看器</h1>
    <p class="meta">静态快照，无需启动 Web Server。生成时间：<span id="generated"></span></p>
  </div>
  <div class="stats">
    <div class="stat"><span>已完成</span><strong id="statAccounts">0</strong></div>
    <div class="stat"><span>邮箱总数</span><strong id="statOutlook">0</strong></div>
    <div class="stat"><span>可用邮箱</span><strong id="statAvailable">0</strong></div>
  </div>
</header>
<main>
  <div class="toolbar">
    <div class="search"><input id="q" placeholder="搜索邮箱、token、clientId、状态"></div>
    <div class="buttons">
      <button class="primary" id="copyAllTokens">复制全部 Token</button>
      <button class="good" id="copyAllLines">复制全部整行</button>
      <button id="copyAllEmails">复制全部邮箱素材</button>
    </div>
  </div>
  <section>
    <div class="head">
      <h2>已完成账号</h2>
      <p>整行格式：邮箱----密码----clientId----邮箱刷新令牌----accessToken----totpSecret（如有）</p>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>邮箱</th><th>来源</th><th>Token</th><th>备注</th><th>2FA</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody id="accountsBody"></tbody>
      </table>
    </div>
  </section>
  <section>
    <div class="head">
      <h2>邮箱素材库</h2>
      <p>原始格式：邮箱----密码----clientId----邮箱刷新令牌；注册完成后可直接复制对应 Token 或整行。</p>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>邮箱</th><th>状态</th><th>Token</th><th>导入时间</th><th>已用时间</th><th>操作</th></tr></thead>
        <tbody id="outlookBody"></tbody>
      </table>
    </div>
  </section>
</main>
<div id="toast"></div>
<script id="snapshot" type="application/json">{data_json}</script>
<script>
const SNAPSHOT = JSON.parse(document.getElementById('snapshot').textContent);
const $ = (s) => document.querySelector(s);
let copySeq = 0;
const copyStore = new Map();

function fmt(v) {{ return v == null || v === '' ? '-' : String(v); }}
function esc(v) {{
  return fmt(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}
function short(v, n = 34) {{
  const s = v || '';
  return s.length > n ? `${{s.slice(0, n)}}...` : s;
}}
function copyId(v) {{
  if (!v) return '';
  const id = `c${{++copySeq}}`;
  copyStore.set(id, v);
  return id;
}}
function btn(label, value, cls = '') {{
  const id = copyId(value);
  return `<button class="${{cls}}" data-copy-id="${{id}}" ${{id ? '' : 'disabled'}}>${{label}}</button>`;
}}
function pill(status) {{
  const map = {{ available: '可用', used: '已用', failed: '失败' }};
  const label = map[status] || status || '-';
  return `<span class="pill status-${{esc(status)}}">${{esc(label)}}</span>`;
}}
function showToast(text) {{
  const toast = $('#toast');
  toast.textContent = text;
  toast.classList.add('show');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove('show'), 1400);
}}
async function copyText(text) {{
  if (!text) return;
  if (navigator.clipboard && window.isSecureContext) {{
    await navigator.clipboard.writeText(text);
  }} else {{
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }}
  showToast('已复制');
}}
function haystack(row) {{
  return Object.values(row).join('\\n').toLowerCase();
}}
function render() {{
  copyStore.clear();
  copySeq = 0;
  const q = $('#q').value.trim().toLowerCase();
  const accounts = SNAPSHOT.accounts.filter((r) => !q || haystack(r).includes(q));
  const outlook = SNAPSHOT.outlook.filter((r) => !q || haystack(r).includes(q));
  $('#generated').textContent = SNAPSHOT.generated_at;
  $('#statAccounts').textContent = SNAPSHOT.summary.accounts;
  $('#statOutlook').textContent = SNAPSHOT.summary.outlook_total;
  $('#statAvailable').textContent = SNAPSHOT.summary.outlook_available;
  $('#accountsBody').innerHTML = accounts.map((r) => `
    <tr>
      <td class="muted">#${{esc(r.id)}}</td>
      <td><div class="main-cell">${{esc(r.email)}}</div><div class="sub-cell">${{esc(r.user_name || '-')}}</div></td>
      <td>${{esc(r.email_source || '-')}}</td>
      <td><span class="mono">${{esc(short(r.access_token || '', 42))}}</span></td>
      <td title="${{esc(r.note || '')}}">${{r.note ? esc(short(r.note, 60)) : '<span class="muted">-</span>'}}</td>
      <td>${{r.totp_secret ? '已启用' : '<span class="muted">未启用</span>'}}</td>
      <td class="muted">${{esc(r.created_at || '-')}}</td>
      <td class="actions">${{btn('复制Token', r.access_token, 'primary')}} ${{btn('复制整行', r.copy_line, 'good')}}</td>
    </tr>`).join('');
  $('#outlookBody').innerHTML = outlook.map((r) => `
    <tr>
      <td><div class="main-cell">${{esc(r.email)}}</div><div class="sub-cell mono">${{esc(short(r.copy_line, 76))}}</div></td>
      <td>${{pill(r.status)}}</td>
      <td><span class="mono">${{esc(short(r.access_token || '', 36) || '未生成')}}</span></td>
      <td class="muted">${{esc(r.imported_at || r.created_at || '-')}}</td>
      <td class="muted">${{esc(r.used_at || '-')}}</td>
      <td class="actions">${{btn('复制邮箱', r.copy_line)}} ${{btn('复制Token', r.access_token, 'primary')}} ${{btn('复制整行', r.account_copy_line, 'good')}}</td>
    </tr>`).join('');
}}
document.addEventListener('click', (e) => {{
  const target = e.target.closest('[data-copy-id]');
  if (!target) return;
  copyText(copyStore.get(target.dataset.copyId));
}});
$('#q').addEventListener('input', render);
$('#copyAllTokens').addEventListener('click', () => copyText(SNAPSHOT.accounts.map((r) => r.access_token).filter(Boolean).join('\\n')));
$('#copyAllLines').addEventListener('click', () => copyText(SNAPSHOT.accounts.map((r) => r.copy_line).filter(Boolean).join('\\n')));
$('#copyAllEmails').addEventListener('click', () => copyText(SNAPSHOT.outlook.map((r) => r.copy_line).filter(Boolean).join('\\n')));
render();
</script>
</body>
</html>
"""
    tmp = _VIEWER_HTML.with_suffix(".html.tmp")
    tmp.write_text(html_text, encoding="utf-8")
    try:
        tmp.replace(_VIEWER_HTML)
        return _VIEWER_HTML
    except PermissionError:
        # Windows 下如果目标 HTML 正被浏览器或编辑器短暂占用，原子替换可能失败。
        # 先尝试直接覆盖；仍失败时写一个时间戳快照，避免注册流程被查看页刷新阻断。
        try:
            _VIEWER_HTML.write_text(html_text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            return _VIEWER_HTML
        except PermissionError:
            fallback = _DATA_DIR / f"accounts_viewer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            fallback.write_text(html_text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            return fallback


def _load_outlook() -> list[dict]:
    rows = _read_json(_OUTLOOK_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_OUTLOOK_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_outlook(rows: list[dict]) -> None:
    _write_json(_OUTLOOK_JSON, rows)
    _sync_outlook_txt(rows)
    _render_static_viewer(outlook_rows=rows)


def _load_generic_api_emails() -> list[dict]:
    rows = _read_json(_GENERIC_API_EMAIL_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_generic_api_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _generic_api_email_line(row)
    _write_json(_GENERIC_API_EMAIL_JSON, rows)
    _sync_generic_api_email_txt(rows)


def _load_accounts() -> list[dict]:
    rows = _read_json(_ACCOUNTS_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_ACCOUNTS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_accounts(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _account_line(row)
    _write_json(_ACCOUNTS_JSON, rows)
    _sync_accounts_txt(rows)
    _sync_tokens_txt(rows)
    _render_static_viewer(account_rows=rows)


@contextmanager
def _security_checkpoint_file_lock(timeout: float = 30.0):
    """跨进程串行化 checkpoint 的 read-modify-replace/consume。"""
    _ensure_storage()
    _SECURITY_CHECKPOINTS_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = _SECURITY_CHECKPOINTS_LOCK.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + max(0.1, float(timeout or 30.0))
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("安全凭据 checkpoint 跨进程锁超时") from exc
                time.sleep(0.05)
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                logger.warning("安全凭据 checkpoint 跨进程锁释放异常")
        handle.close()


def _load_security_checkpoints() -> list[dict]:
    """严格读取敏感 checkpoint；损坏或不可读时拒绝覆盖原文件。"""
    try:
        raw = _SECURITY_CHECKPOINTS_JSON.read_text(encoding="utf-8")
        decoded = json.loads(raw)
    except FileNotFoundError:
        return []
    except Exception as exc:
        raise RuntimeError("安全凭据 checkpoint 不可读或 JSON 已损坏；已保留原文件") from exc
    if not isinstance(decoded, list):
        raise RuntimeError("安全凭据 checkpoint 顶层结构无效；已保留原文件")

    rows: list[dict] = []
    seen: set[str] = set()
    for item in decoded:
        if not isinstance(item, dict):
            raise RuntimeError("安全凭据 checkpoint 包含无效记录；已保留原文件")
        normalized_email = str(item.get("email") or "").strip().lower()
        if not normalized_email or normalized_email in seen:
            raise RuntimeError("安全凭据 checkpoint 包含空邮箱或重复记录；已保留原文件")
        seen.add(normalized_email)
        row = dict(item)
        row["email"] = normalized_email
        rows.append(row)
    return rows


def _save_security_checkpoints(rows: list[dict]) -> None:
    """原子写入 pending 安全凭据；空集合直接删除敏感运行时文件。"""
    if rows:
        _write_json(_SECURITY_CHECKPOINTS_JSON, rows)
    else:
        _SECURITY_CHECKPOINTS_JSON.unlink(missing_ok=True)


def _load_account_groups() -> list[dict]:
    rows = _read_json(_ACCOUNT_GROUPS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_account_groups(groups: list[dict]) -> None:
    _write_json(_ACCOUNT_GROUPS_JSON, groups)


def _remove_emails_from_account_groups(emails: set[str]) -> int:
    """Remove deleted accounts from every group immediately; caller holds _LOCK."""
    targets = {str(email or "").strip().lower() for email in emails if str(email or "").strip()}
    if not targets:
        return 0
    groups = _load_account_groups()
    removed = 0
    changed = False
    for group in groups:
        if not isinstance(group, dict):
            continue
        before = [str(value or "").strip().lower() for value in group.get("emails") or [] if str(value or "").strip()]
        after = [email for email in before if email not in targets]
        if len(after) != len(before):
            removed += len(before) - len(after)
            group["emails"] = after
            group["updated_at"] = _now()
            changed = True
    if changed:
        _save_account_groups(groups)
    return removed


def list_account_groups() -> list[dict]:
    """返回本地账号分组；成员以邮箱保存，避免账号删除后 ID 重排导致错组。"""
    with _LOCK:
        account_by_email = {
            str(row.get("email") or "").strip().lower(): int(row.get("id") or 0)
            for row in _load_accounts()
            if str(row.get("email") or "").strip()
        }
        groups = _load_account_groups()
        # Repair legacy duplicate memberships. The most recently updated group
        # wins; for equal timestamps, the later group entry wins deterministically.
        email_owner: dict[str, tuple[tuple[str, int], str]] = {}
        for index, raw in enumerate(groups):
            if not isinstance(raw, dict):
                continue
            group_id = str(raw.get("id") or "").strip()
            stamp = str(raw.get("updated_at") or raw.get("created_at") or "")
            for value in raw.get("emails") or []:
                email = str(value or "").strip().lower()
                score = (stamp, index)
                if email and email in account_by_email and (email not in email_owner or score >= email_owner[email][0]):
                    email_owner[email] = (score, group_id)
        normalized: list[dict] = []
        changed = False
        for raw in groups:
            if not isinstance(raw, dict):
                changed = True
                continue
            group_id = str(raw.get("id") or "").strip()
            name = str(raw.get("name") or "").strip()
            if not group_id or not name:
                changed = True
                continue
            emails = []
            seen = set()
            for value in raw.get("emails") or []:
                email = str(value or "").strip().lower()
                if (
                    email
                    and email in account_by_email
                    and email_owner.get(email, (("", -1), ""))[1] == group_id
                    and email not in seen
                ):
                    emails.append(email)
                    seen.add(email)
            if emails != list(raw.get("emails") or []):
                changed = True
            normalized.append({
                "id": group_id,
                "name": name,
                "emails": emails,
                "account_ids": [account_by_email[email] for email in emails],
                "count": len(emails),
                "created_at": raw.get("created_at") or "",
                "updated_at": raw.get("updated_at") or "",
            })
        if changed:
            _save_account_groups([{key: group[key] for key in ("id", "name", "emails", "created_at", "updated_at")} for group in normalized])
        return normalized


def create_account_group(name: str) -> dict:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("分组名称不能为空")
    if len(clean_name) > 80:
        raise ValueError("分组名称最多 80 个字符")
    with _LOCK:
        groups = _load_account_groups()
        if any(str(group.get("name") or "").strip().casefold() == clean_name.casefold() for group in groups if isinstance(group, dict)):
            raise ValueError("已存在同名分组")
        now = _now()
        group = {"id": uuid.uuid4().hex, "name": clean_name, "emails": [], "created_at": now, "updated_at": now}
        groups.append(group)
        _save_account_groups(groups)
        return {**group, "account_ids": [], "count": 0}


def ensure_account_group(name: str) -> dict:
    """并发安全地取得或创建命名分组。"""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("分组名称不能为空")
    if len(clean_name) > 80:
        raise ValueError("分组名称最多 80 个字符")
    with _LOCK:
        groups = _load_account_groups()
        existing = next((
            group for group in groups
            if isinstance(group, dict)
            and str(group.get("name") or "").strip().casefold() == clean_name.casefold()
        ), None)
        if existing is None:
            now = _now()
            existing = {"id": uuid.uuid4().hex, "name": clean_name, "emails": [], "created_at": now, "updated_at": now}
            groups.append(existing)
            _save_account_groups(groups)
        account_by_email = {
            str(row.get("email") or "").strip().lower(): int(row.get("id") or 0)
            for row in _load_accounts()
            if str(row.get("email") or "").strip()
        }
        emails = [
            str(value or "").strip().lower()
            for value in existing.get("emails") or []
            if str(value or "").strip().lower() in account_by_email
        ]
        return {
            **existing,
            "emails": emails,
            "account_ids": [account_by_email[email] for email in emails],
            "count": len(emails),
        }


def delete_account_group(group_id: str) -> bool:
    target = str(group_id or "").strip()
    with _LOCK:
        groups = _load_account_groups()
        remaining = [group for group in groups if str(group.get("id") or "") != target]
        if len(remaining) == len(groups):
            return False
        _save_account_groups(remaining)
        return True


def rename_account_group(group_id: str, name: str) -> dict | None:
    target = str(group_id or "").strip()
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("分组名称不能为空")
    if len(clean_name) > 80:
        raise ValueError("分组名称最多 80 个字符")
    with _LOCK:
        groups = _load_account_groups()
        group = next((item for item in groups if str(item.get("id") or "") == target), None)
        if group is None:
            return None
        if any(
            item is not group
            and str(item.get("name") or "").strip().casefold() == clean_name.casefold()
            for item in groups
            if isinstance(item, dict)
        ):
            raise ValueError("已存在同名分组")
        group["name"] = clean_name
        group["updated_at"] = _now()
        _save_account_groups(groups)
        return next((item for item in list_account_groups() if item["id"] == target), None)


def add_accounts_to_group(group_id: str, account_ids: list[int] | None) -> tuple[dict | None, list[dict]]:
    target = str(group_id or "").strip()
    ids = {int(value) for value in (account_ids or []) if str(value).strip().isdigit()}
    with _LOCK:
        groups = _load_account_groups()
        group = next((item for item in groups if str(item.get("id") or "") == target), None)
        if group is None:
            return None, [{"reason": "分组不存在"}]
        accounts = {int(row.get("id") or 0): row for row in _load_accounts()}
        skipped = [{"id": account_id, "reason": "账号不存在"} for account_id in ids if account_id not in accounts]
        moving_emails = {
            str(accounts[account_id].get("email") or "").strip().lower()
            for account_id in ids
            if account_id in accounts and str(accounts[account_id].get("email") or "").strip()
        }
        # Account groups are exclusive. Moving an account into the target group
        # removes it from GC/default custom groups first, preventing duplicates.
        for other in groups:
            if not isinstance(other, dict) or other is group:
                continue
            before = [str(value or "").strip().lower() for value in other.get("emails") or [] if str(value or "").strip()]
            after = [email for email in before if email not in moving_emails]
            if after != before:
                other["emails"] = after
                other["updated_at"] = _now()
        emails = [str(value or "").strip().lower() for value in group.get("emails") or []]
        known = set(emails)
        for account_id in ids:
            row = accounts.get(account_id)
            email = str((row or {}).get("email") or "").strip().lower()
            if email and email not in known:
                emails.append(email)
                known.add(email)
        group["emails"] = emails
        group["updated_at"] = _now()
        _save_account_groups(groups)
        result = next((item for item in list_account_groups() if item["id"] == target), None)
        return result, skipped


def move_accounts_to_default_group(account_ids: list[int] | None) -> tuple[dict, list[dict]]:
    """把账号移出所有自定义分组，使其回到默认组。"""
    ids = {int(value) for value in (account_ids or []) if str(value).strip().isdigit()}
    with _LOCK:
        accounts = {int(row.get("id") or 0): row for row in _load_accounts()}
        skipped = [{"id": account_id, "reason": "账号不存在"} for account_id in ids if account_id not in accounts]
        moving_emails = {
            str(accounts[account_id].get("email") or "").strip().lower()
            for account_id in ids
            if account_id in accounts and str(accounts[account_id].get("email") or "").strip()
        }
        groups = _load_account_groups()
        changed = False
        for group in groups:
            if not isinstance(group, dict):
                continue
            before = [str(value or "").strip().lower() for value in group.get("emails") or [] if str(value or "").strip()]
            after = [email for email in before if email not in moving_emails]
            if after != before:
                group["emails"] = after
                group["updated_at"] = _now()
                changed = True
        if changed:
            _save_account_groups(groups)
        return {"id": "default", "name": "默认组", "count": len(moving_emails)}, skipped


def _load_jobs() -> list[dict]:
    rows = _read_json(_JOBS_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_JOBS_JSON, [])
    return rows if isinstance(rows, list) else []


def _load_mail_status_pool() -> list[dict]:
    rows = _read_json(_MAIL_STATUS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_mail_status_pool(rows: list[dict]) -> None:
    _write_json(_MAIL_STATUS_JSON, rows)


def _save_jobs(rows: list[dict]) -> None:
    _write_json(_JOBS_JSON, rows)


def _find_by_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def _decorate_account(row: dict) -> dict:
    out = dict(row)
    out["note"] = out.get("note") or ""
    out["note_updated_at"] = out.get("note_updated_at") or ""
    jp_trial_status = str(out.get("jp_trial_status") or "unchecked").strip().lower()
    out["jp_trial_status"] = jp_trial_status if jp_trial_status in {
        "unchecked", "eligible", "ineligible", "failed",
    } else "unchecked"
    plan_status = out.get("plan_check_status")
    if plan_status in {"queued", "running"}:
        try:
            stamp_key = "plan_check_queued_at" if plan_status == "queued" else "plan_check_started_at"
            stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if plan_status == "queued" else _PLAN_CHECK_STALE_SECONDS
            started_at = datetime.fromisoformat(str(out.get(stamp_key) or ""))
            if (datetime.now() - started_at).total_seconds() >= stale_after:
                out["plan_check_status"] = "failed"
                out["plan_check_error"] = "上次套餐查询状态已超时，可重新查询"
                out["plan_check_stale"] = True
        except (TypeError, ValueError):
            out["plan_check_status"] = "failed"
            out["plan_check_error"] = "上次套餐查询状态异常，可重新查询"
            out["plan_check_stale"] = True
    out["copy_line"] = _account_line(out)
    return out


def _account_matches_plan_filter(row: dict, plan_filter: str | None = None) -> bool:
    """账号套餐过滤；试用资格只采用最近一次成功查询得到的明确结果。"""
    f = str(plan_filter or "").strip().lower()
    if not f or f in {"all", "any"}:
        return True
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    subscription_plan = str(row.get("subscription_plan") or "").strip().lower()
    has_active_plus = bool(row.get("has_active_plus_subscription")) or bool(
        row.get("has_active_subscription")
        and "plus" in subscription_plan
        and "free" not in subscription_plan
    )
    is_free = bool(row.get("is_free_plan")) or (
        not has_active_plus
        and (plan == "free" or subscription_plan == "chatgptfreeplan")
    )
    trial_checked = is_free and bool(row.get("plan_last_success_at"))
    trial_eligible = trial_checked and bool(row.get("plus_trial_eligible"))
    trial_offer_kind = str(row.get("plus_trial_offer_kind") or "").strip().lower()
    if trial_eligible and trial_offer_kind in {"", "none"}:
        # Backfill classification for rows saved before offer-kind persistence.
        from core.chatgpt_plan import classify_plus_trial_offer
        trial_offer_kind = classify_plus_trial_offer({
            "id": row.get("plus_trial_campaign_id"),
            "metadata": {
                "title": row.get("plus_trial_title"),
                "summary": row.get("plus_trial_summary"),
                "promotion_type_label": row.get("plus_trial_promotion_type_label"),
                "discount": {
                    "percentage": (
                        row.get("plus_trial_offer_percentage")
                        if row.get("plus_trial_offer_percentage") is not None
                        else row.get("plus_trial_discount_percentage")
                    ),
                },
            },
        })["kind"]
    if f == "plus":
        return has_active_plus
    if f == "free":
        return is_free
    if f in {"trial", "free-trial", "trial-eligible"}:
        return trial_eligible
    if f in {"zero-trial", "free-trial-zero", "trial-free"}:
        return trial_eligible and trial_offer_kind == "free_trial"
    if f in {"half-trial", "half-price", "trial-half"}:
        return trial_eligible and trial_offer_kind == "half_price"
    if f in {"discount-trial", "trial-discount", "other-trial"}:
        return trial_eligible and trial_offer_kind in {"discount", "trial"}
    if f in {"no-trial", "trial-ineligible"}:
        return trial_checked and not bool(row.get("plus_trial_eligible"))
    if f in {"nonfree", "non-free", "not-free", "paid"}:
        # 未查询/无法识别的空套餐不归到“非 Free”，避免把未知账号误当付费账号。
        return bool(plan) and plan != "free"
    return plan == f


def _renumber_accounts_after_delete(rows: list[dict]) -> dict[int, int]:
    """将删除后的本地账号 ID 压缩为连续序号，并返回旧 ID 到新 ID 的映射。"""
    mapping: dict[int, int] = {}
    for new_id, row in enumerate(sorted(rows, key=lambda x: int(x.get("id") or 0)), start=1):
        old_id = int(row.get("id") or 0)
        mapping[old_id] = new_id
        row["id"] = new_id
    return mapping


def _remap_account_id_references(id_mapping: dict[int, int]) -> None:
    """同步邮箱池和任务记录中保存的本地账号 ID，已删除账号的引用清空。"""
    def remap(rows: list[dict], save) -> None:
        changed = False
        for row in rows:
            raw_id = row.get("registered_account_id")
            if raw_id is None:
                continue
            try:
                old_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if old_id in id_mapping:
                if raw_id != id_mapping[old_id]:
                    row["registered_account_id"] = id_mapping[old_id]
                    changed = True
            else:
                row.pop("registered_account_id", None)
                changed = True
        if changed:
            save(rows)

    remap(_load_outlook(), _save_outlook)
    remap(_load_generic_api_emails(), _save_generic_api_emails)

    jobs = _load_jobs()
    changed = False
    for job in jobs:
        raw_id = job.get("account_id")
        if raw_id is None:
            continue
        try:
            old_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        # 历史任务不应指向随后可能被复用的 ID；删除的账号改为空。
        new_id = id_mapping.get(old_id)
        if new_id is None:
            job["account_id"] = None
            changed = True
        elif raw_id != new_id:
            job["account_id"] = new_id
            changed = True
    if changed:
        _save_jobs(jobs)


_POOL_ACCOUNT_PLAN_FIELDS = (
    "plan_type", "current_plan_type", "subscription_plan",
    "has_active_subscription", "has_active_plus_subscription", "is_free_plan",
    "plus_trial_eligible", "plan_check_status", "plan_check_ok", "plan_check_error",
    "plan_check_trigger", "plan_check_queued_at", "plan_check_started_at",
    "plan_check_completed_at", "plan_check_network_route", "plan_check_proxy_used",
    "plan_check_proxy_fallback_reason", "plan_check_proxy_country",
    "plan_checked_at", "plan_last_success_at", "plan_expires_at", "plan_renews_at",
    "billing_period", "billing_currency", "discount_amount", "discount_type",
    "discount_expires_at", "discount_promo_campaign_id",
    "plan_detection_source", "plan_authority", "plan_confidence",
    "mail_plus_status", "mail_plus_promoted", "mail_plus_checked_at",
    "mail_plus_evidence", "mail_plus_subject", "mail_plus_date", "mail_plus_account_id",
)


def _attach_pool_account(out: dict, account: dict | None) -> dict:
    """按邮箱把账号池信息附加到邮箱池行，套餐字段始终以账号池为准。"""
    if not account:
        return out
    # 已经落入账号池的邮箱不可能再用于注册。即使旧数据或异步收尾曾把
    # 原始池状态写回 available，列表也必须按已使用展示。
    if (out.get("status") or "available") == "available":
        out["status"] = "used"
        out["used_at"] = out.get("used_at") or account.get("created_at")
    out["registered_account_id"] = account.get("id")
    out["access_token"] = account.get("access_token")
    out["access_token_preview"] = (
        (account.get("access_token") or "")[:40] + "..."
        if account.get("access_token")
        else ""
    )
    out["account_copy_line"] = _account_line(account)
    out["totp_secret"] = account.get("totp_secret")
    for key in _POOL_ACCOUNT_PLAN_FIELDS:
        if key in account:
            out[key] = account.get(key)
    return out


def _decorate_outlook(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _outlook_line(out)
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    return _attach_pool_account(out, account)


def _decorate_generic_api_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _generic_api_email_line(out)
    out["password"] = out.get("password") or ""
    out["client_id"] = out.get("client_id") or ""
    out["refresh_token"] = out.get("refresh_token") or ""
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    return _attach_pool_account(out, account)


def _decorate_domain_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = out.get("email") or ""
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    return _attach_pool_account(out, account)


def _get_conn() -> None:
    """兼容旧入口：初始化文件存储目录。"""
    _ensure_storage()
    return None


def _row_to_dict(row: dict | None) -> dict | None:
    return dict(row) if row is not None else None


# ============================================================
# registered_accounts
# ============================================================


def save_security_checkpoint(
    email: str,
    *,
    registration_password: str | None = None,
    totp_secret: str | None = None,
    access_token: str | None = None,
) -> dict | None:
    """幂等保存已被服务端确认的密码/TOTP 中间态。

    这里刻意只写独立 checkpoint 文件：不会创建正式账号、不会标记邮箱池
    ``used/completed``，也不会触发批次归档或套餐查询。空值不覆盖已有值。
    """
    normalized_email = str(email or "").strip().lower()
    password_value = str(registration_password or "").strip()
    secret_value = str(totp_secret or "").strip()
    token_value = str(access_token or "").strip()
    if not normalized_email or not (password_value or secret_value or token_value):
        return None

    with _LOCK, _security_checkpoint_file_lock():
        rows = _load_security_checkpoints()
        row = _find_by_email(rows, normalized_email)
        now = _now()
        if row is None:
            row = {
                "email": normalized_email,
                "pending": True,
                "created_at": now,
            }
            rows.append(row)
        if password_value:
            row["registration_password"] = password_value
            row["password_confirmed_at"] = now
        if secret_value:
            row["totp_secret"] = secret_value
            row["totp_activated_at"] = now
        if token_value:
            row["access_token"] = token_value
        row["updated_at"] = now
        _save_security_checkpoints(rows)
        return dict(row)


def get_security_checkpoint(email: str) -> dict | None:
    """读取某邮箱尚未并入正式账号的安全凭据检查点。"""
    normalized_email = str(email or "").strip().lower()
    if not normalized_email:
        return None
    with _LOCK, _security_checkpoint_file_lock():
        row = _find_by_email(_load_security_checkpoints(), normalized_email)
        return dict(row) if row is not None else None


def _checkpoint_extra(
    extra: dict | None,
    checkpoint: dict | None,
    existing_extra_json: object = None,
) -> dict | None:
    """合并既有元数据、checkpoint 与本次终态，避免局部更新抹掉旧字段。

    优先级为 ``existing < checkpoint < explicit extra``；但显式空密码不覆盖
    checkpoint 中已由服务端确认的密码。
    """
    existing_extra: dict = {}
    if isinstance(existing_extra_json, dict):
        existing_extra = dict(existing_extra_json)
    elif isinstance(existing_extra_json, str) and existing_extra_json.strip():
        try:
            decoded = json.loads(existing_extra_json)
            if isinstance(decoded, dict):
                existing_extra = decoded
        except (TypeError, ValueError, json.JSONDecodeError):
            existing_extra = {}

    merged = dict(existing_extra)
    checkpoint_password = str((checkpoint or {}).get("registration_password") or "").strip()
    if checkpoint_password:
        merged["registration_password"] = checkpoint_password

    explicit = dict(extra or {})
    explicit_password = str(explicit.get("registration_password") or "").strip()
    merged.update(explicit)
    if checkpoint_password and not explicit_password:
        merged["registration_password"] = checkpoint_password
    return merged or None


def insert_account(
    *,
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    registration_name: str | None = None,
    birth_date: str | None = None,
    registration_exit_ip: str | None = None,
    registration_exit_country: str | None = None,
    openai_created_at: str | None = None,
    plan_type: str | None = None,
    expires_at: str | None = None,
    device_id: str | None = None,
    proxy_used: str | None = None,
    email_source: str | None = None,
    extra: dict | None = None,
    codex_status: str | None = None,   # success / failed / skipped / missing
    codex_error: str | None = None,    # 失败原因（仅 codex_status=failed 时有意义）
) -> int:
    """插入或更新注册成功账号，返回本地文件中的 id。"""
    normalized_email = str(email or "").strip()
    if not normalized_email:
        raise ValueError("email 不能为空")
    with _LOCK, _security_checkpoint_file_lock():
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        generic_rows = _load_generic_api_emails()
        domain_rows = _load_domain_pool()
        existing = _find_by_email(accounts, normalized_email)
        outlook_row = _find_by_email(outlook_rows, normalized_email)
        generic_row = _find_by_email(generic_rows, normalized_email)
        domain_row = _find_domain_email(domain_rows, normalized_email)
        checkpoints = _load_security_checkpoints()
        checkpoint = _find_by_email(checkpoints, normalized_email)
        merged_extra = _checkpoint_extra(
            extra,
            checkpoint,
            existing.get("extra_json") if existing is not None else None,
        )
        extra_json = json.dumps(merged_extra, ensure_ascii=False) if merged_extra else None
        checkpoint_token = str((checkpoint or {}).get("access_token") or "").strip()
        effective_access_token = str(access_token or "").strip() or checkpoint_token
        checkpoint_secret = str((checkpoint or {}).get("totp_secret") or "").strip()
        explicit_secret = str(totp_secret or "").strip()
        # 空字符串与 None 都表示“本次未提供”，不能覆盖已确认的 checkpoint Secret。
        # 真正清除 TOTP 应走专用账号编辑/解绑流程，而不是注册终态 upsert。
        effective_totp_secret = explicit_secret or checkpoint_secret or None

        if existing is None:
            row_id = _next_id(accounts)
            row = {
                "id": row_id,
                "email": normalized_email,
                "created_at": _now(),
            }
            accounts.append(row)
        else:
            row = existing
            row_id = int(row["id"])

        stored_registration_name = (
            registration_name if registration_name is not None else row.get("registration_name")
        )
        stored_user_name = user_name or row.get("user_name") or stored_registration_name
        row.update({
            "access_token": effective_access_token,
            "totp_secret": (
                effective_totp_secret
                if effective_totp_secret is not None
                else row.get("totp_secret")
            ),
            "user_id": user_id if user_id is not None else row.get("user_id"),
            "user_name": stored_user_name,
            "registration_name": stored_registration_name,
            "birth_date": birth_date if birth_date is not None else row.get("birth_date"),
            "registration_exit_ip": registration_exit_ip if registration_exit_ip is not None else row.get("registration_exit_ip"),
            "registration_exit_country": registration_exit_country if registration_exit_country is not None else row.get("registration_exit_country"),
            "openai_created_at": openai_created_at if openai_created_at is not None else row.get("openai_created_at"),
            "plan_type": plan_type if plan_type is not None else row.get("plan_type"),
            "expires_at": expires_at if expires_at is not None else row.get("expires_at"),
            "device_id": device_id if device_id is not None else row.get("device_id"),
            "proxy_used": proxy_used if proxy_used is not None else row.get("proxy_used"),
            "email_source": email_source if email_source is not None else row.get("email_source"),
            "extra_json": extra_json if extra_json is not None else row.get("extra_json"),
            "codex_status": codex_status if codex_status is not None else row.get("codex_status"),
            "codex_error": codex_error if codex_error is not None else row.get("codex_error"),
            "updated_at": _now(),
        })

        if outlook_row:
            row["password"] = outlook_row.get("password")
            row["client_id"] = outlook_row.get("client_id")
            row["refresh_token"] = outlook_row.get("refresh_token")
            row["original_email_line"] = _outlook_line(outlook_row)
            outlook_row["status"] = "used"
            outlook_row["used_at"] = outlook_row.get("used_at") or _now()
            outlook_row["registered_account_id"] = row_id
            outlook_row["access_token"] = effective_access_token
            outlook_row["completed_at"] = _now()
            if effective_totp_secret:
                outlook_row["totp_secret"] = effective_totp_secret

        for pool_row in (generic_row, domain_row):
            if not pool_row:
                continue
            pool_row["status"] = "used"
            pool_row["used_at"] = pool_row.get("used_at") or _now()
            pool_row["registered_account_id"] = row_id
            pool_row["access_token"] = effective_access_token
            pool_row["completed_at"] = _now()
            if effective_totp_secret:
                pool_row["totp_secret"] = effective_totp_secret

        row["copy_line"] = _account_line(row)
        _save_accounts(accounts)
        _save_outlook(outlook_rows)
        _save_generic_api_emails(generic_rows)
        _save_domain_pool(domain_rows)
        if checkpoint is not None:
            target = str(email or "").strip().lower()
            try:
                _save_security_checkpoints([
                    item
                    for item in checkpoints
                    if str(item.get("email") or "").strip().lower() != target
                ])
            except Exception as exc:
                # 正式账号及邮箱池已经 durable save；清理失败只会留下可幂等消费的
                # pending checkpoint，不能把已成功的注册反报为失败。
                logger.warning("安全凭据 checkpoint 清理失败，将保留待下次合并：%s", type(exc).__name__)
        return row_id


def update_account_codex_status(email: str, codex_status: str, codex_error: str | None = None) -> bool:
    """
    单独更新某账号的 codex_status / codex_error（手动补跑 Codex 时用）。
    返回是否找到该账号。
    """
    with _LOCK:
        accounts = _load_accounts()
        row = _find_by_email(accounts, email)
        if row is None:
            return False
        row["codex_status"] = codex_status
        row["codex_error"] = codex_error
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def claim_account_codex_agent(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 Codex Agent Token 生成任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("codex_agent_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "codex_agent_queued_at" if current_status == "queued" else "codex_agent_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["codex_agent_status"] = "queued"
        row["codex_agent_ok"] = False
        row["codex_agent_trigger"] = str(trigger or "manual")
        row["codex_agent_queued_at"] = now
        row["codex_agent_started_at"] = None
        row["codex_agent_completed_at"] = None
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_codex_agent_running(acc_id: int) -> bool:
    """把 Codex Agent Token 生成任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("codex_agent_status") not in {"queued", "running"}:
            return False
        row["codex_agent_status"] = "running"
        row["codex_agent_started_at"] = _now()
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "正在生成 Codex Agent Token"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_codex_agent(acc_id: int, result: dict | None = None) -> bool:
    """更新账号 Codex Agent Token 生成结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["codex_agent_status"] = status
        row["codex_agent_ok"] = ok
        row["codex_agent_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["codex_agent_completed_at"] = _now()
        row["codex_agent_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["codex_agent_message"] = result.get("message")
        if result.get("agent_runtime_id") is not None:
            row["codex_agent_runtime_id"] = result.get("agent_runtime_id")
        if result.get("auth_path") is not None:
            row["codex_agent_auth_path"] = result.get("auth_path")
        if isinstance(result.get("auth_json"), dict):
            row["codex_agent_token"] = json.dumps(result.get("auth_json"), ensure_ascii=False)
        for _k in (
            "codex_agent_network_route",
            "codex_agent_proxy_mode",
            "codex_agent_proxy_used",
            "codex_agent_proxy_fallback_reason",
            "codex_agent_device_id",
            "codex_agent_oai_session_id",
            "codex_agent_attempt_count",
            "codex_agent_max_attempts",
            "codex_agent_request_timeout",
            "codex_agent_sub2api_path",
            "codex_agent_sub2api_url",
            "codex_agent_sub2api_mode",
            "codex_agent_sub2api_total",
        ):
            src_key = _k.replace("codex_agent_", "", 1)
            if result.get(src_key) is not None:
                row[_k] = result.get(src_key)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_codex_agents() -> int:
    """服务启动时恢复上次进程中断的 Codex Agent 任务状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("codex_agent_status") not in {"queued", "running"}:
                continue
            row["codex_agent_status"] = "failed"
            row["codex_agent_ok"] = False
            row["codex_agent_error"] = "WebUI 重启导致 Codex Agent Token 任务中断，请重新生成"
            row["codex_agent_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def claim_account_checkout_kind(acc_id: int, trigger: str = "manual") -> bool:
    """Atomically reserve a non-confirming OAICS/CSLIVE detection task."""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("checkout_kind_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "checkout_kind_queued_at" if current_status == "queued" else "checkout_kind_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["checkout_kind_status"] = "queued"
        row["checkout_kind_ok"] = False
        row["checkout_kind_trigger"] = str(trigger or "manual")
        row["checkout_kind_queued_at"] = now
        row["checkout_kind_started_at"] = None
        row["checkout_kind_completed_at"] = None
        row["checkout_kind_error"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_checkout_kind_running(acc_id: int) -> bool:
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("checkout_kind_status") not in {"queued", "running"}:
            return False
        row["checkout_kind_status"] = "running"
        row["checkout_kind_started_at"] = _now()
        row["checkout_kind_error"] = None
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_checkout_kind(acc_id: int, result: dict | None = None) -> bool:
    """Persist only classification metadata; never persist the AT or full Checkout URL."""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        ok = bool(result.get("ok"))
        row["checkout_kind_status"] = "success" if ok else "failed"
        row["checkout_kind_ok"] = ok
        row["checkout_kind"] = str(result.get("kind") or "unknown")[:32]
        row["checkout_kind_provider"] = str(result.get("checkout_provider") or "")[:80]
        row["checkout_kind_processor"] = str(result.get("processor_entity") or "")[:80]
        row["checkout_kind_session_prefix"] = str(result.get("session_prefix") or "")[:32]
        row["checkout_kind_confirm_sent"] = bool(result.get("confirm_sent"))
        row["checkout_kind_checked_at"] = result.get("checked_at") or _now()
        row["checkout_kind_completed_at"] = _now()
        row["checkout_kind_error"] = None if ok else str(result.get("error") or "检测失败")[:500]
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_checkout_kind_checks() -> int:
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("checkout_kind_status") not in {"queued", "running"}:
                continue
            row["checkout_kind_status"] = "failed"
            row["checkout_kind_ok"] = False
            row["checkout_kind_error"] = "WebUI 重启导致 Checkout 类型检测中断，请重新检测"
            row["checkout_kind_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def claim_account_gcash(acc_id: int, trigger: str = "manual") -> bool:
    """Atomically reserve a non-confirming GCash eligibility detection task."""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("gcash_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "gcash_queued_at" if current_status == "queued" else "gcash_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["gcash_status"] = "queued"
        row["gcash_ok"] = False
        row["gcash_eligible"] = False
        row["gcash_trigger"] = str(trigger or "manual")
        row["gcash_payment_method_id"] = ""
        row["gcash_error"] = None
        row["gcash_queued_at"] = now
        row["gcash_started_at"] = None
        row["gcash_completed_at"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_gcash_running(acc_id: int) -> bool:
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("gcash_status") not in {"queued", "running"}:
            return False
        row["gcash_status"] = "running"
        row["gcash_started_at"] = _now()
        row["gcash_error"] = None
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_gcash(acc_id: int, result: dict | None = None) -> bool:
    """Persist GCash threshold metadata only; never the AT or Checkout URL."""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        gcash = bool(result.get("gcash"))
        ok = bool(result.get("ok")) or gcash
        row["gcash_status"] = "success" if ok else "failed"
        row["gcash_ok"] = ok
        row["gcash_eligible"] = gcash
        row["gcash_payment_method_id"] = str(result.get("custom_payment_method_id") or "")[:80]
        row["gcash_checkout_country"] = str(result.get("checkout_country") or "PH")[:8]
        row["gcash_checkout_currency"] = str(result.get("checkout_currency") or "PHP")[:8]
        row["gcash_checked_at"] = result.get("checked_at") or _now()
        row["gcash_completed_at"] = _now()
        row["gcash_error"] = None if ok else str(result.get("error") or "GCash 检测失败")[:500]
        row["gcash_attempt_count"] = int(result.get("attempt_count") or 1)
        row["gcash_retried_proxies"] = result.get("retried_proxies")
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_gcash_checks() -> int:
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("gcash_status") not in {"queued", "running"}:
                continue
            row["gcash_status"] = "failed"
            row["gcash_ok"] = False
            row["gcash_eligible"] = False
            row["gcash_error"] = "WebUI 重启导致 GCash 检测中断，请重新检测"
            row["gcash_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def claim_account_oaics_extract(acc_id: int, trigger: str = "manual") -> bool:
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("oaics_extract_status") in {"queued", "running"}:
            return False
        now = _now()
        row.update({
            "oaics_extract_status": "queued", "oaics_extract_ok": False,
            "oaics_extract_error": None, "oaics_extract_trigger": str(trigger or "manual"),
            "oaics_extract_stage": "\u6392\u961f\u7b49\u5f85\u540e\u53f0\u7ebf\u7a0b",
            "oaics_extract_queued_at": now, "oaics_extract_started_at": None,
            "oaics_extract_completed_at": None, "updated_at": now,
        })
        _save_accounts(accounts)
        return True


def mark_account_oaics_extract_running(acc_id: int) -> bool:
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("oaics_extract_status") not in {"queued", "running"}:
            return False
        row["oaics_extract_status"] = "running"
        row["oaics_extract_stage"] = "\u51c6\u5907 PayPal OAICS \u6838\u5fc3"
        row["oaics_extract_started_at"] = _now()
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_oaics_extract_progress(acc_id: int, stage: str, detail: str = "") -> bool:
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("oaics_extract_status") not in {"queued", "running"}:
            return False
        clean_stage = str(stage or "\u63d0\u94fe\u5904\u7406\u4e2d").strip()[:120]
        clean_detail = str(detail or "").strip()[:240]
        row["oaics_extract_stage"] = clean_stage
        row["oaics_extract_log"] = clean_detail
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_oaics_extract(acc_id: int, result: dict | None = None) -> bool:
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        link = str(result.get("paypal_link") or result.get("url") or "").strip()
        ok = bool(link)
        row.update({
            "oaics_extract_status": "success" if ok else "failed",
            "oaics_extract_stage": "\u63d0\u94fe\u5b8c\u6210" if ok else "\u63d0\u94fe\u5931\u8d25",
            "oaics_extract_log": "\u5df2\u53d6\u5f97 PayPal BA \u94fe\u63a5" if ok else str(result.get("error") or "")[:240],
            "oaics_extract_ok": ok,
            "oaics_extract_error": None if ok else str(result.get("error") or "提链失败")[:500],
            "oaics_extract_completed_at": result.get("checked_at") or _now(),
            "oaics_link": link if ok else row.get("oaics_link") or "",
            "oaics_session_id": str(result.get("checkout_session_id") or "")[:80],
            "updated_at": _now(),
        })
        _save_accounts(accounts)
        if ok:
            group = ensure_account_group("OAICS账号")
            group_id = str(group.get("id") or "")
            if group_id:
                add_accounts_to_group(group_id, [int(acc_id)])
        return True


def recover_interrupted_oaics_extracts() -> int:
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        for row in accounts:
            if row.get("oaics_extract_status") not in {"queued", "running"}:
                continue
            row["oaics_extract_status"] = "failed"
            row["oaics_extract_stage"] = "\u56e0\u91cd\u542f\u4e2d\u65ad"
            row["oaics_extract_ok"] = False
            row["oaics_extract_error"] = "WebUI 重启导致 OAICS 提链中断，请重新提链"
            row["oaics_extract_completed_at"] = _now()
            row["updated_at"] = _now()
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def update_account_jp_trial(acc_id: int, result: dict | None = None) -> bool:
    """保存 JP 资格结论；检测失败绝不写成无资格。"""
    result = result or {}
    if bool(result.get("rate_limited")) or int(result.get("http_status") or 0) == 429:
        return False
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        ok = bool(result.get("ok"))
        if ok:
            eligible = bool(result.get("eligible"))
            row["jp_trial_status"] = "eligible" if eligible else "ineligible"
            row["jp_trial_eligible"] = eligible
            row["jp_trial_evidence"] = str(result.get("evidence") or "")[:500] or None
            row["jp_trial_error"] = None
        else:
            row["jp_trial_status"] = "failed"
            row["jp_trial_eligible"] = None
            row["jp_trial_evidence"] = None
            row["jp_trial_error"] = str(result.get("error") or "检测失败")[:500]
        row["jp_trial_checked_at"] = result.get("checked_at") or now
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def claim_account_plan_check(
    acc_id: int | None = None,
    email: str | None = None,
    trigger: str = "manual",
) -> bool:
    """原子占用账号的套餐查询；已有未超时查询时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        current_status = row.get("plan_check_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "plan_check_queued_at" if current_status == "queued" else "plan_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass

        now = _now()
        row["plan_check_status"] = "queued"
        row["plan_check_trigger"] = str(trigger or "manual")
        row["plan_check_queued_at"] = now
        row["plan_check_started_at"] = None
        row["plan_check_completed_at"] = None
        row["plan_check_error"] = None
        row["plan_check_proxy_country"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_plan_check_running(acc_id: int, proxy_country: str | None = None) -> bool:
    """把已排队的套餐查询标记为执行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("plan_check_status") not in {"queued", "running"}:
            return False
        row["plan_check_status"] = "running"
        row["plan_check_started_at"] = _now()
        row["plan_check_error"] = None
        normalized_country = str(proxy_country or "").strip().upper()
        row["plan_check_proxy_country"] = (
            normalized_country
            if len(normalized_country) == 2 and normalized_country.isalpha()
            else None
        )
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_plan_checks() -> int:
    """服务启动时把上次进程遗留的内存队列状态恢复为可重试失败。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("plan_check_status") not in {"queued", "running"}:
                continue
            row["plan_check_status"] = "failed"
            row["plan_check_ok"] = False
            row["plan_check_error"] = "WebUI 重启导致套餐查询中断，请重新查询"
            row["plan_check_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def update_account_plan_check(acc_id: int | None = None, email: str | None = None, result: dict | None = None) -> bool:
    """更新账号套餐/Plus 试用资格查询结果。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        ok = bool(result.get("ok"))
        row["plan_check_status"] = "success" if ok else "failed"
        row["plan_check_ok"] = ok
        row["plan_checked_at"] = result.get("checked_at") or _now()
        row["plan_check_completed_at"] = _now()
        row["plan_check_http_status"] = result.get("http_status")
        row["plan_check_error"] = None if ok else result.get("error")
        authority = str(result.get("plan_authority") or "").strip().lower()
        capability = str(result.get("plan_detection_capability") or "").strip().lower()
        if ok and authority not in {"none", "promo_only"} and capability != "promo_only":
            row["plan_detection_status"] = "confirmed"
            row["mail_plus_promoted"] = False
        elif "ok" in result:
            row["plan_detection_status"] = "inconclusive"

        for key in (
            "plan_detection_source", "plan_detection_capability", "plan_authority",
            "plan_confidence", "plan_evidence_path", "plan_evidence_scope",
            "subscription_status", "plan_terminal_code", "status_code",
        ):
            if result.get(key) is not None:
                row[key] = result.get(key)

        if result.get("account_id"):
            row["account_id"] = result.get("account_id")
        # 查询失败只更新本次错误和网络信息，不覆盖上一次成功拿到的套餐、
        # 试用资格、优惠及有效期，避免临时网络故障把真实权益清空。
        if ok:
            row["plan_terminal_code"] = result.get("plan_terminal_code")
            row["status_code"] = result.get("status_code") or "ok"
            if result.get("current_plan_type"):
                row["current_plan_type"] = result.get("current_plan_type")
                row["plan_type"] = result.get("current_plan_type")
            if result.get("subscription_plan") is not None:
                row["subscription_plan"] = result.get("subscription_plan")
            if result.get("has_active_subscription") is not None:
                row["has_active_subscription"] = bool(result.get("has_active_subscription"))
            if result.get("has_active_plus_subscription") is not None:
                row["has_active_plus_subscription"] = bool(result.get("has_active_plus_subscription"))
            if result.get("is_free_plan") is not None:
                row["is_free_plan"] = bool(result.get("is_free_plan"))
            if result.get("subscription_status") is None:
                if result.get("has_active_subscription") is True:
                    row["subscription_status"] = "active"
                elif result.get("is_free_plan") is True or str(result.get("current_plan_type") or "").lower() == "free":
                    row["subscription_status"] = "free"
            if result.get("expires_at") is not None:
                row["plan_expires_at"] = result.get("expires_at")
            if result.get("renews_at") is not None:
                row["plan_renews_at"] = result.get("renews_at")
            if result.get("cancels_at") is not None:
                row["plan_cancels_at"] = result.get("cancels_at")
            if result.get("billing_period") is not None:
                row["billing_period"] = result.get("billing_period")
            if result.get("billing_currency") is not None:
                row["billing_currency"] = result.get("billing_currency")
            if result.get("is_delinquent") is not None:
                row["is_delinquent"] = bool(result.get("is_delinquent"))
            for _k in (
                "discount_type",
                "discount_amount",
                "discount_duration_num_periods",
                "discount_expires_at",
                "discount_cancellation_policy",
                "discount_promo_campaign_id",
                "last_purchase_origin_platform",
                "last_will_renew",
            ):
                if result.get(_k) is not None:
                    row[_k] = result.get(_k)

            if not result.get("preserve_plus_trial_eligibility"):
                row["plus_trial_eligible"] = bool(result.get("plus_trial_eligible"))
                row["plus_trial_campaign_id"] = result.get("plus_trial_campaign_id")
                row["plus_trial_title"] = result.get("plus_trial_title")
                row["plus_trial_summary"] = result.get("plus_trial_summary")
                row["plus_trial_discount_percentage"] = result.get("plus_trial_discount_percentage")
                row["plus_trial_duration_num_periods"] = result.get("plus_trial_duration_num_periods")
                row["plus_trial_duration_period"] = result.get("plus_trial_duration_period")
                row["plus_trial_promotion_type_label"] = result.get("plus_trial_promotion_type_label")
                row["plus_trial_offer_kind"] = result.get("plus_trial_offer_kind") or "none"
                row["plus_trial_offer_label"] = result.get("plus_trial_offer_label")
                row["plus_trial_offer_percentage"] = result.get("plus_trial_offer_percentage")
                row["plus_trial_offer_evidence"] = result.get("plus_trial_offer_evidence")
                row["eligible_offer_ids"] = result.get("eligible_offer_ids") or []
            row["plan_last_success_at"] = result.get("checked_at") or _now()
            row["plan_last_success_result_json"] = json.dumps(result, ensure_ascii=False)
        row["plan_check_proxy_mode"] = result.get("proxy_mode")
        row["plan_check_network_route"] = result.get("network_route")
        row["plan_check_proxy_used"] = result.get("proxy_used")
        row["plan_check_proxy_fallback_reason"] = result.get("proxy_fallback_reason")
        if "plan_check_proxy_country" in result:
            normalized_country = str(result.get("plan_check_proxy_country") or "").strip().upper()
            row["plan_check_proxy_country"] = (
                normalized_country
                if len(normalized_country) == 2 and normalized_country.isalpha()
                else None
            )
        row["token_expired"] = result.get("token_expired")
        row["token_expires_at"] = result.get("token_expires_at")
        row["plan_check_result_json"] = json.dumps(result, ensure_ascii=False)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def _account_matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _account_matches_at_validity_filter(row: dict, at_filter: str | None) -> bool:
    value = str(at_filter or "").strip().lower().replace("_", "-")
    if not value:
        return True
    status = str(row.get("at_validity_status") or "unchecked").strip().lower()
    if value in {"invalid-or-error", "invalid-error", "problem", "problems"}:
        return status in {"invalid_confirmed", "check_error"}
    if value in {"invalid", "invalid-confirmed"}:
        return status == "invalid_confirmed"
    if value in {"error", "check-error"}:
        return status == "check_error"
    if value == "valid":
        return status == "valid"
    if value == "unchecked":
        return status in {"", "unchecked"}
    return True


def _filtered_decorated_accounts(
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    q: str | None = None,
    at_filter: str | None = None,
) -> list[dict]:
    rows = _load_accounts()
    if archived in (True, "1", "true", "yes", "only"):
        rows = [r for r in rows if bool(r.get("archived"))]
    elif archived in ("all", "include"):
        pass
    else:
        rows = [r for r in rows if not bool(r.get("archived"))]
    decorated = [_decorate_account(r) for r in rows]
    decorated = [r for r in decorated if _account_matches_plan_filter(r, plan_filter)]
    decorated = [r for r in decorated if _account_matches_at_validity_filter(r, at_filter)]
    decorated = [r for r in decorated if _account_matches_query(r, q)]
    return sorted(decorated, key=lambda x: int(x.get("id") or 0), reverse=True)


def list_account_plan_check_statuses(limit: int = 5000, offset: int = 0, archived: str | bool | None = False, plan_filter: str | None = None, q: str | None = None, at_filter: str | None = None) -> dict:
    """返回不含 Token/邮箱密码的套餐查询轻量状态快照。"""
    fields = (
        "id", "email", "archived",
        "plan_type", "current_plan_type", "subscription_plan", "has_active_subscription",
        "has_active_plus_subscription", "is_free_plan", "plus_trial_eligible",
        "plus_trial_offer_kind", "plus_trial_offer_label", "plus_trial_offer_percentage",
        "plus_trial_offer_evidence", "plus_trial_campaign_id", "plus_trial_title",
        "plus_trial_summary", "plus_trial_discount_percentage",
        "plus_trial_duration_num_periods", "plus_trial_duration_period",
        "plus_trial_promotion_type_label",
        "plan_check_status", "plan_check_ok", "plan_check_error",
        "plan_check_trigger", "plan_check_queued_at", "plan_check_started_at",
        "plan_check_completed_at", "plan_checked_at", "plan_last_success_at",
        "plan_check_network_route", "plan_check_proxy_used", "plan_check_proxy_fallback_reason",
        "plan_check_proxy_country",
        "at_validity_status", "at_validity_valid", "at_validity_checked_at",
        "at_validity_http_status", "at_validity_error_code", "at_validity_error", "at_validity_trigger",
        "at_validity_network_route", "at_validity_proxy_used", "at_validity_proxy_source",
        "at_validity_proxy_fallback_reason", "at_validity_attempt_count",
        "expires_at", "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        "checkout_kind_status", "checkout_kind_ok", "checkout_kind",
        "checkout_kind_provider", "checkout_kind_processor",
        "checkout_kind_session_prefix", "checkout_kind_confirm_sent",
        "checkout_kind_checked_at", "checkout_kind_error",
        "gcash_status", "gcash_ok", "gcash_eligible",
        "gcash_payment_method_id", "gcash_checkout_country", "gcash_checkout_currency",
        "gcash_checked_at", "gcash_error", "gcash_completed_at",
        "oaics_extract_status", "oaics_extract_ok", "oaics_extract_error",
        "oaics_extract_stage", "oaics_extract_log", "oaics_extract_started_at", "oaics_extract_completed_at", "oaics_link",
        "jp_trial_status", "jp_trial_eligible", "jp_trial_evidence", "jp_trial_error",
        "jp_trial_checked_at",
        "codex_status", "codex_error",
        "codex_agent_status", "codex_agent_message",
        "codex_agent_runtime_id", "codex_agent_sub2api_url",
        "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
    )
    with _LOCK:
        all_rows = _filtered_decorated_accounts(archived=archived, plan_filter=plan_filter, q=q, at_filter=at_filter)
        total = len(all_rows)
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        rows = all_rows[offset: offset + limit]
        items = []
        for row in rows:
            item = {"id": row.get("id"), "email": row.get("email")}
            for key in fields:
                value = row.get(key)
                if key in ("id", "email"):
                    continue
                if value is not None and value != "":
                    item[key] = value
            item.update({
                "jp_trial_status": row.get("jp_trial_status") or "unchecked",
                "jp_trial_eligible": row.get("jp_trial_eligible"),
                "jp_trial_evidence": row.get("jp_trial_evidence"),
                "jp_trial_error": row.get("jp_trial_error"),
                "jp_trial_checked_at": row.get("jp_trial_checked_at"),
            })
            plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
            if not any(x in plan for x in ("plus", "pro", "team", "go")):
                for expire_key in ("expires_at", "plan_expires_at", "plan_renews_at", "renews_at"):
                    item.pop(expire_key, None)
            item["codex_agent_has_token"] = bool(str(row.get("codex_agent_token") or "").strip())
            item["has_access_token"] = bool(str(row.get("access_token") or "").strip())
            items.append(item)
        latest = max((str(row.get("updated_at") or "") for row in all_rows), default="")
        # updated_at 目前只有秒级精度；一次快速查询可能在同一秒内完成
        # queued -> running -> success/failed，导致 revision 不变，前端跳过合并状态，
        # 页面就会一直停在“查询中”。把轻量状态本身纳入签名，保证状态变化可被轮询发现。
        revision_payload = json.dumps(
            [
                {
                    "id": row.get("id"),
                    "updated_at": row.get("updated_at"),
                    "plan_check_status": row.get("plan_check_status"),
                    "plan_check_ok": row.get("plan_check_ok"),
                    "plan_check_error": row.get("plan_check_error"),
                    "plan_check_queued_at": row.get("plan_check_queued_at"),
                    "plan_check_started_at": row.get("plan_check_started_at"),
                    "plan_check_completed_at": row.get("plan_check_completed_at"),
                    "plan_checked_at": row.get("plan_checked_at"),
                    "plan_last_success_at": row.get("plan_last_success_at"),
                    "plan_check_proxy_country": row.get("plan_check_proxy_country"),
                    "at_validity_status": row.get("at_validity_status"),
                    "at_validity_checked_at": row.get("at_validity_checked_at"),
                    "at_validity_error_code": row.get("at_validity_error_code"),
                    "at_validity_network_route": row.get("at_validity_network_route"),
                    "at_validity_proxy_used": row.get("at_validity_proxy_used"),
                    "current_plan_type": row.get("current_plan_type"),
                    "plan_type": row.get("plan_type"),
                    "subscription_plan": row.get("subscription_plan"),
                    "has_active_plus_subscription": row.get("has_active_plus_subscription"),
                    "plus_trial_eligible": row.get("plus_trial_eligible"),
                    "checkout_kind_status": row.get("checkout_kind_status"),
                    "checkout_kind": row.get("checkout_kind"),
                    "gcash_status": row.get("gcash_status"),
                    "gcash_ok": row.get("gcash_ok"),
                    "gcash_eligible": row.get("gcash_eligible"),
                    "oaics_extract_status": row.get("oaics_extract_status"),
                    "oaics_extract_ok": row.get("oaics_extract_ok"),
                    "oaics_extract_error": row.get("oaics_extract_error"),
                    "oaics_extract_stage": row.get("oaics_extract_stage"),
                    "oaics_extract_log": row.get("oaics_extract_log"),
                    "oaics_link": row.get("oaics_link"),
                    "jp_trial_status": row.get("jp_trial_status") or "unchecked",
                    "jp_trial_eligible": row.get("jp_trial_eligible"),
                    "jp_trial_error": row.get("jp_trial_error"),
                    "jp_trial_checked_at": row.get("jp_trial_checked_at"),
                    "codex_status": row.get("codex_status"),
                    "codex_agent_status": row.get("codex_agent_status"),
                }
                for row in all_rows
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        revision_sig = hashlib.sha1(revision_payload.encode("utf-8")).hexdigest()[:12]
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}:{revision_sig}"}


def list_accounts(limit: int = 500, offset: int = 0, archived: str | bool | None = False, plan_filter: str | None = None, q: str | None = None, at_filter: str | None = None) -> list[dict]:
    with _LOCK:
        rows = _filtered_decorated_accounts(archived=archived, plan_filter=plan_filter, q=q, at_filter=at_filter)
        return rows[max(0, int(offset or 0)): max(0, int(offset or 0)) + max(1, int(limit))]


def list_accounts_page(limit: int = 50, offset: int = 0, archived: str | bool | None = False, plan_filter: str | None = None, q: str | None = None, at_filter: str | None = None) -> dict:
    with _LOCK:
        rows = _filtered_decorated_accounts(archived=archived, plan_filter=plan_filter, q=q, at_filter=at_filter)
        total = len(rows)
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        items = rows[offset: offset + limit]
        latest = max((str(row.get("updated_at") or "") for row in rows), default="")
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}"}


def get_account(acc_id: int) -> dict | None:
    with _LOCK:
        row = next((r for r in _load_accounts() if int(r.get("id") or 0) == int(acc_id)), None)
        return _decorate_account(row) if row else None


def get_account_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_accounts(), email)
        return _decorate_account(row) if row else None


def reset_account_at_validity(acc_id: int) -> bool:
    """新 AT 落库后清除旧 token 的有效性结论，等待下一次独立 AT 检测。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        row["at_validity_status"] = "unchecked"
        row["at_validity_valid"] = None
        row["at_validity_checked_at"] = None
        row["at_validity_error_code"] = None
        row["at_validity_error"] = None
        row["at_validity_http_status"] = None
        row["at_validity_network_route"] = None
        row["at_validity_proxy_used"] = None
        row["at_validity_proxy_source"] = None
        row["at_validity_proxy_fallback_reason"] = None
        row["at_validity_attempt_count"] = None
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def _stored_registration_password(row: dict) -> str:
    """读取账号已确认的 OpenAI 密码，供换绑结果按“密码 + 2FA”匹配原账号。"""
    raw = row.get("extra_json")
    if isinstance(raw, dict):
        extra = raw
    else:
        try:
            extra = json.loads(str(raw or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            extra = {}
    if not isinstance(extra, dict):
        return ""
    return str(extra.get("registration_password") or extra.get("chatgpt_password") or "").strip()


def import_rebound_accounts(records: list[dict], target_group_id: str = "default") -> tuple[list[dict], list[dict]]:
    """导入独立换绑分站结果，并把原账号身份迁移到新邮箱。

    密码账号由 ``原密码 + 原2FA`` 匹配；邮箱 API 登录账号由分站导出的
    ``原邮箱 + 原取码URL`` 格式识别，并按原邮箱精确匹配。匹配成功后只替换
    邮箱与 access token，保留账号 ID、套餐、历史和其他业务字段。旧邮箱会从
    全部自定义分组移除，新邮箱只加入用户当前选择的目标分组。
    """
    target = str(target_group_id or "default").strip() or "default"
    if target == "archived":
        raise ValueError("归档分组不能作为换绑结果导入目标")

    with _LOCK:
        accounts = _load_accounts()
        groups = _load_account_groups()
        if target != "default" and not any(
            isinstance(group, dict) and str(group.get("id") or "") == target
            for group in groups
        ):
            raise ValueError("目标分组不存在")

        original_accounts = copy.deepcopy(accounts)
        original_groups = copy.deepcopy(groups)
        updated: list[dict] = []
        skipped: list[dict] = []
        seen_new_emails: set[str] = set()

        for index, raw in enumerate(records or [], start=1):
            new_email = str(raw.get("email") or raw.get("new_email") or "").strip()
            password = str(raw.get("password") or "").strip()
            totp_secret = str(raw.get("totp_secret") or raw.get("mfa_secret") or "").strip()
            old_email_hint = str(raw.get("old_email") or "").strip()
            source_api_url = str(raw.get("source_api_url") or raw.get("api_url") or "").strip()
            access_token = str(raw.get("access_token") or raw.get("at") or "").strip()
            normalized_new = new_email.lower()
            credential_mode = bool(password and totp_secret)
            email_api_mode = bool(
                old_email_hint and "@" in old_email_hint
                and source_api_url.lower().startswith(("http://", "https://"))
            )
            if not (new_email and "@" in new_email and access_token and (credential_mode or email_api_mode)):
                skipped.append({
                    "line": index, "email": new_email,
                    "reason": "需要：新邮箱----密码----2FA----AT，或 新邮箱----原邮箱----原取码URL----AT",
                })
                continue
            if normalized_new in seen_new_emails:
                skipped.append({"line": index, "email": new_email, "reason": "本次导入的新邮箱重复"})
                continue

            if email_api_mode:
                normalized_hint = old_email_hint.lower()
                matches = [
                    row for row in accounts
                    if str(row.get("email") or "").strip().lower() == normalized_hint
                ]
                no_match_reason = "未找到原邮箱完全匹配的主站账号"
                duplicate_reason = "原邮箱匹配到多个账号，已避免误替换"
            else:
                matches = [
                    row for row in accounts
                    if _stored_registration_password(row) == password
                    and str(row.get("totp_secret") or "").replace(" ", "").upper()
                    == totp_secret.replace(" ", "").upper()
                ]
                no_match_reason = "未找到密码和 2FA 同时匹配的原账号"
                duplicate_reason = "密码和 2FA 匹配到多个账号，已避免误替换"
            if not matches:
                skipped.append({"line": index, "email": new_email, "reason": no_match_reason})
                continue
            if len(matches) > 1:
                skipped.append({"line": index, "email": new_email, "reason": duplicate_reason})
                continue
            row = matches[0]
            conflict = next((
                other for other in accounts
                if other is not row and str(other.get("email") or "").strip().lower() == normalized_new
            ), None)
            if conflict is not None:
                skipped.append({"line": index, "email": new_email, "reason": "新邮箱已属于另一个主站账号"})
                continue

            old_email = str(row.get("email") or "").strip()
            normalized_old = old_email.lower()
            now = _now()
            raw_extra = row.get("extra_json")
            if isinstance(raw_extra, dict):
                extra = dict(raw_extra)
            else:
                try:
                    decoded = json.loads(str(raw_extra or "{}"))
                    extra = dict(decoded) if isinstance(decoded, dict) else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    extra = {}
            history = extra.get("email_rebind_history")
            if not isinstance(history, list):
                history = []
            history.append({"from": old_email, "to": new_email, "imported_at": now})
            extra["email_rebind_history"] = history[-20:]
            extra["email_rebind_last"] = {"from": old_email, "to": new_email, "imported_at": now}

            row.update({
                "email": new_email,
                "access_token": access_token,
                "original_email_line": new_email,
                "extra_json": json.dumps(extra, ensure_ascii=False),
                "email_rebind_status": "success",
                "email_rebind_label": "换绑过后的",
                "email_rebind_from": old_email,
                "email_rebound_at": now,
                "archived": False,
                "updated_at": now,
            })
            # 换绑结果带来的是一枚全新 AT；旧 token 的有效/失效结论不能沿用。
            row["at_validity_status"] = "unchecked"
            row["at_validity_valid"] = None
            row["at_validity_checked_at"] = None
            row["at_validity_error_code"] = None
            row["at_validity_error"] = None
            row["at_validity_http_status"] = None
            row["at_validity_network_route"] = None
            row["at_validity_proxy_used"] = None
            row["at_validity_proxy_source"] = None
            row["at_validity_proxy_fallback_reason"] = None
            row["at_validity_attempt_count"] = None
            row.pop("archived_at", None)

            # 分组成员以邮箱保存。先从所有分组清除原/新邮箱，再仅写入当前目标组，
            # 因而“分组1旧邮箱 → 当前分组2新邮箱”的迁移不会留下幽灵成员。
            for group in groups:
                if not isinstance(group, dict):
                    continue
                before = [str(value or "").strip().lower() for value in group.get("emails") or [] if str(value or "").strip()]
                after = [value for value in before if value not in {normalized_old, normalized_new}]
                if str(group.get("id") or "") == target and normalized_new not in after:
                    after.append(normalized_new)
                if after != before:
                    group["emails"] = after
                    group["updated_at"] = now

            seen_new_emails.add(normalized_new)
            updated.append({
                "account_id": int(row.get("id") or 0),
                "old_email": old_email,
                "new_email": new_email,
                "group_id": target,
                "label": "换绑过后的",
            })

        if updated:
            try:
                _save_accounts(accounts)
                _save_account_groups(groups)
            except Exception:
                # 不创建本地备份文件；用内存中的提交前状态恢复两个持久化对象。
                _save_accounts(original_accounts)
                _save_account_groups(original_groups)
                raise
        return updated, skipped


def update_account_note(acc_id: int, note: str) -> bool:
    """更新单个已注册账号备注。note 为空字符串时表示清空备注。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["note"] = str(note or "")
        row["note_updated_at"] = now
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_account_liveness(acc_id: int, result: dict | None = None) -> bool:
    """写回账号查活结果；成功时同步刷新最新 access_token 和账号基础信息。"""
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False

        now = _now()
        ok = bool(result.get("ok"))
        status = str(result.get("status") or ("live" if ok else "failed"))
        row["live_check_status"] = status
        row["live_check_ok"] = ok
        row["live_checked_at"] = result.get("checked_at") or now
        row["live_check_error"] = None if ok else result.get("error")
        row["updated_at"] = now

        if status == "deactivated":
            row["codex_status"] = "deactivated"
            row["codex_error"] = result.get("error") or "账号已删除/停用/封禁"

        if ok:
            token = str(result.get("access_token") or "").strip()
            if token:
                row["access_token"] = token
            session = result.get("session") or {}
            user = session.get("user") or {}
            account = session.get("account") or {}
            if user.get("id"):
                row["user_id"] = user.get("id")
            if user.get("name") is not None:
                row["user_name"] = user.get("name")
            if account.get("planType"):
                row["plan_type"] = account.get("planType")
            if session.get("expires"):
                row["expires_at"] = session.get("expires")
            if result.get("device_id"):
                row["device_id"] = result.get("device_id")
            if result.get("proxy_used"):
                row["live_check_proxy_used"] = result.get("proxy_used")
            row["live_check_error"] = None
            # 重新登录成功并拿到最新 AT，本身就是一次确定的有效性证明。
            row["at_validity_status"] = "valid"
            row["at_validity_valid"] = True
            row["at_validity_checked_at"] = result.get("checked_at") or now
            row["at_validity_error_code"] = None
            row["at_validity_error"] = None
            row["at_validity_http_status"] = None
            row["at_validity_trigger"] = "live-check-refresh"
            row["at_validity_network_route"] = None
            row["at_validity_proxy_used"] = None
            row["at_validity_proxy_source"] = None
            row["at_validity_proxy_fallback_reason"] = None
            row["at_validity_attempt_count"] = 0

        row["copy_line"] = _account_line(row)
        _save_accounts(rows)
        return True


def update_account_at_validity(acc_id: int, result: dict | None = None, trigger: str = "at-validity") -> bool:
    """写回独立 AT 有效性结论，不把临时网络错误折叠成失效。"""
    result = result or {}
    outcome = str(result.get("outcome") or "check_error").strip().lower()
    if outcome not in {"valid", "invalid_confirmed", "check_error"}:
        outcome = "check_error"
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["at_validity_status"] = outcome
        row["at_validity_valid"] = True if outcome == "valid" else False if outcome == "invalid_confirmed" else None
        row["at_validity_checked_at"] = result.get("checked_at") or now
        row["at_validity_http_status"] = result.get("http_status")
        row["at_validity_error_code"] = None if outcome == "valid" else str(result.get("error_code") or "check_error")[:100]
        row["at_validity_error"] = None if outcome == "valid" else str(result.get("error") or "AT 检测失败")[:500]
        row["at_validity_trigger"] = str(trigger or "at-validity")[:80]
        row["at_validity_network_route"] = str(result.get("network_route") or "")[:40] or None
        row["at_validity_proxy_used"] = str(result.get("proxy_used") or "")[:300] or None
        row["at_validity_proxy_source"] = str(result.get("proxy_source") or "")[:80] or None
        row["at_validity_proxy_fallback_reason"] = str(result.get("proxy_fallback_reason") or "")[:300] or None
        try:
            row["at_validity_attempt_count"] = max(0, int(result.get("attempt_count") or 0))
        except (TypeError, ValueError):
            row["at_validity_attempt_count"] = 0
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def claim_account_live_check(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号查活任务；已有 queued/running 时返回 False。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        if row.get("live_check_status") in {"queued", "running"}:
            try:
                stamp_key = "live_check_queued_at" if row.get("live_check_status") == "queued" else "live_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if row.get("live_check_status") == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["live_check_status"] = "queued"
        row["live_check_ok"] = False
        row["live_check_trigger"] = str(trigger or "manual")
        row["live_check_queued_at"] = now
        row["live_check_started_at"] = None
        row["live_checked_at"] = None
        row["live_check_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def recover_interrupted_live_checks() -> int:
    """服务启动时恢复上次进程中断的查活状态，避免 queued/running 卡死。"""
    with _LOCK:
        rows = _load_accounts()
        recovered = 0
        now = _now()
        for row in rows:
            if row.get("live_check_status") not in {"queued", "running"}:
                continue
            row["live_check_status"] = "failed"
            row["live_check_ok"] = False
            row["live_check_error"] = "WebUI 重启或任务异常中断，请重新查活"
            row["live_checked_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(rows)
        return recovered


def mark_account_live_check_running(acc_id: int) -> bool:
    """把账号查活任务标记为运行中。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("live_check_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["live_check_status"] = "running"
        row["live_check_started_at"] = now
        row["live_check_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_accounts_note(account_ids: list[int] | None, note: str) -> tuple[list[dict], list[dict]]:
    """
    批量更新已注册账号备注。
    返回 (updated, skipped)，updated/skipped 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        text = str(note or "")
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["note"] = text
            row["note_updated_at"] = now
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "note": text, "note_updated_at": now})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def archive_account(acc_id: int, archived: bool = True) -> bool:
    """归档/取消归档单个已注册账号。归档不会删除 token，只影响默认账号列表查询。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["archived"] = bool(archived)
        row["archived_at"] = now if archived else None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def archive_accounts(account_ids: list[int] | None, archived: bool = True) -> tuple[list[dict], list[dict]]:
    """批量归档/取消归档账号。返回 (updated, skipped)。"""
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["archived"] = bool(archived)
            row["archived_at"] = now if archived else None
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "archived": bool(archived), "archived_at": row.get("archived_at")})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def count_accounts() -> int:
    with _LOCK:
        return len(_load_accounts())


def delete_account(acc_id: int | None = None, email: str | None = None) -> bool:
    """删除一个已注册账号记录，并同步刷新 注册成功的邮箱.txt / token.txt / 静态查看页。"""
    with _LOCK:
        rows = _load_accounts()
        target_email = (email or "").lower()
        new_rows = []
        deleted = False
        deleted_emails: set[str] = set()
        for row in rows:
            match_id = acc_id is not None and int(row.get("id") or 0) == int(acc_id)
            match_email = bool(target_email) and (row.get("email") or "").lower() == target_email
            if match_id or match_email:
                deleted = True
                deleted_emails.add(str(row.get("email") or "").strip().lower())
                continue
            new_rows.append(row)
        if not deleted:
            return False
        id_mapping = _renumber_accounts_after_delete(new_rows)
        _save_accounts(new_rows)
        _remove_emails_from_account_groups(deleted_emails)
        _remap_account_id_references(id_mapping)
        return True


def delete_accounts(account_ids: list[int] | None = None, emails: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """
    批量删除已注册账号。
    返回 (deleted, skipped)，deleted 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().isdigit()}
    email_set = {(e or "").lower() for e in (emails or []) if e}
    deleted: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        new_rows = []
        seen_ids: set[int] = set()
        seen_emails: set[str] = set()
        for row in rows:
            row_id = int(row.get("id") or 0)
            row_email = (row.get("email") or "").lower()
            if row_id in ids or row_email in email_set:
                deleted.append({"id": row_id, "email": row.get("email")})
                seen_ids.add(row_id)
                seen_emails.add(row_email)
                continue
            new_rows.append(row)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        for item in email_set - seen_emails:
            skipped.append({"email": item, "reason": "账号不存在"})
        if deleted:
            id_mapping = _renumber_accounts_after_delete(new_rows)
            _save_accounts(new_rows)
            _remove_emails_from_account_groups({str(item.get("email") or "").strip().lower() for item in deleted})
            _remap_account_id_references(id_mapping)
    return deleted, skipped


def purge_emails_everywhere(emails: list[str], *, protect_active_jobs: bool = True) -> dict:
    """按邮箱清理所有本地界面对应的数据。

    包括账号/Plus 派生记录、三类邮箱池、邮件检测池、Codex 凭证、
    已结束的注册任务与日志。默认保护仍在排队或运行的任务，避免后台线程
    在清理完成后重新写回数据。
    """
    targets = {
        str(email or "").strip().lower(): str(email or "").strip()
        for email in (emails or [])
        if str(email or "").strip() and "@" in str(email or "")
    }
    result = {
        "requested": len(targets),
        "purged_emails": [],
        "protected": [],
        "counts": {
            "accounts": 0,
            "outlook": 0,
            "generic_api": 0,
            "domain": 0,
            "mail_status": 0,
            "codex": 0,
            "jobs": 0,
            "logs": 0,
        },
    }
    if not targets:
        return result

    log_files: list[str] = []
    with _LOCK:
        jobs = _load_jobs()
        active_states = {"pending", "running", "stopping"}
        protected_keys = {
            str(row.get("email") or "").strip().lower()
            for row in jobs
            if row.get("status") in active_states
            and str(row.get("email") or "").strip().lower() in targets
        } if protect_active_jobs else set()
        result["protected"] = [targets[key] for key in targets if key in protected_keys]
        purge_keys = set(targets) - protected_keys
        if not purge_keys:
            return result

        accounts = _load_accounts()
        kept_accounts = [
            row for row in accounts
            if str(row.get("email") or "").strip().lower() not in purge_keys
        ]
        result["counts"]["accounts"] = len(accounts) - len(kept_accounts)
        if len(kept_accounts) != len(accounts):
            id_mapping = _renumber_accounts_after_delete(kept_accounts)
            _save_accounts(kept_accounts)
            _remove_emails_from_account_groups(set(purge_keys))
            _remap_account_id_references(id_mapping)

        pool_specs = (
            ("outlook", _load_outlook, _save_outlook),
            ("generic_api", _load_generic_api_emails, _save_generic_api_emails),
            ("domain", _load_domain_pool, _save_domain_pool),
            ("mail_status", _load_mail_status_pool, _save_mail_status_pool),
        )
        for label, loader, saver in pool_specs:
            rows = loader()
            kept = [
                row for row in rows
                if str(row.get("email") or "").strip().lower() not in purge_keys
            ]
            result["counts"][label] = len(rows) - len(kept)
            if len(kept) != len(rows):
                saver(kept)

        export_state = _load_codex_export_state()
        codex_changed = False
        if _CODEX_DIR.exists():
            for row in list_codex_accounts():
                if str(row.get("email") or "").strip().lower() not in purge_keys:
                    continue
                path = _CODEX_DIR / str(row.get("filename") or "")
                try:
                    path.unlink(missing_ok=True)
                    result["counts"]["codex"] += 1
                    codex_changed = True
                except Exception:
                    continue
                export_state.pop(path.name, None)
        if codex_changed:
            _save_codex_export_state(export_state)

        # 账号 ID 重排可能已经改写任务文件，因此这里重新读取。
        jobs = _load_jobs()
        kept_jobs = []
        for row in jobs:
            email_key = str(row.get("email") or "").strip().lower()
            if email_key in purge_keys and (not protect_active_jobs or row.get("status") not in active_states):
                result["counts"]["jobs"] += 1
                if row.get("log_file"):
                    log_files.append(str(row.get("log_file")))
                continue
            kept_jobs.append(row)
        if len(kept_jobs) != len(jobs):
            _save_jobs(kept_jobs)

        result["purged_emails"] = [targets[key] for key in targets if key in purge_keys]

    for raw_path in log_files:
        try:
            Path(raw_path).unlink(missing_ok=True)
            result["counts"]["logs"] += 1
        except Exception:
            pass
    return result


# ============================================================
# outlook_pool
# ============================================================

def _email_pool_int(row: dict, key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def email_pool_is_deprioritized(row: dict) -> bool:
    """注册失败或回收过的邮箱在邮箱池和自动领取队列中统一后置。"""
    status = str(row.get("status") or "available").strip().lower()
    note = str(row.get("note") or "").strip().lower()
    return (
        status in {"failed", "disabled"}
        or _email_pool_int(row, "registration_failure_count") > 0
        or _email_pool_int(row, "retry_count") > 0
        or row.get("retry_queue_seq") is not None
        or "移至队尾" in note
        or "moved to back" in note
    )


def _sort_email_pool_rows(rows: list[dict]) -> list[dict]:
    """先按新旧排序，再稳定地把失败/重试记录放到全部正常记录之后。"""
    ordered = sorted(rows, key=lambda item: _email_pool_int(item, "id"), reverse=True)
    return sorted(ordered, key=email_pool_is_deprioritized)


def _email_pool_claim_queue_key(item: dict) -> tuple[int, int, int]:
    """正常新邮箱优先；只有正常邮箱耗尽后，才按队列顺序重试失败邮箱。"""
    retry_seq = item.get("retry_queue_seq")
    has_retry_history = (
        email_pool_is_deprioritized(item)
        or bool(str(item.get("note") or "").strip())
    )
    if not has_retry_history:
        return (0, 0, -_email_pool_int(item, "id"))
    return (1, _email_pool_int(item, "retry_queue_seq"), _email_pool_int(item, "id"))

def import_outlook_accounts(records: list[dict]) -> tuple[int, int]:
    """
    批量导入 Outlook 账号。
    records 元素：{email, password, client_id, refresh_token}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_outlook()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "password": (raw.get("password") or "").strip(),
                "client_id": (raw.get("client_id") or raw.get("clientId") or "").strip(),
                "refresh_token": (raw.get("refresh_token") or raw.get("refreshToken") or "").strip(),
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
                "retry_count": 0,
                "retry_queue_seq": None,
            }
            row["copy_line"] = _outlook_line(row)
            rows.append(row)
            inserted += 1
        _save_outlook(rows)
        return inserted, skipped


def import_registered_email_accounts(records: list[dict], source: str | None) -> tuple[int, int]:
    """
    把邮箱素材直接导入为“已注册成功账号”，用于跳过注册、直接在账号页补跑 Codex 授权。

    source:
      - outlook: records 元素 {email,password,client_id,refresh_token[,access_token,totp_secret]}
      - generic_api/domain_api/inbox_mate: records 元素 {email,code_url[,password,provider,api_base]}

    返回 (新增账号数, 跳过数)。已存在账号会跳过；邮箱池中已存在的素材会复用并标记 used。
    """
    source = (source or "").strip().lower()
    if source not in ("outlook", "generic_api", "domain_api", "inbox_mate"):
        raise ValueError("source 必须显式传入 outlook / generic_api / domain_api / inbox_mate")

    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        generic_rows = _load_generic_api_emails()
        inserted = skipped = 0
        inserted_domains: dict[str, list[int]] = {}

        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(accounts, email):
                skipped += 1
                continue

            now = _now()
            original_line = email
            pool_row = None

            if source in ("generic_api", "domain_api", "inbox_mate"):
                code_url = (raw.get("code_url") or raw.get("url") or "").strip()
                if not code_url:
                    skipped += 1
                    continue
                pool_row = _find_by_email(generic_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(generic_rows),
                        "email": email,
                        "code_url": code_url,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    if source in ("domain_api", "inbox_mate"):
                        pool_row["provider"] = source
                        pool_row["email_domain"] = str(raw.get("email_domain") or email.rsplit("@", 1)[-1]).lower()
                        if source == "inbox_mate":
                            pool_row["password"] = str(raw.get("password") or "")
                            pool_row["api_base"] = str(raw.get("api_base") or "")
                    generic_rows.append(pool_row)
                else:
                    pool_row["code_url"] = code_url or pool_row.get("code_url")
                    if source in ("domain_api", "inbox_mate"):
                        pool_row["provider"] = source
                        pool_row["email_domain"] = str(raw.get("email_domain") or email.rsplit("@", 1)[-1]).lower()
                        if source == "inbox_mate":
                            pool_row["password"] = str(raw.get("password") or pool_row.get("password") or "")
                            pool_row["api_base"] = str(raw.get("api_base") or pool_row.get("api_base") or "")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _generic_api_email_line(pool_row)
                original_line = _generic_api_email_line(pool_row)
            else:
                password = (raw.get("password") or "").strip()
                client_id = (raw.get("client_id") or raw.get("clientId") or "").strip()
                refresh_token = (raw.get("refresh_token") or raw.get("refreshToken") or "").strip()
                if not (password and client_id and refresh_token):
                    skipped += 1
                    continue
                pool_row = _find_by_email(outlook_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(outlook_rows),
                        "email": email,
                        "password": password,
                        "client_id": client_id,
                        "refresh_token": refresh_token,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    outlook_rows.append(pool_row)
                else:
                    pool_row["password"] = password or pool_row.get("password")
                    pool_row["client_id"] = client_id or pool_row.get("client_id")
                    pool_row["refresh_token"] = refresh_token or pool_row.get("refresh_token")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _outlook_line(pool_row)
                original_line = _outlook_line(pool_row)

            row_id = _next_id(accounts)
            access_token = (raw.get("access_token") or raw.get("token") or "").strip()
            totp_secret = (raw.get("totp_secret") or raw.get("totp") or "").strip() or None
            account = {
                "id": row_id,
                "email": email,
                "created_at": now,
                "access_token": access_token,
                "totp_secret": totp_secret,
                "user_id": raw.get("user_id"),
                "user_name": raw.get("user_name") or "Imported Account",
                "plan_type": raw.get("plan_type"),
                "expires_at": raw.get("expires_at"),
                "device_id": raw.get("device_id"),
                "proxy_used": raw.get("proxy_used"),
                "email_source": source,
                "extra_json": json.dumps({"imported_registered": True}, ensure_ascii=False),
                "codex_status": raw.get("codex_status") or "",
                "codex_error": raw.get("codex_error"),
                "updated_at": now,
                "original_email_line": original_line,
            }
            if source == "outlook":
                account["password"] = pool_row.get("password")
                account["client_id"] = pool_row.get("client_id")
                account["refresh_token"] = pool_row.get("refresh_token")
            account["copy_line"] = _account_line(account)
            accounts.append(account)
            if source in ("domain_api", "inbox_mate"):
                domain = str(raw.get("email_domain") or email.rsplit("@", 1)[-1]).strip().lower()
                if domain:
                    inserted_domains.setdefault(domain, []).append(row_id)

            pool_row["registered_account_id"] = row_id
            pool_row["access_token"] = access_token
            if totp_secret:
                pool_row["totp_secret"] = totp_secret
            inserted += 1

        _save_outlook(outlook_rows)
        _save_generic_api_emails(generic_rows)
        _save_accounts(accounts)
        for domain, account_ids in inserted_domains.items():
            group = ensure_account_group(f"域名邮箱 · {domain}")
            add_accounts_to_group(str(group["id"]), account_ids)
        return inserted, skipped


def claim_next_outlook() -> dict | None:
    """原子领取 Outlook：正常新邮箱优先，失败回收邮箱统一后置。"""
    with _LOCK:
        rows = _load_outlook()
        registered_emails = {
            str(account.get("email") or "").strip().lower()
            for account in _load_accounts()
        }
        candidates = [
            r for r in rows
            if r.get("status") == "available"
            and str(r.get("email") or "").strip().lower() not in registered_emails
        ]
        candidates.sort(key=_email_pool_claim_queue_key)
        row = candidates[0] if candidates else None
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_outlook(rows)
        return _decorate_outlook(row)


def claim_email(email: str, source: str) -> dict | None:
    """Atomically reserve one explicitly selected available mailbox.

    The email-pool UI submits both the mailbox address and its pool source. Do
    not fall back to another mailbox: a stale or already-used selection must be
    reported instead of registering with an unexpected address.
    """
    normalized_email = str(email or "").strip()
    normalized_source = str(source or "").strip().lower()
    if not normalized_email or normalized_source not in {
        "outlook", "generic_api", "domain_api", "inbox_mate", "cloudflare_domain",
    }:
        return None

    with _LOCK:
        if _find_by_email(_load_accounts(), normalized_email) is not None:
            return None
        if normalized_source == "outlook":
            rows = _load_outlook()
            row = _find_by_email(rows, normalized_email)
            save = _save_outlook
            decorate = _decorate_outlook
        elif normalized_source == "cloudflare_domain":
            rows = _load_domain_pool()
            row = _find_domain_email(rows, normalized_email)
            save = _save_domain_pool
            decorate = dict
        else:
            rows = _load_generic_api_emails()
            row = _find_by_email(rows, normalized_email)
            provider = str((row or {}).get("provider") or "").strip().lower()
            expected_providers = {
                "generic_api": {"", "generic_api"},
                "domain_api": {"domain_api"},
                "inbox_mate": {"inbox_mate"},
            }
            if provider not in expected_providers[normalized_source]:
                return None
            save = _save_generic_api_emails
            decorate = _decorate_generic_api_email

        if row is None or row.get("status") != "available":
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        save(rows)
        return decorate(row)


def release_outlook(email: str, status: str = "available", note: str | None = None) -> None:
    """把账号状态改回 available，或标记为 used/failed/disabled。"""
    with _LOCK:
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None:
            return
        if status == "available" and _find_by_email(_load_accounts(), email) is not None:
            status = "used"
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_outlook(rows)


def release_unconsumed_outlook(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的 Outlook 邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        row["retry_count"] = int(row.get("retry_count") or 0) + 1
        row["retry_queue_seq"] = max(
            (int(item.get("retry_queue_seq") or 0) for item in rows),
            default=0,
        ) + 1
        if note is not None:
            row["note"] = note
        _save_outlook(rows)
        return True


def delete_outlook(email: str) -> bool:
    """从邮箱池彻底删除一个邮箱（按 email 匹配）。返回是否删到。"""
    with _LOCK:
        rows = _load_outlook()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_outlook(new_rows)
        return True


def list_outlook_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        account_by_email = {
            (a.get("email") or "").lower(): a
            for a in _load_accounts()
        }
        rows = [_decorate_outlook(r, account_by_email) for r in _load_outlook()]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = _sort_email_pool_rows(rows)
        return rows[:limit]


def outlook_pool_summary() -> dict:
    with _LOCK:
        out = {"available": 0, "used": 0, "failed": 0}
        registered_emails = {
            str(account.get("email") or "").strip().lower()
            for account in _load_accounts()
        }
        for row in _load_outlook():
            status = row.get("status") or "available"
            if status == "available" and str(row.get("email") or "").strip().lower() in registered_emails:
                status = "used"
            out[status] = out.get(status, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_outlook_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_outlook(), email)
        return _decorate_outlook(row) if row else None


# ============================================================
# generic_api email pool
# ============================================================

def import_generic_api_emails(records: list[dict]) -> tuple[int, int]:
    """
    批量导入通用 API 取码邮箱。
    records 元素：{email, code_url}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_generic_api_emails()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            code_url = (raw.get("code_url") or raw.get("url") or "").strip()
            if not email or not code_url:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "code_url": code_url,
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            provider = str(raw.get("provider") or "").strip().lower()
            if provider:
                row["provider"] = provider
            email_domain = str(raw.get("email_domain") or "").strip().lower()
            if email_domain:
                row["email_domain"] = email_domain
            for key in ("password", "api_base", "mail_provider"):
                value = str(raw.get(key) or "").strip()
                if value:
                    row[key] = value
            row["copy_line"] = _generic_api_email_line(row)
            rows.append(row)
            inserted += 1
        _save_generic_api_emails(rows)
        return inserted, skipped


def upsert_manual_email_url(email: str, code_url: str) -> dict:
    """保存手动添加账号的取码 URL；已有邮箱只更新用户本次明确提供的 URL。"""
    normalized_email = str(email or "").strip()
    normalized_url = str(code_url or "").strip()
    if not normalized_email or "@" not in normalized_email:
        raise ValueError("邮箱格式无效")
    parsed = urlparse(normalized_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("取码 URL 必须是有效的 http(s) 地址")
    with _LOCK:
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, normalized_email)
        if row is None:
            row = {
                "id": _next_id(rows),
                "email": normalized_email,
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            rows.append(row)
        row["code_url"] = normalized_url
        row["provider"] = "generic_api"
        row["updated_at"] = _now()
        row["copy_line"] = _generic_api_email_line(row)
        _save_generic_api_emails(rows)
        return dict(row)


def quarantine_exhausted_generic_api_emails(
    failure_limit: int,
    provider: str | None = "generic_api",
) -> int:
    """Disable retry-exhausted API mailboxes, including legacy retry records.

    Older Roxy error handling returned a mailbox to ``available`` before the
    service could increment ``registration_failure_count``.  Those rows still
    carry an ASCII exception type in ``note`` and an accurate ``retry_count``.
    Migrate only that mailbox-specific history; proxy/browser failures remain
    reusable and never poison a mailbox.
    """
    limit = max(1, int(failure_limit or 1))
    provider_filter = str(provider or "").strip().lower()
    mailbox_markers = (
        "genericapimailerror",
        "genericapitransporterror",
        "mailbox",
        "pickup endpoint",
        " otp",
        '"code":null',
        "ai100.my",
    )
    with _LOCK:
        rows = _load_generic_api_emails()
        changed = 0
        for row in rows:
            if row.get("status") != "available":
                continue
            row_provider = str(row.get("provider") or "").strip().lower()
            if provider_filter == "generic_api" and row_provider not in ("", "generic_api"):
                continue
            if provider_filter not in ("", "generic_api") and row_provider != provider_filter:
                continue
            failures = int(row.get("registration_failure_count") or 0)
            note = str(row.get("note") or "").lower()
            if failures <= 0 and any(marker in note for marker in mailbox_markers):
                failures = int(row.get("retry_count") or 0)
                if failures:
                    row["registration_failure_count"] = failures
            if failures < limit:
                continue
            row["status"] = "disabled"
            row["used_at"] = row.get("used_at") or _now()
            row["note"] = f"Auto-disabled after {failures} mailbox/OTP failures: {str(row.get('note') or '')[:180]}"
            changed += 1
        if changed:
            _save_generic_api_emails(rows)
        return changed


def claim_next_generic_api_email(provider: str | None = None) -> dict | None:
    """原子领取一个可用通用 API 邮箱并标记为 used。

    优先领取从未失败过的较新记录。旧逻辑固定从最小 ID 开始，导致已经被
    回收并带有失败备注的老邮箱反复排在新邮箱之前；OpenAI 会把这些历史
    邮箱判定为 ``account_deactivated``。
    """
    with _LOCK:
        rows = _load_generic_api_emails()
        registered_emails = {
            str(account.get("email") or "").strip().lower()
            for account in _load_accounts()
        }
        provider_filter = str(provider or "").strip().lower()
        candidates = []
        for row in rows:
            if row.get("status") != "available":
                continue
            if str(row.get("email") or "").strip().lower() in registered_emails:
                continue
            row_provider = str(row.get("provider") or "").strip().lower()
            if provider_filter == "generic_api" and row_provider not in ("", "generic_api"):
                continue
            if provider_filter not in ("", "generic_api") and row_provider != provider_filter:
                continue
            candidates.append(row)
        candidates.sort(key=_email_pool_claim_queue_key)
        row = candidates[0] if candidates else None
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_generic_api_emails(rows)
        return _decorate_generic_api_email(row)


def release_generic_api_email(email: str, status: str = "available", note: str | None = None) -> None:
    """把通用 API 邮箱状态改回 available，或标记为 failed/used。"""
    with _LOCK:
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return
        if status == "available" and _find_by_email(_load_accounts(), email) is not None:
            status = "used"
        row["status"] = status
        if status == "available":
            row["used_at"] = None
            row["retry_count"] = int(row.get("retry_count") or 0) + 1
            row["retry_queue_seq"] = max(
                (int(item.get("retry_queue_seq") or 0) for item in rows),
                default=0,
            ) + 1
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_generic_api_emails(rows)


def release_unconsumed_generic_api_email(
    email: str,
    note: str | None = None,
    *,
    count_failure: bool = False,
    failure_limit: int = 0,
) -> bool:
    """原子回收未生成本地账号且仍为 used 的通用 API 邮箱。

    真实注册失败会累加 ``registration_failure_count`` 并移到队尾；达到阈值后
    直接停用。用户停止/取消只做无损回收，不计失败。
    """
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        if count_failure:
            failures = int(row.get("registration_failure_count") or 0) + 1
            row["registration_failure_count"] = failures
            row["retry_count"] = int(row.get("retry_count") or 0) + 1
            row["retry_queue_seq"] = max(
                (int(item.get("retry_queue_seq") or 0) for item in rows),
                default=0,
            ) + 1
            should_disable = int(failure_limit or 0) > 0 and failures >= int(failure_limit)
            row["status"] = "disabled" if should_disable else "available"
            row["used_at"] = _now() if should_disable else None
        else:
            row["status"] = "available"
            row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_generic_api_emails(rows)
        return True


def delete_generic_api_email(email: str) -> bool:
    """从通用 API 邮箱池彻底删除一个邮箱。"""
    with _LOCK:
        rows = _load_generic_api_emails()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_generic_api_emails(new_rows)
        return True


def list_generic_api_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        account_by_email = {
            (a.get("email") or "").lower(): a
            for a in _load_accounts()
        }
        rows = [_decorate_generic_api_email(r, account_by_email) for r in _load_generic_api_emails()]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = _sort_email_pool_rows(rows)
        return rows[:limit]


def generic_api_email_pool_summary(provider: str | None = None) -> dict:
    with _LOCK:
        out = {"available": 0, "used": 0, "failed": 0}
        registered_emails = {
            str(account.get("email") or "").strip().lower()
            for account in _load_accounts()
        }
        provider_filter = str(provider or "").strip().lower()
        for row in _load_generic_api_emails():
            row_provider = str(row.get("provider") or "").strip().lower()
            if provider_filter == "generic_api" and row_provider not in ("", "generic_api"):
                continue
            if provider_filter not in ("", "generic_api") and row_provider != provider_filter:
                continue
            status = row.get("status") or "available"
            if status == "available" and str(row.get("email") or "").strip().lower() in registered_emails:
                status = "used"
            out[status] = out.get(status, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_generic_api_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_generic_api_emails(), email)
        return _decorate_generic_api_email(row) if row else None


# ============================================================
# OpenAI 邮件状态检测池
# ============================================================

def _public_mail_status_row(row: dict) -> dict:
    """Never expose mailbox pickup tokens/URLs to the browser."""
    return {
        key: value for key, value in row.items()
        if key != "code_url" and not str(key).startswith("_previous_")
    }


def list_mail_status_pool(status: str | None = None, limit: int = 5000) -> list[dict]:
    with _LOCK:
        rows = _load_mail_status_pool()
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows = sorted(rows, key=lambda row: int(row.get("id") or 0))
        return [_public_mail_status_row(row) for row in rows[:max(1, int(limit or 5000))]]


def get_mail_status_entry(email: str, *, include_secret: bool = False) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_mail_status_pool(), email)
        if not row:
            return None
        return dict(row) if include_secret else _public_mail_status_row(row)


def add_mail_status_emails(emails: list[str]) -> tuple[list[dict], list[dict]]:
    """Add selected emails, resolving their private read URL from the generic API pool."""
    with _LOCK:
        rows = _load_mail_status_pool()
        generic_rows = _load_generic_api_emails()
        added: list[dict] = []
        skipped: list[dict] = []
        for raw in emails:
            email = str(raw or "").strip()
            if not email or "@" not in email:
                skipped.append({"email": email, "reason": "邮箱格式不正确"})
                continue
            existing = _find_by_email(rows, email)
            source = _find_by_email(generic_rows, email)
            if not source or not str(source.get("code_url") or "").strip():
                skipped.append({"email": email, "reason": "通用 API 邮箱池中没有读取链接"})
                continue
            if existing:
                # Refresh the private URL in case the mailbox material was re-imported.
                existing["code_url"] = source.get("code_url")
                skipped.append({"email": email, "reason": "已在邮件检测池"})
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "code_url": source.get("code_url"),
                "status": "pending",
                "label": "待检测",
                "evidence": "",
                "subject": "",
                "mail_date": "",
                "mail_id": "",
                "mail_source": "",
                "message_count": 0,
                "error": "",
                "added_at": _now(),
                "checked_at": "",
            }
            rows.append(row)
            added.append(_public_mail_status_row(row))
        _save_mail_status_pool(rows)
        return added, skipped


def mark_mail_status_checking(email: str) -> dict | None:
    with _LOCK:
        rows = _load_mail_status_pool()
        row = _find_by_email(rows, email)
        if not row:
            return None
        if row.get("status") in {"plus", "nonplus", "banned"}:
            for key in (
                "status", "label", "evidence", "subject", "mail_date", "mail_id",
                "mail_source", "message_count", "error", "account_id", "checked_at",
            ):
                row[f"_previous_{key}"] = row.get(key)
        row["status"] = "checking"
        row["label"] = "检测中"
        row["error"] = ""
        row["check_started_at"] = _now()
        _save_mail_status_pool(rows)
        return dict(row)


def update_mail_status_result(email: str, result: dict) -> dict | None:
    with _LOCK:
        rows = _load_mail_status_pool()
        row = _find_by_email(rows, email)
        if not row:
            return None
        incoming_status = str(result.get("status") or "").strip().lower()
        previous_status = str(row.get("_previous_status") or "").strip().lower()
        if incoming_status == "error" and previous_status in {"plus", "nonplus", "banned"}:
            for key in (
                "status", "label", "evidence", "subject", "mail_date", "mail_id",
                "mail_source", "message_count", "error", "account_id", "checked_at",
            ):
                previous_key = f"_previous_{key}"
                if previous_key in row:
                    row[key] = row.get(previous_key)
            row["last_check_status"] = "error"
            row["last_check_error"] = result.get("error") or "邮箱读取失败"
            row["last_check_failed_at"] = _now()
            row["preserved_previous_result"] = True
        else:
            for key in (
                "status", "label", "evidence", "subject", "mail_date", "mail_id",
                "mail_source", "message_count", "error", "account_id",
            ):
                if key in result:
                    row[key] = result.get(key)
            row["checked_at"] = _now()
            row["last_check_status"] = incoming_status or row.get("status")
            row["last_check_error"] = ""
            row["last_check_failed_at"] = ""
            row["preserved_previous_result"] = False
        for key in list(row):
            if str(key).startswith("_previous_"):
                row.pop(key, None)
        row.pop("check_started_at", None)
        _save_mail_status_pool(rows)
        return _public_mail_status_row(row)


def _mail_received_timestamp(value: Any) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def _plan_detection_confirmed(row: dict) -> bool:
    explicit = str(row.get("plan_detection_status") or "").strip().lower()
    if explicit:
        return explicit == "confirmed"
    if row.get("plan_check_ok") is not True and row.get("plan_check_status") != "success":
        return False
    if str(row.get("plan_authority") or "").strip().lower() in {"none", "promo_only"}:
        return False
    if str(row.get("plan_detection_capability") or "").strip().lower() == "promo_only":
        return False
    plan = str(
        row.get("current_plan_type") or row.get("plan_type") or row.get("subscription_plan") or ""
    ).strip().lower()
    return bool(plan and plan not in {"unknown", "none", "null"})


def _terminal_subscription_evidence(row: dict) -> bool:
    code = str(row.get("status_code") or row.get("plan_terminal_code") or "").strip().lower()
    if code in _SUBSCRIPTION_TERMINAL_CODES:
        return True
    status = str(row.get("subscription_status") or "").strip().lower()
    return _plan_detection_confirmed(row) and status in _SUBSCRIPTION_TERMINAL_STATUSES


def _should_promote_plus_mail(row: dict, result: dict) -> bool:
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    if plan == "plus" or row.get("has_active_plus_subscription") is True:
        return False
    if _terminal_subscription_evidence(row):
        return False
    if not _plan_detection_confirmed(row):
        return True

    received_at = _mail_received_timestamp(result.get("mail_date"))
    now_timestamp = datetime.now().timestamp()
    if received_at is None:
        return False
    if received_at > now_timestamp + _PLUS_MAIL_CONFIRMATION_FUTURE_SKEW_SECONDS:
        return False
    if now_timestamp - received_at > _PLUS_MAIL_CONFIRMATION_MAX_AGE_SECONDS:
        return False

    mail_account_id = str(result.get("account_id") or "").strip()
    account_ids = {
        str(value or "").strip()
        for value in (row.get("user_id"), row.get("account_id"))
        if str(value or "").strip()
    }
    return bool(mail_account_id and mail_account_id in account_ids)


def sync_account_mail_status(email: str, result: dict) -> bool:
    """保存原始邮件证据，并按 Aliashub 边界决定是否晋升套餐。"""
    with _LOCK:
        accounts = _load_accounts()
        row = _find_by_email(accounts, email)
        if not row:
            return False
        status = str(result.get("status") or "")
        row["mail_plus_status"] = status
        row["mail_plus_checked_at"] = _now()
        row["mail_plus_evidence"] = result.get("evidence") or ""
        row["mail_plus_subject"] = result.get("subject") or ""
        row["mail_plus_date"] = result.get("mail_date") or ""
        row["mail_plus_account_id"] = result.get("account_id") or ""
        row["mail_plus_source"] = result.get("mail_source") or ""
        row["mail_plus_id"] = result.get("mail_id") or ""
        if status == "plus":
            already_promoted = bool(row.get("mail_plus_promoted"))
            promoted = already_promoted or _should_promote_plus_mail(row, result)
            row["mail_plus_promoted"] = promoted
            if promoted:
                if row.get("plan_check_error"):
                    row["last_at_plan_check_error"] = row.get("plan_check_error")
                    row["last_at_plan_check_http_status"] = row.get("plan_check_http_status")
                    row["last_at_plan_checked_at"] = row.get("plan_checked_at")
                row["plan_check_status"] = "success"
                row["plan_check_ok"] = True
                row["plan_check_error"] = None
                row["plan_check_http_status"] = None
                row["plan_checked_at"] = row["mail_plus_checked_at"]
                row["plan_check_completed_at"] = row["mail_plus_checked_at"]
                row["plan_last_success_at"] = row["mail_plus_checked_at"]
                row["plan_detection_status"] = "confirmed"
                row["plan_detection_source"] = "mail/plus-confirmation"
                row["plan_authority"] = "mail_confirmation"
                row["plan_confidence"] = "high" if row.get("mail_plus_account_id") else "medium"
                row["plan_evidence_path"] = "mail_messages.account_manage_link+subject+body"
                row["current_plan_type"] = "plus"
                row["plan_type"] = "plus"
                row["subscription_plan"] = "chatgptplusplan"
                row["subscription_status"] = "active"
                row["has_active_subscription"] = True
                row["has_active_plus_subscription"] = True
                row["is_free_plan"] = False
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def delete_mail_status_emails(emails: list[str]) -> int:
    with _LOCK:
        targets = {str(email or "").strip().lower() for email in emails if str(email or "").strip()}
        rows = _load_mail_status_pool()
        remaining = [row for row in rows if str(row.get("email") or "").lower() not in targets]
        removed = len(rows) - len(remaining)
        if removed:
            _save_mail_status_pool(remaining)
        return removed


# ============================================================
# Codex 授权账号（来自 codex_accounts/codex-邮箱-plan.json）
# ============================================================

def _load_codex_export_state() -> dict:
    """读导出状态映射 {filename: {exported_at, exported_count}}。不存在返回 {}。"""
    data = _read_json(_CODEX_EXPORT_STATE, {})
    return data if isinstance(data, dict) else {}


def _save_codex_export_state(state: dict) -> None:
    _write_json(_CODEX_EXPORT_STATE, state)


def list_codex_accounts() -> list[dict]:
    """
    扫 codex_accounts/ 目录，每个 codex-*.json 是一条 CPA 兼容凭证。
    返回带元信息的列表（含导出状态、文件大小、token 预览等）。
    """
    with _LOCK:
        out = []
        if not _CODEX_DIR.exists():
            return out
        export_state = _load_codex_export_state()
        for path in sorted(_CODEX_DIR.glob("codex-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                content = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            fname = path.name
            es = export_state.get(fname) or {}
            # 从文件名抽 email 和 plan：codex-{email}.json 或 codex-{email}-{plan}.json
            stem = path.stem  # codex-邮箱-plan
            without_prefix = stem[len("codex-"):] if stem.startswith("codex-") else stem
            # plan 可能为空。简单做法：直接读 JSON 里的 email（更准），文件名只做 fallback
            email = content.get("email") or ""
            if not email:
                # JSON 里 email 为空（旧 bug 产物），从文件名兜底
                # 文件名格式 codex-{email}-{plan}.json，email 里可能有 - 但是常见邮箱不会有
                # 简单做法：去掉末尾 -plan（如 -free / -plus / -team），剩下的当 email
                parts = without_prefix.rsplit("-", 1)
                if len(parts) == 2 and parts[1].lower() in ("free", "plus", "team", "pro", "enterprise"):
                    email = parts[0]
                else:
                    email = without_prefix
            # 推断 plan
            plan = ""
            if "-" in without_prefix:
                tail = without_prefix.rsplit("-", 1)[-1].lower()
                if tail in ("free", "plus", "team", "pro", "enterprise"):
                    plan = tail
            out.append({
                "filename": fname,
                "path": str(path),
                "email": email,
                "plan": plan,
                "account_id": content.get("account_id", ""),
                "type": content.get("type", "codex"),
                "last_refresh": content.get("last_refresh", ""),
                "expired": content.get("expired", ""),
                "access_token_preview": (content.get("access_token", "") or "")[:32],
                "size": path.stat().st_size,
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                "exported_at": es.get("exported_at"),
                "exported_count": es.get("exported_count", 0),
            })
        return out


def read_codex_credential(filename: str) -> tuple[str, str]:
    """
    读取一个 codex-*.json 文件原始内容。
    Returns: (content_string, filename)
    抛 ValueError：文件名不合法（防目录穿越）/ 不存在。
    """
    with _LOCK:
        # 防注入：只允许 codex-*.json 模式，不允许路径分隔符
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        path = _CODEX_DIR / filename
        if not path.exists() or not path.is_file():
            raise ValueError(f"文件不存在: {filename}")
        return path.read_text(encoding="utf-8"), filename


def mark_codex_exported(filename: str) -> dict:
    """
    标记某个 codex 凭证已导出（导出计数 +1，记录最近导出时间）。
    Returns: 该 filename 当前的导出状态记录。
    """
    with _LOCK:
        state = _load_codex_export_state()
        rec = state.get(filename) or {"exported_count": 0}
        rec["exported_count"] = int(rec.get("exported_count", 0)) + 1
        rec["exported_at"] = _now()
        state[filename] = rec
        _save_codex_export_state(state)
        return rec


def reset_codex_exported(filename: str) -> None:
    """清掉某个 codex 凭证的导出状态（用户想重置时用）。"""
    with _LOCK:
        state = _load_codex_export_state()
        if filename in state:
            del state[filename]
            _save_codex_export_state(state)


def delete_codex_credential(filename: str) -> bool:
    """删除一个本地 codex-*.json 凭证文件，并清理导出状态。"""
    with _LOCK:
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        path = _CODEX_DIR / filename
        if not path.exists() or not path.is_file():
            return False
        path.unlink()
        state = _load_codex_export_state()
        if filename in state:
            del state[filename]
            _save_codex_export_state(state)
        return True


def codex_accounts_summary() -> dict:
    """codex 账号汇总：总数 / 已导出 / 未导出。"""
    with _LOCK:
        rows = list_codex_accounts()
        total = len(rows)
        exported = sum(1 for r in rows if r.get("exported_count", 0) > 0)
        return {
            "total": total,
            "exported": exported,
            "pending": total - exported,
        }


# ============================================================
# registration_jobs
# ============================================================

def _new_job_row(
    rows: list[dict],
    *,
    email_source: str,
    job_type: str = "registration",
    parent_job_id: int | None = None,
    root_job_id: int | None = None,
    retry_attempt: int = 0,
    retry_action: str | None = None,
    email: str | None = None,
    account_id: int | None = None,
    gc_mode: bool = False,
    proxy_mode: str | None = None,
) -> dict:
    job_uuid = str(uuid.uuid4())
    log_file = str(_LOG_DIR / f"{job_uuid}.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    job_id = _next_id(rows)
    return {
        "id": job_id,
        "job_uuid": job_uuid,
        "job_type": job_type,
        "parent_job_id": parent_job_id,
        "root_job_id": root_job_id,
        "retry_attempt": int(retry_attempt or 0),
        "retry_action": retry_action,
        "email_source": email_source,
        "proxy_mode": proxy_mode,
        "email": email,
        "status": "pending",
        "error_message": None,
        "log_file": log_file,
        "started_at": None,
        "completed_at": None,
        "account_id": account_id,
        "gc_mode": bool(gc_mode),
        "gc_window_label": (f"GC-A{int(account_id)}" if gc_mode and account_id else (f"GC-J{job_id}" if gc_mode else None)),
        "roxy_profile_id": None,
        "gc_window_state": None,
        "gc_check_state": None,
        "gc_check_message": None,
        "gc_checked_at": None,
        "created_at": _now(),
    }


def create_job(
    email_source: str,
    *,
    email: str | None = None,
    gc_mode: bool = False,
    proxy_mode: str | None = None,
) -> dict:
    """创建一个首次执行的 pending 注册任务。"""
    with _LOCK:
        rows = _load_jobs()
        row = _new_job_row(
            rows,
            email_source=email_source,
            email=email,
            gc_mode=gc_mode,
            proxy_mode=proxy_mode,
        )
        rows.append(row)
        _save_jobs(rows)
        return dict(row)


def create_retry_job(
    source_job_id: int,
    *,
    job_type: str,
    email_source: str,
    email: str | None = None,
    account_id: int | None = None,
    gc_mode: bool = False,
    proxy_mode: str | None = None,
) -> tuple[dict, bool]:
    """原子创建重试子任务；同一任务链已有活跃任务时直接复用。"""
    with _LOCK:
        rows = _load_jobs()
        source = next((r for r in rows if int(r.get("id") or 0) == int(source_job_id)), None)
        if source is None:
            raise LookupError("任务不存在")
        if source.get("status") not in ("failed", "stopped", "cancelled"):
            raise ValueError(f"当前状态不支持重试：{source.get('status')}")

        root_id = int(source.get("root_job_id") or source.get("id"))
        active_states = {"pending", "running", "stopping"}
        active = next((
            r for r in rows
            if int(r.get("id") or 0) != int(source_job_id)
            and int(r.get("root_job_id") or 0) == root_id
            and r.get("status") in active_states
        ), None)
        if active is not None:
            if active.get("job_type", "registration") != job_type:
                raise ValueError(f"已有其他类型重试任务 #{active.get('id')} 在排队或运行中")
            return dict(active), False

        attempts = [
            int(r.get("retry_attempt") or 0)
            for r in rows
            if int(r.get("id") or 0) == root_id or int(r.get("root_job_id") or 0) == root_id
        ]
        row = _new_job_row(
            rows,
            email_source=email_source,
            job_type=job_type,
            parent_job_id=int(source_job_id),
            root_job_id=root_id,
            retry_attempt=(max(attempts) if attempts else 0) + 1,
            retry_action=("codex" if job_type == "codex_retry" else "registration"),
            email=email,
            account_id=account_id,
            gc_mode=gc_mode,
            proxy_mode=proxy_mode,
        )
        rows.append(row)
        _save_jobs(rows)
        return dict(row), True


def update_job(
    job_id: int,
    *,
    status: str | None = None,
    email: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    account_id: int | None = None,
    roxy_profile_id: str | None = None,
    gc_window_state: str | None = None,
    gc_check_state: str | None = None,
    gc_check_message: str | None = None,
    gc_checked_at: str | None = None,
) -> None:
    with _LOCK:
        rows = _load_jobs()
        row = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if row is None:
            return
        if status is not None:
            row["status"] = status
        if email is not None:
            row["email"] = email
        if error is not None:
            row["error_message"] = error
        if started_at is not None:
            row["started_at"] = started_at
        if completed_at is not None:
            row["completed_at"] = completed_at
        if account_id is not None:
            row["account_id"] = account_id
        if roxy_profile_id is not None:
            row["roxy_profile_id"] = str(roxy_profile_id)
        if gc_window_state is not None:
            row["gc_window_state"] = gc_window_state
        if gc_check_state is not None:
            row["gc_check_state"] = gc_check_state
        if gc_check_message is not None:
            row["gc_check_message"] = gc_check_message
        if gc_checked_at is not None:
            row["gc_checked_at"] = gc_checked_at
        _save_jobs(rows)


def list_jobs(limit: int = 100) -> list[dict]:
    with _LOCK:
        rows = sorted(_load_jobs(), key=lambda x: int(x.get("id") or 0), reverse=True)
        return [dict(r) for r in rows[:limit]]


def get_job(job_id: int) -> dict | None:
    with _LOCK:
        row = next((r for r in _load_jobs() if int(r.get("id") or 0) == int(job_id)), None)
        return dict(row) if row else None


def start_pending_job(job_id: int, *, started_at: str) -> dict | None:
    """Atomically move a pending job to running and return its bound mailbox."""
    with _LOCK:
        rows = _load_jobs()
        row = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if row is None or row.get("status") != "pending":
            return None
        row["status"] = "running"
        row["started_at"] = started_at
        _save_jobs(rows)
        return dict(row)


def cancel_pending_job(job_id: int, *, completed_at: str, error: str) -> dict | None:
    """Atomically cancel a still-pending job; running jobs remain untouched."""
    with _LOCK:
        rows = _load_jobs()
        row = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if row is None or row.get("status") != "pending":
            return None
        row["status"] = "cancelled"
        row["completed_at"] = completed_at
        row["error_message"] = error
        _save_jobs(rows)
        return dict(row)


def get_successful_retry_for_job(job_id: int) -> dict | None:
    """返回同一任务链中已成功的其他重试任务，用于保留原任务历史状态并阻止重复重试。"""
    with _LOCK:
        rows = _load_jobs()
        source = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if source is None:
            return None
        root_id = int(source.get("root_job_id") or source.get("id") or 0)
        matches = [
            r for r in rows
            if int(r.get("id") or 0) != int(job_id)
            and int(r.get("root_job_id") or 0) == root_id
            and r.get("status") == "success"
        ]
        if not matches:
            return None
        return dict(max(matches, key=lambda r: int(r.get("id") or 0)))


def delete_job(job_id: int, *, delete_log: bool = True, allow_running: bool = False) -> bool:
    """
    删除一个注册任务记录；默认同时删除该任务日志文件。返回是否删除到记录。
    默认不删除 running 任务，避免后台线程仍在执行但前端记录消失。
    """
    with _LOCK:
        rows = _load_jobs()
        idx = next((i for i, r in enumerate(rows) if int(r.get("id") or 0) == int(job_id)), None)
        if idx is None:
            return False
        if not allow_running and rows[idx].get("status") in ("running", "stopping"):
            return False
        if not allow_running and rows[idx].get("gc_window_state") == "open":
            return False
        row = rows.pop(idx)
        _save_jobs(rows)

    if delete_log:
        log_file = row.get("log_file")
        if log_file:
            try:
                Path(log_file).unlink(missing_ok=True)
            except Exception:
                pass
    return True


# ============================================================
# 迁移与路径
# ============================================================

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _migrate_legacy_sqlite() -> dict:
    summary = {"sqlite_accounts_imported": 0, "sqlite_outlook_imported": 0, "sqlite_outlook_skipped": 0}
    if not _LEGACY_SQLITE.exists():
        return summary
    try:
        conn = sqlite3.connect(str(_LEGACY_SQLITE))
        conn.row_factory = sqlite3.Row
        if _table_exists(conn, "outlook_pool"):
            records = []
            statuses = []
            for row in conn.execute("SELECT * FROM outlook_pool").fetchall():
                records.append({
                    "email": row["email"],
                    "password": row["password"],
                    "client_id": row["client_id"],
                    "refresh_token": row["refresh_token"],
                })
                statuses.append({
                    "email": row["email"],
                    "status": row["status"],
                    "note": row["note"],
                })
            ins, skip = import_outlook_accounts(records)
            for item in statuses:
                if item["status"] != "available":
                    release_outlook(item["email"], status=item["status"], note=item["note"])
            summary["sqlite_outlook_imported"] += ins
            summary["sqlite_outlook_skipped"] += skip
        if _table_exists(conn, "registered_accounts"):
            for row in conn.execute("SELECT * FROM registered_accounts").fetchall():
                insert_account(
                    email=row["email"],
                    access_token=row["access_token"],
                    totp_secret=row["totp_secret"],
                    user_id=row["user_id"],
                    user_name=row["user_name"],
                    plan_type=row["plan_type"],
                    expires_at=row["expires_at"],
                    device_id=row["device_id"],
                    proxy_used=row["proxy_used"],
                    email_source=row["email_source"],
                    extra=json.loads(row["extra_json"]) if row["extra_json"] else None,
                )
                summary["sqlite_accounts_imported"] += 1
        conn.close()
    except Exception as exc:
        summary["sqlite_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def migrate_legacy_files() -> dict:
    """
    把历史 SQLite、accounts/*.json、outlook_accounts.txt、outlook_accounts_used.json
    迁移到当前 JSON/TXT 文件存储。多次调用是幂等的。
    """
    summary = {
        "accounts_imported": 0,
        "outlook_imported": 0,
        "outlook_skipped": 0,
    }
    summary.update(_migrate_legacy_sqlite())

    accounts_dir = _PROJECT_ROOT / "accounts"
    if accounts_dir.exists():
        for jf in accounts_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if not data.get("email") or not data.get("access_token"):
                    continue
                extra = data.get("extra") or {}
                user = extra.get("user") or {}
                account = extra.get("account") or {}
                insert_account(
                    email=data["email"],
                    access_token=data["access_token"],
                    totp_secret=data.get("totp_secret"),
                    user_id=user.get("id"),
                    user_name=user.get("name"),
                    plan_type=account.get("planType"),
                    expires_at=extra.get("expires"),
                    device_id=extra.get("device_id"),
                    extra=extra,
                )
                summary["accounts_imported"] += 1
            except Exception:
                continue

    for txt in (_PROJECT_ROOT / "outlook_accounts.txt", _OUTLOOK_TXT):
        if txt.exists():
            records = []
            for line in txt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("----")
                # 支持 4 段或 6 段格式
                if len(parts) == 4:
                    email, password, client_id, refresh_token = (p.strip() for p in parts)
                elif len(parts) == 6:
                    email, password, client_id, refresh_token, _, _ = (p.strip() for p in parts)
                else:
                    continue
                records.append({
                    "email": email,
                    "password": password,
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                })
            ins, skip = import_outlook_accounts(records)
            summary["outlook_imported"] += ins
            summary["outlook_skipped"] += skip

    used = _PROJECT_ROOT / "outlook_accounts_used.json"
    if used.exists():
        try:
            emails = json.loads(used.read_text(encoding="utf-8"))
            for email in emails:
                release_outlook(email, status="used")
        except Exception:
            pass

    return summary


def db_path() -> Path:
    """兼容旧名称，返回当前文件存储目录。"""
    return _DATA_DIR


def storage_paths() -> dict:
    return {
        "outlook_json": str(_OUTLOOK_JSON),
        "outlook_txt": str(_OUTLOOK_TXT),
        "accounts_json": str(_ACCOUNTS_JSON),
        "accounts_txt": str(_ACCOUNTS_TXT),
        "tokens_txt": str(_TOKENS_TXT),
        "viewer_html": str(_VIEWER_HTML),
        "jobs_json": str(_JOBS_JSON),
        "logs_dir": str(_LOG_DIR),
    }


def refresh_static_viewer() -> Path:
    """手动刷新静态查看器，返回 HTML 路径。"""
    with _LOCK:
        outlook_rows = _load_outlook()
        account_rows = _load_accounts()
        _sync_outlook_txt(outlook_rows)
        _sync_accounts_txt(account_rows)
        _sync_tokens_txt(account_rows)
        return _render_static_viewer(outlook_rows=outlook_rows, account_rows=account_rows)


# ============================================================
# Domain email pool（Cloudflare 域名邮箱跟踪）
# ============================================================

_DOMAIN_EMAIL_JSON = _PROJECT_ROOT / "用于注册的域名邮箱.json"


def _load_domain_pool() -> list[dict]:
    rows = _read_json(_DOMAIN_EMAIL_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_domain_pool(rows: list[dict]) -> None:
    _write_json(_DOMAIN_EMAIL_JSON, rows)


def _find_domain_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def claim_next_domain_email(email: str) -> dict:
    """记录一个新的域名邮箱地址到池中（标记为 available）。"""
    with _LOCK:
        rows = _load_domain_pool()
        if _find_domain_email(rows, email):
            # 已存在，直接返回
            row = _find_domain_email(rows, email)
            return row
        row = {
            "id": _next_id(rows),
            "email": email,
            "status": "available",
            "used_at": None,
            "note": None,
            "created_at": _now(),
        }
        rows.append(row)
        _save_domain_pool(rows)
        return dict(row)


def release_domain_email(email: str, status: str = "available", note: str | None = None) -> None:
    """更新域名邮箱状态。"""
    with _LOCK:
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None:
            return
        if status == "available" and _find_by_email(_load_accounts(), email) is not None:
            status = "used"
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_domain_pool(rows)


def release_unconsumed_domain_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的域名邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_domain_pool(rows)
        return True


def get_domain_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_domain_email(_load_domain_pool(), email)
        return dict(row) if row else None


def list_domain_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        account_by_email = {
            (a.get("email") or "").lower(): a
            for a in _load_accounts()
        }
        rows = [_decorate_domain_email(r, account_by_email) for r in _load_domain_pool()]
        rows = _sort_email_pool_rows(rows)
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return rows[:limit]


def domain_email_pool_summary() -> dict:
    with _LOCK:
        out: dict[str, int] = {"available": 0, "used": 0, "failed": 0}
        registered_emails = {
            str(account.get("email") or "").strip().lower()
            for account in _load_accounts()
        }
        for row in _load_domain_pool():
            s = row.get("status") or "available"
            if s == "available" and str(row.get("email") or "").strip().lower() in registered_emails:
                s = "used"
            out[s] = out.get(s, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def delete_domain_email(email: str) -> bool:
    """从域名邮箱池删除一个邮箱。"""
    with _LOCK:
        rows = _load_domain_pool()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_domain_pool(new_rows)
        return True
