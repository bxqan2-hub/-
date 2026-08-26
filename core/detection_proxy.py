# -*- coding: utf-8 -*-
"""套餐、AT 有效性与 Checkout 检测使用的独立按国家静态代理池。"""
from __future__ import annotations

import calendar
import random
import re
import threading
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from config import proxy as proxy_cfg


_REGION_STANDARD_OFFSETS = {
    "DE": 60,
    "JP": 540,
    "US": -300,
    "BR": -180,
    "GB": 0,
    "FR": 60,
    "KR": 540,
    "SG": 480,
}

_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")
_PROXY_REGION_TAG_RE = re.compile(r"(?:^|[-_])region[-_]([A-Z]{2})(?=$|[-_])", re.IGNORECASE)
DETECTION_PROXY_POOL_MAX_ENTRIES = 50_000
DETECTION_PROXY_IMPORT_MAX_ENTRIES = 10_000
_POOL_ROTATION_LOCK = threading.Lock()
_POOL_ROTATIONS: dict[tuple[str, str], dict[str, object]] = {}


def _last_sunday(year: int, month: int) -> int:
    return max(
        day for day in range(1, calendar.monthrange(year, month)[1] + 1)
        if datetime(year, month, day).weekday() == 6
    )


def _region_offset_minutes(region: str) -> int | None:
    standard = _REGION_STANDARD_OFFSETS.get(region)
    if standard is None:
        return None
    if region not in {"DE", "FR", "GB"}:
        return standard
    now = datetime.now(timezone.utc)
    dst_start = datetime(now.year, 3, _last_sunday(now.year, 3), 1, tzinfo=timezone.utc)
    dst_end = datetime(now.year, 10, _last_sunday(now.year, 10), 1, tzinfo=timezone.utc)
    return standard + (60 if dst_start <= now < dst_end else 0)


def parse_detection_proxy_pool(
    value,
    *,
    limit: int = DETECTION_PROXY_POOL_MAX_ENTRIES,
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        entries = value.splitlines()
    elif isinstance(value, list):
        entries = value
    else:
        raise ValueError("检测代理池必须是文本或数组")
    specs: list[str] = []
    for raw in entries:
        text = str(raw or "").strip()
        if not text or text.startswith("#"):
            continue
        if len(text) > 4096:
            raise ValueError("检测代理池单行内容过长")
        specs.append(text)
    if len(specs) > limit:
        raise ValueError(f"检测代理池最多 {limit} 行")
    return specs


def detection_proxy_profiles(value) -> list[dict[str, str]]:
    """返回已标注国家的静态代理；动态 API 与未归类旧格式不会进入检测池。"""
    profiles: list[dict[str, str]] = []
    for spec in parse_detection_proxy_pool(value):
        country, raw_proxy = _split_label(spec)
        country = country.upper()
        if not _COUNTRY_CODE_RE.fullmatch(country) or not raw_proxy or _is_api_spec(spec):
            continue
        profiles.append({
            "key": country,
            "label": proxy_cfg.proxy_region_label(country),
            "country": country,
            "spec": f"{country}|{raw_proxy}",
        })
    return profiles


def detection_proxy_country_groups(value) -> list[dict[str, object]]:
    """把静态代理按两位国家代码归类，供设置页下拉框和服务端选择共用。"""
    grouped: dict[str, list[str]] = {}
    for profile in detection_proxy_profiles(value):
        country = profile["country"]
        grouped.setdefault(country, []).append(profile["spec"])
    return [
        {
            "country": country,
            "label": proxy_cfg.proxy_region_label(country),
            "count": len(specs),
            "specs": specs,
        }
        for country, specs in grouped.items()
    ]


def _randomized_rotation_choice(purpose: str, country: str, specs: list[str]) -> str:
    """洗牌后逐条分配，确保一轮内并发任务不会反复挤到同一条代理。"""
    signature = tuple(specs)
    key = (purpose, country)
    with _POOL_ROTATION_LOCK:
        state = _POOL_ROTATIONS.get(key)
        if not state or state.get("signature") != signature or not state.get("remaining"):
            remaining = list(specs)
            random.shuffle(remaining)
            previous = str((state or {}).get("last") or "")
            if previous and len(remaining) > 1 and remaining[-1] == previous:
                remaining[0], remaining[-1] = remaining[-1], remaining[0]
            state = {"signature": signature, "remaining": remaining, "last": previous}
            _POOL_ROTATIONS[key] = state
        selected = state["remaining"].pop()
        state["last"] = selected
        return str(selected)


def configured_detection_proxy_spec(purpose: str) -> str | None:
    normalized = str(purpose or "plan").strip().lower()
    if normalized == "checkout":
        entries = getattr(proxy_cfg, "CHECKOUT_CHECK_PROXY_PROFILES", []) or []
        active = str(getattr(proxy_cfg, "CHECKOUT_CHECK_PROXY_ACTIVE", "") or "").strip()
    elif normalized in {"at", "at-validity", "at_validity"}:
        normalized = "at"
        entries = getattr(proxy_cfg, "AT_VALIDITY_PROXY_PROFILES", []) or []
        active = str(getattr(proxy_cfg, "AT_VALIDITY_PROXY_ACTIVE", "") or "").strip()
    else:
        normalized = "plan"
        entries = getattr(proxy_cfg, "PLAN_CHECK_PROXY_PROFILES", []) or []
        active = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_ACTIVE", "") or "").strip()
    groups = detection_proxy_country_groups(entries)
    if not groups:
        if parse_detection_proxy_pool(entries):
            raise ValueError("当前检测配置只有动态 API 或未归类线路，请先通过“加入代理池”导入静态代理")
        return None
    active_country = active.upper()
    selected = next((item for item in groups if item["country"] == active_country), None) or groups[0]
    return _randomized_rotation_choice(
        normalized,
        str(selected["country"]),
        list(selected["specs"]),
    )


def qualification_proxy_specs(country: str) -> list[str]:
    """Return every static qualification proxy for one exact country."""
    requested = str(country or "").strip().upper()
    if not _COUNTRY_CODE_RE.fullmatch(requested):
        raise ValueError("资格检测国家必须是两位国家代码")
    entries = getattr(proxy_cfg, "QUALIFICATION_CHECK_PROXY_PROFILES", []) or []
    for group in detection_proxy_country_groups(entries):
        if str(group["country"]) == requested:
            return list(group["specs"])
    return []


def _split_label(spec: str) -> tuple[str, str]:
    text = str(spec or "").strip()
    if "|" in text and not text.lower().startswith(("http://", "https://", "socks5://", "socks5h://")):
        label, value = text.split("|", 1)
        return label.strip(), value.strip()
    return "", text


def _is_api_spec(spec: str) -> bool:
    _label, value = _split_label(spec)
    lowered = value.lower()
    if lowered.startswith("api:"):
        return True
    if not lowered.startswith(("http://", "https://")):
        return False
    parsed = urlparse(value)
    if parsed.username or parsed.password:
        return False
    if parsed.query or parsed.fragment:
        return True
    if parsed.path not in {"", "/"}:
        return True
    return parsed.port is None


def is_detection_proxy_api(spec: str | None) -> bool:
    if spec is None:
        return False
    return _is_api_spec(str(spec or "").strip())


def _api_url(spec: str) -> str:
    _label, value = _split_label(spec)
    if value.lower().startswith("api:"):
        value = value[4:].strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("代理 API 必须是有效的 http/https 地址")
    return value


def resolve_detection_proxy(
    spec: str | None,
    *,
    api_timeout: float | None = None,
    api_max_attempts: int | None = None,
    validation_timeout: float | None = None,
    validate: bool | None = None,
) -> str | None:
    if spec is None:
        return None
    text = str(spec or "").strip()
    if not text:
        return ""
    if _is_api_spec(text):
        fetch_kwargs = {
            "api_url": _api_url(text),
            "timeout": api_timeout,
            "max_attempts": api_max_attempts,
            "validation_timeout": validation_timeout,
            "force": True,
        }
        if validate is not None:
            fetch_kwargs["validate"] = validate
        return proxy_cfg.fetch_proxy_from_api(
            **fetch_kwargs,
        )
    _label, value = _split_label(text)
    proxy = proxy_cfg._expand_pool_entry(value)
    if not proxy:
        raise ValueError("检测代理为空或无法识别")
    return proxy


def resolve_static_detection_proxy(spec: str | None) -> str | None:
    """解析静态检测代理，并明确拒绝套餐/AT/Checkout 再调用动态代理 API。"""
    if spec is not None and _is_api_spec(str(spec or "").strip()):
        raise ValueError("套餐、AT 与 Checkout 检测只支持静态代理池，不再调用动态代理 API")
    return resolve_detection_proxy(spec)


def infer_static_proxy_country(spec: str) -> str:
    """从代理用户名中的 ``region-XX`` 标签读取供应商指定的国家代码。"""
    proxy = resolve_static_detection_proxy(spec)
    if not proxy:
        return ""
    parsed = urlparse(proxy)
    username = unquote(str(parsed.username or ""))
    match = _PROXY_REGION_TAG_RE.search(username)
    if not match:
        return ""
    country = match.group(1).upper()
    return country if _COUNTRY_CODE_RE.fullmatch(country) else ""


def infer_detection_proxy_country(spec: str | None) -> str:
    """读取检测代理的国家标签；无标签时再尝试静态代理用户名。"""
    if spec is None:
        return ""
    label, _value = _split_label(str(spec))
    country = str(label or "").strip().upper()
    if _COUNTRY_CODE_RE.fullmatch(country):
        return country
    return infer_static_proxy_country(str(spec))


def inspect_static_proxy(spec: str, *, timeout: float = 12.0) -> dict[str, str]:
    """优先读取代理自带地区；没有 ``region-XX`` 时才探测实际出口。"""
    proxy = resolve_static_detection_proxy(spec)
    if not proxy:
        raise ValueError("静态代理为空或无法识别")
    tagged_country = infer_static_proxy_country(proxy)
    if tagged_country:
        return {
            "country": tagged_country,
            "country_label": proxy_cfg.proxy_region_label(tagged_country),
            "country_source": "proxy_region_tag",
            "proxy": proxy,
            "masked_proxy": proxy_cfg.mask_proxy_url(proxy),
            "exit_ip": "",
            "region": "",
            "city": "",
        }

    from config import browser as browser_cfg
    from core.session import BrowserSession
    from curl_cffi.requests import Session as CurlSession

    request_timeout = max(2.0, min(float(timeout or 12.0), 30.0))
    endpoints = list(getattr(browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
    if not endpoints:
        endpoints = ["https://ipinfo.io/json"]
    session = CurlSession()
    session.proxies = {"http": proxy, "https": proxy}
    errors: list[str] = []
    try:
        for endpoint in endpoints:
            try:
                response = session.get(
                    endpoint,
                    headers={"accept": "application/json"},
                    timeout=request_timeout,
                )
                if int(response.status_code) != 200:
                    errors.append(f"HTTP {response.status_code}")
                    continue
                geo = BrowserSession._normalize_geo_response(response.json())
                country = str(geo.get("country") or "").strip().upper()
                if not _COUNTRY_CODE_RE.fullmatch(country):
                    errors.append("出口国家无法识别")
                    continue
                return {
                    "country": country,
                    "country_label": proxy_cfg.proxy_region_label(country),
                    "country_source": "exit_geo",
                    "proxy": proxy,
                    "masked_proxy": proxy_cfg.mask_proxy_url(proxy),
                    "exit_ip": str(geo.get("ip") or "").strip(),
                    "region": str(geo.get("region") or "").strip(),
                    "city": str(geo.get("city") or "").strip(),
                }
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
        detail = errors[-1] if errors else "所有地理检测接口均未返回国家"
        raise RuntimeError(f"静态代理出口检测失败：{detail}")
    finally:
        try:
            session.close()
        except Exception:
            pass


def infer_timezone_offset_min(spec: str | None, fallback: str = "-") -> str:
    label, value = _split_label(str(spec or ""))
    region = proxy_cfg.infer_proxy_api_region(_api_url(value) if _is_api_spec(value) else "", fallback=label)
    offset_minutes = _region_offset_minutes(region)
    if offset_minutes is None:
        fallback_text = str(fallback or "").strip()
        try:
            return str(int(fallback_text))
        except (TypeError, ValueError):
            offset = datetime.now().astimezone().utcoffset()
            if offset is None:
                return "0"
            offset_minutes = int(offset.total_seconds() // 60)
    return str(-offset_minutes)


def pool_spec_for_index(specs: list[str], index: int) -> str | None:
    if not specs:
        return None
    return specs[index % len(specs)]


__all__ = [
    "infer_timezone_offset_min",
    "infer_detection_proxy_country",
    "configured_detection_proxy_spec",
    "detection_proxy_country_groups",
    "detection_proxy_profiles",
    "infer_static_proxy_country",
    "inspect_static_proxy",
    "is_detection_proxy_api",
    "parse_detection_proxy_pool",
    "pool_spec_for_index",
    "qualification_proxy_specs",
    "resolve_detection_proxy",
    "resolve_static_detection_proxy",
]
