# 近期失败日志与 OTP 时间水位修复（2026-09-12）

## 范围、现场与原因分级

- 修改前 HEAD：`d4f520d`，当前分支 `codex/password-2fa-extension`；拉取远端后无分歧。
- 实际服务为 `.venv/Scripts/python.exe web.py --host 127.0.0.1 --port 5001`，PID 52812，启动于 2026-09-11 23:35:14。排查时有 16 个注册任务运行，保留该进程及其浏览器，不中断批次。
- 2026-09-12 01:27:40 的只读任务快照：当天创建的 198 条保留记录中，123 success、36 failed、16 running、23 pending。这是当时保留任务的快照，不是完整历史或最终批次成功率。
- 查看日志前初步排序：高概率 OTP 页面/Session 重认证；中概率浏览器生命周期与代理出口；低概率邮箱取件、落盘及 MFA。随后逐项核对如下，不混为单一网络错误。

| 当天失败分类 | 数量 | 核对结论 |
| --- | ---: | --- |
| 邮箱列表未出现时间水位后的新邮件 | 9 | Job 569、633、768 有 Email verified 后重新登录的路径；发现本地 callback 后重置水位缺陷。其他首次收码超时不据此归因。 |
| 末次邮箱请求 ReadTimeout，被记为连续不可达 | 4 | Job 591、600、606、647；保留 mail_list/mail_detail 区别。 |
| 代理候选出口已占用或冷却 | 11 | 已有隔离规则主动终止，不增加候选重试、不释放占用来掩盖冲突。 |
| 账号已创建但 Session/AT 恢复失败 | 5 | 浏览器 Failed to fetch 或 WARNING_BANNER，后续登录收到 403；不是 MFA 写失败。 |
| OTP 提交无接受信号 | 2 | Job 910 是明确的页面 Route Error (500)；Job 615 未见该签名，继续保留 pending 超时语义。 |
| 窗口出口漂移或探测无结果 | 3 | Job 895、920 漂移，Job 882 无有效出口；仍终止，不用预检替代窗口实测。 |
| 密码重认证启动异常 | 1 | Job 568，stage=password_reauth，code=password_reauth_start_failed，内部 exception、无 HTTP 状态；不足以认定 Session 403 或 MFA 失败。 |
| 邮箱提交后的授权中转页停滞 | 1 | Job 915 停在 authorize 中转页，未进入新一次 OTP/MFA；不盲目重放。 |

## Evidence → Finding → Path → 修复

### F1：回调期间收到的新邮件被过晚水位过滤（高概率、代码缺陷已复现）

- 证据：Job 647 的 `注册日志/9043503e-ce6b-46a2-b30a-0f99e4a3c304.log`，44–57 行依次出现 Email verified、callback 恢复、提交邮箱、OTP 页；第二轮邮件轮询随后超时。Job 569、633、768 也有同类回调链路。
- Path：`core/roxy_registration.py::run_roxy_registration` 的五处 Email verified → `_resume_chatgpt_login_callback` → OTP 分支，原来在 callback 返回之后才更新 `otp_after_ts`；`core/generic_api_mail_client.py::_fetch_yangyang_otp` 按此水位过滤旧邮件。
- 最小复现：真实注册入口配合虚拟 driver/邮箱；callback 在 t=1000 发信，t=1010 返回。五分支旧代码均传入 after_ts=1010，错误排除 t=1000 的新邮件。
- 修复：把既有水位赋值移动到每次 callback 调用之前。保持同一 driver、原次数/预算及历史 OTP 排除，不新增配置、执行器或额外重发。
- 证据边界：五分支时间缺陷已离线复现；本轮未查询真实邮箱正文/远端列表，未把上述所有超时都宣称为已证实漏信。

### F2：总预算末尾的单次超时误报为连续不可达（高概率、现场与回放一致）

- 证据：Job 600 (`注册日志/e212d8c1-8411-41ec-9b08-cae7a5c9e8fa.log` 32–44 行)、606 (`注册日志/56cf1b1b-d20f-495f-afdd-d3edc1504082.log` 32–45 行)、647 多次正常轮询，仅最后约 2 秒请求超时，却报 attempts=1 连续不可达。
- Path：`core/generic_api_mail_client.py::fetch_latest_otp` 的 provider 异常与 requests 网络异常两个原有分支。
- 修复：快速失败阈值仅在总 deadline 尚未耗尽时成立；已耗尽则沿原 OTP 等待超时出口，保留最后 stage/type。早期失败仍遵循既有阈值；401/403 永久错误不改为普通等待超时。
- 不延长 max_wait、不改变 after_ts 过滤规则、不新增自动重发。

### F3：认证页 500 被普通 pending 掩盖（已确认）

- 证据：Job 910，`注册日志/179f4cc1-4956-4241-a7f2-d19f6a050d48.log` 41–45 行明确记录 `Route Error (500 Internal Server Error)`，最终却只输出 OTP observed 30s。
- Path：`core/roxy_registration.py::_email_otp_terminal_error`，由输入等待、提交观察、刷新重输和重发准备共用。
- 修复：精确识别 Route Error (4xx/5xx)，输出 `stage=otp_page code=auth_route_error page_status=500` 后停止。这里的 page_status 来源于渲染页，不冒充抓包得到的 POST HTTP 状态。
- 保持未知写结果不重放；不输出页面正文、URL 参数、邮箱、验证码。普通 Try again/invalid 提示不触发新分支，account_deactivated 保持原优先级。

### F4：安全补设重试链丢失结构化错误且输出原始异常（同步审计发现）

- Path：`core/account_security_service.py::_run_security_setup` 原来把密码失败 dict 转成 RuntimeError(message)，最终统一 stage=failed；任意异常正文和 traceback 会进入任务日志。
- 修复：复用 `TwoFASetupError` 与既有错误 payload，保留 stage，并在既有 error 字符串中记录有限 code/http_status；不复制原始异常正文。重试清理、失败落库、停止和最终清理日志仅记异常类型。
- 读取链路：安全 worker 结果 → `db.update_account_security_setup` → `security_setup_stage/security_setup_error` → 账号 API compact 字段 → WebUI 现有错误提示。无新增配置或闲置结果字段。
- 这是注册安全失败后补设入口的审计修复，不冒称当前 36 条注册失败均经过该 worker。

## 上游对照与密码/MFA 边界

修改前重读 `docs/UPSTREAM_PROJECT_INDEX.md`，并获取锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的两个 raw 文件，均 HTTP 200，未创建上游源码副本/备份：

| 固定上游路径（均位于 vendor/turb_gpt_free_register/core） | 字节 | SHA256 |
| --- | ---: | --- |
| account_export.py | 20189 | 985b99e669711d3de50dd6072ba83f660ac74d5ea156bf84f829c8fb258d0547 |
| roxy_registration.py | 187920 | 454a5a84a75613e5617a54491744c2cd9f595d05af64fab824e4de880219ccf5 |

- 上游无本地五分支 callback、水位封装或登录后补密码扩展；上游离开 OTP 页便 accepted，本地继续要求明确资料页/Session/Email verified，不降级这一边界。
- 密码仅成功终态后 checkpoint；MFA 仅 activate `success is True` 后保存 Secret；只读 Token 校验失败与写操作失败独立。
- 目标邮箱匹配、同窗 Cookie/代理、显式 Token、密码后清旧 Token 并重读当前 Session 均保留。独立 Profile、随机指纹请求、出口占用/冷却与漂移终止均未改。
- 安全 worker 回归使用虚拟 Token/Secret/URL，确认失败时不提交 MFA、不保存未确认密码/Secret，诊断不包含测试中的私密值。

## 验证与交付记录

- 红测：callback 五分支失败；邮箱预算分类五个用例失败；Route Error 一项失败；安全 worker 四组错误传播/脱敏失败。修改后对应定向回归通过。
- 已有环境冲突单独复核：`tests/test_extract_center_cleanup.py::ExtractCenterCleanupTests::test_protocol_payment_routes_and_runtime_are_removed` 仍因修改前已存在的未跟踪 `integrations/paypal_agreement_protocol/` 目录而失败。未删除用户目录、未改支付代码或测试断言，完整回归明确排除此项。
- 测试沿用既有 `tests/conftest.py` 的临时 DB 隔离，不操作运行账号库；未发起新注册或真实密码/MFA 请求。
- 最终全量：`972 passed, 1 deselected, 1 warning, 79 subtests passed in 35.44s`。唯一 deselected 为上列已复核的环境冲突；warning 为既存 Requests 字符检测依赖提示。
- 密码/MFA/安全 worker 五文件回归 221 passed；追加 DB 读取异常/空邮箱用例后，安全 worker/停止/队列回归 43 passed。Roxy 两文件 92 passed，邮箱三文件 48 passed。最终全量涵盖全部新增用例。
- `compileall`、`git diff --check`、`tools/check_integrations.py` 均通过。

## 运行生效

- 01:33 核对保留任务文件无 pending/running，实际 `/api/jobs` HTTP 200 返回的最近任务也无活动项；账号记录无 queued/running 安全设置，RoxyChrome/chromedriver 数量为 0。
- 01:34:35 再次检查空闲状态及原 PID 命令行，仅停止本站 PID 52812，以原启动入口/参数和 UTF-8 环境启动隐藏进程，5001 新监听 PID 42452，更新 `run/webui.pid`。
- 重启后带本站既有认证检查 `/`、`/api/jobs`、`/api/accounts` 均 HTTP 200。18794/PID 48800、50000/PID 14200 保持不变；未调用批量停止脚本。
- 新日志：`logs/webui-5001-20260912-013435.stdout.log`、`logs/webui-5001-20260912-013435.stderr.log`，受 Git 忽略。本轮没有创建新账号或重跑历史失败账号。

## R8 交付自检

1. 修改的已有代码：
   - `core/roxy_registration.py::run_roxy_registration/_email_otp_terminal_error`。
   - `core/generic_api_mail_client.py::fetch_latest_otp`。
   - `core/account_security_service.py::_run_security_setup`。
2. 新增生产函数/类/执行器：无。新增测试：
   - `tests/test_roxy_registration_session_recovery.py::test_registration_callback_keeps_mail_received_during_navigation`。
   - `tests/test_roxy_registration_otp_recovery.py::test_auth_route_error_stops_otp_wait_and_replay_without_exposing_page/test_auth_route_error_detection_does_not_reject_generic_page_hints`。
   - `tests/test_generic_api_yangyang.py::test_polling_final_request_timeout_preserves_wait_deadline/test_polling_final_raw_timeout_preserves_wait_deadline_and_redaction/test_polling_early_provider_error_keeps_fast_failure/test_polling_permanent_error_at_deadline_keeps_provider_failure`。
   - `tests/test_account_security_extension.py::test_security_worker_preserves_failure_fields_without_logging_credentials`（六组参数）。
   - 测试补足原有覆盖缺口，不替换仍有效的历史用例；旧的过晚水位、无 deadline 的快速失败判定、原始异常输出已在原位替换/删除。新报告记录本轮证据，不复制工程或作为备份。
3. 搬迁项：无文件搬迁；`TwoFASetupError` 导入前移到同一函数开头，原内层导入已删除。
4. 新增配置项/结果字段：无。既有字段读取链路见 F4。
5. 死引用回扫：下列三条命令输出均为空，退出码均为 1（无匹配）。

   ```powershell
   rg -n -U 'if callback_state == "otp":\r?\n(?:[^\n]*\n){0,4}\s+otp_after_ts = time\.time\(\)' core/roxy_registration.py
   rg -n '^\s+if not best_otp and consecutive_transport_errors >= max_transport_errors:$' core/generic_api_mail_client.py
   rg -n 'str\(password_result\.get\("message"\)|"stage": "failed"|logger\.exception\("\[安全扩展\] (Roxy 重试前|写入|任务失败|清理 Roxy)' core/account_security_service.py
   ```

6. diff 统计：全提交 `+533 / −25`，其中代码与测试 `+402 / −25`，文档 `+131 / −0`。删除行数非零，没有平行生产实现。
7. 最终全量命令：`.venv/Scripts/python.exe -X utf8 -m pytest -q --disable-warnings --deselect=tests/test_extract_center_cleanup.py::ExtractCenterCleanupTests::test_protocol_payment_routes_and_runtime_are_removed`。末五行原始输出：

   ```text
   ........................................................................ [ 80%]
   ........................................................................ [ 88%]
   ........................................................................ [ 95%]
   ...........................................                              [100%]
   972 passed, 1 deselected, 1 warning, 79 subtests passed in 35.44s
   ```

8. 未做与存疑逐条见下节；Git 只提交本轮明确列出的代码、测试、索引与报告，保留原有未跟踪文件。

## 未做与存疑

- 真实邮箱投递、授权中转页停滞、浏览器 Session fetch/403、Roxy open 超时与代理漂移没有被离线测试消除；不改代理规则或将其统称为代码已修复。
- 初始 Session 缺 Token 时安全 worker 的账号库旧 AT 回退仍是防御性审计疑点；原 `_fetch_chatgpt_session` 正常成功出口要求 Token，本轮不更改其协议。
- `account_export.py::_set_twofa_error` 和其他历史诊断路径仍有仅遮蔽六位 OTP 的任意异常正文处理；本轮限定修复安全 worker 输出边界，后续可单独统一凭据脱敏，不宣称全仓日志已完全脱敏。
- 跨进程共享 proxy lease 仍未实现；多个 CLI/WebUI 进程继续按既有规则串行。
- 初始三项未跟踪内容（支付目录与两个支付测试）保留原样，不纳入提交；以已跟踪文件干净和本地/远端 HEAD 一致作为本次交付核对，整棵工作树仍包含用户原有未跟踪内容。
