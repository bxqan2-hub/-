# -*- coding: utf-8 -*-
"""账号检测页面提交的代理池解析与按需 API 取代理。"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone
from urllib.parse import urlparse

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


def parse_detection_proxy_pool(value, *, limit: int = 500) -> list[str]:
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
    profiles: list[dict[str, str]] = []
    used: dict[str, int] = {}
    for index, spec in enumerate(parse_detection_proxy_pool(value), 1):
        label, _value = _split_label(spec)
        if not label and _is_api_spec(spec):
            label = proxy_cfg.infer_proxy_api_region(_api_url(spec), fallback=f"线路 {index}")
        label = label or f"线路 {index}"
        used[label] = used.get(label, 0) + 1
        key = label if used[label] == 1 else f"{label}-{used[label]}"
        profiles.append({"key": key, "label": label, "spec": spec})
    return profiles


def configured_detection_proxy_spec(purpose: str) -> str | None:
    normalized = str(purpose or "plan").strip().lower()
    if normalized == "checkout":
        entries = getattr(proxy_cfg, "CHECKOUT_CHECK_PROXY_PROFILES", []) or []
        active = str(getattr(proxy_cfg, "CHECKOUT_CHECK_PROXY_ACTIVE", "") or "").strip()
    else:
        entries = getattr(proxy_cfg, "PLAN_CHECK_PROXY_PROFILES", []) or []
        active = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_ACTIVE", "") or "").strip()
    profiles = detection_proxy_profiles(entries)
    if not profiles:
        return None
    selected = next((item for item in profiles if item["key"] == active or item["label"] == active), None)
    return str((selected or profiles[0])["spec"])


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
    "configured_detection_proxy_spec",
    "detection_proxy_profiles",
    "is_detection_proxy_api",
    "parse_detection_proxy_pool",
    "pool_spec_for_index",
    "resolve_detection_proxy",
]
