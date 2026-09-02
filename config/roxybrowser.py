# -*- coding: utf-8 -*-
"""
RoxyBrowser 指纹浏览器自动化注册配置。

官方文档：
- API 默认 host: http://127.0.0.1:50000
- 所有接口请求头必须带 token
- 可配合 Selenium / Puppeteer / Playwright 自动化
"""
from config.env_loader import env_str, apply_env_overrides


# 注册驱动：
#   "protocol"     = 原有 curl_cffi 纯协议注册（容易封号，不建议）
#   "roxy"         = 调用 RoxyBrowser 指纹浏览器 + Selenium 自动化注册
#   "cloak"        = 调用 CloakBrowser + Playwright/Selenium 适配层注册
#   "browser_use"  = Browser Use Cloud stealth Chromium + Playwright
#   "skyvern"      = Skyvern Browser Sessions + Playwright
REGISTRATION_DRIVER: str = "roxy"

# RoxyBrowser 本地 API
ROXY_API_BASE: str = "http://127.0.0.1:50100"
ROXY_API_TOKEN: str = env_str("ROXY_API_TOKEN", "")

# Roxy 环境/Profile ID；留空时使用 ROXY_PROFILE_CREATE_* 先创建临时环境（如果接口支持）
ROXY_PROFILE_ID: str = ""

# Roxy 工作区 ID。Roxy 创建 Profile 时接口要求 workspaceId，必须填写。
# 可在 Roxy 工作区/团队页面或 API 返回中查看。
ROXY_WORKSPACE_ID: str = "90143"

# Roxy 项目 ID。/browser/workspace 返回 project_details.projectId；创建 Profile 时一并提交。
ROXY_PROJECT_ID: str = "97471"

# 获取团队/工作区列表接口路径。不同版本若不同，可在 WebUI 修改；客户端也会自动尝试多个常见路径。
ROXY_WORKSPACE_LIST_PATH: str = "/browser/workspace"
ROXY_WORKSPACE_LIST_METHOD: str = "GET"

# 接口路径模板。不同版本如有差异，只改这里即可。
# {profile_id} 会替换为 ROXY_PROFILE_ID。
ROXY_OPEN_PATH: str = "/browser/open"
ROXY_CLOSE_PATH: str = "/browser/close"
ROXY_CREATE_PATH: str = "/browser/create"

# 接口方法：常见 open/close 为 GET；若你的版本要求 POST，可在 WebUI/配置里改。
ROXY_OPEN_METHOD: str = "POST"
ROXY_CLOSE_METHOD: str = "POST"
ROXY_CREATE_METHOD: str = "POST"

# 打开浏览器时是否无头启动：
#   False = 显示 Roxy 浏览器窗口（便于观察/调试）
#   True  = 无头启动，不显示窗口（如果当前 Roxy 版本支持 headless）
ROXY_OPEN_HEADLESS: bool = False

# 打开浏览器时附加参数；会合并到 /browser/open 请求体，优先级高于默认值。
ROXY_OPEN_EXTRA_PARAMS: dict = {}

# Selenium 行为
ROXY_SELENIUM_TIMEOUT: int = 90
# 本地 Roxy OpenAPI 与 Selenium 页面等待使用不同预算。创建 Profile 会触发
# 指纹/代理初始化，在并发批次中经常超过 15 秒；创建请求单独使用更长预算，
# 避免把“服务端仍在创建”误报为失败后重复创建孤儿环境。
ROXY_API_TIMEOUT: int = 15
# 仅用于 /browser/create；关闭/删除等短生命周期接口继续使用
# ROXY_API_TIMEOUT，避免它们失败时长时间占用队列。
ROXY_CREATE_API_TIMEOUT: int = 45
ROXY_KEEP_BROWSER_OPEN: bool = False

# 注册快速等待预算。这些值只限制单个阶段，成功信号仍会立即结束等待。
# 旧逻辑把邮箱提交、OTP、session 请求共用 90s Selenium 超时，
# 一次卡住的 async fetch 就可以超出上层 deadline。分开后可独立调整。
# 只提交一次并持续观察状态：真实线路的邮箱 -> OTP 跳转可能需要 20~50s。
# 延长观察窗口不会拖慢成功路径（成功信号立即返回），但可避免超时后重复填写。
ROXY_EMAIL_SUBMIT_TIMEOUT: int = 50
ROXY_EMAIL_SUBMIT_ATTEMPTS: int = 1
# 最新批次成功 OTP 全部在 16s 内取得；25s 仍无码时结束本轮，避免坏邮箱
# 占住窗口到 40s。成功信号仍会立即返回。
ROXY_OTP_MAX_WAIT: int = 25
ROXY_OTP_POLL_INTERVAL: int = 2
ROXY_OTP_SETTLE_SECONDS: int = 1
ROXY_OTP_MAX_ATTEMPTS: int = 2
ROXY_OTP_RETRY_ON_MAIL_TIMEOUT: bool = False
ROXY_OTP_SUBMIT_TIMEOUT: int = 15
ROXY_OTP_SUBMIT_ATTEMPTS: int = 2
ROXY_OTP_PENDING_GRACE: int = 0
ROXY_PASSWORD_SUBMIT_TIMEOUT: int = 16
ROXY_PASSWORD_SUBMIT_ATTEMPTS: int = 2
ROXY_PROFILE_TIMEOUT: int = 35
ROXY_PROFILE_STALL_LIMIT: int = 3
ROXY_SESSION_WAIT_TIMEOUT: int = 25
ROXY_SESSION_AUTO_JUMP_WAIT: int = 8
ROXY_SESSION_REQUEST_TIMEOUT: int = 6
ROXY_AT_RECOVERY_PREFLIGHT_ATTEMPTS: int = 2
# Conservative registration traffic optimization for Roxy/Selenium.
ROXY_LOW_TRAFFIC: bool = True
ROXY_STATIC_CACHE: bool = True
ROXY_TRAFFIC_CAPTURE: bool = True
ROXY_TRAFFIC_BUDGET_BYTES: int = 3145728
ROXY_CACHE_DIR: str = "data/browser_static_cache"
ROXY_CACHE_MAX_AGE: int = 604800
ROXY_CACHE_MAX_ITEM_BYTES: int = 8388608
ROXY_CACHE_REFRESH_RATE: float = 0.12
ROXY_CACHE_REFRESH_BUDGET_BYTES: int = 262144
ROXY_CACHE_REFRESH_MAX_ITEM_BYTES: int = 65536

# GC 注册模式：注册完成拿到 AT 后保留本次独立 Roxy 窗口，等待人工支付并查询 Plus。
# False 时完全沿用普通注册的窗口清理流程。
GC_REGISTRATION_MODE: bool = False

# Roxy API 短重试。生命周期接口最多 2 次；create 只会对明确忙碌状态重试。
ROXY_API_RETRIES: int = 2
ROXY_CREATE_API_ATTEMPTS: int = 2
ROXY_API_RETRY_DELAY: int = 1

# 环境生命周期：
#   True  = 一号一环境：每个账号强制创建新 Profile，用完关闭并删除，不允许复用 ROXY_PROFILE_ID
#   False = 可复用 ROXY_PROFILE_ID 或只关闭不删除
ROXY_ONE_PROFILE_PER_ACCOUNT: bool = True

# 一号一环境结束后是否删除 Profile。建议保持 True。
ROXY_DELETE_PROFILE_AFTER_RUN: bool = True

# 删除环境接口路径/方法；如你的 Roxy 版本不同，只改这里。
ROXY_DELETE_PATH: str = "/browser/delete"
ROXY_DELETE_METHOD: str = "POST"

# 创建 Roxy 环境时随机系统指纹；开启后每次 /browser/create 在 Windows / macOS 里随机选一个，
# 避免固定 macOS 指纹。
ROXY_RANDOM_OS_ON_CREATE: bool = True
ROXY_RANDOM_OS_CHOICES: str = "Windows,macOS"

# 创建 Roxy 环境时随机名称；开启后会覆盖 ROXY_PROFILE_CREATE_PAYLOAD 里的固定 name。
ROXY_RANDOM_PROFILE_NAME_ON_CREATE: bool = True
ROXY_PROFILE_NAME_PREFIX: str = "rb"

# 创建 Roxy 环境时默认系统指纹。仅在 ROXY_RANDOM_OS_ON_CREATE=False 时使用。
# Roxy 官方 os 枚举：Windows / macOS / Linux / IOS / Android。
ROXY_DEFAULT_OS: str = "macOS"
# 留空则使用 Roxy 对应系统的默认/最大版本；如需固定可填 15.3.2、14.7 等。
ROXY_DEFAULT_OS_VERSION: str = ""

# 创建 Roxy 环境时是否使用 config/proxy.py 的 PROXY_POOL：
#   False = 不主动给 Roxy 环境设置代理
#   True  = 每次创建环境时从 PROXY_POOL 随机取一个代理写入 proxyInfo
ROXY_CREATE_USE_PROXY_POOL: bool = False

# Roxy 代理检测通道；留空则不传 checkChannel。
ROXY_PROXY_CHECK_CHANNEL: str = "IPRust.io"

# 创建环境前先读取出口 IP。每条代理只快速测一次；失败时最多换 3 条代理，
# 杜绝 attempts=0 导致十个注册线程在错误代理格式上无限循环。
ROXY_PROXY_PREFLIGHT_ATTEMPTS: int = 1
ROXY_PROXY_PREFLIGHT_PROXY_ATTEMPTS: int = 3
ROXY_PROXY_PREFLIGHT_RETRY_DELAY: float = 0.5

# 窗口启动后再从 Selenium 上下文复核实际出口 IP；仍失败则终止注册。
ROXY_BROWSER_EXIT_IP_ATTEMPTS: int = 1
ROXY_BROWSER_EXIT_IP_RETRY_DELAY: float = 0.5

# 没有 ROXY_PROFILE_ID 时创建环境的最小 payload；按你的 Roxy 版本字段调整。
# 默认开启 ROXY_RANDOM_PROFILE_NAME_ON_CREATE，因此这里的 name 只是兜底值。
ROXY_PROFILE_CREATE_PAYLOAD: dict = {
    "windowName": "gpt-free-register",
    "os": "macOS",
}


# Roxy Codex 授权等待 callback 的最长秒数
ROXY_CODEX_CALLBACK_TIMEOUT: int = 180

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'REGISTRATION_DRIVER': 'str', 'ROXY_API_BASE': 'str', 'ROXY_API_TOKEN': 'str',
    'ROXY_PROFILE_ID': 'str', 'ROXY_WORKSPACE_ID': 'str', 'ROXY_PROJECT_ID': 'str',
    'ROXY_WORKSPACE_LIST_PATH': 'str', 'ROXY_OPEN_PATH': 'str', 'ROXY_OPEN_HEADLESS': 'bool',
    'ROXY_CLOSE_PATH': 'str', 'ROXY_KEEP_BROWSER_OPEN': 'bool',
    'ROXY_SELENIUM_TIMEOUT': 'int', 'ROXY_API_TIMEOUT': 'int',
    'ROXY_CREATE_API_TIMEOUT': 'int',
    'ROXY_EMAIL_SUBMIT_TIMEOUT': 'int',
    'ROXY_EMAIL_SUBMIT_ATTEMPTS': 'int', 'ROXY_OTP_MAX_WAIT': 'int',
    'ROXY_OTP_POLL_INTERVAL': 'int', 'ROXY_OTP_SETTLE_SECONDS': 'int',
    'ROXY_OTP_MAX_ATTEMPTS': 'int', 'ROXY_OTP_RETRY_ON_MAIL_TIMEOUT': 'bool',
    'ROXY_OTP_SUBMIT_TIMEOUT': 'int', 'ROXY_OTP_SUBMIT_ATTEMPTS': 'int',
    'ROXY_OTP_PENDING_GRACE': 'int', 'ROXY_PROFILE_TIMEOUT': 'int',
    'ROXY_PASSWORD_SUBMIT_TIMEOUT': 'int', 'ROXY_PASSWORD_SUBMIT_ATTEMPTS': 'int',
    'ROXY_PROFILE_STALL_LIMIT': 'int', 'ROXY_SESSION_WAIT_TIMEOUT': 'int',
    'ROXY_SESSION_AUTO_JUMP_WAIT': 'int', 'ROXY_SESSION_REQUEST_TIMEOUT': 'int',
    'ROXY_AT_RECOVERY_PREFLIGHT_ATTEMPTS': 'int',
    'ROXY_API_RETRIES': 'int', 'ROXY_API_RETRY_DELAY': 'int',
    'ROXY_CREATE_API_ATTEMPTS': 'int', 'GC_REGISTRATION_MODE': 'bool',
    'ROXY_ONE_PROFILE_PER_ACCOUNT': 'bool', 'ROXY_DELETE_PROFILE_AFTER_RUN': 'bool',
    'ROXY_RANDOM_OS_ON_CREATE': 'bool', 'ROXY_RANDOM_OS_CHOICES': 'str',
    'ROXY_DEFAULT_OS': 'str', 'ROXY_RANDOM_PROFILE_NAME_ON_CREATE': 'bool',
    'ROXY_PROFILE_NAME_PREFIX': 'str', 'ROXY_CREATE_USE_PROXY_POOL': 'bool',
    'ROXY_PROXY_CHECK_CHANNEL': 'str', 'ROXY_PROXY_PREFLIGHT_ATTEMPTS': 'int',
    'ROXY_PROXY_PREFLIGHT_PROXY_ATTEMPTS': 'int',
    'ROXY_PROXY_PREFLIGHT_RETRY_DELAY': 'float', 'ROXY_BROWSER_EXIT_IP_ATTEMPTS': 'int',
    'ROXY_BROWSER_EXIT_IP_RETRY_DELAY': 'float', 'ROXY_DELETE_PATH': 'str',
    'ROXY_CODEX_CALLBACK_TIMEOUT': 'int',
})
apply_env_overrides(globals(), {
    'ROXY_LOW_TRAFFIC': 'bool',
    'ROXY_STATIC_CACHE': 'bool',
    'ROXY_TRAFFIC_CAPTURE': 'bool',
    'ROXY_TRAFFIC_BUDGET_BYTES': 'int',
    'ROXY_CACHE_DIR': 'str',
    'ROXY_CACHE_MAX_AGE': 'int',
    'ROXY_CACHE_MAX_ITEM_BYTES': 'int',
    'ROXY_CACHE_REFRESH_RATE': 'float',
    'ROXY_CACHE_REFRESH_BUDGET_BYTES': 'int',
    'ROXY_CACHE_REFRESH_MAX_ITEM_BYTES': 'int',
})
