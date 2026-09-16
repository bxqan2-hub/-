// 注入层到底把 canvas 读回钩在哪一层？
const API = 'http://127.0.0.1:50001';
const post = async (p, b) => (await fetch(API + p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b ?? {}) })).json();

const created = await post('/browser/create', { windowName: 'hook-probe', locale: 'pt-BR', screen: [1920,1080], proxy: 'direct', open: true });
const { dirId, http } = created.data;
const port = http.split(':')[1];
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
const sid = (await call('Target.attachToTarget', { targetId: tg.result.targetInfos.find(t=>t.type==='page').targetId, flatten: true })).result.sessionId;
await call('Page.enable', {}, sid);
await call('Page.navigate', { url: 'http://127.0.0.1:50001/_blank' }, sid);
await new Promise(r => setTimeout(r, 1500));

const EXPR = `(() => {
  const c = document.createElement('canvas'); c.width = 20; c.height = 20;
  const g = c.getContext('2d');
  const desc = (o, k) => { const d = Object.getOwnPropertyDescriptor(o, k); return d ? {
    get: !!d.get, set: !!d.set, writable: d.writable, configurable: d.configurable, enumerable: d.enumerable,
    valueType: typeof d.value, native: d.value ? /native code/.test(String(d.value)) : null, len: d.value ? String(d.value).length : null
  } : null; };
  return {
    toDataURL_onInstance: (() => { c.toDataURL = function(){ return 'PATCHED'; }; try { return c.toDataURL(); } catch(e) { return 'throw:'+e.message; } })(),
    toDataURL_protoDesc: desc(HTMLCanvasElement.prototype, 'toDataURL'),
    getImageData_protoDesc: desc(CanvasRenderingContext2D.prototype, 'getImageData'),
    toBlob_protoDesc: desc(HTMLCanvasElement.prototype, 'toBlob'),
    protoToDataURL_equals_instanceLookup: c.toDataURL === HTMLCanvasElement.prototype.toDataURL ? 'same' : 'differs',
    isNativeToDataURL: /native code/.test(String(HTMLCanvasElement.prototype.toDataURL)),
    ctxProto: Object.getPrototypeOf(g)?.constructor?.name,
    ctxProtoDesc: desc(Object.getPrototypeOf(g), 'getImageData'),
    ctxOwnGetImageData: Object.prototype.hasOwnProperty.call(g, 'getImageData'),
    canvasOwnProps: Object.getOwnPropertyNames(c).filter(n => /data|blob|image|read/i.test(n)),
  };
})()`;

const r = await call('Runtime.evaluate', { expression: EXPR, returnByValue: true }, sid);
console.log(JSON.stringify(r.result?.result?.value ?? r.result, null, 2));
ws.close();
await post('/browser/close', { dirId });
await post('/browser/delete', { dirId });
