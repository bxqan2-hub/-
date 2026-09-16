// 读回值是否反映真实画布内容？
const API = 'http://127.0.0.1:50001';
const post = async (p, b) => (await fetch(API + p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b ?? {}) })).json();
const created = await post('/browser/create', { windowName: 'content-probe', locale: 'pt-BR', screen: [1920,1080], proxy: 'direct', open: true });
const { dirId, http } = created.data;
const port = http.split(':')[1];
const ver = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
const ws = new WebSocket(ver.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
let id = 0; const pending = new Map();
ws.addEventListener('message', (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
const call = (m, p, s) => new Promise((res) => { const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method: m, params: p, ...(s ? { sessionId: s } : {}) }));
  setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); res({ __t: m }); } }, 10000); });
const tg = await call('Target.getTargets', {});
const sid = (await call('Target.attachToTarget', { targetId: tg.result.targetInfos.find(t=>t.type==='page').targetId, flatten: true })).result.sessionId;
await call('Page.enable', {}, sid);
await call('Page.navigate', { url: 'http://127.0.0.1:50001/_blank' }, sid);
await new Promise(r => setTimeout(r, 1500));

const EXPR = `(() => {
  const fnv=(s)=>{let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return (h>>>0).toString(16).padStart(8,'0');};
  const mk=(draw)=>{const c=document.createElement('canvas');c.width=220;c.height=40;const g=c.getContext('2d');draw(g);return c;};
  const A = mk(g=>{g.textBaseline='top';g.font='16px Arial';g.fillStyle='#f60';g.fillRect(0,0,120,22);g.fillStyle='#069';g.fillText('RoxyFingerprint!',2,4);});
  const B = mk(g=>{g.fillStyle='#000';g.fillRect(0,0,220,40);});
  const C = mk(g=>{g.fillStyle='#f0f';g.beginPath();g.arc(110,20,15,0,6.3);g.fill();});
  const rawPixels = (c)=>{const g=c.getContext('2d');const d=g.getImageData(0,0,c.width,c.height).data;let s=0;for(let i=0;i<d.length;i+=997)s=(s+d[i])%1000000;return s;};
  return {
    hashA: fnv(A.toDataURL()), hashB: fnv(B.toDataURL()), hashC: fnv(C.toDataURL()),
    pxA: rawPixels(A), pxB: rawPixels(B), pxC: rawPixels(C),
    sameHashAB: fnv(A.toDataURL()) === fnv(B.toDataURL()),
    protoToDataURLLen: HTMLCanvasElement.prototype.toDataURL.length,
    getImageDataLen: CanvasRenderingContext2D.prototype.getImageData.length,
    putImageDataLen: CanvasRenderingContext2D.prototype.putImageData.length,
    toDataURLStr: String(HTMLCanvasElement.prototype.toDataURL).slice(0,180),
  };
})()`;
const r = await call('Runtime.evaluate', { expression: EXPR, returnByValue: true }, sid);
console.log(JSON.stringify(r.result?.result?.value ?? r.result, null, 2));
ws.close();
await post('/browser/close', { dirId });
await post('/browser/delete', { dirId });
