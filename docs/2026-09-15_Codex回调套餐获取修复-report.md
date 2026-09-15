# 2026-09-15 Codex 回调套餐获取修复

## 结果

- CPA callback 只有一次性 `code/state`，现在 callback 完成后会优先读取 CPA 已落库的完整 OAuth 凭证；sub2/CPA 响应的明确 `auth_json`/`data`/`credentials` wrapper 和 camelCase Token 字段会被识别，不从任意列表抓取首条账号。
- local、CPA、sub2 三条路径统一先校验邮箱与 workspace `account_id`，先原子保存完整 OAuth 凭证，再用同一条本地代理调用 `check_account_plan` 查询权威账号/订阅状态；凭证 JSON 保存 `plan_type`、`subscription_plan`、订阅状态/有效期、查询时间和证据等级等精简元数据。JWT 的 `chatgpt_plan_type` 单独保存为 `plan_claim_type`，仅作为接口查询失败时的提示值。
- Codex 页面现在优先读取 JSON 内的 `plan_type`，不再只依赖文件名尾缀；回调成功消息会显示已获取的 plan。没有及时下载到 CPA 凭证时仍保留原 callback 回执，授权主流程不被套餐查询失败阻断。

## 修改路径

- `core/codex_oauth.py:save_codex_credential`：统一身份校验、原子落盘和有界权威套餐查询；`_extract_cpa_auth_json` 只解析明确 wrapper 和 camelCase Token 字段。
- `core/codex_oauth.py:_enrich_codex_auth_json_plan`：callback 后用 access token 查询套餐并写入非敏感元数据；`_callback_credential_from_cpa` 在 CPA 回执成功后读取远端 auth-file。
- `core/codex_oauth.py:build_codex_storage`：本地 PKCE 结果同时保存 `plan_type`；三种 OAuth 入口的成功消息显示 plan。
- `core/roxy_codex_oauth.py`、`core/browser_use_codex_oauth.py`：CPA/sub2 callback 成功分支展示已保存套餐。
- `core/db.py:list_codex_accounts`：列表优先读取凭证 JSON 的 plan 字段。
- `tests/test_codex_oauth_export.py`：新增嵌套凭证提取、权威套餐合并、CPA 回执下载落盘回归。

## 本轮重试流程复核

- Browser Use 接码路径现在在所有返回/异常路径关闭 HTTP 会话；手机号提交后已到 callback 时先完成当前 activation，避免遗留未释放号码。
- Browser Use 手机号失败重试前清理浏览器 Cookie、auth origin 的 localStorage/sessionStorage 并切换到空白页，再生成一次性授权地址并重新提交邮箱；不再直接复用旧授权页状态。
- 协议接码路径的页面/传输层异常增加当前 activation 释放兜底；Roxy 原有的重建授权回调与供应商/国家快照保持不变。
- token 交换辅助 BrowserSession 增加显式关闭；未改端口 5002、流量压缩或密码/2FA 路径。

## 上游对照与安全边界

- 读取 `docs/UPSTREAM_PROJECT_INDEX.md` 锁定的 GPT-utral-platform commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d`；未覆盖其 OAuth 主流程。
- 对照 Cockpit Tools `jlcodes99/cockpit-tools` 的 `crates/cockpit-core/src/modules/codex_quota.rs`：`/backend-api/accounts/check/v4-2023-04-27`、缺失/过期时的 `/backend-api/subscriptions?account_id=...`、Bearer access token 与 workspace 绑定头。callback URL 本身不解析套餐。
- 不把 access token、refresh token、完整响应或套餐接口原始证据写入日志；凭证文件保留原 OAuth 字段，仅新增套餐元数据。注册账号主表的 ChatGPT access token/套餐字段未被 Codex token 覆盖。

## R1–R8 自检

1. **修改已有代码**：上述 OAuth callback 保存、凭证列表和三种入口的既有路径。
2. **新增代码**：明确 wrapper 的 callback 凭证提取、统一保存入口的套餐元数据合并、CPA 回执后凭证读取及对应测试；没有新建第二套 OAuth 执行器，旧保存路径继续作为失败回执回退。
3. **搬迁项**：无。
4. **新增配置项链路**：无；套餐请求继续读取现有 `PLAN_CHECK_*` / `PLAN_CHECK_PROXY` 配置，经 `check_account_plan` 实际使用。
5. **死引用回扫**：

   ```powershell
   rg -n "_enrich_codex_auth_json_plan|_callback_credential_from_cpa|_saved_codex_plan_type|plan_type" core/codex_oauth.py core/roxy_codex_oauth.py core/browser_use_codex_oauth.py core/db.py tests/test_codex_oauth_export.py
   ```

   结果均为当前实现和测试引用，无旧函数残留。
6. **Diff 统计**：相对已推送提交 `e96696e`，本轮最终暂存范围为 `+432 / −143`；删除的是旧的浅层 callback 候选解析、仅按文件名推断套餐分支和未释放接码会话路径，保留原回执回退。
7. **测试**：

   - `py_compile core/codex_oauth.py core/roxy_codex_oauth.py core/browser_use_codex_oauth.py core/db.py tests/test_codex_oauth_export.py`：通过。
   - `pytest tests/test_codex_oauth_export.py tests/test_codex_oauth_network_retry.py tests/test_codex_plus_detection.py tests/test_roxy_codex_phone_classification.py tests/test_roxy_codex_proxy_preflight.py tests/test_roxy_codex_otp_polling.py`：`92 passed, 1 warning`。

8. **未做/存疑**：

   - 未使用真实邮箱、Token 或远端 CPA/sub2 账号做现场 OAuth；回归使用隔离凭证和 mock 套餐响应。
   - 若 CPA callback 已成功但远端 auth-file 在三次短重试后仍不可读，本轮仍先保存 callback 回执；该次授权不伪造套餐结果，需重新授权后再执行凭证与套餐落盘。
   - 5002 运行端口、代理池、流量压缩、密码/2FA 流程未改。
   - 未使用真实邮箱、短信或远端浏览器做现场重试；Browser Use 的页面存储清理使用 Playwright context 与 auth origin 原生 storage API，未新增页面注入脚本。
