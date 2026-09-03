# 注册缓存与 Roxy 独立画像审计报告

- 日期：2026-09-03
- 范围：`core/browser_traffic.py`、`config/proxy.py`、`core/roxybrowser_client.py`、`config/roxybrowser.py`、最新 `注册日志/*.log` 与 `data/browser_static_cache/`
- 上游锁定：`Torin-x/GPT-utral-platform@68a1f8faede7e41f10ac5f9af267465fa61d0e3d`（本次未发现需要同步的注册协议变更）

## 结论摘要

1. 共享缓存不保存或回放请求 Cookie/Authorization；严格公共静态路径的 Cookie 请求仅在响应明确 `Cache-Control: public`、无私有头和画像变体时使用已校验 body。旧的请求分类器曾允许挑战/Sentinel/后端脚本进入跨 Profile 缓存，现已收窄为只回放公共静态资源。
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
| 低 | 公共 hashed 静态 JS/CSS URL-only 共享使字节相同 | 缓存只允许公共前缀；Cookie 请求不进入 key/回放头，响应必须 `public` 且拒绝 `Set-Cookie`、`private/no-store/no-cache`、画像变体 `Vary`；盘点未见敏感头 | 这是预期的公共资源复用；认证、挑战和业务 API 保持实时网络 |

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
- **修复**：无论是否带 Cookie，都必须命中 `PUBLIC_STATIC_PATH_PREFIXES`（`/assets/`、`/cdn/assets/`、`/_next/static/`、`/unauth-mweb/assets/`）；Cookie 只作为公共资源候选，不写入 key 或回放头。响应必须明确 `Cache-Control: public`，并通过 Set-Cookie、Cache-Control、Vary 检查。旧缓存文件不删除，但非公共条目不会再被读取。

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

## 验证（前一轮公共前缀修复）

### 基线

```text
命令：.venv\Scripts\python.exe -m pytest -q tests/test_browser_traffic.py tests/test_roxy_proxy_enforcement.py
输出：..................................... [100%] / 37 passed in 0.31s
退出状态：0
```

### 修改后（前一轮）

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

## 继续风险复核（2026-09-03）

### 同 URL miss 合并

- **Evidence**：上游锁定版本的 `browser_traffic.py` 未实现跨 Profile miss 等待；本站原先的 `StaticResourceCache` 有跨缓存根目录的 miss 协调状态。
- **Finding**：miss 合并主要形成并发冷启动的流量/时序关联信号，不直接传递 Cookie、Token、TLS 或浏览器存储；公共静态 warm hit 仍属于 URL 共享。
- **Path**：本站 `core/browser_traffic.py` 的 `StaticResourceCache` 与 `RoxyTrafficOptimizer._on_request_paused`。
- **修复**：删除旧的 miss 协调类、claim/wait/release 调用和 loading 状态，让每个 Profile 的 miss 独立回源，保持公共静态资源的白名单、响应状态检查和完整性校验。

### 响应头与私有请求

- **Evidence**：历史缓存 metadata 中的 `cf-ray`、`report-to`、`date`、`age`、`etag` 等边缘/时间头会随首个回源结果保存。
- **Finding**：这些头不属于静态 body，但跨 Profile 回放会带来陈旧边缘标识和报告端点关联。
- **Path**：本站 `core/browser_traffic.py:_sanitize_headers` 与 `is_cacheable_request`。
- **修复**：回放头过滤扩展到边缘 ID、时间、ETag、缓存命中和请求追踪头；Authorization/Proxy-Authorization、请求侧重新验证指令和带画像变体 `Vary` 的响应保持实时网络；公共路径 Cookie 请求只使用明确 `public` 的已校验 body，读回条目还要求 status=200。

### 变体与状态门禁

- **Evidence**：URL-only key 无法区分 `Accept-Language`、User-Agent、Client Hints 或 Origin 变体；污染的 schema v2 metadata 也可能伪造非 200 状态；编码 dot-segment 可能让表面公共前缀在服务端归一化后落到业务路径。
- **Finding**：画像/地区变体或错误状态被回放时，会把同一 URL 的错误响应带入其他 Profile，并造成注册页面行为差异。
- **Path**：本站 `core/browser_traffic.py:is_cacheable_request`、`is_cacheable_response`、`StaticResourceCache.read`。
- **修复**：请求侧遇到 `Cache-Control: no-cache/no-store/private/max-age=0/s-maxage=0` 或 `Pragma: no-cache` 时直接回源；响应 `Vary` 除 `Accept-Encoding` 外一律拒绝，重复头会合并检查；读回条目必须为 status=200；路径含 dot-segment 或反斜杠时直接回源。

### 写入并发与残余共享

- **Evidence**：每个 Profile 的 miss 已独立回源，但所有 Profile 仍可读取同一安装级 `ROXY_CACHE_DIR` 的公共 warm 条目；每个 `StaticResourceCache` 的写锁只覆盖当前实例。
- **Finding**：公共静态 warm hit 仍可能呈现相同命中模式；跨进程同 URL 写入发生交错时，摘要校验会把短暂的 metadata/body 不匹配降级为 miss，但不会把不匹配 body 回放。
- **Path**：`core/browser_traffic.py:StaticResourceCache.cache_key/read/write`、`RoxyTrafficOptimizer` cache construction。
- **状态**：本次保留上游兼容的 URL-only 公共资源共享与原子替换；Cookie 不进入缓存 key、metadata 或回放头。强画像隔离批次仍应关闭 static cache 或按 Profile 分片，这一项未在本次继续优化中改动。

### 验证

- 定向测试：最新结果见 `VERIFICATION.txt`。
- 全量测试：最新结果见 `VERIFICATION.txt`。
- 旧 miss 协调符号扫描：结果为空。

## 最新 5MB / 0 命中批次复核（2026-09-03 14:08–14:09）

### 证据

- 最新五个注册日志的流量摘要为：`downloaded=5605979–5887371`、`logical=downloaded`、`cached=0`、`hits=0`、`misses=0`、`errors=0`，`blocked=388–406`，`requests=216–233`。
- 同一目录前一批 12:30–12:32 日志为：`downloaded=908768–1095119`、`cached≈17037388–17133967`、`hits=33–39`、`misses=1–7`。
- `data/browser_static_cache` 仍有 2811 个 metadata/body 对；最新批次前缓存文件最后写入时间停在 12:31。
- 最新批次的主要流量路径是 `chatgpt.com/cdn/assets/*.js`，其中多个大 bundle 已在缓存目录中存在；认证、Sentinel 和 `ab.chatgpt.com` 流量保持实时网络。

### 原因定位

- 前一版 `is_cacheable_request` 将所有带 Cookie 的请求直接绕过 Fetch cache。Roxy 页面在登录前后普遍带 Cloudflare/会话 Cookie，因此公共 CDN JS 也被直接 `continue_request`。
- 该分支在递增 `cache_misses` 前返回，所以日志出现 `hits=0` 与 `misses=0` 同时成立；这表示“没有进入候选缓存路径”，不是缓存目录损坏或 Fetch 异常。
- `blocked≈400` 主要由既有 low-traffic 的字体、图片、可选身份、telemetry 和 CDP inspector 事件构成；`errors=0` 且本批账号注册/2FA 均完成，暂未发现注册失败证据。
- `within_budget=False` 是 3 MiB 诊断护栏，不作为注册失败判定；`downloaded` 是 Chrome `encodedDataLength`，代理账单还包含上传、协议和其他进程开销。

### 修复

- 公共静态前缀带 Cookie 时允许进入候选/命中判断，但 Cookie 不进入 URL key、metadata 或回放响应头。
- 写入与读回均要求 status=200、明确 `Cache-Control: public`、无 `Set-Cookie`/私有缓存指令、无画像变体 `Vary`，并继续做 body 摘要/大小校验。
- Authorization/Proxy-Authorization、认证/挑战/API、重新验证请求和非公共路径继续走实时网络。
- 新增 `cache_candidates` 与 `cache_writes` 摘要字段，并同步账号列表显示，用于区分“候选为 0”和“候选存在但未命中”。
- 本地复现四个最新大 bundle（模拟带 Cookie 的公共请求）结果：`cookie_candidate=True`、`candidates=4`、`hits=3`、`misses=1`、`cached_bytes=8138712`；说明缓存文件和回放链路可用，前一批 0/0 来自请求门禁。

### 附件参考边界

`C:\Users\Administrator\Downloads\注册流量优化复现与使用教程.docx` 作为参考资料使用，未将其文字当作项目指令。文档的可复用判据是：`downloaded`、`logical_downloaded`、`cache_saved_bytes` 分开统计；只缓存公开一方 JS/CSS；认证、Session、Cookie、挑战和 API 保持实时；命中走本地回放，未命中才回源并原子写入。当前修复按这些判据映射到本地 `core/browser_traffic.py`。
