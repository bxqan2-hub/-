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

## 本次 smiles_forlorn.9c 2FA activate 失败核对（2026-09-02）

- 日志：`注册日志/d19de9d0-2deb-44e6-9f21-d023913ab73d.log`，任务 110。
- 高概率原因：注册、密码、session/access token 和 TOTP enroll 均已完成；仅在 `totp_activate` 阶段失败，`http_status=null`，说明 Selenium/浏览器 fetch 在 activate 边界发生异常或 status=0。旧实现的 execute_async_script 异常被压缩成通用消息，无法继续区分 renderer/代理传输与服务端拒绝。
- 中概率原因：enroll 到 activate 的时间窗口或浏览器网络瞬态导致本次 6 位码提交失败；低概率原因：服务端对该 enrollment 返回一次性业务拒绝。日志没有返回体/状态码，不能把它们混为确定根因。
- 对照上游锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`：本地顺序仍保持同一浏览器上下文、显式 access token、enroll → TOTP → activate、仅 `success=true` 才保存 Secret；本次新增一轮同 enrollment activate 重试，不重复 enroll、不切代理、不提前落盘。
- 本地修改：`core/account_export.py::_browser_authenticated_json_post` 保留脱敏 `stage=exception detail=...`；`_setup_totp_with_driver` 对 status 为空、408/425/429、5xx 或 `success=false` 做一次新窗口码重试。账号仍按“密码已完成、2FA 未完成”保留，可从账号页安全设置重新执行。
- 验证：新增 renderer detail 与同 enrollment 重试测试；基线相关测试 `19 passed`，修改后 2FA/注册回归 `123 passed`，全量 `712 passed, 16 subtests passed`。

## 本次注册缓存与 Roxy 独立画像核对（2026-09-03）

- 上游锁定 commit 仍为 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`；本次没有发现需要同步的上游注册协议变更。Roxy 官方字段和画像建议另以 [API endpoint 文档](https://roxybrowser.com/docs/api-documentation/api-endpoint.html) 与 [Profile configuration 文档](https://roxybrowser.com/docs/features/profile-configuration.html) 为准。
- 证据：`core/browser_traffic.py` 使用共享目录 `data/browser_static_cache`，原请求分类器在无 Cookie 时仍会把 `/backend-api/`、`/sentinel/`、`/cdn-cgi/challenge-platform/` 和 `/unauth-mweb/scripts/` 脚本判为可缓存；现已收窄为仅 `/assets/`、`/cdn/assets/`、`/_next/static/`、`/unauth-mweb/assets/` 公共静态前缀，挑战、Sentinel、后端和未认证脚本必须走当前 Profile 的实时网络。
- 现有缓存盘点为 2811 个 metadata/body 对，未发现 `set-cookie`、`authorization`、`cookie` 或 `proxy-authorization` 敏感头；旧的非公共条目保留在磁盘但在新分类器下不会再被写入或回放。
- 代理证据：当前 `ROXY_CREATE_USE_PROXY_POOL=True`、`PROXY_API_ENABLED=False`、静态池 200 条、`PROXY_POOL_ACTIVE` 为空，候选仍由 `config/proxy.py::_pick_static_or_system_proxy` 的 `random.choice(available)` 抽取；历史重复 `72.82.55.137` 已由 `open_profile` 的真实出口 IP reservation 路径覆盖。预检冲突时会排除候选、轮换并在池耗尽时 fail-closed；释放后 15 分钟内仍拒绝同一 IP。当前 200 条池条目均标记 `region-US`，所以本批随机的是 US 会话线路，不是随机国家。
- Roxy 证据：`ROXY_ONE_PROFILE_PER_ACCOUNT=True` 且 `ROXY_DELETE_PROFILE_AFTER_RUN=True`，每次创建的 Profile ID 不同并在结束后删除；`ROXY_RANDOM_OS_ON_CREATE=True` 只在 `Windows,macOS` 中随机系统，`coreVersion` 未由本地 payload 指定，最新日志全部由已安装 Roxy runtime 返回 `152`，因此浏览器内核当前固定为 152 而非随机。对照上游后，本地 `/browser/create` 现强制发送 `randomFingerprint=True`；`fingerInfo`/语言/时区仍不由本地伪造，以 Roxy 返回和窗口内出口复核为准。
- 详细证据、Finding→Path、修复和验证记录见 `docs/2026-09-03_注册缓存与Roxy独立画像审计-report.md`。
- 上游与本站缓存实现的逐项差异见 `docs/2026-09-03_上游与本站流量缓存机制对比-report.md`；本次继续优化已将本站跨 Profile miss 合并删除，认证凭据请求绕过共享缓存，公共路径 Cookie 请求仅复用明确 `public` 的已校验 body，并过滤边缘/时间响应头，当前冷启动回源行为与上游一致且回放头更窄。


## 本次缓存 miss 时序与回放头隔离修复（2026-09-03）

- 风险核对：本站旧的同 URL miss 合并会让并发 Profile 等待首个回源结果；上游锁定版本没有该等待层。该行为主要形成冷启动流量/时序关联，不传递账号 Cookie、Token 或浏览器存储。
- 对照上游后，本地对严格公共 JS/CSS 的冷 miss 使用 8 秒有界单飞等待；首个请求失败或等待超时仍回到各 Profile 实时网络，保留公共静态 URL 的完整性校验与 URL-only warm cache。
- 本地共享缓存现在拒绝 Authorization/Proxy-Authorization 请求；公共静态路径带 Cookie 时只复用明确 `Cache-Control: public` 的 body，Cookie 不进入 key、metadata 或回放头，并在回放头中剥离 `cf-ray`、`report-to`、`date`、`age`、`etag`、`x-request-id` 等边缘/时间字段。
- 本地还绕过请求侧重新验证指令，拒绝除 `Accept-Encoding` 外的 `Vary`、非 200 读回条目和含 dot-segment/反斜杠路径，并合并检查重复响应头；新增 `cache_candidates`/`cache_writes` 指标，供日志和账号列表区分 0 候选与真实 0 命中。
- 本地 refresh salt 改为 `secrets.token_bytes(16)`；最新 5MB/0 命中批次的证据与修复结果见 `docs/2026-09-03_注册缓存与Roxy独立画像审计-report.md` 和 `VERIFICATION.txt`。

## 本次账号级出口/IP 与 Roxy 指纹隔离（2026-09-03）

- 上游锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 `create_profile` 明确提交 `randomFingerprint`；本地在模板和调用 payload 合并后强制为 `True`，并用 `secrets` 生成系统/环境名随机值。
- `config/proxy.py` 新增 canonical IP reservation、owner 校验、15 分钟 reuse cooldown 和清理接口；`core/roxybrowser_client.py::open_profile` 在创建前预检并占用真实 IP，`core/roxy_registration.py::_verify_registration_exit_geo` 在 Selenium 上下文复核漂移/重复，`cleanup_profile` 在终态释放或保留现场时继续持有。
- 出口冲突/冷却/漂移在 `core/roxy_registration.py` 中标记为 `stage=proxy_isolation`，与代理传输、邮箱 OTP、密码和 2FA 失败分开统计。
- 预检返回非法 IP 时单独轮换候选并保留“快速检测失败”原因，避免把输入格式问题误报为并发占用。
- 账号 `extra_json.roxybrowser.isolation` 记录无凭据摘要（Profile、core、OS、出口 IP、验证来源）；本轮不新增代理凭据字段，也不记录 Cookie、Authorization、Token 或 TOTP Secret。
- 同一 Python 进程线程池内已保证并发 IP 不重复；不同进程（CLI/多个 WebUI）尚未共享 reservation，需串行运行。详细高/中/低原因、证据和测试见 `docs/2026-09-03_注册缓存与Roxy独立画像审计-report.md`。
- 验证：基线定向 `146 passed`、修改后定向 `159 passed`；基线全量 `716 passed, 16 subtests passed`、修改后全量 `729 passed, 16 subtests passed`。

## 本次 5MB / 0 命中回归复核（2026-09-03）

- 最新 14:08–14:09 五个注册日志均为 `downloaded=5.60–5.89MB`、`cached=0`、`hits=0`、`misses=0`、`errors=0`；原因已定位为 Cookie 请求在候选缓存判断前被短路，非缓存目录损坏。
- 对照附件 `C:\Users\Administrator\Downloads\注册流量优化复现与使用教程.docx` 的公开 JS/CSS 分层原则，本地现在允许严格公共路径的 Cookie 请求复用已验证公共 body；Cookie 仍不进入 key、metadata 或回放头。
- 响应写入/读回要求 `Cache-Control: public`、status=200、无私有/画像变体指令；认证、挑战、Sentinel、API、Authorization 和 Proxy-Authorization 继续实时联网。
- `cache_candidates` 与 `cache_writes` 已从 `core/browser_traffic.py` 传入注册摘要、Roxy 日志和账号列表，后续日志可直接区分候选为 0 与真实 0 命中；前一轮缓存定向为 68 项、全量为 716 项，本轮 IP/指纹隔离回归结果见上方。

## 本次最新注册失败、Roxy 启动并发与 2FA 复核（2026-09-03）

- 复核 `注册日志/` 22:20–22:39：58 个任务中 40 成功、14 失败、4 手动停止；43 个任务进入 2FA，40 个完成 enroll/activate。3 条 2FA 失败日志中 Job 74 与 Job 81 是同一邮箱/账号记录，因此为 2 个独立账号。
- 高概率 2FA 原因：Job 61 的 Selenium `script timeout` 来自 `_safe_get` 临时 8 秒脚本预算未恢复；已在 `core/roxy_registration.py::_safe_get` 的 `finally` 恢复 script timeout，并新增回归测试。Job 74/81 的 `password_email_reauth_submit_failed` 属于 OTP 提交 DOM 瞬态；`core/account_export.py::_setup_password_with_driver` 现在对同一验证码重新定位并重试一次，仍失败时记录脱敏状态。
- Roxy 打开速度确有下降：同窗口批次 `create` 中位数 8.5 秒/P90 23 秒，`open` 中位数 23 秒/P90 47 秒/最大 71 秒。原因是 10 路可视 Profile 与 `_ROXY_LIFECYCLE_LOCK` 串行化叠加，而非 workers 被代码调小；锁暂不删除，下一轮按固定并发采集后再评估分离锁或有限 semaphore。
- 上游仍锁定 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`；本地保持上游同窗 Cookie/代理、显式或同源 Session Token、enroll → activate、`success=true` 后才 checkpoint 的边界。完整 Finding→Path→修复→验证见 `docs/2026-09-03_最新注册失败与2FA并发复核-report.md`。
- 验证：定向 `126 passed`，全量 `743 passed, 16 subtests passed`，`compileall` 输出 `COMPILEALL_OK`；新进程下一批需复核 Job 61 的 timeout 消失和密码重认证重试成功率。

## 本次 Roxy 指纹生成字段显式映射（2026-09-03）

- 上游锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 `core/roxybrowser_client.py::create_profile` 会在 `/browser/create` payload 中显式发送 `randomFingerprint`，默认值为真；该字段由 Roxy 负责生成整套 Profile 指纹，不应由本地 Selenium 再改写 `navigator`。
- 本地原路径此前只随机 `os` 与 `windowName`，没有发送 `randomFingerprint`；因此“每账号新建 Profile”不等于已确认调用 Roxy 的新指纹生成器。
- 本次直接在现有 `create_profile` 路径强制写入 `randomFingerprint=True`，覆盖旧模板或调用方误传的 false；未新增开关、未伪造 `fingerInfo`、`coreVersion`、语言或时区值。`coreVersion` 继续由已安装 Roxy runtime 返回并记录为观察值。
- `ROXY_ONE_PROFILE_PER_ACCOUNT=True`、注册前代理出口预检和代理 reservation 共同决定账号级环境/出口隔离；随机抽取本身不代表 IP 唯一，IP 冲突由代理层单独处理。
- 验证覆盖：`tests/test_roxy_proxy_enforcement.py::test_profile_create_always_requests_fresh_random_fingerprint`，并检查模板与调用 payload 传 false 时最终请求仍为 true；未记录 Cookie、Token、邮箱或 MFA Secret。

## 本次 Roxy 并发进程页面负载优化（2026-09-03）

- 上游锁定 commit 仍为 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`；上游 `core/roxy_registration.py` 通过 `/browser/open` 的 `args` 传递启动参数，`core/browser_traffic.py` 以默认参数调用 `Network.enable`。
- 现象核对：当前批次高峰同时运行 10 个 Profile 时曾出现约 90 个 `RoxyChrome` 子进程、约 7.9 GB 工作集；批次收敛后 `RoxyChrome=0`、Roxy 工作集约 0.99 GB，说明页面卡顿主要由活动 Profile 的进程/缓冲负载触发，不是历史账号条目数直接造成。
- 本地 `core/roxybrowser_client.py::RoxyBrowserClient.open_profile` 保留调用方 args 并去重追加后台服务优化参数；不改变 worker/Profile 并发数、UA、OS、`randomFingerprint`、代理或 Cookie。
- 本地 `core/browser_traffic.py::RoxyTrafficOptimizer._enable_network_domain` 为每个 Profile 设置 2 MiB 总 Network 缓冲、512 KiB 单资源缓冲和 4 KiB POST 元数据上限；旧版 CDP 拒绝可选字段时回退默认 `Network.enable`。
- Roxy 官方 API 文档确认 `/browser/open` 支持 `args` 列表，并列明 `--no-first-run`、`--no-default-browser-check` 等内置参数不可修改；本次未重复注入这些字段。
- 详细 Finding、路径、边界和验证记录见 `docs/2026-09-03_Roxy并发进程页面负载优化-report.md`。
- 验证覆盖：有界 Network 缓冲、旧版 CDP 回退、并发 Profile 启动参数合并与去重；下一批需保持原并发数量采集前后 Roxy 工作集、子进程数和页面等待时间。

## 本次 Roxy 浏览器缓存盘点与 WebUI 清理按钮（2026-09-03）

- 上游锁定 commit 仍为 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`；Roxy 官方 API 文档提供 `/browser/clear_local_cache`，其中 `partial` 级别保留扩展、登录状态、指纹和 IP。本地没有扩大清理范围，而是新增固定目录白名单清理。
- 当前盘点：Roxy `browser-cache` Profile 存储约 1.28 GiB，其中可回收网页缓存约 276.8 MiB；Roxy 管理器 `Cache` 约 67.2 MiB；注册共享公开 JS/CSS 缓存约 765.6 MiB。按钮只清理前两项，公开静态缓存继续保留。
- 新增 `core/browser_cache_service.py`，清理前核对注册任务为 0 且 `RoxyChrome/chromedriver` 为 0；只清理 `Cache`、`Code Cache`、`GPUCache`、Shader/Dawn 缓存和 Roxy 管理器缓存子项，保留 Cookies、Local Storage、指纹、代理、Profile 运行时文件。
- 新增 WebUI 接口 `GET /api/roxy/cache/status` 与 `POST /api/roxy/cache/clear`；清理接口要求显式 `confirm=true`，并返回删除字节、删除文件、部分占用文件和清理后的容量。
- 配置页顶部新增“清理缓存”按钮，显示总占用、可回收容量和活动阻断原因；确认框、加载状态、成功提示和禁用状态遵循现有 WebUI 样式。
- 详细容量、边界和验证见 `docs/2026-09-03_Roxy浏览器缓存清理与容量审计-report.md`。

## 本次冷缓存十路并发卡死核对与并发护栏（2026-09-03）

- 上游锁定 commit 仍为 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`。已读取上游 `vendor/turb_gpt_free_register/core/roxybrowser_client.py`（`open_profile` 使用 `args` 列表）与 `core/browser_traffic.py`（`Network.enable({})`）；上游没有本地主机注册并发上限，本次新增的是本地资源护栏，不改变注册协议。
- 失败批次证据：20:21:12–20:21:13 同时启动 `reg-worker-1_0`…`reg-worker-1_9` 共 10 个任务；清理后的共享缓存随后在 20:21:27–20:22:04 冷启动重建 38 个条目。20:24:27 触发 Kernel-Power Event 41，20:24:33 记录 EventLog 6008，20:32:27 WebUI 才在重启后恢复。
- 本地修复：`core/browser_traffic.py` 对严格公共 JS/CSS 的冷 miss 使用 8 秒有界单飞等待；首个请求失败或等待超时仍回到各 Profile 实时网络。
- 该修复不改用户输入的 workers、Roxy Profile、代理出口、Cookie、access token、密码或 2FA；高/中/低原因、事件证据、路径和验证见 `docs/2026-09-03_Roxy冷缓存并发卡死核对-report.md`。

## 本次 Profile 目录与共享 JS/CSS 缓存分离清理（2026-09-03）

- 复核官方 `/browser/list_v3` 后确认当前返回 0 个 Profile，而本地 `browser-cache` 仍有 12 个 32 位 Profile 目录；它们合计约 1.00 GiB，属于已从 Roxy 列表移除的孤儿运行数据。
- 现有“清理 Profile 缓存”按钮已扩展为：官方列表核对成功、注册任务和 `RoxyChrome/chromedriver` 均为空时，删除不在官方列表中的孤儿 Profile 目录；已登记 Profile 只清理网页缓存子项。
- 新增旁边的“清理共享 JS/CSS”按钮和 `POST /api/roxy/cache/clear-shared`，只处理 `data/browser_static_cache`；它与 Profile 清理的确认、活动进程检查和结果提示分开。
- 共享缓存默认保留以降低注册冷启动流量；显式清理后，下一批注册会重新构建公开资源缓存。
- 详细 Finding、容量、路径和验证见 `docs/2026-09-03_Roxy浏览器缓存清理与容量审计-report.md`。

## 本次最新注册日志 2FA 失败核对与 Token 刷新修复（2026-09-03）

### 日志证据（21:52–21:57）

- 本批 7 个 Job 中，Job 21、24 的 MFA enroll/activate 与 Token 校验均完成；Job 29 的 enroll/activate 已完成，但后续只读 Token 校验连续 3 次未通过，日志明确写成 `totp_token_validation_failed`，账号仍保存 Secret。该只读失败不能倒推为 2FA 未激活。
- Job 25、28 在 `stage=password_email`、`code=password_email_reauth_submit_failed` 停止，均未进入 MFA enroll；这是邮箱重认证验证码提交/页面推进失败，不是缓存命中失败。
- Job 27 在补设密码成功后进入 `stage=totp_enroll`，HTTP 401 返回 `token_revoked`。补设密码前后，注册 Token 可能被服务端吊销；旧调用链仍把注册阶段旧 Token 传给 MFA enroll，正好解释该条 401。
- Job 26 在邮箱 OTP 取码端超时并未到达 2FA；其响应为 HTML 邮件内容未提取到 6 位码，属于邮箱取码失败。

### 流量缓存核对

- `data/browser_static_cache` 在本批开始后从冷目录重建为 666 个文件，文件写入集中于 21:52:20–21:56:19；日志同时出现 `cache_hits/cache_misses` 和大量回源，说明删除缓存只改变了公共 JS/CSS 的冷启动负载。
- 认证、Session、Sentinel、MFA API 不进入共享缓存：`core/browser_traffic.py::is_cacheable_request` 只接受 GET 的一方公共 script/stylesheet，`set_session_only` 后 ChatGPT 文档与 Session-required 路径继续实时联网。因此没有“缓存回放旧 MFA 响应”的证据。
- 对照结果：Job 28 在正常命中（36 hits/4 misses）下仍发生 `password_email_reauth_submit_failed`，Job 29 在同样 36 hits/4 misses 下完成 MFA；这直接把缓存命中与 MFA 成败拆开。冷缓存可能放大 10 路可视 Profile 的页面/网络负载，但不是这些 2FA 业务错误的直接请求路径。

### Finding → Path → 修复

- **高概率（已修复）**：密码重认证成功后旧 registration access token 被吊销，MFA enroll 使用旧 Token。路径：`core/account_export.py::_setup_2fa_result` → `_setup_password_with_driver` → `_setup_totp_with_driver`。修复后在浏览器补设密码并同步 Cookie 后清空旧 Token，让同一浏览器上下文的 MFA helper 重新读取当前 `/api/auth/session` Token；未补设密码的原路径仍显式透传注册 Token。
- **中概率（待继续观察）**：`password_email_reauth_submit_failed` 表示验证码已取到但页面提交没有推进，现有日志没有 DOM/响应体证据，不能归因于缓存；保留为独立 `password_email` 阶段，不与 MFA enroll 混报。
- **低概率**：Job 29 的只读 Token 校验瞬态失败；激活已确认且 Secret 已 checkpoint，按现有规则保留 Secret，后续可单独重试只读校验。

### 本轮验证

- 定向：`.\\venv\\Scripts\\python.exe -m pytest -q tests\\test_twofa_registration.py tests\\test_roxy_registration_session_recovery.py tests\\test_roxy_registration_otp_recovery.py` → `124 passed`，退出 0。
- 全量：`.\\venv\\Scripts\\python.exe -m pytest -q` → `742 passed, 16 subtests passed`，退出 0。
- 编译：`.\\venv\\Scripts\\python.exe -m compileall -q config core webui tests` → `COMPILEALL_OK`，退出 0。

## 本次只改不堆首轮收缩（2026-09-05）

- 对照锁定上游 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 后未覆盖上游实现；仅修改本地既有路径。
- `core/registration_service.py` 在 `_run_one_job`、`_run_codex_retry_job` 的启动异常边界补齐 `_deactivate_job()`，并删除无调用点的 `_enqueue_checkout_kind_after_registration` 私有死代码。
- `config/email.py` 将已有 `OTP_SETTLE_SECONDS` 纳入环境覆盖 schema；`core/outlook_client.py::fetch_latest_otp` 改为读取 `_email_cfg.OTP_SETTLE_SECONDS`，使 WebUI/热加载后的值真正生效。
- 验证：定向 `25 passed`、`compileall` 为 `COMPILEALL_OK`；全量 `749 passed, 16 subtests passed`。

## 第二批只改不堆收缩（2026-09-05）

- `core/roxy_registration.py::_safe_get` 修复 script timeout 恢复错误，保留真实 driver 设置，读取失败才使用配置回退。
- `core/session.py` 改为动态读取 `config.browser` / `config.openai_protocol`，消除热加载后的旧常量滞留。
- 验证：定向 `108 passed`；全量 `750 passed, 16 subtests passed`；`compileall` 通过。

## 第三批只改不堆收缩（2026-09-05）

- `core/db.py` 旧 SQLite 迁移按记录隔离异常，避免坏数据阻断整批导入。
- `core/email_provider.py::release_email` 合并邮箱源释放分支，减少平行调用路径。
- 验证：定向 `32 passed`、迁移冒烟通过；全量 `750 passed, 16 subtests passed`。

## 第四批审计（2026-09-05）

- `core/browser_traffic.py`、`core/browser_use_registration.py` 已完成调用链与测试覆盖复核，未发现可安全删除的堆积代码，因此保持实现不变。

## 第五批浏览器体验优化（2026-09-05）

- 分组/精确邮箱筛选的后台刷新由 2 秒改为 10 秒节流，避免 700 账号分组反复触发全量内存筛选。
- 新建分组后保持当前分组和列表位置，新组仅设为移动目标。
- 验证：定向 `33 passed`；全量 `751 passed, 16 subtests passed`。
