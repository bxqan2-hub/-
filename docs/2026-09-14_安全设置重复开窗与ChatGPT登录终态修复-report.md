# 安全设置重复开窗与 ChatGPT 登录终态修复（2026-09-14）

## 范围与运行证据

- 用户点名账号的当前 ID 为 88；邮箱只用于本地身份匹配，不写入报告。该账号来自此前 Job 770，首次注册成功但补密码邮件等待失败。此次仅处理该账号独立安全设置，不再批量注册。
- 本轮基线 HEAD `3fb690a`，5002 listener PID 24372 持续运行。启动稳定性修复与此次登录状态机问题分开记录。
- 19:59:52 实际安全任务 `mode=add`，本地无确认密码/TOTP。第一次 20:00:45 收到 OTP，20:00:56 页面回到 `https://chatgpt.com/` 且 helper 记录 `accepted`，20:01:42 却报 browser_login 失败并删除环境。
- 第二次 20:02:44 收到 OTP，20:02:53 同样回到 ChatGPT 根页并 accepted，20:03:39 再失败重开。第三次由用户停止。前两次均未进入补密码、MFA enroll 或 activate。

## 先分级，再归因

| 优先级 | 假设 | 证据与结论 |
| --- | --- | --- |
| 高 | 普通登录误套 Codex OAuth 的完成条件 | 确证：_complete_login_after_email 只识别 OAuth callback、auth.openai.com 的手机号/consent/workspace；ChatGPT 根页落入非 auth origin 等待分支，45 秒后抛错，吻合两次时序 |
| 高 | 所有异常都被当作需重开浏览器的瞬态故障 | 确证：安全 worker 的登录循环对任意 Exception 都清理并再开 Profile，最多 3 次，放大完成判定错误 |
| 中 | 代理/浏览器连接问题 | 第二轮创建前有候选 TLS 失败但已换候选恢复；之后成功进入验证码并回到首页，不是 accepted 后固定 45 秒失败的主因 |
| 低 | 邮箱收不到码、密码写入或 MFA 激活失败 | 两次均真实取码并 accepted；密码/MFA 尚未执行，故本次错误不属于远端安全设置写入失败 |

## 上游与边界

- 修改前重读 UPSTREAM_PROJECT_INDEX，仍锁定 Torin-x/GPT-utral-platform `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`。
- 本轮 raw 对照：roxy_codex_oauth.py HTTP 200 / SHA-256 `8a6b2ab48bac00b3fbcbff84edfd9ad5a27661d047b99607806420d6de16015d`；account_export.py HTTP 200 / `985b99e669711d3de50dd6072ba83f660ac74d5ea156bf84f829c8fb258d0547`；account_security_service.py 为 404，属于本地扩展。
- 上游 OAuth helper 也按 callback/phone/consent 等授权状态推进，不应把普通 ChatGPT 首页加入所有 OAuth 流程的成功条件。本地沿已存在 auth_url 传递入口语义，不增加开关或第二套登录器。
- 普通登录首页只意味着可交给调用方读 Session；目标邮箱、同窗 Cookie、显式 Token 仍严格核对后才允许密码/MFA。密码仅确认成功后保存；Secret 仅 enroll/activate 成功后保存；只读 Token 失败与写入失败分开。

## 修复与验证

### Finding → Path → 修改

1. **普通登录的完成目标错误**：原 `_fill_email_and_otp` 已有 auth_url，本次将它透传给 `_complete_login_after_email`，并由唯一 `_is_chatgpt_login_return` 严格验证普通 `/auth/login` 入口与 `/` 回跳。HTTPS、精确 hostname、默认 443、无 userinfo/query/fragment/params；首次/重开已登录页面共用该判定。OAuth 默认调用仍不接受 ChatGPT 根页。
2. **无差别重开与诊断丢失**：原安全 worker 只对既有 proxy transport helper 确认的传输错误或明确失效 WebDriver 类型保留有限重试；邮箱异常、语义错误、账号终态直接清理停止。使用既有 TwoFASetupError 记录固定 stage/code，不复制原异常正文。退避复用既有可取消 wait，及时清理 active context。
3. **旧 Token 掩盖新 Session 缺凭据**：登录后必须从此次同窗 Session 明确取得 Token，删除旧数据库 Token 回退。匹配邮箱但无新 Token 的回归先复现越过门禁，修复后零密码/MFA 写操作；数据库旧值不因此删除，也不会拿它代替此次身份校验。

### 离线回归

- 普通 ChatGPT 终态红测 23 failed / 15 passed；修复后该文件 38 passed，扩大 Codex/安全覆盖 101 passed。
- 无差别重开红测 15 failed / 25 passed；补充 Token 空值与传输恢复测试后安全扩展文件 43 passed。
- 合并 11 个相关测试文件：521 passed、134 subtests、1 个既有 requests 依赖 warning，21.21 秒；compileall 与 diff 检查通过。

### 指定账号实测

- 原 5002 重启为 PID 71412，20:17:51 经原 WebUI API 返回 HTTP 202 启动同一账号的一次 add 模式安全任务；没有新增注册或其他账号测试。运行 manifest 保存两个生产文件的 hash，结束后逐一核对一致。
- 20:18:01 创建唯一 Profile；20:18:55 OTP accepted，同秒记录 login_session_pending，20:18:56 同窗 Session 返回 Token 并严格匹配目标邮箱。交接约 1 秒，未再出现旧 45 秒等待及第二/第三次重开。
- 20:19:34 密码确认完成并保存；同秒密码重认证后的 Session/Cookie 同步。20:19:37 MFA enroll HTTP 200，20:19:41 activate HTTP 200 后保存激活凭据。
- 20:19:44 DB 状态 success/complete，password_done/totp_done 均 true；事后按邮箱身份核对，密码、TOTP Secret、Token 均存在。最终信息没有 Token 只读校验失败附注；依据现有成功/失败记录逻辑判断校验没有抛错，日志不含单独的正向校验行，不虚构该行。
- 20:19:49 关闭、20:19:50 删除同一 Profile，创建/关闭/删除各一次。20:20:54 复核 source hash 一致、账号状态保持成功、5002 仍由同一 PID 监听。
- 脱敏实测证据见 run/security-homepage-live-test-20260914.json 与 run/security-homepage-live-result-20260914.json（忽略目录），账号具体凭据通过既有账号页读取，不输出到报告。

## 交付自检（R1–R8）

1. **已有代码修改**：`core/roxy_codex_oauth.py::_complete_login_after_email`、`_fill_email_and_otp`；`core/account_security_service.py::_run_security_setup`；相应现有测试及上游索引。
2. **新增代码及替代**：新增 `_is_chatgpt_login_return`，替代对普通 ChatGPT 回跳未判定的缺口；检索已有 `_auth_origin`/trusted URL helper 后确认其范围过宽，未另建登录器。新增测试：`test_chatgpt_login_root_hands_off_to_session_check`、`test_chatgpt_root_handoff_rejects_other_targets`、`test_chatgpt_otp_acceptance_uses_real_login_transition`、`test_existing_chatgpt_login_without_email_input_is_target_scoped`、`test_security_worker_retries_only_confirmed_browser_transport_failures`。旧无目标调用、无差别重开和旧 Token 回退均删除。
3. **搬迁项**：无；不涉及源文件搬迁，没有备份工程或平行运行路径。
4. **新增配置链路**：无新配置、UI、环境变量或开关。已有安全入口 → `_fill_email_and_otp(..., auth_url)` → `_complete_login_after_email(..., auth_url=auth_url)` → 精确回跳判定 → worker 的 Session 邮箱/Token 校验 → 原密码/MFA helper。新 helper 参数传递现有业务目标，并非新旧双实现开关。
5. **死引用回扫**：以下两条输出为空；没有保留旧休眠/未传目标调用。

   ```powershell
   rg -n -F '_complete_login_after_email(driver, email)' core/roxy_codex_oauth.py
   rg -n 'time\.sleep|account_security_service\.time' core/account_security_service.py tests/test_account_security_extension.py
   ```

6. **Diff 统计**：代码/测试四文件 `+297 / −24`；含索引和本报告共 `+382 / −24`。生产仅一条现有登录/安全设置链路。
7. **测试最后五行**（`run/security-homepage-combined-tests-20260914.log`）：

   ```text
   ........................................................................ [ 55%]
   ....................................................................................................... [ 75%]
   ........................................................................................................................... [ 98%]
   .......                                                                  [100%]
   521 passed, 1 warning, 134 subtests passed in 21.21s
   ```

8. **未做 / 存疑**：不把本例推广为所有邮箱或代理问题已经解决。当前严格匹配不接受带 query/fragment 的未观测 ChatGPT 根回跳，避免猜测成功终态。既有“凭据已完整时跳过重复设置”的 reset 语义与 Codex 同邮箱停止标记的跨域问题单列，不作为本例根因；本轮目标无保存密码/TOTP、实际 mode=add，且重启后旧进程内停止标记已清。没有扩大为批量注册或改动其他账号。

## 运行证据

- 脱敏基线与上游 hash 位于忽略目录 run；账号日志继续在既有运行目录维护，不复制为回滚备份。
- 原始邮箱、取件 URL、密码、OTP、Token、TOTP Secret 不进入 Git；未改三个接管前未跟踪的支付路径。
