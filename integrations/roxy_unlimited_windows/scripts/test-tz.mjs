// ============================================================
//  test-tz.mjs  —  验证 timeZone / appLocale / acceptLang 是否真的生效
//
//  结论（本机实测）：写进 lumi.conf 立即生效，不需要 TZ 环境变量。
//    写入 America/Sao_Paulo / pt-BR / pt-BR,pt
//    → Intl 时区 America/Sao_Paulo、偏移 180 分钟、navigator.language pt-BR
//
//  用法：node test-tz.mjs [--full-paths]
// ============================================================
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn, execFileSync } from 'node:child_process';
import {
  decLumi, encLumi, getPaths, pathHelp, show,
  profileDir, newestTemplateDir, coreExe,
} from './fingerprint.mjs';

const FULL = process.argv.includes('--full-paths');
const P = getPaths();
if (!P.ok) { console.error(pathHelp(undefined, FULL)); process.exit(2); }

const EXE = coreExe();
// 模板动态选取：优先挑一个已经带 timeZone/appLocale 的档案，否则用最新的
function pickTemplate() {
  const dir = P.browserCacheDir;
  const ids = fs.readdirSync(dir, { withFileTypes: true })
    .filter((e) => e.isDirectory())
    .map((e) => e.name);
  const withTz = ids.find((id) => {
    try { const c = JSON.parse(decLumi(fs.readFileSync(path.join(dir, id, 'lumi.conf'), 'utf8'))); return c.timeZone || c.appLocale; } catch { return false; }
  });
  return withTz ?? newestTemplateDir();
}
const TEMPLATE = process.env.TEMPLATE_DIR ?? pickTemplate();
if (!TEMPLATE) { console.error('没有可用作模板的档案，先跑 mkprofile.mjs 建一个'); process.exit(2); }
console.log(`[env] 数据目录 ${show(P.dataDir, FULL)}`);
console.log(`[env] 模板档案 ${TEMPLATE}\n`);

const template = JSON.parse(decLumi(fs.readFileSync(path.join(P.browserCacheDir, TEMPLATE, 'lumi.conf'), 'utf8')));

// ---- 两组对照：A 沿用模板原值，B 改成 BR ----
const cases = [
  { tag: 'A-原值', tz: template.timeZone ?? 'Asia/Tokyo', locale: template.appLocale ?? 'ja-JP', accept: template.acceptLang ?? 'ja-JP,ja' },
  { tag: 'B-改成BR', tz: 'America/Sao_Paulo', locale: 'pt-BR', accept: 'pt-BR,pt' },
];

const results = [];
for (const c of cases) {
  const dirId = crypto.randomBytes(16).toString('hex');
  const ud = profileDir(dirId);
  fs.mkdirSync(ud, { recursive: true });

  const cfg = JSON.parse(JSON.stringify(template));
  cfg.windowName = c.tag;
  cfg.timeZone = c.tz;
  cfg.appLocale = c.locale;
  cfg.acceptLang = c.accept;
  cfg.browserIconPath = path.join(ud, 'chrome-icon.ico');
  cfg.portScan = { enablePortScanWhiteList: true, portScanWhiteList: '45535;' };
  fs.writeFileSync(path.join(ud, 'lumi.conf'), encLumi(JSON.stringify(cfg)));

  const p = spawn(EXE, [
    '--disable-background-mode', '--no-first-run', '--no-default-browser-check',
    '--use-mock-keychain', '--no-sandbox', '--disable-setuid-sandbox',
    '--password-store=basic', '--disable-backgrounding-occluded-windows',
    `--user-data-dir=${ud}`, '--remote-debugging-port=0', '--headless=new',
    '--disable-gpu', 'about:blank',
  ], { detached: true, stdio: 'ignore', windowsHide: true });
  p.unref();

  const f = path.join(ud, 'DevToolsActivePort');
  let port = 0;
  for (let i = 0; i < 100; i++) {
    await new Promise((r) => setTimeout(r, 300));
    try { const t = fs.readFileSync(f, 'utf8').split('\n').filter(Boolean); if (t[0] && parseInt(t[0], 10) > 0) { port = parseInt(t[0], 10); break; } } catch {}
  }
  let got = null;
  if (port) {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const page = list.find((t) => t.type === 'page') ?? list[0];
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
    const expr = `({tz: Intl.DateTimeFormat().resolvedOptions().timeZone, lang: navigator.language, langs: (navigator.languages||[]).join(','), off: new Date().getTimezoneOffset()})`;
    got = await new Promise((res) => {
      ws.addEventListener('message', (ev) => { const m = JSON.parse(ev.data); if (m.id === 1) res(m.result?.result?.value); });
      ws.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression: expr, returnByValue: true } }));
      setTimeout(() => res(null), 10000);
    });
    ws.close();
  }
  results.push({ tag: c.tag, 写入: { tz: c.tz, locale: c.locale, accept: c.accept }, 实测: got });
  try { execFileSync('taskkill', ['/PID', String(p.pid), '/T', '/F'], { stdio: 'ignore' }); } catch {}
  await new Promise((r) => setTimeout(r, 800));
  try { fs.rmSync(ud, { recursive: true, force: true }); } catch {}
}

console.log('写入 vs 浏览器内实测：\n');
for (const r of results) {
  console.log(`  [${r.tag}]`);
  console.log(`    写入  tz=${r.写入.tz}  locale=${r.写入.locale}  accept=${r.写入.accept}`);
  console.log(`    实测  tz=${r.实测?.tz}  language=${r.实测?.lang}  languages=${r.实测?.langs}  偏移=${r.实测?.off}分钟`);
  const ok = r.实测 && r.实测.lang === r.写入.locale && r.实测.langs === r.写入.accept;
  console.log(`    判定  ${ok ? '语言类一致' : '不一致'}（时区可能是 ICU 等价别名，如 Asia/Ho_Chi_Minh → Asia/Saigon）`);
  console.log('');
}
