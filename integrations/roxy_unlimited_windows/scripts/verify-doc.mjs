// 逐字执行文档 §7.1 / §7.3 / §7.4 的代码，验证文档准确性
const API = 'http://127.0.0.1:50001';

// ---------- §7.1 批量开 N 个窗口（并行） ----------
const N = 5;
const names = Array.from({ length: N }, (_, i) => `bot-${i + 1}`);

const handles = await Promise.all(names.map(async (windowName) => {
  const r = await fetch(`${API}/browser/create`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ windowName, open: true, startUrl: 'about:blank' }),
  });
  return (await r.json()).data;
}));

console.log(`[§7.1] opened ${handles.length} windows in parallel:`);
console.log(handles.map(h => `${h.windowName}\thttp://${h.http}\tpid=${h.pid}`).join('\n'));

// ---------- §7.3 复用已关闭窗口 ----------
const { data: { rows } } = await (await fetch(`${API}/browser/list`)).json();
const closed = rows.filter(r => r.openStatus === 0).slice(0, 2);
console.log(`\n[§7.3] reusing ${closed.length} closed profile(s): ${closed.map(w => w.dirId.slice(0, 8)).join(', ')}`);
for (const w of closed) {
  const r = await fetch(`${API}/browser/open`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dirId: w.dirId }),
  });
  const j = await r.json();
  console.log(`  ${w.dirId.slice(0, 8)} -> code=${j.code} http=${j.data?.http ?? '-'}`);
}

// ---------- §5.5 connection_info ----------
const ci = await (await fetch(`${API}/browser/connection_info`)).json();
console.log(`\n[§5.5] connection_info total = ${ci.data.length}`);

// ---------- §7.4 收尾 ----------
const ca = await (await fetch(`${API}/browser/close_all`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
})).json();
console.log(`[§7.4] close_all -> ${JSON.stringify(ca.data)}`);

const after = await (await fetch(`${API}/browser/connection_info`)).json();
console.log(`[verify] connection_info after close = ${after.data.length}`);
