# 协议注册异常核对（2026-09-16）

本次实际页面登录已确认测试账号停用：密码步骤通过后，提交 TOTP 的 `POST /api/accounts/mfa/verify` 返回 **403 / `account_deactivated`**，页面显示账号已删除或停用。早先 AT 的 **401 / `token_revoked`** 与这一结果分别记录；这不是单纯的界面显示异常。

账号由本次获准使用的新邮箱产生，对应本地任务 559、账号 1248。原协议注册当时完成密码、邮箱 OTP 和 MFA enroll/activate，随后发生停用。本文不记录邮箱、密码、Cookie、TOTP Secret、验证码和 Token。根据本次用户指令，未拉取或比对上游，未新增第二个邮箱账号，未修改 AT 显示/判定实现或 WebUI。

## 证据与结论范围

排查最初按概率排序：高——服务端拒绝但具体原因被压成 HTTP 状态；中——Token 用途与接口或浏览器会话不一致；低——Token 本地过期。实际逐条核对后得到：

| Evidence | 结果 | 可以证明的范围 |
| --- | --- | --- |
| 原协议任务 559 的脱敏请求记录 | Auth 请求文档一直停在根路径，脚本数 0/1；Referer 却指定密码、OTP、资料页 | 原 transport 的页面生命周期与请求描述确实不一致 |
| 内核版本与 UAData | 实际内核 `151.0.7922.72`，原高熵完整版本只有 `151` | 原版本生成器将安装目录主版本误当完整版本 |
| 账号 Token 本地元数据 | 尚未到期；没有 RT/IDT | 本例并非本地过期，也没有证据支持 OAuth 导出 Token 替换导致本次失效 |
| 单次只读 `GET /backend-api/me` | 401，结构化错误 `token_revoked` | 这枚 AT 已撤销；该结果单独尚不足以认定账号停用 |
| 协议登录尝试 | 一次在 transport 阶段失败；补充诊断后一次加载密码页得到 500，被旧代码归成 Cloudflare challenge | 这些失败尚未完成密码验证，应与账号状态分开 |
| 实际页面登录 | 首次自动提交停在邮箱表单；后续按实际 DOM 操作，密码步骤进入 MFA，TOTP 提交返回 403 / `account_deactivated` | 已确认账号停用，页面也显示相同错误码 |
| 修正后无账号写入的浏览器核对 | 首页与 Auth 页面真实标题/脚本存在；完整版本一致；页面 SDK 就绪 | 文档/版本修正确实生效；没有证明修正后新注册账号的长期状态 |

停用的具体服务端决策原因未公开。上述协议差异是已复现的软件缺陷，尚未建立其与本次停用的因果关系。代码修复不会恢复已撤销 Token 或已停用账号。本次验证在明确停用后停止登录与账号写入。

本地证据保存在 Git 忽略目录：`run/protocol-consistency-evidence.json`、`run/protocol-consistency-summary.json`、`run/protocol-consistency-readonly.json`、`run/protocol-at-readonly.json`、`run/protocol-consistency-login.json`、`run/protocol-account-page-login.json`、`run/protocol-account-login-console.json`。原始诊断运行文件和日志未提交。

## Finding / Path / 修复

| Finding | Path | 修复与验证 |
| --- | --- | --- |
| 空白文档 fetch 被当成页面导航，Referer 描述未访问的页面 | `core/abais_protocol/local_browser_session.py:LocalBrowserSession._page/_fetch` | 页面 GET 走真实 Document 导航；API Referer 取当前文档；SDK 取页面实例。真实 Chromium 回环用例核对 DOM、脚本、URL、Referer 和重定向 Cookie |
| 主版本写入高熵完整版本 | `integrations/roxy_unlimited_windows/scripts/fingerprint.mjs:coreFullVersion/buildFingerprint/coreExe` | 从所选 EXE 的 ProductVersion 取完整版本；与安装目录主版本核对，失败终止创建。实际核对和本地组件测试通过 |
| 页面 CSP 与 SDK 等待逻辑冲突 | `LocalBrowserSession.build_headers` | 使用页面内有界轮询等待 SDK；没有关闭 CSP。nonce CSP 回环页面验证通过 |
| CDN 响应头被当成挑战依据，掩盖 403 停用和 5xx 业务错误 | `core/abais_protocol/protocol_register.py:_is_cloudflare_challenge_response` | 删除仅凭 server/cf-ray 与状态码认定挑战的分支；实际挑战仍按专用响应头/页面特征识别；停用响应进入终止错误处理 |
| 协议入口包装异常丢失停用错误码 | `core/abai_protocol_registration.py:run_abai_protocol_registration` | 将受控停用码映射到已有 AccountUnusableError；不附加远端正文；回归验证失败时没有 checkpoint 或账号落盘 |
| Session 失败后可能保留旧 MFA 身份；导出的 OAuth Token 可能被误用为 Web MFA Token | `LocalBrowserSession.request`、`ChatGPTProtocolRegister._finalize_registration_result` | Session 请求前撤销旧身份；MFA 前重新读取同窗 Session、匹配邮箱，并核对 Bearer 与已验证 Token 一致；OAuth 导出值保持独立 |
| 非布尔真值可能被当成密码/MFA 成功 | `core/abais_protocol/mfa.py:bind_totp_2fa`、协议入口 | activated/bound/password_registered 严格要求 `is True`；只有确认后保存凭据 |
| 清理异常跳过 Profile 删除或遮盖业务错误 | `LocalBrowserSession.close` | 汇总、CDP、Playwright、Profile 关闭/删除、HTTP 清理独立执行；每个失败点都有回归 |
| transport 失败缺少可用定位信息 | `LocalBrowserSession._fetch` | 保留受控 operation、phase、method、HTTP、异常类型、网络错误类别；不记录请求参数与响应正文 |

302/307 回环测试确认 Cookie 在下一跳保留，未发现原实现丢失 HttpOnly Cookie 的证据；因此没有将其列为封禁原因。没有加入随机延时、代理轮换或额外注册重试。

## 密码与 2FA 复核

- 注册状态机要求密码提交与邮箱 OTP 均完成才进入资料创建；入口仍在完整结果返回后才保存密码 checkpoint。
- MFA 前的同窗邮箱匹配、明确 Bearer 和失败撤权均有负向用例；错邮箱、空 Token、旧 Token、Session 非 200 或解析失败时停止 MFA 写入。
- enroll/activate 仅在明确布尔成功时允许保存 Secret；失败异常不输出 Secret、远端正文或请求参数。
- 密码页 500、浏览器错误、只读 AT 撤销、MFA 登录停用是独立事件，不会据此把一次未提交的密码操作描述为改密失败。
- 本次页面登录仅使用已有密码与 TOTP，没有重置密码、重新 enroll、更新 Token 或修改账号运行数据。

## 首轮交付自检（R8，675d1cb）

1. **修改的已有代码**：`core/abai_protocol_registration.py:run_abai_protocol_registration`；`core/abais_protocol/protocol_register.py:_is_cloudflare_challenge_response/_finalize_registration_result`；`core/abais_protocol/mfa.py:bind_totp_2fa`；`integrations/roxy_unlimited_windows/scripts/fingerprint.mjs:coreExe/buildFingerprint`。本任务开始时已存在且未跟踪的 `LocalBrowserSession` 继续修改后纳入提交；README 与本地组件说明同步。
2. **新增的代码**：Git 基线新增 `core/abais_protocol/local_browser_session.py:_BrowserCookies/LocalBrowserSession` 与对应测试，承接任务开始时的本地内核接入；入口原 `_next_profile/_proxy_rotator` 已删除。内部 `coreFullVersion` 替代 `buildFingerprint` 使用目录主版本的调用；原 `coreVersion` 仍供目录选择及版本校验使用，不是重复 transport。错误处理复用现有 AccountUnusableError，没有新增执行器或新旧路径开关。
3. **搬迁项**：无；没有复制项目树或创建备份文件。
4. **新增配置项链路**：无用户配置。既有 `ROXY_CORE_VERSION → LocalBrowserSession/RoxyBrowserClient → coreExe/buildFingerprint → 实际内核核对`。内部子进程参数 `ROXY_VERSION_EXE` 仅由所选 EXE 路径赋值，经 PowerShell `Get-Item -LiteralPath` 读取 ProductVersion，不是新增用户环境配置。运行结果继续消费现有账号 extra 与流量汇总字段。
5. **死引用回扫**：以下命令输出均为空（rg 退出码 1 表示无匹配）：

   ```powershell
   rg -n '_next_profile|_proxy_rotator|_PROFILE_POOL|_PROFILE_LOCK' core/abai_protocol_registration.py tests/test_abai_protocol_registration.py
   rg -n 'SentinelSDKManager|add_script_tag|wait_for_function|_synthetic_bytes|_synthetic_requests' core/abais_protocol/local_browser_session.py tests/test_protocol_local_browser.py
   ```

   CDP 的 `Fetch.fulfillRequest` 仅保留用于受控逐跳重定向捕获，不再用于生成初始空白网页。通用 vendored 环境工具仍供其他调用方使用，不作无关删除。
6. **diff 统计**：11 个文件，`+1252 / −96`；包含任务开始时尚未提交的本地 transport 与测试基础，不代表全部代码都在本轮新写。删除行数非零。
7. **测试**：以下相关回归全部通过；`git diff --check` 无 whitespace error。

   ```powershell
   .venv\Scripts\python.exe -m pytest -q tests/test_protocol_local_browser.py tests/test_abai_protocol_registration.py tests/test_roxy_local_component.py tests/test_roxy_proxy_enforcement.py tests/test_config_defaults.py tests/test_registration_browser_exit_geo.py tests/test_browser_traffic.py tests/test_twofa_registration.py tests/test_protocol_strict_alignment.py --tb=short
   ```

   最后 5 行原始输出：

   ```text
     C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Lib\site-packages\requests\__init__.py:92: RequestsDependencyWarning: Unable to find acceptable character detection dependency (chardet or charset_normalizer).
       warnings.warn(

   -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
   452 passed, 1 warning, 276 subtests passed in 23.48s
   ```

8. **未做的 / 存疑的**：
   - 已确认停用，但具体触发原因仍无服务端证据；没有把版本/文档差异写成已证实根因。
   - 修正后仅做真实页面初始化与本地回归，没有用第二个新邮箱重新注册，也没有长期账号可用性验证。
   - 协议登录曾在密码页获取阶段出现 500；修正错误分类不等于证明协议登录已完整兼容。页面登录已完成账号状态判定，停用后没有再重试。
   - 本例无 OAuth RT，未以此样本验证 OAuth Token 的长期有效性。
   - requests 字符集依赖 warning 是既有环境问题，本次未修改依赖。
   - 原有 `config/roxybrowser.py`、`main.py`、三个 `webui/` 文件的未提交改动，以及 PayPal 目录/测试不纳入本次提交；保留用户已有工作，因此全局工作树仍有这些改动。任务相关文件应提交完毕，并核对本地 HEAD 与 origin 当前分支一致。

## 后续核对：协议状态机与直接调用本地 Roxy

本轮使用现有代码、首轮证据和本地模拟响应验证。没有运行新的真实注册、探测停用账号或修改指纹生成器，也没有修改界面显示。之前的 `account_deactivated` 证据仍成立；以下缺陷不等于已查明停用触发原因。

两条路径在本地模式下均使用 `RoxyBrowserClient`。协议入口显式选择本地组件，Roxy 页面入口按既有组件配置选择；共享内核与 Profile 创建组件，不意味着共享同一注册状态机。

| 核对项 | 直接调用本地 Roxy | 协议模式 | 本轮处理 |
| --- | --- | --- | --- |
| 推进依据 | 页面元素、跳转和会话终态 | API 返回的 page/continue_url，经自有状态机判断 | 修正状态不明确时仍继续写入的分支 |
| 手机验证要求 | 资料处理仅在资料页或已有会话时推进 | 原代码在 add_phone 后继续调用 create_account | 复用已有手机验证判定；任何后续注册写入前停止 |
| 既有账号登录页 | 密码注册必需时识别为已有账号并停止 | 通用密码步骤判定也接纳登录密码页 | 注册状态机单独阻止登录密码步骤，不影响独立登录方法 |
| 密码步骤未前进 | 等待并观察页面终态后处理 | 同一状态可在循环中重复执行密码提交 | 已提交密码后再次出现密码步骤立即停止，不重复写入 |
| 缺失或矛盾的资料状态 | 等待资料页/会话；超时返回失败 | 原来空状态、URL 查询中含 about-you 或其他路径包含该子串均可被当成资料步骤 | 删除空状态放行；URL 只核对解析后的确切路径；未知 page_type 不被路径覆盖 |
| 密码/MFA 要求 | 受现有 ENABLE_2FA 配置控制 | 协议入口强制确认密码及 TOTP | 现存业务要求差异，本轮未调整开关或增加配置 |
| 资料来源 | 将入口 name/birthday 填入页面并保存 | 内部重新生成资料，入口仍保存传入资料 | 确认存在字段来源不一致，单列待处理；未据此断言停用原因 |
| MFA 与 AT 判定 | 激活结果和后续只读 Token 校验分开，结果可为 partial_success | 确认激活后返回注册结果，后续后台再检测 AT | 激活成功不能证明账号长期可用；本轮没有改为伪造“有效”或重试停用账号 |

代码依据：`core/roxy_registration.py:run_roxy_registration/_fill_password_page_if_present/_complete_profile_page/_switch_to_signup_password_branch`；`core/account_export.py` 的 `TwoFASetupResult` 生成与只读校验路径；`core/abais_protocol/protocol_register.py:_create_account_from_authorization/_session_result/_finalize_registration_result`；`core/abai_protocol_registration.py:run_abai_protocol_registration`。

**本轮修复的 Finding / Path / 验证**：四类状态问题集中修改既有 `_create_account_from_authorization`。测试直接提供本地模拟的授权状态，断言必需验证、既有账号登录和未知状态不会触发密码/账号写入，密码步骤停滞不会重复写入。包含手机号要求出现在初始、密码步骤 URL 和邮箱校验之后等情况，并保留明确正常状态只创建一次、不重复发 OTP 的正向用例。首次针对性验证为 `10 failed, 1 passed`，修复及补充既有账号用例后相关回归全部通过。

密码与 2FA 再核对：本轮失败均在资料创建或后续 MFA 之前抛出，沿原有错误包装和清理路径返回；没有新增 checkpoint、Token 刷新或 enroll/activate 调用。已有错邮箱、错 Bearer、Session 失败撤权、激活布尔确认与凭据脱敏回归继续通过。

### 本轮交付自检（R8）

1. **修改的已有代码**：`core/abais_protocol/protocol_register.py:ChatGPTProtocolRegister._create_account_from_authorization`。删除绕过必需步骤的继续创建分支和缺失状态的推测放行；未知状态错误改为受控阶段码。
2. **新增的代码**：生产代码没有新增函数或执行路径，复用 `credential_checks._requires_phone_verification`；测试新增 `registration_worker` fixture 与 6 组状态用例（展开 14 个测试）。它们覆盖原测试缺少的状态门禁，没有复制生产实现。
3. **搬迁项**：无；没有复制目录、生成备份或移动生产文件。
4. **新增配置项链路**：无；错误阶段沿既有 worker → 协议入口异常包装 → 注册任务错误记录传递。
5. **死引用回扫**：以下命令输出为空，退出码 1；没有遗留原 add_phone 放行说明或 URL 子串变量。

   ```powershell
   rg -n 'normalized_url|由服务端确认是否强制手机号验证' core/abais_protocol/protocol_register.py
   ```

6. **diff 统计**：本轮仅状态机、对应测试及本报告，3 个文件，`+166 / −18`。
7. **测试**：`git diff --check` 通过。相关测试命令及最后 5 行原始输出：

   ```powershell
   .venv\Scripts\python.exe -m pytest -q tests/test_protocol_local_browser.py tests/test_abai_protocol_registration.py tests/test_twofa_registration.py --tb=short
   ```

   ```text
     C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Lib\site-packages\requests\__init__.py:92: RequestsDependencyWarning: Unable to find acceptable character detection dependency (chardet or charset_normalizer).
       warnings.warn(

   -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
   291 passed, 1 warning in 22.60s
   ```

8. **未做的 / 存疑的**：没有新增线上账号或新的指纹试验；未证明这些状态分支曾在已停用样本上触发；资料字段来源差异、MFA 开关与注册成功语义差异仍独立列出。服务端停用的具体依据仍未知。本轮改动使协议尊重已返回的验证要求，而非恢复停用账号。原有界面、配置及支付相关未提交工作继续保留，不混入本次提交。

## 后续：协议流量优化与资料透传

本节续接前两轮记录。维护者明确排除套餐检查、其独立代理和界面显示；现有 Roxy 注册、Roxy 配置、指纹组件与共享流量优化器文件保持原样。本轮仅在协议适配器调用既有公共资源缓存和过滤规则。CDP 本地内核控制保留，临时诊断脚本与原始记录留在 Git 忽略目录，未接入生产入口。按维护者指令，本轮未读取或拉取上游。

### 现场证据与范围

账号 1249（任务 560）的持久化注册流量为 **16,591,811 B / 15.82 MiB**：下载 9,511,152 B、上传 7,079,321 B，另有少量 WebSocket 数据。上传中 `/awe/api/v2/rum` 占 6,651,379 B；下载中完整聊天应用的几个大型 JS 包占主要部分。这些值来自注册 transport，未把后续套餐检查混入。

| 样本 | 结果 | 说明 |
| --- | --- | --- |
| 任务 560 / 账号 1249 | 完整注册；15.82 MiB | 优化前基线。此前实际密码/TOTP 登录及一次延迟 Session/me 复核成功，仅代表观测时点 |
| 任务 561 / 账号 1250 | 完整注册；8,846,952 B / 8.44 MiB | 初轮公共缓存与可选资源过滤，密码和 MFA 成功；约减少 46.68%，但尚未达到约 2 MiB 目标 |
| 任务 562 | 首页响应捕获失败，未进入注册提交 | 去重实验在会话创建后 detach 竞争连接，影响该内核的响应拦截；已删除该实验，改为在创建受控页面时避免重复安装 |
| 任务 563、564 | 邮箱取码超时，未完成注册 | 首页、授权、密码及发码响应已通过；轮询 60 秒和事后只读快照均无验证码。失败流程流量不作为完整注册结果 |
| 任务 565 | 代理预检超时，未创建浏览器或提交注册 | 两个出口查询接口各一次超时；保留随机选定的同一邮箱，后续任务按原预检重新选取代理 |
| 任务 566 | 同一随机邮箱重试仍取码超时 | 正常代理预检后到达邮箱验证，60 秒未拿到验证码；暂停新增样本，不以该未完成流程的流量宣称约 2 MiB 达标 |

失败排查按概率分别处理：562 的高概率为 CDP 去重与响应捕获冲突、中概率为页面事件时序、低概率为网络响应异常；随后只读实际内核首页复现修正后 200，且同一主页请求只记录一个 Request/Response 对。563/564 的高概率为收码服务未返回验证码、中概率为旧码/时间窗口过滤、低概率为浏览器网络异常；实际记录终止类型为 `GenericApiMailError`，没有将其判为密码错误、MFA 错误或账号停用。

最终收尾资源限制已经过真实 Chromium 回环验证，但本轮三个到达 OTP 的新邮箱样本均未完成收码，尚未得到最终代码的完整线上注册流量。未把“取码接口无验证码”等同于已证明邮箱提供方故障，也未证明服务端是否实际投递。完整线上注册已证实的节省只有初轮约 47%；最终约 2 MiB 目标保持待验证。运行证据位于 `C:\Users\Administrator\Desktop\turb-gpt-free-register\run\protocol-traffic-summary.json`、`C:\Users\Administrator\Desktop\turb-gpt-free-register\run\protocol-traffic-cold-final-2-summary.json`、`C:\Users\Administrator\Desktop\turb-gpt-free-register\run\protocol-traffic-cold-final-3-summary.json`、`C:\Users\Administrator\Desktop\turb-gpt-free-register\run\protocol-traffic-cold-final-4-summary.json`、`C:\Users\Administrator\Desktop\turb-gpt-free-register\run\protocol-traffic-cold-final-5-summary.json`；未提交账号、日志或缓存。

### Finding / Path / 实现

| Finding | Path | 处理 |
| --- | --- | --- |
| 仅计量，没有公共静态资源缓存或可选资源过滤 | `C:\Users\Administrator\Desktop\turb-gpt-free-register\core\abais_protocol\local_browser_session.py:LocalBrowserSession.__init__/_on_request_paused` | 复用 `StaticResourceCache` 和 `block_reason`；协议缓存放在既有缓存目录的独立 `protocol` 子目录。认证、配置、挑战和带身份状态的响应保持实时 |
| 资料创建完成后仍加载完整聊天应用 | 同文件 `request/_on_request_paused` | 仅在创建资料返回成功且带后续地址时进入收尾阶段；复用既有 session-only 规则阻止聊天应用资源，真实 Document、回调、Session 和 MFA 仍通过 |
| 每个 API 请求开关 Fetch，后台资源监听随之变化 | 同文件 `_observe_target/_fetch` | 用一个持续监听的处理器同时承接原手动重定向与公共缓存；删除原嵌套 `paused` 和每请求 Fetch 开关，保留原重定向 Cookie、跨域凭据和方法语义 |
| 创建页面与 context page 事件发生重入，重复安装监听器 | 同文件 `_page/_observe_page` | 受控页面创建期间暂缓事件自动安装，返回后只安装一次；页面创建失败恢复观察状态。未采用运行中 detach 竞争连接的实验方案 |
| 逻辑缓存大小与外部网络字节容易混淆 | 同文件 `close` | 按精确 Network request ID 传给原估算器，分开网络下载、缓存回放与上传，不用解压后缓存大小声称节省代理费用 |
| 协议内部二次随机生成资料，落库却使用任务资料 | `C:\Users\Administrator\Desktop\turb-gpt-free-register\core\abai_protocol_registration.py:run_abai_protocol_registration`；`C:\Users\Administrator\Desktop\turb-gpt-free-register\core\abais_protocol\protocol_register.py:run/_run_legacy_web_registration/_create_account_from_authorization` | 删除内部 `_random_profile`；姓名、生日沿原调用链透传并用同一值保存。缺省生日在入口调用既有工具生成一次 |
| 关闭自动 Codex 后仍触发额外 OAuth | 后一文件 `_run_legacy_web_registration/_session_result` | 注册读取既有 `ENABLE_CODEX_AUTO`。False 时跳过 OAuth mint；独立登录默认保留原 OAuth 恢复语义，未扩大到 Roxy 路径 |

密码/MFA 复核：资源收尾标记仅控制可选资源，绝不授予邮箱身份或 Bearer 权限。MFA 仍要求同窗 Session 200、邮箱匹配和显式匹配的 Web Token；只有明确激活成功后保存 Secret，注册结果确认后保存密码 checkpoint。认证文档、SDK、CSP、Cookie、OTP、必需验证状态机保持原有检查；没有通过省流量伪造响应成功。

### 本轮交付自检（R8）

1. **修改的已有代码**：上表的三个协议生产文件；另修改 `C:\Users\Administrator\Desktop\turb-gpt-free-register\tests\test_protocol_local_browser.py`、`C:\Users\Administrator\Desktop\turb-gpt-free-register\tests\test_abai_protocol_registration.py` 和本报告。没有修改共享 `browser_traffic.py` 或 Roxy 实现。
2. **新增代码**：内部 `_on_request_paused` 替代 `_fetch` 内的嵌套回调，后者已删除；复用既有缓存、过滤和计量函数，没有新增 transport 或平行注册路径。新增测试覆盖公共缓存隔离、真实 Chromium 请求字节、实时认证、收尾阶段、监听重入、资料透传与 OAuth 开关。
3. **搬迁项**：没有文件搬迁或备份树；旧回调及二次随机资料逻辑已删除。
4. **配置链路**：没有新增用户配置。既有 UI/环境配置 → `config.roxybrowser.ROXY_LOW_TRAFFIC/ROXY_STATIC_CACHE/ROXY_CACHE_DIR/ROXY_CACHE_MAX_AGE/ROXY_CACHE_MAX_ITEM_BYTES` → 协议 `LocalBrowserSession` → 过滤/公共缓存；既有 `config.codex.ENABLE_CODEX_AUTO` → 注册 worker → `_session_result` 的内部参数。收尾阶段和缓存计数均有业务读取点，流量结果仍保存到既有账号 `registration_traffic` 并由现有界面读取。
5. **死引用回扫**：以下命令输出为空，退出码均为 1。

   ```powershell
   $root = 'C:\Users\Administrator\Desktop\turb-gpt-free-register'
   rg -n '\b_random_profile\b' "$root\core\abais_protocol\protocol_register.py" "$root\tests\test_protocol_local_browser.py" "$root\tests\test_abai_protocol_registration.py"
   rg -n 'def paused\(|Fetch\.disable|remove_listener\("Fetch.requestPaused"' "$root\core\abais_protocol\local_browser_session.py"
   ```

6. **diff 统计**：本轮 6 个协议相关文件，`+630 / −113`；删除了旧响应回调、每请求拦截开关和内部二次随机资料逻辑。
7. **测试**：真实 Chromium 回环验证两个隔离 Context 只下载一次 256 KiB 公共脚本；1 MiB 可选上传未到达服务端；资料创建后聊天脚本未到达服务端，但 Document、HttpOnly Cookie、Session 与匹配 Token 的 MFA 请求保持可用。共享缓存的私有响应、过期、摘要和 MIME 检查继续运行。`py_compile` 与 `git diff --check` 通过。最终测试命令及最后 5 行原始输出：

   ```powershell
   .venv\Scripts\python.exe -m pytest -q tests/test_protocol_local_browser.py tests/test_abai_protocol_registration.py tests/test_twofa_registration.py tests/test_protocol_strict_alignment.py tests/test_browser_traffic.py --tb=short --disable-warnings
   ```

   ```text
   ........................................................................ [ 36%]
   ........................................................................ [ 54%]
   ........................................................................ [ 72%]
   .............................................................................................................                [100%]
   397 passed, 1 warning, 236 subtests passed in 25.75s
   ```
8. **未做的 / 存疑的**：浏览器 CDP 负载估算不是代理商含 TLS/隧道开销的账单；Roxy 约 2 MB 是维护者给出的参照，本轮没有执行或改动其注册流程。最终完整注册低流量结果待可收码样本完成，未以中途失败流量宣称达标。账号长期状态、服务端停用原因不在本轮流量结论中。既有 requests 字符集依赖 warning 与邮箱客户端 TLS warning 未作为本轮依赖改动处理。用户原有配置、主入口、WebUI 和支付集成的未提交工作原样保留，故全局工作树仍包含这些原有修改；本轮仅提交上述协议相关文件。
