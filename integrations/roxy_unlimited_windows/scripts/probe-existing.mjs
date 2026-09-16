// 用同一个探针测任意已存在档案（对照组：官方档案）
const API = 'http://127.0.0.1:50001';
const dirId = process.argv[2];

const PROBE = `(() => {
  const c = document.createElement('canvas'); c.width = 220; c.height = 40;
  const g = c.getContext('2d');
  g.textBaseline='top'; g.font='16px Arial';
  g.fillStyle='#f60'; g.fillRect(0,0,120,22);
  g.fillStyle='#069'; g.fillText('RoxyFingerprint!',2,4);
  g.fillStyle='rgba(102,204,0,0.7)'; g.fillText('RoxyFingerprint!',4,8);
  const fnv=(s)=>{let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return (h>>>0).toString(16).padStart(8,'0');};
  const d1=c.toDataURL();
  const gl=document.createElement('canvas').getContext('webgl');
  const dbg=gl&&gl.getExtension('WEBGL_debug_renderer_info');
  return { canvasHash: fnv(d1), pixelSum: (()=>{const d=g.getImageData(0,0,c.width,c.height).data;let s=0;for(let i=0;i<d.length;i+=97)s=(s+d[i])%100000;return s;})(),
    audioSum: null, webglRenderer: dbg?gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL):null };
})()`;

const post = async (p, b) => (await fetch(API + p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b ?? {}) })).json();

const opened = await post('/browser/open', { dirId });
if (opened.code !== 0) { console.error('open failed:', opened); process.exit(2); }
const port = opened.data.http.split(':')[1];

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const page = list.find((t) => t.type === 'page') ?? list[0];
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
const val = await new Promise((res) => {
  ws.addEventListener('message', (ev) => { const m = JSON.parse(ev.data); if (m.id === 1) res(m.result?.result?.value); });
  ws.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression: PROBE, returnByValue: true } }));
  setTimeout(() => res(null), 12000);
});
ws.close();

const fp = (await (await fetch(`${API}/browser/fingerprint?dirId=${dirId}&full=1`)).json()).data;
console.log(JSON.stringify({
  dirId: dirId.slice(0, 8),
  windowName: fp.windowName,
  canvasHash: val.canvasHash,
  pixelSum: val.pixelSum,
  webglRenderer: val.webglRenderer,
  writtenCanvasNoise: fp.canvasContext?.canvasContextNoiseValue ?? null,
  writtenEnable: fp.canvasContext?.enableCanvasContextNoise ?? null,
}, null, 2));

await post('/browser/close', { dirId });
