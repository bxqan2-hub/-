// ============================================================
//  paths.mjs  —  RoxyBrowser 路径自动发现
//
//  不再写死 %APPDATA%\RoxyBrowser。按优先级搜索，并给出可诊断的失败原因。
//
//  解析顺序
//    数据目录（含内核、档案、配置）
//      1. --data-dir / ROXY_HOME 环境变量
//      2. %APPDATA%\RoxyBrowser 与 %APPDATA%\roxybrowser（两者都真实存在过）
//      3. 扫描 %APPDATA% 下任何名字含 roxy 且含 chrome-bin/ 或 browser-cache/ 的目录
//      4. 从正在运行的 RoxyBrowser.exe 进程反推
//    安装目录（可选，只用于官方扩展与 blockDomain 页）
//      1. --install-dir / ROXY_INSTALL
//      2. 常见安装位置
//      3. 数据目录里的 installer-path / config.json 线索
//      4. 注册表卸载项
//      5. 正在运行的进程
//    内核
//      1. <数据目录>\chrome-bin\<coreVersion>\RoxyChrome.exe，取版本号最大者
//      2. 兜底：在 <数据目录>\chrome-bin 下递归找 RoxyChrome.exe
// ============================================================
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const exists = (p) => { try { return fs.existsSync(p); } catch { return false; } };
const isDir = (p) => { try { return fs.statSync(p).isDirectory(); } catch { return false; } };
const listDir = (p) => { try { return fs.readdirSync(p, { withFileTypes: true }); } catch { return []; } };

const uniq = (a) => [...new Set(a.filter(Boolean))];

// ---------- 输出脱敏 ----------
// 实际读写始终用真实路径；只有「打印给人看」的时候把用户名换成占位符，
// 免得截图 / 贴日志 / 提 issue 时把本机用户名带出去。
//
// 用子串替换而不是前缀匹配：诊断信息常是「中文前缀 + 路径」的形式
// （例如 "内核目录不存在：C:\Users\x\..."），前缀匹配会漏掉。
// 顺序：LOCALAPPDATA / APPDATA 都在 USERPROFILE 之下，先替换更长的。
const REDACT_ENVS = [
  ['LOCALAPPDATA', '%LOCALAPPDATA%'],
  ['APPDATA', '%APPDATA%'],
  ['ProgramData', '%ProgramData%'],
  ['USERPROFILE', '%USERPROFILE%'],
  ['TEMP', '%TEMP%'],
];
const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

export function redactPath(s) {
  if (!s || typeof s !== 'string') return s;
  let out = s;
  for (const [env, token] of REDACT_ENVS) {
    const base = process.env[env];
    if (!base || base.length < 4) continue;
    out = out.replace(new RegExp(escRe(base), 'gi'), token);
  }
  return out;
}
/** 显示用：--full-paths 时原样返回 */
export function show(p, fullPaths = false) { return fullPaths ? p : redactPath(p); }

// ---------- 数据目录 ----------
function looksLikeDataDir(p) {
  if (!isDir(p)) return false;
  return exists(path.join(p, 'chrome-bin')) || exists(path.join(p, 'browser-cache'));
}

function findDataDir(opts, diag) {
  // 显式指定的路径是权威的：错了就报错，绝不静默回退到别的目录
  const explicit = [];
  if (opts.dataDir) explicit.push(path.resolve(opts.dataDir));
  if (process.env.ROXY_HOME) explicit.push(path.resolve(process.env.ROXY_HOME));
  if (explicit.length) {
    const seen = [];
    for (const c of uniq(explicit)) {
      const hit = looksLikeDataDir(c);
      seen.push({ path: c, ok: hit, explicit: true });
      if (hit) { diag.dataDir = c; diag.dataDirSource = 'explicit'; return c; }
    }
    diag.dataDirCandidates = seen;
    diag.dataDirExplicitFailed = true;
    return null;
  }

  const cands = [];
  const appdata = process.env.APPDATA;
  if (appdata) {
    cands.push(path.join(appdata, 'RoxyBrowser'));
    cands.push(path.join(appdata, 'roxybrowser'));
    // 扫描同级的任何 roxy* 目录（dev 版、改名版、多用户版）
    for (const e of listDir(appdata)) {
      if (!e.isDirectory() || !/roxy/i.test(e.name)) continue;
      cands.push(path.join(appdata, e.name));
    }
  }

  // 从运行中的进程反推（有些安装把数据目录放在别处）
  const fromProc = processDataDir();
  if (fromProc) cands.push(fromProc);

  const seen = [];
  for (const c of uniq(cands)) {
    const hit = looksLikeDataDir(c);
    seen.push({ path: c, ok: hit });
    if (hit) { diag.dataDir = c; diag.dataDirSource = 'auto'; return c; }
  }
  diag.dataDirCandidates = seen;
  return null;
}

function processDataDir() {
  try {
    const out = execFileSync('powershell', [
      '-NoProfile', '-NonInteractive', '-Command',
      "$p = Get-Process RoxyBrowser -ErrorAction SilentlyContinue | Select-Object -First 1; if ($p) { $p.Path }",
    ], { encoding: 'utf8', timeout: 8000, windowsHide: true }).trim();
    return out || null;
  } catch { return null; }
}

// ---------- 安装目录（可选） ----------
function findInstallDir(opts, diag) {
  const cands = [];
  if (opts.installDir) cands.push(path.resolve(opts.installDir));
  if (process.env.ROXY_INSTALL) cands.push(path.resolve(process.env.ROXY_INSTALL));

  const la = process.env.LOCALAPPDATA, pf = process.env['ProgramFiles'], pf86 = process.env['ProgramFiles(x86)'];
  if (la) {
    cands.push(path.join(la, 'Programs', 'RoxyBrowser'));
    for (const e of listDir(path.join(la, 'Programs'))) {
      if (e.isDirectory() && /roxy/i.test(e.name)) cands.push(path.join(la, 'Programs', e.name));
    }
  }
  if (pf) cands.push(path.join(pf, 'RoxyBrowser'));
  if (pf86) cands.push(path.join(pf86, 'RoxyBrowser'));

  // 注册表卸载项
  try {
    const keys = ['HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall', 'HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall'];
    for (const k of keys) {
      const out = execFileSync('reg', ['query', k, '/s', '/f', 'RoxyBrowser', '/d'], { encoding: 'utf8', timeout: 8000, windowsHide: true });
      for (const m of out.matchAll(/([A-Za-z]:\\[^\r\n"]*RoxyBrowser[^\r\n"]*)/gi)) cands.push(m[1].trim());
    }
  } catch { /* 没有注册表项是正常的 */ }

  const seen = [];
  for (const c of uniq(cands)) {
    const ok = exists(path.join(c, 'RoxyBrowser.exe')) || exists(path.join(c, 'resources'));
    seen.push({ path: c, ok });
    if (ok) { diag.installDir = c; return c; }
  }
  diag.installDirCandidates = seen;
  return null;   // 安装目录缺失不是致命错误
}

// ---------- 内核 ----------
function findCore(dataDir, diag) {
  const bin = path.join(dataDir, 'chrome-bin');
  if (!isDir(bin)) {
    diag.coreProblem = `内核目录不存在：${bin}`;
    return null;
  }
  // 正常布局：chrome-bin\<coreVersion>\RoxyChrome.exe
  const direct = listDir(bin)
    .filter((e) => e.isDirectory())
    .map((e) => ({ ver: e.name, exe: path.join(bin, e.name, 'RoxyChrome.exe') }))
    .filter((c) => exists(c.exe))
    .sort((a, b) => {
      const na = /^\d+$/.test(a.ver) ? parseInt(a.ver, 10) : -1;
      const nb = /^\d+$/.test(b.ver) ? parseInt(b.ver, 10) : -1;
      return nb - na;
    });
  if (direct.length) {
    diag.coreCandidates = direct.map((c) => c.exe);
    return direct[0];
  }
  // 兜底：递归找（有些版本层级不同）
  const found = [];
  (function walk(d, depth) {
    if (depth > 4) return;
    for (const e of listDir(d)) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p, depth + 1);
      else if (e.name.toLowerCase() === 'roxychrome.exe') found.push(p);
    }
  })(bin, 0);
  if (found.length) {
    diag.coreCandidates = found;
    return { ver: 'unknown', exe: found[0] };
  }
  diag.coreProblem = `在 ${bin} 下没找到 RoxyChrome.exe`;
  return null;
}

// ---------- 主入口 ----------
export function resolveRoxyPaths(opts = {}) {
  const diag = {};

  const dataDir = findDataDir(opts, diag);
  const installDir = findInstallDir(opts, diag);
  const core = dataDir ? findCore(dataDir, diag) : null;

  const P = {
    ok: !!(dataDir && core),
    dataDir,
    installDir,
    coreExe: core?.exe ?? null,
    coreVersion: core?.ver ?? null,
    coreBinDir: dataDir ? path.join(dataDir, 'chrome-bin') : null,
    chromedriver: core ? path.join(path.dirname(core.exe), 'chromedriver.exe') : null,
    browserCacheDir: opts.profileDir ? path.resolve(opts.profileDir) : (dataDir ? path.join(dataDir, 'browser-cache') : null),
    tempDir: opts.profileDir ? path.join(path.dirname(path.resolve(opts.profileDir)), 'temp') : (dataDir ? path.join(dataDir, 'temp') : null),
    logsDir: dataDir ? path.join(dataDir, 'logs') : null,
    // 安装目录相关（可选，缺失时优雅降级）
    extensionDir: installDir
      ? path.join(installDir, 'resources', 'app.asar.unpacked', 'resources', 'automation-control-extension')
      : null,
    blockDomainPageFile: installDir
      ? path.join(installDir, 'resources', 'app.asar.unpacked', 'dist', 'web', 'blockDomain.html')
      : null,
    diag,
  };
  if (P.chromedriver && !exists(P.chromedriver)) P.chromedriver = null;
  if (P.extensionDir && !exists(P.extensionDir)) P.extensionDir = null;
  if (P.blockDomainPageFile && !exists(P.blockDomainPageFile)) P.blockDomainPageFile = null;
  if (P.browserCacheDir && !isDir(P.browserCacheDir)) { try { fs.mkdirSync(P.browserCacheDir, { recursive: true }); } catch {} }

  return P;
}

/** 人话版的失败原因，直接打给用户看（默认脱敏） */
export function explainFailure(P, fullPaths = false) {
  const L = [];
  L.push('');
  L.push('找不到 RoxyBrowser 的运行环境。');
  L.push('');
  if (!P.dataDir) {
    if (P.diag.dataDirExplicitFailed) {
      L.push('  ✗ 数据目录：你显式指定的路径无效');
      for (const c of P.diag.dataDirCandidates ?? []) L.push(`      [无效] ${show(c.path, fullPaths)}`);
      L.push('    判定标准：目录里要有 chrome-bin\\ 或 browser-cache\\');
      L.push('    （已显式指定，故不会自动回退到其它目录）');
    } else {
      L.push('  ✗ 数据目录：没找到');
      L.push('    已尝试这些位置：');
      for (const c of P.diag.dataDirCandidates ?? []) L.push(`      ${c.ok ? '[有]' : '[无]'} ${show(c.path, fullPaths)}`);
      L.push('    判定标准：目录里要有 chrome-bin\\ 或 browser-cache\\');
      L.push('    解决：用 --data-dir 指定，或设环境变量 ROXY_HOME');
    }
  } else {
    L.push(`  ✓ 数据目录：${show(P.dataDir, fullPaths)}`);
    L.push(`  ✗ 内核：${show(P.diag.coreProblem, fullPaths) ?? '未找到'}`);
    L.push('    RoxyChrome.exe 是官方 App 自己下载的，脚本不会生成它。');
    L.push('    解决：在这台机器上安装并运行一次 RoxyBrowser，');
    L.push('          登录后在界面里打开任意一个窗口，让它把内核下载下来。');
    L.push('          或者从别的机器把 chrome-bin\\ 整个目录复制到：');
    L.push(`            ${show(path.join(P.dataDir, 'chrome-bin'), fullPaths)}`);
  }
  L.push('');
  if (!P.installDir) {
    L.push('  ⚠ 安装目录：没找到（不致命，只影响官方扩展与 blockDomain 拦截页）');
  } else {
    L.push(`  ✓ 安装目录：${show(P.installDir, fullPaths)}`);
  }
  L.push('');
  if (!fullPaths) L.push('  （路径已脱敏；要看真实路径加 --full-paths）');
  L.push('');
  return L.join('\n');
}
