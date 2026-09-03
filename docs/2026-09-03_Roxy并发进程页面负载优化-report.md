# Roxy 并发进程页面负载优化核对（2026-09-03）

## 目标

在不减少注册 worker、Profile 或浏览器并发数量的前提下，降低每个 Roxy Profile 的后台服务和 CDP Network 缓冲开销，减少多个页面同时运行时的卡顿。

## 现象与证据

- 当前批次高峰曾同时运行 10 个注册任务，`RoxyChrome` 子进程峰值约 90 个，Roxy 工作集约 7.9 GB；这说明卡顿与活动 Profile 的渲染器数量相关，而不是历史账号文件条目数本身。
- 批次收敛后，任务为 0 个运行中、`RoxyChrome` 为 0 个，Roxy 相关进程工作集约 0.99 GB、系统可用内存约 9.61 GB；活动浏览器清理后内存压力明显下降。
- 批次收敛后 `/browser/workspace` 可快速返回，但 `/browser/list` 仍出现 5 秒读取超时，说明 Roxy Profile 管理接口还存在独立的列表阻塞现象，不能只把页面卡顿归因于账号总数。

## 原因分级

### 高概率

1. 每个 Profile 的 Chromium 后台更新、同步、域名可靠性和崩溃上报服务在并发时争用 CPU/内存。
2. CDP Network 域的事件缓冲按 Profile 持续累积，直到 Selenium 结束时才集中读取，造成并发页面额外内存占用。

### 中概率

1. 可视化窗口会额外承担桌面合成和音频管线工作。
2. Profile 本地缓存目录较多时，Roxy 管理器扫描和删除会产生磁盘 I/O 峰值。

### 低概率

1. `randomFingerprint`、系统画像和当前 coreVersion 本身不是页面卡顿的直接原因。
2. 其他 Edge/Chrome 进程会形成背景负载，但不是本次 Roxy 进程峰值的主因。

## 上游对照

- 上游锁定 commit：`68a1f8faede7e41f10ac5f9af267465fa61d0e3d`。
- 上游 `core/roxy_registration.py` 同样通过 Roxy `/browser/open` 的 `args` 传递启动参数，并使用 Selenium performance 日志；本次保留该调用边界，不改变注册页面、Cookie、代理或指纹流程。
- 上游 `core/browser_traffic.py` 使用 `Network.enable` 默认空参数；本地在同一入口增加可选缓冲上限，并在旧版 CDP 拒绝可选字段时回退 `{}`，避免影响兼容性。
- Roxy 官方 API 文档确认 `/browser/open` 的 `args` 是浏览器启动参数列表；内置的 `--no-first-run`、`--no-default-browser-check` 等参数不重复注入。

## 修改内容

### `core/roxybrowser_client.py::RoxyBrowserClient.open_profile`

- 保留调用方已有 `args`，去重后追加后台优化参数：
  `--disable-background-networking`、`--disable-component-update`、
  `--disable-domain-reliability`、`--disable-sync`、`--disable-breakpad`、
  `--metrics-recording-only`、`--mute-audio`。
- 不修改 UA、OS、`randomFingerprint`、代理、Cookie 或 worker 数量；所有并发 Profile 仍照常启动。

### `core/browser_traffic.py::RoxyTrafficOptimizer._enable_network_domain`

- 每个 Profile 的 `Network.enable` 优先使用 `maxTotalBufferSize=2 MiB`、`maxResourceBufferSize=512 KiB`、`maxPostDataSize=4 KiB`。
- 旧版 Roxy/CDP 不接受可选字段时自动回退默认 `Network.enable`，注册流程继续运行。

## 安全边界核对

- 不新增跨 Profile Cookie、Authorization、Proxy-Authorization 或 Token 共享路径。
- 不关闭 Cloudflare、Arkose、hCaptcha、reCAPTCHA、Sentinel 等安全请求。
- 不改变一号一 Profile、随机指纹、真实出口 IP 预检/复核和完成后清理规则。

## 验证

- 定向测试：`tests/test_browser_traffic.py`、`tests/test_roxy_proxy_enforcement.py`、`tests/test_registration_local_proxy_mode.py`、`tests/test_roxy_registration_otp_recovery.py`、`tests/test_roxy_registration_session_recovery.py`、`tests/test_roxy_codex_otp_polling.py`。
- 新增覆盖：有界 Network 缓冲、旧版 CDP 回退、并发 Profile 的启动参数合并与去重。
- 生产压测未在本次执行；下一批应保持原并发数量并记录 Roxy 工作集、`RoxyChrome` 数量、页面首屏等待和 `/browser/list` 响应时间，作为前后对照。
