# Roxy 十账号流量根因复测（2026-09-14）

## 当前结论与证据更正

目标仍为十个新邮箱完整注册约 20–30 MB，并解释启动、关闭及注册以外的代理消耗。已复现供应商连接大下载并定位到 Roxy 新 Profile 的组件更新；修复后整批达标仍待验证。历史记录 642–651 的 Meta 网卡增量约 398.1 MB，账号 CDP 摘要约 98.3 MB；这两个数不是同一统计范围，其差值不是“注册漏计流量”的证明。

Meta 已确认是 Mihomo 的全机 TUN，承载多个应用。供应商扣费应对照对应上游代理连接字节与供应商账单；Meta 网卡仅作为全机突发参考。CDP 的 `postData` 是请求体估算，未覆盖压缩上传、TLS、请求头、重传、协议请求及启动前流量，所以 `observed_transport_bytes` 也不是精确账单。

## 证据与定位

- 修复缓存前，每个成功账号下载约 5–11 MB 的 ChatGPT 公共 bundle；静态缓存命中关闭后 `cached=0/hits=0`，因此本轮异常不是缓存复用口径造成。
- `/browser/open` 到 Selenium 接入之间缺少账号级事件。历史并发等待较长，而单账号 653/655 仅 4–6 秒；应分开记录本地生命周期排队与外部网络请求，不以等待时长推算字节。
- 密码/2FA 阶段与 Meta 峰值的时间重合，只能定位观察窗口，尚不能证明由 `/backend-api/models` 或重认证造成。

## 已实施

- `core/browser_traffic.py::block_reason` 存在 session-only 分类规则；复核发现 low-traffic Fetch patterns 尚未覆盖普通 ChatGPT JS/XHR，所以仅分类器测试通过不足以证明运行时拦截。修复匹配范围前还需保留密码/MFA 必需请求。
- `stop-webui.bat`：处理“进程在枚举后自行退出”的竞态，避免启动脚本因 `Stop-Process` 偶发找不到 PID 而中止。
- `core/account_export.py::_validate_2fa_token`：只读 Token 校验改为流式响应并在读取正文前关闭，避免 Cloudflare 403 挑战 HTML 被完整下载。
- Roxy 启动参数须以实际进程为准，而不是发送日志。该历史轮次运行时共享缓存关闭、上限 256 KiB；这会让大 bundle 每个新 Profile 重复下载，尚未恢复教程的共享缓存目标。
- `core/browser_traffic.py::is_cacheable_request`：允许公开 CDN 常见的 `x-client-version`/`x-openai-build-id` 等非敏感请求头，仅继续拒绝认证、条件和设备会话头，避免缓存候选被无关 `x-*` 头全部淘汰。
- 热缓存实验（677–686）记录 Meta 增量 153.9 MB 下行、39.3 MB 上行。缓存字段是解压后的本地回放体量，和全机网卡同时增长不能证明回放经过供应商代理。此前“缓存回放放大代理账单”的结论撤回，待单请求冷载/本地回放及代理连接字节对照验证。
- 逐账号上传路径复盘（13:09–13:16）定位到 `auth.openai.com/awe/api/v2/rum`：单账号约 2.82–3.09 MB，十账号约 29.8 MB；该请求是 Auth RUM 遥测批次，不参与注册、密码、OTP 或 MFA。此前分类器将其作为 live security/auth 请求放行，因此正好形成每号约 3 MB 的固定异常上传。
- 低流量策略现在仅对 `https://auth.openai.com/awe/api/v2/rum*` 加 Fetch 精确拦截并记录 `auth_rum`，不扩大到 `auth.openai.com`，挑战、登录页面、OTP/MFA API 继续直连。
- 687–696 的 `auth_rum` 阻断数与 1.58–3.19 MB `uploaded` 同时出现，原因是旧计量在 `requestWillBeSent` 就累加被阻断的尝试正文。这不是“接管前已上传首批 RUM”的证据；此前该推断撤回。`about:blank` 只取消接管时尚未结束的启动页，不据此宣称节省了 3 MB。
- `f2b06eb` 已扣除被阻断 POST，但对响应阶段阻断、重定向、localhost 和双向预算还需进一步修正。即便扣除阻断请求，CDP 字节仍仅为观测估算。
- Roxy `History` 出现本地 Dashboard，磁盘缓存合计约 196.8 MB；目录大小不能证明这些资源通过代理传输。此前“Dashboard 已经被代理转发”的结论撤回。`d5f596a` 把 `openWorkbench/startupParam` 放在 body 顶层，与官方 `fingerInfo` 层级不符，且 `<-loopback>` 实际删除 Chromium 隐式本地绕行规则。现在修正为 `fingerInfo.openWorkbench=0`，不再注入该代理参数，保留调用方其他指纹设置。

## 下一轮验证

1. 用 Mihomo 命名管道只读 `/connections`，记录全局总量与匹配本项目供应商 host/port 的连接增量，注明短连接采样丢失的限制。
2. 不先消耗新邮箱：单 Profile 比较 workbench 开/关，再比较公开 JS 冷载与本地回放，建立可复现的真实代理对账链。
3. 修复缓存/拦截实际执行链后再运行十个新邮箱，完整记录成功、失败、关闭阶段；失败与额外流程消耗均纳入批次实际费用。未达到目标则继续定位而非把 UI 估算数字作为完成证据。

## 14:23–14:28 单变量对照与修复

- 用户提供供应商余额 **4.93 GB**，该截图精度不足以单独核对 KiB 级差异；待十账号后再次对账。
- 空启动对照（未注册账号）：`fingerInfo.openWorkbench=1` 实际打开本地 Dashboard；`=0` 实际打开 `chrome://newtab/`。两个探针均确认关闭、删除。对应供应商连接采样约 42,548 B 与 229,665 B（包含启动后台请求，且为采样下界），没有 18–22 MB/窗口的证据。
- 冷载公开 `conversation-small-h1dtzoris1y9588z.js`：编码下载 1,581,965 B，缓存正文 5,194,056 B；供应商连接采样窗口上传 22,606 B、下载 1,687,294 B。
- 新 Profile 读取已有公共缓存：`cache_hits=1`、`misses=0`、`errors=[]`、CDP 外部下载 0。回放窗口供应商采样双向 104,625 B、Mihomo 全局双向 783,137 B，均包含背景流量；不支持 5.19 MB 回放被当作同量外部流量的假说。同 Profile 重复加载仅为浏览器内存复用（hits=0），已和这次真正共享缓存命中分开。
- 因此恢复现有缓存路径：`ROXY_STATIC_CACHE=True`、每项 8 MiB、最大年龄 7 天。源站 freshness、public/MIME、私有响应头等门禁不变；不共享账户状态。
- 修正 session-only 实际链路：ChatGPT URL-only Fetch 注册覆盖脚本/XHR；仅在已验证 Session 后收紧，精确保留 MFA enroll/activate；可选 Codex 清状态重登前解除 session-only。测试覆盖 patterns → callback，而非只测纯分类器。
- 计量修正：逐 redirect hop 结算；原生缓存与 loopback 排除；保留响应侧阻断已发生的流量；记录请求前 Fetch 阻断身份；双向预算；二进制 WebSocket 按解码 payload 估算。指标仍不等同供应商账单。
- 密码与 2FA 复核：未改变邮箱匹配、同窗 Cookie/代理、显式 Token、密码成功 checkpoint、enroll/activate 成功后保存；MFA 路由仅放行而不缓存；已验证会话前保持登录恢复所需资源。

证据位于忽略目录：`run/startup-workbench-ab-20260914.json`、`run/cache-cold-replay-ab-20260914.json`、`run/cache-true-replay-20260914.json` 及对应 `run/proxy-controller-*.jsonl`。连接采样可能遗漏短连接及关闭前尾部，未用下界伪装账单。

验证：浏览器流量、Roxy 代理/Session 恢复、密码/2FA 聚焦套件 **337 passed, 260 subtests passed**；流量与配置默认值套件 **80 passed, 247 subtests passed**。十账号新回归仍待完成，当前不宣称全目标达成。

参考：[Roxy 官方 API 字段层级](https://roxybrowser.com/docs/api-documentation/api-endpoint.html)、[Chromium 代理隐式绕行与减法规则](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md)。


## 14:30–14:40 Cliproxy 十账号与单账号连接定位

用户明确 4.93 GB 是 **Cliproxy** 余额，不是 Clash 套餐；Cliproxy 面板有延迟结算。余额刷新时点不当作字节实际传输时点。

### 707–716：页面压缩有效，但整体仍有额外大下载

- 7 个任务成功、3 个任务失败，最后关闭/删除环境于 14:34:40。
- 全部十次浏览器双向 payload 观测合计 **21,608,025 B（21.61 MB）**，缓存命中 **443** 次。284,936,648 B 的解压后本地回放不计入实际流量。
- 14:30:47–14:35:40 的 Cliproxy 匹配连接采样下界 **154,968,588 B（154.97 MB）**；Mihomo 全局 **182,488,176 B**，二者不混算。
- 7 条大连接合计 **127,143,429 B 下行**，上行仅 8,987 B；14:32:13–14:32:46 的 32.65 秒集中增加 **100,087,397 B**。不是内存“释放”流量，而是实际大下载在短时间发生，叠加供应商面板刷新/延迟结算表现为跳变。
- 连接采样仍有短连接/关闭尾部遗漏：1459 个有效样本、3 次读取错误、515 条关闭连接尾部未知。154.97 MB 是观测下界，不伪装精确账单。

### 717：单账号因果链

为避免直接再烧十账号，增加一个新邮箱的诊断运行；仅采集 HTTP 元信息、操作系统 TCP/PID 和 Chromium NetLog，不修改业务请求。该任务最后在补密码的邮件阶段失败，但后台大下载已经完整发生。

1. Cliproxy 连接 `1188b60d-f794-4591-9c26-b6aa5de794e8` 于 14:38:43.333 建立，源端口 **58857**，TCP 表精确映射 **RoxyChrome.exe PID 59976**，父进程 **59152**。
2. NetLog SOCKET source **1372** 的本地地址端口与之吻合；HTTP_STREAM 依赖链指向 URL_REQUEST **1368**。
3. 目标是 `edgedl.me.gvt1.com` 的 `chrome_component` 下载，文件标识 `oimompecagnajdejgnnjijobebaeigek_4.10.3050.0_win64_….crx3`（Widevine）。HTTP 200、gzip、Content-Length **17,704,052 B**。
4. 解码后的 CRX **22,692,383 B**，与同一 Profile 在 14:38:48 写入的 `component_crx_cache/7b81444b063f81e77c527c5589e86836cc2f7328deb2772300ccc7dc9e2510c3` 大小一致；还新增了 **7,929,264 B** 的本地建议模型 CRX。磁盘大小仅作交叉核对，外部计量使用 NetLog/代理连接字节。
5. 浏览器页面/Service Worker CDP 只见普通小请求；Python Token 校验和套餐查询时点晚于主要下载首段。根因位于浏览器后台组件更新，不是缓存 fulfill、本地 Dashboard 或这两条 Python 请求。

### 参数路径的实际缺陷

`_ROXY_PROFILE_EFFICIENCY_ARGS` 早已包含 `--disable-component-update` 等参数，但只放在 `/browser/open` 的 `args`。单元测试验证了发送 payload，真实 RoxyChrome argv 却没有这些参数。

当前运行时确认 `fingerInfo.startupParam` 真正进入 argv。官方 API 规定该字段以 **分号**分隔：把两个 flag 用空格拼接会被作为一个 argv（诊断 NetLog 文件名实际吞入第二个 flag，退出后仍成功落盘）。因此修复应搬迁已有默认参数到 Profile 创建的真实读取字段，而不是追加另一套省流开关。

### 失败分型与密码/MFA复核

先按高/中/低概率核查，再对证据分类：

- 高概率已证实：707 是邮件列表单请求 5 秒 ReadTimeout，连续错误阈值 1 触发终止；711 是 25 秒内没有 after_ts 后的新 OTP；715 是补设密码前同窗只读 Session 返回 HTTP 403。
- 中概率待核：715 具体是远端挑战还是鉴权状态，当前响应摘要不足；不把它和邮件错误混合。
- 低概率直接机制排除：真实 HTTP403 重放得到 `stage=session,status=403`，本地 Fetch rejection 得到 `stage=exception,status=null`；Session 精确放行且不缓存。715 尚未发起密码/MFA写操作。
- 保留账号邮箱匹配、同一浏览器 Cookie/代理、Token 显式透传、密码终态 checkpoint、enroll/activate 成功后保存；不为省流共享账号态或跳过确认。

诊断均保留在忽略目录，不进入 Git：`run/batch707-716-proxy-summary.json`、`run/batch707-717-browser-summary.json`、`run/proxy-controller-diagnostic717.jsonl`、`run/supplier-diagnostic-cdp-717.jsonl` 与 NetLog。原始诊断可能含敏感 URL，仅报告脱敏 host/path/字节及关联 ID。

补充参考：[Roxy startupParam 分号约定与 open args](https://roxybrowser.com/docs/api-documentation/api-endpoint.html)。


### 组件明细闭环与最小修复

717 的同一供应商 Socket 编码正文：Widevine 17,704,052 B、输入建议模型差分 4,665,960 B、其他组件 544,456 B，共 **22,914,468 B**；Socket 实收 **22,917,769 B**，余量 3,301 B 为响应头/隧道等传输差异。整个浏览器供应商下行 **28,734,120 B**，后台组件占 **79.75%**。这些是 NetLog 层级量，不包括 Python 请求，也不冒充最终供应商扣费。

已在 `RoxyBrowserClient.create_profile` 将原 7 个效率参数迁移到分号分隔的 `fingerInfo.startupParam`，`open_profile` 删除默认参数注入。保留显式用户参数及配置对象不可变；新 Profile 实际进程 argv 已确认 `--disable-component-update` 等 7 项全部存在。不是只检查发送 payload。

报告证据补充：`run/supplier-diagnostic-717-netlog-summary.json`。不修改系统 Clash 配置、不复用个人 Profile、不共享 Cookie/账号凭据、不干预账号验证；修复限定在项目创建的临时浏览器生命周期。


## 14:43–14:45 修复后登录驻留验证（未注册新账号）

- 单独创建新 Profile，确认真实进程 argv 中原有 7 个效率参数全部存在；登录页加载后驻留 100 秒，完成关闭和删除。
- NetLog **8,687 事件**，组件/CRX/diffgen 下载 **0 次**，`update.googleapis.com` 更新检查 **0 次**。
- 22 个浏览器供应商 Socket：下行 **1,130,312 B**、上行 **74,362 B**。Controller 供应商窗口下界下行 **1,147,487 B**、上行 **78,795 B**，合计 **1,226,282 B（1.23 MB）**，0 次读取错误。
- 窗口固定至 **14:45:12.334**，不混入其他后续活动；Mihomo 全局从来不作为该供应商费用。
- 该结果证明后台组件异常消失，**不把登录驻留的 1.23 MB 冒充完整账号成本**。717 的组件正文 22,914,468 B 与修复后组件 0 B 使用同层级对比，完整十账号仍待复测。
- 用户仍等待 Cliproxy 余额延迟刷新；为了取得独立的新基准，暂停新增注册与外部探针。收到稳定余额后再启动下一批十个新邮箱。当前没有宣称完成全部目标。

证据：`run/component-fixed-probe-summary.json`、`run/component-fixed-probe-netlog-summary.json`、`run/proxy-controller-component-fixed-probe.jsonl`。

## 本次交付自检（R8）

1. **修改的已有代码**：`core/roxybrowser_client.py:RoxyBrowserClient.create_profile/open_profile`；`core/roxy_registration.py:run_roxy_registration` 更正旧注释；此前本轮 `core/browser_traffic.py:RoxyTrafficOptimizer/summarize_performance_logs`、`webui/app.py:_compact_registration_traffic`、`webui/templates/index.html:_registrationTrafficLine` 和缓存默认值亦已提交。
2. **新增代码**：没有新增业务函数/模块或独立策略。原 create_profile 内合并既有参数；新增回归用例替代了只检查 open payload 的旧断言，旧断言已删除。
3. **搬迁**：默认参数注入从 open_profile 移到 create_profile，源注入已删除；未移动文件、未建立备份树。
4. **配置链路**：无新增配置。已有 `_ROXY_PROFILE_EFFICIENCY_ARGS → create_profile → fingerInfo.startupParam（分号）→ RoxyChrome argv`，实际进程逐项验证通过；原 public 静态缓存参数仍经已有配置对象进入 optimizer。
5. **死引用回扫**：下列命令输出为空：

   `rg -n 'for value in \(\*configured_args, \*_ROXY_PROFILE_EFFICIENCY_ARGS\)|test_open_merges_profile_efficiency_args_without_reducing_concurrency|缓存省:|实际新增网络下载:' core/roxybrowser_client.py tests/test_roxy_proxy_enforcement.py webui/templates/index.html`

6. **diff**：`4a40d20` +529/−147；`7802625` +122/−8；组件根因修复 `3a0bd25` **+154/−27**（含证据文档）。最终结果记录单独作为文档提交，不追加业务代码。
7. **测试**：完整聚焦验证 **478 passed，300 subtests passed**；提交后烟雾验证 **130 passed，262 subtests passed**。`git diff --check` 通过。真实 `start-webui.bat 5002` 依赖自检、旧实例替换和 `/login` readiness 全通过，新 WebUI PID 73884。完整套件最后 5 行：

```text
...................................................... [ 46%]
........................................................................ [ 61%]
........................................................................ [ 76%]
..................................................................................................................               [100%]
478 passed, 1 warning, 300 subtests passed in 20.27s
```

8. **未做/存疑**：等待 Cliproxy 延迟刷新后的稳定余额；待新十账号完整复测，尚未证明每个成功账号含全部附加流量稳定 2–3 MB；连接快照尾部缺失与供应商实际计费差异如实保留；715 只读 Session403的远端来源未细分。没有夹带修复邮件服务超时或其他支付模块。既有三项未跟踪文件/目录保持原样，故仅声称本次修改已提交、不称全仓无未跟踪项。
