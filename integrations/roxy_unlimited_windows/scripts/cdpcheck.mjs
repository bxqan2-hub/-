// ============================================================
//  cdpcheck.mjs  —  read the fingerprint a running Roxy core exposes
//
//  Usage: node cdpcheck.mjs <debugPort>
//  Uses Node's built-in WebSocket (Node >= 22), no dependencies.
// ============================================================
const port = process.argv[2] ?? '9300';

const EXPR = `(() => {
  const c = document.createElement('canvas');
  const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
  const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
  const uaData = navigator.userAgentData;
  return {
    userAgent: navigator.userAgent,
    appVersion: navigator.appVersion,
    platform: navigator.platform,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory,
    maxTouchPoints: navigator.maxTouchPoints,
    languages: (navigator.languages || []).join(','),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    uaData: uaData ? { platform: uaData.platform, mobile: uaData.mobile, brands: (uaData.brands||[]).map(b => b.brand + ' ' + b.version).join(', ') } : null,
    webglVendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
    webglRenderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
    screen: [screen.width, screen.height, screen.availWidth, screen.availHeight, screen.colorDepth].join('x'),
    plugins: navigator.plugins.length,
    webdriver: navigator.webdriver,
    doNotTrack: navigator.doNotTrack,
  };
})()`;

const rpc = (ws, id, method, params) => new Promise((resolve, reject) => {
  const onMsg = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.id !== id) return;
    ws.removeEventListener('message', onMsg);
    m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
  };
  ws.addEventListener('message', onMsg);
  ws.send(JSON.stringify({ id, method, params }));
  setTimeout(() => reject(new Error(`timeout on ${method}`)), 15000);
});

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const page = list.find((t) => t.type === 'page') ?? list[0];
if (!page) { console.error('[!] no target'); process.exit(2); }

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });

const r = await rpc(ws, 1, 'Runtime.evaluate', { expression: EXPR, returnByValue: true, awaitPromise: false });
console.log(JSON.stringify(r.result.value, null, 2));
ws.close();
