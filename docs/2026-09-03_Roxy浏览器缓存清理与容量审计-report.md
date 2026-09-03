# Roxy 浏览器缓存清理与容量审计（2026-09-03）

## 当前容量

本次通过 `core.browser_cache_service.cache_status()` 读取本机缓存，未删除任何真实缓存：

| 范围 | 容量 | 文件/目录 | 清理策略 |
| --- | ---: | ---: | --- |
| Roxy `browser-cache` Profile 存储 | 约 1.28 GiB | 约 5,317 个文件 | 只清理已从官方 Profile 列表移除的孤儿目录 |
| 可回收 Profile 网页缓存 | 约 276.8 MiB | 约 2,077 个文件、98 个目录 | “清理 Profile 缓存”按钮处理 |
| Roxy 管理器 `Cache` | 约 67.2 MiB | 427 个文件 | “清理 Profile 缓存”按钮处理 |
| 注册共享公开 JS/CSS 缓存 | 约 765.6 MiB | 5,628 个文件 | “清理共享 JS/CSS”按钮单独处理 |

此前可回收网页缓存约 **344 MiB**，全部相关存储约 **2.06 GiB**。当前 Profile 列表核对显示 0 个已登记 Profile、12 个本地孤儿目录，因此 Profile 清理按钮现在还会回收约 1 GiB 的孤儿 Profile 运行数据。Profile 存储中的运行时 DLL/WASM、Cookies、Local Storage、指纹和代理数据只会在目录已被官方 API 移除且没有运行进程时随孤儿目录一起删除。

## 安全边界

- 清理前要求注册任务状态为 0 个 `pending/running/stopping`。
- 清理前要求 `RoxyChrome` 和 `chromedriver` 为 0 个；Roxy 主程序可以继续运行，锁定文件会被跳过并报告部分清理。
- Profile 清理先读取官方 `/browser/list_v3`，只删除名称符合 Profile ID 格式且不在官方列表中的本地目录；已登记 Profile 只清理固定网页缓存子项。
- 共享 `data/browser_static_cache` 由旁边的独立按钮清理；默认 Profile 清理按钮不会触碰它。
- 不调用 cloud cache 清理，不触碰账号 Cookie、Session、Token、TOTP、指纹、代理或 Profile 身份数据。

## 修改路径

- 新增服务：`core/browser_cache_service.py`
  - `cache_status()`：容量、活动任务、浏览器进程和可清理范围
  - `clear_cache()`：Profile 列表核对、孤儿目录清理、固定网页缓存清理和结果汇总
  - `clear_shared_static_cache()`：单独清理共享公开 JS/CSS 缓存
- 新增接口：`webui/app.py`
  - `GET /api/roxy/cache/status`
  - `POST /api/roxy/cache/clear`，要求 JSON `{"confirm": true}`
  - `POST /api/roxy/cache/clear-shared`，要求 JSON `{"confirm": true}`
- 新增 UI：`webui/templates/index.html`
  - 配置页顶部显示缓存容量和可清理容量
  - “清理 Profile 缓存”和“清理共享 JS/CSS”两个相邻按钮，分别带确认、禁用状态、清理中状态和结果提示

## 上游对照

Roxy 官方 API 提供 `/browser/clear_local_cache`，并区分 `partial`、`all`、`cloud` 清理级别；其中 `partial` 保留扩展、登录状态、指纹和 IP。本地 Profile 按钮先通过官方 `/browser/list_v3` 核对目录是否已被删除，再对孤儿目录做本地清理；共享公开 JS/CSS 由独立按钮处理。[Roxy API 文档](https://roxybrowser.com/docs/api-documentation/api-endpoint.html)

上游锁定 commit 仍为 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`；上游 `browser_traffic.py` 的公开静态缓存边界未被本次清理功能放宽或复用。

## 验证

- 服务测试覆盖：容量分类、共享静态缓存保留、Cookie 保留、活动任务阻断、活动浏览器进程阻断。
- WebUI 测试覆盖：状态接口鉴权、清理确认边界、清理路由转发和页面按钮存在。
- 当前真实状态：活动任务 0、活动 `RoxyChrome` 0、官方 Profile 列表 0、本地孤儿 Profile 12 个；Profile 清理可回收约 1.00 GiB，共享缓存可单独回收约 765.6 MiB。
