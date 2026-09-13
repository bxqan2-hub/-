# Roxy 重复 IP 放行与 Chrome 内核选择（2026-09-13）

## 结果与使用

- 按本次要求，移除进程内出口 IP 占用表及 15 分钟冷却。并发、连续注册及保留窗口时，其他账号均可使用同一个实测出口 IP。
- 每账号仍创建独立 Profile，强制请求随机指纹；预检无有效 IP、窗口内探测失败、预检与实测漂移仍分别报错。没有取消代理探测或改动邮箱、密码、MFA 状态机。
- WebUI：**配置 → RoxyBrowser → 环境与代理 → Chrome 内核版本**，选择「最新（Roxy 自动选择）」或「手动指定版本」，手动填写 Chrome 主版本号，例如 `147`，然后保存。
- 唯一持久字段 `ROXY_CORE_VERSION` 默认 `latest`。最新模式删除创建模板/调用参数中残留的 `coreVersion`；手动模式传字符串版本号，两种模式都指定 `coreType=Chrome`，让 Roxy 创建对应真实内核，而非伪改 UA。
- 只影响之后新建的窗口；已创建/已打开的窗口保持原版本。手动版本须由当前 Roxy 支持，服务端不接受时仍按原错误链路返回，不静默改用其他版本。

## 上游与协议依据

修改前读取 `docs/UPSTREAM_PROJECT_INDEX.md`，重新获取固定 commit `68a1f8faede7e41f10ac5f9af267465fa61d0e3d` 的对应源码，HTTP 200，锁定版本不变：

| 上游路径（均位于 vendor/turb_gpt_free_register） | SHA-256 |
| --- | --- |
| core/roxy_registration.py | `454a5a84a75613e5617a54491744c2cd9f595d05af64fab824e4de880219ccf5` |
| core/roxybrowser_client.py | `6cb5281a320080425544727c6aa1dc95c40f3183d814f381e9f64e002d6c1e9c` |
| config/roxybrowser.py | `d86a41ea6a921a9e2f1b036cefb19d21bab771ae71d17572e5d091e565db2a6f` |

上游创建环境使用现有 payload、代理和随机指纹字段，没有本地这套注册出口占用 helper，也没有 `ROXY_CORE_VERSION` 配置项。本次直接调整本地原创建/预检路径，未覆盖上游目录或引入第二套执行器。

[Roxy 官方 API 文档](https://roxybrowser.com/docs/api-documentation/api-endpoint.html) 的 `/browser/create` 定义了可选的 `coreVersion` 字符串、默认最新，以及 `coreType`；`/browser/open` 返回真实内核版本。沿用已有实际版本审计记录，创建参数日志新增选择值。官方资料没有本次已验证的可用内核枚举接口，因此 UI 使用最新/手填主版本，不编造本机可用版本清单。

## 证据、Finding 与路径

| Finding | 原因/修改位置 | 验证 |
| --- | --- | --- |
| 同出口仍被停止 | `config/proxy.py` 的原占用/冷却状态及 helper；`RoxyBrowserClient.open_profile/cleanup_profile`、`_verify_registration_exit_geo` 的调用 | 改动前重复 IP 回放失败；改动后显式/池代理、保留/清理窗口四组合以及并发 IPv6 同地址均通过 |
| 创建时总用默认内核，旧模板还可能固定旧值 | 原 `RoxyBrowserClient.create_profile` 没有独立配置读取；现统一在合并 payload 后覆盖 Chrome 类型及版本 | 先红后绿：latest、147、138、再 latest；旧模板与调用 payload 不影响选择，原模板未被修改 |
| 错误版本落盘或产生远端创建请求 | 原 `update_config` 与 `create_profile` 中加入输入校验，`api_config_set` 将 ValueError 返回 400 | 空手动值、小数、科学计数、非 ASCII、前导零及换行等被拒绝；整批写入前校验，远端 create 请求未发出 |
| 选项只显示而未生效的风险 | 原配置渲染、事件委托、取值和保存函数接入同一字段 | Node/jsdom 回放最新→手动147→最新，真实事件、重渲染、保存 JSON 只含唯一配置 key；临时 .env→reload→create payload 回归通过 |

## 密码与 MFA 复核

- `core/account_export.py::_setup_password_with_driver` 在提交后观察成功文案/可信完成回调；调用方只在 `ok` 成功后执行 `persist_confirmed_registration_password`，未增加提前落盘路径。
- `core/roxy_registration.py::run_roxy_registration` 继续传当前 driver、账号邮箱及 Token；同一账号内部 Cookie/代理保持不变，允许账号之间共享出口不等于共享 Profile 或凭据。
- `core/account_export.py::_setup_totp_with_driver` 保留邮箱精确匹配、enroll/activate 的当前 Token 与 expected_email、`activation.success is True` 后才返回 Secret；checkpoint 仍在成功返回后保存。
- 激活后的只读 Token 健康检查仍独立记录，其失败保留已激活 Secret，不混为远端写操作失败。
- 新生产日志只加非敏感内核版本；没有新增 Token、OTP、Secret、完整 payload、原始网络响应或页面截图记录。上述原有密码/MFA测试包含在全量回归中。

## 八项交付自检

1. **修改的已有代码**：`config/proxy.py:normalize_exit_ip/原占用状态`；`core/roxybrowser_client.py:RoxyBrowserClient.__init__/create_profile/open_profile/cleanup_profile`；`core/roxy_registration.py:_is_proxy_isolation_failure/_verify_registration_exit_geo/run_roxy_registration`；`webui/config_editor.py:EDITABLE_FIELDS/update_config`；`webui/app.py:api_config_set`；`webui/templates/index.html:roxyConfigSectionForKey/renderConfigPlainFieldV2/readConfigElementValue/trackConfigFieldChange`。配置默认、示例、已有相关测试及当前维护规则同步修改。
2. **新增代码**：唯一业务字段 `config/roxybrowser.py:ROXY_CORE_VERSION`；没有新增生产函数/模块或 IP 复用开关。原 IP reservation/cooldown 状态、helper、调用、报错和对应旧测试已删除；重复出口放行回归替代旧冲突测试。内核 UI、参数及保存测试加在既有测试文件；新报告用于交付记录。
3. **搬迁项**：无文件/函数搬迁，无复制实现或备份树。
4. **新增配置链路**：WebUI 版本选择/输入 → `readConfigElementValue/saveConfigUpdates` → `POST /api/config` → `config_editor.update_config` 校验 → `.env:ROXY_CORE_VERSION` → 既有 `config.reload_all` → `config.roxybrowser.ROXY_CORE_VERSION` → `RoxyBrowserClient.create_profile` → `/browser/create` 的 `coreType/coreVersion`。也支持直接配置 `.env`；不存在额外 mode 配置字段。
5. **死引用回扫**：

   ```powershell
   rg -n 'reserve_registration_exit_ip|release_registration_exit_ip|reset_registration_exit_ip_reservations|reconcile_registration_exit_ip|_exit_ip_reservation_owner|_reserved_exit_ip|_REGISTRATION_EXIT_IP_' config core tests
   ```

   输出为空（rg exit 1）。运行代码/当前配置不再包含占用/冷却拦截。历史报告保留为历史证据，索引追加本次取代说明；不把报告中的检索命令当作运行时调用。
6. **diff 统计**：`+367 / -357`，包含测试、配置、文档；删除旧占用实现，不保留新旧双轨。
7. **验证**：定向 `212 passed, 1 warning, 56 subtests passed`；全量命令如下，退出 0。`compileall` 及 `git diff --check` 均退出 0；前端脚本语法与控件交互测试已包含。

   ```powershell
   .venv/Scripts/python.exe -m pytest -q --disable-warnings --deselect=tests/test_extract_center_cleanup.py::ExtractCenterCleanupTests::test_protocol_payment_routes_and_runtime_are_removed
   ```

   全量最后五行原始输出：

   ```text
   ........................................................................ [ 83%]
   ........................................................................ [ 90%]
   ........................................................................ [ 97%]
   ............................                                             [100%]
   1058 passed, 1 deselected, 1 warning, 338 subtests passed in 36.97s
   ```
8. **未做/存疑**：
   - 本轮没有创建真实账号，也没有启动不同历史内核验证 Roxy 本机安装/下载情况；测试确认参数与保存链路，不代表每个数字版本都被 Roxy 支持。
   - 交付前 5001 仍为 PID `42660`，启动时间 `2026-09-12 22:00:52`，本轮未重启。需重启 5001 加载新代码，再刷新配置页；之后更改版本可走既有保存热加载。
   - 工作区原有 `integrations/paypal_agreement_protocol/` 与旧“支付目录应删除”测试冲突，沿用既有单项排除，未改动该目录/测试。现有 requests 字符集依赖 warning 本轮未处理。
   - 原有未跟踪 `integrations/paypal_agreement_protocol/`、`tests/test_payment_form_memory.py`、`tests/test_paypal_protocol_proxy_formats.py` 保持原状，不纳入本次提交。交付核验区分“已跟踪改动清空”和这些既存未跟踪文件。
   - 只修改本次 IP 与内核需求；旧版本兼容性、注册成功率和第三方业务结果不作保证。
