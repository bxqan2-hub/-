# 上游项目索引：GPT-utral-platform

> 该文件是注册机维护时的固定上游参考入口。修改 ChatGPT 注册、Roxy 浏览器、密码或 2FA 流程前，先查看本索引，再按记录的固定 commit 对照上游实现。

## 来源与锁定版本

- 项目：[`Torin-x/GPT-utral-platform`](https://github.com/Torin-x/GPT-utral-platform)
- 当前参考分支：`main`
- 索引时读取的 commit：`68a1f8faede7e41f10ac5f9af267465fa61d0e3d`
- commit 页面：<https://github.com/Torin-x/GPT-utral-platform/commit/68a1f8faede7e41f10ac5f9af267465fa61d0e3d>
- 本地直接拉取路径：`vendor/GPT-utral-platform`（Git submodule，固定到上述 commit）。
- 注册运行入口：`core/upstream_registration_bridge.py` 直接启动该 submodule 内的
  `vendor/turb_gpt_free_register/bridge_runner.py`，本地不再复制其注册实现。
- 上游定位：仓库 README 说明这是基于 `lxf746/any-auto-register` 的多平台二次开发；`vendor/turb_gpt_free_register/` 是其中保留的独立注册组件，来源和许可证以该目录说明为准。

## 与本仓库最相关的文件

| 上游文件 | 关注内容 | 用途 |
| --- | --- | --- |
| `vendor/turb_gpt_free_register/core/account_export.py` | `setup_2fa_from_selenium`、`_selenium_authenticated_json_post` | 在已登录 Roxy Selenium 网络栈中直接发送 MFA enroll/activate |
| `vendor/turb_gpt_free_register/core/roxy_registration.py` | 注册完成后调用 `setup_2fa_from_selenium` 的位置 | 复用注册环境、Cookie、IP、UA，并把已有 `access_token` 显式传入 |
| `vendor/turb_gpt_free_register/core/account_export.py` | `setup_2fa`、`_trigger_reauth`、`_follow_reauth`、`_validate_reauth_otp`、`_exchange_new_token` | 直接浏览器请求失败时的协议重认证备用链路 |
| `README.md` | 浏览器/API 双模式、代理池、实时日志、账号生命周期 | 维护时的总体架构和可观测性参考 |

## 上游 2FA 实现要点

### 浏览器网络栈路径

上游 `setup_2fa_from_selenium` 的顺序是：

1. 比较 `authenticated_email` 与目标邮箱。
2. 在当前浏览器上下文中 POST `/backend-api/accounts/mfa/enroll`，JSON 为 `{"factor_type":"totp"}`。
3. 从响应读取 `secret` 与 `session_id`。
4. 使用 `pyotp.TOTP(secret).now()` 生成 6 位码。
5. 在同一浏览器上下文 POST `/backend-api/accounts/mfa/user/activate_enrollment`，提交 `code`、`factor_type`、`session_id`。
6. 只有响应 JSON 的 `success` 为真才认为激活完成。

关键实现细节：上游把注册阶段刚拿到的 `access_token` 显式传给浏览器 POST helper；如果不传，helper 才会再次读取 `/api/auth/session`。本仓库此次错误正是因为调用链没有把这个 token 传到 MFA enroll。

### 协议重认证备用路径

上游保留完整备用链路：

`/api/auth/csrf` → `/api/auth/signin/openai` → `auth.openai.com` 邮箱 OTP → `/api/accounts/email-otp/validate` → callback 刷新 Session Token → enroll → activate。

因此后续若浏览器 enroll 返回 401/403，应优先记录状态和响应错误字段，再考虑降级到这条重认证链路，而不是把所有失败都记成同一个“请求失败”。

## 本地实现映射与维护规则

- 当前 Roxy 注册入口：`main.py` → `core/upstream_registration_bridge.py` →
  `vendor/GPT-utral-platform/vendor/turb_gpt_free_register/bridge_runner.py` → 上游 `core/roxy_registration.py`。
- 本地 `core/roxy_registration.py` 不再位于 Roxy 注册运行路径，仅保留给独立工具和历史回归测试。
- 独立账号安全设置：`core/account_security_service.py` 使用同一规则传入现有 token。
- 上游注册核心以只读 submodule + 隔离 Bridge 方式直接运行；本站仅保留任务、数据落库和
  Windows 无控制台窗口适配，不再把上游注册函数复制进本地执行入口。
- 每次后续修改前：
  1. 打开本文件确认上游锁定 commit；
  2. 读取对应上游 raw 文件并比较本地差异；
  3. 在本文件追加新的 commit、差异和验证结果；
  4. 运行相关 2FA/注册测试后再提交。

## Windows 注册时闪现 CMD 窗口修复（2026-08-24）

- 进程与源码检查确认，注册表单阶段会多次调用 `core/sentinel_runner.py`，它通过
  `subprocess.run()` 启动 `node.exe`，原实现没有传入 Windows `CREATE_NO_WINDOW`。
- 本地协议路径已为 Node 子进程加上 `CREATE_NO_WINDOW`。
- 直接上游 Bridge 进程以 `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` 启动；隔离 worker
  通过 `upstream_runtime/sitecustomize.py` 给它内部产生的 Node/命令行子进程统一补上
  `CREATE_NO_WINDOW`，上游 submodule 文件保持原始哈希不变。

## 本次 miss.uplink_0a 失败记录

- 日志：`注册日志/28be0110-c6a6-4daa-9b77-9b669e33ec21.log`
- 注册、邮箱 OTP、资料页和 access token 均已成功。
- 失败阶段：`totp_enroll`，原日志最终只显示“浏览器 MFA enroll 请求失败”。
- 根因定位：上游调用显式传入注册阶段 `access_token`，本地浏览器 MFA 调用没有传入，导致浏览器 helper 被迫再次读取 Session，实际错误状态被统一包装后丢失。
- 修复后的日志会保留 `stage`、`code`、`http_status` 和脱敏错误摘要，便于下一次直接定位。

## 本次 create-account/password 提交停留修复（2026-08-24）

- 近期日志：`注册日志/f86df278-31a0-4044-b4ef-9a8665422218.log`、`注册日志/126ad589-27ab-4b90-8caa-fd5184de44da.log`、`注册日志/003e9757-7d08-42c6-90d7-4b43bcc70070.log`。
- 共同现象：密码输入已写入，第一次提交后等待约 8 秒无导航，第二次提交后仍位于 `/create-account/password`；页面同时存在可见 `type="button"` 控件和真正的 submit 控件，错误列表为空。
- 对照上游锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的密码提交实现后，本地仅优化提交动作：优先选择显式 `button[type="submit"]` / `input[type="submit"]`，并在支持时用所属表单 `requestSubmit()` 触发 React/原生提交；不改变密码策略、重试次数或后续 MFA 流程。
- 本地修改：`core/roxy_registration.py::_submit_signup_password_direct`；测试：`tests/test_roxy_registration_otp_recovery.py`。

## 本次密码页瞬态输入框修复（2026-08-24）

- 近期日志：`注册日志/12c5001c-bf61-41cb-b9dc-79ac3afeb35f.log`。
- 新报错的直接原因：`requestSubmit()` 触发 React 路由切换时，`/create-account/password` 页面会短暂卸载密码 input；第二次定位在这个窗口内返回 `missing_password_input`，旧逻辑把瞬态 DOM 状态误判成“密码页处理失败”。日志随后仍能读到密码 input，说明不是密码策略拒绝。
- 上游锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的对应实现会先等待密码 input hydration（最长 8 秒），点击后再观察页面（最长 20 秒），并把表单未前进与真正拒绝分开处理；上游文件没有本地 `_submit_signup_password_direct` 这个封装。
- 本地提交 `1de69761dff7a3c61015ed94bb2cb227a8b7f052` 与回归测试提交 `5b1ffc992f6c418b52c97f3392e09d70ec5b8fec`：`_fill_password_page_if_present` 现在把 `missing_password_input` 视为导航/hydration 瞬态，等待邮箱验证码页、登录态、错误字段或密码 input 重新出现；未出现 input 时才消耗重试次数，input 重新出现则重用同一次提交额度。服务端错误仍优先抛出，确认回调只在已观察到最终状态后执行。
- 验证：`PYTHONPATH=. pytest -q tests/test_roxy_registration_otp_recovery.py`，`59 passed`；新增用例覆盖“输入框重新出现后复用同一次提交额度”。

## 本次密码页提交仍停留修复（2026-08-24）

- 最新失败日志：`注册日志/bc7ad3fd-198c-4ea9-8890-f97fb9d0217b.log`；同型记录还包括 `注册日志/1d36617b-9894-443c-b2b8-231a177083c5.log`、`注册日志/003e9757-7d08-42c6-90d7-4b43bcc70070.log`。
- 共同现象：第一次提交后固定等待约 8 秒立即发起第二次提交，第二次结束后仍为 `/create-account/password`；最终 state 仍有可见 `new-password` input、`button[type=submit]` 和空 `errors`。这次不是 input 瞬态，而是前一轮提交仍在 auth 前端事件/导航窗口内，旧的同步 `requestSubmit()` 重试过早。
- 对照上游锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`：上游优先点击可见 submit 按钮，并在提交后观察约 20 秒；本地现在首次使用异步原生 click（带 pointer/mouse 事件），观察窗口为 20 秒，只有窗口耗尽才切换第二次 `requestSubmit`，日志会记录 `method=scheduled_click_async` 或 `method=scheduled_request_submit_async`。
- 本地提交 `22dbd68d11db5f8aefd4bed367dcb2dba8b9956e`，修改字段：`core/roxy_registration.py::_submit_signup_password_direct`、`_fill_password_page_if_present`；回归测试新增十路并发密码页回放，覆盖 9 个首轮 click 成功和 1 个第二策略恢复。
- 本地十路并发回放结果：10/10 返回确认密码，提交策略调用总数 11（第 10 路使用 `click_async → request_submit_async`），未出现“仍停留在密码页”。
- 验证：注册/密码/2FA/停止相关测试合计 `110 passed`。
