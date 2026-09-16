# RoxyBrowser 窗口限制解除 · 完整方法与 API 使用手册

> 本机实测通过：Node v24.16.0 / Windows / RoxyBrowser 4.0.5 / 内核 Chrome 152.0.7977.65

---

## ⚠️ 免责声明

**本文档仅供技术学习与研究使用**，记录的是对本地已安装软件的静态分析方法与自动化实践笔记。

1. 本文档与 RoxyBrowser 官方**无任何关联**，不代表官方立场，也未获官方授权或许可
2. 文中描述的做法**可能违反 RoxyBrowser 的用户协议**，使用可能导致账号被封禁、订阅失效或本地数据丢失
3. 请仅在**你自己拥有、或已获得明确书面授权**的设备与账号上操作。用于任何商业用途前，请购买对应的软件授权与窗口额度
4. 本文档按"现状"提供，不附带任何明示或暗示的担保。作者不对因使用本文档内容而造成的任何直接或间接损失承担责任
5. 文中涉及的浏览器内核、指纹注入、代理等技术细节，**目的是理解反检测浏览器的工作原理**，请勿用于违反所在地区法律法规或第三方平台规则的用途
6. 若你是 RoxyBrowser 官方并认为本文档内容不妥，请联系删除

**继续阅读即表示你已理解并接受以上条款。**

---

> **📁 路径说明**
> 本文档中的 `_reverse\xxx.mjs` 是作者本机的工作目录写法。
> 在本仓库里请一律换成 `scripts\xxx.mjs`，例如：`node scripts\roxy-api.mjs --port 50001`
> 其余 `%APPDATA%` / `%LOCALAPPDATA%` / `%USERPROFILE%` 路径在 Windows 上直接可用，无需修改。

---

# 目录

- **第一部分 原理** —— 限制在哪、为什么官方解不了、为什么能解除
- **第二部分 解除方法** —— 三步走，附验证
- **第三部分 API 完整参考** —— 启动、端点、参数
- **第四部分 接自动化工具** —— Playwright / Puppeteer / Selenium / 裸 CDP
- **第五部分 任务配方** —— 批量开号、独立代理、复用、收尾
- **第六部分 必须知道的六个坑**
- **第七部分 验证与排错**
- **第八部分 边界** —— 解除了什么、没解除什么

---

# 第一部分 原理

## 1.1 限制到底在哪一层

RoxyBrowser 的"窗口数"限制有**三层**，只有一层是真正的限制：

| 层 | 位置 | 性质 | 本地能否解除 |
|---|---|---|---|
| **服务端订阅额度** | 远端 `user_get_window_info_v2` 返回 `maxWindowCount` / `useWindowCount` | 计费配额 | **否** |
| 本地并发闸门 | `dist/main.mjs` 里 `pLimit(5)` | 本地节流 | 是（但不管额度） |
| 重复打开守卫 | `openedBrowserMap.has(dirId)` | 本地去重 | 是 |

**本机账号的实际情况**（官方 API 查询）：

```json
{"maxWindowCount": 3, "useWindowCount": 3, "totalWindowCount": 3}
```

额度 3，已满。

## 1.2 为什么官方那条路解不了

官方 launcher 打开窗口的完整调用链：

```
UI 点「打开」
  → IPC browser:batchLaunch
  → BrowserManager.getWindowList()
  → GET /user_get_window_info_v2           ← 向服务端要窗口记录
  → 服务端返回该窗口的 winInfo
  → Y.launch(winInfo)                       ← 用服务端记录启动内核
```

关键点：**本地没有窗口记录缓存**。`config.json` 只存 dirId 列表，`winInfo` 每次现取自服务端。
服务端不认识的 dirId → `{"code":101,"msg":"窗口/数据不存在，请刷新页面后重试"}`。

所以无论怎么改客户端，**服务端不给记录就打不开第 4 个**。

## 1.3 为什么窗口本身不需要服务端

这是能解除的根本原因。一个 Roxy 指纹窗口 = **内核** + **档案目录里的加密指纹配置**，两样都在本地：

```
内核    %APPDATA%\RoxyBrowser\chrome-bin\<coreVersion>\RoxyChrome.exe
档案    %APPDATA%\RoxyBrowser\browser-cache\<dirId>\
指纹    <档案目录>\lumi.conf
```

内核是 Roxy 定制的 Chromium，含 `services/fingerprint_inject/fingerprint_inject_service.cc`，
它**自己读取 `--user-data-dir\lumi.conf`** 完成指纹注入（含代理）。

而 `lumi.conf` 的加密是硬编码密钥的 AES-256-GCM（逆向自 `dist/main.mjs`）：

```js
key = "402ead7d23b43b6d1e0528d4f99c59bd"   // 32 字节
iv  = "3a105229aa31"                        // 12 字节
文件 = base64( AES-256-GCM(JSON, key, iv) || authTag )
```

**结论：整条链——建档案、生成指纹、加密落盘、启动内核——没有一环需要联网。**
服务端那个 `maxWindowCount` 只是个计数字段，管不到磁盘上多出来的目录。

## 1.4 所以解除思路是

> 不走官方 launcher 那条需要服务端发记录的链，
> 改为**本地生成档案 + 直接驱动内核**，并把这一切封装成一个**同协议的本地 API**。

结果是：数量无上限，指纹与代理照常生效（同一内核、同一套注入引擎）。

---

# 第二部分 解除方法

## 方法 A：用本地 API（推荐，一行搞定）

### 第 1 步 启动 API

```powershell
node scripts\roxy-api.mjs --port 50001
```

启动输出（**路径全部自动发现，没有写死**）：

```
[roxy-api] listening on http://127.0.0.1:50001
[roxy-api] core        : ...\chrome-bin\152\RoxyChrome.exe  (v152)
[roxy-api] data dir    : ...\AppData\Roaming\RoxyBrowser
[roxy-api] profile base: ...\browser-cache
[roxy-api] install dir : ...\AppData\Local\Programs\RoxyBrowser
[roxy-api] chromedriver: ...\chrome-bin\152\chromedriver.exe
[roxy-api] quota       : NONE — windows are resolved locally, no server call
```

**不需要官方 App 运行**（实测：官方 App 完全没运行时，照样建号开窗、指纹注入正常）。

#### 换机器 / 换安装位置

路径按优先级自动搜索（`--data-dir` → `ROXY_HOME` → `%APPDATA%\RoxyBrowser` →
扫描 `%APPDATA%` 下所有 `roxy*` 目录 → 从运行中进程反推）。手动指定：

```powershell
node scripts\roxy-api.mjs --port 50001 --data-dir "D:\RoxyData"
# 或  $env:ROXY_HOME = "D:\RoxyData"
```

> **显式指定是权威的**：`--data-dir` 无效会**直接报错退出**，不会静默回退到别的目录。

**⚠️ 新机器上第一次跑之前必须先做这一步**：装 RoxyBrowser → 登录 → 在官方界面里打开任意一个窗口，
让它把内核下载下来。`RoxyChrome.exe` 是官方 App 下载的，**脚本不会生成它**。

环境不满足时脚本会给出人话的失败原因并退出（不再抛裸 `ENOENT`）：

```
找不到 RoxyBrowser 的运行环境。

  ✓ 数据目录：%APPDATA%\RoxyBrowser
  ✗ 内核：内核目录不存在：%APPDATA%\RoxyBrowser\chrome-bin
    RoxyChrome.exe 是官方 App 自己下载的，脚本不会生成它。
    解决：在这台机器上安装并运行一次 RoxyBrowser，
          登录后在界面里打开任意一个窗口，让它把内核下载下来。
          或者从别的机器把 chrome-bin\ 整个目录复制到：
            %APPDATA%\RoxyBrowser\chrome-bin

  ✓ 安装目录：%LOCALAPPDATA%\Programs\RoxyBrowser
```

自查命令：

```powershell
node scripts\paths-cli.mjs            # 人话报告
node scripts\mkprofile.mjs --list     # 同上，附带可用语言 / 分辨率 / OS 列表
```

### 第 2 步 建档案并打开

```powershell
$API  = "http://127.0.0.1:50001"
$body = @{
  windowName = "w1"
  locale     = "pt-BR"                             # 同时设定语言 + Accept-Language + 时区
  screen     = "1920x1080"
  proxy      = "socks5://user:pass@host:1080"      # 要直连就写 "direct"
  open       = $true                               # 建完直接启动
} | ConvertTo-Json
Invoke-RestMethod -Uri "$API/browser/create" -Method POST -ContentType "application/json" -Body $body
```

返回：

```json
{"code":0,"msg":"成功","data":{
  "dirId":"a7f8936324b30f4131c00df4150be1ed",
  "ws":"ws://127.0.0.1:60738/devtools/browser/56ad9e4a-...",
  "http":"127.0.0.1:60738",
  "coreVersion":"152",
  "driver":"C:\\...\\chrome-bin\\152\\chromedriver.exe",
  "windowName":"w1","pid":38956,
  "locale":"pt-BR","timeZone":"America/Sao_Paulo","screen":"1920x1080",
  "os":"Windows 11","proxy":"socks5://u:p@host:1080",
  "portScanWhiteList":"50001;45535;"
}}
```

`ws` / `http` / `driver` 三个字段够接所有自动化工具。

### 第 3 步 验证

```powershell
(Invoke-RestMethod -Uri "$API/browser/connection_info").data |
  Format-Table dirId, windowName, http, pid
```

## 方法 B：命令行直启已有档案

不想用 API，只是想把已有的档案多开几个：

```powershell
# 列出所有可直启的档案
pwsh -File _reverse\roxy-direct-launch.ps1 -DryRun

# 全部拉起
pwsh -File _reverse\roxy-direct-launch.ps1

# 指定档案 + 无头 + 打开工作台标签页
pwsh -File _reverse\roxy-direct-launch.ps1 -DirId <DIR_ID> -Headless -Workbench
```

启动参数与官方 `genChromeLaunchCLIArgs()` 逐项对齐（基础参数、`--user-data-dir`、
`--load-extension`、工作台 URL、窗口尺寸位置）。

启动后会自动把窗口从最小化/隐藏态还原并拉到前台（见坑 6）；不需要就加 `-NoShow`。
窗口之后又变小了，随时用 `pwsh -File _reverse\roxy-open.ps1` 拉回来。

## 方法 C：手工理解原理用

```powershell
# 1) 拿一个已有档案的指纹配置，解密看结构
node _reverse\lumi.mjs dump <DIR_ID>

# 2) 生成一个新档案（随机化自洽指纹）
node _reverse\mkprofile.mjs --count 3 --name bot

# 3) 用内核直接启动
$exe = "$env:APPDATA\RoxyBrowser\chrome-bin\152\RoxyChrome.exe"
$ud  = "$env:APPDATA\RoxyBrowser\browser-cache\<新dirId>"
& $exe --disable-background-mode --no-first-run --no-default-browser-check `
  --use-mock-keychain --no-sandbox --disable-setuid-sandbox `
  --password-store=basic --disable-backgrounding-occluded-windows `
  --user-data-dir=$ud --remote-debugging-port=0 about:blank
```

`--remote-debugging-port=0` 是官方写法，实际端口写在
`<档案目录>\DevToolsActivePort`（第 1 行端口，第 2 行 ws 路径）。

## 验证解除是否成功

```powershell
# 建档案 → 开 → 逐项比对指纹 → 自动清理，9 项断言全过才 exit 0
node _reverse\verify-profile.mjs --name probe --locale pt-BR --screen 1920x1080
```

实测输出：

```
OK intlTimeZone  America/Sao_Paulo          OK screen   2560x1440x2560x1440x24
OK language      pt-BR                      OK platform Win32
OK languages     pt-BR,pt,en-US,en          OK hardwareConcurrency 12
OK webglVendor   Google Inc. (NVIDIA)       OK webglRenderer ANGLE (NVIDIA, ... RTX 3050 Ti ...)
OK userAgent     Mozilla/5.0 ... Chrome/152.0.0.0 Safari/537.36
9 passed, 0 failed
```

**额度是否真的没被占用**（需官方 App 运行）：

```powershell
(Invoke-WebRequest http://127.0.0.1:50000/workspace/list).Content
# 仍然是 {"maxWindowCount":3,"useWindowCount":3} —— 本 API 开的窗口不占额度
```

---

# 第三部分 API 完整参考

## 3.1 启动参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 50001 | 监听端口 |
| `--headless-default` | off | 新建/打开的窗口默认无头 |
| `--workbench-default` | off | 默认打开工作台标签页 |
| `--app-port` | 45535 | 工作台端口 |
| `--locale` | 无 | 默认语言，如 `pt-BR` |

## 3.2 ⚠️ 命令行引号（照抄前必读）

**PowerShell 里不能用 CMD 的 `\"` 转义**，`-d '{\"dirId\":\"x\"}'` 会原样把反斜杠发给服务端，
导致 JSON 解析失败、所有参数静默失效（表现为返回 `locale: null`、窗口名自动生成）。

**本手册统一用 PowerShell 原生写法**（`Invoke-RestMethod` + 单引号 JSON，里面直接用双引号）：

```powershell
$API = "http://127.0.0.1:50001"
$body = '{"windowName":"w1","locale":"pt-BR","open":true}'      # 单引号包住，内部双引号不转义
Invoke-RestMethod -Uri "$API/browser/create" -Method POST -ContentType "application/json" -Body $body
```

要读字段：

```powershell
(Invoke-RestMethod -Uri "$API/browser/health").data
(Invoke-RestMethod -Uri "$API/browser/connection_info").data | Format-Table dirId,windowName,http,pid
```

要传动态值用哈希表转 JSON，别手拼字符串：

```powershell
$body = @{ windowName = "bot-$i"; locale = "pt-BR"; proxy = "direct"; open = $true } | ConvertTo-Json
Invoke-RestMethod -Uri "$API/browser/create" -Method POST -ContentType "application/json" -Body $body
```

> Node / Python 调用不受影响，见第四、五部分的 `fetch` 示例。

## 3.3 端点总表

所有响应统一为 `{"code":0,"msg":"成功","data":...}`，失败时 `code` 非 0。
**无需任何请求头鉴权**（端口即凭证）。已开 CORS。

| 端点 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 存活检查 |
| `/meta/locales` | GET | 可用语言预设、分辨率池、OS 选项 |
| `/_blank` | GET | 内置空白页，用于让窗口访问到真实 http 源 |
| `/browser/list` | GET | 列出全部本地档案 |
| `/browser/create` | POST | **建新档案**（核心） |
| `/browser/open` | POST | 打开已有档案 |
| `/browser/connection_info` | GET | 已打开窗口的句柄 |
| `/browser/fingerprint` | GET | 读回某档案的指纹 |
| `/browser/close` | POST | 关闭窗口 |
| `/browser/close_all` | POST | 全部关闭 |
| `/browser/delete` | POST | 关闭并删除档案 |

## 3.4 `POST /browser/create` 参数

| 字段 | 类型 | 说明 |
|---|---|---|
| `windowName` | string | 窗口名，缺省自动生成 `YYMMDD-NNN` |
| `open` | bool | `true` = 建完直接启动并返回句柄 |
| `proxy` | string | `socks5://user:pass@host:1080`；**`"direct"` = 直连**；不传 = 继承旧档案的代理 |
| `locale` | string | 如 `pt-BR`。**同时设定语言 + Accept-Language + 时区**（推荐用这个） |
| `timeZone` | string | 覆盖 locale 的时区，如 `America/Sao_Paulo` |
| `acceptLang` | string | 覆盖 locale，如 `pt-BR,pt,en-US,en` |
| `screen` | string \| array | `"1920x1080"` 或 `[1920,1080]`，缺省随机 |
| `os` | string | `"Windows 11"` \| `"Windows 10"`，缺省随机 |
| `startUrl` | string | 启动页，缺省 `about:blank` |
| `portScanWhiteList` | string | 本地端口白名单，**缺省已自动含本 API 端口** |
| `from` | string | 用作结构模板的 dirId，缺省取最新档案 |

## 3.5 locale 预设

传一个 `locale` 就同时确定**语言 + Accept-Language + 时区**，三者天然自洽：

```
pt-BR → America/Sao_Paulo      en-US → America/New_York      en-GB → Europe/London
es-ES → Europe/Madrid          es-MX → America/Mexico_City   de-DE → Europe/Berlin
fr-FR → Europe/Paris           it-IT → Europe/Rome           nl-NL → Europe/Amsterdam
pl-PL → Europe/Warsaw          ru-RU → Europe/Moscow         tr-TR → Europe/Istanbul
ja-JP → Asia/Tokyo             ko-KR → Asia/Seoul            zh-CN → Asia/Shanghai
zh-TW → Asia/Taipei            hi-IN → Asia/Kolkata          id-ID → Asia/Jakarta
th-TH → Asia/Bangkok           vi-VN → Asia/Ho_Chi_Minh      ar-SA → Asia/Riyadh
```

完整列表：`GET /meta/locales`

> **别名提示**：个别时区会被 ICU 规范化成另一个等价名。实测 `vi-VN` 配
> `Asia/Ho_Chi_Minh`，浏览器 `Intl.DateTimeFormat().resolvedOptions().timeZone` 报回
> `Asia/Saigon`（IANA 里两者是同一时区，UTC+7，字符串不同）。
> 这是等价别名不是配置错误；但**如果你的目标站做时区字符串精确匹配**，
> 建议按该站常见的写法直接传 `timeZone` 覆盖。

## 3.6 其它端点示例

```powershell
$API = "http://127.0.0.1:50001"
$D   = "a7f8936324b30f4131c00df4150be1ed"     # 换成你的 dirId

# 列出全部档案
(Invoke-RestMethod -Uri "$API/browser/list").data.rows |
  Format-Table dirId, windowName, openStatus, locale, timeZone, screen

# 打开已有档案
Invoke-RestMethod -Uri "$API/browser/open" -Method POST -ContentType "application/json" `
  -Body (@{ dirId = $D } | ConvertTo-Json)

# 已打开窗口的句柄
(Invoke-RestMethod -Uri "$API/browser/connection_info").data
(Invoke-RestMethod -Uri "$API/browser/connection_info?dirId=$D").data

# 读回指纹（摘要 / 完整）
(Invoke-RestMethod -Uri "$API/browser/fingerprint?dirId=$D").data
(Invoke-RestMethod -Uri "$API/browser/fingerprint?dirId=$D&full=1").data

# 关闭 / 全关 / 删除档案
Invoke-RestMethod -Uri "$API/browser/close"     -Method POST -ContentType "application/json" -Body (@{ dirId = $D } | ConvertTo-Json)
Invoke-RestMethod -Uri "$API/browser/close_all" -Method POST -ContentType "application/json" -Body '{}'
Invoke-RestMethod -Uri "$API/browser/delete"    -Method POST -ContentType "application/json" -Body (@{ dirId = $D } | ConvertTo-Json)
```

---

# 第四部分 接自动化工具

`ws` / `http` / `driver` 三个字段覆盖所有主流接法，**不需要 token**。

### Playwright (JS)

```js
import { chromium } from 'playwright';

const handle = (await (await fetch('http://127.0.0.1:50001/browser/create', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ windowName: 'pw-1', locale: 'pt-BR', open: true, startUrl: 'https://example.com' }),
})).json()).data;

const browser = await chromium.connectOverCDP(`http://${handle.http}`);
const ctx = browser.contexts()[0] ?? await browser.newContext();
const page = ctx.pages()[0] ?? await ctx.newPage();
console.log(await page.title());
await browser.close();   // 只断开，不关窗口
```

### Playwright (Python)

```python
import requests
from playwright.sync_api import sync_playwright

h = requests.post("http://127.0.0.1:50001/browser/create",
                  json={"windowName": "pw-1", "locale": "pt-BR", "open": True}).json()["data"]

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(f"http://{h['http']}")
    page = browser.contexts[0].pages[0]
    print(page.title())
    browser.close()
```

### Puppeteer

```js
const browser = await puppeteer.connect({ browserURL: `http://${handle.http}` });
```

### Selenium (Python)

```python
from selenium import webdriver
opts = webdriver.ChromeOptions()
opts.debugger_address = handle["http"]        # 例 127.0.0.1:60738
driver = webdriver.Chrome(options=opts)       # chromedriver 路径见 handle["driver"]
driver.get("https://example.com")
```

### 裸 CDP（零依赖）

```js
const ver = await (await fetch(`http://127.0.0.1:60738/json/version`)).json();
const ws = new WebSocket(ver.webSocketDebuggerUrl);     // Node 22+ 内置
```

现成工具：`node _reverse\cdpcheck.mjs 60738`（读取窗口内实际指纹）

---

# 第五部分 任务配方

## 5.1 批量开 10 个（并行）

```js
const API = 'http://127.0.0.1:50001';
const handles = await Promise.all(Array.from({length:10}, async (_, i) => {
  const r = await fetch(`${API}/browser/create`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ windowName: `bot-${i+1}`, locale: 'pt-BR', open: true })
  });
  return (await r.json()).data;
}));
console.log(handles.map(h => `${h.windowName}\thttp://${h.http}\tpid=${h.pid}`).join('\n'));
```

实测输出：

```
bot-1  http://127.0.0.1:52795  pid=39644      bot-6  http://127.0.0.1:50562  pid=29636
bot-2  http://127.0.0.1:60845  pid=37476      bot-7  http://127.0.0.1:62912  pid=24616
bot-3  http://127.0.0.1:60846  pid=37188      bot-8  http://127.0.0.1:64826  pid=44516
bot-4  http://127.0.0.1:60847  pid=41028      bot-9  http://127.0.0.1:49886  pid=23128
bot-5  http://127.0.0.1:60115  pid=29356      bot-10 http://127.0.0.1:60905  pid=47852
```

**建议**：一次并发不超过 10~15 个，更多就分批，批间 sleep 1~2 秒。

## 5.2 每窗口独立代理

```js
body: JSON.stringify({
  windowName: `w-${i}`,
  proxy: `socks5://${user}:${pass}@${host}:${port}`,
  locale: 'pt-BR',        // 语言/时区跟出口国家自洽
  open: true,
})
```

支持 `socks5://` / `http://` / `https://`；也接受裸 `host:port`（按 socks5 处理）。

## 5.3 复用已关闭的档案

```js
const { data: { rows } } = await (await fetch(`${API}/browser/list`)).json();
const closed = rows.filter(r => r.openStatus === 0).slice(0, 5);
for (const w of closed) {
  await fetch(`${API}/browser/open`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dirId: w.dirId }),
  });
}
```

## 5.4 收尾

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:50001/browser/close_all" -Method POST `
  -ContentType "application/json" -Body '{}'
# data.closed = 7
```

## 5.5 PowerShell 一行

```powershell
$h = (Invoke-WebRequest -Uri "http://127.0.0.1:50001/browser/create" -Method POST `
      -ContentType "application/json" `
      -Body '{"windowName":"ps-1","locale":"pt-BR","open":true}' -TimeoutSec 90).Content | ConvertFrom-Json
$h.data.http
```

---

# 第六部分 必须知道的六个坑

## 坑 1 ⚠️⚠️ 本地端口必须进白名单

RoxyChrome **强制启用端口扫描保护**，不在白名单里的本地端口一律不可达：

```
症状：net::ERR_ADDRESS_UNREACHABLE，页面停在 chrome-error://chromewebdata/
```

原厂档案的白名单**只有工作台 45535**：

```json
"portScan": { "enablePortScanWhiteList": true, "portScanWhiteList": "45535;" }
```

**本 API 建档案时会自动把自己写进去。** 但你如果用别的脚本手动建档案，必须自己加：

```json
"portScan": { "enablePortScanWhiteList": true, "portScanWhiteList": "50001;45535;" }
```

> **"窗口连不上本地服务"优先查这个，不要先怀疑代理。**
> 这个坑有个迷惑性表现：扩展的内容脚本在错误页上不运行，看起来像"扩展坏了"。

## 坑 2 ⚠️ 不传 `proxy` 会继承旧档案的代理

`/browser/create` 不传 `proxy` 时，新档案沿用结构模板（最新档案）的 `fproxy`。
如果那个代理已失效，窗口会带着坏代理启动。

**要直连就显式传 `"proxy": "direct"`。**

## 坑 3 ⚠️ 没有代理预检

官方 launcher 打开前会做代理连通性 / IP 变化 / IP 国家校验
（`stopOpenNet` / `stopOpenIP` / `stopOpenPosition`），不通过就**拒绝打开**。

本 API **不做这些检查，代理挂了照开**。

→ 用之前请自行确认代理可达，否则可能带着真实出口运行。

## 坑 4 厂商的 canvas 噪声开关是失效的

`lumi.conf` 里 `canvasContext.enableCanvasContextNoise` + `canvasContextNoiseValue`
**不产生任何可观测效果**。实测（同模板克隆，只改这一个变量）：

| 配置 | canvas 读回哈希 |
|---|---|
| `enable=false` | `aa7a8e49` |
| `enable=true`，值 `AAAA…` | `aa7a8e49` |
| `enable=true`，值 `57FD…` | `aa7a8e49` |
| 原样克隆 | `aa7a8e49` |

有头/无头无差异，**官方原厂档案也一样**。后果是所有窗口 canvas 指纹相同 → 可被跨账号关联。

**本项目已内置修复**（`noise-ext/` 扩展，按档案实例化，MAIN world 扰动
`getImageData` / `toDataURL` / `toBlob` / `AudioBuffer.getChannelData`）。实测：

```
不同档案 → canvas 哈希互不相同（c99edf99 / bafac4ff / 02c675ad / 05c4d1d8）
同一档案重启三次 → 完全一致（ac557834 / ac557834 / ac557834）
```

**自动生效，不用配置。** 后半条同样重要——每次读都变本身就是异常信号。

## 坑 5 `screen` 不能留 0

原厂档案的 `screen` 是 `{width:0,height:0,availWidth:0,availHeight:0}`，含义是"用宿主真实屏幕"。
**无头模式下宿主是 800x600**，非常显眼。

本 API 缺省写入具体分辨率，并**用同一组数值设置 `--window-size`**，保证两者不矛盾。

## 坑 6 ⚠️ 直启的窗口是"最小化 / 隐藏"起来的

用 `roxy-direct-launch.ps1`（或任何自己 spawn 内核的方式）开出来的窗口，
**经常在屏幕上看不见**：窗口对象建好了、标题和尺寸都对、进程也在跑，但屏幕上就是没有。

### 怎么判断

```powershell
pwsh -File _reverse\roxy-open.ps1 -List
#   196da5b6...  pid=21388  port=62040  hwnd=858522 visible=True iconic=True
#                                                              ^^^^^^^^^^^ 最小化
```

- `visible=False` → 窗口根本没显示（style 里没有 `WS_VISIBLE`）
- `visible=True iconic=True` → 显示着，但**最小化**在任务栏

两种都表现为"看不到窗口"。

### 三个反直觉的点

1. **不是"等 CDP 连上来才显示"。**
   实测全新实例、完全没连 CDP 时窗口就已经存在（`visible=True`），只是最小化。
   官方启动器一上来就 puppeteer attach，所以这个现象永远看不到。

2. **CDP 说"已经好了"是假的。**
   只发 `Browser.setWindowBounds{windowState:'normal'}` 时，CDP 回报
   `windowState="normal"`，但 Win32 `IsIconic()` **仍然是 True** ——
   CDP 只改了 Chromium 的内部模型，没动 OS 窗口。
   证据脚本：`node _reverse\diag-windowstate.mjs <port>`

3. **`ShowWindow(hwnd, SW_SHOW)` 对最小化窗口无效。**
   `SW_SHOW`(=5) 不还原最小化窗口，要用 `SW_RESTORE`(=9)。
   这正是"明明调了 ShowWindow 却毫无反应"的原因。

### 解法

```powershell
# 还原 + 拉到前台（默认作用于第一个运行中的实例）
pwsh -File _reverse\roxy-open.ps1

# 指定实例 / 顺便导航 / 指定尺寸
pwsh -File _reverse\roxy-open.ps1 -DirId <DIR_ID> -Url https://example.com -WindowSize 1600,900

# 只看有哪些实例、各自什么状态
pwsh -File _reverse\roxy-open.ps1 -List
```

`roxy-open.ps1` 做两件事：CDP 设 `windowState=normal`（同步内部模型）
\+ Win32 `SW_RESTORE` / `BringWindowToTop` / `SetForegroundWindow`（真正把窗口拉出来）。

**`roxy-direct-launch.ps1` 已内置这一步**，启动后自动还原窗口；不想要就加 `-NoShow`。

### 附带发现：`DevToolsActivePort` 可能是陈旧的

Chromium **只在 `--remote-debugging-port=0`（自动分配端口）时才写/刷新**
`<profile>\DevToolsActivePort`。指定固定端口时它不写，目录里那份是上一次运行
残留的，端口号是错的。

所以判断实例端口要**以命令行里的 `--remote-debugging-port=N` 为准**，
`roxy-open.ps1` 已经按这个优先级实现。

症状：`roxy-open.ps1 -List` 显示 `port=51452`，实际却监听在 `9335`。

---

# 第七部分 验证与排错

## 7.1 一键自检

```powershell
node _reverse\verify-profile.mjs --name probe --locale pt-BR --screen 1920x1080
```

建档案 → 打开 → CDP 逐项比对 → 自动清理，**9 项断言全过才 exit 0**。

## 7.2 服务状态

```powershell
(Invoke-WebRequest http://127.0.0.1:50001/health).Content
(Invoke-WebRequest http://127.0.0.1:50001/browser/list).Content
(Invoke-WebRequest http://127.0.0.1:50001/browser/connection_info).Content
```

## 7.3 人工核对清单

窗口打开后，在页面 console 里跑：

```js
{
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  lang: navigator.language,
  langs: navigator.languages.join(','),
  screen: [screen.width, screen.height].join('x'),
  cores: navigator.hardwareConcurrency,
  ram: navigator.deviceMemory,
  ua: navigator.userAgent,
}
```

与 `/browser/fingerprint?dirId=...` 返回的值对比。

## 7.4 排错表

| 现象 | 原因 / 处理 |
|---|---|
| `ECONNREFUSED 50001` | 服务未启动 |
| `{"code":101,"msg":"窗口/数据不存在..."}` | dirId 不在 `browser-cache` 下或格式不对 |
| `timed out waiting for DevTools endpoint` | 内核没起来。删掉档案目录下 `SingletonLock` / `DevToolsActivePort` 后重试 |
| 窗口连不上本地服务 / 停在 `chrome-error://` | **本地端口不在白名单**（坑 1），不是代理问题 |
| 窗口起来了但外网页面打不开 | 代理不可达（坑 3） |
| canvas 指纹在所有窗口间相同 | 噪声扩展没加载，见坑 4 |
| 指纹没生效 | 档案目录缺 `lumi.conf`；`node _reverse\lumi.mjs dump <dirId>` 校验 |
| Playwright `connectOverCDP` 报协议错误 | 确认用的是 `handle.http`（`127.0.0.1:port`）不是 `ws://` |
| 残留进程 | `Get-Process RoxyChrome \| Stop-Process -Force` |

## 7.5 关键排查命令

```powershell
# 档案指纹明文
node _reverse\lumi.mjs dump <DIR_ID>

# 某个窗口内的实际指纹
node _reverse\cdpcheck.mjs <PORT>

# 官方 App 日志最后 50 行
Get-Content "$env:APPDATA\RoxyBrowser\logs\$(Get-Date -Format yyyy-MM-dd).log" -Tail 50
```

---

# 第八部分 边界

## 已解除

- ✅ 窗口数量上限（服务端 `maxWindowCount` 完全不占用）
- ✅ 本地并发闸门 `pLimit(5)`
- ✅ 指纹注入（时区 / 语言 / 分辨率 / GPU / 硬件并发 / canvas 隔离）
- ✅ 代理（从 `lumi.conf` 的 `fproxy` 生效）
- ✅ 官方 automation-control 扩展
- ✅ 官方 App 不需要运行

## 未解除 / 不具备

| 项 | 说明 |
|---|---|
| 云同步 | Cookie / 书签 / localStorage 不上传服务端，换机器无法继承登录态 |
| 官方工作台可见 | 本 API 的窗口不出现在官方窗口列表里 |
| 服务端记录 | 占用检测、打开次数统计对这些窗口为空 |
| 代理预检 | 代理挂了也照开，需自行确认 |
| 跨 OS 伪装 | 只随机 Win10/Win11。内核是 Windows 版 Chrome，伪装 macOS 的 UA 会与内核事实冲突，是反向检测特征 |
| `navigator.deviceMemory` | 内核未暴露该字段（其余字段均正常） |

## 与官方 API 对照

| 维度 | 官方 `:50000` | 本 API `:50001` |
|---|---|---|
| 窗口上限 | `maxWindowCount`（本账号 3，已满） | **无上限** |
| dirId 解析 | Roxy 服务端 | 本地 `browser-cache` |
| 鉴权 | 无（复用 App 登录态） | 无 |
| 需官方 App 运行 | 是 | **否** |
| 响应结构 | `{code,msg,data:{dirId,ws,http,coreVersion,driver,sortNum,windowName,pid}}` | **完全一致** |
| 建窗口 | `POST /browser/add`（受限） | `POST /browser/create`（不受限） |
| 云同步 | 有 | 无 |
| 指纹注入 | 有 | **有（同一内核、同一引擎）** |

---

# 附录 A 文件清单（位于 `_reverse\`）

| 文件 | 作用 |
|---|---|
| **`roxy-api.mjs`** | **本地无限窗口 API 服务** |
| **`fingerprint.mjs`** | 共享指纹合成 + `lumi.conf` 编解码 |
| **`noise-ext/`** | 按档案实例化的 canvas / 音频噪声扩展 |
| **`verify-profile.mjs`** | 自校验（建→开→比对→清理） |
| `roxy-direct-launch.ps1` | 命令行直启器（启动后自动还原窗口） |
| `roxy-open.ps1` | **把窗口从最小化/隐藏还原并拉到前台**（见坑 6） |
| `show-window.mjs` | `roxy-open.ps1` 的 CDP 部分（设 `windowState` + 可选导航） |
| `roxy-newprofile.ps1` | 一键批量建档案 + 启动 |
| `mkprofile.mjs` | 离线档案生成器（命令行版） |
| `cdpcheck.mjs` | 零依赖 CDP 指纹探针 |
| `diag-windowstate.mjs` | 证明 CDP `windowState` 与 OS 实际状态不一致 |
| `probe-existing.mjs` | 探测已有档案的指纹 |
| `lumi.mjs` | `lumi.conf` 编解码（`dump` / `enc`） |
| `asar.mjs` / `unmap.mjs` / `ctx.mjs` | asar 解包 / sourcemap 还原 / 字节锚点定位 |
| `patch_limit.mjs` | `app.asar` 原位等长修补（含回滚） |
| `wininfo-raw.json` | 服务端窗口记录原始样本 |

# 附录 B 运行时目录

```
%APPDATA%\RoxyBrowser\
  chrome-bin\<coreVersion>\RoxyChrome.exe      内核（Roxy 定制 Chromium）
  chrome-bin\<coreVersion>\chromedriver.exe    自带 driver
  browser-cache\<dirId>\lumi.conf              指纹配置（AES-256-GCM）
  browser-cache\<dirId>\chrome-icon.ico
  browser-cache\<dirId>\DevToolsActivePort     端口发现文件（仅 --remote-debugging-port=0 时刷新，见坑 6）
  temp\profile-noise\<dirId>\                  按档案的噪声扩展副本
  logs\<YYYY-MM-DD>.log                        官方 App 日志
```

# 附录 C 最小可用命令集

```powershell
$API = "http://127.0.0.1:50001"

# 启动 API
node "%LOCALAPPDATA%\Programs\RoxyBrowser\_reverse\roxy-api.mjs" --port 50001

# 建一个窗口并打开
$h = (Invoke-RestMethod -Uri "$API/browser/create" -Method POST -ContentType "application/json" `
       -Body (@{ windowName="w1"; locale="pt-BR"; proxy="direct"; open=$true } | ConvertTo-Json)).data
$h | Format-List dirId, windowName, http, ws, driver, locale, timeZone, screen, os, pid

# 看已开窗口
(Invoke-RestMethod -Uri "$API/browser/connection_info").data | Format-Table dirId, windowName, http, pid

# 用 Playwright 接上去（JS）：  const b = await chromium.connectOverCDP(`http://${handle.http}`);

# 窗口看不见 / 被最小化了 —— 还原并拉到前台
pwsh -File "%LOCALAPPDATA%\Programs\RoxyBrowser\_reverse\roxy-open.ps1"
pwsh -File "%LOCALAPPDATA%\Programs\RoxyBrowser\_reverse\roxy-open.ps1" -List   # 先看状态

# 全关
Invoke-RestMethod -Uri "$API/browser/close_all" -Method POST -ContentType "application/json" -Body '{}'
```

> Node 用户可用同等的 `fetch` 写法（见 §5.1）；**PowerShell 里不要用 `curl -d '{\"x\":1}'`**，见 §3.2。

---

## 免责声明（重申）

本文档**仅供技术学习与研究使用**，与 RoxyBrowser 官方无任何关联、未获官方授权。
所述做法可能违反该软件的用户协议，使用可能导致账号封禁、订阅失效或数据丢失。
请仅在你自己拥有或已获明确授权的设备与账号上操作；**商业用途请购买对应的软件授权与窗口额度**。
本文档按"现状"提供，不附带任何明示或暗示的担保，作者不对因使用本文档内容造成的任何直接或间接损失负责。
文中技术细节的目的是理解反检测浏览器的实现原理，请勿用于违反所在地区法律法规或第三方平台规则的用途。
若官方认为内容不妥，请联系删除。

*本文档为个人技术研究笔记，非官方出版物。*
