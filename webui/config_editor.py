# -*- coding: utf-8 -*-
"""
配置读写层（供 WebUI /api/config 使用）。

设计原则：
    1. 白名单：只暴露"运行时安全"的开关/数值/默认值，协议级常量
       （client_id / scope / sentinel 版本等）一律不开放，避免一改就废号。
    2. WebUI 可编辑项默认写入项目根 `.env`；超大代理池写入忽略目录中的运行时文件。
    3. `config/*.py` 只保留默认值；运行时通过 config.env_loader 加载持久化覆盖。
    4. 读取时优先 `.env`，缺失时回退解析 `config/*.py` 默认值。
"""
import ast
import os
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
EXPLICIT_EMPTY_LIST_KEYS = {
    "PROXY_POOL", "PROXY_API_PROFILES", "PLAN_CHECK_PROXY_PROFILES", "AT_VALIDITY_PROXY_PROFILES",
    "CHECKOUT_CHECK_PROXY_PROFILES", "GC_CHECK_PROXY_PROFILES", "QUALIFICATION_CHECK_PROXY_PROFILES",
    "GCASH_CHECK_PROXY_PROFILES", "GOPAY_CHECK_PROXY_PROFILES", "MOMO_CHECK_PROXY_PROFILES",
    "PAYPAL_OAICS_PROXY_PROFILES",
}


# ============================================================
# 白名单：每个可编辑项声明它在哪个文件、键名、类型、分组、说明
# type 决定前端控件 + 写回时的字面量格式：
#   bool   -> True/False
#   int    -> 整数
#   str    -> 带引号字符串
#   list_str_multiline -> 多行字符串列表（PROXY_POOL 专用，整块替换）
# ============================================================

EDITABLE_FIELDS = [
    # ---- 账号 AT 有效性 ----
    {
        "key": "AT_VALIDITY_AUTO_CHECK_ENABLED", "file": "at_validity.py", "type": "bool", "group": "账号 AT 有效性",
        "label": "启用 AT 定时检测", "help": "只调用 AT 会话接口验证未归档账号的 Access Token，不查询套餐或 0 元试用；专属池非空时用专属池，空池时只走本机 VPN/系统代理，不读取任何代理池",
    },
    {
        "key": "AT_VALIDITY_CHECK_INTERVAL_MINUTES", "file": "at_validity.py", "type": "int", "group": "账号 AT 有效性",
        "label": "AT 检测周期（分钟）", "help": "默认 360 分钟；也可直接在账号页顶部修改，范围 1 到 43200 分钟",
    },
    {
        "key": "AT_VALIDITY_RECHECK_INTERVAL_MINUTES", "file": "at_validity.py", "type": "int", "group": "账号 AT 有效性",
        "label": "已查询账号复查周期（分钟）", "help": "账号首次检测后使用的再次检测周期；例如 1440 分钟就是 1 天，范围 1 到 43200 分钟",
    },
    {
        "key": "AT_VALIDITY_REQUEST_ATTEMPTS", "file": "at_validity.py", "type": "int", "group": "账号 AT 有效性",
        "label": "AT 网络错误尝试次数", "help": "默认 5 次，范围 1 到 10；代理断开、TLS、超时、限流和服务端错误会重新建立会话后继续尝试",
    },
    {
        "key": "AT_VALIDITY_RETRY_DELAY", "file": "at_validity.py", "type": "float", "group": "账号 AT 有效性",
        "label": "AT 重试基础等待（秒）", "help": "默认 1 秒，随后按 1、2、4、8 秒退避，最长单次等待 8 秒",
    },
    # ---- WebUI 授权 ----
    {
        "key": "WEBUI_AUTH_CODE", "file": "codex.py", "type": "str", "group": "WebUI 授权",
        "label": "WebUI 授权码", "help": "仅保存在 .env（WEBUI_AUTH_CODE），避免出现在进程命令行中；保存后重启 WebUI 生效",
        "storage": "env", "secret": True,
    },
    {
        "key": "WEBUI_SESSION_SECRET", "file": "codex.py", "type": "str", "group": "WebUI 授权",
        "label": "Session 签名密钥", "help": "可选，保存在 .env（WEBUI_SESSION_SECRET）；不填则从固定授权码派生，修改授权码会使已有登录失效",
        "storage": "env", "secret": True,
    },
    # ---- 功能开关 ----
    {
        "key": "ENABLE_CODEX_AUTO", "file": "codex.py", "type": "bool", "group": "功能开关",
        "label": "启用 Codex OAuth", "help": "注册成功后自动跑 Codex 授权（全新session+接码），落盘 codex-邮箱.json",
    },
    {
        "key": "GC_REGISTRATION_MODE", "file": "roxybrowser.py", "type": "bool", "group": "功能开关",
        "label": "GC 注册模式", "help": "仅限 Roxy：每个任务使用独立窗口；拿到 AT 后保留窗口，人工支付后可循环查 Plus，并可随时停止查询或精确关闭该任务窗口",
    },
    {
        "key": "REGISTRATION_DRIVER", "file": "roxybrowser.py", "type": "str", "group": "注册方式",
        "label": "注册驱动", "help": "默认推荐 roxy；protocol=纯协议，容易封号不建议；roxy=RoxyBrowser；cloak=CloakBrowser；browser_use=Browser Use Cloud+Playwright；skyvern=Skyvern Browser Sessions+Playwright",
    },

    # ---- CloakBrowser ----
    {
        "key": "CLOAK_HEADLESS", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak无头", "help": "True=无头运行；False=显示浏览器窗口",
    },
    {
        "key": "CLOAK_HUMANIZE", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak人工行为", "help": "启用 CloakBrowser humanize 鼠标/键盘/滚动行为",
    },
    {
        "key": "CLOAK_GEOIP", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak按出口定位", "help": "按当前出口 IP 自动匹配时区/语言/WebRTC IP；支持显式代理、系统代理/VPN",
    },
    {
        "key": "CLOAK_LOCALE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak语言", "help": "留空自动；日本可填 ja-JP，美国 en-US",
    },
    {
        "key": "CLOAK_TIMEZONE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak时区", "help": "留空自动；日本可填 Asia/Tokyo，美国 America/Los_Angeles",
    },
    {
        "key": "CLOAK_USE_PROXY", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak使用代理", "help": "把本项目传入或代理池抽取的代理传给 CloakBrowser",
    },
    {
        "key": "CLOAK_LICENSE_KEY", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak License", "help": "Pro license；留空使用免费 binary",
    },
    {
        "key": "CLOAK_FINGERPRINT_SEED", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak指纹Seed", "help": "留空每次随机；固定值可保持同一指纹",
    },
    {
        "key": "CLOAK_USER_DATA_DIR", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak用户目录", "help": "留空使用临时上下文；填写路径则持久化 cookies/cache",
    },
    {
        "key": "CLOAK_SELENIUM_TIMEOUT", "file": "cloakbrowser.py", "type": "int", "group": "CloakBrowser",
        "label": "Cloak超时", "help": "页面和元素等待超时时间，秒",
    },
    {
        "key": "CLOAK_KEEP_BROWSER_OPEN", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "保留Cloak浏览器", "help": "调试时开启，任务结束后不自动关闭",
    },

    # ---- Browser Use Cloud ----
    {
        "key": "BROWSER_USE_API_KEY", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "Browser Use API Key", "help": "保存在 .env（BROWSER_USE_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "BROWSER_USE_PROXY_COUNTRY_CODE", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "代理国家代码", "help": "两位国家码，如 jp/us/sg；配合 Browser Use 内置 residential proxy",
    },
    {
        "key": "BROWSER_USE_USE_PROXY", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "使用内置代理", "help": "True=连接参数带 proxyCountryCode；False=不强制传国家代理参数",
    },
    {
        "key": "BROWSER_USE_PROFILE_ID", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "Profile ID", "help": "可选。填写则复用 Browser Use profile 的 cookies/localStorage；批量建议留空",
    },
    {
        "key": "BROWSER_USE_CDP_BASE", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "CDP 地址", "help": "默认 wss://connect.browser-use.com",
    },
    {
        "key": "BROWSER_USE_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "Browser Use",
        "label": "操作超时(秒)", "help": "Playwright 默认操作超时",
    },
    {
        "key": "BROWSER_USE_SESSION_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "Browser Use",
        "label": "云端keepAlive(分钟)", "help": "传给 Browser Use connect URL 的 timeout/keepAlive；程序会自动限制到 1-240，建议 240",
    },
    {
        "key": "BROWSER_USE_FAST_MODE", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "快速模式", "help": "减少 Browser Use 额外等待和 humanize 延迟；建议开启，异常排查时可关闭",
    },
    {
        "key": "BROWSER_USE_LOG_TIMING", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "耗时日志", "help": "打印 Browser Use 各阶段耗时：连接、打开页面、邮箱、OTP、手机、callback",
    },
    {
        "key": "BROWSER_USE_KEEP_BROWSER_OPEN", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "保留远端会话", "help": "调试时可不主动 browser.close()；默认 False",
    },
    {
        "key": "BROWSER_USE_START_URL", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },

    # ---- Skyvern Cloud Browser ----
    {
        "key": "SKYVERN_API_KEY", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "Skyvern API Key", "help": "保存在 .env（SKYVERN_API_KEY），用于创建 Skyvern Browser Session",
        "storage": "env", "secret": True,
    },
    {
        "key": "SKYVERN_API_BASE", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "API 地址", "help": "默认 https://api.skyvern.com",
    },
    {
        "key": "SKYVERN_BROWSER_SESSION_TIMEOUT", "file": "skyvern.py", "type": "int", "group": "Skyvern",
        "label": "Session 超时(分钟)", "help": "创建 Skyvern Browser Session 时传入的 timeout",
    },
    {
        "key": "SKYVERN_BROWSER_PROFILE_ID", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "Browser Profile ID", "help": "可选，复用 Skyvern browser profile",
    },
    {
        "key": "SKYVERN_PROXY_LOCATION", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "代理地区", "help": "可填 jp/us/gb 等简写；会自动转为 Skyvern 枚举，如 jp→RESIDENTIAL_JP；留空不传",
    },
    {
        "key": "SKYVERN_BROWSER_TYPE", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "浏览器类型", "help": "Skyvern 支持 msedge / chrome / stealth-chromium；旧值 chromium-headful 会自动转为 stealth-chromium",
    },
    {
        "key": "SKYVERN_AD_BLOCKER", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "广告拦截", "help": "创建 Skyvern Browser Session 时启用 ad_blocker",
    },
    {
        "key": "SKYVERN_GENERATE_BROWSER_PROFILE", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "保存浏览器Profile", "help": "Session 结束时是否让 Skyvern 生成/保存 browser profile",
    },
    {
        "key": "SKYVERN_KEEP_BROWSER_OPEN", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不主动关闭 Skyvern Browser Session",
    },
    {
        "key": "SKYVERN_START_URL", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },
    {
        "key": "ROXY_API_BASE", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy API 地址", "help": "默认 http://127.0.0.1:50000；需在 Roxy 应用 API 配置中开启",
    },
    {
        "key": "ROXY_API_TOKEN", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy API Key", "help": "保存在 .env（ROXY_API_TOKEN），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "ROXY_PROFILE_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 环境ID", "help": "指定要打开的 Roxy 浏览器环境/Profile ID；留空则尝试创建临时环境",
    },
    {
        "key": "ROXY_WORKSPACE_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 工作区ID", "help": "创建一号一环境时必填，会作为 workspaceId 提交给 Roxy 创建 Profile 接口",
    },
    {
        "key": "ROXY_PROJECT_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 项目ID", "help": "从 /browser/workspace 的 project_details.projectId 获取；创建 Profile 时会作为 projectId 提交",
    },
    {
        "key": "ROXY_WORKSPACE_LIST_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "获取团队接口", "help": "默认 /browser/workspace；点击获取团队/项目时会先试此路径，再自动尝试常见兼容路径",
    },
    {
        "key": "ROXY_OPEN_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "打开接口路径", "help": "默认 /browser/open；如 Roxy 版本不同可在此调整",
    },
    {
        "key": "ROXY_OPEN_HEADLESS", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "无头启动窗口", "help": "打开 Roxy 环境时向 /browser/open 传 headless；False=显示窗口，True=无头启动",
    },
    {
        "key": "ROXY_CLOSE_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "关闭接口路径", "help": "默认 /browser/close",
    },
    {
        "key": "ROXY_KEEP_BROWSER_OPEN", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不自动关闭 Roxy 环境",
    },
    {
        "key": "ROXY_MAX_CONCURRENT_REGISTRATIONS", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Roxy 最大并发", "help": "同时运行的 Roxy 注册 Profile 上限；默认 2，防止冷缓存启动时耗尽渲染器和桌面合成资源",
    },
    {
        "key": "ROXY_LOW_TRAFFIC", "file": "roxybrowser.py", "type": "bool", "group": "Roxy流量优化",
        "label": "省流模式", "help": "拦截明确非必要的遥测、重资源和可选登录入口；安全验证与未知请求默认放行",
    },
    {
        "key": "ROXY_STATIC_CACHE", "file": "roxybrowser.py", "type": "bool", "group": "Roxy流量优化",
        "label": "共享公开JS/CSS缓存", "help": "仅缓存一方公开GET脚本/样式，不共享Cookie、Session或Profile",
    },
    {
        "key": "ROXY_TRAFFIC_CAPTURE", "file": "roxybrowser.py", "type": "bool", "group": "Roxy流量优化",
        "label": "记录流量摘要", "help": "读取Roxy performance log并输出压缩响应字节、缓存命中和Host/Path聚合",
    },
    {
        "key": "ROXY_TRAFFIC_BUDGET_BYTES", "file": "roxybrowser.py", "type": "int", "group": "Roxy流量优化",
        "label": "浏览器流量预算字节", "help": "仅作诊断告警，默认3 MiB，不作为注册成功条件",
    },
    {
        "key": "ROXY_CACHE_DIR", "file": "roxybrowser.py", "type": "str", "group": "Roxy流量优化",
        "label": "公开缓存目录", "help": "只保存公开JS/CSS的缓存文件；不要指向账号、Cookie或Profile目录",
    },
    {
        "key": "ROXY_CACHE_MAX_AGE", "file": "roxybrowser.py", "type": "int", "group": "Roxy流量优化",
        "label": "缓存有效期秒", "help": "默认604800秒（7天）",
    },
    {
        "key": "ROXY_CACHE_MAX_ITEM_BYTES", "file": "roxybrowser.py", "type": "int", "group": "Roxy流量优化",
        "label": "缓存对象上限字节", "help": "超过上限的对象不写入共享缓存，默认8 MiB",
    },
    {
        "key": "ROXY_CACHE_REFRESH_RATE", "file": "roxybrowser.py", "type": "float", "group": "Roxy流量优化",
        "label": "缓存随机刷新比例", "help": "默认0.12；只刷新小对象，避免永久复用旧资源",
    },
    {
        "key": "ROXY_CACHE_REFRESH_BUDGET_BYTES", "file": "roxybrowser.py", "type": "int", "group": "Roxy流量优化",
        "label": "单会话刷新预算字节", "help": "默认262144字节（256 KiB）",
    },
    {
        "key": "ROXY_CACHE_REFRESH_MAX_ITEM_BYTES", "file": "roxybrowser.py", "type": "int", "group": "Roxy流量优化",
        "label": "单次刷新对象上限字节", "help": "默认65536字节（64 KiB）",
    },
    {
        "key": "ROXY_ONE_PROFILE_PER_ACCOUNT", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "一号一环境", "help": "每个账号强制创建新 Roxy Profile，用完关闭并删除，禁止复用固定环境",
    },
    {
        "key": "ROXY_DELETE_PROFILE_AFTER_RUN", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "结束后删除环境", "help": "一号一环境模式下，任务结束后删除本轮创建的 Roxy Profile",
    },
    {
        "key": "ROXY_RANDOM_OS_ON_CREATE", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境随机OS", "help": "创建 Roxy 环境时每次在 Windows / macOS 中随机，不固定 macOS",
    },
    {
        "key": "ROXY_RANDOM_OS_CHOICES", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "随机OS范围", "help": "逗号分隔，默认 Windows,macOS；Roxy 支持 Windows / macOS / Linux / IOS / Android",
    },
    {
        "key": "ROXY_DEFAULT_OS", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "固定系统", "help": "选择 Windows 或 macOS 后会自动关闭随机 OS；仅影响之后新建的 Roxy 环境",
        "options": ["Windows", "macOS"],
    },
    {
        "key": "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境随机名称", "help": "创建 Roxy 环境时自动生成不同名称，避免固定 gpt-free-register",
    },
    {
        "key": "ROXY_PROFILE_NAME_PREFIX", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "随机名称前缀", "help": "默认 rb；实际名称格式类似 rb-时间戳-随机码",
    },
    {
        "key": "ROXY_CREATE_USE_PROXY_POOL", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境使用代理池", "help": "创建 Roxy 环境时从配置页「代理池」随机取一个代理，写入 Roxy proxyInfo",
    },
    {
        "key": "ROXY_PROXY_CHECK_CHANNEL", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "代理检测通道", "help": "写入 Roxy proxyInfo.checkChannel；留空则不传，默认 IPRust.io",
    },
    {
        "key": "ROXY_PROXY_PREFLIGHT_ATTEMPTS", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "单代理出口检测次数", "help": "限制为 1-10；0 也按 1 次处理，避免坏代理无限卡住",
    },
    {
        "key": "ROXY_PROXY_PREFLIGHT_PROXY_ATTEMPTS", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "出口检测换代理上限", "help": "默认 3；一条检测失败就从粘性池随机换下一条，不重复已失败代理",
    },
    {
        "key": "ROXY_BROWSER_EXIT_IP_ATTEMPTS", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "窗口出口复核次数", "help": "限制为 1-10；默认 1，窗口启动后快速复核一次",
    },
    {
        "key": "ROXY_DELETE_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "删除接口路径", "help": "默认 /browser/delete；如 Roxy 版本不同可调整",
    },
    {
        "key": "ROXY_CREATE_API_ATTEMPTS", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "创建环境尝试次数", "help": "默认 2；只重试 Roxy 明确的创建忙磁状态，固定 15 秒指纹失败不会再重试",
    },
    {
        "key": "ROXY_API_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Roxy 本地 API 超时", "help": "创建/打开/关闭/删除环境的单请求秒数，与 Selenium 页面等待分开",
    },
    {
        "key": "ROXY_API_RETRIES", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Roxy API 尝试次数", "help": "默认 2；用于打开/关闭/删除等短暂错误，创建接口另行收敛",
    },
    {
        "key": "ROXY_API_RETRY_DELAY", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Roxy API 重试间隔", "help": "默认 1 秒；第二次前的短延迟",
    },
    {
        "key": "ROXY_EMAIL_SUBMIT_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "邮箱提交单轮超时", "help": "等待进入密码/OTP 页的单轮秒数，默认 20",
    },
    {
        "key": "ROXY_EMAIL_SUBMIT_ATTEMPTS", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "邮箱提交次数", "help": "UI + NextAuth 兜底后的最大轮数，默认 2",
    },
    {
        "key": "ROXY_OTP_MAX_WAIT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Roxy OTP单轮等待", "help": "Roxy 注册每轮收取邮箱 OTP 的最长秒数，默认 25",
    },
    {
        "key": "ROXY_OTP_MAX_ATTEMPTS", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Roxy OTP最大轮数", "help": "仅验证码被拒绝/过期时重发；取码端点不可达会快速结束",
    },
    {
        "key": "ROXY_OTP_RETRY_ON_MAIL_TIMEOUT", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "取码超时后重发", "help": "默认关闭：单轮取码超时就结束；开启后才重发并再等一轮",
    },
    {
        "key": "ROXY_OTP_SUBMIT_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "OTP提交超时", "help": "提交 OTP 后等待资料页/登录态的主观察窗口，默认 15 秒",
    },
    {
        "key": "ROXY_OTP_SUBMIT_ATTEMPTS", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "OTP 表单提交次数", "help": "默认 2；首次无响应时刷新、重填同一验证码后只再提交一次",
    },
    {
        "key": "ROXY_PASSWORD_SUBMIT_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "密码提交总超时", "help": "密码表单的总观察秒数，默认 16；单次无响应会重新定位原表单",
    },
    {
        "key": "ROXY_PASSWORD_SUBMIT_ATTEMPTS", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "密码表单提交次数", "help": "默认 2；只允许一次快速重提，不重走整个注册流程",
    },
    {
        "key": "ROXY_PROFILE_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "资料页超时", "help": "姓名/生日页总预算，相同 DOM 连续停滞会更早失败",
    },
    {
        "key": "ROXY_SESSION_WAIT_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Session总等待", "help": "注册后等待 ChatGPT accessToken 的秒数，默认 25",
    },
    {
        "key": "ROXY_SESSION_REQUEST_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Session单请求超时", "help": "页面内 /api/auth/session fetch 的硬超时，防止超出上层 deadline",
    },
    {
        "key": "ROXY_AT_RECOVERY_PREFLIGHT_ATTEMPTS", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "AT恢复预检次数", "help": "后台邮箱登录恢复 AT 时的网络预检次数，默认 2",
    },
    {
        "key": "CODEX_OAUTH_DRIVER", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "Codex授权驱动", "help": "默认推荐 roxy；protocol=原协议授权；roxy=用 RoxyBrowser；cloak=用 CloakBrowser；browser_use=用 Browser Use Cloud；skyvern=用 Skyvern；same_as_registration=跟随注册驱动",
    },
    {
        "key": "CODEX_LOCAL_PROXY", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "Codex本地代理", "help": "仅用于 Codex 授权、HeroSMS 接码和 Token 交换；与注册代理池分离，例如 http://127.0.0.1:7890",
    },
    {
        "key": "CODEX_EMAIL_OTP_WAIT", "file": "codex.py", "type": "int", "group": "Codex",
        "label": "Codex邮箱验证码等待", "help": "单轮等待新邮箱验证码的秒数，默认 120；旧码失败后仅在当前验证页点击一次重发",
    },
    {
        "key": "CODEX_HEADLESS", "file": "codex.py", "type": "bool", "group": "Codex",
        "label": "Codex接码无头运行", "help": "仅让 Codex/接码浏览器无头运行，不影响注册浏览器窗口",
    },
    {
        "key": "ROXY_CODEX_CALLBACK_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Codex回调超时", "help": "Roxy Codex OAuth 等待 localhost:1455 callback 的最长秒数",
    },
    {
        "key": "ENABLE_2FA", "file": "twofa.py", "type": "bool", "group": "功能开关",
        "label": "启用密码 + 2FA(TOTP)", "help": "默认关闭；开启后新账号才设置 OpenAI 密码并启用 MFA，需浏览器驱动和自动邮箱（会多收一封 OTP 邮件）",
    },
    {
        "key": "TWOFA_OTP_MAX_WAIT", "file": "twofa.py", "type": "int", "group": "邮箱 / OTP",
        "label": "2FA OTP 最大等待(秒)", "help": "仅用于 2FA 重认证取码，不改变普通注册 OTP；默认 120",
    },
    {
        "key": "TWOFA_OTP_POLL_INTERVAL", "file": "twofa.py", "type": "int", "group": "邮箱 / OTP",
        "label": "2FA OTP 轮询间隔(秒)", "help": "仅用于 2FA 重认证取码；默认 2",
    },
    {
        "key": "TWOFA_OTP_SETTLE_SECONDS", "file": "twofa.py", "type": "int", "group": "邮箱 / OTP",
        "label": "2FA OTP 稳定等待(秒)", "help": "确认新验证码稳定后再提交；默认 1",
    },
    {
        "key": "TWOFA_GENERIC_API_REQUEST_TIMEOUT", "file": "twofa.py", "type": "float", "group": "邮箱 / OTP",
        "label": "2FA 通用取码请求超时", "help": "仅用于 2FA 重认证取码主请求；默认 12 秒",
    },
    {
        "key": "TWOFA_GENERIC_API_RETRY_TIMEOUT", "file": "twofa.py", "type": "float", "group": "邮箱 / OTP",
        "label": "2FA 通用取码重试超时", "help": "仅用于 2FA 重认证取码短重试；默认 8 秒",
    },
    {
        "key": "TWOFA_GENERIC_API_MAX_CONSECUTIVE_ERRORS", "file": "twofa.py", "type": "int", "group": "邮箱 / OTP",
        "label": "2FA 取码连续错误上限", "help": "仅用于 2FA 重认证取码；默认 2",
    },
    {
        "key": "ENABLE_FLOW_TRIGGER", "file": "flow_trigger.py", "type": "bool", "group": "功能开关",
        "label": "启用 Flow 触发", "help": "注册成功后自动调用内部 Flow 接口（不影响注册结果）",
    },
    {
        "key": "ENABLE_HUMANIZE_DELAY", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "启用随机停顿", "help": "在注册、OTP、授权等步骤之间加入随机等待，更接近人工操作节奏",
    },
    {
        "key": "HUMANIZE_DELAY_FACTOR", "file": "humanize.py", "type": "float", "group": "人工节奏",
        "label": "停顿倍率", "help": "随机停顿整体倍率；1.0=默认，0.5=减半，2.0=加倍",
    },
    {
        "key": "ENABLE_HUMANIZE_BROWSER_ACTIONS", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "浏览器动作随机化", "help": "Roxy/Cloak 点击、输入、页面观察使用随机鼠标落点和逐字输入，降低机械操作痕迹",
    },
    # ---- 邮箱 / OTP ----
    {
        "key": "USE_EMAIL_SERVICE", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "自动取邮箱+收码", "help": "True=从邮箱池自动领邮箱并自动收 OTP；False=手动模式：用 REGISTER_EMAIL，OTP 在任务页手填",
    },
    {
        "key": "REGISTER_EMAIL", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "手动注册邮箱", "help": "USE_EMAIL_SERVICE=False 时必填。例如你的 outlook.com 地址；OTP 去网页邮箱看，再回任务页提交",
    },
    {
        "key": "REGISTER_PASSWORD", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "OpenAI 注册密码", "help": "仅在开启密码 + 2FA 时使用；留空则每号随机生成并保存到账号凭据",
        "secret": True,
    },
    {
        "key": "REGISTER_NAME", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "显示名称", "help": "留空则自动生成英文名",
    },
    {
        "key": "OTP_MAX_WAIT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 最长等待(秒)", "help": "等待验证码邮件的最长秒数，超时判失败",
    },
    {
        "key": "OTP_POLL_INTERVAL", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 轮询间隔(秒)", "help": "每隔多少秒查一次新邮件",
    },
    {
        "key": "GENERIC_API_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "通用取码请求超时", "help": "取码 URL 主请求的秒数，默认 8",
    },
    {
        "key": "GENERIC_API_RETRY_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "通用取码短重试超时", "help": "主请求网络失败后立即重试一次的秒数，默认 5",
    },
    {
        "key": "GENERIC_API_MAX_CONSECUTIVE_ERRORS", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "通用取码熔断轮数", "help": "主请求+短重试都失败的连续轮数，达到后停止空转",
    },
    {
        "key": "GENERIC_API_REGISTRATION_FAILURE_LIMIT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "邮箱注册失败停用阈值", "help": "同一取码邮箱连续失败达到该次数后停用，默认 2",
    },
    {
        "key": "EMAIL_SOURCE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "邮箱来源", "help": "可填单个或多个，逗号分隔并按顺序兜底：outlook,generic_api,domain_api,inbox_mate,cloudflare_domain,cloudflare,gptmail,mailnest,cloudmail",
    },
    {
        "key": "DOMAIN_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "域名邮箱取码地址", "help": "可留空；导入账户页面 URL 时自动识别。只粘贴邮箱+密码时填写该服务的 m.php 地址",
    },
    {
        "key": "INBOX_MATE_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Inbox Mate 服务地址", "help": "默认 https://mail.ap1x.xyz，可换成内网或其他邮箱任务站",
    },
    {
        "key": "GPTMAIL_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "GPTMail API Key", "help": "选择 gptmail 邮箱来源时必填；保存在 .env，不会写入 config 源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare API 地址", "help": "Worker 临时邮箱 API 根地址，如 https://mail.example.com；选择 cloudflare 时必填",
        "storage": "env",
    },
    {
        "key": "CLOUDFLARE_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare API Key", "help": "匿名可空；admin 模式填 ADMIN_PASSWORD；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_AUTH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 鉴权模式", "help": "none / bearer / x-api-key / x-admin-auth / query-key",
    },
    {
        "key": "CLOUDFLARE_CUSTOM_AUTH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 全局密码", "help": "Worker PASSWORDS，注入 x-custom-auth；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_PATH_ACCOUNTS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 创建路径", "help": "默认 /api/new_address；admin 常用 /admin/new_address",
    },
    {
        "key": "CLOUDFLARE_PATH_MESSAGES", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 邮件路径", "help": "默认 /api/mails",
    },
    {
        "key": "CLOUDFLARE_PATH_DOMAINS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 域名路径", "help": "默认 /api/domains（预留）",
    },
    {
        "key": "CLOUDFLARE_PATH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare Token路径", "help": "默认 /api/token（fallback 预留）",
    },
    {
        "key": "CLOUDFLARE_DEFAULT_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "Cloudflare 默认域名", "help": "收信域名，每行一个或逗号分隔；创建时轮询使用，可留空",
    },
    {
        "key": "CLOUDFLARE_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 请求超时(秒)", "help": "HTTP 请求超时，默认 20",
    },
    {
        "key": "CLOUDFLARE_NAME_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 随机名前缀长度", "help": "admin 创建时 local-part 长度，默认 10",
    },
    {
        "key": "OUTLOOK_FETCH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Outlook取件模式", "help": "auto=远端优先，远端 402/DEPLOYMENT_DISABLED 自动切 Graph 直连；direct=只用 Microsoft Graph 直连；remote=只用远端服务",
    },
    {
        "key": "EMAIL_DOMAIN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "转发域名(cloudflare_domain)", "help": "仅 cloudflare_domain 使用：Email Routing 的域名，如 mydomain.com；与 EMAIL_SOURCE=cloudflare 无关",
    },
    {
        "key": "QQ_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱地址", "help": "仅 cloudflare_domain：接收 Email Routing 转发的 QQ 邮箱，如 123456@qq.com",
    },
    {
        "key": "QQ_IMAP_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱 IMAP 授权码", "help": "仅 cloudflare_domain：QQ IMAP 授权码，保存在 .env，不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "MAIL_NEST_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "MailNest API Key", "help": "选择 mailnest 邮箱来源时必填；保存在 .env，不会写入 config 源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "MAIL_NEST_PROJECT_CODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "MailNest 项目代码", "help": "项目代码 默认 chatgpt001 获取页面 mailnest.top/buy-email",
    },
    {
        "key": "CLOUDMAIL_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail API 地址", "help": "Cloud Mail Worker/API 地址，例如 https://mail.example.com",
    },
    {
        "key": "CLOUDMAIL_ADMIN_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail管理员邮箱", "help": "用于生成 Token；域名被平台隐藏时也会用它登录读取域名",
        "storage": "env",
    },
    {
        "key": "CLOUDMAIL_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail 密码", "help": "用于自动获取 Token；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDMAIL_TOKEN_PATH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail Token路径", "help": "固定使用 /api/public/genToken；如部署版本不同可修改",
    },
    {
        "key": "CLOUDMAIL_AUTH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail Token", "help": "CloudMail/Cloud Mail API Authorization Token；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDMAIL_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "CloudMail 域名列表", "help": "可留空；运行时会自动从平台获取。也可点“获取 CloudMail 域名”缓存到这里",
    },
    {
        "key": "CLOUDMAIL_AUTO_ADD_USER", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "CloudMail自动创建用户", "help": "生成随机邮箱后调用 /api/public/addUser 创建用户",
    },
    {
        "key": "CLOUDMAIL_RANDOM_LOCAL_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "CloudMail随机名前缀长度", "help": "生成邮箱 local-part 的长度，建议 10-16",
    },
    # ---- 浏览器地区画像 ----
    {
        "key": "BROWSER_LOCALE_PROFILE", "file": "browser.py", "type": "str", "group": "浏览器画像",
        "label": "地区画像", "help": "应与代理出口地区一致；可选 jp/cn/us/sg。当前本地代理实测为日本东京，推荐 jp",
    },

    {
        "key": "AUTO_BROWSER_LOCALE_FROM_IP", "file": "browser.py", "type": "bool", "group": "浏览器画像",
        "label": "按出口IP自动画像", "help": "开启后每个 BrowserSession 会用当前代理出口 IP 自动选择语言/时区；失败时回退到地区画像",
    },
    {
        "key": "IP_GEO_TIMEOUT", "file": "browser.py", "type": "float", "group": "浏览器画像",
        "label": "IP定位超时(秒)", "help": "出口 IP 地理信息接口的单次请求超时；接口失败会自动回退，不影响注册",
    },

    # ---- 代理池 ----
    {
        "key": "PROXY_POOL", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "粘性代理池(每行一个)",
        "help": "不限制为 10 条，可直接粘贴 1000+ 条代理；关闭 API 代理后，每个新注册窗口从完整列表随机挑选一条，并在该窗口全流程固定使用",
        "ui_variant": "large_pool", "storage": "runtime_file",
    },
    {
        "key": "PROXY_POOL_ACTIVE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "静态池优先代理", "help": "可选；填写 PROXY_POOL 中的完整代理 URL会优先使用，留空时纯协议注册从粘性静态池随机选择并在单次会话内固定",
    },
    {
        "key": "PROXY_API_ENABLED", "file": "proxy.py", "type": "bool", "group": "代理池",
        "label": "启用API代理", "help": "开启后每个新注册会话/指纹环境优先实时调用代理 API，静态代理池可留空",
    },
    {
        "key": "PROXY_API_URL", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "单一代理API地址(兼容)", "help": "未配置多地区列表时使用；配置多地区列表后由当前地区选择覆盖",
        "storage": "env", "secret": True,
    },
    {
        "key": "PROXY_API_PROFILES", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "代理API列表(由导入按钮管理)", "help": "每行一个 Cliproxy API URL；系统从 region 参数自动识别地区",
        "storage": "env", "secret": True,
    },
    {
        "key": "PROXY_API_ACTIVE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "当前API地区", "help": "系统根据 URL 的 region 参数生成选择项",
    },
    {
        "key": "PROXY_API_PROTOCOL", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "API代理协议", "help": "Cliproxy 白名单接口填写 socks5h（代理端解析DNS，传给本地指纹浏览器时自动转为SOCKS5）",
    },
    {
        "key": "PROXY_API_TIMEOUT", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "API请求超时(秒)", "help": "获取临时代理的单次超时，建议 10-20 秒",
    },
    {
        "key": "PROXY_API_MAX_ATTEMPTS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "API最大尝试次数", "help": "接口或代理验证临时失败时等待后重试；建议 3 次",
    },
    {
        "key": "PROXY_API_RETRY_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "API重试等待(秒)", "help": "第一次失败等待该秒数，第二次失败等待双倍秒数；建议 3 秒",
    },
    {
        "key": "PROXY_API_VALIDATE", "file": "proxy.py", "type": "bool", "group": "代理池",
        "label": "获取后验证代理", "help": "推荐开启；验证端口、SOCKS5 隧道和 TLS 证书，不可用时自动重新调用 API",
    },
    {
        "key": "PROXY_API_VALIDATE_TIMEOUT", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "代理验证超时(秒)", "help": "临时节点的端口/握手检测超时，建议 10-15 秒",
    },
    {
        "key": "PROXY_API_VALIDATE_CONNECT", "file": "proxy.py", "type": "bool", "group": "代理池",
        "label": "验证代理隧道", "help": "在 SOCKS5 greeting 后继续测试 CONNECT，提前剔除会导致 SSL 断开的节点",
    },
    {
        "key": "PROXY_API_VALIDATE_TARGET", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "代理验证目标", "help": "默认 chatgpt.com:443；用于检测 SOCKS5 CONNECT 与目标站点证书",
    },
    {
        "key": "PROXY_API_VALIDATE_TLS", "file": "proxy.py", "type": "bool", "group": "代理池",
        "label": "验证代理TLS证书", "help": "推荐开启；淘汰给 chatgpt.com 返回错误证书的代理节点",
    },
    {
        "key": "PROXY_API_CACHE_SECONDS", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "API结果缓存(秒)", "help": "0=每个新会话重新取代理；大于0会在指定秒数内复用同一代理",
    },
    {
        "key": "PROXY_API_FAIL_CLOSED", "file": "proxy.py", "type": "bool", "group": "代理池",
        "label": "API失败禁止直连", "help": "推荐开启；API获取失败时中止任务，避免真实出口IP意外暴露",
    },
    {
        "key": "PLAN_CHECK_PROXY_MODE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐/Agent网络模式", "help": "用于查套餐和生成 Agent Token；auto=本地代理可用则走代理、未监听则直连；proxy=强制代理；direct=强制直连",
    },
    {
        "key": "PLAN_CHECK_PROXY", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐/Agent专用代理", "help": "可填写 http://127.0.0.1:端口，供套餐和 Agent Token 使用；可能包含认证信息，仅保存到 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "PLAN_CHECK_PROXY_PROFILES", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "套餐检测静态代理池", "help": "由“加入代理池”读取代理用户名中的 region-XX 并自动归类；只供套餐/Plus/试用检测使用，每个国家可加入多条静态代理",
        "storage": "runtime_file", "secret": True,
    },
    {
        "key": "PLAN_CHECK_PROXY_ACTIVE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐检测当前国家", "help": "选择国家后，套餐/Plus/试用检测会在该国家静态池内随机洗牌取代理",
    },
    {
        "key": "AT_VALIDITY_PROXY_PROFILES", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "AT 有效性检测专属代理池", "help": "只供 AT 定时/立即有效性检测使用；非空时只用此池，空池时只走本机 VPN/系统代理并完全跳过 PROXY_POOL；不会查询套餐或 0 元试用",
        "storage": "runtime_file", "secret": True,
    },
    {
        "key": "AT_VALIDITY_PROXY_ACTIVE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "AT 有效性检测当前国家", "help": "选择国家后，AT 检测会在该国家专属静态池内随机洗牌取代理",
    },
    {
        "key": "CHECKOUT_CHECK_PROXY_PROFILES", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "Checkout检测静态代理池", "help": "由“加入代理池”读取代理用户名中的 region-XX 并自动归类；与套餐检测静态池完全独立",
        "storage": "runtime_file", "secret": True,
    },
    {
        "key": "CHECKOUT_CHECK_PROXY_ACTIVE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "Checkout检测当前国家", "help": "选择国家后，OAICS/CSLIVE 检测会在该国家静态池内随机洗牌取代理",
    },
    {
        "key": "GC_CHECK_PROXY_PROFILES", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "gc查询代理池", "help": "GCash 资格检测专用 PH 出口代理；每行一个，支持 名称|代理 或 名称|API。只影响账号页“查询GC”",
        "storage": "env", "secret": True,
    },
    {
        "key": "QUALIFICATION_CHECK_PROXY_PROFILES", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "资格检测按国家代理池", "help": "GCash 固定调用 PH 国家池，GoPay 固定调用 ID 国家池；设置页可一次加入多国静态代理，后续资格检测继续复用此池",
        "storage": "runtime_file", "secret": True,
    },
    {
        "key": "QUALIFICATION_CHECK_PROXY_ACTIVE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "资格代理管理国家", "help": "只用于设置页选择和删除国家池；实际检测由资格类型固定选择 PH 或 ID",
    },
    {
        "key": "GCASH_CHECK_PROXY_PROFILES", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "GCash 资格代理池（PH）", "help": "GCash 独立菲律宾静态代理池；账号查询 GCash 只使用这里的代理",
        "storage": "runtime_file", "secret": True,
    },
    {
        "key": "GCASH_CHECK_PROXY_ACTIVE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "GCash 当前国家", "help": "固定为 PH（菲律宾）资格检测出口",
    },
    {
        "key": "GOPAY_CHECK_PROXY_PROFILES", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "GoPay 资格代理池（ID）", "help": "GoPay 独立印度尼西亚静态代理池；账号查询 GoPay 只使用这里的代理",
        "storage": "runtime_file", "secret": True,
    },
    {
        "key": "GOPAY_CHECK_PROXY_ACTIVE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "GoPay 当前国家", "help": "固定为 ID（印度尼西亚）资格检测出口",
    },
    {
        "key": "MOMO_CHECK_PROXY_PROFILES", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "MoMo 资格代理池（VN）", "help": "MoMo 独立越南静态代理池；账号查询 MoMo 只使用这里的代理",
        "storage": "runtime_file", "secret": True,
    },
    {
        "key": "MOMO_CHECK_PROXY_ACTIVE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "MoMo 当前国家", "help": "固定为 VN（越南）资格检测出口",
    },
    {
        "key": "PAYPAL_OAICS_PROXY_PROFILES", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "PayPal OAICS专用代理池", "help": "每行一个代理，支持 名称|代理；账号页 OAICS 提链只使用此池",
        "storage": "env", "secret": True,
    },
    {
        "key": "PAYPAL_OAICS_PROXY_ACTIVE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "PayPal OAICS当前线路", "help": "留空时并发任务轮换专用池全部代理；填写名称后只使用该线路",
        "storage": "env", "secret": True,
    },
    {
        "key": "PAYPAL_OAICS_WORKERS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "PayPal OAICS并发数", "help": "账号界面 OAICS 提链的后台并发线程数，建议 2-8",
    },
    {
        "key": "PLAN_CHECK_TIMEOUT", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent单次超时(秒)", "help": "只限制单次网络请求，不是套餐检测总时长；套餐检测失败会继续重试到明确结果",
    },
    {
        "key": "PLAN_CHECK_MAX_ATTEMPTS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐最大尝试次数", "help": "0=持续检测到明确结果；大于 0 才限制尝试次数。账号页和 GC 的 AT 套餐检测固定使用持续模式",
    },
    {
        "key": "PLAN_CHECK_RETRY_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent重试间隔(秒)", "help": "查套餐和生成 Agent Token 的重试间隔，按尝试次数递增；服务端 Retry-After 优先",
    },
    {
        "key": "PLAN_CHECK_WORKERS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询并发数", "help": "自动、手动和批量查套餐共用；默认 10，不设置软件固定上限；Agent Token 生成使用独立队列",
    },
    {
        "key": "PLAN_CHECK_QUEUE_LIMIT", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询队列上限", "help": "防止异常批量操作无限堆积，建议 100-1000",
    },
    {
        "key": "PLAN_CHECK_MIN_INTERVAL", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent请求最小间隔(秒)", "help": "限制查套餐和生成 Agent Token 的请求启动频率，降低 429 风险",
    },
    {
        "key": "PLAN_CHECK_JITTER", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent请求随机抖动(秒)", "help": "在查套餐和生成 Agent Token 的最小间隔上增加随机延迟，避免请求过于规律",
    },
    # ---- Codex 配置 ----
    {
        "key": "SUB2API_AUTO_EXPORT", "file": "sub2api.py", "type": "bool", "group": "Codex",
        "label": "Agent sub2 自动同步", "help": "生成 Codex Agent Token 成功后自动同步到 sub2api",
    },
    {
        "key": "SUB2API_SYNC_MODE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 同步模式", "help": "api=直接上传接口；file=写本地json；both=接口+本地json",
    },
    {
        "key": "SUB2API_API_BASE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API基址", "help": "sub2api 服务地址；Agent Token 上传和 Codex OAuth 共用，例如 http://127.0.0.1:8080",
    },
    {
        "key": "SUB2API_API_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API Key", "help": "sub2api 管理接口 API Key；请求头使用 x-api-key；为空则不带鉴权头", "storage": "env", "secret": True,
    },
    {
        "key": "SUB2API_API_TIMEOUT", "file": "sub2api.py", "type": "int", "group": "Codex",
        "label": "sub2 超时", "help": "sub2api 请求超时秒数",
    },
    {
        "key": "SUB2API_OUTPUT_PATH", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 本地路径", "help": "仅 SUB2API_SYNC_MODE=file/both 时使用；相对路径按项目根目录解析",
    },
    {
        "key": "SUB2API_PROXY_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 代理键", "help": "可选；写入 account.proxy_key，并在 proxies 为空时初始化 proxies[0].proxy_key",
    },
    # ---- 接码平台 ----
    # ---- Codex：基础 / CPA / sub2api 配置 ----
    {
        "key": "CODEX_AUTH_URL_SOURCE", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "授权地址来源", "help": "cpa=CPA生成并上传CPA；sub2=sub2生成并上传sub2；local=本地PKCE",
    },
    {
        "key": "CPA_MANAGEMENT_URL", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "CPA 管理地址", "help": "例如 http://localhost:8317/admin/oauth；程序会取 origin 调用 /v0/management/*",
    },
    {
        "key": "CPA_MANAGEMENT_KEY", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "管理密钥", "help": "保存在 .env（CPA_MANAGEMENT_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "CPA_REQUEST_TIMEOUT", "file": "codex.py", "type": "int", "group": "Codex",
        "label": "CPA 超时(秒)", "help": "请求 CPA 管理接口的超时时间",
    },
    {
        "key": "CPA_SAVE_CALLBACK_RECEIPT", "file": "codex.py", "type": "bool", "group": "Codex",
        "label": "保存CPA回执", "help": "CPA 未返回完整授权文件时，本地仍保存一份回调提交记录",
    },

    {
        "key": "SMS_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "HeroSMS API 地址", "help": "默认 https://hero-sms.com/stubs/handler_api.php，一般无需修改",
    },
    {
        "key": "SMS_COUNTRY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "HeroSMS 国家策略", "help": "推荐 auto：按金额上限自动选择价格最低且有库存的国家；也可填写固定数字国家 ID",
    },
    {
        "key": "SMS_PRIORITY_COUNTRIES", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "优先国家", "help": "auto 模式优先尝试国家 ID，逗号分隔；默认 56 西班牙、54 墨西哥、33 哥伦比亚",
    },
    {
        "key": "SMS_EXCLUDED_COUNTRIES", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "永久排除国家", "help": "HeroSMS 国家 ID，逗号分隔；默认 4，永久排除菲律宾号码",
    },
    {
        "key": "SMS_SERVICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "HeroSMS 服务代码", "help": "传给 getNumber 的 service；OpenAI 默认使用 dr",
    },
    {
        "key": "SMS_MAX_RETRIES", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "换号重试次数", "help": "一个号收不到短信/被OpenAI拒时换下一个号，最多重试几次",
    },
    {
        "key": "SMS_CODE_WAIT", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "单号等短信(秒)", "help": "单个号等待短信到达的最长秒数，超时则换号",
    },
    {
        "key": "SMS_MAX_PRICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "单号金额上限", "help": "默认 0.15；自动筛选国家和最终 getNumber 都会强制使用该 maxPrice",
    },
    {
        "key": "SMS_POLL_INTERVAL", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "短信轮询间隔(秒)", "help": "调用 HeroSMS getStatus 查询验证码的间隔",
    },
    {
        "key": "SMS_REQUEST_TIMEOUT", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "API 请求超时(秒)", "help": "HeroSMS 单次 HTTP 请求超时时间",
    },
    {
        "key": "SMS_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "HeroSMS API Key", "help": "HeroSMS 平台 API Key，保存在 .env（SMS_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
]

_FIELD_BY_KEY = {f["key"]: f for f in EDITABLE_FIELDS}


# ============================================================
# 读：解析源码取当前值（不 import，避免缓存/副作用）
# ============================================================

def _config_path(filename: str) -> Path:
    path = (_CONFIG_DIR / filename).resolve()
    # 防目录穿越：必须落在 config/ 下
    if _CONFIG_DIR not in path.parents:
        raise ValueError(f"非法配置路径: {filename}")
    return path


def _literal_default_from_expr(node):
    """尽量从赋值表达式中取“源码默认值”，不执行模块代码。

    兼容：
      KEY = "literal"
      KEY: str = env_str("KEY", "default")
      KEY = env_bool("KEY", True)
      KEY = env_value("KEY", 123, "int")
    """
    try:
        return ast.literal_eval(node)
    except Exception:
        pass

    if isinstance(node, ast.Call):
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        # env_str/env_bool/env_int/env_float/env_list 的第二个位置参数是默认值。
        if func_name in {"env_str", "env_bool", "env_int", "env_float", "env_list"}:
            if len(node.args) >= 2:
                try:
                    return ast.literal_eval(node.args[1])
                except Exception:
                    return None
            return None

        # env_value(key, default, vtype)
        if func_name == "env_value" and len(node.args) >= 2:
            try:
                return ast.literal_eval(node.args[1])
            except Exception:
                return None

    return None


def _find_assignment_value_node(source: str, key: str):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == key:
                return node.value
    return None


def _parse_value_from_source(source: str, key: str, vtype: str):
    """从源码里解析 KEY 的当前值。失败返回 None。"""
    if vtype == "list_str_multiline":
        # 用 AST 解析整个模块，取这个赋值的 list 字面量
        value_node = _find_assignment_value_node(source, key)
        if value_node is None:
            return None
        try:
            val = ast.literal_eval(value_node)
            if isinstance(val, (list, tuple)):
                return [str(x) for x in val]
        except (ValueError, SyntaxError):
            return None
        return None

    # 标量：优先 AST 取默认值，避免 env_str("KEY", "") 被当成普通字符串。
    value_node = _find_assignment_value_node(source, key)
    if value_node is not None:
        value = _literal_default_from_expr(value_node)
        if value is not None:
            return value

    # AST 失败时再回退到旧的正则解析。
    m = re.search(
        rf"^{re.escape(key)}\s*(?::[^=\n]+)?=\s*(.+?)\s*(?:#.*)?$",
        source, re.MULTILINE,
    )
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _parse_env_typed_value(raw: str, fallback, vtype: str):
    """把 .env 字符串按字段类型转换；失败时回退 fallback。"""
    from config.env_loader import env_value
    return env_value("__NO_SUCH_ENV_KEY__", fallback, vtype) if raw is None else _coerce_raw_value(raw, fallback, vtype)


def _coerce_raw_value(raw: str, fallback, vtype: str):
    try:
        if raw is None or str(raw).strip() == "":
            return fallback
        if vtype == "bool":
            return str(raw).strip().lower() in ("true", "1", "yes", "on", "y")
        if vtype == "int":
            return int(str(raw).strip())
        if vtype == "float":
            return float(str(raw).strip())
        if vtype == "list_str_multiline":
            text = str(raw)
            try:
                val = ast.literal_eval(text)
                if isinstance(val, (list, tuple)):
                    return [str(x).strip() for x in val if str(x).strip()]
            except Exception:
                pass
            return [line.strip() for line in text.splitlines() if line.strip()]
        return str(raw)
    except Exception:
        return fallback


def get_config() -> list[dict]:
    """返回所有可编辑项的当前值 + 元信息，供前端渲染表单。

    优先读取 `.env` / 环境变量；没有配置时回退到 `config/*.py` 默认值。
    """
    from config.env_loader import load_env, read_env_file
    load_env(override=True)
    env_file_values = read_env_file()

    out = []
    for field in EDITABLE_FIELDS:
        key = field["key"]
        path = _config_path(field["file"])
        source = path.read_text(encoding="utf-8") if path.exists() else ""
        fallback = _parse_value_from_source(source, key, field["type"])

        runtime_value = None
        if field.get("storage") == "runtime_file":
            from config.env_loader import read_runtime_list_file
            runtime_value = read_runtime_list_file(key)

        if runtime_value is not None:
            value = runtime_value
        elif key in env_file_values:
            raw_env_value = env_file_values[key]
            if field["type"] == "list_str_multiline" and key in EXPLICIT_EMPTY_LIST_KEYS and str(raw_env_value).strip() == "":
                value = []
            else:
                value = _coerce_raw_value(raw_env_value, fallback, field["type"])
        elif os.getenv(key) is not None:
            value = _coerce_raw_value(os.getenv(key, ""), fallback, field["type"])
        else:
            value = fallback

        if field["type"] in ("str", "list_str_multiline"):
            value = _normalize_config_value(value, field["type"])
        item = dict(field)
        item["storage"] = field.get("storage", "env")
        item["value"] = value
        out.append(item)
    return out


# ============================================================
# 写：统一写 .env，不修改 config/*.py
# ============================================================


_PLACEHOLDER_EMPTY = {
    "", "-", "—", "无", "空", "none", "null", "n/a", "na", "未设置", "未配置",
}


def _normalize_config_value(value, vtype: str):
    """把前端/历史占位空值规范化，避免 '-' 被当成真实配置。"""
    if vtype == "str":
        s = "" if value is None else str(value).strip()
        if s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
            return ""
        return s
    if vtype == "list_str_multiline":
        if value is None:
            return []
        if isinstance(value, str):
            lines = value.splitlines()
        elif isinstance(value, (list, tuple)):
            lines = list(value)
        else:
            lines = [str(value)]
        out = []
        for item in lines:
            s = str(item or "").strip()
            if not s or s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
                continue
            out.append(s)
        return out
    return value


def _format_literal(value, vtype: str) -> str:
    """把前端传来的值格式化成 Python 字面量字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "str":
        s = str(value)
        # 用 repr 保证转义安全，但统一成双引号风格
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise ValueError(f"_format_literal 不支持的类型: {vtype}")


def _replace_scalar(source: str, key: str, literal: str) -> str:
    """替换 `KEY[: 类型] = 旧值` 行的右值，保留行内注释和类型标注。"""
    pattern = re.compile(
        rf"^(?P<head>{re.escape(key)}\s*(?::[^=\n]+)?=\s*)"
        rf"(?P<val>.+?)"
        rf"(?P<tail>\s*(?:#.*)?)$",
        re.MULTILINE,
    )
    if not pattern.search(source):
        raise ValueError(f"未在源码中找到可替换的赋值: {key}")
    return pattern.sub(lambda m: f"{m.group('head')}{literal}{m.group('tail')}", source, count=1)


def _replace_proxy_pool(source: str, lines: list[str]) -> str:
    """整块替换 PROXY_POOL = [ ... ] 列表字面量（保留前面的赋值头）。"""
    items = [ln.strip() for ln in lines if ln.strip()]
    if items:
        body = "\n".join(
            '    "' + it.replace("\\", "\\\\").replace('"', '\\"') + '",'
            for it in items
        )
        literal = "[\n" + body + "\n]"
    else:
        literal = "[]"

    # 匹配 PROXY_POOL = [ ... ]（含跨行），用 AST 定位起止偏移最稳
    tree = ast.parse(source)
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "PROXY_POOL":
                src_lines = source.splitlines(keepends=True)
                start = node.value.lineno          # 值（[）所在行，1-based
                end = node.value.end_lineno        # 值（]）所在行，1-based
                col = node.value.col_offset         # [ 在起始行的列偏移
                # 保留起始行 [ 之前的内容（即 "PROXY_POOL = " 或 "PROXY_POOL: list = "）
                prefix = src_lines[start - 1][:col]
                # 保留结束行 ] 之后的内容（行内注释 / 换行）
                end_line = src_lines[end - 1]
                suffix = end_line[node.value.end_col_offset:]
                new_lines = (
                    src_lines[: start - 1]
                    + [prefix + literal + suffix]
                    + src_lines[end:]
                )
                return "".join(new_lines)
    raise ValueError("未找到 PROXY_POOL 赋值")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _format_env_value(value, vtype: str) -> str:
    """把前端值格式化成适合写入 .env 的字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on", "y")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "list_str_multiline":
        lines = _normalize_config_value(value, vtype)
        return "\n".join(lines) if lines else "[]"
    if vtype == "str":
        return _normalize_config_value(value, vtype)
    return "" if value is None else str(value)


def update_config(updates: dict) -> dict:
    """批量更新配置；大代理池写运行时文件，其余字段写项目根 `.env`。"""
    from config.env_loader import write_env_values, write_runtime_list_file, load_env

    updated, ignored, runtime_file_updated = [], [], []
    env_updates: dict[str, str] = {}

    for key, value in updates.items():
        field = _FIELD_BY_KEY.get(key)
        if field is None:
            ignored.append(key)
            continue
        if field.get("storage") == "runtime_file":
            write_runtime_list_file(key, _normalize_config_value(value, field["type"]))
            # 清掉 .env 中可能遗留的大字段，仅保留一个小占位值；运行时文件优先。
            env_updates[key] = "[]"
            updated.append(key)
            runtime_file_updated.append(key)
            continue
        env_updates[key] = _format_env_value(value, field["type"])
        updated.append(key)


    env_updated = write_env_values(env_updates) if env_updates else []
    if env_updated:
        load_env(override=True)

    return {
        "updated": updated,
        "ignored": ignored,
        "env_updated": env_updated,
        "runtime_file_updated": runtime_file_updated,
    }
