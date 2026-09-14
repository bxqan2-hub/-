# Roxy 十账号流量根因复测（2026-09-14）

## 结论

本轮 642–651 使用 `static_cache=False`、`low_traffic=True`、并发 10，代理 Meta 隧道增量约 **398.1 MB**（下行 246.4 MB、上行 151.7 MB）。账号侧性能日志仅观测到 **98.3 MB**（下行 73.5 MB、上行 24.8 MB），仍有约 **299.8 MB** 未被 Selenium 性能事件覆盖，远高于教程要求的 20–30 MB/10 账号。

## 证据与定位

- 每个成功账号仍下载约 5–11 MB 的 ChatGPT 公共 bundle；静态缓存命中关闭后 `cached=0/hits=0`，因此本轮异常不是缓存复用口径造成。
- `Roxy` `/browser/open` 在 Selenium 连接前通常等待约 40–50 秒；该窗口启动阶段发生的代理流量没有账号级 CDP 事件，属于主要未计量区间。
- Meta 隧道上行在并发 10 时出现约 151.7 MB，远高于浏览器 postData 观测值，说明代理/浏览器启动阶段存在重试或控制面开销，需继续按并发 1 与 Roxy open 生命周期单独复测。
- 并发 1 的 Job 653/655 复核显示 `/browser/open` 仅 4–6 秒、启动阶段小于 0.5 MB；主要峰值出现在密码/2FA 重认证和只读 `/backend-api/models` 校验。Meta 网卡同时承载 Roxy UI、Edge/ChatGPT 等其它进程，单看网卡计数会混入非注册流量。

## 已实施

- `core/browser_traffic.py::block_reason` 与 `RoxyTrafficOptimizer._on_request_paused`：Session 建立后仅放行 ChatGPT auth/session/callback 文档及 API，阻止应用壳与后台轮询，避免认证完成后的二次 bundle 下载。
- `stop-webui.bat`：处理“进程在枚举后自行退出”的竞态，避免启动脚本因 `Stop-Process` 偶发找不到 PID 而中止。
- `core/account_export.py::_validate_2fa_token`：只读 Token 校验改为流式响应并在读取正文前关闭，避免 Cloudflare 403 挑战 HTML 被完整下载。
- 复核发现 Roxy 151 会忽略 `/browser/open` 的 `args`；不能把日志中的 open 参数当作进程级优化已生效。另将运行时共享缓存上限设为 256 KiB，避免 CDP 回放解压后的大 bundle 反而放大代理流量。
- `core/browser_traffic.py::is_cacheable_request`：允许公开 CDN 常见的 `x-client-version`/`x-openai-build-id` 等非敏感请求头，仅继续拒绝认证、条件和设备会话头，避免缓存候选被无关 `x-*` 头全部淘汰。
- 热缓存实验（677–686，停止于第 3 分钟）产生 153.9 MB 下行 + 39.3 MB 上行；同一批账号的缓存字段达到 182.8 MB，证明 CDP `Fetch.fulfillRequest` 回放解压正文会放大 Meta 流量。实验后运行时恢复 `ROXY_STATIC_CACHE=False`、`ROXY_CACHE_MAX_ITEM_BYTES=262144`。
- 逐账号上传路径复盘（13:09–13:16）定位到 `auth.openai.com/awe/api/v2/rum`：单账号约 2.82–3.09 MB，十账号约 29.8 MB；该请求是 Auth RUM 遥测批次，不参与注册、密码、OTP 或 MFA。此前分类器将其作为 live security/auth 请求放行，因此正好形成每号约 3 MB 的固定异常上传。
- 低流量策略现在仅对 `https://auth.openai.com/awe/api/v2/rum*` 加 Fetch 精确拦截并记录 `auth_rum`，不扩大到 `auth.openai.com`，挑战、登录页面、OTP/MFA API 继续直连。
- 新一轮 687–696 证实：`auth_rum` 已命中 198–206 次，但成功账号仍上传 1.58–3.19 MB 的同一路径；时间线显示 Roxy `/browser/open` 后约 13 秒才接入 Selenium，启动页已在拦截器安装前发送首个批次。注册入口现于 Selenium 接入后立即导航 `about:blank`，先取消 Roxy 的存储启动页，再安装拦截器并进入登录页。
- 计量口径同时修正：`Network.requestWillBeSent` 会先带出 POST `postData`，即使随后被 Fetch 拦截也会被旧逻辑计入 `uploaded`。现在延后到请求终态，`loadingFailed.blockedReason` 的 body 从 `uploaded/observed_transport_bytes` 排除；因此账号表只显示实际到达代理的上传，阻断体量仍由 `blocked_by_reason` 保留用于审计。
- Roxy Profile 缓存与 `History` 进一步定位到启动前的本地 Dashboard：每个新 Profile 首个 URL 是 `http://127.0.0.1:45535/dashboard.html?id=...`，单 Profile 缓存约 18–22 MB，十个目录合计约 196.8 MB；该页面在 Selenium 接管前加载，且曾被 Profile 代理转发。创建 Profile 现默认 `openWorkbench=0`，并传入 `startupParam=--proxy-bypass-list=<-loopback>,localhost,127.0.0.1`，从源头关闭 Dashboard/绕过 loopback，保留后续 ChatGPT 外部请求走代理。

## 下一轮验证

1. 重新运行 10 个新邮箱，记录 `/browser/open` 前后 Meta 增量，验证启动页清空后首个 RUM 批次是否消失；并按日志 `upload_by_path` 复核每号上传。
2. 若单账号仍超过 3 MB，继续拆分 Roxy 启动阶段与 ChatGPT 页面阶段；在确认启动开销后再调整 Roxy `args`，不关闭安全挑战域名。
3. 代理商账单以 Meta 隧道双向字节为准，浏览器 `downloaded/observed` 仅作定位指标。
