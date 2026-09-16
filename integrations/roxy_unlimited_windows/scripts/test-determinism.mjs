// 确定性验证：同一档案关闭再打开，canvas 哈希必须一致
const API = 'http://127.0.0.1:50001';
const post = async (p, b) => (await fetch(API + p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b ?? {}) })).json();

const HASH = `(() => {
  const c=document.createElement('canvas'); c.width=220; c.height=40;
  const g=c.getContext('2d');
  g.textBaseline='top'; g.font='16px Arial';
  g.fillStyle='#f60'; g.fillRect(0,0,120,22);
  g.fillStyle='#069'; g.fillText('RoxyFingerprint!',2,4);
  g.fillStyle='rgba(102,204,0,0.7)'; g.fillText('RoxyFingerprint!',4,8);
  const fnv=(s)=>{let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return (h>>>0).toString(16).padStart(8,'0');};
  return { hash: fnv(c.toDataURL()), shim: window.__roxyNoiseSeed ?? null, hits: window.__roxyNoiseHits ?? null };
})()`;

async function probe(dirId, http) {
  const ver = await (await fetch(`http://127.0.0.1:${http.split(':')[1]}/json/version`)).json();
  const ws = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
  let id = 0; const pending = new Map();
  ws.addEventListener('message', (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
  const call = (m, p, s) => new Promise((res) => { const mid = ++id; pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method: m, params: p, ...(s ? { sessionId: s } : {}) }));
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); res(null); } }, 8000); });
  const tg = await call('Target.getTargets', {});
  const sid = (await call('Target.attachToTarget', { targetId: tg.result.targetInfos.find(t=>t.type==='page').targetId, flatten: true })).result.sessionId;
  await call('Page.enable', {}, sid);
  await call('Page.navigate', { url: 'http://127.0.0.1:50001/_blank' }, sid);
  await new Promise(r => setTimeout(r, 1500));
  const r = await call('Runtime.evaluate', { expression: HASH, returnByValue: true }, sid);
  // read a second time to see within-load stability
  const r2 = await call('Runtime.evaluate', { expression: HASH, returnByValue: true }, sid);
  ws.close();
  return { first: r?.result?.result?.value, second: r2?.result?.result?.value };
}

const created = await post('/browser/create', { windowName: 'det', locale: 'pt-BR', screen: [1920,1080], proxy: 'direct' });
const dirId = created.data.dirId;
console.log(`profile ${dirId.slice(0,8)}  seed(written)=${created.data.dirId}`);

const runs = [];
for (let round = 1; round <= 3; round++) {
  const opened = await post('/browser/open', { dirId });
  if (opened.code !== 0) { console.error('open failed', opened); break; }
  const p = await probe(dirId, opened.data.http);
  const h1 = p.first?.hash, h2 = p.second?.hash;
  console.log(`  round ${round}: hash=${h1}  shim=${p.first?.shim}  hits=${p.first?.hits}  withinLoadStable=${h1 === h2}`);
  runs.push(h1);
  await post('/browser/close', { dirId });
  await new Promise(r => setTimeout(r, 1500));
}

const uniq = new Set(runs);
console.log(`\n跨 ${runs.length} 次启动: ${uniq.size} 个不同哈希 -> ${uniq.size === 1 ? '确定性 OK（同一档案指纹稳定）' : '不稳定，会被检测为异常'}`);
await post('/browser/delete', { dirId });
