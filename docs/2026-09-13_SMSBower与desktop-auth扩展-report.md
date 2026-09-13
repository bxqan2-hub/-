# SMSBower 第二平台与 Codex desktop-auth 扩展报告

## 结论

- 账号列表约 3 MiB 的“流量”对应 `downloaded` 去除缓存回放后的新增网络下载，并非页面全部逻辑资源。近期样本新增网络约 2.5–3.3 MiB，`logical_downloaded`（网络 + 缓存回放）约 38–87 MiB；本次 UI 已同时展示两项。
- HeroSMS 保持原 API 地址与密钥；SMSBower 使用独立地址 `https://smsbower.page/stubs/handler_api.php` 与 `SMSBOWER_API_KEY`，不会串用凭据。
- Roxy Codex 流程使用最新内核配置、无头模式和 `CODEX_LOCAL_PROXY`；授权地址动态生成后包裹 desktop-auth 外层，登录、邮箱 OTP、手机号轮询、callback 及 CPA/sub2 导出仍走原状态机。

## 原因核对

1. **高概率：流量口径误读。** `core/browser_traffic.py` 已区分网络下载和缓存回放；日志中 `downloaded` 约 3 MiB、`logical_downloaded` 更大，属于指标含义不同。
2. **中概率：缓存跨账号复用。** 共享静态缓存命中数和节省字节单独记录，未计入新增网络字节。
3. **低概率：日志截断。** `metrics_version=3`、请求数、命中/未命中/写入均来自 performance log 汇总，列表展示只取脱敏汇总字段。

## 修改点

- `config/codex.py`：新增 SMSBower 配置和 `CODEX_DESKTOP_AUTH_WRAPPER`。
- `core/sms_provider.py`：在原 handler API 路径加入平台选择与独立 endpoint/key，保留取号、轮询、完成、取消逻辑。
- `core/codex_oauth.py`、`core/roxy_codex_oauth.py`：新增动态 desktop-auth 包装并在 Roxy 无头授权入口使用。
- `webui/app.py`、`webui/templates/index.html`、`webui/config_editor.py`：增加平台/国家/价格库存查询。
- `config/env_loader.py`：登记 `SMSBOWER_API_KEY` 为密钥字段。

## 验证

`.venv\\Scripts\\python.exe -m pytest -q tests/test_sms_provider_configuration.py tests/test_sms_provider_herosms.py tests/test_webui_helper_regressions.py tests/test_browser_traffic.py`

结果：`100 passed, 1 warning, 205 subtests passed in 2.13s`。

后续界面回归发现本地 `127.0.0.1:7890` 未监听，导致原国家列表请求直接停在“自动选择”。现已对 SMSBower 的国家/价格只读元数据增加直连重试；取号、短信轮询仍使用配置的本地代理。带授权会话的接口实测返回 202 个国家、0.15 价格上限内 28 个有库存报价。

未执行真实账号登录、短信购买或远端 callback；需在配置页填写 `SMSBOWER_API_KEY` 后选择 SMSBower，再运行 Codex 授权任务验证实际库存和回调。

## 本次 SMSBower 多价格档位展示（2026-09-13）

- 原 `/api/sms/prices` 传入 `service=dr`，平台只返回 OpenAI 单一报价，因此界面看不到截图中的多行级别。现对 SMSBower 元数据查询使用 `getPricesV3&service=dr`，解析完整的 `country → provider_id → price/count` 结构；等级字段按 API 实际返回，未返回时明确显示。
- 新增 `core.sms_provider.list_price_tiers`，保留原 `offers` 兼容字段，同时返回 `countries[].tiers[]`；价格上限与库存过滤仍在服务端执行，取号/短信轮询路径不变。
- 接码中心改为国家卡片 + 价格档位表，显示级别/服务、ID、库存、成本，并提供“选择国家”按钮；国家下拉始终保留自动项和 API 返回的手动国家项。
- 直连实测 SMSBower `getPricesV3&service=dr` 返回 122 个国家、1,725 个供应商档位；美国国家 ID 187 返回 19 档。上限 0.15 时界面保留全部档位并标注“超预算”，不再把高价供应商静默隐藏。
