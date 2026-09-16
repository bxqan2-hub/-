// 最后一问：canvas 噪声是不是在 headless 下被跳过？对照 headful。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn, execFileSync } from 'node:child_process';
import { decLumi, encLumi, getPaths, coreExe, profileDir, newestTemplateDir } from './fingerprint.mjs';
const PATHS = getPaths();

const EXE = coreExe();
const TEMPLATE = process.env.TEMPLATE_DIR ?? newestTemplateDir();   // 动态选取，不再写死
const template = JSON.parse(decLumi(fs.readFileSync(path.join(PATHS.browserCacheDir, TEMPLATE, 'lumi.conf'), 'utf8')));

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

const run = async (tag, headful, noiseValue) => {
  const cfg = JSON.parse(JSON.stringify(template));
  cfg.windowName = tag;
  cfg.canvasContext.enableCanvasContextNoise = true;
  cfg.canvasContext.canvasContextNoiseValue = noiseValue;
  const dirId = crypto.randomBytes(16).toString('hex');
  const ud = profileDir(dirId);
  fs.mkdirSync(ud, { recursive: true });
  cfg.browserIconPath = path.join(ud, 'chrome-icon.ico');
  fs.writeFileSync(path.join(ud, 'lumi.conf'), encLumi(JSON.stringify(cfg)));

  const args = ['--disable-background-mode','--no-first-run','--no-default-browser-check',
    '--use-mock-keychain','--no-sandbox','--disable-setuid-sandbox','--password-store=basic',
    '--disable-backgrounding-occluded-windows',`--user-data-dir=${ud}`,'--remote-debugging-port=0','about:blank'];
  if (!headful) args.splice(args.length - 1, 0, '--headless=new');

  const p = spawn(EXE, args, { detached: true, stdio: 'ignore', windowsHide: !headful });
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
    val = await new Promise((res)=>{ ws.addEventListener('message',ev=>{const m=JSON.parse(ev.data); if(m.id===1)res(m.result?.result?.value);});
      ws.send(JSON.stringify({id:1,method:'Runtime.evaluate',params:{expression:PROBE,returnByValue:true}})); setTimeout(()=>res(null),10000); });
    ws.close();
  }
  try { execFileSync('taskkill',['/PID',String(p.pid),'/T','/F'],{stdio:'ignore'}); } catch {}
  await new Promise(r => setTimeout(r, 1500));
  try { fs.rmSync(ud, { recursive: true, force: true }); } catch {}
  const line = `  ${tag.padEnd(28)} headless=${String(!headful).padEnd(5)} noise=${noiseValue.slice(0,8)}  ->  hash=${val?.hash}  sum=${val?.sum}`;
  console.log(line);
  return { tag, headless: !headful, noise: noiseValue.slice(0, 8), hash: val?.hash, sum: val?.sum };
};

const results = [];
results.push(await run('headful-A', true,  '11111111111111111111111111111111'));
results.push(await run('headful-B', true,  '22222222222222222222222222222222'));
results.push(await run('headless-A', false,'33333333333333333333333333333333'));
results.push(await run('headless-B', false,'44444444444444444444444444444444'));

const headfulHashes = new Set(results.filter(r => !r.headless).map(r => r.hash));
const headlessHashes = new Set(results.filter(r => r.headless).map(r => r.hash));
console.log('\n=== 结论 ===');
console.log(`  headful  两个不同 noise 值 -> ${headfulHashes.size} 个不同哈希 ${headfulHashes.size > 1 ? '（噪声生效）' : '（噪声无效）'}`);
console.log(`  headless 两个不同 noise 值 -> ${headlessHashes.size} 个不同哈希 ${headlessHashes.size > 1 ? '（噪声生效）' : '（噪声无效）'}`);
