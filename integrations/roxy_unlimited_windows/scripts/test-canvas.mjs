// 变量隔离：从同一个模板克隆，只改 canvas 相关开关，看 canvas 输出是否变化
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn, execFileSync } from 'node:child_process';
import { decLumi, encLumi, getPaths, coreExe, profileDir, readFingerprint, newestTemplateDir } from './fingerprint.mjs';
const PATHS = getPaths();

const EXE = coreExe();
const TEMPLATE = process.env.TEMPLATE_DIR ?? newestTemplateDir();   // 动态选取，不再写死

const cases = [
  { tag: 'noise-OFF',                    mutate: (c) => { c.canvasContext.enableCanvasContextNoise = false; } },
  { tag: 'noise-ON-newvalue',            mutate: (c) => { c.canvasContext.enableCanvasContextNoise = true; c.canvasContext.canvasContextNoiseValue = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'; } },
  { tag: 'noise-ON-value-from-2e30',     mutate: (c) => { c.canvasContext.enableCanvasContextNoise = true; c.canvasContext.canvasContextNoiseValue = '57FD1D43519B3AC8E6C49EB9B29E8B28'; } },
  { tag: 'untouched-clone',              mutate: () => {} },
];

const PROBE = `(() => {
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

const template = JSON.parse(decLumi(fs.readFileSync(path.join(PATHS.browserCacheDir, TEMPLATE, 'lumi.conf'), 'utf8')));
console.log(`template ${TEMPLATE.slice(0,8)}  canvasNoise=${template.canvasContext.canvasContextNoiseValue}  enable=${template.canvasContext.enableCanvasContextNoise}\n`);

const out = [];
for (const c of cases) {
  const cfg = JSON.parse(JSON.stringify(template));
  c.mutate(cfg);
  const dirId = crypto.randomBytes(16).toString('hex');
  const ud = profileDir(dirId);
  fs.mkdirSync(ud, { recursive: true });
  cfg.windowName = c.tag;
  cfg.browserIconPath = path.join(ud, 'chrome-icon.ico');
  fs.writeFileSync(path.join(ud, 'lumi.conf'), encLumi(JSON.stringify(cfg)));

  const p = spawn(EXE, ['--disable-background-mode','--no-first-run','--no-default-browser-check',
    '--use-mock-keychain','--no-sandbox','--disable-setuid-sandbox','--password-store=basic',
    '--disable-backgrounding-occluded-windows',`--user-data-dir=${ud}`,'--remote-debugging-port=0',
    '--headless=new','--disable-gpu','about:blank'], { detached: true, stdio: 'ignore', windowsHide: true });
  p.unref();

  const f = path.join(ud, 'DevToolsActivePort');
  let port = 0;
  for (let i = 0; i < 100; i++) { await new Promise(r => setTimeout(r, 300));
    try { const t = fs.readFileSync(f,'utf8').split('\n').filter(Boolean); if (t[0] && +t[0] > 0) { port = +t[0]; break; } } catch {} }
  let val = null;
  if (port) {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const page = list.find(t => t.type === 'page') ?? list[0];
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((r,j)=>{ws.addEventListener('open',r);ws.addEventListener('error',j);});
    val = await new Promise((res)=>{ ws.addEventListener('message',ev=>{const m=JSON.parse(ev.data); if(m.id===1) res(m.result?.result?.value);});
      ws.send(JSON.stringify({id:1,method:'Runtime.evaluate',params:{expression:PROBE,returnByValue:true}})); setTimeout(()=>res(null),10000); });
    ws.close();
  }
  out.push({ tag: c.tag, enable: cfg.canvasContext.enableCanvasContextNoise, value: cfg.canvasContext.canvasContextNoiseValue.slice(0,8), hash: val?.hash, sum: val?.sum });
  console.log(`  ${String(c.tag).padEnd(24)} enable=${String(cfg.canvasContext.enableCanvasContextNoise).padEnd(5)} value=${cfg.canvasContext.canvasContextNoiseValue.slice(0,8)}  ->  hash=${val?.hash}  sum=${val?.sum}`);
  try { execFileSync('taskkill',['/PID',String(p.pid),'/T','/F'],{stdio:'ignore'}); } catch {}
  await new Promise(r => setTimeout(r, 1500));
  try { fs.rmSync(ud, { recursive: true, force: true }); } catch { console.log(`  (dir busy, left behind: ${dirId.slice(0,8)})`); }
  await new Promise(r => setTimeout(r, 400));
}
console.log('\n' + JSON.stringify(out, null, 2));
