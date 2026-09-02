# Repository instructions

## Change delivery and rollback

- Do not create local backup copies, rollback fixtures, duplicate project
  trees, or `artifacts/` backup directories before or after a modification.
- Use commits in `https://github.com/bxqan2-hub/-.git` as the rollback history.
- After every completed modification, run the relevant validation, commit the
  finished change to the current branch, and push it directly to `origin`.
- Keep runtime data, credentials, logs, caches, and generated account files out
  of Git according to `.gitignore`.
- Before handoff, verify that the working tree is clean and that the local HEAD
  matches the pushed remote branch.

## Persistent upstream reference

- Before changing ChatGPT registration, Roxy browser, password, or MFA logic,
  read `docs/UPSTREAM_PROJECT_INDEX.md`. It locks the GPT-utral-platform source
  URL/commit, relevant upstream files, local mappings, and the required
  comparison/update steps so the upstream address does not need to be supplied
  again for each change.

## 注册失败与安全核对规则（持久）

- 每次查看注册失败日志时，先按高概率、中概率、低概率列出原因，再逐条核对证据、定位代码路径、实施修复并运行回归验证；不得把网络、浏览器生命周期、邮箱 OTP、密码或 2FA 问题混为一个“请求失败”。
- ChatGPT 注册、设置密码和 2FA 的实现必须先对照 `docs/UPSTREAM_PROJECT_INDEX.md` 锁定的 `Torin-x/GPT-utral-platform` commit，再保留本地安全边界（账号邮箱匹配、同一浏览器 Cookie/代理、Token 显式透传、enroll/activate 成功确认和脱敏日志）。
- 密码只有在页面进入成功终态后写入 checkpoint；TOTP Secret 只有 enroll/activate 返回成功后保存；任何只读 Token 校验失败都必须与远端写操作失败分开记录。
- 失败修复完成后必须同时复核密码与 2FA 流程的缺陷/越权/凭据泄露风险，并把证据、Finding、Path 和验证结果写入项目 `docs/` 报告。

## Payment / extraction integrations

The only runtime implementation for payment-link extraction is:

- `integrations/pay153_checkout`, upstream: https://github.com/1537271403/pay153-checkout-link

Before changing this integration, payment routing, the Extract Center, or any
Kakao/PayPal/PIX/GCash provider logic, first read this file and
`integrations/UPSTREAM_SOURCES.md`, then fetch/read the relevant upstream
repositories and compare their files with the vendored copies. Record any new
upstream commit in `integrations/upstream-lock.json` and the source document.

For Kakao OAICS changes, also read
https://github.com/m1243808154/kakao_oaics_source first. It is a protocol and
attribution source only; do not add it as a third runtime service. Preserve its
credit in the Extract Center and source documentation.

Never overwrite the vendored integration blindly. Review the upstream diff,
preserve deliberate local routing/security patches, enforce the one-shot
Kakao confirm boundary, and run the focused integration tests plus the WebUI
tests affected by the change.
