# Roxy 缓存回放计费波动修复（2026-09-14）

## Finding

10 路批次的 `Meta` 网卡增量为 329,415,289 B；账号摘要中的
`cache_saved_bytes` 合计为 329,463,539 B，差值仅 48,250 B（0.015%）。
同批 `downloaded` 合计只有 20,172,227 B。大幅扣费来自共享静态缓存的
`Fetch.fulfillRequest(body=...)` 回放，而不是注册 API 本身。

Roxy 浏览器由管理端启动，CDP Fetch 回放的 body 会经过管理浏览器传输链路，
该链路落在 `Meta` 代理适配器计量范围内，所以“缓存命中”在账号摘要中显示为
节省字节，却在余额中产生等量流量，形成瞬时约 100 MB 波动。

另外，旧版 `Network.enable` 将事件缓冲限制为 2 MiB 总量/512 KiB 单资源，
导致 `Network.loadingFinished` 被截断，账号 `downloaded` 被系统性低估。

## 修复

- `config/roxybrowser.py` 与工作区 `.env` 默认关闭跨 Profile 的共享 Fetch 缓存回放。
- `core/browser_traffic.py` 恢复 `Network.enable({})`，不再截断计量事件流。
- 低流量拦截、独立 Profile、代理出口、Cookie/Token/密码/2FA 边界保持不变。
- 共享缓存文件不删除；待缓存服务部署到 Roxy 网络内后再显式开启。

## 验证

- `tests/test_browser_traffic.py`：`41 passed, 204 subtests passed`。
- 注册/OTP/Session/本地代理回归：`102 passed, 26 subtests passed`。
