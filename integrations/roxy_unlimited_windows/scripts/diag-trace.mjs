// 定位：shim 装上后，toDataURL 到底被调用了吗？
const API = 'http://127.0.0.1:50001';
const fs = await import('node:fs');
const path = await import('node:path');
const post = async (p, b) => (await fetch(API + p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b ?? {}) })).json();
const NOISE = fs.readFileSync(path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), 'noise-ext', 'noise.js'), 'utf8').replaceAll('__SEED__', '123456789');

const created = await post('/browser/create', { windowName: 'trace', locale: 'pt-BR', screen: [1920,1080], proxy: 'direct', open: true });
const { dirId, http } = created.data;
const ver = await (await fetch(`http://127.0.0.1:${http.split(':')[1]}/json/version`)).json();
const ws = new WebSocket(ver.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
let id = 0; const pending = new Map();
ws.addEventListener('message', (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
const call = (m, p, s) => new Promise((res) => { const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method: m, params: p, ...(s ? { sessionId: s } : {}) }));
  setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); res({ __t: m }); } }, 8000); });
const tg = await call('Target.getTargets', {});
const sid = (await call('Target.attachToTarget', { targetId: tg.result.targetInfos.find(t=>t.type==='page').targetId, flatten: true })).result.sessionId;
await call('Page.enable', {}, sid);
await call('Page.navigate', { url: 'http://127.0.0.1:50001/_blank' }, sid);
await new Promise(r => setTimeout(r, 1500));
const ev = async (e) => (await call('Runtime.evaluate', { expression: e, returnByValue: true }, sid)).result?.result?.value;

console.log('1) shim 前 prototype 源:', await ev(`String(HTMLCanvasElement.prototype.toDataURL).slice(0,60)`));
await ev(NOISE);
console.log('2) shim 后 prototype 源:', await ev(`String(HTMLCanvasElement.prototype.toDataURL).slice(0,60)`));
console.log('3) 计数器 (噪声分支是否进入):', await ev(`window.__roxyNoiseHits ?? 'no counter'`));

// 手工验证：直接调 my toDataURL 并计数
console.log('\n4) 直接调用被 patch 的 toDataURL:', await ev(`(() => {
  const c=document.createElement('canvas'); c.width=10; c.height=10;
  const g=c.getContext('2d'); g.fillStyle='#123456'; g.fillRect(0,0,10,10);
  const a=c.toDataURL();
  const d=document.createElement('canvas'); d.width=10; d.height=10;
  d.getContext('2d').fillStyle='#123456'; d.getContext('2d').fillRect(0,0,10,10);
  return { same: a === d.toDataURL(), lenA: a.length };
})()`));

// 5) 手工走一遍 noisyCopy 的逻辑
console.log('\n5) 手工 noisify 后哈希是否变化:', await ev(`(() => {
  const fnv=(s)=>{let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return (h>>>0).toString(16).padStart(8,'0');};
  const mk=()=>{const c=document.createElement('canvas');c.width=220;c.height=40;const g=c.getContext('2d');
    g.textBaseline='top';g.font='16px Arial';g.fillStyle='#f60';g.fillRect(0,0,120,22);
    g.fillStyle='#069';g.fillText('RoxyFingerprint!',2,4);return c;};
  const c1=mk(); const h1=fnv(c1.toDataURL());
  const c2=mk(); const g2=c2.getContext('2d'); const im=g2.getImageData(0,0,220,40);
  im.data[100]= (im.data[100]+1)%255; g2.putImageData(im,0,0);
  const h2=fnv(c2.toDataURL());
  return { h1, h2, changed: h1!==h2, stride997len: 220*40*4 };
})()`));

ws.close();
await post('/browser/close', { dirId });
await post('/browser/delete', { dirId });
