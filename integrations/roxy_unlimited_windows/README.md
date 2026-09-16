# RoxyBrowser 无限窗口工具包

> **⚠️ 仅供技术学习与研究使用。** 与本产品官方无关联、未获授权，所述做法可能违反其用户协议，
> 使用可能导致账号封禁或数据丢失。请仅在自己拥有或已获授权的设备与账号上操作，商业用途请购买授权。
> 按"现状"提供，不附带担保，作者不承担使用本文档造成的任何损失。继续阅读即表示接受。

---

## 这是什么

一组零依赖的 Node.js / PowerShell 脚本，让你可以启动**任意数量**的 RoxyBrowser 指纹窗口，
不受 Roxy 账号窗口额度（`maxWindowCount`）的限制。

原理：RoxyBrowser 的窗口 = **定制 Chromium 内核** + **档案目录里的加密指纹配置（`lumi.conf`）**，
两者都在本地，启动过程不需要联网。脚本绕过官方 launcher，直接驱动内核，
并提供一个与官方响应结构完全一致的本地 HTTP API。

## 包内容

```
README.md                    本文件
MANUAL-zh.md                 完整手册（原理 + 方法 + API + 六个坑）  ← 先看这个
TECHNICAL-zh.md              技术详解（含逆向依据、源码位置、已证伪结论附录）

scripts/                     核心脚本（全部零第三方依赖，需 Node ≥ 22）
  roxy-api.mjs               ★ 本地无限窗口 API 服务
  fingerprint.mjs            ★ 共享指纹合成 + lumi.conf 编解码
  paths.mjs                  ★ 路径自动发现（数据目录/安装目录/内核）
  paths-cli.mjs              路径解析结果的命令行查询（供 PowerShell 复用）
  noise-ext/                 按档案实例化的 canvas/音频噪声扩展
  roxy-direct-launch.ps1     命令行直启器（启动后自动还原窗口）
  roxy-open.ps1              ★ 把窗口从最小化/隐藏还原并拉到前台（见「坑 6」）
  show-window.mjs            roxy-open.ps1 的 CDP 部分（设 windowState + 可选导航）
  roxy-newprofile.ps1        一键批量建档案 + 启动 + 健康检查
  mkprofile.mjs              离线档案生成器（命令行版）
  lumi.mjs                   lumi.conf 编解码器
  cdpcheck.mjs               零依赖 CDP 指纹探针
  probe-existing.mjs         探测已有档案的指纹
  verify-profile.mjs         自校验（建→开→逐项比对→清理）
  verify-quick.mjs           快速端到端验证
  diag-*.mjs / test-*.mjs    取证与诊断脚本

tools/                       静态分析工具（可选，与运行无关）
  asar.mjs                   asar 解包 / 列表 / 提取
  unmap.mjs                  从 sourcemap 还原可读源码
  ctx.mjs                    在压缩产物中按字节定位锚点
  patch_limit.mjs            app.asar 原位等长修补（含回滚）
```

## 快速开始

```powershell
# 1) 启动 API
node scripts\roxy-api.mjs --port 50001

# 2) 建一个窗口并打开
$API = "http://127.0.0.1:50001"
$h = (Invoke-RestMethod -Uri "$API/browser/create" -Method POST -ContentType "application/json" `
       -Body (@{ windowName="w1"; locale="pt-BR"; proxy="direct"; open=$true } | ConvertTo-Json)).data
$h | Format-List dirId, http, ws, driver, locale, timeZone, pid

# 3) 用 Playwright 接上去
#    const b = await chromium.connectOverCDP(`http://${h.http}`);

# 4) 全关
Invoke-RestMethod -Uri "$API/browser/close_all" -Method POST -ContentType "application/json" -Body '{}'
```

## 前置条件

| 项 | 要求 |
|---|---|
| OS | Windows |
| Node | ≥ 22（需要内置 `fetch` / `WebSocket`） |
| 依赖 | **无**，全部脚本零第三方依赖 |
| RoxyBrowser | **必须已安装，且至少成功下载过一次内核** |
| 官方 App | 运行与否都行（脚本不需要它） |

### ⚠️ 关键：内核不是脚本能生成的

`RoxyChrome.exe` 是**官方 App 自己下载的**，脚本不会生成它。所以一台干净的机器上直接跑会失败：

```
✗ 内核：内核目录不存在：%APPDATA%\RoxyBrowser\chrome-bin

  RoxyChrome.exe 是官方 App 自己下载的，脚本不会生成它。
  解决：在这台机器上安装并运行一次 RoxyBrowser，
        登录后在界面里打开任意一个窗口，让它把内核下载下来。
        或者从别的机器把 chrome-bin\ 整个目录复制到：
          %APPDATA%\RoxyBrowser\chrome-bin
```

**新机器上跑之前，先做这一步**：装 RoxyBrowser → 登录 → 在官方界面里打开任意一个窗口。
内核下载完成后，本工具包就能用了。

## 路径自动发现

**没有任何写死的路径。** 脚本按优先级搜索：

**数据目录**（含内核、档案、配置 —— 这是必需的）

1. `--data-dir <路径>` 或环境变量 `ROXY_HOME`
2. `%APPDATA%\RoxyBrowser`、`%APPDATA%\roxybrowser`
3. 扫描 `%APPDATA%` 下任何名字含 `roxy` 且含 `chrome-bin\` 或 `browser-cache\` 的目录
4. 从正在运行的 `RoxyBrowser.exe` 进程反推

**安装目录**（可选 —— 只用于官方扩展和 blockDomain 拦截页）

1. `--install-dir <路径>` 或环境变量 `ROXY_INSTALL`
2. `%LOCALAPPDATA%\Programs\RoxyBrowser`、`%ProgramFiles%\RoxyBrowser`
3. 注册表卸载项
4. 常见安装位置扫描

安装目录找不到**不是致命错误**，只会少一个官方扩展，启动照常。

**内核**：`<数据目录>\chrome-bin\<coreVersion>\RoxyChrome.exe`，取版本号最大者；
找不到就递归兜底搜索。

### 查看解析结果

```powershell
node scripts\paths-cli.mjs             # 人话报告（路径已脱敏）
node scripts\mkprofile.mjs --list      # 同上，附带可用语言/分辨率/OS
node scripts\paths-cli.mjs --full-paths   # 显示真实路径
```

```
环境检查通过
  数据目录  : %APPDATA%\RoxyBrowser
  安装目录  : %LOCALAPPDATA%\Programs\RoxyBrowser
  内核      : %APPDATA%\RoxyBrowser\chrome-bin\152\RoxyChrome.exe  (v152)
  chromedriver: %APPDATA%\RoxyBrowser\chrome-bin\152\chromedriver.exe
  档案目录  : %APPDATA%\RoxyBrowser\browser-cache

（路径已脱敏，加 --full-paths 显示真实路径）
```

### 输出默认脱敏

**打印出来的路径不会带你的 Windows 用户名。** 所有人类可读的输出都会把
`C:\Users\<你的用户名>\...` 换成 `%USERPROFILE%` / `%APPDATA%` / `%LOCALAPPDATA%`，
方便截图、贴日志、提 issue。

- **只有显示被脱敏**，实际读写始终用真实路径，功能不受影响
- `--json` 输出（供 PowerShell 脚本消费）始终是真实路径
- 要看真实路径：加 `--full-paths`

```powershell
node scripts\roxy-api.mjs --port 50001                # 输出脱敏
node scripts\roxy-api.mjs --port 50001 --full-paths   # 输出真实路径
```

### 手动指定

```powershell
node scripts\roxy-api.mjs --port 50001 --data-dir "D:\RoxyData"
$env:ROXY_HOME = "D:\RoxyData"     # 或设环境变量
```

> **显式指定是权威的**：`--data-dir` 给了个无效路径会**直接报错退出**，
> 不会静默回退到别的目录 —— 免得你以为在用 D 盘的数据、实际在用 C 盘的。

## 四条必读注意事项

1. **本地端口必须进白名单**。RoxyChrome 强制端口扫描保护，不在 `lumi.conf` 的
   `portScan.portScanWhiteList` 里的本地端口一律不可达（`net::ERR_ADDRESS_UNREACHABLE`）。
   原厂档案只白名单了工作台 45535。`roxy-api.mjs` 建档案时会自动写入自身端口。
   排查"窗口连不上本地服务"**先看这个**，不要先怀疑代理。
2. **不传 `proxy` 会继承旧档案的代理**。要直连就显式传 `"proxy": "direct"`。
3. **没有代理预检**。官方打开前会验代理连通性，本工具不做，代理挂了照开。
4. **直启的窗口可能在屏幕上看不见**。自己 spawn 内核时窗口常常以**最小化/隐藏**态起来
   （`visible=False` 或 `iconic=True`）——官方启动器会替你还原，我们得自己做。
   一行还原并置前：

   ```powershell
   pwsh -File scripts\roxy-open.ps1          # 加 -List 先看有哪些实例和状态
   ```

   `roxy-direct-launch.ps1` 已内置这一步（`-NoShow` 可关）。

完整清单见 `MANUAL-zh.md` 第六部分（六个坑）。

## 验证

```powershell
node scripts\verify-profile.mjs --name probe --locale pt-BR --screen 1920x1080
```

建档案 → 打开 → CDP 逐项比对指纹 → 自动清理，**9 项断言全过才 exit 0**。

## 边界

**已解除**：窗口数量上限、本地并发闸门、指纹注入（时区/语言/分辨率/GPU/硬件并发/canvas 隔离）、
代理、官方 automation-control 扩展。

**不具备**：云同步（Cookie/书签/localStorage 不上传）、官方工作台可见性、服务端记录、
代理预检、跨 OS 伪装（只随机 Win10/Win11）。

## 免责声明

本文档与脚本**仅供技术学习与研究使用**，与 RoxyBrowser 官方无任何关联、未获官方授权或许可。

- 所述做法**可能违反该软件的用户协议**，使用可能导致账号被封禁、订阅失效或本地数据丢失
- 请仅在**你自己拥有、或已获得明确书面授权**的设备与账号上操作；
  **用于商业用途前，请购买对应的软件授权与窗口额度**
- 按"现状"提供，不附带任何明示或暗示的担保；作者不对因使用本工具包造成的任何直接或间接损失负责
- 文中技术细节的目的是理解反检测浏览器的实现原理，
  **请勿用于违反所在地区法律法规或第三方平台规则的用途**
- 若官方认为内容不妥，请联系删除

**使用本工具包即表示你已理解并接受以上条款。**

*个人技术研究笔记，非官方出版物。*

许可条款见 [LICENSE](LICENSE)（Educational Use Only：允许学习研究使用，
**不授予商业用途授权**，亦不授予任何规避付费授权的商业性使用权利）。
