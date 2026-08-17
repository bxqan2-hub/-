# AliasHub 部署与合并边界

## 当前部署

- 本站：Python + Flask，默认 `127.0.0.1:5000`。
- AliasHub：React/Vite + Node/Express + SQLite，独立部署在 `C:\Users\Administrator\Desktop\Aliashub`，默认 `127.0.0.1:4180`。
- 注册服务：FastAPI + React + SQLite + Camoufox/Playwright，位于桌面 Aliashub 的 `registration-worker`，默认 `127.0.0.1:8000`。
- AliasHub 主库：桌面 Aliashub 的 `data/outlook-alias-hub.db`。
- 注册服务主库：桌面 Aliashub 的 `registration-worker/data/account_manager.db`。
- 密钥与管理员口令：桌面 Aliashub 的 `.env`。备份时必须与 `data` 一起备份，禁止提交到 Git。
- 本地安全补丁：注册服务求解器显式监听 `127.0.0.1:8889`，避免 Windows 原生部署时暴露到局域网。

Windows 启停入口位于 Aliashub 项目根目录：

```powershell
.\一键启动.bat
.\查看运行状态.bat
.\一键停止.bat
```

## 两个项目的职责

| 领域 | 本站 | AliasHub / 注册服务 |
| --- | --- | --- |
| 注册任务调度 | 现有线程池及多驱动流程 | FastAPI 注册任务 API |
| Outlook 素材 | 根目录 JSON/TXT 池 | `source_accounts`、`addresses`、别名及收件箱 |
| 注册结果 | 根目录 JSON/TXT、Codex 导出目录 | 注册服务账号表，AliasHub 任务镜像 |
| 支付链接 | 本站现有集成 | 不迁移，仍由本站负责 |
| 邮件验证码 | 本站邮箱适配器 | AliasHub 的 Graph/Gmail/iCloud/取件链接接口 |

## 推荐合并方式

先保持三个进程和两个 SQLite 库独立，不直接复制表或让两个服务同时写同一文件。合并采用 API 适配层：

1. 本站增加 `AliasHubClient`，只负责健康检查、提交任务、查询任务和拉取账号。
2. 以 AliasHub 的外部任务 ID、外部账号 ID 做幂等键，本站保存映射，不按数组序号关联。
3. 邮箱和别名由 AliasHub 作为唯一写入方；本站通过 API 获取可用地址和验证码。
4. 注册结果由适配层规范化为本站账号字段，再调用本站现有持久化函数，禁止直接写 AliasHub SQLite。
5. 第一阶段只在本站增加“AliasHub 注册”入口和新标签页；稳定后再统一导航和登录。

建议的数据映射：

| 标准字段 | 本站字段 | 注册服务字段 |
| --- | --- | --- |
| 邮箱 | `email` | `email` / `login_identifier` |
| Access Token | `access_token` | credential `access_token` |
| Refresh Token | `refresh_token` | credential `refresh_token` |
| 外部账号 ID | 新增 `aliashub_account_id` | `id` |
| 外部任务 ID | 新增 `aliashub_task_id` | `task_id` |
| 来源 | 新增 `registration_source=aliashub` | 固定值 |

## 不能直接合并的原因

- 本站是 MIT；AliasHub 和其注册服务是 AGPL-3.0。复制或深度混合其代码后，对外提供网络服务通常会触发 AGPL 的源码提供义务，需要先确认发布策略。
- 本站的注册池主要由 JSON/TXT 文件维护，AliasHub 使用关系型模型和数据库迁移；直接共库会破坏双方升级能力。
- AliasHub 已经通过 `REGISTRATION_SERVICE_URL` 和 Bearer token 把主站与注册服务解耦，继续沿用 HTTP 边界最稳妥。

## 后续实施顺序

1. 完成 AliasHub 邮箱账号配置并做一次手工注册验收。
2. 在本站实现只读健康状态和打开 AliasHub 的入口。
3. 实现任务提交、轮询、取消的 API 适配器。
4. 实现注册结果的幂等同步和失败重试。
5. 最后再决定是否统一界面、账号权限和部署域名。
