# 上游项目索引：GPT-utral-platform

> 该文件是注册机维护时的固定上游参考入口。修改 ChatGPT 注册、Roxy 浏览器、密码或 2FA 流程前，先查看本索引，再按记录的固定 commit 对照上游实现。

## 来源与锁定版本

- 项目：[`Torin-x/GPT-utral-platform`](https://github.com/Torin-x/GPT-utral-platform)
- 当前参考分支：`main`
- 索引时读取的 commit：`68a1f8faede7e41f10ac5f9af267465fa61d0e3d`
- commit 页面：<https://github.com/Torin-x/GPT-utral-platform/commit/68a1f8faede7e41f10ac5f9af267465fa61d0e3d>
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

- 本仓库入口：`core/account_export.py::_setup_totp_with_driver`。
- 当前优化：支持 `access_token` 显式透传；MFA 浏览器失败消息包含 stage、HTTP 状态和非敏感错误摘要。
- 注册调用方：`core/roxy_registration.py` 必须从注册阶段的 `session_info` 传入 `access_token`。
- 独立账号安全设置：`core/account_security_service.py` 使用同一规则传入现有 token。
- 不把上游代码整目录覆盖到本仓库；只提取已验证的协议、调用顺序、日志字段和测试思路。
- 每次后续修改前：
  1. 打开本文件确认上游锁定 commit；
  2. 读取对应上游 raw 文件并比较本地差异；
  3. 在本文件追加新的 commit、差异和验证结果；
  4. 运行相关 2FA/注册测试后再提交。

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

## 本次纯协议注册替换（2026-08-25）

- 新协议来源：[`asz798838958/aBaiFreeGPT`](https://github.com/asz798838958/aBaiFreeGPT)。
- 锁定 commit：`98e0ad6717566dcaec2a2d7feb7b3bea2458de1`（2026-08-25）。
- 本地复制目录：`core/abais_protocol/`；入口适配：`core/abai_protocol_registration.py`。
- `main.py` 的 `protocol` / `api` / `http` 分支已直接调用该复制实现；Roxy、CloakBrowser、Browser Use、Skyvern 分支保持原调用。
- 复制的协议实现包含 `protocol_register.py`、`oauth.py`、`credential_checks.py`、`mfa.py`、`environment_profile.py`、`sentinel_vm.py`、Sentinel SDK/Node 运行时和常量；仅把上游包名改为本地 `core.abais_protocol` 命名空间。
- 上游流程特征：邮箱 OTP 状态机、注册密码提交、同一注册会话内 TOTP enroll/activate、持久 Sentinel VM、统一环境画像和 Cloudflare 后代理轮换。
- 本地适配只负责已有账号存储/邮箱回收/Flow 触发及结果字段映射；不再调用旧 `core.chatgpt_auth` / `core.openai_auth` 注册链路。
- 验证：`.\\venv\\Scripts\\python.exe -m pytest -q tests/test_protocol_strict_alignment.py tests/test_twofa_registration.py tests/test_registration_local_proxy_mode.py`，`65 passed`。

## 本次纯协议静态粘性代理选择放宽（2026-08-28）

- 纯协议仍要求解析到有效代理，并继续由严格会话固定代理、TLS、UA 与 OS 画像。
- 移除“静态池有多条时必须填写 `PROXY_POOL_ACTIVE`”的入口限制；留空时从 `PROXY_POOL` 随机选择一条，并在单次注册会话内固定。
- `PROXY_POOL_ACTIVE` 保留为可选优先线路；配置指向已移除的旧线路时回退到当前静态池，不再阻止注册。
- Cloudflare 后轮换仍通过 `excluded` 排除失败线路，从剩余粘性代理中选择。

## 本次 WebUI 2FA 复制取码地址（2026-08-29）

- 账号页“复制密码2FA”现在输出 `账号----密码----https://2fa.fb.tools/<TOTP Secret>`，不再把 MFA Secret 作为第三段直接复制；批量复制与单账号复制保持同一格式。
- 账号页“验证码”按钮继续使用原有后端本地 `pyotp` 生成方式；本次不改变点击验证码的行为。
- 本地修改：仅保留 `webui/app.py` 的 `_account_2fa_url`、`_account_password_2fa_line`，以及 `webui/templates/index.html` 的复制提示；回归覆盖 `tests/test_webui_helper_regressions.py`。
- 验证：`PYTHONPATH=. .\\venv\\Scripts\\python.exe -m pytest -q tests/test_webui_helper_regressions.py`（26 passed）；全量测试结果以本次提交记录为准。

## 本次换绑导出恢复原始 2FA（2026-09-01）

- 默认“复制密码2FA”和批量复制恢复为 `账号----密码----原始 TOTP Secret`，确保换绑分站导回主站时不会把取码 URL 整串写入 `totp_secret`。
- 新增独立“复制URL”与“复制所选2FA URL”按钮，只有显式使用新按钮时才输出 `账号----密码----https://2fa.fb.tools/<Secret>`。
- 换绑导入兼容已经导出的 `2fa.fb.tools` URL：严格校验域名与单段路径后提取原始 Base32 Secret；其他域名、查询参数或无效 Base32 会拒绝保存。
- 本地动态验证码生成、MFA enroll/activate 和账号资格检测核心逻辑均未改动。

## 本次 AT 定时检测网络错误自动重试（2026-08-30）

- 现象核对：`注册成功的邮箱.json` 中 6 个账号的 `request_error` 均为 curl 连接 `chatgpt.com:443` 超时，5 次请求全部失败，网络路径为 `local_vpn`；这属于网络/VPN 路径未连通，不能据此判定 AT 失效。
- `core/at_validity.py` 原本单次检测已执行最多 5 次指数退避重试；本次在队列层增加 2 轮、每轮间隔 5 秒的自动重新入队，期间不把临时错误覆盖为最终状态。仍失败后才保留 `AT检测: 网络错误`。
- 调度器会把已保存的 `request_error`、408/425/429/5xx 结果视为立即到期；WebUI 启动时发现这类历史错误会优先触发重新查询，不再等待完整复查周期。
- 验证：AT 定向测试 23 项；全量测试结果记录在本次提交的 `VERIFICATION.txt`。

## 本次套餐查询语言跟随代理地区（2026-08-30）

- 重新核对上游 `main`，当前仍为锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`。上游 `vendor/turb_gpt_free_register/core/session.py` 会先探测代理出口，再把 `Accept-Language`、`navigator.language` 和时区画像切到出口地区；上游独立 `paypal_global_rotation_source/gpt_account_plan.py` 的老式套餐探针则固定 `en-US`。
- 本地套餐探针为了避免每个短请求额外访问 IP 地理接口而保留 `detect_exit_geo=False`，但现在把套餐静态代理条目已经确认的两位国家标签作为 `profile_geo` 传给 `BrowserSession`，达到与上游“出口决定画像”相同的效果，同时不增加地理探测请求。
- 补齐当前套餐池实际使用的 `ID → id-ID / Asia/Jakarta`、`PH → en-PH / Asia/Manila`、`VN → vi-VN / Asia/Ho_Chi_Minh`，并覆盖 KR/IN/BR/IT/ES；JP/US/SG 等继续使用原映射。代理重试换国时，下一轮会话语言也随新代理国家更新。
- 查询结果新增 `plan_check_locale_country` 与 `plan_check_request_language`，账号页套餐提示会直接显示例如 `请求语言: id-ID（跟随印度尼西亚 ID 代理）`，便于确认文案语言与代理地区一致。

## 本次注册失败分级与生命周期/回调修复（2026-09-02）

- 最新批次（任务 12、19、24、28、33）共 5 条失败：3 条为本地 Roxy `/browser/create` 在并发指纹初始化期间超过 15 秒，1 条为 `ERR_PROXY_CONNECTION_FAILED`，1 条为 Email verified 后 callback 尚未稳定便重复要求 OTP。
- 与锁定上游 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 对照后，本地保留上游“同一浏览器 Cookie/代理 + 显式 access token + enroll/activate 成功确认”安全边界；本次只调整生命周期等待预算、代理错误 stage 和 callback 的 session settle 顺序。
- 本地修改：`config/roxybrowser.py` / `.env.example` 新增 `ROXY_CREATE_API_TIMEOUT=45`；`core/roxybrowser_client.py` 仅对 `/browser/create` 使用该预算；`core/roxy_registration.py` 将明确代理链路错误标记为 `stage=proxy_transport`，并在 callback 恢复时先读取稳定 session，避免重复消耗 OTP。
- 验证：`tests/test_roxy_proxy_enforcement.py` 覆盖创建与其他生命周期接口超时隔离；`tests/test_roxy_registration_session_recovery.py` 覆盖代理错误分类和 Email verified callback settle。
