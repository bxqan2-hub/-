# Roxy 十账号流量根因复测（2026-09-14）

## 结论

本轮 642–651 使用 `static_cache=False`、`low_traffic=True`、并发 10，代理 Meta 隧道增量约 **398.1 MB**（下行 246.4 MB、上行 151.7 MB）。账号侧性能日志仅观测到 **98.3 MB**（下行 73.5 MB、上行 24.8 MB），仍有约 **299.8 MB** 未被 Selenium 性能事件覆盖，远高于教程要求的 20–30 MB/10 账号。

## 证据与定位

- 每个成功账号仍下载约 5–11 MB 的 ChatGPT 公共 bundle；静态缓存命中关闭后 `cached=0/hits=0`，因此本轮异常不是缓存复用口径造成。
- `Roxy` `/browser/open` 在 Selenium 连接前通常等待约 40–50 秒；该窗口启动阶段发生的代理流量没有账号级 CDP 事件，属于主要未计量区间。
- Meta 隧道上行在并发 10 时出现约 151.7 MB，远高于浏览器 postData 观测值，说明代理/浏览器启动阶段存在重试或控制面开销，需继续按并发 1 与 Roxy open 生命周期单独复测。

## 已实施

- `core/browser_traffic.py::block_reason` 与 `RoxyTrafficOptimizer._on_request_paused`：Session 建立后仅放行 ChatGPT auth/session/callback 文档及 API，阻止应用壳与后台轮询，避免认证完成后的二次 bundle 下载。
- `stop-webui.bat`：处理“进程在枚举后自行退出”的竞态，避免启动脚本因 `Stop-Process` 偶发找不到 PID 而中止。

## 下一轮验证

1. 以并发 1 运行 10 个新邮箱，分别记录 `/browser/open` 前后 Meta 增量。
2. 若单账号仍超过 3 MB，继续拆分 Roxy 启动阶段与 ChatGPT 页面阶段；在确认启动开销后再调整 Roxy `args`，不关闭安全挑战域名。
3. 代理商账单以 Meta 隧道双向字节为准，浏览器 `downloaded/observed` 仅作定位指标。
