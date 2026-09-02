"""独立 MoMo 资格批量查询 WebUI。

启动：
    ..\\.venv\\Scripts\\python.exe app.py --port 5013

AT 与代理只在进程内存中处理，不写入账号库、日志文件或结果文件。
"""
from __future__ import annotations

import argparse
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, render_template_string, request

try:
    from .momo_detector import check_momo
except ImportError:  # direct ``python app.py`` execution
    from momo_detector import check_momo

app = Flask(__name__)
_JOBS: dict[str, dict] = {}
_LOCK = threading.RLock()
_RETRY_HINTS = ("timeout", "timed out", "connection", "proxy", "ssl", "502", "503", "504", "429", "限流", "网络")

HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MoMo 资格批量查询</title>
<style>
:root{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;color:#172033;background:#f5f7fb}body{margin:0}.wrap{max-width:1180px;margin:28px auto;padding:0 18px}.card{background:#fff;border:1px solid #dfe5ef;border-radius:12px;padding:18px;margin-bottom:16px;box-shadow:0 8px 24px #1720330d}h1{margin:0 0 8px;font-size:24px}p,.hint{color:#667085;font-size:13px}label{display:block;font-weight:700;font-size:13px;margin:12px 0 6px}textarea,input,select{width:100%;box-sizing:border-box;border:1px solid #cfd7e6;border-radius:8px;padding:10px;font:inherit}textarea{min-height:150px;resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:12px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.row>*{width:auto}.btn{border:0;border-radius:8px;padding:10px 16px;background:#2563eb;color:white;font-weight:700;cursor:pointer}.btn:disabled{opacity:.5;cursor:not-allowed}.btn.stop{background:#b42318}.status{font-size:13px;color:#475467}.bar{height:8px;background:#e9eef7;border-radius:99px;overflow:hidden;margin:12px 0}.bar i{display:block;height:100%;background:#2563eb;width:0}.table{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:9px;border-bottom:1px solid #edf1f5;text-align:left;white-space:nowrap}th{background:#f8fafc}.ok{color:#07883f;font-weight:700}.bad{color:#b42318;font-weight:700}.muted{color:#667085}@media(max-width:760px){.grid{grid-template-columns:1fr}}
</style></head><body><main class="wrap"><section class="card"><h1>MoMo 资格批量查询</h1><p>手动填写 VN 代理池与 AT；仅执行 VN/VND Checkout 支付方式探测，不执行确认、扣款或支付跳转。AT 不保存到磁盘。</p><div class="grid"><div><label for="tokens">AT（每行一个；也支持“备注----AT”）</label><textarea id="tokens" placeholder="eyJ...\n备注----eyJ..."></textarea></div><div><label for="proxies">VN 代理池（每行一个；支持 VN|http://host:port）</label><textarea id="proxies" placeholder="VN|http://127.0.0.1:7890\nVN|socks5h://user:pass@host:port"></textarea></div></div><div class="row"><label>并发 <input id="workers" type="number" min="1" max="16" value="4"></label><label>每个 AT 最多尝试代理数 <input id="retries" type="number" min="1" max="50" value="6"></label><button class="btn" id="start">开始查询</button><button class="btn stop" id="stop" disabled>停止</button><span class="status" id="status">等待输入</span></div></section><section class="card"><div class="bar"><i id="bar"></i></div><div class="table"><table><thead><tr><th>#</th><th>AT</th><th>结果</th><th>代理</th><th>HTTP</th><th>检测结论</th><th>错误</th></tr></thead><tbody id="rows"></tbody></table></div></section></main><script>
const $=id=>document.getElementById(id);let job=null,timer=null;
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function draw(data){const total=data.total||0,done=data.completed||0;$('bar').style.width=(total?Math.round(done*100/total):0)+'%';$('status').textContent=`${data.status||''} · ${done}/${total}`;const rows=(data.results||[]).map((r,i)=>`<tr><td>${i+1}</td><td>${esc(r.token_preview)}</td><td class="${r.momo?'ok':'bad'}">${r.momo?'有 MoMo':'无/失败'}</td><td>${esc(r.proxy_preview)}</td><td>${esc(r.http_status??'')}</td><td>${esc(r.detection_outcome||'')}</td><td class="muted">${esc(r.error||'')}</td></tr>`).join('');$('rows').innerHTML=rows}
async function poll(){if(!job)return;const r=await fetch('/api/jobs/'+job);const d=await r.json();draw(d);if(['done','failed','cancelled'].includes(d.status)){clearInterval(timer);$('start').disabled=false;$('stop').disabled=true}}
$('start').onclick=async()=>{const r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tokens:$('tokens').value,proxies:$('proxies').value,workers:$('workers').value,max_retries:$('retries').value})});const d=await r.json();if(!r.ok){$('status').textContent=d.error||'提交失败';return}job=d.job_id;$('start').disabled=true;$('stop').disabled=false;$('rows').innerHTML='';timer=setInterval(poll,700);poll()};$('stop').onclick=async()=>{if(job)await fetch('/api/jobs/'+job+'/cancel',{method:'POST'});};
</script></body></html>'''


def _parse_lines(text: str, *, token_mode: bool) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if token_mode and "----" in line:
            line = line.rsplit("----", 1)[-1].strip()
        elif not token_mode and "|" in line and not line.lower().startswith(("http://", "https://", "socks5://", "socks5h://")):
            line = line.split("|", 1)[1].strip()
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def _retryable(result: dict) -> bool:
    if result.get("momo") is True:
        return False
    outcome = str(result.get("detection_outcome") or "")
    if outcome in {"no_momo_in_create_response", "no_momo_in_stripe_init", "account_trial_ineligible", "unsupported_custom_checkout"}:
        return False
    error = str(result.get("error") or "").lower()
    return any(hint in error for hint in _RETRY_HINTS)


def _run_job(job_id: str, tokens: list[str], proxies: list[str], workers: int, max_retries: int) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        job["status"] = "running"
    def run_one(index: int, token: str) -> dict:
        last: dict = {"ok": False, "momo": False, "error": "未执行"}
        candidates = proxies[:max_retries] if max_retries else proxies
        for proxy in candidates:
            with _LOCK:
                if _JOBS[job_id].get("cancelled"):
                    return {"token_preview": token[:6] + "…", "proxy_preview": "", "error": "已停止", "momo": False}
            last = check_momo(token, proxy)
            last["attempt_count"] = int(last.get("attempt_count") or 0) + 1
            if not _retryable(last):
                break
        return last
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="momo-standalone") as pool:
            futures = {pool.submit(run_one, i, token): i for i, token in enumerate(tokens)}
            for future in as_completed(futures):
                result = future.result()
                with _LOCK:
                    job["results"].append(result)
                    job["completed"] += 1
                    if job.get("cancelled"):
                        break
        with _LOCK:
            job["status"] = "cancelled" if job.get("cancelled") else "done"
    except Exception as exc:
        with _LOCK:
            job["status"] = "failed"
            job["error"] = f"任务异常：{type(exc).__name__}"


@app.get("/")
def index():
    return render_template_string(HTML)


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "momo-qualification-checker"})


@app.post("/api/jobs")
def create_job():
    data = request.get_json(silent=True) or {}
    tokens = _parse_lines(data.get("tokens"), token_mode=True)
    proxies = _parse_lines(data.get("proxies"), token_mode=False)
    if not tokens:
        return jsonify({"error": "至少填写一个 AT"}), 400
    if not proxies:
        return jsonify({"error": "至少填写一条 VN 代理"}), 400
    if len(tokens) > 500 or len(proxies) > 500:
        return jsonify({"error": "单次最多 500 个 AT 和 500 条代理"}), 400
    try:
        workers = max(1, min(16, int(data.get("workers") or 4)))
        retries = max(1, min(len(proxies), int(data.get("max_retries") or len(proxies))))
    except (TypeError, ValueError):
        return jsonify({"error": "并发和重试次数必须是整数"}), 400
    job_id = uuid.uuid4().hex
    with _LOCK:
        _JOBS[job_id] = {"job_id": job_id, "status": "queued", "total": len(tokens), "completed": 0, "results": [], "cancelled": False, "created_at": time.time()}
    threading.Thread(target=_run_job, args=(job_id, tokens, proxies, workers, retries), daemon=True).start()
    return jsonify({"job_id": job_id, "total": len(tokens)})


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify({k: v for k, v in job.items() if k not in {"cancelled", "created_at"}})


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        job["cancelled"] = True
        if job["status"] == "queued":
            job["status"] = "cancelled"
    return jsonify({"ok": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MoMo 资格批量查询 WebUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5013)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, threaded=True)
