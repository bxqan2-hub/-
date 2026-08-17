# PAY.153 Checkout Link Router

<p align="center">
  <strong>多支付通道提链控制台。</strong><br>
  Hosted、PayPal、iDEAL、UPI、PIX、Team 与 Codex 空间方案统一任务化处理。
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="Flask" src="https://img.shields.io/badge/Backend-Flask-000000?logo=flask&logoColor=white">
  <img alt="curl_cffi" src="https://img.shields.io/badge/HTTP-curl__cffi-0EA5E9">
  <img alt="Repository" src="https://img.shields.io/badge/Repository-Private-111827">
</p>

## 页面预览

### 桌面端

![桌面端页面](docs/screenshots/desktop.png)

### 手机端

<p align="center">
  <img src="docs/screenshots/mobile.png" alt="手机端页面" width="390">
</p>

## 功能概览

- 支持 Plus、Pro、Team 与 Codex 低价空间等计划参数。
- 支持 Hosted 官方长链、PayPal、iDEAL、UPI 和 PIX 支付路径。
- 支持 Access Token 和 Session JSON 自动识别。
- 双代理池、1–500 条代理、本地保存、代理检测和地区自适应。
- 按支付地区自动选择币种，并处理不受支持币种的回退逻辑。
- 支持优惠更新、金额重新校验与零元账单判断。
- UPI 零元路径优先使用 Go Elements/B 引擎，执行 IN → VN → IN 路由、inline confirm、approval 与结果轮询。
- 支持失败重试，每轮重建 Checkout、设备标识和支付参数。
- 提供全局 RPM、单 IP RPM、并发限制和任务排队。
- 提供不出现在公开导航中的私有直通页，使用独立执行池，不占用公开队列与 RPM。
- 支持停止任务、进度展示、精简前端日志和完整后台日志。
- 支持支付二维码、跳转链接、倒计时和结果复制。
- 深色/浅色主题，以及桌面端和手机端响应式布局。

## 支付路径

| 路径 | 用途 |
|---|---|
| Hosted | 返回官方 Checkout 长链 |
| PayPal | 创建 PayPal PaymentMethod 并返回 Approve 跳转 |
| iDEAL | 荷兰银行支付路径 |
| UPI | 印度 UPI 支付与二维码 |
| PIX | 巴西 PIX 支付与二维码 |

## 项目结构

```text
pay153-checkout-link/
├─ app.py                       # Flask API、任务队列、限流与入口
├─ provider_checkout.py         # Checkout、地区、账单与支付提供商流程
├─ stripe_checkout.py           # Stripe 初始化、金额、确认与跳转处理
├─ upi_go_runner.py             # UPI Go 子进程封装、取消和结果映射
├─ tools/upi_go/                # UPI Elements/B Go 源码
├─ billing_address_resolver.py  # 在线地图及账单地址解析
├─ sentinel_token.py            # Sentinel Token 生成与请求封装
├─ sentinel_sdk_full.js         # Sentinel SDK/VM 辅助代码
├─ gen_token_jsdom.js           # Node/JSDOM Token 辅助脚本
├─ static/
│  ├─ index.html                # 提链控制台
│  ├─ app.js                    # 前端任务与交互逻辑
│  └─ styles.css                # 主界面样式
├─ docs/screenshots/            # README 截图
├─ requirements.txt
└─ .env.example
```

## 快速启动

```bash
git clone https://github.com/1537271403/pay153-checkout-link.git
cd pay153-checkout-link

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 本项目的 Sentinel helper 还需要锁定的 Node.js 依赖
npm ci

python app.py
```

本站不会单独启动这个 Flask 应用。请从主项目根目录运行
`start-webui.bat`，通过主站的 `/pay153/` 路径访问。

## 环境变量

复制示例文件：

```bash
cp .env.example .env
```

| 变量 | 作用 |
|---|---|
| `GOOGLE_MAPS_API_KEY` | 在线地图地址解析密钥，可选 |
| `PAY153_BILLING_ADDRESS_CACHE` | 地址缓存文件路径 |
| `PAY153_WORKERS` | 后台任务并发数 |
| `PAY153_LOG_DIR` | 完整后台日志目录 |
| `PAY153_LEGACY_BASE` | 旧服务兼容地址，可选 |
| `PAY153_INTERNAL_KEY` | 内部 API 请求头校验密钥 |
| `PAY153_PRIVATE_PAGE_KEY` | 私有直通页首次访问密钥，与内部 API 密钥分离 |
| `PAY153_INTERNAL_WORKERS` | 私有直通独立执行池并发数，默认 `5` |
| `PAY153_UPI_GO_BINARY` | UPI Go 二进制路径，默认 `tools/upi_go/pix_extract_slot` |
| `PAY153_UPI_GO_PROMO_COUNTRY` | UPI 优惠更新地区，默认 `VN` |
| `PAY153_UPI_GO_EFFECTIVE_RETRIES` | 单次 Flask 尝试内的 Go 有效尝试数，默认 `1` |

## 代理池

每行一条代理，支持常见格式：

```text
host:port:username:password
http://username:password@host:port
https://username:password@host:port
socks5://username:password@host:port
```

任务提交后会根据支付路径和地区选择代理；代理凭据仅应通过网页或环境变量传入。

### 各支付路径的代理职责

| 支付路径 | 代理池 1 | 代理池 2 | 最终账单 / 支付地区 |
|---|---|---|---|
| Hosted | Checkout、优惠更新、Stripe 金额校验；使用账号常用地区或所选账单地区 | 不使用 | 使用页面选择的国家与币种 |
| 菲律宾短链 | `US` Checkout 入口 | `TR` 优惠识别与更新；关闭优惠时可与池 1 同为 `US` | `PH/PHP` |
| PayPal | 优惠识别与更新，通常使用 `TR/JP`；巴西特殊流程使用 `BR` | PayPal 支付出口与资料地区；推荐 `DE`，巴西流程使用 `BR` | `US/GB/DE/FR/IE/NL/ES/IT/AT` 原生；其他地区回退 `DE/EUR` Checkout |
| iDEAL | 优惠识别与更新，推荐 `NL` | `NL` Checkout、Stripe 与 iDEAL 支付 | `NL/EUR` |
| UPI | 优惠识别，推荐 `TR/JP/BR` | `IN` Checkout 与 UPI 支付 | `IN/INR` |
| PIX | `BR` 单链路，负责 Checkout、优惠、Stripe、PIX 与 approval | 不使用，复用池 1 | `BR/BRL` |
| MoMo | `VN` 单链路，负责 Checkout、优惠、Stripe、MoMo 与 approval | 不使用，复用池 1 | `VN/VND` |
| GCash | `US` Checkout 入口、GCash 确认与跳转 | 优惠识别与更新，推荐 `VN` | `PH/PHP` |
| Kakao Pay | `VN` 优惠识别与更新 | `KR` Checkout、Kakao Pay confirm 与 Nicepay 跳转 | `KR/KRW` |
| TWINT | 优惠识别与更新，推荐 `CH` | `CH` Checkout 与 TWINT 支付 | `CH/CHF` |

PayPal 的代理池 2 同时决定 PayPal 支付资料地区。原生 PayPal 账单地区直接使用对应国家；例如巴西等非原生地区会创建 `DE/EUR` OpenAI Checkout，但仍使用代理池 2 检测到的国家生成 PayPal 支付资料。

## 生产部署

PAY.153 已由主站进程内加载，不需要单独配置 systemd、Gunicorn 或 Nginx
端口。生产环境只需部署根目录的 `web.py`，提链中心通过 `/pay153/` 提供。

## 数据与密钥

仓库未包含以下生产数据：

- `.env` 与真实管理令牌
- 代理账号和密码
- Access Token、Session JSON 与账号池
- 任务结果、日志、截图缓存和历史备份
- `data/`、`logs/`、`backups/` 与虚拟环境

提交代码前应继续检查环境变量、调试输出和测试文件，避免将运行凭据写入 Git 历史。

## 与协议支付项目的关系

本仓库负责生成支付链接或二维码；PayPal BA 链生成后，可以交给独立的协议支付服务继续处理：

https://github.com/1537271403/paypal-agreement-protocol

## 更新流程

```bash
git pull
python -m pip install -r requirements.txt
sudo systemctl restart pay153
```

检查集成环境：

```bash
python tools/check_integrations.py
```
