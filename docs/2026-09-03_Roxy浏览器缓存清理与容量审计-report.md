# Roxy 浏览器缓存清理与容量审计（2026-09-03）

## 当前容量

本次通过 `core.browser_cache_service.cache_status()` 读取本机缓存，未删除任何真实缓存：

| 范围 | 容量 | 文件/目录 | 清理策略 |
| --- | ---: | ---: | --- |
| Roxy `browser-cache` Profile 存储 | 约 1.28 GiB | 约 5,317 个文件 | 只清理其中可回收网页缓存目录 |
| 可回收 Profile 网页缓存 | 约 276.8 MiB | 约 2,077 个文件、98 个目录 | 清理 |
| Roxy 管理器 `Cache` | 约 67.2 MiB | 427 个文件 | 清理 |
| 注册共享公开 JS/CSS 缓存 | 约 765.6 MiB | 5,628 个文件 | 保留，避免下一批注册重新冷启动下载 |

可回收总量约 **344 MiB**，全部缓存存储约 **2.06 GiB**。Profile 存储中的运行时 DLL/WASM、Cookies、Local Storage、指纹和代理数据不属于可回收网页缓存，按钮不会删除它们。

## 安全边界

- 清理前要求注册任务状态为 0 个 `pending/running/stopping`。
- 清理前要求 `RoxyChrome` 和 `chromedriver` 为 0 个；Roxy 主程序可以继续运行，锁定文件会被跳过并报告部分清理。
- 只删除固定缓存目录的子项，保留缓存根目录和 Profile 目录。
- 共享 `data/browser_static_cache` 只读盘点，不由该按钮清理；它仍只包含已校验的公开静态资源。
- 不调用 cloud cache 清理，不触碰账号 Cookie、Session、Token、TOTP、指纹、代理或 Profile 身份数据。

## 修改路径

- 新增服务：`core/browser_cache_service.py`
  - `cache_status()`：容量、活动任务、浏览器进程和可清理范围
  - `clear_cache()`：活动检查、路径边界检查、固定缓存目录清理和结果汇总
- 新增接口：`webui/app.py`
  - `GET /api/roxy/cache/status`
  - `POST /api/roxy/cache/clear`，要求 JSON `{"confirm": true}`
- 新增 UI：`webui/templates/index.html`
  - 配置页顶部显示缓存容量和可清理容量
  - “清理缓存”按钮带确认、禁用状态、清理中状态和结果提示

## 上游对照

Roxy 官方 API 提供 `/browser/clear_local_cache`，并区分 `partial`、`all`、`cloud` 清理级别；其中 `partial` 保留扩展、登录状态、指纹和 IP。本地按钮采用更窄的固定目录清理，避免把 Profile ID 列表、Cookie 或服务器缓存交给清理操作。[Roxy API 文档](https://roxybrowser.com/docs/api-documentation/api-endpoint.html)

上游锁定 commit 仍为 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`；上游 `browser_traffic.py` 的公开静态缓存边界未被本次清理功能放宽或复用。

## 验证

- 服务测试覆盖：容量分类、共享静态缓存保留、Cookie 保留、活动任务阻断、活动浏览器进程阻断。
- WebUI 测试覆盖：状态接口鉴权、清理确认边界、清理路由转发和页面按钮存在。
- 当前真实状态：活动任务 0、活动 `RoxyChrome` 0、可清理约 344 MiB。
