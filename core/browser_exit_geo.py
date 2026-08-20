# -*- coding: utf-8 -*-
"""Probe the actual egress used by an active registration browser context."""
from __future__ import annotations

import ipaddress
import logging
import math
import time

logger = logging.getLogger(__name__)


def normalize_browser_exit_geo(data: dict | None) -> dict:
    """Normalize the JSON shapes returned by the configured IP geo services."""
    if not isinstance(data, dict):
        return {}
    ip_value = str(data.get("ip") or data.get("query") or "").strip()
    try:
        ip_value = str(ipaddress.ip_address(ip_value))
    except ValueError:
        return {}

    timezone = data.get("timezone")
    if isinstance(timezone, dict):
        timezone = timezone.get("id") or timezone.get("name")
    connection = data.get("connection") if isinstance(data.get("connection"), dict) else {}
    country = str(
        data.get("country_code")
        or data.get("countryCode")
        or data.get("country")
        or ""
    ).strip()
    if len(country) in (2, 3):
        country = country.upper()
    return {
        "ip": ip_value,
        "country": country,
        "region": data.get("region") or data.get("regionName"),
        "city": data.get("city"),
        "timezone": timezone or "",
        "org": data.get("org") or data.get("isp") or connection.get("org"),
    }


def _probe_settings() -> tuple[list[str], float]:
    try:
        from config import browser as browser_config

        endpoints = [
            str(value).strip()
            for value in (getattr(browser_config, "IP_GEO_ENDPOINTS", []) or [])
            if str(value).strip()
        ]
        timeout = max(1.0, float(getattr(browser_config, "IP_GEO_TIMEOUT", 6) or 6))
        return endpoints, timeout
    except Exception:
        return [], 6.0


def _log_detected(label: str, geo: dict) -> None:
    logger.info(
        "[%s] 浏览器代理出口 IP: %s country=%s",
        label,
        geo.get("ip") or "?",
        geo.get("country") or "?",
    )


def probe_proxy_exit_geo(
    proxy_url: str,
    *,
    label: str,
    attempts: int = 1,
    retry_delay: float = 0.0,
    stop_check=None,
) -> dict:
    """Probe an explicit proxy before a browser window is opened.

    Attempts are always bounded to 1..10. Historical ``attempts <= 0`` values
    now mean one attempt, so a malformed or dead proxy cannot occupy a worker
    forever. The optional ``stop_check`` callback is invoked between operations.
    """
    proxy_url = str(proxy_url or "").strip()
    endpoints, timeout = _probe_settings()
    if not proxy_url or not endpoints:
        return {}

    from curl_cffi.requests import Session

    configured_attempts = int(attempts if attempts is not None else 1)
    max_attempts = max(1, min(10, configured_attempts or 1))
    delay = max(0.0, float(retry_delay or 0.0))
    attempt = 0
    while True:
        attempt += 1
        if callable(stop_check):
            stop_check()
        session = Session(impersonate="chrome")
        session.proxies = {"http": proxy_url, "https": proxy_url}
        try:
            for endpoint in endpoints:
                if callable(stop_check):
                    stop_check()
                try:
                    response = session.get(
                        endpoint,
                        headers={"Accept": "application/json"},
                        timeout=timeout,
                    )
                    if int(response.status_code) != 200:
                        continue
                    geo = normalize_browser_exit_geo(response.json())
                    if geo.get("ip"):
                        _log_detected(label, geo)
                        return geo
                except Exception as exc:
                    logger.debug(
                        "[%s] 窗口打开前出口 IP 探测失败 endpoint=%s attempt=%s/%s: %s: %s",
                        label, endpoint, attempt, max_attempts, type(exc).__name__, exc,
                    )
        finally:
            try:
                session.close()
            except Exception:
                pass
        if attempt >= max_attempts:
            break
        if delay:
            time.sleep(min(5.0, delay * attempt))
    logger.warning("[%s] 窗口打开前未能读取代理出口 IP", label)
    return {}


def probe_playwright_context_exit_geo(context, *, label: str) -> dict:
    """Use a temporary page in the active Playwright context to probe egress."""
    endpoints, timeout = _probe_settings()
    if context is None or not endpoints:
        return {}

    probe_page = None
    try:
        probe_page = context.new_page()
        timeout_ms = int(math.ceil(timeout * 1000))
        probe_page.set_default_timeout(timeout_ms)
        probe_page.set_default_navigation_timeout(timeout_ms)
        for endpoint in endpoints:
            try:
                response = probe_page.goto(endpoint, wait_until="domcontentloaded", timeout=timeout_ms)
                status = getattr(response, "status", None) if response is not None else None
                if status is not None and not (200 <= int(status) < 300):
                    continue
                data = probe_page.evaluate(
                    """() => {
                      const text = document.body?.innerText || document.documentElement?.innerText || '';
                      try { return JSON.parse(text); } catch (_) { return null; }
                    }"""
                )
                geo = normalize_browser_exit_geo(data)
                if geo.get("ip"):
                    _log_detected(label, geo)
                    return geo
            except Exception as exc:
                logger.debug(
                    "[%s] 浏览器出口 IP 探测失败，继续下一个服务: %s: %s",
                    label,
                    type(exc).__name__,
                    exc,
                )
        logger.warning("[%s] 未能从当前注册浏览器上下文识别出口 IP", label)
        return {}
    except Exception as exc:
        logger.warning(
            "[%s] 无法创建浏览器出口探测临时页，账号将留空: %s: %s",
            label,
            type(exc).__name__,
            exc,
        )
        return {}
    finally:
        if probe_page is not None:
            try:
                probe_page.close()
            except Exception:
                pass


def probe_selenium_driver_exit_geo(
    driver,
    *,
    label: str,
    restore_page_load_timeout: float | int | None = None,
    restore_script_timeout: float | int | None = None,
    attempts: int = 1,
    retry_delay: float = 0.0,
    stop_check=None,
) -> dict:
    """Use a temporary tab in the active Selenium browser to probe egress."""
    endpoints, timeout = _probe_settings()
    if driver is None or not endpoints:
        return {}

    original_handle = None
    probe_opened = False
    try:
        original_handle = driver.current_window_handle
        driver.switch_to.new_window("tab")
        probe_opened = True
        timeout_seconds = max(1, int(math.ceil(timeout)))
        driver.set_page_load_timeout(timeout_seconds)
        # Selenium 的 async callback 还需要极短的收尾空间；所有 IP 服务在浏览器
        # 内并行请求，因此一次尝试只消耗一个 timeout，而不是 endpoints * timeout。
        driver.set_script_timeout(timeout_seconds + 1)
        configured_attempts = int(attempts if attempts is not None else 1)
        max_attempts = max(1, min(10, configured_attempts or 1))
        delay = max(0.0, float(retry_delay or 0.0))
        attempt = 0
        while True:
            attempt += 1
            if callable(stop_check):
                stop_check()
            if callable(stop_check):
                stop_check()
            try:
                # Give the CORS-based parallel fast path at most two seconds.
                # A direct navigation fallback below then gets the normal page
                # timeout, keeping the full recovery bounded to about six seconds.
                parallel_timeout_ms = max(250, min(2000, int(math.ceil(timeout * 1000))))
                data = driver.execute_async_script(
                    """
                    const endpoints = Array.isArray(arguments[0]) ? arguments[0] : [];
                    const timeoutMs = Math.max(250, Number(arguments[1]) || 1000);
                    const done = arguments[arguments.length - 1];
                    const request = async url => {
                      const controller = new AbortController();
                      const timer = setTimeout(() => controller.abort(), timeoutMs);
                      try {
                        const response = await fetch(url, {
                          method: 'GET',
                          headers: {Accept: 'application/json'},
                          cache: 'no-store',
                          credentials: 'omit',
                          signal: controller.signal,
                        });
                        if (!response.ok) throw new Error(`HTTP ${response.status}`);
                        const payload = await response.json();
                        const ip = String(payload?.ip || payload?.query || '').trim();
                        if (!ip) throw new Error('missing_ip');
                        return payload;
                      } finally {
                        clearTimeout(timer);
                      }
                    };
                    Promise.any(endpoints.map(request))
                      .then(payload => done(payload))
                      .catch(() => done(null));
                    """,
                    endpoints,
                    parallel_timeout_ms,
                )
                geo = normalize_browser_exit_geo(data)
                if geo.get("ip"):
                    _log_detected(label, geo)
                    return geo
                # about:blank fetch occasionally returns null inside Roxy even
                # though the same proxy passed preflight. Navigate one endpoint
                # directly to remove CORS/fetch-context ambiguity; do not walk
                # every endpoint sequentially.
                if endpoints:
                    if callable(stop_check):
                        stop_check()
                    fallback_endpoint = endpoints[-1]
                    logger.info("[%s] 并行出口检测无结果，改用一次同窗口直达复核", label)
                    driver.get(fallback_endpoint)
                    fallback_data = driver.execute_script(
                        """
                        const text = document.body?.innerText || document.documentElement?.innerText || '';
                        try { return JSON.parse(text); } catch (_) { return null; }
                        """
                    )
                    geo = normalize_browser_exit_geo(fallback_data)
                    if geo.get("ip"):
                        _log_detected(label, geo)
                        return geo
            except Exception as exc:
                logger.debug(
                    "[%s] 浏览器出口 IP 并行探测失败 attempt=%s/%s: %s: %s",
                    label, attempt, max_attempts, type(exc).__name__, exc,
                )
            if attempt >= max_attempts:
                break
            if delay:
                time.sleep(min(5.0, delay * attempt))
        logger.warning("[%s] 未能从当前注册浏览器上下文识别出口 IP，账号将留空", label)
        return {}
    except Exception as exc:
        if callable(stop_check):
            stop_check()
        logger.warning(
            "[%s] 无法创建浏览器出口探测临时标签: %s: %s",
            label,
            type(exc).__name__,
            exc,
        )
        return {}
    finally:
        if probe_opened:
            try:
                driver.close()
            except Exception:
                pass
        if original_handle is not None:
            try:
                driver.switch_to.window(original_handle)
            except Exception:
                pass
        if restore_page_load_timeout is not None:
            try:
                driver.set_page_load_timeout(restore_page_load_timeout)
            except Exception:
                pass
        if restore_script_timeout is not None:
            try:
                driver.set_script_timeout(restore_script_timeout)
            except Exception:
                pass
