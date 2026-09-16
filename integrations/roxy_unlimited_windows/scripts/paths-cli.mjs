// ============================================================
//  paths-cli.mjs  —  把路径解析结果吐给 PowerShell 脚本用
//
//    node paths-cli.mjs --json                 -> JSON（真实路径，供脚本使用）
//    node paths-cli.mjs                        -> 人话报告（路径已脱敏）
//    node paths-cli.mjs --full-paths           -> 人话报告，显示真实路径
//    node paths-cli.mjs --data-dir X --json
//
//  退出码：0 = 可用，2 = 环境不满足
//
//  说明：--json 始终返回真实路径（PowerShell 要拿它去操作文件）；
//        人话输出默认把 C:\Users\<你的用户名>\... 换成 %USERPROFILE%\...，
//        方便截图 / 贴日志 / 提 issue。
// ============================================================
import { resolveRoxyPaths, explainFailure, show } from './paths.mjs';

const a = process.argv.slice(2);
const argOf = (n, d = null) => { const i = a.indexOf('--' + n); return i === -1 ? d : a[i + 1]; };
const FULL = a.includes('--full-paths');

const P = resolveRoxyPaths({ dataDir: argOf('data-dir'), installDir: argOf('install-dir') });

if (a.includes('--json')) {
  process.stdout.write(JSON.stringify(P));
  process.exit(P.ok ? 0 : 2);
}

if (!P.ok) console.log(explainFailure(P, FULL));
else console.log('环境检查通过');

console.log(`  数据目录  : ${show(P.dataDir, FULL) ?? '(未找到)'}`);
console.log(`  安装目录  : ${show(P.installDir, FULL) ?? '(未找到)'}`);
console.log(`  内核      : ${show(P.coreExe, FULL) ?? '(未找到)'}${P.coreVersion ? `  (v${P.coreVersion})` : ''}`);
console.log(`  chromedriver: ${show(P.chromedriver, FULL) ?? '(未找到)'}`);
console.log(`  档案目录  : ${show(P.browserCacheDir, FULL) ?? '(未找到)'}`);
if (!FULL) console.log('\n（路径已脱敏，加 --full-paths 显示真实路径）');
process.exit(P.ok ? 0 : 2);
