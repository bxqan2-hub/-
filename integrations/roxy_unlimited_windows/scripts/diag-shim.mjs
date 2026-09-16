// 拆分问题：shim 代码本身对不对？(直接 evaluate，不走 document_start 注入)
import fs from 'node:fs';
import path from 'node:path';
import { coreExe } from './fingerprint.mjs';

const API = 'http://127.0.0.1:50001';
const post = async (p, b) => (await fetch(API + p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b ?? {}) })).json();

const NOISE = fs.readFileSync(path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), 'noise-ext', 'noise.js'), 'utf8')
  .replaceAll('__SEED__', '123456789');

const HASH = `(() => {
  const c=document.createElement('canvas'); c.width=220; c.height=40;
  const g=c.getContext('2d');
  g.textBaseline='top'; g.font='16px Arial';
  g.fillStyle='#f60'; g.fillRect(0,0,120,22);
  g.fillStyle='#069'; g.fillText('RoxyFingerprint!',2,4);
  g.fillStyle='rgba(102,204,0,0.7)'; g.fillText('RoxyFingerprint!',4,8);
  const fnv=(s)=>{let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return (h>>>0).toString(16).padStart(8,'0');};
  const d=g.getImageData(0,0,c.width,c.height).data; let s=0; for(let i=0;i<d.length;i+=97)s=(s+d[i])%100000;
  return { hash: fnv(c.toDataURL()), sum: s };
})()`;

const created = await post('/browser/create', { windowName: 'shim-test', locale: 'pt-BR', screen: [1920,1080], proxy: 'direct', open: true });
const { dirId, http } = created.data;
const port = http.split(':')[1];
console.log(`opened ${dirId.slice(0,8)} http=${http}`);

const ver = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
const ws = new WebSocket(ver.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
let id = 0; const pending = new Map();
ws.addEventListener('message', (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
const call = (method, params, sessionId) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params, ...(sessionId ? { sessionId } : {}) }));
  setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); res({ __timeout: method }); } }, 10000);
});

const tg = await call('Target.getTargets', {});
const page = tg.result.targetInfos.find((t) => t.type === 'page');
const att = await call('Target.attachToTarget', { targetId: page.targetId, flatten: true });
const sid = att.result.sessionId;

await call('Page.enable', {}, sid);
await call('Page.navigate', { url: 'http://127.0.0.1:50001/_blank' }, sid);
await new Promise((r) => setTimeout(r, 1500));
const href = await call('Runtime.evaluate', { expression: 'location.href', returnByValue: true }, sid);
console.log(`href = ${href.result?.result?.value}`);

const before = await call('Runtime.evaluate', { expression: HASH, returnByValue: true }, sid);
console.log(`BEFORE shim: ${JSON.stringify(before.result?.result?.value)}`);

const run = await call('Runtime.evaluate', { expression: NOISE, returnByValue: true }, sid);
console.log(`evaluate shim -> ${JSON.stringify(run.result?.exceptionDetails ? run.result.exceptionDetails.text : 'ok')}`);
const marker = await call('Runtime.evaluate', { expression: 'typeof window.__roxyNoiseSeed === "number" ? window.__roxyNoiseSeed : null', returnByValue: true }, sid);
console.log(`marker = ${JSON.stringify(marker.result?.result?.value)}`);

const after1 = await call('Runtime.evaluate', { expression: HASH, returnByValue: true }, sid);
const after2 = await call('Runtime.evaluate', { expression: HASH, returnByValue: true }, sid);
console.log(`AFTER shim #1: ${JSON.stringify(after1.result?.result?.value)}`);
console.log(`AFTER shim #2: ${JSON.stringify(after2.result?.result?.value)}`);

ws.close();
await post('/browser/close', { dirId });
await post('/browser/delete', { dirId });
