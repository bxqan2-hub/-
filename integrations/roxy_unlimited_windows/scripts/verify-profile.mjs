// ============================================================
//  verify-profile.mjs  —  create a profile and assert that every
//  advertised fingerprint value actually reaches the page.
//
//  Usage: node verify-profile.mjs [--locale pt-BR] [--tz ...] [--screen 1920x1080]
// ============================================================
const API = 'http://127.0.0.1:50001';
const argv = process.argv.slice(2);
const argOf = (n, d) => { const i = argv.indexOf('--' + n); return i === -1 ? d : argv[i + 1]; };

const spec = {
  windowName: argOf('name', 'verify'),
  locale: argOf('locale', 'pt-BR'),
  timeZone: argOf('tz', undefined),
  screen: argOf('screen', undefined),
  proxy: argOf('proxy', 'direct'),   // default: no relay, so the local probe page is reachable
  open: true,
};

const PROBE = `(() => {
  const c = document.createElement('canvas');
  c.width = 220; c.height = 40;
  const g = c.getContext('2d');
  g.textBaseline = 'top'; g.font = '16px Arial';
  g.fillStyle = '#f60'; g.fillRect(0, 0, 120, 22);
  g.fillStyle = '#069'; g.fillText('RoxyFingerprint!', 2, 4);
  g.fillStyle = 'rgba(102,204,0,0.7)'; g.fillText('RoxyFingerprint!', 4, 8);
  // FNV-1a over the WHOLE dataURL — hashing the tail is useless (PNG IEND is constant)
  const fnv = (s) => { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0).toString(16).padStart(8, '0'); };
  const d1 = c.toDataURL(), d2 = c.toDataURL();
  const cvHash = fnv(d1);
  const cvStable = d1 === d2;
  const pxSum = (() => { const d = g.getImageData(0, 0, c.width, c.height).data; let s = 0; for (let i = 0; i < d.length; i += 97) s = (s + d[i]) % 100000; return s; })();

  const gl = document.createElement('canvas').getContext('webgl');
  const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');

  return {
    intlTimeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    intlLocale: Intl.DateTimeFormat().resolvedOptions().locale,
    tzOffsetMin: new Date().getTimezoneOffset(),
    language: navigator.language,
    languages: (navigator.languages || []).join(','),
    screen: [screen.width, screen.height, screen.availWidth, screen.availHeight, screen.colorDepth].join('x'),
    viewport: window.innerWidth + 'x' + window.innerHeight,
    devicePixelRatio: window.devicePixelRatio,
    platform: navigator.platform,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory ?? null,
    userAgent: navigator.userAgent,
    uaDataPlatform: navigator.userAgentData ? navigator.userAgentData.platform : null,
    uaDataPlatformVersion: navigator.userAgentData ? (navigator.userAgentData.getHighEntropyValues ? 'async' : null) : null,
    webglVendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
    webglRenderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
    canvasHash: cvHash,
    canvasStable: cvStable,
    canvasPixelSum: pxSum,
    noiseShimSeed: (typeof window.__roxyNoiseSeed === 'number' ? window.__roxyNoiseSeed : null),
    webdriver: navigator.webdriver,
  };
})()`;

const post = async (p, b) => (await fetch(API + p, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b ?? {}),
})).json();
const get = async (p) => (await fetch(API + p)).json();

async function probe(port) {
  const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = list.find((t) => t.type === 'page') ?? list[0];
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });

  const send = (id, method, params) => new Promise((res) => {
    const on = (ev) => { const m = JSON.parse(ev.data); if (m.id === id) { ws.removeEventListener('message', on); res(m.result); } };
    ws.addEventListener('message', on);
    ws.send(JSON.stringify({ id, method, params }));
    setTimeout(() => res(null), 12000);
  });

  // CRITICAL: content scripts never run on about:blank, so navigate to a real
  // http origin before probing — otherwise the noise shim would look broken.
  await send(1, 'Page.enable', {});
  await send(2, 'Page.navigate', { url: 'http://127.0.0.1:50001/_blank' });
  await new Promise((r) => setTimeout(r, 1200));

  const r = await send(3, 'Runtime.evaluate', { expression: PROBE, returnByValue: true });
  ws.close();
  return r?.result?.value ?? null;
}

// ---------- 1. create ----------
const created = await post('/browser/create', spec);
if (created.code !== 0) { console.error('create failed:', created); process.exit(2); }
const dirId = created.data.dirId;
console.log(`[create] ${created.data.windowName}  dirId=${dirId}`);
console.log(`         locale=${created.data.locale}  timeZone=${created.data.timeZone}  screen=${created.data.screen}  os=${created.data.os}`);

// ---------- 2. read back what was written to lumi.conf ----------
const fp = (await get(`/browser/fingerprint?dirId=${dirId}&full=1`)).data;

// ---------- 3. probe the live browser ----------
const port = created.data.http.split(':')[1];
const live = await probe(port);
if (!live) { console.error('CDP probe returned nothing'); await post('/browser/delete', { dirId }); process.exit(2); }

// ---------- 4. compare ----------
const expect = {
  intlTimeZone: fp.timeZone,
  language: fp.appLocale,
  languages: fp.acceptLang,
  screen: fp.screen ? `${fp.screen.width}x${fp.screen.height}x${fp.screen.availWidth}x${fp.screen.availHeight}x${fp.screen.colorDepth}` : null,
  platform: fp.navigator?.platform,
  hardwareConcurrency: fp.navigator?.hardwareConcurrency,
  webglVendor: fp.WebGL?.webglVendor,
  webglRenderer: fp.WebGL?.webglRenderer,
  userAgent: fp.userAgent,
};
const canvasNoiseValue = fp.canvasContext?.canvasContextNoiseValue ?? null;

const rows = [];
const cmp = (field, exp, got, note = '') => {
  let ok;
  if (field === 'webglRenderer') ok = String(got ?? '').startsWith(String(exp ?? '').slice(0, 60));
  else ok = String(exp) === String(got);
  rows.push({ field, expected: exp ?? '(unset)', actual: got ?? '(unset)', ok, note });
};
cmp('intlTimeZone', expect.intlTimeZone, live.intlTimeZone);
cmp('language', expect.language, live.language);
cmp('languages', expect.languages, live.languages);
cmp('screen', expect.screen, live.screen);
cmp('platform', expect.platform, live.platform);
cmp('hardwareConcurrency', expect.hardwareConcurrency, live.hardwareConcurrency);
cmp('webglVendor', expect.webglVendor, live.webglVendor);
cmp('webglRenderer', expect.webglRenderer, live.webglRenderer);
cmp('userAgent', expect.userAgent, live.userAgent);

let pass = 0, fail = 0;
for (const r of rows) { r.ok ? pass++ : fail++; }
console.log('\n  field                 expected                                     actual                                       ');
console.log('  ' + '-'.repeat(114));
for (const r of rows) {
  console.log(`  ${r.ok ? 'OK  ' : 'FAIL'} ${r.field.padEnd(20)} ${String(r.expected).padEnd(44)} ${String(r.actual)}`);
}
console.log(`\n  ${pass} passed, ${fail} failed`);
console.log(`\n  [extra] viewport=${live.viewport}  dpr=${live.devicePixelRatio}  tzOffset=${live.tzOffsetMin}min  deviceMemory=${live.deviceMemory}  webdriver=${live.webdriver}`);
console.log(`  [extra] canvasHash=${live.canvasHash}  stableWithinProfile=${live.canvasStable}  pixelSum=${live.canvasPixelSum}  noiseShim=${live.noiseShimSeed ?? 'NOT INJECTED'}`);
console.log(`  [extra] canvasNoiseValue(written)=${canvasNoiseValue}`);

// ---------- 5. cleanup ----------
await post('/browser/close', { dirId });
await post('/browser/delete', { dirId });
console.log(`\n[cleanup] closed + deleted ${dirId.slice(0, 8)}…`);
process.exit(fail === 0 ? 0 : 1);
