UPI 资格查询交付报告（2026-09-17）

使用：设置 → 检测代理 → 支付资格代理池 → 加入 UPI 代理（IN）；账号页勾选账号 → 查询资格 → 查询 UPI。全部查询包含 UPI；资格筛选支持“有 UPI 资格”和“任一资格”。清空 UPI 代理仅删除独立 IN 池。

判定：选择账号 AT，通过独立印度代理创建不带活动的 IN/INR Plus Checkout。创建响应直接发布 UPI 时立即完成；否则 Stripe 会话执行初始化，OAICS 会话读取一次状态，均复用创建时的 HTTP Session 和代理。明确发布 upi 且币种 INR 判有资格，金额/免费试用标记不影响资格；未发布记为已完成无资格，协议/网络异常记为检测失败。流程在提炼中心的支付方式可用性检查处结束。

上游对照：git fetch PAY.153 当前 HEAD 得到 e8b36626162f09363f29b85af42de98cc8114c9b，与锁定版本一致。读取并比较 app.py、provider_checkout.py、stripe_checkout.py、upi_go_runner.py；后三者与上游相同，保留 app.py 既有本地差异。没有同步新版本，integrations/upstream-lock.json 保持原锁；审计补入 integrations/UPSTREAM_SOURCES.md。

R8 逐项交付自检：

1. 修改的已有代码（项目根目录：C:/Users/Administrator/Desktop/turb-gpt-free-register）：

   - webui/app.py:_compact_account_for_list、create_app 内账号列表/邮箱过滤、代理导入删除和启动恢复：接入 UPI。
   - webui/templates/index.html:_qualificationBadges、checkSelectedQualification、markQualificationChecksQueued、pollAccountPlanStatuses、renderDetectionProxyPanelV2、代理导入/清空与配置字段映射：接入第四种资格。
   - core/db.py:stop_account_page_operations、list_account_plan_check_statuses：接入 UPI 全局停止、状态快照与 revision。
   - core/detection_proxy.py:qualification_proxy_specs：固定读取 UPI 独立 IN 池。
   - config/proxy.py 默认值及环境/运行文件读取；config/env_loader.py 运行文件映射；config/__init__.py 导出；webui/config_editor.py:EDITABLE_FIELDS/空列表规则；.env.example：接入 UPI 配置。
   - integrations/pay153_checkout/app.py 的原有会话分类函数原位更名为 _qualification_checkout_kind，由 MoMo 与 UPI 共用。

2. 新增的代码：

   - integrations/pay153_checkout/app.py:detect_upi；core/upi_service.py:check_upi、enqueue、get_executor 及队列/重试内部函数。
   - core/db.py:claim_account_upi、mark_account_upi_running、update_account_upi、recover_interrupted_upi_checks；webui/app.py:api_accounts_check_upi_bulk。
   - tests/test_upi_qualification.py：39 项回归，包括 Node 实际执行前端函数。
   - 修改前检索未发现 detect_upi、check-upi-bulk 或 upi_service 现有实现；新增的是第四种资格的业务实现，不替换原有 GCash/GoPay/MoMo。原分类函数定义和调用已一并更名，旧符号已删除；支付协议仍调用已有 create_checkout、fetch_custom_checkout_session、sc.init_checkout，没有新增运行服务。

3. 搬迁项：无文件搬迁；原会话分类符号原位替换，旧定义已删除。没有本地备份或项目副本。

4. 新增配置项和结果链路：

   - 加入/清空 UPI 代理 UI → /api/detection-proxy-pools/import 或 delete，purpose=upi → config_editor.update_config → data/upi_check_proxy_pool.txt → config.proxy.UPI_CHECK_PROXY_PROFILES → qualification_proxy_specs("IN", "upi") → bulk 路由解析代理 → upi_service → detect_upi。
   - UPI_CHECK_PROXY_ACTIVE 默认 IN：代理池导入/删除结果 → 配置对象及前端 activeKey 映射；删除 API 读取当前管理国家。真实查询始终固定 IN。
   - 查询 UPI/全部查询 → /api/accounts/check-upi-bulk → claim/后台队列 → UPI 结果字段 → 账号列表及状态快照 → 徽标/支付方式 tooltip/筛选。取消代次从入队显式传到 worker、传输/协议阶段检查和 DB 锁内校验。
   - 代理池文件位于已忽略的 data/；源码和 Git 不保存运行代理、AT 或生成账号。

5. 死引用回扫（退出码 1 表示没有匹配）：

```powershell
rg -n '_momo_checkout[_]kind' core integrations tests webui config docs .env.example
rg -n 'gopay|GoPay|IDR|VN|VND' core/upi_service.py
```

两条命令输出均为空；页面 JavaScript 全部内联脚本语法检查通过，修改的 Python 文件语法检查通过，git diff --check 通过。

6. diff 统计：+1219 / −30（仅本次提交；不计任务开始前的改动）。新增代码用于 UPI 业务、配置接入及回归；旧函数名和三种资格的限定列表均在原位替换。

7. 测试：使用项目 .venv/Scripts/python.exe。新增 UPI 测试 39 项通过；最终聚合验证 240 项通过、39 个 subtests，通过范围包含 UPI/MoMo/GoPay、国家代理、配置、全局停止、Checkout 分类、OAICS、Kakao、PayPal OAICS、GCash 重试、套餐筛选、缓存、WebUI 启动与 helper。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_upi_qualification.py tests/test_momo_qualification.py tests/test_gopay_qualification.py tests/test_detection_proxy_profiles.py tests/test_config_defaults.py tests/test_extract_center_cleanup.py tests/test_webui_helper_regressions.py tests/test_account_operation_global_stop.py tests/test_checkout_kind_detection.py tests/test_oaics_account_extraction.py tests/test_pay153_kakao_oaics.py tests/test_pay153_paypal_oaics_mode.py tests/test_gcash_retry.py tests/test_plan_filters_and_workers.py tests/test_summary_cache.py tests/test_webui_startup.py --deselect=tests/test_extract_center_cleanup.py::ExtractCenterCleanupTests::test_protocol_payment_routes_and_runtime_are_removed -q -p no:warnings
```

最后 5 行原始输出：

```text
...................................................................... [ 29%]
........................................................ [ 52%]
........................................................................ [ 82%]
..........................................          [100%]
240 passed, 1 deselected, 39 subtests passed in 9.64s
```

审查中发现并修复：Stripe `init 失败 [429/503]` 的状态码解析；停止后旧任务覆盖新队列；停止后继续 HTTP→HTTPS 重试。对应测试分别验证可重试/认证终止、代次隔离及阶段取消。异常只保留阶段/类型/状态码，测试验证 AT、代理密码及响应正文不进入诊断；付款续接函数在协议测试中设为触发即失败。

8. 未做的 / 存疑的：

   - 未使用真实账号、真实印度代理发起远端 Checkout。测试通过 mock 隔离网络和账号存储；支付方式实际开放情况以用户查询时响应为准。
   - 首轮相关回归 96 passed、1 failed：任务开始前存在未跟踪 integrations/paypal_agreement_protocol/，而 test_protocol_payment_routes_and_runtime_are_removed 断言该目录不存在。保留此目录，最终聚合显式 deselect 这一既有冲突，未删除目录或修改测试掩盖失败。
   - 现有 requests 字符编码依赖有警告；系统默认 Python 没有 pytest，验证使用项目 .venv。未安装或调整依赖。
   - 独立审查发现既有 MoMo 轻量轮询及代理删除 UI 存在缺口，未纳入 UPI 改动。
   - 工作区原有 config/roxybrowser.py、main.py、webui/app.py、webui/config_editor.py、webui/templates/index.html 改动及三个未跟踪项保留。本次提交只包含 UPI 相关差异；交付后工作区仍显示这些原有改动。
   - 没有重启正在使用的 WebUI 进程；运行旧进程时，重启 WebUI 后加载新路由。
