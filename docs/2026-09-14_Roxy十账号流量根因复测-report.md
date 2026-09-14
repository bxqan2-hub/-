# Roxy 十账号流量根因复测（2026-09-14）

## 最终验收与交付（2026-09-14，用户确认结束）

**结论：此前百 MB 级异常的根因已修复，两家代理各十个新邮箱复测通过本轮验收。** 用户确认 1024proxy 扣量符合预期，并接受 Cliproxy 当前未见大幅跳变的结果，明确不再等待结算刷新。停止新增注册、代理探针和余额等待；本次只整理交付文档，业务保持已实测的 `5ced4b4`，没有再改注册流程。

### 为什么之前会突然多出大量流量

- **主要真实消耗在浏览器后台，而不是注册页面。** 新建 Roxy Profile 后 Chromium 自动下载 Widevine、输入建议模型等组件。单账号诊断确认组件压缩传输正文 **22,914,468 B（22.91 MB）**；旧十账号窗口曾在 32.65 秒内增加约 **100.09 MB** 下行。并非每个窗口固定下载同样大小。
- **禁用参数看似发送，实际未生效。** 旧代码把已有七项效率参数放进 `/browser/open.args`，Roxy 接受请求，但真实浏览器启动 argv 缺失这些参数，组件更新仍然执行。
- **原显示范围偏小，且供应商余额延后刷新。** 页面 CDP 看不到完整浏览器后台流量；缓存解压回放、Clash 全局流量也都不是供应商扣量。后台真实下载与余额延迟共同造成“页面只有几 MB，面板随后大幅扣量”的观感；供应商内部结算算法和精确延迟未作推断。

### 如何修复

1. 将七项既有效率参数移入真正生效的 `create_profile → fingerInfo.startupParam`，使用分号分隔，删除 open 阶段的旧默认注入；逐个检查真实进程 argv，并用 NetLog 确认组件下载/更新检查为零。
2. 恢复文档的公共静态资源共享缓存（单项 8 MiB、最长 7 天及既有可缓存条件），补齐 Fetch 实际拦截范围，精确阻断非必需遥测，在已验证 Session 后收紧页面资源请求。账号 Cookie、认证和 MFA 响应不进入共享缓存。
3. 绑定创建、打开与清理的同一 Profile，避免旧 dirId 选错环境；按实际选中的代理脱敏记录。这个潜在 Profile 缺陷单独防回归，不冒充已证实的旧批次大下载触发点。
4. 按供应商端点和连接上下行计量，覆盖启动、注册、密码、2FA、辅助查询及收尾；缓存回放不计消耗，NetLog 与同一连接不重复相加，Clash 全局与两家代理分别记录，未归属流量单列。

### 复测与余额核对

| 批次 | 新邮箱注册/密码/2FA | 主代理连接观测 | 另列 SG 辅助端点 | 供应商余额显示 |
| --- | --- | ---: | ---: | --- |
| 1024proxy / 718–727 | 10/10 成功 | 29.250433 MB | 0.104270 MB | 10.09 → 10.06 GB，名义约 30 MB，与主代理观测吻合 |
| Cliproxy / 728–737 | 10/10 成功 | 26.661866 MB | 0.118342 MB | 4.74 → 4.723 GB，当前名义约 17 MB；用户接受并结束等待 |

- 完整固定窗口含辅助端点分别 **29.354703 MB / 26.780208 MB**，均低于用户十账号 40 MB 目标；SG 独立列账，不并入 1024proxy 套餐扣量。两轮二十个新 Profile 均为 **组件下载 0、更新检查 0**。
- Cliproxy 每账号已归属总量 2.556–2.852 MB；1024proxy 九个约 2.53–2.79 MB、一个约 5.39 MB，用户接受此类正常幅度的起伏。Cliproxy 公共缓存命中 497 次，回放字节未算作消耗。
- **验收边界：** 余额截图有显示精度及刷新延迟，1024proxy 约 30 MB 不是字节级账单；Cliproxy 17 MB 不是最终扣量。用户明确接受当前实测和观察结果，故结束本轮，不再将补齐结算截图作为交付阻塞项。教程的 3 MiB 是页面响应诊断预算，不保证每账号全部成本恒定。
- 注册/密码/2FA 均成功；独立 Plan 查询在 1024proxy 批次为 9/10（723 为 SG TLS 错误），Cliproxy 为 10/10。未把套餐查询失败归为注册失败，也未夹带修改该远端传输错误或已知小幅阶段切换竞争。
- 交付前本地聚焦回归：`116 passed, 1 warning, 270 subtests passed in 0.88s`。保留既有 requests 字符检测依赖 warning；未产生新注册流量。完整证据和已有代码交付自检见下文，逐账号邮箱表及原始日志仅保存在 Git 忽略目录。

> 下文为调查过程与当时状态，历史的“待复测/待截图/尚未完成”不代表当前待办；最终验收以上述用户确认和证据边界为准。

## 15:44 两个供应商的完整十账号复测（历史，截图提交前）

业务版本为 `5ced4b44bd31735fbfe891a3e36122f6c1ce3284`。先完成 1024proxy 718–727，再按用户提供的新 Cliproxy 粘性池完成 728–737；两轮各十个新邮箱均注册/密码/2FA 成功。当前用户接受小幅起伏，重点验证后台大量异常消失；没有把已复现的小幅阶段切换竞争夹带进本轮业务代码。

| 独立批次 | 主注册端点 | 起始余额显示 | 主端点传输观测 | 同轮 SG 辅助端点 |
| --- | --- | ---: | ---: | ---: |
| 718–727 / 1024proxy | us.1024proxy.io:3000 | 10.09 GB | 29.250433 MB | 0.104270 MB |
| 728–737 / Cliproxy | us.arxlabs.io:3010 | 4.74 GB | 26.661866 MB | 0.118342 MB |

以上 MB 为十进制连接层上下行，不是 CDP/缓存逻辑体积，也尚非已核对的供应商扣量。SG 辅助端点 `sg.arxlabs.io:3010` 分开列出，其套餐归属仍随供应商明细核对；Cliproxy 本轮的 1024proxy 连接量为 **0 B**，不把上一轮延后刷新的面板变化算成本轮流量。

### Cliproxy 本轮逐账号报告

监测从 15:39:47 的三端点零连接基准开始；业务 15:39:57–15:42:47，固定窗口截止 15:43:47.174624（Asia/Shanghai），覆盖辅助任务结束后 60.093 秒。表内使用同一 Controller 连接字节口径，浏览器 NetLog 仅用于 source IP/port、Socket 精确起始时间及 Profile 身份关联；不把 NetLog 字节再次叠加。

| Job | 注册 us 端点 MB | 套餐 SG 端点 MB | 全部已归属 MB |
| ---: | ---: | ---: | ---: |
| 728 | 2.677287 | 0.012338 | 2.689625 |
| 729 | 2.752633 | 0.012030 | 2.764663 |
| 730 | 2.693407 | 0.012226 | 2.705633 |
| 731 | 2.580542 | 0.011603 | 2.592145 |
| 732 | 2.640962 | 0.012272 | 2.653234 |
| 733 | 2.547389 | 0.012288 | 2.559677 |
| 734 | 2.610241 | 0.012201 | 2.622442 |
| 735 | 2.547193 | 0.008881 | 2.556074 |
| 736 | 2.839306 | 0.012328 | 2.851634 |
| 737 | 2.671791 | 0.012027 | 2.683818 |

- 未归属单账号：us **101,115 B**（20 条 RoxyBrowser 宿主连接），SG **148 B**（2 条仅有 Python 进程归属的连接），不平均分摊。整轮两端点观测 **26,780,208 B**；账号加未归属量严格守恒。
- 独立按连接 ID 高水位重算与采样累计差为 0。一次 `IncompleteRead` 后恢复，有效样本最大间隔 1.002 秒；极短连接/关闭尾部仍可能漏采，保留观测下界限制。
- us 在辅助结束后尾部 **0 B**；SG 跨结束边界首帧 **3,447 B**，之后超过 60 秒无新增连接/字节。额外的本地采样进程随后由操作者停止，停止前数据逐行刷新，工具句柄已终止。
- 728–737 十个 NetLog 全部完整、484 个浏览器 Socket；原始浏览器层单独观测为 26,066,712 B（上 5,591,853 / 下 20,474,859），只作交叉比较，不再加到 Controller 总量。
- 十个实际 argv 的七项效率参数全部存在，**组件/CRX 下载 0、更新检查 0**，最大单 Socket 792,123 B；没有此前每窗口十几 MB 的组件下载。公共静态缓存命中 497 次，缓存回放不计消耗。
- 所有十个账号的密码/2FA 与套餐查询均成功；邮箱与上一轮十个不重复。用户可读的邮箱对应表保存在忽略目录 `run/batch-clip-retest-report.md`，不将生成账号资料或凭据提交 Git。

### 本轮验证与待办

- 既有业务逻辑未修改；仅复用/参数化忽略目录的观测脚本，不新增业务配置或第二套注册流程。被暂停的红测块已撤回，测试文件归一化内容与 Git 索引一致。
- 现有缓存/代理聚焦测试 **96 passed、259 subtests passed**；保留 requests 字符检测依赖 warning。两个解析器均用旧 NetLog/已完成批次精确字节复算，主端点对调回归也通过。
- 两轮十个 Profile 都无组件更新大下载，完整注册观测低于用户 40 MB 目标；**尚缺两家供应商最终余额截图，未宣称账单闭环或整体目标完成**。两份基准仅显示两位小数 GB，约 10 MB 显示粒度，精确对账需要更细用量记录。
- 1024proxy 的第 727 个任务约 5.38 MB Controller 已归属量，其余约 2.52–2.78 MB；用户接受这种小幅变化。只读证据与纯 mock 证明 session-only 切换可能等待缓存锁、排队请求跨阶段继续回源；本轮未扩大改动，后续如需继续优化应单独验证。

证据：`run/batch-clip-retest-manifest.json`、`run/batch-clip-retest-account-summary.json`、`run/batch-clip-retest-connection-summary.json`、`run/batch-clip-retest-netlog-summary.json`、`run/clip-retest-smoke.txt`；上一轮对应 `run/batch-1024-*.json`。两个固定窗口分别计量，禁止相加重叠原始监控文件。

## 先前结论与证据更正

用户最新验收目标为十个新邮箱完整生命周期实际扣量低于 40 MB，并解释每个账号启动、关闭及注册以外的代理消耗。已确认配置代理连接中存在 Chromium 后台组件大下载；既不据此宣称“每个账号额外扣 17 MB”，也不宣称修复后整批达标。历史记录 642–651 的 Meta 网卡增量约 398.1 MB，账号 CDP 摘要约 98.3 MB；这两个数不是同一统计范围，其差值不是“注册漏计流量”的证明。

Meta 已确认是 Mihomo 的全机 TUN，承载多个应用。供应商扣费应对照对应上游代理连接字节与供应商账单；Meta 网卡仅作为全机突发参考。CDP 的 `postData` 是请求体估算，未覆盖压缩上传、TLS、请求头、重传、协议请求及启动前流量，所以 `observed_transport_bytes` 也不是精确账单。

## 切换 1024proxy 前的修复与验收依据（历史）

- Cliproxy 后续截图 4.777 → 4.774 → 4.768 GB，相对原始 4.930 GB 累计名义减少 153 → 156 → 162 MB。量级接近原十账号端点观测 154.97 MB，支持超量不只是页面计数误差；截图结算截至时间未知，原余额窗口还包含其他诊断探针，保留逐项对账限制。
- 用户已授权换入十条 1024proxy 粘性代理，端点 `us.1024proxy.io:3000`，新截图基准 **10.09 GB**。原 Cliproxy 余额不带入新批次；其他流程仍配置的旧端点另行记录。凭据、逐账号原始 NetLog 和运行日志均在 Git 忽略范围。
- 已证实的大下载修复位于 `RoxyBrowserClient.create_profile`：禁用组件更新等 7 个已有参数经分号分隔的 `fingerInfo.startupParam` 进入真实 argv。此前登录驻留探针组件下载为零，这不是完整账号成本结论。
- 本次补上 Profile 配置旁路：`open_profile` 原 `params.setdefault("dirId", pid)` 会让残留 extra.dirId 打开旧 Profile，而创建/返回/清理仍指向新 Profile。纯 mock 复现后改为绑定实际选定 pid，防止绕开新环境的省流参数与代理。当前配置无旧 dirId，此项不是已证实旧批次大流量触发点。
- 本次补上记录缺口：`run_roxy_registration` 之前保存空的入口 proxy，遗漏随机池/预检轮换后的 `client.profile_proxy`。现在既有字段优先实际代理，再回落入口，并复用 `mask_proxy_url` 脱敏；不改变真实网络会话。读取方为 DB/批次 JSON/离线查看页，未发现该字段用于后续网络选路。
- 密码与 MFA 复核：邮箱匹配、同窗 Cookie/代理、显式 Token、密码成功终态 checkpoint、enroll/activate 成功保存均未改变；实际代理凭据不因计量修复新增到持久摘要或日志。
- 附件 §1.1（提取段落 58）明确 **3 MiB 是浏览器压缩响应诊断预算，不是代理账单**；§9/§12 要求成功率不下降、热缓存组低于基线并与供应商计量核对，没有承诺固定 30/40 MB。3 MiB × 10 = 31.46 MB，尚不含全部上传及协议开销。40 MB 作为本轮目标检验，不当作教程定理。
- 本轮按账号关联浏览器全生命周期 NetLog、实际 argv、Python 请求源端口及异步套餐检测账号；连接采样分别输出 1024/旧 Cliproxy/未知归属。初始存量做基准、同连接高水位去重；短连接/尾部缺失仍单列，未归属量不平均分摊。

### 本次代码交付自检（R8，完整实测前）

1. 修改既有代码：`core/roxybrowser_client.py:RoxyBrowserClient.open_profile`、`core/roxy_registration.py:run_roxy_registration`。
2. 新增业务函数/配置均无；新增测试 `test_fresh_profile_launch_and_cleanup_ignore_stale_extra_profile_id`、`test_explicit_maintenance_profile_overrides_stale_extra_profile_id`，替换 open 的旧 setdefault 及入口代理落盘表达式。没有第二套注册路径。
3. 无文件搬迁；旧语句已删除，无备份树。
4. 无新增配置；既有代理池 → client.profile_proxy → mask_proxy_url → save_account_data.proxy_used → DB/导出。运行代理保持完整，记录脱敏。
5. 死引用回扫：`git grep -n -e 'params.setdefault("dirId"' -- core/roxybrowser_client.py`；`git grep -n 'proxy_used=proxy or None' -- core/roxy_registration.py`，输出为空。
6. 业务/测试四文件 diff：+85/−7；文档更新单独计入 Git 统计。增加的是防回归断言，未保留被替换业务语句。
7. 本次本地回归 413 passed、286 subtests passed；`git diff --check` 与诊断脚本语法检查通过。最后五行原始输出如下。

```text
  C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Lib\site-packages\requests\__init__.py:92: RequestsDependencyWarning: Unable to find acceptable character detection dependency (chardet or charset_normalizer).
    warnings.warn(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
413 passed, 1 warning, 286 subtests passed in 19.86s
```

8. 未做/存疑：完整十账号实测待执行，尚无新供应商结算终值；一般维护配置的 workspaceId 多来源冲突未扩大修复，当前运行配置一致。既有三个未跟踪项原样保留。requests 字符检测依赖 warning 未夹带处理。

## 14:55 本地复核：余额继续变化，计费归因暂未闭环（历史暂停记录）

用户确认这份 Cliproxy 套餐的端点为 `us.arxlabs.io:3010`，与采样端点一致。用户同时明确要求等待、暂停任何新增耗流量操作；当前不启动注册、外网探针、代理检测或远程推送。

| 截图本地落盘时间（+08:00） | Cliproxy 剩余显示 | 相对 4.930 GB 的名义减少 |
| --- | ---: | ---: |
| 14:23:21 | 4.930 GB（原图显示 4.93） | 0 MB |
| 14:46:40 | 4.899 GB | 31 MB |
| 14:50:53 | 4.875 GB | 55 MB |
| 14:52:12 | 4.865 GB | 65 MB |
| 14:53:34 | 4.843 GB | 87 MB |

以上按十进制显示值计算；截图本地时间不是供应商计量截至时间。4.899 GB 后还在下降，故 31 MB 不是稳定终值；最新 87 MB 也仅是当前显示差额，不当作最终结算。正常 GB/GiB 或显示精度差异不足以单独解释 31 MB 与 154.97 MB 的冲突。

**独立复核保留的证据：**

- 707–716 的限定窗口含 515 个唯一连接；重复 ID、消失重现、计数回退、全局重置均为 0。按各连接最大值独立求和仍为 **154,968,588 B**，与增量算法一致。这是匹配端点的传输观测，不是已经核实的账户扣费。
- 717 大连接的 Mihomo 与 NetLog 下行同为 **22,917,769 B**、上行同为 **1,893 B**。SOCKS5 端点解析、CONNECT_JOB、Socket、Stream 和 CRX 请求存在逐项绑定；该下载确实发生过，但单样本不证明每个窗口固定下载/扣费同量。
- 原始监控文件时段相互重叠：batch 文件覆盖 14:30:13–14:45:13，717 文件覆盖 14:37:17–14:47:17。只使用已限定起止的窗口或独立 NetLog 层级量；禁止把整份原始文件合计相加。
- 14:52:11–14:53:41 本地采样已终止，179 个样本、0 错误；`us.arxlabs.io:3010` 为 **0 连接、0 新增字节**。它只覆盖该端点及该时段，短连接采样限制仍存在。
- 14:54:06 的另一份本地 Controller 单次快照中，us 与 `sg.arxlabs.io:3010` 均为 0 连接；单次快照不代表整个间隔没有连接。Plan 持久状态为 running=0/queued=0，最新完成时间 14:39:51；这不是内存任务队列的完整证明。

**尚缺的计费对应关系：**

- host/port 相同已由用户确认，但历史 Profile 已删除、持久 `proxy_used` 为空，尚缺每次实际凭据与截图中套餐/子账号的完整对应。注册实际配置由 `data/proxy_pool.txt` 读取；100 条仅随机 session 不同，不应误判成 100 个付费身份。
- 发现已有记录缺陷：`core/roxy_registration.py:run_roxy_registration` 保存入口 `proxy`，而随机选中/预检后的实际代理保存在 `client.profile_proxy`。本轮先记录 Finding，不在等待结算期间改动业务或增加采样流量。
- 当前 Plan 等辅助流程还配置了 sg 端点与其他凭据骨架；不把仅筛选 us 的采样标作全部 Cliproxy 业务覆盖。去 session/地区标签后的同一凭据骨架只是同源线索，不等同供应商同一套餐证明。
- 需要稳定余额及其计量窗口，或供应商同时间段的扣量明细，才能判断实际扣费是否达到目标。结算延迟、其他设备共用或免计费规则均不预设为结论。

证据：`run/cliproxy-balance-ledger-20260914.json`、`run/supplier-meter-audit-20260914.json`、`run/proxy-controller-settlement-idle-20260914.jsonl`。本节优先于下文历史试验中尚未完成账单核对的措辞。

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
- 逐账号上传路径复盘（13:09–13:16）定位到 `auth.openai.com/awe/api/v2/rum`：单账号约 2.82–3.09 MB，十账号约 29.8 MB；该请求是 Auth RUM 遥测批次，不参与注册、密码、OTP 或 MFA。此前分类器将其作为 live security/auth 请求放行；所列字节来自 CDP 请求体估算，先记作 RUM 尝试正文量，不直接推定已发出、已计费或同额节省。
- 低流量策略现在仅对 `https://auth.openai.com/awe/api/v2/rum*` 加 Fetch 精确拦截并记录 `auth_rum`，不扩大到 `auth.openai.com`，挑战、登录页面、OTP/MFA API 继续直连。
- 687–696 的 `auth_rum` 阻断数与 1.58–3.19 MB `uploaded` 同时出现，原因是旧计量在 `requestWillBeSent` 就累加被阻断的尝试正文。这不是“接管前已上传首批 RUM”的证据；此前该推断撤回。`about:blank` 只取消接管时尚未结束的启动页，不据此宣称节省了 3 MB。
- `f2b06eb` 已扣除被阻断 POST，但对响应阶段阻断、重定向、localhost 和双向预算还需进一步修正。即便扣除阻断请求，CDP 字节仍仅为观测估算。
- Roxy `History` 出现本地 Dashboard，磁盘缓存合计约 196.8 MB；目录大小不能证明这些资源通过代理传输。此前“Dashboard 已经被代理转发”的结论撤回。`d5f596a` 把 `openWorkbench/startupParam` 放在 body 顶层，与官方 `fingerInfo` 层级不符，且 `<-loopback>` 实际删除 Chromium 隐式本地绕行规则。现在修正为 `fingerInfo.openWorkbench=0`，不再注入该代理参数，保留调用方其他指纹设置。

## 当时的下一轮验证计划（历史，已执行）

1. 用 Mihomo 命名管道只读 `/connections`，记录全局总量与匹配本项目供应商 host/port 的连接增量，注明短连接采样丢失的限制。
2. 不先消耗新邮箱：单 Profile 比较 workbench 开/关，再比较公开 JS 冷载与本地回放，建立可复现的真实代理对账链。
3. 修复缓存/拦截实际执行链后再运行十个新邮箱，完整记录成功、失败、关闭阶段；失败与额外流程的传输观测均纳入固定批次窗口；实际扣量另以供应商同窗口记录核对。未达到目标则继续定位而非把 UI 估算数字作为完成证据。

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

用户明确 4.93 GB 是 **Cliproxy** 余额，不是 Clash 套餐；用户观察到余额延后变化；具体结算机制及计量截至时间尚未核实。余额刷新时点不当作字节实际传输时点。

### 707–716：页面压缩有效，但整体仍有额外大下载

- 7 个任务成功、3 个任务失败，最后关闭/删除环境于 14:34:40。
- 全部十次浏览器双向 payload 观测合计 **21,608,025 B（21.61 MB）**，缓存命中 **443** 次。284,936,648 B 的解压后本地回放不计入实际流量。
- 14:30:47–14:35:40 的配置代理端点匹配连接传输观测下界 **154,968,588 B（154.97 MB）**；Mihomo 全局 **182,488,176 B**，二者不混算。
- 7 条大连接合计 **127,143,429 B 下行**，上行仅 8,987 B；14:32:13–14:32:46 的 32.65 秒集中增加 **100,087,397 B**。连接侧确实存在短时间的大下载；它是否及何时形成 Cliproxy 面板的同额扣量，仍待同一计费身份、结算窗口及规则核对。面板跳变的原因尚未因此证实。
- 连接采样仍有短连接/关闭尾部遗漏：1459 个有效样本、3 次读取错误、515 条关闭连接尾部未知。154.97 MB 是观测下界，不伪装精确账单。

### 717：单账号因果链

为避免直接再烧十账号，增加一个新邮箱的诊断运行；仅采集 HTTP 元信息、操作系统 TCP/PID 和 Chromium NetLog，不修改业务请求。该任务最后在补密码的邮件阶段失败，但后台大下载已经完整发生。

1. Mihomo 中匹配配置代理端点的连接 `1188b60d-f794-4591-9c26-b6aa5de794e8` 于 14:38:43.333 建立，源端口 **58857**，TCP 表精确映射 **RoxyChrome.exe PID 59976**，父进程 **59152**。
2. NetLog SOCKET source **1372** 的本地地址端口与之吻合；HTTP_STREAM 依赖链指向 URL_REQUEST **1368**。
3. 目标是 `edgedl.me.gvt1.com` 的 `chrome_component` 下载，文件标识 `oimompecagnajdejgnnjijobebaeigek_4.10.3050.0_win64_….crx3`（Widevine）。HTTP 200、gzip、Content-Length **17,704,052 B**。
4. 解码后的 CRX **22,692,383 B**，与同一 Profile 在 14:38:48 写入的 `component_crx_cache/7b81444b063f81e77c527c5589e86836cc2f7328deb2772300ccc7dc9e2510c3` 大小一致；还新增了 **7,929,264 B** 的本地建议模型 CRX。磁盘大小仅作交叉核对，外部计量使用 NetLog/代理连接字节。
5. 浏览器页面/Service Worker CDP 只见普通小请求；Python Token 校验和套餐查询时点晚于主要下载首段。该大连接的传输来源是浏览器后台组件更新，不是缓存 fulfill、本地 Dashboard 或这两条 Python 请求；这不是 Cliproxy 扣费归因的最终结论。

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
- 该结果证明本次登录驻留探针未再发生组件下载，**不推广为所有流程均已消除异常，不把 1.23 MB 冒充完整账号成本**。717 的组件正文 22,914,468 B 与修复后组件 0 B 使用同层级对比，完整十账号仍待复测。
- 遵照用户要求暂停新增流量；余额变化原因及统计截至时间仍待核实。余额暂时不变本身不证明结算完成；仅在用户明确恢复、先确定账户及计量窗口后再启动下一批十个新邮箱。当前没有宣称完成全部目标。

证据：`run/component-fixed-probe-summary.json`、`run/component-fixed-probe-netlog-summary.json`、`run/proxy-controller-component-fixed-probe.jsonl`。

## 组件修复时的交付自检（R8，历史）

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

8. **未做/存疑**：等待用户确认恢复及 Cliproxy 同窗口计量依据；待新十账号完整复测，尚未证明每个成功账号含全部附加流量稳定 2–3 MB；连接快照尾部缺失与供应商实际计费差异如实保留；715 只读 Session403的远端来源未细分。没有夹带修复邮件服务超时或其他支付模块。既有三项未跟踪文件/目录保持原样，故仅声称本次修改已提交、不称全仓无未跟踪项。
