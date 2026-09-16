# RoxyBrowser 无限窗口 API —— 交接文档

> **⚠️ 免责声明：本文档仅供技术学习与研究使用。** 与 RoxyBrowser 官方无任何关联、未获授权，
> 所述做法可能违反其用户协议，使用可能导致账号封禁或数据丢失。请仅在你自己拥有或已获明确授权的
> 设备与账号上操作；商业用途请购买对应授权与窗口额度。本文档按"现状"提供，不附带任何担保，
> 作者不承担因使用本文档内容造成的任何损失。继续阅读即表示接受以上条款。

> **读者**：另一个 AI / 自动化工程师
> **目的**：让你能直接驱动本机上任意数量的 RoxyBrowser 指纹窗口，不受账号额度限制
> **状态**：已在本机实测通过（node v24.16.0，Windows）

> **📁 路径说明**：文中 `_reverse\xxx.mjs` 是作者本机的工作目录写法，
> 在本仓库里请一律换成 `scripts\xxx.mjs`。`%APPDATA%` / `%LOCALAPPDATA%` 路径可直接用。

---

## 0. 给 AI 的快速摘要

```
本机有两个 HTTP API，都在 127.0.0.1：

  50000  = RoxyBrowser 官方 OpenAPI
           窗口由服务端解析 → 最多 3 个（账号额度 maxWindowCount=3，已满）
           每个 dirId 必须存在于服务端，否则返回 code 101

  50001  = 本项目提供的本地 OpenAPI（roxy-api.mjs）
           窗口由本地磁盘解析 → 数量无上限
           返回结构与官方 50000 完全一致

要驱动无限窗口 → 用 50001。端口即凭证，无需 token，无需 workspaceId。
```

启动 50001（若未运行）：

```powershell
node "%LOCALAPPDATA%\Programs\RoxyBrowser\_reverse\roxy-api.mjs" --port 50001
```

一键开一个可自动化的窗口并拿到 CDP 句柄：

```bash
curl -s -X POST http://127.0.0.1:50001/browser/create \
  -H "Content-Type: application/json" \
  -d '{"windowName":"w1","open":true,"startUrl":"about:blank"}'
```

---

## 1. 背景：为什么不能用官方 API

RoxyBrowser 官方本地 API 的发射链（源码 `dist/main.mjs`，已逆向确认）：

```
POST /browser/open {dirId}
  → BrowserManager.getWindowList()
  → GET /user_get_window_info_v2        ← 向 Roxy 服务端请求窗口记录
  → 服务端返回该窗口的 winInfo
  → Y.launch(winInfo)                    ← 用服务端记录启动内核
```

**关键**：本地没有窗口记录缓存。`config.json` 只存 dirId 列表，`winInfo` 每次现取自服务端。
所以服务端不认识的 dirId → `{"code":101,"msg":"窗口/数据不存在，请刷新页面后重试"}`。

账号窗口额度由服务端字段 `maxWindowCount` 判定，本地任何改动都不会改变它。本机当前：

```json
{"maxWindowCount": 3, "useWindowCount": 3, "totalWindowCount": 3}
```

**结论：官方 API 无法超过 3 个，且无本地绕过方式。** 因此本项目改为绕过 launcher，
直接驱动 Roxy 定制的 Chromium 内核，并提供一个**同协议的本地 API**。

---

## 2. 原理：指纹窗口是怎么起来的

Roxy 的窗口 = 「定制 Chromium 内核」+「档案目录里的加密指纹配置」。
两者都在本地，**启动过程中没有任何一步需要服务端**。

```
内核    %APPDATA%\RoxyBrowser\chrome-bin\<coreVersion>\RoxyChrome.exe
档案    %APPDATA%\RoxyBrowser\browser-cache\<dirId>\
指纹    <档案目录>\lumi.conf
```

`lumi.conf` 的加解密（逆向自 `dist/main.mjs` 的 `genFingerprintConfig/util.ts`）：

```js
// key/iv 全部硬编码
key = "402ead7d23b43b6d1e0528d4f99c59bd"   // 32 bytes, AES-256
iv  = "3a105229aa31"                        // 12 bytes, GCM nonce
file = base64( ciphertext || authTag(16) )  // authTag 拼在尾部
```

内核含 `services/fingerprint_inject/fingerprint_inject_service.cc`，**自己读取
`--user-data-dir\lumi.conf`** 完成指纹注入（含代理）。因此只要档案目录里有合法的
`lumi.conf`，直启内核就能得到与官方窗口完全一致的指纹环境。

---

## 3. 环境与前置条件

| 项 | 值 |
|---|---|
| OS | Windows |
| Node | ≥ 22（需内置 `fetch` / `WebSocket`；本机 v24.16.0） |
| 依赖 | **无**。全部脚本零第三方依赖 |
| RoxyBrowser 安装 | `%USERPROFILE%\AppData\Local\Programs\RoxyBrowser\` |
| 数据目录 | `%APPDATA%\RoxyBrowser\` |
| 官方 App 是否需要运行 | **不需要**，50001 独立于官方 App |

---

## 4. 启动本地 API

```powershell
# 有头窗口（可见，默认）
node _reverse\roxy-api.mjs --port 50001

# 无头窗口
node _reverse\roxy-api.mjs --port 50001 --headless-default

# 每个窗口额外打开本地工作台标签页
node _reverse\roxy-api.mjs --port 50001 --workbench-default
```

启动输出：

```
[roxy-api] listening on http://127.0.0.1:50001
[roxy-api] core        : %USERPROFILE%\AppData\Roaming\RoxyBrowser\chrome-bin\152\RoxyChrome.exe
[roxy-api] profile base: %USERPROFILE%\AppData\Roaming\RoxyBrowser\browser-cache
[roxy-api] quota       : NONE — windows are resolved locally, no server call
```

### 可选参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 50001 | 监听端口 |
| `--headless-default` | off | 新建/打开的窗口默认无头 |
| `--workbench-default` | off | 默认打开工作台标签页 |
| `--app-port` | 45535 | 工作台端口（仅 `--workbench-default` 时用） |

### 存活检查

```bash
curl -s http://127.0.0.1:50001/health
# {"code":0,"msg":"成功","data":"ok"}
```

---

## 5. API 参考

所有响应统一为 `{"code":0,"msg":"成功","data":...}`，失败时 `code` 非 0。
**无需任何请求头鉴权**（端口即凭证）。已开 CORS。

### 5.1 `GET /health`

```json
{"code":0,"msg":"成功","data":"ok"}
```

### 5.2 `GET /browser/list`

列出全部本地档案。

```json
{"code":0,"msg":"成功","data":{
  "rows":[
    {"dirId":"a7f8936324b30f4131c00df4150be1ed","windowName":"260902-1",
     "windowSortNum":1,"openStatus":0,"statusInfo":null,"proxyInfo":{...}}
  ],
  "total":9
}}
```

`openStatus`：`1` = 当前已打开，`0` = 未打开。

### 5.3 `POST /browser/create` —— 建新档案（核心）

创建全新指纹档案；可选立刻打开。

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `windowName` | string | 否 | 窗口名，缺省自动生成 `YYMMDD-NNN` |
| `proxy` | string | 否 | 形如 `socks5://user:pass@host:1080`；**缺省继承现有档案的代理**（见 §9.2） |
| `startUrl` | string | 否 | 打开后访问的地址，默认 `about:blank` |
| `open` | bool | 否 | `true` = 创建后立刻启动并返回句柄 |
| `headless` | bool | 否 | 覆盖服务默认 |
| `args` | string[] | 否 | 额外的内核命令行参数 |
| `locale` | string | 否 | 如 `pt-BR`。**同时设定** `appLocale` + `acceptLang` + `timeZone`（见 §5.9 预设表） |
| `timeZone` | string | 否 | 覆盖 locale 预设的 IANA 时区，如 `America/Sao_Paulo` |
| `acceptLang` | string | 否 | 覆盖 locale 预设，如 `pt-BR,pt,en-US,en` |
| `screen` | string \| number[] | 否 | `"1920x1080"` 或 `[1920,1080]`。缺省随机 |
| `os` | string | 否 | `"Windows 11"` \| `"Windows 10"`。缺省随机 |
| `portScanWhiteList` | string | 否 | 分号分隔的本地端口白名单。**缺省已自动含本 API 端口**，见 §9.4 |
| `from` | string | 否 | 用该 dirId 的档案作为结构模板（缺省取最新档案） |

```bash
curl -s -X POST http://127.0.0.1:50001/browser/create \
  -H "Content-Type: application/json" \
  -d '{"windowName":"w1","proxy":"socks5://user:pass@host:1080","open":true,"startUrl":"https://example.com"}'
```

`open:true` 时的真实响应（**已实测**）：

```json
{"code":0,"msg":"成功","data":{
  "dirId":"a7f8936324b30f4131c00df4150be1ed",
  "ws":"ws://127.0.0.1:60738/devtools/browser/56ad9e4a-7cb2-4c4d-bbd3-c76108811e22",
  "http":"127.0.0.1:60738",
  "coreVersion":"152",
  "driver":"%USERPROFILE%\\AppData\\Roaming\\RoxyBrowser\\chrome-bin\\152\\chromedriver.exe",
  "sortNum":1,
  "windowName":"api-1",
  "windowRemark":"",
  "pid":38956,
  "proxy":null,
  "startUrl":"about:blank"
}}
```

`open:false`（或不传）时 `data` 只含 `{dirId, windowName, proxy, startUrl}`。

### 5.4 `POST /browser/open`

打开已存在的本地档案。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `dirId` | string | **是** | 32 位十六进制档案 id |
| `startUrl` | string | 否 | 启动页 |
| `headless` | bool | 否 | 覆盖服务默认 |
| `workbench` | bool | 否 | 是否打开工作台标签页 |
| `args` | string[] | 否 | 额外内核参数 |
| `useGpu` | bool | 否 | 传 `false` 加 `--disable-gpu` |

```bash
curl -s -X POST http://127.0.0.1:50001/browser/open \
  -H "Content-Type: application/json" \
  -d '{"dirId":"a7f8936324b30f4131c00df4150be1ed"}'
```

响应同 §5.3 的 `open:true`。若档案不存在：`{"code":101,"msg":"窗口/数据不存在，请刷新页面后重试"}`。

**幂等**：同一 `dirId` 重复调用返回同一句柄，不会重复启动。

### 5.5 `GET /browser/connection_info?dirId=<dirId>`

不带 `dirId` = 返回所有已打开窗口；带上 = 只返回该窗口。`data` 是数组。

```bash
curl -s "http://127.0.0.1:50001/browser/connection_info"
```

```json
{"code":0,"msg":"成功","data":[
  {"dirId":"a7f8936324b30f4131c00df4150be1ed","ws":"ws://127.0.0.1:60738/devtools/browser/56ad9e4a-...",
   "http":"127.0.0.1:60738","coreVersion":"152","driver":"...chromedriver.exe",
   "sortNum":1,"windowName":"api-1","windowRemark":"","pid":38956}
]}
```

> ⚠️ 与官方不同：官方此端点是 **GET**（用 POST 会 404）。
> ⚠️ 官方 `/browser/close` 收 `dirId` **单数**，传 `dirIds` 数组会回 `dirId is required`。

### 5.6 `POST /browser/close`

```bash
curl -s -X POST http://127.0.0.1:50001/browser/close \
  -H "Content-Type: application/json" -d '{"dirId":"a7f89363..."}'
```

先通过 CDP 发 `Browser.close` 优雅关闭（让内核刷盘），超时后兜底 `taskkill /T /F`。
窗口未打开时返回 `{"code":101,"msg":"window is not open"}`。

### 5.7 `POST /browser/close_all`

```json
{"code":0,"msg":"成功","data":{"closed":3}}
```

### 5.9 其它端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/meta/locales` | GET | 返回全部 locale 预设、分辨率池、OS 选项 |
| `/browser/fingerprint?dirId=&full=1` | GET | 读回该档案的指纹。默认返回摘要；`full=1` 返回完整配置 |
| `/_blank` | GET | 本服务自带的空白页。**用于让窗口访问到一个真实 http 源**，见 §9.4 |

`/meta/locales` 的 locale 预设（每个值**同时给出**时区与 Accept-Language，保证三者自洽）：

```
pt-BR → America/Sao_Paulo     en-US → America/New_York      en-GB → Europe/London
es-ES → Europe/Madrid         es-MX → America/Mexico_City   de-DE → Europe/Berlin
fr-FR → Europe/Paris          it-IT → Europe/Rome           nl-NL → Europe/Amsterdam
pl-PL → Europe/Warsaw         ru-RU → Europe/Moscow         tr-TR → Europe/Istanbul
ja-JP → Asia/Tokyo            ko-KR → Asia/Seoul            zh-CN → Asia/Shanghai
zh-TW → Asia/Taipei           hi-IN → Asia/Kolkata          id-ID → Asia/Jakarta
th-TH → Asia/Bangkok          vi-VN → Asia/Ho_Chi_Minh      ar-SA → Asia/Riyadh
```

> **为什么用 locale 而不是 `--lang`：** `--lang` 只影响浏览器 UI 语言。真正决定
> `navigator.language` / `navigator.languages` / `Intl` 时区的是 `lumi.conf` 里的
> `appLocale` / `acceptLang` / `timeZone` 三个顶层键，由厂商自己的指纹注入器生效
> （与 canvas / WebGL 同一条通道）。实测写入后 `Intl.DateTimeFormat().resolvedOptions().timeZone`
> 与 `navigator.languages` 立即跟随变化，**不需要 `TZ` 环境变量**（Windows 上 Chromium 也不读它）。

### 5.8 `POST /browser/delete`

关闭并**删除档案目录**（不可恢复）。

```bash
curl -s -X POST http://127.0.0.1:50001/browser/delete \
  -H "Content-Type: application/json" -d '{"dirId":"a7f89363..."}'
```

---

## 6. 接入自动化工具

`ws` / `http` / `driver` 三个字段够用三种主流接法。**不需要 token。**

### Playwright (JS)

```js
import { chromium } from 'playwright';

const handle = await (await fetch('http://127.0.0.1:50001/browser/create', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ windowName: 'pw-1', open: true, startUrl: 'https://example.com' }),
})).json().then(r => r.data);

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
                  json={"windowName": "pw-1", "open": True,
                        "startUrl": "https://example.com"}).json()["data"]

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(f"http://{h['http']}")
    ctx = browser.contexts[0]
    page = ctx.pages[0]
    print(page.title())
    browser.close()
```

### Puppeteer

```js
const puppeteer = require('puppeteer');
const browser = await puppeteer.connect({ browserURL: `http://${handle.http}` });
```

### Selenium (Python)

```python
from selenium import webdriver
opts = webdriver.ChromeOptions()
opts.debugger_address = handle["http"]          # 例 127.0.0.1:60738
driver = webdriver.Chrome(options=opts)         # 需 chromedriver，路径见 handle["driver"]
driver.get("https://example.com")
```

### 裸 CDP（无依赖）

`http://127.0.0.1:<port>/json/version` 拿 `webSocketDebuggerUrl`，
`/json/list` 拿页面级 target。本项目 `_reverse/cdpcheck.mjs` 就是零依赖示范：

```bash
node _reverse\cdpcheck.mjs 60738
```

输出（实测）：

```json
{
  "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/152.0.0.0 Safari/537.36",
  "platform": "Win32",
  "hardwareConcurrency": 8,
  "maxTouchPoints": 0,
  "webglVendor": "Google Inc. (NVIDIA)",
  "webglRenderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
  "webdriver": false
}
```

---

## 7. 常用配方

### 7.1 批量开 N 个窗口（并行）

```js
const API = 'http://127.0.0.1:50001';
const N = 10;
const names = Array.from({ length: N }, (_, i) => `bot-${i + 1}`);

const handles = await Promise.all(names.map(async (windowName) => {
  const r = await fetch(`${API}/browser/create`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ windowName, open: true, startUrl: 'about:blank' }),
  });
  return (await r.json()).data;
}));

console.log(handles.map(h => `${h.windowName}\thttp://${h.http}\tpid=${h.pid}`).join('\n'));
```

> 建议：一次并发不超过 10~15 个，内核启动较吃内存。要更多就分批，每批之间 `sleep` 1~2 秒。

### 7.2 每窗口独立代理

```js
body: JSON.stringify({
  windowName: `w-${i}`,
  proxy: `socks5://${user}:${pass}@${host}:${port}`,
  open: true,
})
```

支持 `socks5://` / `http://` / `https://`；也接受裸 `host:port`（默认按 socks5 处理）。

### 7.3 复用已有关闭的窗口

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

### 7.4 收尾

```bash
curl -s -X POST http://127.0.0.1:50001/browser/close_all \
  -H "Content-Type: application/json" -d '{}'
# {"code":0,"msg":"成功","data":{"closed":3}}
```

### 7.5 PowerShell 一行

```powershell
$h = (Invoke-WebRequest -Uri "http://127.0.0.1:50001/browser/create" -Method POST `
      -ContentType "application/json" `
      -Body '{"windowName":"ps-1","open":true}' -TimeoutSec 90).Content | ConvertFrom-Json
$h.data.http   # 127.0.0.1:xxxxx
```

---

## 8. 文件与目录

### 8.1 交付脚本（全部位于 `_reverse\`，相对 RoxyBrowser 安装目录）

| 文件 | 作用 |
|---|---|
| **`roxy-api.mjs`** | **本地无限窗口 API 服务（本文档主体）** |
| `fingerprint.mjs` | **共享指纹合成 + `lumi.conf` 编解码**（roxy-api 与 mkprofile 共用同一套随机化） |
| `noise-ext/` | 按档案实例化的 canvas / 音频噪声扩展（见 §9.5） |
| `verify-profile.mjs` | 自校验：建档案 → 开 → CDP 逐项比对指纹 → 自动清理并打印 OK/FAIL 表 |
| `roxy-direct-launch.ps1` | 命令行直启器，参数与官方 `genChromeLaunchCLIArgs()` 对齐；**启动后自动还原窗口**（`-NoShow` 关闭） |
| **`roxy-open.ps1`** | **把窗口从最小化/隐藏还原并拉到前台**（CDP + Win32 两步，见 §14.6） |
| `show-window.mjs` | `roxy-open.ps1` 的 CDP 部分：设 `windowState` + 可选 `--url` 导航 |
| `diag-windowstate.mjs` | 证明 CDP `windowState` 与 OS 实际状态不一致（§14.6 复现脚本） |
| `roxy-newprofile.ps1` | 一键「批量建档案 + 启动 + 健康检查」 |
| `mkprofile.mjs` | 离线档案生成器（命令行版） |
| `cdpcheck.mjs` | 零依赖 CDP 探针，读窗口内实际指纹 |
| `probe-existing.mjs` | 探测任意已存在档案的指纹（对照组用） |
| `lumi.mjs` | `lumi.conf` 编解码器（`dump` / `enc`） |
| `asar.mjs` | asar 解包 / 列表 / 提取 |
| `unmap.mjs` | 从 sourcemap 还原可读源码 |
| `ctx.mjs` | 在压缩产物中按字节定位锚点 |
| `patch_limit.mjs` | `app.asar` 原位等长修补（并发闸门，含回滚） |
| `verify-doc.mjs` | 本文档 §7.1 / §7.3 / §5.5 / §7.4 配方的逐字验证脚本 |
| `wininfo-raw.json` | 服务端窗口记录原始样本（结构参考） |

### 8.2 运行时目录

```
%APPDATA%\RoxyBrowser\
  chrome-bin\<cfgVersion>\RoxyChrome.exe     ← 内核（Roxy 定制 Chromium）
  chrome-bin\<cfgVersion>\chromedriver.exe   ← 自带 driver，Selenium 用
  chrome-bin\<cfgVersion>\<fullVersion>\     ← 内核载荷（chrome.dll 等 332MB）
  browser-cache\<dirId>\lumi.conf            ← 指纹配置（加密）
  browser-cache\<dirId>\chrome-icon.ico
  browser-cache\<dirId>\DevToolsActivePort   ← 端口发现文件（内核写）
  logs\<YYYY-MM-DD>.log                      ← 官方 App 日志（排错用）
```

**端口发现机制**：本 API 与官方一致，用 `--remote-debugging-port=0` 启动，
内核把实际端口写进 `<档案目录>\DevToolsActivePort`（第 1 行端口，第 2 行 ws 路径）。
本 API 会在启动前删除该文件，轮询等待其出现，然后 `GET /json/version` 确认可连接。

---

## 9. 限制与注意事项

### 9.1 服务端额度完全不受影响

实测：通过 50001 开 3 个窗口后，官方额度仍为

```json
{"maxWindowCount": 3, "useWindowCount": 3}
```

即 50001 的窗口在 Roxy 账号体系里**不存在**。

### 9.2 ⚠️ 代理默认是继承的，务必显式指定

`/browser/create` 不传 `proxy` 时，新档案会**继承结构模板（最新现有档案）中的 `fproxy`**，
即沿用旧档案的代理地址。若那个代理已失效，窗口会带着一个坏代理启动。

**建议：除非确实要复用，一律显式传 `proxy`。**

### 9.3 没有代理预检

官方 launcher 在打开前会做代理连通性 / IP 变化 / IP 国家校验
（`stopOpenNet` / `stopOpenIP` / `stopOpenPosition`），不通过就**拒绝打开**。
本 API **不做这些检查**，代理挂了照开。

→ 用之前请自行确认代理可达，否则可能带着真实出口运行。

### 9.4 ⚠️⚠️ 本地端口必须进白名单，否则窗口连不到任何本地服务

`lumi.conf` 里有：

```json
"portScan": { "enablePortScanWhiteList": true, "portScanWhiteList": "45535;" }
```

RoxyChrome **强制启用端口扫描保护**：**任何不在白名单里的回环端口都会被拒绝**，
症状是 `net::ERR_ADDRESS_UNREACHABLE`，页面停在 `chrome-error://chromewebdata/`。

官方原厂档案的白名单里**只有工作台 45535**，所以默认情况下：

- 本 API（50001）在窗口内**不可达**
- 任何自建的本地自动化端点、本地 mock server、本地调试页**全都不可达**
- 扩展的内容脚本也不会在错误页上运行，看起来像"扩展坏了"

**本 API 新建档案时会自动把自身端口写进白名单**（`50001;45535;45535;`）。
如果你用 `roxy-direct-launch.ps1` 或 `mkprofile.mjs` 手动建档案，必须自己处理这一点：

```json
"portScan": { "enablePortScanWhiteList": true, "portScanWhiteList": "50001;45535;" }
```

> 排查任何"窗口连不上本地服务"的问题，先看这个白名单，再看代理。

### 9.5 ⚠️ 厂商自带的 canvas 噪声开关是**失效**的

`lumi.conf` 里 `canvasContext.enableCanvasContextNoise` 和 `canvasContextNoiseValue`
**不产生任何可观测效果**。实测（同模板克隆，只改这一个变量）：

| 配置 | canvas 读回哈希 |
|---|---|
| `enable=false` | `aa7a8e49` |
| `enable=true`，值 `AAAA…` | `aa7a8e49` |
| `enable=true`，值 `57FD…`（另一档案的值） | `aa7a8e49` |
| 原样克隆 | `aa7a8e49` |

有头 / 无头同样无差异。**官方原厂档案也一样**（`2e30e3ad` 与随机生成的档案哈希相同），
所以这不是本项目的缺陷，是内核这个开关没实现或没生效。

→ **后果**：若不做处理，所有窗口的 canvas 指纹是同一个，可被跨账号关联。

→ **本项目的处理**：`noise-ext/` 是一个按档案实例化的 MV3 扩展，在 MAIN world 对
`getImageData` / `toDataURL` / `toBlob` / `AudioBuffer.getChannelData` 做**确定性**扰动
（种子由该档案的 `canvasContextNoiseValue` + dirId 派生）。实测结果：

```
四个不同档案 -> canvas 哈希 14542ef9 / 18bf7cc2 / c8c46bef / af90d992   （互不相同）
同一档案重启三次 -> ac557834 / ac557834 / ac557834                        （完全一致）
```

即：**档案之间隔离，档案自身稳定**。后者很重要——每次读都变本身就是异常信号。

### 9.6 screen 必须是具体分辨率

原厂档案的 `screen` 是 `{width:0,height:0,availWidth:0,availHeight:0}`，
含义是"用宿主真实屏幕"。**无头模式下宿主是 800x600**，这会很明显。

本 API 缺省写入具体分辨率，并**用同一组数值设置 `--window-size`**，保证 `screen.*`
与窗口尺寸不矛盾。另外注入器会强制 `availWidth/availHeight == width/height`
（写任务栏留白会被静默忽略），所以配置里两者保持一致。

### 9.7 不参与云同步

官方窗口关闭时会把 Cookie / localStorage / 书签打包上传服务端
（`groupdata/workspace/<ws>/window/<dirId>/browser-cache.zip`）。
本 API 的窗口**纯本地**，换机器无法继承登录态。

### 9.8 不出现在官方工作台

官方 UI 的窗口列表来自服务端，看不到本地档案。这是「不受额度限制」的同一枚硬币的另一面。

### 9.9 其它

- 同一 `dirId` 同时只能开一个实例（Chromium 的 `--user-data-dir` 独占）
- `sortNum` 是本地枚举序号，不是服务端排序值
- 内核升级后 `chrome-bin\<coreVersion>` 变化，本 API 自动选版本号最大的可用内核
- **跨 OS 伪装不做**：只随机 Windows 10 / 11。内核本身是 Windows 版 Chrome，
  伪装成 macOS 的 UA 会与内核事实冲突，是反向的检测特征
- `navigator.deviceMemory` 未生效（内核未暴露该字段），其余字段均正常

---

## 10. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `ECONNREFUSED 50001` | 服务未启动 → 见 §4 |
| `{"code":101,"msg":"窗口/数据不存在..."}` | `dirId` 不在 `browser-cache` 下，或不是 32 位十六进制 |
| `timed out waiting for DevTools endpoint` | 内核没起来。检查档案目录权限、是否有残留进程占用 `--user-data-dir`；删掉该目录下的 `SingletonLock` / `DevToolsActivePort` 后重试 |
| 窗口连不上本地服务 / 页面停在 `chrome-error://` | **本地端口不在白名单**（见 §9.4），不是代理问题 |
| 窗口起来了但外网页面打不开 | 代理不可达（见 §9.3）。`lumi.conf` 的 `fproxy` 决定了窗口走哪个代理 |
| canvas 指纹在所有窗口间相同 | 厂商噪声开关失效（见 §9.5），需 `noise-ext` 扩展 |
| 指纹没生效 | 档案目录缺 `lumi.conf`。用 `node _reverse\lumi.mjs dump <dirId>` 校验能否解密 |
| Playwright `connectOverCDP` 报协议错误 | 确认用的是 `handle.http`（`127.0.0.1:port`）而不是 `ws://` |
| 想确认某窗口真实指纹 | `node _reverse\cdpcheck.mjs <port>` |
| 残留进程 | `Get-Process RoxyChrome \| Stop-Process -Force` |

### 关键排查命令

```powershell
# 服务存活
(Invoke-WebRequest http://127.0.0.1:50001/health).Content

# 全部本地档案
(Invoke-WebRequest http://127.0.0.1:50001/browser/list).Content

# 当前打开的窗口
(Invoke-WebRequest http://127.0.0.1:50001/browser/connection_info).Content

# 某个档案的指纹明文
node _reverse\lumi.mjs dump a7f8936324b30f4131c00df4150be1ed

# 自校验：建档案 → 开 → 逐项比对指纹 → 清理（9 项断言，全过才 exit 0）
node _reverse\verify-profile.mjs --name probe --locale pt-BR --screen 1920x1080

# 官方 App 日志最后 50 行
Get-Content "$env:APPDATA\RoxyBrowser\logs\$(Get-Date -Format yyyy-MM-dd).log" -Tail 50
```

---

## 11. 与官方 API 对照

| 维度 | 官方 `:50000` | 本 API `:50001` |
|---|---|---|
| 窗口上限 | `maxWindowCount`（本账号 3，已满） | **无上限** |
| dirId 解析 | Roxy 服务端 | 本地 `browser-cache` |
| 鉴权 | 本地服务复用 App 登录态，无需显式 token | 无需任何凭证 |
| 需官方 App 运行 | 是 | **否** |
| 响应结构 | `{code,msg,data:{dirId,ws,http,coreVersion,driver,sortNum,windowName,pid}}` | **完全相同** |
| `connection_info` | GET | GET |
| `close` 参数名 | `dirId`（单数） | `dirId`（单数） |
| 建窗口 | `POST /browser/add`（受额度限制） | `POST /browser/create`（不受限） |
| 云同步 | 有 | 无 |
| 代理预检 | 有 | 无 |
| 指纹注入 | 有 | **有（同一内核、同一引擎）** |
| 官方 automation-control 扩展 | 有 | 有（自动加载） |

---

## 12. 附：官方启动参数实录（供对齐参考）

从官方日志 `logs\YYYY-MM-DD.log` 实录的一次真实启动：

```
--disable-background-mode
--disable-popup-blocking
--no-first-run
--no-default-browser-check
--remote-debugging-port=0
--use-mock-keychain
--user-data-dir=%APPDATA%\RoxyBrowser\browser-cache\<dirId>
--no-sandbox
--disable-setuid-sandbox
--password-store=basic
--disable-backgrounding-occluded-windows
http://127.0.0.1:45535/dashboard.html?id=<dirId>&workspaceType=0
--load-extension=%APPDATA%\RoxyBrowser\temp\automation-control-extension\<hash>
--window-size=1000,1000
--window-position=0,0
https://chatgpt.com/
```

executablePath：`%APPDATA%\RoxyBrowser\chrome-bin\<cfgVersion>\RoxyChrome.exe`

本 API 采用同一套基础参数（除 `--window-size` / `--window-position` 交由内核默认，
工作台标签页由 `--workbench-default` 控制）。

---

## 13. 文档中已脱敏的值

以下值在本机真实存在，但**已从本文档移除**，需要时由本机读取：

| 项 | 获取方式 |
|---|---|
| 账号邮箱 / 团队名 | `GET http://127.0.0.1:50000/workspace/list` |
| `workspaceId` | 同上（官方 API 的部分调用需要） |
| 代理地址与账密 | `node _reverse\lumi.mjs dump <dirId>` 看 `fproxy` 字段 |

> 若你要把本文档转发给外部服务，请保持这些值脱敏。

---

## 14. 附录：已证伪 / 已修正的技术结论

本节记录一次外部评审提出的三点，以及逐条实测的结论。**如果你是基于那份评审改进本项目的，
请先读这里**——其中两条的机制判断是错的，按错误机制去改会引入新问题。

### 14.1 ❌ "`lumi.conf` 没有 timezone / language 键，所以时区改不掉"

**部分成立，但结论错误。**

- 事实：`timeZone` / `appLocale` / `acceptLang` **是** `lumi.conf` 的合法顶层键
- 同目录另一个真实档案 `2e30e3ad` 里就写着 `timeZone: "Asia/Tokyo"`、`appLocale: "ja-JP"`
- 被评审看到的那份原厂档案恰好缺这三个键 —— 因为生成函数里它们是 **getter**：

  ```js
  get timeZone() { if (e.isTimeZone && t.timezone) return t.timezone; if (e.timeZone) return e.timeZone.split(" ").at(-1); }
  get appLocale() { return e.isLanguageBaseIp ? Xi(t.country_code) : e.language; }
  get acceptLang() { ... }
  ```

  上游对应字段为空时 getter 返回 `undefined`，被 `JSON.stringify` 丢弃 → 键不存在。
  **这是模板的偶然缺失，不是 schema 不支持。**

- 实测：写入 `America/Sao_Paulo` / `pt-BR` / `pt-BR,pt` 后

  | | 写入值 | 浏览器内实测 |
  |---|---|---|
  | `Intl` 时区 | America/Sao_Paulo | **America/Sao_Paulo** |
  | 时区偏移 | — | **180 分钟（UTC-3）** |
  | `navigator.language` | pt-BR | **pt-BR** |
  | `navigator.languages` | pt-BR,pt | **pt-BR,pt** |

**因此 `env: { TZ: 'America/Sao_Paulo' }` 是不必要且更差的方案**：Windows 上 Chromium/ICU
基本不读 `TZ`，而写进 `lumi.conf` 走的是厂商自己的注入通道（与 canvas/WebGL 同一条），
是真正生效的那条。本项目按后者实现。

### 14.2 ❌ "语言要用 `--lang=pt-BR --accept-lang=pt-BR,pt` 改"

方向对（BR 出口配 en-US 确实不自洽），但**改的地方不对**。

- `--lang` 只影响浏览器 UI 语言，不改 `navigator.language`
- 真正生效的是 `lumi.conf` 的 `appLocale` / `acceptLang`
- 本项目实现为 **locale 预设**：传一个 `locale: "pt-BR"` 同时确定
  `appLocale` + `acceptLang` + `timeZone`，三者天然自洽，没法配错
- 内核命令行的 `--lang` / `--accept-lang` 仍会按 `lumi.conf` 的值一并下发，双保险

### 14.3 ✅ "指纹随机化是缩水版 / screen 全 0"

**成立，已修。**

- 把完整随机化抽成 `fingerprint.mjs`，`roxy-api.mjs` 与 `mkprofile.mjs` 共用同一套，
  不再各自维护一份
- `screen` 改为写入具体分辨率，并**用它设置 `--window-size`**（两者不会再矛盾）
- 随机化的字段：`computerName` / `macAddress` / `windowName` / `chromeVersion` / `userAgent`
  / `userAgentMetadata`(Win10/Win11) / `audioBuffer` / `canvasContext` / `clientRects`
  / `WebGL`(9 种 GPU) / `WebGPU` / `navigator.hardwareConcurrency` / `deviceMemory`
  / `screen`(8 种分辨率) / `taskBarIcon` / `timeZone` / `appLocale` / `acceptLang`

  **不做跨 OS 伪装**（只随机 Win10/Win11）：内核是 Windows 版 Chrome，
  伪装成 macOS 的 UA 与内核事实冲突，是反向检测特征。
- 注：`availWidth/availHeight` 被注入器强制等于 `width/height`，写任务栏留白会被静默忽略，
  配置里已按实际行为对齐

### 14.4 本轮额外发现的两个真问题

评审没提到的、但会实际导致故障的两点：

1. **`portScanProtect` 白名单**（见 §9.4）—— 原厂档案只白名单了工作台 45535，
   导致窗口内**任何其它本地端口都不可达**。这个是排查"窗口连不上本地服务"的第一现场。
2. **厂商 canvas 噪声开关失效**（见 §9.5）—— 导致所有窗口 canvas 指纹相同，
   是跨账号关联风险。已用 `noise-ext` 扩展补上，并验证了**档案间互异 + 档案内稳定**。

### 14.5 总结：三条评审的处理

| 评审条目 | 判定 | 处理 |
|---|---|---|
| 时区改不掉，要用 `TZ` 环境变量 | 机制错误（键存在，是模板缺失） | 写进 `lumi.conf`，实测生效 |
| 语言要用 `--lang` / `--accept-lang` | 改错位置 | 用 locale 预设写入 `appLocale`/`acceptLang`，命令行同步下发 |
| 随机化缩水 + screen 全 0 | 成立 | 抽出共享模块，screen 写具体值并驱动 `--window-size` |

### 14.6 ❌ "窗口看不见是因为内核在等 CDP 客户端 attach"

**结论：不成立。** 窗口启动时就已经是 `visible=True`，只是**最小化**。
官方启动器一上来就 puppeteer attach，所以这个现象在官方路径下永远观察不到。

实测（全新实例、完全未建立 CDP WS 会话）：

```
16b2e5fb...  pid=47016  hwnd=6950478  visible=True  iconic=True
```

跑 `show-window.mjs --attach-only`（只连接、不发任何改变窗口状态的命令）后状态**不变**。

**顺带证伪的第二个假设：** "CDP 的 `Browser.setWindowBounds` 能把窗口还原"也不成立 ——

```
CDP:   AFTER : {"left":-37,"top":145,...,"windowState":"normal"}
Win32: iconic=True          ← 仍然最小化
```

CDP 只更新了 Chromium 的内部窗口模型，**没有动 OS 窗口**。
真正生效的是 Win32 `ShowWindow(hwnd, SW_RESTORE)`（=9）。
注意 `SW_SHOW`（=5）**不还原**最小化窗口 —— 这是"调了 `ShowWindow` 却毫无反应"的根因。

复现脚本：`node scripts\diag-windowstate.mjs <port>`（配合 `pwsh -File scripts\roxy-open.ps1 -List` 看 OS 侧状态）。

**修复：** `roxy-open.ps1` 用 CDP + Win32 两步还原并置前；
`roxy-direct-launch.ps1` 启动后自动调用它（`-NoShow` 可关闭）。详见 MANUAL 坑 6。

### 14.7 修正：`DevToolsActivePort` 的读取优先级

之前假定 `<profile>\DevToolsActivePort` 总是当前有效端口。**只在
`--remote-debugging-port=0`（自动分配）时才成立** —— 此时 Chromium 会写该文件。

指定**固定端口**时 Chromium **不写也不刷新**它，目录里那份是上一次运行残留的，
端口号是错的。症状：`roxy-open.ps1 -List` 报 `port=51452`，实际监听在 `9335`。

正确优先级：**命令行 `--remote-debugging-port=N`（N>0）优先，否则才读 `DevToolsActivePort`**。
`roxy-open.ps1` 已按此实现。
