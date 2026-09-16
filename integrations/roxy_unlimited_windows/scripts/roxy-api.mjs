// ============================================================
//  roxy-api.mjs  —  Roxy-compatible local OpenAPI for UNLIMITED windows
//
//  Why this instead of patching the official asar:
//    the official OpenAPI resolves every dirId through the server
//    (user_get_window_info_v2), so it can never exceed maxWindowCount.
//    This server speaks the SAME response shape but resolves dirIds
//    locally, so it is not bound by the account quota at all.
//
//  Response shape is identical to the official one:
//    {code:0,msg:"成功",data:{dirId,ws,http,coreVersion,driver,sortNum,windowName,pid}}
//
//  Run:  node roxy-api.mjs --port 50001
// ============================================================
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { spawn, execFile } from 'node:child_process';
import {
  getPaths, pathHelp, show, LOCALE_PRESETS, SCREENS, WINDOWS_PROFILES,
  coreExe, coreVersion, lumiPath, profileDir, hasProfile, isDirId,
  readFingerprint, createProfileOnDisk,
} from './fingerprint.mjs';

// ---------- config ----------
if (Number(process.versions.node.split('.')[0]) < 22) {
  console.error('Node.js 22+ is required');
  process.exit(2);
}
const argv = process.argv.slice(2);
const argOf = (n, d) => { const i = argv.indexOf('--' + n); return i === -1 ? d : argv[i + 1]; };
const PORT      = parseInt(argOf('port', '50001'), 10);
const HEADLESS  = argv.includes('--headless-default');
const WORKBENCH = argv.includes('--workbench-default');
const APP_PORT  = parseInt(argOf('app-port', '45535'), 10);
const DEF_LOCALE = argOf('locale', null);
const FULL_PATHS = argv.includes('--full-paths');   // 默认脱敏显示路径

// 路径自动发现：--data-dir / --install-dir / ROXY_HOME / ROXY_INSTALL / 常见位置 / 注册表 / 运行中进程
const PATHS = getPaths({ dataDir: argOf('data-dir'), installDir: argOf('install-dir'),
  profileDir: argOf('profile-dir', fileURLToPath(new URL('../../../data/roxy_local/browser-cache/', import.meta.url))) });

const windowNameOf = (dirId) => readFingerprint(dirId)?.windowName || dirId.slice(0, 8);

// ---------- per-profile canvas/audio noise via CDP ----------
// The core's own canvasContext.noise knobs are inert in this build (verified:
// four configs differing only in enable/value produce one identical canvas hash),
// and MAIN-world content scripts do not inject reliably here, so the shim is
// installed over CDP with Page.addScriptToEvaluateOnNewDocument — the same
// mechanism Playwright uses. It survives detach for the lifetime of each target.
const NOISE_SRC_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), 'noise-ext');
const NOISE_SRC = path.join(NOISE_SRC_DIR, 'noise.js');
const NOISE_TEMP = path.join(PATHS.tempDir ?? process.cwd(), 'profile-noise');
const noiseSource = (dirId, fp) => {
  const seedSource = String(fp?.canvasContext?.canvasContextNoiseValue ?? '') + '|' + dirId;
  const seed = crypto.createHash('sha256').update(seedSource).digest().readUInt32BE(0);
  // replaceAll, not replace: __SEED__ also appears in the file's header comment
  return fs.readFileSync(NOISE_SRC, 'utf8').replaceAll('__SEED__', String(seed >>> 0));
};

/** Per-profile unpacked extension copy — Chromium loads one instance per dirId. */
function materializeNoiseExt(dirId, fp) {
  const dir = path.join(NOISE_TEMP, dirId);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'noise.js'), noiseSource(dirId, fp));
  for (const f of ['manifest.json', 'bg.js']) fs.copyFileSync(path.join(NOISE_SRC_DIR, f), path.join(dir, f));
  return dir;
}

/** Keeps a controller CDP connection alive so new tabs also get the shim. */
async function installNoiseShim(wsUrl, source) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });

  let id = 0;
  const pending = new Map();
  const send = (method, params, sessionId) => new Promise((res) => {
    const mid = ++id;
    pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method, params, ...(sessionId ? { sessionId } : {}) }));
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); res(null); } }, 8000);
  });

  const inject = async (sessionId) => {
    await send('Page.addScriptToEvaluateOnNewDocument', { source, runImmediately: true }, sessionId);
  };

  ws.addEventListener('message', (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); return; }
    if (m.method === 'Target.attachedToTarget') {
      const t = m.params.targetInfo;
      if (t.type === 'page' || t.type === 'iframe') inject(m.params.sessionId).catch(() => {});
    }
  });

  await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });

  // cover targets that already existed before auto-attach was armed
  const list = await send('Target.getTargets', {});
  for (const t of list?.targetInfos ?? []) {
    if (t.type !== 'page' && t.type !== 'iframe') continue;
    const att = await send('Target.attachToTarget', { targetId: t.targetId, flatten: true });
    if (att?.sessionId) await inject(att.sessionId);
  }
  return ws;
}

// ---------- running-window registry ----------
/** dirId -> {dirId,pid,ws,http,windowName,coreVersion,driver,startedAt} */
const running = new Map();
const launching = new Map();

const json = (res, obj, status = 200) => {
  const body = JSON.stringify(obj);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(body) });
  res.end(body);
};
const ok  = (res, data) => json(res, { code: 0, msg: '成功', data: data ?? null });
const err = (res, msg, code = 101) => json(res, { code, msg, data: null });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitDevTools(ud, timeoutMs = 45000) {
  const f = path.join(ud, 'DevToolsActivePort');
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const t = fs.readFileSync(f, 'utf8').split('\n').map((s) => s.trim()).filter(Boolean);
      const port = parseInt(t[0], 10);
      if (port > 0 && t[1]) {
        try {
          const r = await fetch(`http://127.0.0.1:${port}/json/version`, { signal: AbortSignal.timeout(3000) });
          if (r.ok) return { port, wsPath: t[1] };
        } catch { /* not listening yet */ }
      }
    } catch { /* not written yet */ }
    await sleep(300);
  }
  throw new Error('timed out waiting for DevTools endpoint');
}

async function launchWindow(dirId, opts = {}) {
  if (!hasProfile(dirId)) throw Object.assign(new Error('窗口/数据不存在，请刷新页面后重试'), { code: 101 });

  const cur = running.get(dirId);
  if (cur) { try { process.kill(cur.pid, 0); return cur; } catch { running.delete(dirId); } }

  const ud = profileDir(dirId);
  for (const f of ['DevToolsActivePort', 'SingletonCookie', 'SingletonLock', 'SingletonSocket']) {
    try { fs.rmSync(path.join(ud, f), { force: true }); } catch {}
  }

  const fp = readFingerprint(dirId) ?? {};
  const executable = coreExe({ coreVersion: fp.chromeVersion });
  // window size follows the profile's own screen config so the two never disagree
  const scr = fp.screen ?? {};
  const w = Number(scr.width) || 1920;
  const h = Number(scr.height) || 1080;

  const headless = opts.headless ?? HEADLESS;
  const args = [
    '--disable-background-mode',
    '--disable-popup-blocking',
    '--no-first-run',
    '--no-default-browser-check',
    '--use-mock-keychain',
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--password-store=basic',
    '--disable-backgrounding-occluded-windows',
    `--user-data-dir=${ud}`,
    '--remote-debugging-port=0',          // official uses port 0 + DevToolsActivePort
    `--window-size=${w},${h}`,
  ];
  // keep the browser-level language in lockstep with lumi.conf's appLocale/acceptLang
  if (fp.appLocale)  args.push(`--lang=${fp.appLocale}`);
  if (fp.acceptLang) args.push(`--accept-lang=${fp.acceptLang}`);

  const useWorkbench = opts.workbench ?? WORKBENCH;
  if (useWorkbench) args.push(`http://127.0.0.1:${APP_PORT}/dashboard.html?id=${dirId}&workspaceType=0`);

  const exts = [];
  if (PATHS.extensionDir) exts.push(PATHS.extensionDir);           // vendor automation-control
  exts.push(materializeNoiseExt(dirId, fp));                // our per-profile noise
  args.push(`--load-extension=${exts.join(',')}`);

  if (Array.isArray(opts.args)) args.push(...opts.args);
  if (headless) args.push('--headless=new');
  if (opts.useGpu === false) args.push('--disable-gpu');
  args.push(opts.startUrl || 'about:blank');

  const proc = spawn(executable, args, { detached: true, stdio: 'ignore', windowsHide: true });
  await new Promise((resolve, reject) => { proc.once('spawn', resolve); proc.once('error', reject); });
  proc.unref();
  running.set(dirId, { dirId, pid: proc.pid });
  let endpoint;
  try { endpoint = await waitDevTools(ud); }
  catch (error) { launching.delete(dirId); await closeWindow(dirId); throw error; }
  const { port, wsPath } = endpoint;
  const ws = `ws://127.0.0.1:${port}${wsPath}`;
  const rec = {
    dirId, pid: proc.pid, port,
    http: `127.0.0.1:${port}`,
    ws,
    windowName: windowNameOf(dirId),
    coreVersion: fp.chromeVersion,
    driver: path.join(path.dirname(executable), 'chromedriver.exe'),
    startedAt: Date.now(),
    noiseInstalled: false,
  };
  running.set(dirId, rec);

  // per-profile canvas/audio shim — non-fatal if it fails
  try {
    rec.noiseController = await installNoiseShim(ws, noiseSource(dirId, fp));
    rec.noiseInstalled = true;
  } catch (e) {
    console.warn(`[roxy-api] noise shim failed for ${dirId}: ${e?.message ?? e}`);
  }

  return rec;
}

const handleOf = (rec, sortNum) => ({
  dirId: rec.dirId, ws: rec.ws, http: rec.http, coreVersion: rec.coreVersion,
  driver: rec.driver, sortNum: sortNum ?? 1, windowName: rec.windowName,
  windowRemark: '', pid: rec.pid,
});

async function cdpClose(wsUrl) {
  return new Promise((resolve) => {
    let done = false;
    let ws;
    const fin = () => { if (!done) { done = true; try { ws.close(); } catch {} resolve(); } };
    try {
      ws = new WebSocket(wsUrl);
      ws.addEventListener('open', () => ws.send(JSON.stringify({ id: 1, method: 'Browser.close' })));
      ws.addEventListener('error', fin);
      setTimeout(fin, 1500);
    } catch { fin(); }
  });
}

async function closeWindow(dirId) {
  if (launching.has(dirId)) { try { await launching.get(dirId); } catch {} }
  const rec = running.get(dirId);
  if (!rec) return false;
  try { rec.noiseController?.close(); } catch {}
  try { await cdpClose(rec.ws); } catch {}
  await sleep(600);
  try {
    process.kill(rec.pid, 0);
    await new Promise((resolve) => execFile('taskkill', ['/PID', String(rec.pid), '/T', '/F'], { windowsHide: true }, resolve));
  } catch {}
  try { process.kill(rec.pid, 0); throw new Error('browser process is still running'); }
  catch (error) { if (error.code !== 'ESRCH') throw error; }
  running.delete(dirId);
  return true;
}

// ---------- HTTP ----------
const readBody = (req) => new Promise((resolve, reject) => {
  let b = '';
  req.on('data', (c) => { b += c; if (b.length > 4e6) req.destroy(); });
  req.on('error', reject);
  req.on('end', () => { try { const body = b ? JSON.parse(b) : {}; if (!body || Array.isArray(body) || typeof body !== 'object') throw new Error(); resolve(body); } catch { reject(new Error('invalid JSON body')); } });
});

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const p = url.pathname;
  try {
    if (![`127.0.0.1:${PORT}`, `localhost:${PORT}`].includes(req.headers.host) || req.headers.origin) return json(res, { code: 403, msg: 'local API clients only' }, 403);
    const writing = ['/browser/create', '/browser/open', '/browser/close', '/browser/close_all', '/browser/delete'].includes(p);
    if (req.method !== (writing ? 'POST' : 'GET')) return json(res, { code: 405, msg: 'method not allowed' }, 405);
    if (writing && !/^application\/json(?:;|$)/i.test(req.headers['content-type'] || '')) return json(res, { code: 415, msg: 'application/json required' }, 415);
    const body = writing ? await readBody(req) : {};
    if (p === '/health') return ok(res, { service: 'roxy-unlimited-windows', profileRoot: PATHS.browserCacheDir, coreVersion: PATHS.coreVersion });

    // minimal same-origin page: gives the per-profile noise extension a real
    // http origin to run on (content scripts never match about:blank)
    if (p === '/_blank') {
      const html = '<!doctype html><meta charset="utf-8"><title>blank</title><body></body>';
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Access-Control-Allow-Origin': '*' });
      return res.end(html);
    }

    // ---------- metadata: what the caller may ask for ----------
    if (p === '/meta/locales') {
      return ok(res, { locales: LOCALE_PRESETS, screens: SCREENS, os: WINDOWS_PROFILES.map((w) => w.name) });
    }

    if (p === '/browser/list') {
      const rows = fs.readdirSync(PATHS.browserCacheDir, { withFileTypes: true })
        .filter((e) => e.isDirectory() && hasProfile(e.name))
        .map((e, i) => {
          const fp = readFingerprint(e.name) ?? {};
          return {
            dirId: e.name,
            windowName: fp.windowName ?? e.name.slice(0, 8),
            windowSortNum: i + 1,
            openStatus: running.has(e.name) ? 1 : 0,
            statusInfo: null,
            proxyInfo: fp.fproxy ?? {},
            // extras beyond the official shape (safe to ignore)
            timeZone: fp.timeZone ?? null,
            locale: fp.appLocale ?? null,
            screen: fp.screen ? `${fp.screen.width}x${fp.screen.height}` : null,
          };
        });
      return ok(res, { rows, total: rows.length });
    }

    // ---------- create ----------
    if (p === '/browser/create') {
      let screen = null;
      if (Array.isArray(body.screen)) screen = body.screen;
      else if (typeof body.screen === 'string' && /^\d+x\d+$/.test(body.screen)) screen = body.screen.split('x').map(Number);

      const built = createProfileOnDisk({
        windowName: body.windowName,
        proxy: body.proxy ?? 'direct',
        locale: body.locale ?? DEF_LOCALE ?? 'en-US',
        country: body.country,
        coreVersion: body.coreVersion,
        timeZone: body.timeZone,
        acceptLang: body.acceptLang,
        os: body.os,
        screen,
        // without this the window cannot reach this API at all (portScanProtect)
        portScanWhiteList: body.portScanWhiteList ?? `${PORT};45535;${APP_PORT};`,
      });
      const info = {
        dirId: built.dirId,
        windowName: built.cfg.windowName,
        locale: built.locale,
        timeZone: built.timeZone,
        screen: `${built.screen.width}x${built.screen.height}`,
        os: built.os,
        proxy: built.cfg.fproxy ? `${built.cfg.fproxy.type}://${built.cfg.fproxy.host}:${built.cfg.fproxy.port}` : 'direct',
        portScanWhiteList: built.cfg.portScan?.portScanWhiteList ?? null,
        startUrl: body.startUrl ?? null,
      };
      if (body.open) {
        const rec = await launchWindow(built.dirId, body);
        return ok(res, { ...handleOf(rec), ...info });
      }
      return ok(res, info);
    }

    if (p === '/browser/open') {
      if (!body.dirId) return err(res, 'dirId is required');
      if (!launching.has(body.dirId)) launching.set(body.dirId, launchWindow(body.dirId, body));
      let rec;
      try { rec = await launching.get(body.dirId); }
      finally { launching.delete(body.dirId); }
      return ok(res, handleOf(rec));
    }

    if (p === '/browser/connection_info') {
      const dirId = url.searchParams.get('dirId') ?? body.dirId;
      const list = [...running.values()].filter((r) => !dirId || r.dirId === dirId).map((r, i) => handleOf(r, i + 1));
      return ok(res, list);
    }

    if (p === '/browser/close') {
      if (!body.dirId) return err(res, 'dirId is required');
      const closed = await closeWindow(body.dirId);
      return ok(res, { closed });
    }

    if (p === '/browser/close_all') {
      const ids = [...running.keys()];
      for (const id of ids) await closeWindow(id);
      return ok(res, { closed: ids.length });
    }

    if (p === '/browser/delete') {
      if (!body.dirId) return err(res, 'dirId is required');
      await closeWindow(body.dirId);
      if (!isDirId(body.dirId)) return err(res, 'invalid dirId');
      fs.rmSync(profileDir(body.dirId), { recursive: true, force: true, maxRetries: 8, retryDelay: 250 });
      fs.rmSync(path.join(NOISE_TEMP, body.dirId), { recursive: true, force: true, maxRetries: 8, retryDelay: 250 });
      return ok(res);
    }

    // read a profile's decrypted fingerprint (no secrets beyond what the caller owns)
    if (p === '/browser/fingerprint') {
      const dirId = url.searchParams.get('dirId') ?? body.dirId;
      if (!hasProfile(dirId)) return err(res, '窗口/数据不存在');
      const fp = readFingerprint(dirId);
      if (body.full === true || url.searchParams.get('full') === '1') return ok(res, fp);
      return ok(res, {
        windowName: fp.windowName, userAgent: fp.userAgent, platform: fp.navigator?.platform,
        hardwareConcurrency: fp.navigator?.hardwareConcurrency, deviceMemory: fp.navigator?.deviceMemory,
        webglVendor: fp.WebGL?.webglVendor, webglRenderer: fp.WebGL?.webglRenderer,
        timeZone: fp.timeZone ?? null, appLocale: fp.appLocale ?? null, acceptLang: fp.acceptLang ?? null,
        screen: fp.screen ?? null, proxy: fp.fproxy ?? null,
      });
    }

    return json(res, { code: 104, msg: 'Not Found', data: null }, 404);
  } catch (e) {
    return err(res, e?.message ?? String(e), e?.code ?? 500);
  }
});

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, async () => {
    for (const id of [...running.keys()]) await closeWindow(id);
    server.close(() => process.exit(0));
  });
}

// ---------- 启动前置检查：环境不对就别假装能跑 ----------
if (!PATHS.ok) {
  console.error(pathHelp(undefined, FULL_PATHS));
  console.error('提示：可用 --data-dir <路径> 或环境变量 ROXY_HOME 手动指定数据目录。\n');
  process.exit(2);
}

server.listen(PORT, '127.0.0.1', () => {
  const s = (p) => show(p, FULL_PATHS);   // 默认把用户名换成 %USERPROFILE% 之类的占位符
  console.log(`[roxy-api] listening on http://127.0.0.1:${PORT}`);
  console.log(`[roxy-api] core        : ${s(PATHS.coreExe)}  (v${PATHS.coreVersion})`);
  console.log(`[roxy-api] data dir    : ${s(PATHS.dataDir)}`);
  console.log(`[roxy-api] profile base: ${s(PATHS.browserCacheDir)}`);
  console.log(`[roxy-api] install dir : ${s(PATHS.installDir) ?? '(未找到 — 官方扩展与拦截页将不可用，不影响启动)'}`);
  console.log(`[roxy-api] chromedriver: ${s(PATHS.chromedriver) ?? '(未找到)'}`);
  console.log(`[roxy-api] quota       : NONE — windows are resolved locally, no server call`);
  console.log(`[roxy-api] headless default: ${HEADLESS}   workbench default: ${WORKBENCH}`);
  console.log(`[roxy-api] default locale  : ${DEF_LOCALE ?? '(none — inherit template)'}`);
  if (!FULL_PATHS) console.log(`[roxy-api] 路径已脱敏显示，加 --full-paths 看真实路径`);
});
