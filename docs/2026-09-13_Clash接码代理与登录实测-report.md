# Clash 接码独立代理、登录修复与正式接码实测

日期：2026-09-13。范围：账号页单账号 Codex / SMSBower 接码；测试账号 ID 255。

## 结果与边界

- 本地 Clash 实际端口是 `127.0.0.1:7897`，旧配置 `7890` 未监听。独立接码、短信平台查询/轮询及本地 OAuth Token 交换统一走现有 `CODEX_LOCAL_PROXY=system`，不抽注册代理池。
- 17:06 的单账号登录探针完成 Roxy 创建、动态授权页、已保存密码与 TOTP，17:06:53 到达 `/add-phone`；该探针在取号前停止并清理 Profile。
- 用户随后要求正式接码一次：通过原 `/api/codex/retry` 提交一个账号任务，未另写执行器或修改国家/价格。原配置为 SMSBower、OpenAI/dr、美国/187、供应商 3449、上限 0.126 USD、最多 2 次、短信等待 120 秒、轮询 5 秒。
- 正式任务取号 2 次。第 1 条进入 `/phone-verification`，22 轮均为 `STATUS_WAIT_CODE`，120 秒后取消。第 2 条换号提交后页面为 `invalid_auth_step`，无表单/输入框；原代码仍等了约 120 秒。未收到短信验证码、未捕获 callback、未产生新的 OAuth 凭证。
- 两订单在结束后只读复查均为 `STATUS_CANCEL`；这证明取消状态，不等价于已审计余额变动或实际退款金额。没有为追求成功追加第 3 个号码。
- 真实 Roxy 152 `/browser/open` 接受 `headless=True`，但进程仍有主窗口；单独空白 Profile 复查与追加 `--headless=new` 均如此。用户明确选择“保留 Roxy，暂缓处理无头”。配置保留 True，但本次不宣称实际无头成功。
- 修复已通过原 BAT 加载，当前服务 `127.0.0.1:5002`，监听 PID 32188；`/login` HTTP 200，SMSBower 国家 HTTP 200 / 202 项，美国价格 HTTP 200 / 1 国家 / 1 个预算内报价。

## 修复后用户要求的第二次正式复测

- 17:21:48 再次从账号页原接口启动一个任务；国家、供应商、单价上限与最多两次均未调整，Roxy 保留。
- 17:22:07 密码提交、17:22:10 TOTP 提交、17:22:11 到达 add-phone；17:22:12 日志确认先核验页面，17:22:14 才取号。实际日志平台名为 SMSBower。
- 17:22:25 进入短信验证页；第一条依然只返回 STATUS_WAIT_CODE，17:24:30 超时取消。两次正式任务的第一条均未收到短信，这支持交付阶段问题，但仍不等同于已证明供应商或站点哪一方有故障。
- 17:24:44 换号前先确认表单；第二条提交后，17:24:58 页面 invalid_auth_step 被立即识别并终止，不再等待 120 秒/重复提交。原来强行回到 add-phone 后出现的远端状态失效尚未解决，门禁只验证页面已呈现，不保证后续 POST 仍被远端接受。
- 17:25:03 浏览器关闭、删除，账号状态 failed；本次共 2 个号码、0 条验证码。第二单因最短时限安排 112.3 秒后取消；17:27:00 两单复查均为 STATUS_CANCEL，任务前后平台余额差为 0.000（余额快照差，未另查账单明细）。
- 当前 callback、Token 保存、CPA/sub2 导出仍未实测。未追加第三次任务或擅自更换国家/价格。用户账号原日志路径现展示本次复测，前次关键证据保留于本报告；新一轮收尾摘要写入忽略的 `run/codex-sms-retest-result.json`。

## 原因分级与证据核对

| 概率/分类 | 核对证据 | Finding / Path / 处理 |
| --- | --- | --- |
| 高：代理端口错配 | 原日志 curl_code=7；7890 无监听；Windows 系统代理与 Clash 实际监听均为 7897；经该代理访问身份站与元数据成功 | `config/codex.py:resolve_local_proxy` 统一解析已有配置；拒绝非本地地址，不回退注册池 |
| 中：登录阶段识别错误 | 首次探针停留 `/log-in/password`，账号库已有密码/TOTP；旧 helper 仅选邮件登录且超时后继续取邮件 | `core/roxy_codex_oauth.py:_complete_login_after_email` 按当前表单提交密码/TOTP，确认实际页面推进才继续；重入也传递推进/失败结果 |
| 低：邮箱收件、MFA 本身故障 | 该账号密码、TOTP 实测均通过；此次正常分支未进入邮箱 OTP | 不修改邮箱服务、密码写入或 MFA enroll/activate；邮件分支保留有界轮询回归 |
| 高：短信尚未交付到供应平台 | 第一条号码进入短信验证页，平台连续 22 次 `STATUS_WAIT_CODE`；没有 `STATUS_OK` | 归类为短信交付/等待超时，不称为本地代理连接失败；具体归因到站点、运营商或供应商仍需投递记录 |
| 中：换号后的授权状态失效 | 第二条号码页面正文明确 `error_code: invalid_auth_step`，路径仍 `/add-phone`，inputs/forms 为空 | `:_wait_after_phone_send` 先识别失效，停止等待/补提交；`:_ensure_add_phone_input` 在导航前检查，原调用移动到付费取号前 |
| 低：轮询解析或平台混用 | 请求构造使用独立 SMSBower URL/key；getStatus 返回合法等待状态；日志却写死 HeroSMS | `core/sms_provider.py` 动态平台日志/错误名，保留两套 URL/key；两平台请求路由、密钥、OTP 脱敏回归 |

对第二条的修复是**失败及时识别与取号门禁**，不是“远端授权恢复已成功”。浏览器返回错误的业务原因、换号时页面状态迁移仍未做成功实测。不扩大付费尝试预算。

## 上游对照

修改前按 `docs/UPSTREAM_PROJECT_INDEX.md` 读取固定 Torin-x/GPT-utral-platform commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`，对应 raw 均 HTTP 200。锁定版本未变：

| 上游文件（vendor/turb_gpt_free_register/ 下） | SHA256 |
| --- | --- |
| core/roxy_codex_oauth.py | 8a6b2ab48bac00b3fbcbff84edfd9ad5a27661d047b99607806420d6de16015d |
| core/roxybrowser_client.py | 6cb5281a320080425544727c6aa1dc95c40f3183d814f381e9f64e002d6c1e9c |
| core/account_export.py | 985b99e669711d3de50dd6072ba83f660ac74d5ea156bf84f829c8fb258d0547 |

上游 `_submit_password_and_totp_login` 已有密码/MFA 后等待下一阶段；本地复用现有账号密码提取、表单限定点击、TOTP 时间窗函数，不复制另一套广域 XPath 点击执行器。Roxy 显式代理和 Token 交换链路参考上游；注册复用 Profile 分支未做本轮实测，不以独立接码测试覆盖它。

Roxy 官方文档的 open 示例列有 headless，但同页说明启动参数 `--headless` 不生效；以实测进程为准，不把接口接收配置等同于运行成功：[Roxy API 文档](https://roxybrowser.com/docs/api-documentation/api-endpoint.html)。

## 密码 / 2FA 与凭据复核

- 本次只读取所选账号已保存的密码和 TOTP；数据库邮箱须与任务邮箱匹配，凭据仅提交到精确 HTTPS `auth.openai.com` 标准端口。
- 密码与 TOTP 各阶段只提交一次；限时等待页面确认，不把停留密码页误报为邮件已发送；失效表单不重复写入。
- 沿同一个浏览器登录，独立任务始终显式传本地 Clash；未改注册密码成功终态 checkpoint、MFA enroll/activate 成功确认、显式 Token 透传及只读校验的既有实现。
- 新增登录日志不记录密码、TOTP Secret 或原始验证码；邮件/SMS 收码日志使用已有 `mask_otp`。账号错配、异常 origin、提交超时和 OTP 日志回归通过。
- 本次无设置密码、MFA enroll/activate 写操作。相关既有安全测试通过不等于重新实测远端安全设置。
- 历史授权 URL、callback 诊断及手机号页面状态日志并非本次全面审计范围；正式日志留在 Git 忽略目录，不推送凭据/完整运行明细。未声称整个仓库所有日志均已脱敏。

## 可复现记录（仅本地，Git 忽略）

- `run/codex_clash_login_probe.py`：调用原业务流程，只在手机号入口通过测试桩停止；禁止测试桩购买号码，临时可见配置不写入 .env。
- `run/codex-clash-login-probe.json`：手机号页、空号码输入框、零采购、Profile 已清理。
- `注册日志/codex-clash-login-probe.log`：17:06 登录阶段记录。
- `logs/codex-clash-phone-ready.png`：预检探针的手机号页截图。
- 正式接码使用账号页同一个后台接口与该账号已有日志文件，运行起止 17:12:10–17:17:46；测试临时 Profile、空白无头核查 Profile 均已关闭/删除。
- `logs/clash-codex-regression.txt`：定向回归输出；未追加全仓耗时测试。

## 八项交付自检

1. **已有实现**：`config/codex.py` 现有代理字段；`core/codex_oauth.py:run_codex_oauth`；`core/roxy_codex_oauth.py` 登录、取号前门禁、等待与 Roxy 入口；`core/sms_provider.py:_http/wait_for_sms_code` 等原请求/日志；`core/codex_retry_service.py:run_worker`；`webui/app.py:api_sms_countries/api_sms_prices`；`webui/config_editor.py` 现有字段文案与 `.env.example`。
2. **新增符号**：共享 `config/codex.py:resolve_local_proxy` 取代旧 Roxy 内部代理解析；`:_complete_login_after_email` 取代原仅密码less入口 helper。旧 Roxy 符号已删除。测试补充同账号密码/TOTP、重入状态、平台隔离、无头参数传递/不居中、失效授权不取号/不重复提交。
3. **搬迁**：代理解析职责从 Roxy 文件移动到已有配置文件；原符号删除，两个文件仍各有其他业务而保留。未新增平行模块、备份树或新执行器。
4. **配置链路**：未新增配置项。现有配置 UI/.env → env override/reload → `CODEX_LOCAL_PROXY` → `resolve_local_proxy` → Roxy Profile / BrowserSession / SMS `_http`；`CODEX_HEADLESS` → `open_profile(headless=...)`，并跳过可见窗口居中。真无头按用户选择暂缓。
5. **死引用回扫**：下述定向命令输出均为空。全仓另一个 browser_use 后端有同名独立密码less helper，自有调用仍有效，未作为 Roxy 死引用删除；历史报告不改写。

   ```powershell
   rg -n '_resolve_codex_local_proxy|_maybe_click_passwordless_after_email' config/codex.py core/roxy_codex_oauth.py core/codex_oauth.py webui/app.py tests/test_roxy_codex_otp_polling.py tests/test_roxy_codex_proxy_preflight.py .env.example
   rg -n 'use_proxy=False|\[SMS:HeroSMS\]|provider = "herosms"' core/sms_provider.py core/roxy_codex_oauth.py webui/app.py
   # 输出：空；无匹配退出码 1。
   ```

6. **diff**：代码/配置/测试 `+431 / −173`；含本报告/索引合计 `+534 / −173`。删除原固定端口、注册池回退、短信元数据直连、密码页吞错继续取信、错误平台标签，非只增不删。
7. **测试**：为隔离用户当前平台，测试进程临时 `SMS_PROVIDER=herosms`，SMSBower 用例各自显式覆盖；不写 .env。10 个直接受影响测试文件，最后 5 行原始输出：

   ```text
   tests/test_roxy_codex_phone_classification.py::RoxyCodexPhoneClassificationTests::test_phone_send_waits_for_dom_transition_before_rotating PASSED [ 98%]
   tests/test_roxy_codex_phone_classification.py::RoxyCodexPhoneClassificationTests::test_whatsapp_label_does_not_override_selected_sms PASSED [ 99%]
   tests/test_roxy_codex_phone_classification.py::RoxyCodexPhoneClassificationTests::test_whatsapp_only_page_is_rejected PASSED [100%]

   =================== 142 passed, 9 subtests passed in 2.50s ====================
   ```

8. **未做/存疑**：短信交付端具体故障未定因；没有收到有效短信，callback/Token 保存/CPA/sub2 导出未实测；换号后的远端授权恢复未验证；真正无头按用户确认暂缓；其他浏览器后端与注册复用 Profile 未实测；取消已确认但实际账单金额未核对。保留原有三项无关 untracked 路径，不纳入提交。

## 回调成功后的 OAuth 导出修复（2026-09-13）

- 根因：账号页原来的 `上传 sub2` 只处理 Agent Identity；接码回调保存的是 Codex OAuth storage，因此回调成功后没有对应的 OAuth sub2 导出入口。旧 CPA 批量下载还优先依赖远端 CPA，导致本地刚保存的回调凭证没有形成统一导出链。
- 修复：账号页保留原选择框和按钮风格，新增蓝色“导出 sub2”，与“导出 CPA”共用 `/api/accounts/download-codex-bulk`。本地 `codex-*.json` 优先读取；CPA 输出兼容 ZIP，sub2 输出 `sub2api-data` v1 JSON，字段按 Cockpit Tools 的 OAuth 格式生成。Agent Identity 上传路径未改。
- 身份边界：导出前逐个校验账号邮箱、Token 中的邮箱和 `chatgpt_account_id`；sub2 保留 access/refresh/id token、client_id、过期时间及公开账号元数据，不导出密码、TOTP 或其他数据库字段。没有完整本地 OAuth 时，sub2 不调用远端；CPA 才保留旧远端 CPA 兼容回退。
- 实测：账号 ID 255 `cubit_betel9d@icloud.com` 使用已完成回调的本地文件生成 `accounts-sub2-20260913-100927.json`（1 个 OAuth 账号，三类 Token 字段齐全）和 `accounts-cpa-20260913-100927.zip`（凭证 JSON + manifest），下载 HTTP 200，内容与本地凭证语义一致；未重新接码、未产生新扣费。
- 验证：OAuth 导出/API 测试 46 passed；内联 JavaScript `node --check` 通过；选中账号后两个导出按钮按选择状态启用，导出接口不触发 SMS 请求。生成文件位于 Git 忽略的 `codex_accounts/`。
- 当前边界：本次只验证文件生成和 schema，不向第三方 sub2 服务执行导入；Roxy 真无头仍按用户先前选择暂缓。
