# 注册缓存与 Roxy 独立画像审计报告

- 日期：2026-09-03
- 范围：`core/browser_traffic.py`、`config/proxy.py`、`core/roxybrowser_client.py`、`config/roxybrowser.py`、最新 `注册日志/*.log` 与 `data/browser_static_cache/`
- 上游锁定：`Torin-x/GPT-utral-platform@68a1f8faede7e41f10ac5f9af267465fa61d0e3d`（本次未发现需要同步的注册协议变更）

## 结论摘要

1. 共享缓存不保存或回放请求 Cookie/Authorization；严格公共静态路径的 Cookie 请求仅在响应明确 `Cache-Control: public`、无私有头和画像变体时使用已校验 body。旧的请求分类器曾允许挑战/Sentinel/后端脚本进入跨 Profile 缓存，现已收窄为只回放公共静态资源。
2. 历史批次的随机抽取曾出现两个账号复用 `72.82.55.137`；本次已在真实出口预检后加入原子 reservation、15 分钟冷却和窗口内出口复核，冲突或漂移会在注册前 fail-closed。
3. 当前每个账号会创建不同 Roxy Profile，并在结束后删除；`/browser/create` 已强制发送 `randomFingerprint=True`，系统类型在 Windows/macOS 间随机。浏览器内核版本没有在本地随机，最新五条日志均由 Roxy runtime 返回 `coreVersion=152`。
4. 当前 200 条代理池记录全部含 `region-US`；本批随机的是 US 会话线路，不是随机国家。`PROXY_API_ACTIVE=GB` 在 `PROXY_API_ENABLED=False` 时不生效。

## 高概率、中概率、低概率核对

| 概率 | 原因/风险 | 证据核对 | 结论 |
| --- | --- | --- | --- |
| 高 | 代理池随机抽取后没有跨任务出口 IP 去重 | 历史日志有 `72.82.55.137` 重复；现已在 `open_profile` 预检后调用 `reserve_registration_exit_ip`，占用/冷却冲突时换候选并拒绝创建 | 已修复（同一 Python 进程线程池内）；释放后 15 分钟内仍拒绝复用 |
| 高 | 挑战/Sentinel/后端脚本被共享缓存跨 Profile 回放 | 修复前 `is_cacheable_request(..., headers={})` 对 `/backend-api/sentinel/`、`/sentinel/`、`/cdn-cgi/challenge-platform/` 返回 True；缓存盘有 6 个非公共前缀条目 | 已确认并已修复；这些路径现在强制实时请求 |
| 中 | Roxy 只由 runtime 决定未显式指定的指纹字段，内核可能长期相同 | 上游 `create_profile` 显式发送 `randomFingerprint`；本站旧路径缺失，最新返回仍全部 `coreVersion=152` | 已修复请求字段；内核版本仍由已安装 runtime 决定，不能把版本固定误判为账号指纹复用 |
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
  - 对应日志均记录 `创建环境使用代理池代理`，说明不是直连；旧批次的重复是随机端点映射到同一真实出口造成的。
  - 新增回归模拟：同一 IP 已被其他 owner 占用时，第一次候选被排除，第二条候选才允许创建；释放后冷却窗口内再次领取返回 False。
- **Finding**：随机端点不能证明真实出口唯一；现行路径以预检取得的 canonical IP 为锁定对象，并在浏览器上下文再次核对，避免 Roxy 未应用 `proxyInfo` 或供应商漂移。
- **Path**：`config/proxy.py::reserve_registration_exit_ip/release_registration_exit_ip`、`core/roxybrowser_client.py::open_profile/reconcile_registration_exit_ip/cleanup_profile`、`core/roxy_registration.py::_verify_registration_exit_geo`。
- **结论**：同一 Python 进程的并发注册现已按真实出口 IP 去重；同一 IP 在释放后 15 分钟内仍拒绝复用。不同进程之间的 lease 尚未持久化，需保持 CLI/WebUI 不并行运行才能维持同一保证。

### E3：Roxy Profile、系统与内核

- **Evidence**
  - `ROXY_ONE_PROFILE_PER_ACCOUNT=True`、`ROXY_DELETE_PROFILE_AFTER_RUN=True`。
  - 最新日志各自返回不同 `dirId/profile`，并记录删除成功。
  - 创建参数记录 `random_os=True`，值为 `Windows` 或 `macOS`。
  - 最新五条 open 返回均为 `coreVersion: "152"`，driver 路径为 `chrome-bin\\152\\chromedriver.exe`。
- **Finding**：Profile 生命周期是一号一环境；系统类型随机；内核由 Roxy 安装/runtime 默认固定为 152，本地没有内核随机逻辑。现已显式请求 Roxy `randomFingerprint=True`，不再依赖模板默认值；`fingerInfo`/内核版本仍由 Roxy 版本决定。
- **Path**：`config/roxybrowser.py`、`core/roxybrowser_client.py:create_profile/open_profile`、`core/roxy_registration.py::_profile_isolation_summary`。

## 已实施修改

- 文件：`core/browser_traffic.py`
- 字段/分支：`is_cacheable_request` 的缓存资格判定。
- 行为：将“带 Cookie 时才检查公共前缀”改为“所有请求都必须命中公共前缀”；因此挑战、Sentinel、`/backend-api`、`/unauth-mweb/scripts` 不再进入跨 Profile 缓存。
- 回归：`tests/test_browser_traffic.py` 新增四个非公共路径断言。

### 本轮独立出口与指纹修改

- **文件/路径**：`config/proxy.py`、`core/roxybrowser_client.py`、`core/roxy_registration.py`。
- **出口 IP**：预检得到真实 IP 后 canonicalize 并原子占用；占用冲突时排除当前代理并轮换候选，池耗尽或显式代理冲突直接终止，不创建 Profile。任务结束关闭/删除 Profile 后释放，释放记录保留 15 分钟冷却。
- **窗口复核**：Selenium 上下文 IP 与预检 IP 同时存在且不一致时，先保护实际地址并 fail-closed；一致或仅有预检回退时才进入注册。`ROXY_KEEP_BROWSER_OPEN` 在打开时快照，保留现场时不释放 reservation。
- **失败分类**：出口冲突/冷却/漂移统一标记为 `stage=proxy_isolation`，与 `stage=proxy_transport`、邮箱 OTP、密码和 2FA 业务错误分开记录。
- **输入校验**：预检返回的非 IP 值单独记为无效出口并轮换候选，不再误报为并发冲突。
- **指纹**：现有 `/browser/create` payload 合并后强制 `randomFingerprint=True`；名称/系统选择使用独立随机值，不向 Roxy 伪造 `fingerInfo` 或 `coreVersion`。账号记录新增无凭据的 isolation 摘要（Profile、core、OS、出口 IP、验证来源）。
- **路由记录**：账号 `registration_exit_ip`/`registration_exit_country` 与 isolation 摘要记录核对结果；代理池凭据不额外写入账号字段。

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

1. 保持 `ROXY_ONE_PROFILE_PER_ACCOUNT=True`、`ROXY_DELETE_PROFILE_AFTER_RUN=True` 和 `PROXY_POOL_ACTIVE` 为空；代理池至少要有与并发数相匹配的真实出口，池中只有同一 IP 时应接受 fail-closed 结果。
2. 当前 reservation 是单 Python 进程共享的线程安全状态；CLI 与 WebUI 或多个 WebUI 进程并行时状态不共享，需串行运行或后续接入外部 lease 服务。
3. 公共 hashed JS/CSS 仍使用安装级 URL-only warm cache；它不保存 Cookie/Token，但会保留公共资源命中时序。若某批次把时序关联也视为特征，可将 `ROXY_STATIC_CACHE=False` 作为该批次的隔离策略，代价是每号增加约 5MB 回源流量。
4. Roxy 官方建议语言、显示语言、时区和地理位置自动匹配代理 IP；当前本地不伪造 `fingerInfo`，以 Roxy `randomFingerprint` 和实际 open 响应为准。参考：[Roxy API endpoint 文档](https://roxybrowser.com/docs/api-documentation/api-endpoint.html)、[Roxy Profile configuration 文档](https://roxybrowser.com/docs/features/profile-configuration.html)。

## 继续风险复核（2026-09-03）

### 同 URL miss 合并

- **Evidence**：上游锁定版本的 `browser_traffic.py` 未实现跨 Profile miss 等待；本站原先的 `StaticResourceCache` 有跨缓存根目录的 miss 协调状态。
- **Finding**：冷缓存时每个 Profile 独立回源会重复下载同一公共资源，放大网络、磁盘和 renderer 峰值；单飞协调只作用于严格公共、已校验的 JS/CSS body，不直接传递 Cookie、Token、TLS 或浏览器存储；公共静态 warm hit 仍属于 URL 共享。
- **Path**：本站 `core/browser_traffic.py` 的 `StaticResourceCache` 与 `RoxyTrafficOptimizer._on_request_paused`。
- **修复**：恢复有界的 miss 协调：首个 Profile 回源并原子写入，其他 Profile 最多等待 8 秒读取通过 schema、状态、摘要、缓存头校验的 body；超时或首个请求失败时立即回到各自实时网络。注册 workers、Profile 数量和浏览器并发不变。

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

- **Evidence**：所有 Profile 仍可读取同一安装级 `ROXY_CACHE_DIR` 的公共 warm 条目；冷 miss 通过目录级 coordinator 短暂合并，单个 `StaticResourceCache` 的写锁仍只覆盖自身临时文件替换。
- **Finding**：公共静态 warm hit 仍可能呈现相同命中模式；跨进程同 URL 写入发生交错时，摘要校验会把短暂的 metadata/body 不匹配降级为 miss，但不会把不匹配 body 回放。
- **Path**：`core/browser_traffic.py:StaticResourceCache.cache_key/read/write`、`RoxyTrafficOptimizer` cache construction。
- **状态**：本次保留上游兼容的 URL-only 公共资源共享与原子替换，同时为冷启动增加 8 秒单飞等待；Cookie 不进入缓存 key、metadata 或回放头。强画像隔离批次仍可关闭 static cache 或按 Profile 分片。

### 验证

- 定向测试：最新结果见 `VERIFICATION.txt`。
- 全量测试：最新结果见 `VERIFICATION.txt`。
- miss 协调符号扫描：`CACHE_LOAD_WAIT_SECONDS`、`claim_load`、`wait_for_load` 和 `release_load` 仅出现在 `core/browser_traffic.py` 及对应测试。

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

## 深度隔离复核（2026-09-03 本轮）

### 高概率 / 中概率 / 低概率原因与逐条核对

| 概率 | 核对原因 | 证据与路径 | 处理结果 |
| --- | --- | --- | --- |
| 高 | 随机代理端点映射到同一真实出口 IP，多个账号同时注册 | 历史日志出现重复 `registration_exit_ip`；`config/proxy.py::pick_proxy` 仅负责抽取，真实 IP 只能由 `probe_proxy_exit_geo` 得到 | 预检后以 canonical IP 原子 reservation；冲突换候选，候选耗尽直接失败，避免继续注册 |
| 高 | Roxy 未应用 `proxyInfo` 或供应商在创建/打开间发生出口漂移 | `core/roxy_registration.py::_verify_registration_exit_geo` 对预检 IP 与 Selenium 上下文 IP 做一致性核对 | 两者同时存在且不一致时 fail-closed，并回收 Profile；仅同一代理预检回退才允许继续 |
| 中 | 新 Profile 仍沿用固定模板指纹，随机 OS 被误当作完整随机 | 上游锁定 commit 的 `create_profile` 显式发送 `randomFingerprint`；本地旧 payload 缺失该字段 | `core/roxybrowser_client.py::create_profile` 合并后强制 `randomFingerprint=True`；不伪造 `fingerInfo`/内核版本 |
| 中 | 账号完成后紧邻的下一个任务再次使用刚释放的 IP | 进程内 reservation 的释放时刻可观测，单纯 active set 会立即清空 | `_REGISTRATION_EXIT_IP_LAST_USED` 保留 15 分钟冷却；同一进程内再次领取被拒绝 |
| 低 | 公共 URL-only 静态缓存形成命中时序关联 | `core/browser_traffic.py::StaticResourceCache` 只保存明确 public 的 JS/CSS，Cookie/Authorization 不入 key 或回放头 | 保留公共资源节流；认证、挑战、Sentinel、API 实时。高隔离批次可关闭 `ROXY_STATIC_CACHE` |

### 运行时独立性证据

- 最新 14:42 批五个 Profile 的预检出口分别为 `73.255.202.155`、`76.36.188.15`、`97.95.124.228`、`76.37.89.132`、`131.241.125.43`，均为 US 且无重复；该批在本轮代码加载前完成，作为修复前后对照基线。
- 旧批次 14:08–14:09 的五个账号均成功完成密码与 2FA，但流量为 `5.6–5.9MB`、`hits=0`；14:42 批为 `0.90–1.14MB`、`hits=34–40`，说明缓存修复与 IP/指纹修复是独立维度。
- 本轮新增测试覆盖：同一 IP 并发冲突换代理、显式代理冲突 fail-closed、IPv6 canonical、释放冷却、浏览器出口漂移、预检跳过时的实际 IP reservation、保留窗口时不释放以及 `randomFingerprint` 强制字段。

### 密码与 2FA 交叉复核

- 最新 14:42 五个日志均出现密码确认检查点、MFA enroll/activate 完成和 Token 校验成功；未发现密码或 TOTP Secret 在出口冲突/漂移前落盘的路径。
- 较早的单条失败日志仍明确标记 `stage=cookie_import`、`cookie_auth_missing`，与代理隔离、密码提交和 2FA activate 失败分开；本轮没有把它重新归类为网络失败。
- 现有 checkpoint 规则保持：密码仅在页面成功终态确认后保存，TOTP Secret 仅在 enroll/activate 成功后保存；本轮新增的 isolation 摘要不含 Cookie、Token 或 Secret。

### 仍保留的边界

- reservation 与冷却是当前 Python 进程共享的线程安全状态；不同进程（例如 CLI 与 WebUI 同时运行）不共享，运行策略应保持串行。
- `coreVersion=152` 是本机 Roxy runtime 版本，不能通过注册 payload 安全随机化；真正的 Profile 指纹由 Roxy `randomFingerprint` 生成，需在 Roxy 客户端升级后重新核对响应摘要。
- 代理池若多个端点实际汇聚到同一出口，系统会减少可用账号数而不是复用 IP；这是预期的 fail-closed 行为。

### 本轮回归结果

- 基线定向：`146 passed in 1.05s`；修改后定向：`159 passed in 17.39s`。
- 基线全量：`716 passed, 16 subtests passed in 64.28s (0:01:04)`；修改后全量：`729 passed, 16 subtests passed in 67.37s (0:01:07)`。
- 完整命令、退出状态、hash 与 rollback 结果见项目根目录 `VERIFICATION.txt`。

## 本次 Roxy 指纹生成字段复核（2026-09-03）

### 高概率 / 中概率 / 低概率原因

- **高概率**：历史 `/browser/create` 没有显式传 `randomFingerprint`，Roxy 可能按模板/默认值生成 Profile；该缺口已在本轮修复。
- **中概率**：每账号虽然创建了独立 `dirId`，但系统类型或 runtime `coreVersion` 长期相同；当前仍由已安装 runtime 返回 `coreVersion=152`，这不等同于底层 Profile 指纹相同。
- **低概率**：本地 Selenium 在启动后覆盖 Roxy 生成的 `navigator` 或语言/时区，造成 Profile 内部不一致；当前流程不写入这些指纹字段，仍以 Roxy Profile 为准。

### 证据、Path 与修复

- **Evidence**：上游锁定 commit 的 `vendor/.../core/roxybrowser_client.py::create_profile` 明确把 `randomFingerprint` 写入 `/browser/create` payload；本轮本地请求体已在模板/调用参数合并后强制为真。
- **Finding**：独立 Profile 生命周期不能单独证明每个底层字段都不同；但请求已明确交给 Roxy 的随机指纹生成器，避免旧模板静默关闭该能力。
- **Path**：`core/roxybrowser_client.py::RoxyBrowserClient.create_profile`。
- **Fix**：在现有 payload 合并后强制 `randomFingerprint=True`，防止模板或调用方关闭；不新增平行开关，不写入虚构 `fingerInfo`/`coreVersion`/地区字段，不改 Selenium `navigator`。
- **验证**：新增 `tests/test_roxy_proxy_enforcement.py::test_profile_create_always_requests_fresh_random_fingerprint`，覆盖模板和调用参数均为 false 时最终请求字段仍为 true；出口唯一性由 `open_profile`/代理 reservation 与 `_verify_registration_exit_geo` 单独核验。

### 边界

`randomFingerprint=True` 是向 Roxy 请求新指纹生成的可验证输入，不是对每个底层字段已不同的承诺；`coreVersion` 可以因本机安装版本而保持一致，属于 runtime 版本而非账号标识。实际出口 IP 现由独立 reservation 和窗口内出口复核保护，随机抽取本身仍不等于唯一 IP。
