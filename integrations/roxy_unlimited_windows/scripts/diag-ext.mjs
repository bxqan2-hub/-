// 诊断：噪声扩展到底有没有被加载？
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn, execFileSync } from 'node:child_process';
import { createProfileOnDisk, getPaths, coreExe, profileDir } from './fingerprint.mjs';
const PATHS = getPaths();

const NOISE_SRC = path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), 'noise-ext');
const EXE = coreExe();

const built = createProfileOnDisk({ windowName: 'diag', locale: 'pt-BR', screen: [1920, 1080] });
const dirId = built.dirId;

// 手写一份与 roxy-api 相同的逐档案扩展副本
const seed = crypto.createHash('sha256').update('diag|' + dirId).digest().readUInt32BE(0);
const noiseDir = path.join(PATHS.dataDir, 'temp', 'profile-noise', dirId);
fs.mkdirSync(noiseDir, { recursive: true });
fs.writeFileSync(path.join(noiseDir, 'noise.js'), fs.readFileSync(path.join(NOISE_SRC, 'noise.js'), 'utf8').replace('__SEED__', String(seed >>> 0)));
for (const f of ['manifest.json', 'bg.js']) fs.copyFileSync(path.join(NOISE_SRC, f), path.join(noiseDir, f));

const variants = {
  'only-noise-ext':            [`--load-extension=${noiseDir}`],
  'both-ext-comma':            [`--load-extension=${PATHS.extensionDir},${noiseDir}`],
  'noise-via-disable-except':  [`--load-extension=${noiseDir}`, `--disable-extensions-except=${noiseDir}`],
};

for (const [tag, extra] of Object.entries(variants)) {
  const ud = profileDir(dirId);
  try { fs.rmSync(path.join(ud, 'DevToolsActivePort'), { force: true }); } catch {}
  const args = ['--disable-background-mode','--no-first-run','--no-default-browser-check',
    '--use-mock-keychain','--no-sandbox','--disable-setuid-sandbox','--password-store=basic',
    '--disable-backgrounding-occluded-windows', `--user-data-dir=${ud}`,
    '--remote-debugging-port=0', '--headless=new', ...extra, 'about:blank'];
  const p = spawn(EXE, args, { detached: true, stdio: 'ignore', windowsHide: true });
  p.unref();

  const f = path.join(ud, 'DevToolsActivePort');
  let port = 0;
  for (let i = 0; i < 100; i++) { await new Promise(r => setTimeout(r, 300));
    try { const t = fs.readFileSync(f,'utf8').split('\n').filter(Boolean); if (t[0] && +t[0] > 0) { port = +t[0]; break; } } catch {} }

  let targets = [], shimSeed = null, swTarget = null;
  if (port) {
    targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    swTarget = targets.find(t => t.type === 'service_worker' && String(t.url).includes('noise'));
    // 导航到真实 http 源，再查 MAIN world 的标记
    const page = targets.find(t => t.type === 'page');
    if (page) {
      const ws = new WebSocket(page.webSocketDebuggerUrl);
      await new Promise((r,j)=>{ws.addEventListener('open',r);ws.addEventListener('error',j);});
      const send = (id, m, params) => new Promise(res => { const on = ev => { const x = JSON.parse(ev.data); if (x.id===id){ws.removeEventListener('message',on);res(x.result);} };
        ws.addEventListener('message', on); ws.send(JSON.stringify({id,method:m,params})); setTimeout(()=>res(null),8000); });
      await send(1,'Page.enable',{});
      await send(2,'Page.navigate',{url:'http://127.0.0.1:50001/_blank'});
      await new Promise(r=>setTimeout(r,1500));
      const r = await send(3,'Runtime.evaluate',{expression:'typeof window.__roxyNoiseSeed === "number" ? window.__roxyNoiseSeed : null',returnByValue:true});
      shimSeed = r?.result?.value ?? null;
      const all = await send(4,'Target.getTargets',{});
      if (all?.targetInfos) {
        const extTargets = all.targetInfos.filter(t => String(t.url||'').startsWith('chrome-extension://'));
        console.log(`    extension targets: ${extTargets.map(t=>t.type+':'+t.url.split('/')[2]?.slice(0,8)).join(', ') || 'none'}`);
      }
      ws.close();
    }
  }
  console.log(`  ${tag.padEnd(28)} extTargets=${targets.filter(t=>String(t.url).includes('chrome-extension')).length}  noiseSW=${!!swTarget}  shimSeed=${shimSeed ?? 'NULL'}`);
  try { execFileSync('taskkill',['/PID',String(p.pid),'/T','/F'],{stdio:'ignore'}); } catch {}
  await new Promise(r=>setTimeout(r,1200));
}

fs.rmSync(profileDir(dirId), { recursive: true, force: true });
