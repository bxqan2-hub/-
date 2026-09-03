# Roxy 冷缓存后注册卡死核对（2026-09-03）

## 现象与结论

本次不是邮箱、OTP、密码或 2FA 请求失败导致的单任务卡住，而是冷缓存后一次性启动 10 个可视 Roxy Profile 造成的主机级资源峰值，随后 Windows 发生非正常重启。高概率根因是“冷缓存回源 × 10 路 Roxy/Chromium 并发”，共享静态缓存被清空只是触发条件。

## 原因分级与证据核对

### 高概率：10 路可视 Roxy Profile 冷启动压垮渲染器/桌面合成

- `注册日志/63e23035-8f24-4c55-967e-717f155933c8.log` 至 `注册日志/6655a759-1adf-4fea-95bf-7354c4dba14f.log` 在 20:21:12–20:21:13 同时出现 `reg-worker-1_0`…`reg-worker-1_9` 和 Job 1–10；当前 UI 默认 `regWorkersV2` 为 10，服务层默认线程池也是 10。
- 本次缓存目录在任务后只有 76 个文件、38 个 metadata/body 对、17,088,293 bytes，条目保存时间集中在 20:21:27–20:22:04，说明清理后发生了冷启动重建。Job 4 记录 `cache_misses=38, cache_writes=38, downloaded=5,830,521`；Job 3 随后记录 `cache_hits=38, cached=17,112,064`，与“首批回源、其他 Profile 同时加载”的时序一致。
- 既有负载报告记录过同类峰值：10 个注册任务对应约 90 个 `RoxyChrome` 子进程、约 7.9 GB Roxy 工作集；批次收敛后 `RoxyChrome=0`、Roxy 相关工作集约 0.99 GB。当前 `.env` 还保持 `ROXY_OPEN_HEADLESS=False`、`ROXY_STATIC_CACHE=True`、`ROXY_TRAFFIC_CAPTURE=True`，因此每个 Profile 都承担可视渲染和性能日志采集。
- Windows 证据：`Microsoft-Windows-Kernel-Power` Event 41 at 20:24:27，`EventLog` Event 6008 at 20:24:33；20:32:27 WebUI 才重新启动，随后恢复线程因 Roxy API 暂不可用记录 `WinError 10061`。未发现同时间段 BugCheck 1001 或 minidump，表现为系统停止响应后的硬重启，而非 Python 异常退出。

### 中概率：冷启动时每个 Profile 独立回源，重复抓取公共 JS/CSS

- 修改前 `core/browser_traffic.py::RoxyTrafficOptimizer._on_request_paused` 没有跨 Profile miss 合并；共享缓存为空时，多个 Profile 会同时为相同公共资源走 `Fetch.continue_request(..., intercept_response=True)`，并各自解码、写入和采集响应体。
- 这会放大网络、磁盘和 renderer 内存峰值，但不涉及 Cookie、Authorization、Session、Token 或 TOTP Secret 共享；现已在公共资源范围增加最多 8 秒单飞等待，认证、挑战、Sentinel 和业务 API 仍保持实时网络。

### 中概率：Roxy 生命周期接口超时放大残留进程

- Job 3 在 20:22:51–20:23:22 记录 `/browser/close`、`/browser/delete` 15 秒超时；重启后的 20:32:40–20:33:03 又出现多次 `WinError 10061`，说明 Roxy 管理 API 与任务清理存在独立阻塞。
- 这些日志发生在注册页面已运行之后，是卡死后的清理/恢复后果，不是 OTP 或密码路径把电脑锁死的证据；本次优化先减少重复冷回源，保留现有生命周期重试边界。

### 低概率：邮箱 OTP、密码、2FA 或 Cloudflare

- Job 3 已取得 accessToken 并完成 MFA enroll/activate；Job 4 的 `totp_enroll` 失败发生在 20:23:12，属于单账号业务失败，不能解释 20:24 的整机重启。
- 其他 `live-check-*` 日志里的 Cloudflare 403 属于查活网络路径，与本次注册批次分开；没有证据表明它们触发系统级卡死。

## 修复

- `core/browser_traffic.py::StaticResourceCache` 新增 8 秒目录级 miss coordinator；`RoxyTrafficOptimizer._on_request_paused` 只对严格公共、已校验 JS/CSS 等待同 URL 首个回源结果，超时自动实时回源。
- 未修改注册 workers、并发输入、Profile 数量、代理、Cookie、Token、密码或 MFA 顺序；上游锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 Roxy `open_profile(args)` 和 `Network.enable({})` 对照已完成，本地修改的是缓存回源负载路径。

## 验证

- 基线定向：`.\\venv\\Scripts\\python.exe -m pytest -q tests/test_webui_gptmail.py tests/test_webui_cloudflare.py tests/test_webui_helper_regressions.py tests/test_browser_cache_service.py` → `53 passed in 19.81s`，退出 0。
- 修改后定向：`.\\venv\\Scripts\\python.exe -m pytest -q tests/test_browser_traffic.py tests/test_webui_gptmail.py tests/test_webui_cloudflare.py tests/test_webui_helper_regressions.py tests/test_config_defaults.py tests/test_browser_cache_service.py` → `91 passed in 24.58s`，退出 0。
- 修改后全量：`.\\venv\\Scripts\\python.exe -m pytest -q` → `741 passed, 16 subtests passed in 81.05s (0:01:21)`；编译命令 `.\\venv\\Scripts\\python.exe -m compileall -q config core webui tests` 输出 `COMPILEALL_OK`，均退出 0。

## 结论

清理缓存本身没有改坏账号数据；它移除了下一批可复用的公共资源，使用户输入的多路可视 Roxy 冷启动同时回源。现在公共资源 miss 只做有界单飞合并，注册 workers 和 Profile 并发完全保留用户输入；冷启动后的 Roxy 工作集、子进程数和系统事件应继续复核。
