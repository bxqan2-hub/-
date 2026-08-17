# -*- coding: utf-8 -*-
"""日本 Plus 一个月免费试用资格探针。"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any

from config import proxy as proxy_cfg
from config import USER_AGENT
from config import browser as browser_cfg
from core import db
from core.chatgpt_plan import normalize_token
from core.session import BrowserSession

logger = logging.getLogger(__name__)

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
PROMO_CAMPAIGN = "plus-1-month-free"
_TRIAL_CHECK_CONCURRENCY = 2
_JP_PROXY_CACHE_SECONDS = 5 * 60
_JP_PROXY_CACHE_LOCK = threading.RLock()
_JP_PROXY_CACHE = {"proxy": "", "expires_at": 0.0}
_ACCOUNT_CHECK_LOCK = threading.RLock()
_ACCOUNT_CHECKS: dict[int, Future] = {}
_PROBE_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _eligibility_containers(payload: dict) -> list[dict]:
    containers: list[dict] = [payload]
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        return containers

    def append(value: Any) -> None:
        if isinstance(value, dict):
            containers.append(value)

    ordering = payload.get("account_ordering")
    if isinstance(ordering, list):
        for key in ordering:
            if isinstance(key, str):
                append(accounts.get(key))
    append(accounts.get("default"))
    for account in accounts.values():
        append(account)
    return containers


def _explicit_eligibility(payload: dict) -> tuple[bool, str] | None:
    stack: list[tuple[str, Any]] = [("", payload)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, list):
            for index, child in enumerate(value):
                stack.append((f"{path}.{index}" if path else str(index), child))
            continue
        if not isinstance(value, dict):
            continue
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in {"one_click_trial_eligible", "plus_trial_eligible"} and isinstance(child, bool):
                return child, child_path
            if isinstance(child, (dict, list)):
                stack.append((child_path, child))
    return None


def classify_jp_trial_eligibility(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("账号资格接口返回了无效数据")
    for container in _eligibility_containers(payload):
        campaigns = container.get("eligible_promo_campaigns")
        if not isinstance(campaigns, dict) or "plus" not in campaigns:
            continue
        plus = campaigns.get("plus")
        eligible = bool(
            plus is True
            or (isinstance(plus, str) and plus.strip() == PROMO_CAMPAIGN)
            or (isinstance(plus, dict) and bool(plus))
        )
        return {
            "eligible": eligible,
            "evidence": "eligible_promo_campaigns.plus",
        }
    explicit = _explicit_eligibility(payload)
    if explicit is not None:
        return {"eligible": explicit[0], "evidence": explicit[1]}
    return {
        "eligible": False,
        "evidence": "eligible_promo_campaigns.plus absent",
    }


def _registration_proxy_candidates() -> list[str]:
    candidates: list[str] = []
    for raw in list(getattr(proxy_cfg, "PROXY_POOL", []) or []):
        proxy = proxy_cfg._expand_pool_entry(raw)
        if proxy and proxy not in candidates:
            candidates.append(proxy)
    if bool(getattr(proxy_cfg, "PROXY_API_ENABLED", False)):
        profiles = proxy_cfg.parse_proxy_api_profiles(getattr(proxy_cfg, "PROXY_API_PROFILES", []) or [])
        api_urls = [url for _name, url in profiles]
        if not api_urls:
            active_url = proxy_cfg.get_active_proxy_api_url()
            if active_url:
                api_urls.append(active_url)
        for api_url in api_urls:
            try:
                proxy = proxy_cfg.fetch_proxy_from_api(api_url=api_url, force=True)
                if proxy and proxy not in candidates:
                    candidates.append(proxy)
            except Exception as exc:
                logger.debug("JP 资格检测获取注册 API 代理失败: %s", exc)
    return candidates


def _inspect_proxy_exit(proxy: str) -> dict:
    env = None
    try:
        env = BrowserSession(proxy=proxy, detect_exit_geo=False)
        timeout = max(1.0, float(getattr(browser_cfg, "IP_GEO_TIMEOUT", 6) or 6))
        headers = {"user-agent": USER_AGENT, "accept": "application/json"}
        for endpoint in list(getattr(browser_cfg, "IP_GEO_ENDPOINTS", []) or []):
            try:
                response = env.session.get(endpoint, headers=headers, timeout=timeout)
                if int(response.status_code) != 200:
                    continue
                geo = BrowserSession._normalize_geo_response(response.json())
                country_code = str(geo.get("country") or "").strip().upper()
                if country_code:
                    return {
                        "ip": str(geo.get("ip") or "").strip(),
                        "country_code": country_code,
                    }
            except Exception:
                continue
        return {}
    finally:
        if env is not None:
            try:
                env.session.close()
            except Exception:
                pass


def resolve_japanese_trial_proxy() -> dict:
    with _JP_PROXY_CACHE_LOCK:
        if _JP_PROXY_CACHE["proxy"] and _JP_PROXY_CACHE["expires_at"] > time.monotonic():
            return {"proxy": _JP_PROXY_CACHE["proxy"], "proxy_source": "registration_proxy_pool_cache"}

    candidates = _registration_proxy_candidates()
    if not candidates:
        raise ValueError("代理池为空，请先添加日本代理")
    for proxy in candidates:
        try:
            inspection = _inspect_proxy_exit(proxy)
        except Exception:
            continue
        if inspection.get("country_code") != "JP":
            continue
        with _JP_PROXY_CACHE_LOCK:
            _JP_PROXY_CACHE.update({
                "proxy": proxy,
                "expires_at": time.monotonic() + _JP_PROXY_CACHE_SECONDS,
            })
        return {
            "proxy": proxy,
            "proxy_source": "registration_proxy_pool",
            "exit_ip": inspection.get("ip") or None,
            "exit_country": "JP",
        }
    raise ValueError("代理池中没有检测到 JP 出口，请先添加日本代理")


def _headers(env: BrowserSession, token: str) -> dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": "ja-JP,ja;q=0.9,en;q=0.8",
        "authorization": f"Bearer {token}",
        "oai-device-id": env.device_id,
        "oai-language": "ja-JP",
        "origin": "https://chatgpt.com",
        "referer": f"https://chatgpt.com/?promo_campaign={PROMO_CAMPAIGN}#pricing",
        "x-openai-target-path": ACCOUNTS_CHECK_PATH,
        "x-openai-target-route": ACCOUNTS_CHECK_PATH,
        "user-agent": _PROBE_USER_AGENT,
    }


def check_jp_trial_eligibility(access_token: str, *, proxy: str) -> dict:
    token = normalize_token(access_token)
    checked_at = _now()
    if not token:
        return {"ok": False, "checked_at": checked_at, "error": "账号缺少 AT", "http_status": 409}
    proxy_url = str(proxy or "").strip()
    if not proxy_url:
        return {"ok": False, "checked_at": checked_at, "error": "未配置 JP 资格检测代理", "http_status": 503}
    env = None
    try:
        env = BrowserSession(proxy=proxy_url, detect_exit_geo=False)
        url = f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}"
        response = env.session.get(
            url,
            headers=_headers(env, token),
            allow_redirects=False,
            timeout=45.0,
        )
        http_status = int(response.status_code)
        text = response.text or ""
        if http_status != 200:
            return {
                "ok": False,
                "checked_at": _now(),
                "http_status": http_status,
                "error": f"账号资格检测失败 HTTP {http_status}",
                "rate_limited": http_status == 429,
            }
        try:
            payload: Any = json.loads(text)
        except Exception:
            return {
                "ok": False,
                "checked_at": _now(),
                "http_status": 502,
                "error": "账号资格接口返回了无效 JSON",
            }
        classified = classify_jp_trial_eligibility(payload)
        return {
            "ok": True,
            "checked_at": _now(),
            "http_status": http_status,
            **classified,
        }
    except Exception as exc:
        logger.debug("JP 资格检测失败: %s: %s", type(exc).__name__, exc, exc_info=True)
        return {
            "ok": False,
            "checked_at": _now(),
            "error": str(exc)[:240] or "日本免费试用资格检测失败",
        }
    finally:
        if env is not None:
            try:
                env.session.close()
            except Exception:
                pass


def _result_item(account: dict, result: dict) -> dict:
    return {
        "id": int(account.get("id") or 0),
        "trial_status": result.get("status"),
        "trial_eligible": result.get("eligible"),
        "trial_evidence": result.get("evidence") or "",
        "trial_error": result.get("error") or "",
        "trial_checked_at": result.get("checked_at") or "",
    }


def _run_account_jp_trial(account: dict, *, proxy: str) -> dict:
    result = check_jp_trial_eligibility(account.get("access_token") or "", proxy=proxy)
    if result.get("rate_limited"):
        return _result_item(account, {
            "status": "rate_limited",
            "eligible": None,
            "error": "日本免费试用资格检测触发 HTTP 429 限流，请稍后重试",
        })
    if result.get("ok"):
        db.update_account_jp_trial(int(account["id"]), result)
        return _result_item(account, {
            "status": "eligible" if result.get("eligible") else "ineligible",
            **result,
        })
    db.update_account_jp_trial(int(account["id"]), result)
    return _result_item(account, {"status": "failed", **result})


def check_account_jp_trial(account: dict, *, proxy: str) -> dict:
    account_id = int(account.get("id") or 0)
    with _ACCOUNT_CHECK_LOCK:
        flight = _ACCOUNT_CHECKS.get(account_id)
        if flight is None:
            flight = Future()
            _ACCOUNT_CHECKS[account_id] = flight
            leader = True
        else:
            leader = False
    if not leader:
        return dict(flight.result())
    try:
        item = _run_account_jp_trial(account, proxy=proxy)
        flight.set_result(item)
        return item
    except BaseException as exc:
        flight.set_exception(exc)
        raise
    finally:
        with _ACCOUNT_CHECK_LOCK:
            if _ACCOUNT_CHECKS.get(account_id) is flight:
                _ACCOUNT_CHECKS.pop(account_id, None)


def check_accounts_jp_trial(accounts: list[dict]) -> dict:
    route = resolve_japanese_trial_proxy()
    selected = list(accounts)
    items: list[dict | None] = [None] * len(selected)
    rate_limited = False

    def check_one(index: int) -> dict:
        return check_account_jp_trial(selected[index], proxy=route["proxy"])

    if selected:
        items[0] = check_one(0)
        rate_limited = items[0].get("trial_status") == "rate_limited"
    if not rate_limited and len(selected) > 1:
        cursor = 1
        cursor_lock = threading.Lock()
        rate_limit_event = threading.Event()

        def worker() -> None:
            nonlocal cursor
            while not rate_limit_event.is_set():
                with cursor_lock:
                    if rate_limit_event.is_set() or cursor >= len(selected):
                        return
                    index = cursor
                    cursor += 1
                items[index] = check_one(index)
                if items[index].get("trial_status") == "rate_limited":
                    rate_limit_event.set()

        with ThreadPoolExecutor(max_workers=min(_TRIAL_CHECK_CONCURRENCY, len(selected) - 1)) as executor:
            workers = [
                executor.submit(worker)
                for _ in range(min(_TRIAL_CHECK_CONCURRENCY, len(selected) - 1))
            ]
            for future in workers:
                future.result()
    for index, item in enumerate(items):
        if item is not None:
            continue
        items[index] = _result_item(selected[index], {
            "status": "skipped",
            "eligible": None,
            "error": "本批次触发限流，未继续检测",
        })
    resolved_items = [item for item in items if item is not None]
    return {
        "requested": len(selected),
        "checked": sum(item["trial_status"] in {"eligible", "ineligible"} for item in resolved_items),
        "eligible": sum(item["trial_status"] == "eligible" for item in resolved_items),
        "ineligible": sum(item["trial_status"] == "ineligible" for item in resolved_items),
        "failed": sum(item["trial_status"] == "failed" for item in resolved_items),
        "rate_limited": sum(item["trial_status"] == "rate_limited" for item in resolved_items),
        "skipped": sum(item["trial_status"] == "skipped" for item in resolved_items),
        "items": resolved_items,
    }
