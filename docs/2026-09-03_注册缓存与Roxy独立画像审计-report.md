# 注册缓存与 Roxy 独立画像审计报告

- 日期：2026-09-03
- 范围：`core/browser_traffic.py`、`config/proxy.py`、`core/roxybrowser_client.py`、`config/roxybrowser.py`、最新 `注册日志/*.log` 与 `data/browser_static_cache/`
- 上游锁定：`Torin-x/GPT-utral-platform@68a1f8faede7e41f10ac5f9af267465fa61d0e3d`（本次未发现需要同步的注册协议变更）

## 结论摘要

1. 共享缓存不会保存账号 Cookie、Authorization 或 Set-Cookie 响应；但旧的请求分类器在无 Cookie 时允许挑战/Sentinel/后端脚本进入跨 Profile 缓存。该路径可能复用浏览器挑战代码，不能作为“独立画像”边界。已修复为只回放公共静态资源。
2. 当前代理选择确实是随机抽取，但不是“一号一独立出口 IP”。最新同批日志出现两个不同账号使用相同出口 IP `72.82.55.137`，说明 `random.choice` 不等于 IP 唯一。
3. 当前每个账号会创建不同 Roxy Profile，并在结束后删除；系统类型在 Windows/macOS 间随机。浏览器内核版本没有在本地随机，最新四条日志均由 Roxy runtime 返回 `coreVersion=152`。
4. 当前 200 条代理池记录全部含 `region-US`；本批随机的是 US 会话线路，不是随机国家。`PROXY_API_ACTIVE=GB` 在 `PROXY_API_ENABLED=False` 时不生效。

## 高概率、中概率、低概率核对

| 概率 | 原因/风险 | 证据核对 | 结论 |
| --- | --- | --- | --- |
| 高 | 代理池随机抽取后没有跨任务出口 IP 去重 | `config/proxy.py::_pick_static_or_system_proxy` 使用 `random.choice(available)`；最新日志有 `72.82.55.137` 重复 | 已确认。当前只有随机，没有唯一 IP 保证 |
| 高 | 挑战/Sentinel/后端脚本被共享缓存跨 Profile 回放 | 修复前 `is_cacheable_request(..., headers={})` 对 `/backend-api/sentinel/`、`/sentinel/`、`/cdn-cgi/challenge-platform/` 返回 True；缓存盘有 6 个非公共前缀条目 | 已确认并已修复；这些路径现在强制实时请求 |
| 中 | Roxy 只由 runtime 决定未显式指定的指纹字段，内核可能长期相同 | `create_profile` 只随机 `os` 和 `windowName`，没有本地 `fingerInfo` 或 `coreVersion`；最新返回全部 `coreVersion=152` | 已确认。OS 随机不等于完整指纹随机 |
| 中 | 语言/时区画像与 Roxy 可见 Profile 可能不是同一数据源 | `config/browser.py` 的 `AUTO_BROWSER_LOCALE_FROM_IP=True` 作用于 BrowserSession；Roxy create payload 没有 `fingerInfo` | 需继续以 Roxy API 实际返回/页面 JS 观测确认，不能把本地 locale 配置当作 Roxy 指纹保证 |
| 低 | 公共 hashed 静态 JS/CSS URL-only 共享使字节相同 | 缓存只允许公共前缀，响应拒绝 `Set-Cookie`、`private/no-store` 和 `Vary: Cookie/Authorization`；盘点未见敏感头 | 这是预期的资源复用，不会携带账号会话；仍不应扩大到挑战或业务 API |

## Evidence → Finding → Path

### E1：共享缓存范围

- **Evidence**
  - 命令：
    ```powershell
    .\.venv\Scripts\python.exe -c "from core.browser_traffic import is_cacheable_request as f; print(f('https://chatgpt.com/backend-api/sentinel/sdk.js','GET','script',{})); print(f('https://chatgpt.com/_next/static/app.js','GET','script',{}))"
    ```
  - 修复前输出：`True`、`True`。
  - 修复后输出：`False`、`True`。
  - 缓存盘点输出：`meta_count 2811 body_count 2811 non_public_count 6`；metadata 中敏感头计数为 `0`。
- **Finding**：共享目录本身不是账号 Cookie 泄露点，但原分类器把挑战/Sentinel/后端脚本当作公共静态资源，存在跨 Profile 复用浏览器挑战代码的画像隔离风险。
- **Path**：`core/browser_traffic.py:is_cacheable_request`、`StaticResourceCache`；配置目录 `data/browser_static_cache/`。
- **修复**：无论是否带 Cookie，都必须命中 `PUBLIC_STATIC_PATH_PREFIXES`（`/assets/`、`/cdn/assets/`、`/_next/static/`、`/unauth-mweb/assets/`）才允许缓存；响应仍需通过 Set-Cookie、Cache-Control、Vary 检查。旧缓存文件不删除，但新分类器不会读取它们用于这些路径。

### E2：IP 选择与重复

- **Evidence**
  - 当前配置输出：`pool_count 200 active '' api_enabled False`、`roxy_create_use_pool True`、`proxy regions {'US': 200}`。
  - 最新四条日志的 `exit_ip`：`72.82.55.137`、`173.171.192.234`、`104.251.245.160`、`72.82.55.137`。
  - 对应日志均记录 `创建环境使用代理池代理`，说明不是直连。
- **Finding**：选择算法是随机的，但同一批存在重复实际出口 IP；代理端点的 session 参数不同也不能保证服务商返回不同 IP。当前没有批次级“已占用出口 IP”锁定。
- **Path**：`config/proxy.py::_pick_static_or_system_proxy`、`config/proxy.py:pick_proxy`、`core/roxybrowser_client.py::_ensure_profile_proxy/open_profile`。
- **结论**：若要求严格“一号一 IP”，需要在预检得到真实出口 IP 后做全局/批次级保留，并在并发任务间排除已保留 IP；仅把 `PROXY_POOL_ACTIVE` 留空或扩大池子都不能提供该保证。

### E3：Roxy Profile、系统与内核

- **Evidence**
  - `ROXY_ONE_PROFILE_PER_ACCOUNT=True`、`ROXY_DELETE_PROFILE_AFTER_RUN=True`。
  - 最新日志各自返回不同 `dirId/profile`，并记录删除成功。
  - 创建参数记录 `random_os=True`，值为 `Windows` 或 `macOS`。
  - 最新四条 open 返回均为 `coreVersion: "152"`，driver 路径为 `chrome-bin\\152\\chromedriver.exe`。
- **Finding**：Profile 生命周期是一号一环境；系统类型随机；内核由 Roxy 安装/runtime 默认固定为 152，本地没有内核随机逻辑。`fingerInfo` 未显式传入，因此完整指纹随机性由 Roxy 默认策略决定，不能由本项目保证。
- **Path**：`config/roxybrowser.py`、`core/roxybrowser_client.py:create_profile/open_profile`。

## 已实施修改

- 文件：`core/browser_traffic.py`
- 字段/分支：`is_cacheable_request` 的缓存资格判定。
- 行为：将“带 Cookie 时才检查公共前缀”改为“所有请求都必须命中公共前缀”；因此挑战、Sentinel、`/backend-api`、`/unauth-mweb/scripts` 不再进入跨 Profile 缓存。
- 回归：`tests/test_browser_traffic.py` 新增四个非公共路径断言。

## 验证

### 基线

```text
命令：.venv\Scripts\python.exe -m pytest -q tests/test_browser_traffic.py tests/test_roxy_proxy_enforcement.py
输出：..................................... [100%] / 37 passed in 0.31s
退出状态：0
```

### 修改后

```text
命令：.venv\Scripts\python.exe -m pytest -q tests/test_browser_traffic.py tests/test_roxy_proxy_enforcement.py
输出：..................................... [100%] / 37 passed in 0.31s
退出状态：0

命令：.venv\Scripts\python.exe -m pytest -q
输出：713 passed, 16 subtests passed in 63.07s (0:01:03)
退出状态：0

命令：.venv\Scripts\python.exe -c "from core.browser_traffic import is_cacheable_request as f; ..."
输出：challenge/backend/sentinel/unauth-script=False；public static=True
退出状态：0
```

## 后续实施建议

1. 保持 `ROXY_ONE_PROFILE_PER_ACCOUNT=True`、`ROXY_DELETE_PROFILE_AFTER_RUN=True` 和 `PROXY_POOL_ACTIVE` 为空；不要把“随机代理”描述为“唯一 IP”。
2. 要求严格独立 IP 时，在 `open_profile` 的代理预检阶段增加批次级出口 IP reservation（加锁、冲突换代理、注册结束释放），并把 `registration_exit_ip` 作为唯一性校验字段；若池中没有新 IP，应明确失败而不是复用。
3. 若要验证“完整指纹随机”，在 Roxy `/browser/create`/`/browser/open` 的脱敏日志中记录 `os`、`osVersion`、`coreVersion`、语言、时区、WebGL/字体等非凭据摘要，并与 `registration_exit_country` 做一致性检查；不要记录 Cookie、Token 或 MFA Secret。
4. Roxy 官方建议语言、显示语言、时区和地理位置自动匹配代理 IP；当前本地 `BROWSER_LOCALE_PROFILE` 不能替代 Roxy `fingerInfo`。参考：[Roxy API endpoint 文档](https://roxybrowser.com/docs/api-documentation/api-endpoint.html)、[Roxy Profile configuration 文档](https://roxybrowser.com/docs/features/profile-configuration.html)。
