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
- Roxy 批量注册时每个账号必须创建独立 Profile 并请求独立随机指纹；代理只能先随机抽取候选，随后必须以真实出口 IP 预检、原子占用和窗口内复核确认，禁止在同一运行进程内复用已占用或刚释放的出口。代理冲突、漂移或无法核对时直接终止该账号，不以“请求失败”掩盖路由特征问题；多个 CLI/WebUI 进程需串行运行，直到接入共享 lease。

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

## 修改原则：只改，不堆

以下规则来自项目维护者提供的 `GPT修改规则_只改不堆.md`，适用于本项目的后续修改。

### R1 先读后写

- 动手前先 grep 相关符号并阅读现有实现。
- 未读过现有代码前，不新增函数、类、模块或字段。
- 修改完成后明确记录修改的是哪一处已有代码；若确实没有已有实现，先记录证明不存在的检索结果。

### R2 禁止平行实现

- 不使用新开关、新参数或新标志另开一条新旧并存的代码路径。
- 需求变化时直接修改原路径。
- 确需灰度并存时，必须在同一次交付中写明旧路径的删除条件、删除时机，并列入未完成项。

### R3 搬迁是移动，不是复制

- 代码搬到新文件、目录或包时，同一次改动删除源文件。
- 自查同名函数、类和模块是否同时存在于两个文件。

### R4 新增的东西必须有读取点

- 新增配置字段、环境变量、CLI 参数、UI 控件或返回字段时，给出完整链路：
  `入口（UI/CLI/API）→ 解析 → 配置对象 → 业务代码真正读取的位置`。
- 无法给出完整链路时不新增该项。

### R5 替换后回扫死引用

- 修改后 grep 被替换的旧函数名、旧字段、旧环境变量和旧模块路径。
- 文档、配置示例和测试中的旧引用一并检查；发现残留先清理。

### R6 增删比自检

- 以对齐、重构、迁移、优化、清理或适配为名的改动，删除行数不得为 0。
- 若 diff 只有增加，交付说明必须解释新增内容替代了什么，以及被替代内容为何仍保留。

### R7 不扩大范围

- 只修改任务点名的文件。
- 不顺手格式化、重排 import、重命名、补类型注解或夹带无关优化。
- 发现其他问题时单独列入报告，不混入当前改动。

### R8 交付自检

每次代码修改交付时逐条输出：

1. 修改的已有代码：`文件:函数/类`。
2. 新增的代码：`文件:符号`、替代内容以及被替代物是否已删除。
3. 搬迁项：源文件是否已删除（是/否）。
4. 新增配置项链路：`入口 → 配置对象 → 真正读取位置`。
5. 死引用回扫：贴出 grep 命令和输出，结果应为空。
6. diff 统计：`+X / −Y`；若 `Y=0`，说明原因。
7. 测试：贴最后 5 行原始输出。
8. 未做的 / 存疑的：逐条列出，不用笼统结论替代。
