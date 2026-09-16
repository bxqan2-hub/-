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

## 交付自检（R8）

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
