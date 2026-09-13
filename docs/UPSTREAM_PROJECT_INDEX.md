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

## 本次 OTP-only 注册补密码与安全重试修复（2026-09-11）

- 修改前重新读取锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 `vendor/turb_gpt_free_register/core/account_export.py` 与 `core/roxy_registration.py`。上游没有本地 `post_login_add_password` 扩展；本次参考其 OTP 重定位/页面状态等待，以及同窗 Cookie、显式 Token、邮箱匹配和 enroll/activate 成功确认，不覆盖 vendor 或新增平行实现。
- 最新 9 个已保存账号中 7 个停在 `password_email`，分别为 6 个提交失败和 1 个推进超时；它们未进入 MFA。修改 `core/account_export.py` 的既有验证码 helper/状态机，替换 0.75 秒固定重试和 8 秒推进判定，保持有界等待与密码终态确认。
- `core/registration_service.py` 的安全失败任务优先调用已有账号安全队列，而非补跑 Codex；`webui/app.py` / `webui/templates/index.html` 复用现有字段展示错误和正确的重试操作。
- `core/account_security_service.py` 补密码后不再透传旧 Token，重新读取同一浏览器 Session、匹配邮箱并同步 Cookie。已有 TOTP 且密码已确认时，只读刷新失败只附注，不将两项已完成凭据误标为激活失败；账号错配仍停止。
- 证据、Finding→Path、验证与未完成事项见 `docs/2026-09-11_注册密码与2FA重认证修复-report.md`。不自动注册、不自动补设历史账号，实际新批次由用户验证。

## 本次最新失败实查与五邮箱回归（2026-09-11）

- 再次读取锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 `vendor/turb_gpt_free_register/core/account_export.py` 和 `core/roxy_registration.py`；锁定版本不变。上游没有本地刷新重输 OTP 的封装，也没有本次 HTTP 瞬态重试；只对照其同窗、身份匹配、显式 Token 与 enroll/activate 成功顺序，不整体覆盖。
- 新批次 10 条中 6 成功、4 失败：1 条登录中转停滞，2 条已收邮件但刷新后 DOM 等待过短，1 条密码已确认后 MFA enroll HTTP 503。实查 Job 16/20 远端邮箱，邮件存在且邮件 ID 与取码日志相符；Job 13 当前邮箱为空，注册尚未进入发码阶段。
- 修改本地既有邮箱提交与 OTP 状态观察，完整等待后才做一次恢复；明确资料页/Session/Email verified 后不重输已用码。按原代理隔离规则删除预检结果替代窗口实测的回退，独立记录 `proxy_isolation`。
- MFA 只对 503/429 且空业务响应做最多 3 次同窗重试；删除模糊写结果的 activate 外层重放。Session 重读在 POST 前匹配目标邮箱，新 Token 通过原 checkpoint 与内部 session 透传给 Roxy finally，即使 MFA 失败也不重新保存旧注册 Token；Secret 仍等待 `success is True`。
- 邮箱接口阶段/预算/日志脱敏与时区、历史消息 ID 修复沿现有适配器；不增加配置或第二套注册执行器。统一回归及用户要求的五新邮箱现场验证，见 `docs/2026-09-11_最新注册失败与邮箱实查修复-report.md`。

## 本次 US 英文重认证批量失败修复（2026-09-11）

- 修改前重新获取并对照锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的两个注册/MFA 文件；锁定版本不变。上游没有本地登录后补密码扩展，保留同窗 Cookie/代理、精确邮箱匹配、显式 Token 和 enroll/activate 成功确认，不复制上游整条协议备用实现。
- 16:55 出口由 VN 变 US 后，两批共有 22 个账号停在密码邮箱重认证，未进入 MFA。实查最新八个邮箱均已收到正确的新邮件；英文页面触发提前重发，而越南语没有。旧整页 `error/try again` 判断还会在输入前误报拒绝。
- 修改 `core/account_export.py` 的原收码/提交路径：初始邮件先等、仅真实超时且同一有效挑战重发一次；普通提示与 code/page/HTTP 错误分开；被动追踪当前浏览器 POST，避免自动提交后再点击，不读取正文或消耗共享 performance 日志。密码重认证 JS 在 CSRF/signin 前校验目标邮箱。
- 本次 US 五个新邮箱的密码与 MFA 远端均成功；四次邮箱重认证都 `resend=0 post_count=1 http=200`，另一个走注册密码页。两次最终本地 JSON replace 失败已从确认 checkpoint 恢复，不将首轮 3/5 完整任务改写为 5/5。
- 现场追加修复 `core/db.py::_write_json` 的 Windows 暂态原子替换，真实 Win32 占用回归通过；补充统一 pytest DB 路径隔离，防止旧 WebUI 启动恢复测试修改运行数据。五个最终账号密码/2FA 均具备，套餐 HTTP 200；隔离全量 `953 passed, 1 deselected, 1 warning, 61 subtests passed`。
- 证据、Finding→Path、安全复核、局限及八项自检见 `docs/2026-09-11_US英文重认证批量失败修复-report.md`。

## 本次出口探测回归与注册指纹核对（2026-09-11）

- 修改前重读并对照锁定 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的注册和 MFA 文件，版本不变；上游没有本地出口探测 helper，不覆盖上游实现。
- 现场同一代理重现 4 秒超时、8 秒约 4.1–4.4 秒成功；删除本地 Selenium 固定 2 秒并行路径和只访问末端接口的回退，在原 helper 中使用同窗、按配置顺序的有界导航。保留真实出口占用/冷却与漂移即停止，不以预检代替窗口实测。
- 密码终态 checkpoint、精确邮箱匹配、同窗 Cookie/代理、显式 Token 与 enroll/activate 成功确认均复核，未修改密码/MFA 状态机。
- 3 个独立 Profile 的出口预检/实测一致并完成清理；定向 62 项通过，全量 `959 passed, 1 deselected, 1 warning, 61 subtests passed`。8 秒配置已热加载；用户随后明确要求自行重启，22:56 的新 5001/PID 49360 已加载修复。
- 最初只有 12 个有资格旧账号；随后按用户要求提交十个新邮箱：8 注册成功、2 初始邮箱 OTP 超时；成功账号密码与 MFA 均确认，资格查询均 HTTP 200，1 有资格、7 无资格。十个不同 IP/Profile/Canvas，临时 Profile 全部清理。比较只看注册时指纹/IP，未实施复用；正例只有 1 个，不作因果结论。
- 以上为现有 417222c 路径的现场验证，未新增生产逻辑或更换锁定上游。逐账号敏感运行明细留在 Git 忽略的 run 目录；证据、邮箱实查、限制与自检追加到同一份 `docs/2026-09-11_出口探测回归与注册指纹核对-report.md`。
- 用户随后要求无额外指纹采样的正常流程再跑十个：使用普通批量注册请求、原邮箱池自动领取、页面默认十路并发，不连接额外 CDP、不修改生产配置。结果 8 完整成功、1 账号已建但 Session 读取 403 导致安全设置未完成、1 已收码但 OTP DOM 为空；9 个已建账号查询均 HTTP 200，2 有资格、7 无资格（包含安全未完成账号）。两个正例分别为 Windows、macOS；完整成功账号密码/MFA 均确认，10 个 Profile 均清理。对照和限制追加到同一报告，不将不同并发/邮箱/IP 的两批当作单变量实验。

## 本次近期失败日志与 OTP 时间水位修复（2026-09-12）

- 修改前重新读取固定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的注册与 MFA 两文件，均 HTTP 200；锁定版本不变，不复制上游备用协议。
- 本地 `run_roxy_registration` 五处 Email verified callback 后才记录取信水位，导致回调期间的新邮件被过滤；水位移动至 callback 前，五分支虚拟时钟回归先失败后通过，保持旧码排除、同窗、原预算与重试次数。
- `fetch_latest_otp` 的总等待 deadline 优先于末次单个请求的快速失败阈值，保留 mail_list/mail_detail/HTTP 分类；精确 Route Error (4xx/5xx) 在原 OTP 状态 helper 中独立终止，标注渲染页 page_status，不增加验证码重放。
- 独立安全 worker 复用既有 stage/error 字段保留密码/Session 错误阶段、code、HTTP 状态，删除原始异常正文/traceback 输出。密码终态 checkpoint、当前 Token 与身份匹配、activate `success is True`、只读校验分离均保留。
- 批次结束并核对无待执行注册、安全设置与浏览器后，仅重启 5001 服务为 PID 42452；账号/任务/首页 HTTP 200，18794 与 50000 服务保持原 PID。证据、完整回归、八项自检和未完成项见 `docs/2026-09-12_近期失败日志与OTP时间水位修复-report.md`。

## 本次流量共享边界与会话隔离修复（2026-09-12）

- 修改前在内存重新读取锁定 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 browser_traffic、config/roxybrowser、roxy_registration、roxybrowser_client 四文件，均 HTTP 200；锁定版本不变，hash 与差异见本次报告。
- 上游默认关闭低流量，且缺少本站已有的响应私有性/完整性门禁，未照搬其宽缓存和登录后应用壳拦截。本站仅共享精确公共 CDN 的无凭据/hash 名/public immutable JS/CSS；ChatGPT 同源、动态配置、认证与 Cookie 请求走各自 Profile。schema 3 自动忽略旧缓存；响应头白名单、MIME、原始 freshness 和 session/finalize 状态共同约束回放。
- 删除粗粒度 URL 黑名单，用已有 Fetch handler 精确处理公共 CDN 可选媒体；不再整域拦截 Statsig/feature gates、身份提供方、图片/字体/manifest。session 标记只停止共享缓存，不阻断后续密码/MFA 应用壳；不修改独立 Profile/指纹/出口边界。
- 此节取代此前“Cookie 公共候选可共享”及宽域静态缓存的当前实现说明；2026-09-03 报告保留为历史证据。密码成功 checkpoint、邮箱匹配、同窗 Token、enroll/activate 成功确认与只读校验分离已复核。
- Finding → Path、上游 hash、验证与八项交付自检见 `docs/2026-09-12_流量缓存会话隔离修复-report.md`。

## 本次按教程恢复折中公共静态缓存（2026-09-12 下午）

- 已读取用户 `注册流量优化复现与使用教程.docx`，把附件作为技术参考；采用公开静态分层、冷/热对照和计量，不照搬其动态配置/字体/图片整类拦截及登录后应用壳屏蔽。
- 重新获取锁定 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的流量、Roxy 配置和注册文件，均 HTTP 200，版本不变。本地现场 7 条汇总为零候选，主要流量来自被上一版排除的 `chatgpt.com/cdn/assets/`，响应 public/max-age 但无 immutable。
- 原路径恢复该精确静态前缀的版本化 JS/CSS 缓存；Cookie 只作为公共请求的环境头，不保存/回放；Set-Cookie、私有、变体、认证与动态配置保持实时。登录阶段仍共享已校验公共文件，恢复/终态边界保留。现有 TTL 从运行值 399 秒调为 86400 秒，源站剩余 TTL 仍约束。
- 真实双独立浏览器环境 3 文件冷写成功、热命中 3/3：网络 1,871,666 → 0 字节；只验证公共资源，不自动注册。全量 `992 passed, 1 deselected, 1 warning, 284 subtests passed`，密码/MFA边界复核。
- 此节取代上午仅 cdn.openai.com/必须 immutable/session 停缓存的当前策略说明；完整证据、hash、加载结果与 R8 见 `docs/2026-09-12_公共静态缓存折中优化-report.md`。

## 本次 2FA 补密码邮箱参数漏传修复（2026-09-12 晚）

- 修改前重新在内存读取锁定 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 account_export、roxy_registration、email_provider 与 outlook_client，均 HTTP 200；版本不变。上游无本地 `post_login_add_password` 扩展，保持本地身份匹配、历史邮件水位和同窗 MFA 边界。
- Job 814/816 的补密码邮箱阶段在第一次 `mail_list ReadTimeout` 后提前失败。只读实查两邮箱的新登录邮件均在水位后且可被现有解析器提取，邮件收到时间早于任务退出；当前查询不用于推断当时列表的可见时刻。
- 原 `_setup_password_with_driver` 漏传三项既有 `TWOFA_GENERIC_API_*`，误用普通注册运行值 5/3/1。现在与既有协议重认证分支一样显式传入 12/8/2（可配置），总等待预算、旧码/旧消息过滤与传输错误不重发规则不变。
- 终止错误仅从异常提取白名单邮件阶段、错误类型与 HTTP 状态至现有 message/http_status，删除仅保留异常类名而丢失阶段的诊断；不保存原始异常 URL、邮件正文或 OTP。
- Job 802 已读到验证码但页面未推进，独立列为后续页面提交问题，本轮不重放 OTP。证据、上游 hash、验证、安全核对和 R8 自检见 `docs/2026-09-12_2FA邮箱取件提前失败修复-report.md`。


## 本次验证码提交确认与导航分类修复（2026-09-12 晚）

- 修改前重新在内存获取锁定 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 account_export 和 roxy_registration，均 HTTP 200；锁定版本不变。上游没有本地补密码 helper，不复制上游广域按钮选择或备用协议。
- Job 802 已读码而 `submitted post_count=0`：删除补密码 OTP 点击后 0.5 秒伪确认，持续被动观察至原预算内真实响应/页面推进；Selenium CDP 线程回调乱序通过局部有界 request-id/status 关联处理。无确认/pending/HTTP错误分别记录，不盲目补点。
- 密码/OTP 按钮限定当前字段所属表单，跳过隐藏、禁用、aria-disabled、resend；模糊点击异常不再接全页 JS/Enter。注册 OTP 也限定所属表单并使用单次原生控件点击。
- 注册 OTP 当前 15 秒即刷新同码的设置改用既有 `ROXY_OTP_SUBMIT_TIMEOUT=45`、`ROXY_OTP_SUBMIT_ATTEMPTS=1`，连续观察、pending 停止，默认/示例/当前环境与帮助文案同步；不增加配置或第二套执行器。
- Chrome 错误页从普通推进超时分离为 `email_navigation/browser_navigation_error`。出口占用、冷却及漂移保护保留，不以复用出口提高成功率。
- 删除补密码敏感页原始截图，收窄相关异常原文/完整 URL 诊断；密码成功 checkpoint、同窗 Cookie/Token/邮箱匹配、activate 成功确认、只读 Token 校验分离均复核。
- 本地回归与未验证范围见 `docs/2026-09-12_验证码提交确认与导航分类修复-report.md`。本次不启动真实账号注册或历史账号安全重试；运行加载结果单独记录，不将代码回归视为真实批次成功率。


## 本次套餐查询卡顿与有界重试修复（2026-09-12 晚）

- 锁定 commit 不变。重新通过 Git tree 确认旧索引套餐文件简写的完整路径为 `vendor/turb_gpt_free_register/paypal_global_rotation_source/gpt_account_plan.py`；该文件与 `vendor/turb_gpt_free_register/core/session.py` raw 均 HTTP 200。没有同步其中的支付写操作。
- 当前套餐 VN 池 100 条不同配置共用一个网关，原逻辑已洗牌轮转，并非固定单条。两条不同配置只读对照同一账号：1.70 秒 TLS 失败 / 4.69 秒 HTTP 200；未检测实际出口 IP，结果不证明出口唯一。
- 原账号 worker 强制 max_attempts=0，忽略有限配置；改为读取既有配置，探针默认最多 3 次/允许 1–5，旧 0 兼容为 3。当前及默认单次 timeout 15 秒，失败释放 worker并保留旧权益；重试沿原检测池选路并纳入原限速，退避支持停止。
- 注册、密码、MFA、国家选择和身份隔离边界未改。完整证据、上游 hash、测试、交付自检及重启状态见 `docs/2026-09-12_套餐查询卡顿与有界重试优化-report.md`。


## 本次 2FA 旧邮件误取与阶段耗时修复（2026-09-13）

- 修改前重新获取锁定 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 account_export、roxy_registration、generic_api_mail_client，均 HTTP 200；版本不变，不覆盖上游或新增协议路径。
- 截至 10:04 的安全设置结果中，11 次补密码邮箱 HTTP 401 均再次锁定注册时的旧邮件时间，且没有有效历史 OTP 快照；另有 1 次 activate HTTP 400 单独未定因。详情页时间仅用于历史码例外判断、JSON 过滤后整页正则再取旧码是本地可复现缺陷。
- 原 `fetch_latest_otp` 在候选锁定前统一验证详情/结构化时间，保留两秒容差、列表消息 ID/分钟精度与同码新邮件规则。原快照/补密码/MFA helper 增加脱敏阶段与耗时日志，不扩大邮箱/写请求预算、不换注册代理。
- 密码终态 checkpoint、同窗 Cookie/邮箱匹配、Token 刷新透传、activate 成功确认以及只读校验分离均保持并回归；完整证据、上游 hash、测试与加载状态见 `docs/2026-09-13_2FA旧邮件误取与阶段耗时修复-report.md`。


## 本次重复出口放行与 Chrome 内核选择（2026-09-13）

- 重新读取锁定 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 roxy_registration、roxybrowser_client、config/roxybrowser，均 HTTP 200；版本不变。上游没有本地进程内出口 lease，也没有独立 Chrome 版本配置。
- 按维护者最新要求，删除本地出口 IP 占用/冷却机制和冲突终止分支；同一 IP 允许并发及连续使用，独立 Profile/随机指纹、真实预检及窗口内漂移复核保留。本节取代此前索引/历史报告中“每账号独占出口和释放后冷却”的当前要求，AGENTS.md 同步更新。
- 依据 Roxy 官方 `/browser/create` 文档，在现有创建链路增加唯一 `ROXY_CORE_VERSION`：latest 删除模板残留 coreVersion，指定主版本则传字符串，同时设置 coreType=Chrome。UI 接入现有配置保存/热加载，只影响之后新建窗口，不伪改 UA 或复制旧 Profile。
- 密码成功终态/ checkpoint、同窗 Cookie/Token、邮箱匹配、enroll/activate 成功确认、只读校验分离均保持，未修改这些状态机。回归 `1058 passed, 1 deselected, 1 warning, 338 subtests passed`。
- 上游 hash、完整配置链路、前端交互/后端参数验证、八项自检及待重启状态见 `docs/2026-09-13_Roxy重复IP与Chrome内核选择-report.md`。

## 本次嵌套邮件时间丢失与 2FA 失败续查（2026-09-13 中午）

- 修改前重新获取锁定 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 account_export、roxy_registration、generic_api_mail_client，均 HTTP 200；版本不变，不覆盖上游或新增协议路径。
- 已加载上一轮修复的新日志仍出现 `mail_snapshot ReadTimeout → structured_api ts=None → password_email HTTP 401`。只读接口实际返回 `data{code,body,date,subject}`；原解析器从完整 JSON 提码却只读外层时间，导致上一轮时间门禁失效。
- 原 `_extract_structured_api_code` 先归一化已观察到的单邮件 data 包装，再从同一对象解析 OTP 和时间；不借用外层 API 时间、不新增预算/重试。真实响应内存对照与空快照旧信→新信/持续旧信回归通过，密码/MFA 安全边界未改。
- 两次 Session GET 403、两次窗口出口复核失败、历史 activate 400 分开记录；本次不声称远端故障已解决。证据、上游 hash、1062 项全量回归、R8 与待重启状态见 `docs/2026-09-13_嵌套邮件时间丢失与2FA失败续查-report.md`。

## 本次 SMSBower 第二平台与 Codex desktop-auth 扩展（2026-09-13）

- 保留既有 HeroSMS 地址/密钥字段；新增 `SMS_PROVIDER`、`SMSBOWER_API_BASE`、`SMSBOWER_API_KEY`，两套 handler API 完全独立，OpenAI 服务代码仍为 `dr`。
- Roxy Codex 授权继续使用 `CODEX_HEADLESS=True`、`CODEX_LOCAL_PROXY`，动态 PKCE/state 地址可选包裹 `chatgpt.com/codex/desktop-auth`，回调仍校验原 state 并按 CPA/sub2 原路径导出。
- WebUI 新增接码平台、国家、价格/库存查询框及 `/api/sms/countries`、`/api/sms/prices`；查询临时切换平台并由锁保护，不改变默认运行平台。
- 注册流量列表同时显示新增网络下载与含缓存回放的逻辑总量；近期日志约 2.5–3.3 MiB 是真实新增网络字节，逻辑资源总量约 38–87 MiB，二者未混算。
- 左侧新增“接码中心”页面：顶部选择平台/国家并查询价格库存、应用设置；底部复用账号分组接口展示同一批账号。
- 接码中心现按账号页风格显示账号资料、出口流量、套餐/资格、安全与 Codex 状态；国家下拉同时提供“自动选择（按价格和库存）”与 API 返回的手动国家项，选择账号后可直接开始接码。
- 接码中心顶部已改为带说明的多字段控制面板（平台、国家、服务、单价、等待时间、轮询间隔）；账号勾选与批量开始按钮复用账号页视觉组件和现有 Codex retry-bulk 后端。
- 修复本地 7890 代理未监听时国家列表只能停留自动项的问题：SMSBower 国家/价格只读查询增加直连重试，取号与短信轮询仍保持本地代理；实测返回 202 个国家、28 个有库存报价。
- SMSBower 价格查询现省略 `service` 获取完整国家级别映射，服务端返回 `countries[].tiers[]`（service/name/cost/count）并保留 `offers` 兼容字段；接码中心以国家卡片和多档位表展示，支持自动与手动国家选择。直连实测 `getPricesV3&service=dr` 返回 122 个国家、1,725 个供应商档位；美国 ID 187 返回 19 档，上限 0.15 的档位在界面标注超预算。

## 本次 Clash 独立接码代理与正式接码核查（2026-09-13 下午）

- 修改前重读固定 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 roxy_codex_oauth、roxybrowser_client、account_export，均 HTTP 200；锁定版本不变。对照上游密码/TOTP 登录状态推进，复用本地账号匹配、表单限定点击与脱敏日志，不复制第二套执行器。
- 独立接码旧固定 7890 未监听，Clash 实际端口为 7897。现有 `CODEX_LOCAL_PROXY` 默认 system，共享解析器由 Roxy/协议/SMS 读取；元数据也经本地 Clash，此节取代先前 SMSBower 国家/价格直连的当前策略说明。
- 单账号登录实测完成密码/TOTP 并停在手机号入口；后续按用户要求正式接码一个任务、沿既有两次尝试配置取号 2 次。第一条持续等待短信超时，第二条换号后 invalid_auth_step；两订单均复查 STATUS_CANCEL，Profile 已清理。回调/导出未验证，不宣称全流程成功。
- 修复失效授权页仍空等/补提交、页面核验晚于取号、平台日志写死 HeroSMS；回归 142 passed / 9 subtests。密码 checkpoint、MFA activate 成功门禁与 Token 边界未改。
- Roxy 152 实测忽略无头参数；用户选择保留 Roxy、暂缓真正无头。保留现有参数但跳过接码可见窗口居中，不把配置 True 当作无头成功。完整证据、上游 hash、安全复核及 R8 见 `docs/2026-09-13_Clash接码代理与登录实测-report.md`。
- 用户要求修复后再次正式复测：17:21:48–17:25:03，一个任务沿原两次配置取号 2 次，仍未收到短信。invalid_auth_step 本次及时终止，未再空等 120 秒；真实短信交付及后续回调未通过，结果与取消核对追加同一报告，不自动扩大付费次数。

## OAuth sub2 导出对照与实测（2026-09-13）

- 接码成功后保存的 `core/codex_oauth.py:build_codex_storage` 是完整 OAuth storage；固定 GPT 上游 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 raw `core/codex_oauth.py` 已复核，SHA256 `289ad64b1fc91d3d7fcd0eba2895986754e63b8d0c313d6f8bf895ca32544cba`。本地只新增格式转换，不改变上游授权/Token 交换。
- Cockpit Tools 对照固定 commit `cc4a3f4a1573143f8551fa27825aec443e2df09f` 的 `src/utils/codexExportFormats.ts`，SHA256 `b47b24e78b0a033533f9525c37ff42576ad66311e9b77bc65a6fc0e8cf215558`。本地 `build_sub2api_oauth_account` 生成同一 `sub2api-data` v1 OAuth schema，保留 refresh token，access token-only 仅在有明确 expiry 时允许。
- `webui/app.py:api_accounts_download_codex_bulk` 替代旧 CPA-only endpoint：账号页选择 → format → 本地 OAuth 读取/身份校验 → CPA ZIP 或 sub2 JSON → 一次性 prepared download。旧 `/api/accounts/download-cpa-bulk` 与旧 handler 已删除；Agent Identity 生成/上传接口不变。
- 实测账号 ID 255 已生成并下载 sub2 与 CPA 文件，HTTP 200；本地三类 Token 身份对照通过，导出 metadata 不含 Token。定向测试 `tests/test_codex_oauth_export.py`、WebUI helper、retry logging 共 46 passed，inline JavaScript syntax 通过。

## 本次按注册流量教程扩展低流量拦截（2026-09-13）

- 修改前重读本索引并保持上游锁定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`；未覆盖上游注册实现，仅调整本站 `core/browser_traffic.py` 的既有 `block_reason` 与 Fetch pattern 安装路径。
- 参考用户教程的资源分层，低流量模式现在拦截已知遥测域、可选第三方登录域，以及 ChatGPT/Auth/CDN 一方的图片、媒体、字体和 manifest；Cloudflare、Arkose、hCaptcha、reCAPTCHA、Sentinel、challenge 路径和动态认证请求继续放行。
- 共享缓存范围、Profile 隔离、Cookie/Authorization/Set-Cookie 门禁、session/Token/密码/2FA 流程未改；低流量关闭或注册恢复时，Fetch 仍按原路径禁用并恢复联网。
- Fetch pattern 与唯一 `block_reason` 共用同一判定，避免再增加第二套 URL 黑名单；查询串、片段、编码/路径穿越资源继续放行，遥测与可选身份域允许正常带查询参数被拦截。
- 验证：`tests/test_browser_traffic.py` 通过 `42 passed, 204 subtests`；注册/代理/OTP/Session 相关回归通过 `144 passed, 230 subtests`。尚未启动真实注册批次，不把单元回归等同于代理账单下降。
- 19:36 真实批次发现旧版 Roxy CDP 拒绝过大的按资源类型 pattern 矩阵，造成 `candidates=0/hits=0/errors=1`；后续日志进一步确认是 `Manifest` 枚举被拒绝；现移除该枚举，保留 image/media/font 类型 pattern，manifest 使用 URL-only pattern，再由 `block_reason()` 二次判定，并在流量汇总中输出详细安装错误。
## 本次 Roxy 流量 Fetch 兼容性核对（2026-09-13）

- 现场日志：`注册日志/d9d46d0e-4af4-4770-b7b1-125f170a254b.log`、`注册日志/0f3b87e0-1c8b-4f20-8826-5636346c2910.log` 等。共同证据为 `candidates=0 hits=0 misses=0 blocked=0 errors=1`，安装错误为 `Unknown resource type in fetch filter: 'Manifest'`；因此 `Fetch.enable` 整体未安装，约 10 MiB 回源字节全部按实时网络下载。
- 对照锁定上游 `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的 `vendor/turb_gpt_free_register/core/browser_traffic.py`：Roxy 低流量规则采用 URL pattern，未把 `Manifest` 资源枚举加入 Fetch filter；静态资源缓存按 JS/CSS 和一方 Host 过滤。
- 本地修改：`core/browser_traffic.py::RoxyTrafficOptimizer._install_fetch_cache` 移除不兼容的 `Manifest` 枚举；ChatGPT/Auth/CDN 一方 image/media/font 继续使用已支持的资源类型 pattern，manifest 使用 URL-only `*manifest*` pattern，再由既有 `block_reason()` 二次判定路径、查询串和安全白名单；遥测/可选身份域保持 URL-only。没有新增第二套拦截器，也没有改变 2FA 的认证请求。
- 2FA 复核：重新读取上游 `core/account_export.py`，本地 `setup_2fa_from_selenium` 与上游保持邮箱匹配、同一 driver、显式 `access_token`、`/backend-api/accounts/mfa/enroll` → TOTP → `activate_enrollment` 且仅 `success=true` 保存 Secret 的顺序。
- 验证：`tests/test_browser_traffic.py tests/test_roxy_registration_otp_recovery.py tests/test_roxy_registration_session_recovery.py tests/test_registration_local_proxy_mode.py` 共 `144 passed, 230 subtests passed`。下一批真实批次以 `errors=[]`、`candidates>0`、`blocked_by_reason` 和热缓存 `hits` 验收。
