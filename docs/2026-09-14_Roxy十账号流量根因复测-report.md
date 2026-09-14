# Roxy 十账号流量根因复测（2026-09-14）

## 当前结论与证据更正

目标仍为十个新邮箱完整注册约 20–30 MB，并解释启动、关闭及注册以外的代理消耗。现有复测尚未证明达标。历史记录 642–651 的 Meta 网卡增量约 398.1 MB，账号 CDP 摘要约 98.3 MB；这两个数不是同一统计范围，其差值不是“注册漏计流量”的证明。

Meta 已确认是 Mihomo 的全机 TUN，承载多个应用。供应商扣费应对照对应上游代理连接字节与供应商账单；Meta 网卡仅作为全机突发参考。CDP 的 `postData` 是请求体估算，未覆盖压缩上传、TLS、请求头、重传、协议请求及启动前流量，所以 `observed_transport_bytes` 也不是精确账单。

## 证据与定位

- 每个成功账号仍下载约 5–11 MB 的 ChatGPT 公共 bundle；静态缓存命中关闭后 `cached=0/hits=0`，因此本轮异常不是缓存复用口径造成。
- `/browser/open` 到 Selenium 接入之间缺少账号级事件。历史并发等待较长，而单账号 653/655 仅 4–6 秒；应分开记录本地生命周期排队与外部网络请求，不以等待时长推算字节。
- 密码/2FA 阶段与 Meta 峰值的时间重合，只能定位观察窗口，尚不能证明由 `/backend-api/models` 或重认证造成。

## 已实施

- `core/browser_traffic.py::block_reason` 存在 session-only 分类规则；复核发现 low-traffic Fetch patterns 尚未覆盖普通 ChatGPT JS/XHR，所以仅分类器测试通过不足以证明运行时拦截。修复匹配范围前还需保留密码/MFA 必需请求。
- `stop-webui.bat`：处理“进程在枚举后自行退出”的竞态，避免启动脚本因 `Stop-Process` 偶发找不到 PID 而中止。
- `core/account_export.py::_validate_2fa_token`：只读 Token 校验改为流式响应并在读取正文前关闭，避免 Cloudflare 403 挑战 HTML 被完整下载。
- Roxy 启动参数须以实际进程为准，而不是发送日志。当前运行时共享缓存关闭、上限 256 KiB；这会让大 bundle 每个新 Profile 重复下载，尚未恢复教程的共享缓存目标。
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
