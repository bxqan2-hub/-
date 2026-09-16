// 直接验证 Page.addScriptToEvaluateOnNewDocument 在 RoxyChrome 里到底行不行
const API = 'http://127.0.0.1:50001';
const post = async (p, b) => (await fetch(API + p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b ?? {}) })).json();

const PROXY = process.argv[2] ?? 'direct';
const created = await post('/browser/create', { windowName: 'cdp-diag', locale: 'pt-BR', screen: [1920, 1080], proxy: PROXY, open: true });
if (created.code !== 0) { console.error(created); process.exit(2); }
const { dirId, http } = created.data;
const port = http.split(':')[1];
console.log(`opened dirId=${dirId.slice(0,8)} http=${http}  proxy=${PROXY}`);

const fpx = (await (await fetch(`${API}/browser/fingerprint?dirId=${dirId}&full=1`)).json()).data;
console.log(`profile fproxy = ${JSON.stringify(fpx.fproxy ?? null)}`);

const ver = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
console.log(`browser ws = ${ver.webSocketDebuggerUrl}`);

const ws = new WebSocket(ver.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });

let id = 0;
const pending = new Map();
ws.addEventListener('message', (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
const call = (method, params, sessionId) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params, ...(sessionId ? { sessionId } : {}) }));
  setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); res({ __timeout: method }); } }, 8000);
});

console.log('\n--- Target.getTargets ---');
const tg = await call('Target.getTargets', {});
for (const t of tg.result?.targetInfos ?? []) {
  if (t.type === 'page') console.log(`  page ${t.targetId} ${t.url}`);
}

const pageTarget = (tg.result?.targetInfos ?? []).find((t) => t.type === 'page');
const att = await call('Target.attachToTarget', { targetId: pageTarget.targetId, flatten: true });
console.log(`\n--- attachToTarget -> sessionId=${att.result?.sessionId ? 'ok' : JSON.stringify(att).slice(0, 200)}`);
const sid = att.result?.sessionId;

const SRC = `(() => { window.__cdpProbe = 424242; })()`;
console.log('\n--- Page.addScriptToEvaluateOnNewDocument (flattened session) ---');
const add = await call('Page.addScriptToEvaluateOnNewDocument', { source: SRC, runImmediately: true }, sid);
console.log('  ' + JSON.stringify(add).slice(0, 300));

console.log('\n--- Page.navigate to /_blank ---');
const nav = await call('Page.navigate', { url: 'http://127.0.0.1:50001/_blank' }, sid);
console.log('  ' + JSON.stringify(nav.result ?? nav).slice(0, 200));
await new Promise((r) => setTimeout(r, 1500));

console.log('\n--- evaluate marker ---');
const ev = await call('Runtime.evaluate', { expression: 'JSON.stringify({probe: window.__cdpProbe ?? null, noise: window.__roxyNoiseSeed ?? null, href: location.href})', returnByValue: true }, sid);
console.log('  ' + JSON.stringify(ev.result ?? ev).slice(0, 400));

ws.close();
await post('/browser/close', { dirId });
await post('/browser/delete', { dirId });
