// ============================================================
//  mkprofile.mjs  —  离线档案生成器（命令行版）
//
//  现在是 fingerprint.mjs 的薄 CLI 外壳：不再自带一套重复的加密、
//  路径和随机化实现，全部复用共享模块，因此路径自动发现同样生效。
//
//  用法
//    node mkprofile.mjs --count 3
//    node mkprofile.mjs --name bot --locale vi-VN --proxy "socks5://u:p@host:1080"
//    node mkprofile.mjs --screen 1920x1080 --os "Windows 11"
//    node mkprofile.mjs --from <dirId>          # 用指定档案当结构模板
//    node mkprofile.mjs --list                  # 只列出当前环境解析结果
// ============================================================
import fs from 'node:fs';
import path from 'node:path';
import {
  getPaths, pathHelp, show, createProfileOnDisk, newestTemplateDir,
  LOCALE_PRESETS, SCREENS, WINDOWS_PROFILES,
} from './fingerprint.mjs';

const argv = process.argv.slice(2);
const argOf = (n, d = null) => { const i = argv.indexOf('--' + n); return i === -1 ? d : argv[i + 1]; };
const has = (n) => argv.includes('--' + n);

const FULL_PATHS = has('full-paths');   // 默认脱敏显示路径
const P = getPaths({ dataDir: argOf('data-dir'), installDir: argOf('install-dir') });
const s = (p) => show(p, FULL_PATHS);

// ---------- 环境自检 ----------
if (!P.ok) {
  console.error(pathHelp(undefined, FULL_PATHS));
  console.error('提示：可用 --data-dir <路径> 或环境变量 ROXY_HOME 手动指定数据目录。\n');
  process.exit(2);
}

if (has('list')) {
  console.log(`数据目录  : ${s(P.dataDir)}`);
  console.log(`安装目录  : ${s(P.installDir) ?? '(未找到)'}`);
  console.log(`内核      : ${s(P.coreExe)}  (v${P.coreVersion})`);
  console.log(`chromedriver: ${s(P.chromedriver) ?? '(未找到)'}`);
  console.log(`档案目录  : ${s(P.browserCacheDir)}`);
  const n = fs.existsSync(P.browserCacheDir) ? fs.readdirSync(P.browserCacheDir, { withFileTypes: true }).filter((e) => e.isDirectory()).length : 0;
  console.log(`现有档案  : ${n} 个`);
  console.log(`模板档案  : ${newestTemplateDir() ?? '(无，将使用内置骨架)'}`);
  console.log(`可用语言  : ${Object.keys(LOCALE_PRESETS).join(', ')}`);
  console.log(`可用分辨率: ${SCREENS.map((s) => `${s.width}x${s.height}`).filter((v, i, a) => a.indexOf(v) === i).join(', ')}`);
  console.log(`可用 OS   : ${WINDOWS_PROFILES.map((w) => w.name).join(', ')}`);
  process.exit(0);
}

// ---------- 参数 ----------
const COUNT = parseInt(argOf('count', '1'), 10);
const NAME = argOf('name');
const PROXY = argOf('proxy');
const FROM = argOf('from');
const LOCALE = argOf('locale');
const TZ = argOf('timeZone', argOf('tz'));
const OS = argOf('os');
const screenArg = argOf('screen');
let screen = null;
if (screenArg) {
  if (/^\d+x\d+$/.test(screenArg)) screen = screenArg.split('x').map(Number);
  else { console.error(`--screen 格式应为 WxH，例如 1920x1080（收到 "${screenArg}"）`); process.exit(2); }
}

console.log(`[env] 数据目录 ${s(P.dataDir)}`);
console.log(`[env] 内核 v${P.coreVersion}  ${s(P.coreExe)}`);
console.log(`[env] 模板 ${FROM ?? newestTemplateDir() ?? '(内置骨架)'}`);

// ---------- 生成 ----------
const created = [];
for (let i = 0; i < COUNT; i++) {
  const built = createProfileOnDisk({
    from: FROM ?? undefined,
    windowName: NAME ? (COUNT > 1 ? `${NAME}-${i + 1}` : NAME) : undefined,
    proxy: PROXY ?? undefined,
    locale: LOCALE ?? undefined,
    timeZone: TZ ?? undefined,
    os: OS ?? undefined,
    screen,
    portScanWhiteList: argOf('portScanWhiteList') ?? undefined,
  });
  created.push({
    dirId: built.dirId,
    userDataDir: built.userDataDir,
    windowName: built.cfg.windowName,
    locale: built.locale,
    timeZone: built.timeZone,
    screen: `${built.screen.width}x${built.screen.height}`,
    os: built.os,
    proxy: built.cfg.fproxy ? `${built.cfg.fproxy.type}://${built.cfg.fproxy.host}:${built.cfg.fproxy.port}` : 'direct',
    templateFrom: built.templateFrom,
  });
}

// 写在脚本旁边，roxy-newprofile.ps1 从同目录读
const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
fs.writeFileSync(path.join(HERE, 'last-created.json'), JSON.stringify(created, null, 2));

console.log(`\n[created] ${created.length} 个档案 -> ${s(P.browserCacheDir)}\n`);
for (const c of created) {
  console.log(`  dirId      ${c.dirId}`);
  console.log(`  windowName ${c.windowName}   locale=${c.locale ?? '-'}  tz=${c.timeZone ?? '-'}  screen=${c.screen}  os=${c.os}`);
  console.log(`  proxy      ${c.proxy}`);
  console.log('');
}
console.log('[note] 这些档案只存在于磁盘上，不占用账号窗口额度。');
console.log(`[next] node scripts\\roxy-direct-launch.ps1  或  POST /browser/open {"dirId":"${created[0].dirId}"}`);
