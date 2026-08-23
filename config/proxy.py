# -*- coding: utf-8 -*-
"""
代理池配置

每个注册窗口随机抽取一个代理，并在该窗口全流程内固定使用。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）

跟随系统代理：
    PROXY_POOL 留空，或写入 system / auto / sys
    时，pick_proxy() 会读取：
      1) 环境变量 HTTP(S)_PROXY / ALL_PROXY
      2) Windows 系统代理（Clash 开启“系统代理”时通常写在这里）
    Clash 仅开 TUN、不开系统代理时，这里可能为空（流量由 TUN 接管，等同直连对本机程序）。
"""
from __future__ import annotations

from config.env_loader import apply_env_overrides, read_runtime_list_file
import json
import logging
import os
import random
import re
import socket
import ssl
import threading
import time
from urllib.parse import parse_qs, quote, urlparse

import requests


logger = logging.getLogger(__name__)


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 跟随系统：留空或填 system。
# 固定代理示例：
#   "http://127.0.0.1:7890"
#   "socks5h://127.0.0.1:7891"
PROXY_POOL = [
    "http://127.0.0.1:10808",
]
# 静态池当前选中项。填写与 PROXY_POOL 中解析后的代理 URL 相同的值；
# 为空时普通非协议任务仍可轮询，严格纯协议注册会在多条静态代理时拒绝未选择状态。
PROXY_POOL_ACTIVE = ""

# ---- API 代理（Cliproxy 白名单模式）----
# 开启后，pick_proxy() 优先调用 API 获取临时代理；静态 PROXY_POOL 可以留空。
# Cliproxy white-model 的 txt 接口通常返回 host:port，无需用户名密码。
PROXY_API_ENABLED = False
PROXY_API_URL = ""
# 多地区 API 配置：每行 `名称|API地址`，例如 `US|https://...`。
PROXY_API_PROFILES = []
PROXY_API_ACTIVE = ""
PROXY_API_PROTOCOL = "socks5h"
PROXY_API_TIMEOUT = 20.0
PROXY_API_MAX_ATTEMPTS = 3
PROXY_API_RETRY_DELAY = 3.0
PROXY_API_VALIDATE = True
PROXY_API_VALIDATE_TIMEOUT = 12.0
# 除 SOCKS5 greeting 外再做一次 CONNECT 探测，提前淘汰“能握手但不能建立 TLS 隧道”的节点。
PROXY_API_VALIDATE_CONNECT = True
PROXY_API_VALIDATE_TARGET = "chatgpt.com:443"
# CONNECT 成功后继续完成 TLS 握手并校验证书/SNI，避免错误证书进入注册或套餐查询流程。
PROXY_API_VALIDATE_TLS = True
# 0=每个新会话都重新请求 API；大于 0 时在该秒数内复用同一 API 结果。
PROXY_API_CACHE_SECONDS = 0.0
# True=API 获取失败时中止任务，避免无意直连暴露真实出口；False=回退静态池/系统代理。
PROXY_API_FAIL_CLOSED = True

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；本地代理端口未监听时回退直连
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理，可填写本地代理地址。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = ""

# 套餐、AT 有效性与 Checkout 检测使用三套完全独立的静态代理池。
# WebUI 加入代理时会探测出口并保存为 `国家代码|静态代理`；ACTIVE 保存当前国家代码。
# 每个国家可保存多条代理，检测任务按随机洗牌轮转分配，不调用动态代理 API。
PLAN_CHECK_PROXY_PROFILES = []
PLAN_CHECK_PROXY_ACTIVE = ""
AT_VALIDITY_PROXY_PROFILES = []
AT_VALIDITY_PROXY_ACTIVE = ""
CHECKOUT_CHECK_PROXY_PROFILES = []
CHECKOUT_CHECK_PROXY_ACTIVE = ""

# GCash 资格检测专用代理池；需要 PH 出口代理。每行 `名称|代理或代理API`。
GC_CHECK_PROXY_PROFILES = []

# PayPal OAICS 提链专用代理池；格式支持 `名称|代理` 或直接代理，每行一条。
PAYPAL_OAICS_PROXY_PROFILES = []
PAYPAL_OAICS_PROXY_ACTIVE = ""
PAYPAL_OAICS_WORKERS = 5

# 套餐查询的 timeout 只限制单次网络请求；0 次数表示持续重试到取得明确结果。
# Codex Agent Token 仍由自身队列采用有限重试，不会继承套餐查询的无限循环。
PLAN_CHECK_TIMEOUT = 30.0
PLAN_CHECK_MAX_ATTEMPTS = 0
PLAN_CHECK_RETRY_DELAY = 1.5

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 10
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 0.4
PLAN_CHECK_JITTER = 0.3


_SYSTEM_PROXY_TOKENS = {"", "system", "sys", "auto", "follow", "follow_system"}
_SUPPORTED_PROXY_PROTOCOLS = {"http", "https", "socks5", "socks5h"}
_PROXY_API_LOCK = threading.Lock()
_PROXY_API_CACHE = {"key": None, "proxy": "", "expires_at": 0.0}
_PROXY_API_FLIGHTS = {}


def _clean_proxy_text(raw: str) -> str:
    """统一用户可能从供应商页面复制到的全角代理分隔符。"""
    return (raw or "").strip().strip("\"'").translate(str.maketrans({
        "：": ":",
        "／": "/",
        "＠": "@",
        "．": ".",
    }))


def _normalize_proxy_url(raw: str) -> str:
    value = _clean_proxy_text(raw)
    if not value:
        return ""
    # Windows 可能返回 127.0.0.1:7890
    if "://" not in value:
        # 多协议：http=127.0.0.1:7890;https=127.0.0.1:7890;socks=127.0.0.1:7891
        if "=" in value or ";" in value:
            parts = {}
            for chunk in re.split(r"[;\s]+", value):
                if not chunk or "=" not in chunk:
                    continue
                k, v = chunk.split("=", 1)
                parts[k.strip().lower()] = v.strip()
            for key in ("https", "http", "socks5", "socks", "all"):
                if parts.get(key):
                    host = parts[key]
                    if "://" in host:
                        return host
                    if key.startswith("socks"):
                        return f"socks5h://{host}"
                    return f"http://{host}"
            return ""
        return f"http://{value}"
    return value


def detect_system_proxy() -> str:
    """读取当前系统/环境代理。Clash 开启系统代理时一般可识别。"""
    for key in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        val = _normalize_proxy_url(os.environ.get(key, ""))
        if val:
            return val

    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if int(enable or 0) == 1:
                    server, _ = winreg.QueryValueEx(key, "ProxyServer")
                    return _normalize_proxy_url(str(server or ""))
        except Exception:
            pass
    return ""


def _expand_pool_entry(entry: str) -> str:
    text = _clean_proxy_text(str(entry or ""))
    if text.lower() in _SYSTEM_PROXY_TOKENS:
        return detect_system_proxy()
    # 常见粘性代理供应商直接给出 host:port:username:password。这个格式是
    # SOCKS5 凭据，不可简单拼成 http://host:port:user:password（urllib 会把
    # 后三段误当成非法端口，出口检测随后永久超时）。
    if "://" not in text and len(text.split(":", 3)) == 4:
        return _proxy_url_from_candidate(text, "socks5h")
    return _proxy_url_from_candidate(_normalize_proxy_url(text), "http")


def _proxy_url_from_candidate(candidate, protocol: str) -> str:
    """把 Cliproxy txt/JSON 中的一条代理转换成项目统一代理 URL。"""
    scheme = str(protocol or "socks5h").strip().lower()
    if scheme not in _SUPPORTED_PROXY_PROTOCOLS:
        raise ValueError(f"不支持的 API 代理协议: {scheme}")

    if isinstance(candidate, dict):
        host = str(candidate.get("host") or candidate.get("ip") or candidate.get("server") or "").strip()
        port = str(candidate.get("port") or "").strip()
        username = str(candidate.get("username") or candidate.get("user") or "").strip()
        password = str(candidate.get("password") or candidate.get("pass") or "").strip()
        if not host or not port:
            for key in ("proxy", "address", "value"):
                if candidate.get(key):
                    return _proxy_url_from_candidate(candidate[key], scheme)
            raise ValueError("API JSON 响应缺少 host/ip 或 port")
        auth = ""
        if username:
            auth = quote(username, safe="")
            if password:
                auth += ":" + quote(password, safe="")
            auth += "@"
        text = f"{scheme}://{auth}{host}:{port}"
    else:
        text = str(candidate or "").strip().strip("\"'")
        if not text:
            raise ValueError("API 返回了空代理")
        if "://" not in text:
            if "@" in text:
                text = f"{scheme}://{text}"
            else:
                parts = text.split(":", 3)
                if len(parts) == 2:
                    host, port = parts
                    text = f"{scheme}://{host}:{port}"
                elif len(parts) == 4:
                    host, port, username, password = parts
                    text = (
                        f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}"
                        f"@{host}:{port}"
                    )
                else:
                    raise ValueError(f"无法识别 API 代理格式: {text[:120]}")

    parsed = urlparse(text)
    if parsed.scheme.lower() not in _SUPPORTED_PROXY_PROTOCOLS:
        raise ValueError(f"API 返回了不支持的代理协议: {parsed.scheme or '-'}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口格式非法；请使用 host:port:username:password 或标准代理 URL") from exc
    if not parsed.hostname or not port:
        raise ValueError("API 代理缺少 host/port")
    if not (1 <= int(port) <= 65535):
        raise ValueError(f"API 代理端口非法: {port}")
    return text


def _proxy_candidates_from_payload(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "proxies", "proxy_list", "list", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [value]
            if isinstance(value, str) and value.strip():
                return [value]
        return [payload]
    text = str(payload or "").strip()
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_proxy_api_response(body: str, protocol: str | None = None) -> list[str]:
    """解析 Cliproxy API 的 txt 或常见 JSON 返回，得到标准代理 URL 列表。"""
    text = str(body or "").strip().lstrip("\ufeff")
    if not text:
        raise ValueError("代理 API 返回为空")
    payload = text
    if text[:1] in ("{", "["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"代理 API 返回了无效 JSON: {exc}") from exc

    proxies: list[str] = []
    errors: list[str] = []
    for candidate in _proxy_candidates_from_payload(payload):
        try:
            proxies.append(_proxy_url_from_candidate(candidate, protocol or PROXY_API_PROTOCOL))
        except Exception as exc:
            errors.append(str(exc))
    if not proxies:
        detail = "; ".join(errors[:3]) or text[:160]
        raise ValueError(f"代理 API 响应中没有可用代理: {detail}")
    return proxies


def mask_proxy_url(proxy_url: str) -> str:
    parsed = urlparse(str(proxy_url or "").strip())
    try:
        port = parsed.port
    except ValueError:
        return "已配置（格式错误）"
    if parsed.hostname and port:
        auth = "***:***@" if parsed.username or parsed.password else ""
        return f"{parsed.scheme}://{auth}{parsed.hostname}:{port}"
    return "已配置" if proxy_url else ""


_PROXY_REGION_LABELS = {
    "RAND": "随机地区",
    "US": "美国",
    "JP": "日本",
    "SG": "新加坡",
    "GB": "英国",
    "DE": "德国",
    "FR": "法国",
    "CA": "加拿大",
    "AU": "澳大利亚",
    "KR": "韩国",
    "HK": "中国香港",
    "TW": "中国台湾",
    "BR": "巴西",
    "IN": "印度",
    "NL": "荷兰",
    "IT": "意大利",
    "ES": "西班牙",
}


def infer_proxy_api_region(api_url: str, fallback: str = "") -> str:
    """从 Cliproxy URL 的 region 查询参数自动识别地区代码。"""
    try:
        query = parse_qs(urlparse(str(api_url or "").strip()).query)
        raw = next((values[0] for key, values in query.items() if key.lower() == "region" and values), "")
        code = str(raw or "").strip()
        if code:
            return "RAND" if code.lower() == "rand" else code.upper()
    except Exception:
        pass
    value = str(fallback or "").strip()
    return "RAND" if value.lower() == "rand" else value.upper()


def proxy_region_label(region_code: str) -> str:
    code = infer_proxy_api_region("", fallback=region_code)
    return _PROXY_REGION_LABELS.get(code, code or "未识别地区")


def parse_proxy_api_profiles(entries) -> list[tuple[str, str]]:
    """解析多行 API URL，并优先从 `region` 参数自动生成地区键；兼容旧命名格式。"""
    if isinstance(entries, str):
        entries = entries.splitlines()
    profiles: list[tuple[str, str]] = []
    used: dict[str, int] = {}
    for index, raw in enumerate(entries or [], 1):
        text = str(raw or "").strip()
        if not text or text.startswith("#"):
            continue
        if "|" in text and not text.lower().startswith(("http://", "https://")):
            legacy_name, url = text.split("|", 1)
        elif "=" in text and not text.lower().startswith(("http://", "https://")):
            legacy_name, url = text.split("=", 1)
        else:
            legacy_name, url = "", text
        url = url.strip()
        region = infer_proxy_api_region(url, fallback=legacy_name or f"API-{index}")
        if not region or not url:
            continue
        used[region] = used.get(region, 0) + 1
        key = region if used[region] == 1 else f"{region}-{used[region]}"
        profiles.append((key, url))
    return profiles


def get_active_proxy_api_url() -> str:
    """按当前选择的地区返回 API URL；无匹配时兼容旧的 PROXY_API_URL。"""
    profiles = parse_proxy_api_profiles(PROXY_API_PROFILES)
    active = str(PROXY_API_ACTIVE or "").strip()
    if active:
        normalized_active = "RAND" if active.lower() == "rand" else active.upper()
        for name, url in profiles:
            if active == name or normalized_active == name:
                return url
        if active.startswith(("http://", "https://")):
            return active
    if profiles:
        return profiles[0][1]
    return str(PROXY_API_URL or "").strip()


def validate_proxy_endpoint(proxy_url: str, timeout: float | None = None) -> None:
    """验证代理端口、SOCKS5 隧道及目标站点 TLS 证书。"""
    parsed = urlparse(str(proxy_url or "").strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口格式非法") from exc
    if not parsed.hostname or not port:
        raise ValueError("代理缺少 host/port")
    check_timeout = max(1.0, float(timeout or PROXY_API_VALIDATE_TIMEOUT or 8))
    with socket.create_connection((parsed.hostname, port), timeout=check_timeout) as sock:
        sock.settimeout(check_timeout)
        if parsed.scheme.lower() not in ("socks5", "socks5h"):
            return
        methods = b"\x00\x02" if parsed.username else b"\x00"
        sock.sendall(bytes((5, len(methods))) + methods)
        reply = sock.recv(2)
        if len(reply) != 2 or reply[0] != 5 or reply[1] == 255:
            preview = reply[:16].hex(" ") if reply else "空响应"
            raise RuntimeError(f"SOCKS5 握手失败: {preview}")
        if reply[1] == 2:
            username = (parsed.username or "").encode("utf-8")
            password = (parsed.password or "").encode("utf-8")
            if not username or len(username) > 255 or len(password) > 255:
                raise RuntimeError("SOCKS5 代理要求用户名密码，但凭据为空或过长")
            sock.sendall(bytes((1, len(username))) + username + bytes((len(password),)) + password)
            auth_reply = sock.recv(2)
            if len(auth_reply) != 2 or auth_reply[1] != 0:
                raise RuntimeError("SOCKS5 用户名密码认证失败")
        if bool(PROXY_API_VALIDATE_CONNECT):
            target = str(PROXY_API_VALIDATE_TARGET or "chatgpt.com:443").strip()
            target_host, sep, target_port = target.rpartition(":")
            if not sep or not target_host or not target_port.isdigit():
                raise RuntimeError(f"SOCKS5 检测目标非法: {target}")
            target_host_bytes = target_host.encode("idna")
            if len(target_host_bytes) > 255:
                raise RuntimeError("SOCKS5 检测目标域名过长")
            request = b"\x05\x01\x00\x03" + bytes((len(target_host_bytes),)) + target_host_bytes + int(target_port).to_bytes(2, "big")
            sock.sendall(request)
            connect_reply = sock.recv(4)
            if len(connect_reply) != 4 or connect_reply[0] != 5 or connect_reply[1] != 0:
                preview = connect_reply[:16].hex(" ") if connect_reply else "空响应"
                code = connect_reply[1] if len(connect_reply) > 1 else "-"
                raise RuntimeError(f"SOCKS5 CONNECT 失败: reply={code}, {preview}")
            atyp = connect_reply[3]
            if atyp == 1:
                tail_len = 4 + 2
            elif atyp == 3:
                size = sock.recv(1)
                if len(size) != 1:
                    raise RuntimeError("SOCKS5 CONNECT 返回地址长度为空")
                tail_len = int(size[0]) + 2
            elif atyp == 4:
                tail_len = 16 + 2
            else:
                raise RuntimeError(f"SOCKS5 CONNECT 返回地址类型非法: {atyp}")
            remaining = tail_len
            while remaining:
                chunk = sock.recv(remaining)
                if not chunk:
                    raise RuntimeError("SOCKS5 CONNECT 返回地址不完整")
                remaining -= len(chunk)
            if bool(PROXY_API_VALIDATE_TLS):
                if int(target_port) != 443:
                    raise RuntimeError("启用 TLS 校验时，SOCKS5 检测目标端口必须为 443")
                try:
                    context = ssl.create_default_context()
                    with context.wrap_socket(sock, server_hostname=target_host, do_handshake_on_connect=False) as tls_sock:
                        tls_sock.settimeout(check_timeout)
                        tls_sock.do_handshake()
                except ssl.SSLCertVerificationError as exc:
                    raise RuntimeError(f"SOCKS5 TLS 证书校验失败: {exc}") from exc
                except ssl.SSLError as exc:
                    raise RuntimeError(f"SOCKS5 TLS 握手失败: {exc}") from exc


def _request_proxy_from_api(
    url: str,
    protocol: str,
    request_timeout: float,
    attempts: int,
    retry_delay: float,
    validation_timeout: float,
    validate_endpoint: bool,
) -> str:
    last_error: Exception | None = None
    last_stage = "request"
    last_proxy = ""
    for attempt in range(1, attempts + 1):
        stage = "request"
        try:
            response = requests.get(
                url,
                headers={
                    "Accept": "text/plain, application/json",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                timeout=request_timeout,
            )
            response.raise_for_status()
            choices = parse_proxy_api_response(response.text, protocol=protocol)
            selected = random.choice(choices)
            last_proxy = selected
            if validate_endpoint:
                stage = "validation"
                validate_proxy_endpoint(selected, timeout=validation_timeout)
            logger.info("[代理API] 获取成功：%s", mask_proxy_url(selected))
            return selected
        except Exception as exc:
            last_error = exc
            last_stage = stage
            logger.warning(
                "[代理API] 第 %s/%s 次获取失败：%s: %s",
                attempt, attempts, type(exc).__name__, str(exc)[:240],
            )
            if attempt < attempts:
                wait_seconds = retry_delay * attempt
                logger.info("[代理API] 等待 %.1f 秒后进行第 %s/%s 次尝试", wait_seconds, attempt + 1, attempts)
                time.sleep(wait_seconds)
    if last_stage == "validation":
        raise RuntimeError(
            f"代理 API 已成功返回节点，但节点连接验证失败："
            f"proxy={mask_proxy_url(last_proxy)}, {type(last_error).__name__}: {last_error}"
        ) from last_error
    raise RuntimeError(f"代理 API 请求失败：{type(last_error).__name__}: {last_error}") from last_error


def fetch_proxy_from_api(
    *,
    api_url: str | None = None,
    protocol: str | None = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
    validation_timeout: float | None = None,
    validate: bool | None = None,
    force: bool = False,
) -> str:
    """调用代理 API；同配置的并发调用共享一次请求和验证结果。"""
    url = str(api_url if api_url is not None else get_active_proxy_api_url() or "").strip()
    if not url:
        raise RuntimeError("已启用 API 代理，但 PROXY_API_URL 为空")
    parsed_url = urlparse(url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        raise RuntimeError("PROXY_API_URL 必须是有效的 http/https 地址")

    selected_protocol = protocol or PROXY_API_PROTOCOL
    request_timeout = max(1.0, float(timeout if timeout is not None else PROXY_API_TIMEOUT or 15))
    attempts = max(1, int(max_attempts if max_attempts is not None else PROXY_API_MAX_ATTEMPTS or 1))
    wait_base = max(0.0, float(
        retry_delay if retry_delay is not None else PROXY_API_RETRY_DELAY or 0
    ))
    check_timeout = max(1.0, float(
        validation_timeout if validation_timeout is not None else PROXY_API_VALIDATE_TIMEOUT or 8
    ))
    validate_endpoint = bool(PROXY_API_VALIDATE) if validate is None else bool(validate)
    cache_seconds = max(0.0, float(PROXY_API_CACHE_SECONDS or 0))
    flight_key = (
        url,
        selected_protocol,
        request_timeout,
        attempts,
        wait_base,
        validate_endpoint,
        check_timeout,
        bool(PROXY_API_VALIDATE_CONNECT),
        bool(PROXY_API_VALIDATE_TLS),
        str(PROXY_API_VALIDATE_TARGET),
    )

    with _PROXY_API_LOCK:
        now = time.monotonic()
        if (
            not force
            and cache_seconds > 0
            and _PROXY_API_CACHE["key"] == flight_key
            and _PROXY_API_CACHE["proxy"]
            and now < _PROXY_API_CACHE["expires_at"]
        ):
            return str(_PROXY_API_CACHE["proxy"])
        flight = _PROXY_API_FLIGHTS.get(flight_key)
        if flight is None:
            flight = {"event": threading.Event(), "proxy": "", "error": None}
            _PROXY_API_FLIGHTS[flight_key] = flight
            leader = True
        else:
            leader = False

    if not leader:
        flight["event"].wait()
        if flight["error"] is not None:
            raise RuntimeError(str(flight["error"])) from flight["error"]
        return str(flight["proxy"])

    try:
        flight["proxy"] = _request_proxy_from_api(
            url,
            selected_protocol,
            request_timeout,
            attempts,
            wait_base,
            check_timeout,
            validate_endpoint,
        )
    except BaseException as exc:
        flight["error"] = exc
    finally:
        with _PROXY_API_LOCK:
            if flight["error"] is None:
                _PROXY_API_CACHE["key"] = flight_key
                _PROXY_API_CACHE["proxy"] = flight["proxy"]
                _PROXY_API_CACHE["expires_at"] = time.monotonic() + cache_seconds
            _PROXY_API_FLIGHTS.pop(flight_key, None)
            flight["event"].set()

    if flight["error"] is not None:
        raise flight["error"]
    return str(flight["proxy"])


def _pick_static_or_system_proxy(*, strict: bool = False, excluded: set[str] | None = None) -> str:
    entries = list(PROXY_POOL or [])
    if not entries:
        return detect_system_proxy()

    resolved = []
    for item in entries:
        proxy = _expand_pool_entry(item)
        if proxy:
            resolved.append(proxy)
    if resolved:
        active_raw = str(PROXY_POOL_ACTIVE or "").strip()
        active = _expand_pool_entry(active_raw) if active_raw else ""
        # PROXY_POOL_ACTIVE 只服务于需要固定出口的 strict/纯协议流程。
        # 浏览器注册是非 strict 流程：每个新窗口从完整粘性池随机抽一条，
        # 窗口创建后再由对应的浏览器客户端实例固定该代理。
        if strict and active:
            if active not in resolved:
                raise RuntimeError("PROXY_POOL_ACTIVE 不在当前 PROXY_POOL 中，已停止避免错用代理")
            return active
        if strict and len(resolved) > 1:
            raise RuntimeError("纯协议注册要求先选择 PROXY_POOL_ACTIVE，禁止在多条静态代理之间随机选择")
        excluded = {str(value or "").strip() for value in (excluded or set()) if str(value or "").strip()}
        available = [proxy for proxy in resolved if proxy not in excluded]
        return random.choice(available) if available else ""
    if strict:
        raise RuntimeError("严格代理出口未解析到静态代理或系统代理")
    return detect_system_proxy()


def pick_proxy(*, strict: bool = False, excluded: set[str] | None = None) -> str:
    """从代理池中随机抽取一个代理 URL。

    - 池为空，或条目为 system/auto：跟随当前系统代理
    - 系统代理也没有时返回空串（直连；TUN 模式通常也是这种）
    """
    if bool(PROXY_API_ENABLED):
        try:
            candidate = fetch_proxy_from_api()
            if candidate not in (excluded or set()):
                return candidate
        except Exception:
            if strict or bool(PROXY_API_FAIL_CLOSED):
                raise
            logger.exception("[代理API] 获取失败，按配置回退静态代理池/系统代理")
    return _pick_static_or_system_proxy(strict=strict, excluded=excluded)


def pick_local_proxy(*, strict: bool = False) -> str:
    """只读取本地静态池/系统代理，不调用远程代理 API。"""
    return _pick_static_or_system_proxy(strict=strict)


# 兼容入口；实际注册流程使用 pick_proxy()，API 代理保持按会话实时获取。
PROXY = ""

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PROXY_POOL_ACTIVE': 'str',
    'PROXY_API_ENABLED': 'bool',
    'PROXY_API_URL': 'str',
    'PROXY_API_PROFILES': 'list_str_multiline',
    'PROXY_API_ACTIVE': 'str',
    'PROXY_API_PROTOCOL': 'str',
    'PROXY_API_TIMEOUT': 'float',
    'PROXY_API_MAX_ATTEMPTS': 'int',
    'PROXY_API_RETRY_DELAY': 'float',
    'PROXY_API_VALIDATE': 'bool',
    'PROXY_API_VALIDATE_TIMEOUT': 'float',
    'PROXY_API_VALIDATE_CONNECT': 'bool',
    'PROXY_API_VALIDATE_TARGET': 'str',
    'PROXY_API_VALIDATE_TLS': 'bool',
    'PROXY_API_CACHE_SECONDS': 'float',
    'PROXY_API_FAIL_CLOSED': 'bool',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_PROXY_PROFILES': 'list_str_multiline',
    'PLAN_CHECK_PROXY_ACTIVE': 'str',
    'AT_VALIDITY_PROXY_PROFILES': 'list_str_multiline',
    'AT_VALIDITY_PROXY_ACTIVE': 'str',
    'CHECKOUT_CHECK_PROXY_PROFILES': 'list_str_multiline',
    'CHECKOUT_CHECK_PROXY_ACTIVE': 'str',
    'GC_CHECK_PROXY_PROFILES': 'list_str_multiline',
    'PAYPAL_OAICS_PROXY_PROFILES': 'list_str_multiline',
    'PAYPAL_OAICS_PROXY_ACTIVE': 'str',
    'PAYPAL_OAICS_WORKERS': 'int',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
for _runtime_key in (
    "PROXY_POOL",
    "PLAN_CHECK_PROXY_PROFILES",
    "AT_VALIDITY_PROXY_PROFILES",
    "CHECKOUT_CHECK_PROXY_PROFILES",
):
    _runtime_values = read_runtime_list_file(_runtime_key)
    if _runtime_values is not None:
        globals()[_runtime_key] = _runtime_values
PROXY = _pick_static_or_system_proxy()
