# 本项目部署说明

来源：wangshen233/roxy-unlimited-windows，锁定
`81f7da873fd0a3a9550ec769d2b37e26e28f2715`。保留原 LICENSE 和上游文档；
README/MANUAL/TECHNICAL 中的接口示例代表上游，本站差异以本文为准。

## 唯一运行入口

`scripts/roxy-api.mjs`，由本站 `RoxyBrowserClient.request → ensure_local_api`
按需隐藏启动。Node.js 22+，零 npm 依赖，固定监听 `127.0.0.1:50001`。
不运行 tools 中的二进制修补工具，也不修改官方 App、内核或账号额度数据。

开关 `ROXY_LOCAL_COMPONENT` 默认 False，本机部署时启用。官方配置原样保留，
关闭即使用官方 API。这是用户要求的长期双后端选择，不是待删除的迁移分支。

## 已审查的本地补丁

- `scripts/paths.mjs:resolveRoxyPaths` 接受 `profileDir`；内核仍来自 Roxy 安装目录，
  Profile/temp 分别在本站 `data/roxy_local/browser-cache`、`data/roxy_local/temp`。
  `--profile-dir` 由客户端内部传入，不新增 UI 路径配置。
- `scripts/fingerprint.mjs:profileDir/parseProxy/coreExe/coreVersion/createProfileOnDisk`：
  ID/路径及链接校验；URL 解析保留代理密码中的冒号、@ 和 IPv6；socks5h 归一为 socks5；
  从已安装目录选择指定真实内核；构建失败前不创建目录；默认使用干净骨架，
  不从最新官方或其他账号 Profile 继承代理、Cookie 或扩展状态。
- `scripts/roxy-api.mjs`：使用 URL 解码后的脚本路径；健康响应标记组件和 Profile 根；
  写接口只收 POST JSON，禁止 Origin 请求并校验 Host，不开放跨站 CORS；
  Node 版本检查；并发打开合并；启动失败回收；关闭等待子进程结束；
  删除同时清理该 Profile 的噪声扩展临时目录。
- 本站原 `create_profile/open_profile` 将 proxyInfo、启动效率参数、headless
  转成上游本地协议。任务中的 `local-<dirId>` 只作后端标记，API/目录仍使用 32 位 hex。
  `close_profile/delete_profile` 依据 ID 所属后端路由，避免热切换后清理错窗口。
- 上游 canvas/audio 噪声扩展和 CDP 注入保留；与此前删除的本站通用页面属性注入
  helper 不是同一实现。代理注册新增 country/timeZone 的内部透传：复用已有出口
  预检（包括显式本地代理），ICU/CLDR 匹配国家的默认语言，实际 IANA 时区优先；
  Accept-Language 单语言配置与官方 GB 实测一致。国家/时区缺失或时区无效即报错，
  不新增另一套 GeoIP 请求或地区配置。直连手工 API 调用保留上游 preset 默认。

## 边界与维护

- 注册、OTP、密码、MFA 状态机保持原路径。密码只在成功终态落 checkpoint；
  TOTP Secret 只在 enroll/activate 确认后落盘；邮箱匹配、同窗 Cookie/代理及
  Token 显式透传不变。创建失败与邮箱/安全步骤失败仍分开记录。
- 切换不会停止既有窗口。本地后台服务独立于 WebUI 生命周期；手工重启该服务前
  先关闭其窗口。服务重启后的运行中窗口接管不在本轮实现范围。
- 原 upstream LICENSE 的用途限制仍然保留；vendored 文件不改变其授权。
- 原始上游工具保留作来源资料，不作为本站的第二个启动器。所有运行日志、Profile、
  凭据均由项目 `.gitignore` 的 `data/`、`logs/`、`.env` 规则排除。
- 更新前查看 `../UPSTREAM_SOURCES.md` 和 `../upstream-lock.json`，对照固定上游差异，
  运行 local component、Roxy、config、cache、GC、stop、WebUI 和密码/MFA 回归。
