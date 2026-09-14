# 注册失败与邮箱 API 续查（2026-09-14）

## 范围与接管基线

- 接管任务：`01a09a6e-8923-7e23-8430-f83f3557aebb`，原任务标题“排查注册流量压缩异常”。
- 流量异常已由用户验收；本次延续其最后要求：分析已结束的注册失败，修复已证实缺陷，再用十个新邮箱复测。省流参数、缓存、独立 Profile 和随机指纹保持原实现。
- 接管基线：`7b896f327aa84130e36acea6f1167b893353461f`，分支 `codex/password-2fa-extension`。原窗口留下三个未提交代码/测试文件，本次继续核验而非另建实现。
- 原批次当前留存 19 条任务（737–756，745 不在留存记录中），7 成功、12 失败。统计只覆盖留存记录，不补造缺失任务结果。

## 先分级，再核验证据

| 优先级 | 假设 | 核验结论 |
| --- | --- | --- |
| 高 | 一次邮箱 API 瞬态超时被当成连续失败 | **证实**：740 的 `mail_detail`、751 的 `mail_list` 各一次 5 秒超时，`attempts=1` 即终止 |
| 高 | 等码期间浏览器终态被 provider 回调吞掉 | **代码审计证实缺口**；本批八个空邮箱的日志没有证明发生该状态，故不将它作为八例的确定根因 |
| 中 | 时间过滤、列表/详情解析漏取已有邮件 | 740、751 的现有 parser 均能识别；其余八个网页与 API 均为空，未发现漏取证据 |
| 低 | 代理、密码、2FA 被混记成取码失败 | 两个 `proxy_isolation` 独立列账；本批证据未指向密码或 MFA 为这十个取码失败的原因 |

### 已有邮件却提前失败：740、751

| Job | 请求失败点 | 邮件收到时间（本地） | 任务结束 | 邮件领先结束 | 事后解析 |
| --- | --- | --- | --- | --- | --- |
| 740 | `mail_detail / ReadTimeout` | 16:26:03 | 16:26:17 | 14 秒 | 找到 OTP |
| 751 | `mail_list / ReadTimeout` | 16:27:39 | 16:28:00 | 21 秒 | 找到 OTP |

这证明“服务当时永远收不到码”的结论不成立；也不等于反推当时每个 API 请求都正常。真实缺陷是 yangyang 列表/详情路线缺少普通 GET 路线已有的单次短重试。

### 八个空邮箱：742、744、747、750、753、754、755、756

- 16:32 的取件 API 返回 HTTP 200、`items=[]`。
- 本次 16:39 再核对网页及其对应列表 API：两者仍均为 HTTP 200、0 封，`has_more=false`。网页确实引用相同列表/详情 API，不存在本次代码独自访问另一套邮箱的证据。
- 日志只证明提交邮箱并进入 OTP 页面，没有发信服务的成功回执。现有证据尚不足以区分发信端未投递、邮件传输延迟和邮箱供应端未同步；不得把空列表直接写成“OpenAI 未发信”或“解析器有缺陷”。
- 756 在 25 秒预算尾部另有 `mail_list / ReadTimeout`，属于等码预算耗尽时的最后错误，不与 740、751 的提前失败混算。

### 两个独立代理失败

- 741：预检与窗口内出口不同，`proxy_isolation` 按既有要求终止。
- 749：窗口内两次 `browser_exit_probe` 均为 `TimeoutException`，未形成有效出口确认。
- 本次不放宽出口漂移/探测失败边界，不把它们归入邮箱故障，也不更改代理池或指纹生成逻辑。

## Finding → Path → 修复

1. **F1：yangyang 瞬态错误提前退出**
   - Path：`core/generic_api_mail_client.py::fetch_latest_otp` → `_fetch_yangyang_otp`。
   - 在原路线内使用现有主请求/重试预算；仅对 `retryable` 错误短暂停顿后重试一次，两次均失败才计为一轮错误。
   - 列表和详情继续共享单次遍历预算；重试重新计算总剩余预算。取消及总预算耗尽后不再发请求，保留原始失败阶段。认证等终态错误直接上抛。
   - 旧邮件时间门禁、已拒绝码/消息 ID 过滤、日志脱敏不变；没有新增参数、环境变量或并行 provider。
2. **F2：OTP 页面状态与邮箱等待的错误传播缺口**
   - Path：`core/roxy_registration.py::_otp_flow_advanced_state`、`run_roxy_registration` 的既有 OTP 等待循环。
   - 等待期间识别明确账号删除/停用终态并停止。通过原 `should_stop` 回调保存状态/异常，在 provider 返回或抛错后恢复，避免回调异常被 provider 吞掉后变成邮箱超时。
   - 已进入资料页、登录完成或 Email verified 的状态按原完成分支处理；回退邮箱登录页交回既有页面恢复分支，不被邮箱快速失败分支抢先截断。
   - 不重试账号删除/停用终态，不增加账号、Profile 或代理切换策略。
3. **F3：错误页路径被误当密码成功终态**
   - Path：`core/roxy_registration.py::_fill_password_page_if_present.confirmed`。
   - 正常提交和输入框消失两条分支都可能在 `/email-verification` 实际显示 Route Error/账号停用时提前返回密码并写 checkpoint。四组离线回归先复现失败，再验证修复。
   - 在既有 `confirmed()` 保存前复用终态检测，阻止错误页写入；不改变密码策略或正常成功页面。
   - 关联调用方 `_fetch_or_recover_chatgpt_session` 的终态检测移出 callback 容错块，避免新异常语义被吞后继续使用旧 Session。新增回归确认终态不读 Session、不进入后台邮箱恢复。
   - 此项来自本轮代码审计，不作为八个空邮箱历史失败的归因。

## 上游对照与密码 / 2FA 边界

- 先读 `docs/UPSTREAM_PROJECT_INDEX.md`，继续锁定 `Torin-x/GPT-utral-platform` commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`；对照其 `vendor/turb_gpt_free_register/core/generic_api_mail_client.py`、`core/roxy_registration.py` 和 `core/account_export.py`，本地证据见忽略目录 `run/upstream-otp-review-20260914.json`。
- 上游普通邮箱轮询没有本地 yangyang 专用列表/详情路线；沿用本地有界请求及脱敏边界，不复制上游正文/OTP 日志。
- 保留邮箱匹配、同一浏览器 Cookie/代理、显式 Token、密码成功终态后 checkpoint、MFA enroll/activate 成功确认后保存 Secret。
- 只读 Token 校验、注册/密码、MFA 写入和辅助套餐查询分别记录。当前改动未调整 `core/account_export.py`。

## 验证与十邮箱复测

- 邮箱专项：72 passed、58 subtests；OTP/Session：103 passed、45 subtests；2FA 全文件：204 passed。均保留既有 requests 字符检测依赖 warning。
- 新短重试使 epoch 浮点预算断言产生约 47.7 ns 舍入误差，`tests/test_twofa_registration.py` 两处测试使用绝对 1 µs 容差，生产预算不变。
- 已跟踪测试全量尝试：初轮 11 failed、1132 passed（包含已修复的浮点断言）；其后 10 failed、1134 passed、432 subtests。剩余失败为旧支付目录存在断言 1 项，以及 HeroSMS 测试读到本地 SMSBower 配置的 9 项；对应业务代码未修改。单设子进程 `SMS_PROVIDER=herosms` 仍受测试中配置重载影响，不声称全仓绿色。
- 最终合并相关回归（16 个已跟踪测试文件，覆盖邮箱、Roxy/Session、密码、2FA、缓存和 WebUI helper）：**591 passed、390 subtests**，21.38 秒；`compileall`、`git diff --check`、替换引用回扫通过。
- 十个新邮箱实测结果在本次运行完成后补入；不以既有流量测试结果替代本轮验证。

## 原始证据与隐私

- `run/registration-failures-user-batch-20260914.json`：旧批次留存状态。
- `run/registration-mail-evidence-20260914.json`：旧批次列表/详情响应元数据、邮件时间与 parser 判断。
- `run/registration-mail-recheck-takeover-20260914.json`：本次网页/API 二次交叉检查，仅保存状态/计数/耗时。
- 详细 job 日志在 `注册日志/`；邮箱、取件链接、密码、OTP、Token、TOTP Secret 和账号结果保留在 `.gitignore` 覆盖的运行数据中，不写入本报告或 Git。
