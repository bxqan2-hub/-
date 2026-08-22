# -*- coding: utf-8 -*-
"""通过 RoxyBrowser 指纹浏览器 + Selenium 执行 ChatGPT 注册。"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import string
import time
import uuid
from pathlib import Path

from config import roxybrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data
from core.browser_exit_geo import probe_selenium_driver_exit_geo
from core.browser_traffic import RoxyTrafficOptimizer
from core.email_provider import wait_for_otp, resolve_email_source
from core.generic_api_mail_client import GenericApiMailError, GenericApiTransportError
from core.humanize import delay as human_delay
from core.otp_utils import mask_otp, redact_otp_text
from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult
from core.twofa_proxy import build_twofa_session, resolve_twofa_proxy, twofa_failure_payload

logger = logging.getLogger(__name__)

_LOCAL_PROXY_BYPASS = ("127.0.0.1", "localhost", "::1")
_ROXY_OTP_MAX_WAIT = max(10, int(getattr(_cfg, "ROXY_OTP_MAX_WAIT", 60) or 60))
_ROXY_OTP_POLL_INTERVAL = max(1, int(getattr(_cfg, "ROXY_OTP_POLL_INTERVAL", 2) or 2))
_ROXY_OTP_SETTLE_SECONDS = max(0, int(getattr(_cfg, "ROXY_OTP_SETTLE_SECONDS", 1) or 0))
_ROXY_OTP_MAX_ATTEMPTS = max(1, int(getattr(_cfg, "ROXY_OTP_MAX_ATTEMPTS", 2) or 2))


def _session_request_timeout_ms() -> int:
    """Return a bounded in-page fetch timeout that cannot inherit Selenium's 90s script timeout."""
    seconds = max(1, min(15, int(getattr(_cfg, "ROXY_SESSION_REQUEST_TIMEOUT", 6) or 6)))
    return seconds * 1000


class ChatGPTSessionExpiredError(RuntimeError):
    """The browser session is no longer authenticated and needs a fresh login."""


def _ensure_local_proxy_bypass() -> None:
    """Keep Selenium/Roxy local control traffic out of Clash/system proxies."""
    values: list[str] = []
    seen: set[str] = set()
    for key in ("NO_PROXY", "no_proxy"):
        for value in str(os.environ.get(key) or "").split(","):
            value = value.strip()
            if value and value.lower() not in seen:
                values.append(value)
                seen.add(value.lower())
    for value in _LOCAL_PROXY_BYPASS:
        if value.lower() not in seen:
            values.append(value)
            seen.add(value.lower())
    merged = ",".join(values)
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged


def _log_prefix(driver=None) -> str:
    """按当前浏览器实现返回注册日志前缀。

    CloakBrowser 复用 Roxy 的页面操作函数；这些共享函数必须跟随实际 driver
    输出 `[Cloak注册]`，避免 Cloak 流程里混入 `[Roxy注册]` 日志。
    """
    try:
        explicit = str(getattr(driver, "_registration_log_prefix", "") or "").strip()
        if explicit:
            return explicit
        if driver is not None and driver.__class__.__name__ == "CloakSeleniumDriver":
            return "[Cloak注册]"
    except Exception:
        pass
    return "[Roxy注册]"


def _build_driver(opened: RoxyOpenResult):
    _ensure_local_proxy_bypass()
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.remote.webdriver import WebDriver as RemoteWebDriver

    def _launch(service_path: str = ""):
        if service_path:
            return webdriver.Chrome(service=Service(executable_path=service_path), options=options)
        return webdriver.Chrome(options=options)

    def _is_startup_failure(exc: BaseException) -> bool:
        message = str(exc or "").lower()
        return "unexpectedly exited" in message and (
            "3221225794" in message or "0xc0000142" in message
        )

    def _launch_with_retry(service_path: str = ""):
        last: BaseException | None = None
        for attempt in range(1, 4):
            try:
                return _launch(service_path)
            except WebDriverException as exc:
                last = exc
                if not _is_startup_failure(exc) or attempt >= 3:
                    raise
                delay = min(5.0, 1.5 * attempt)
                logger.warning(
                    "[Roxy] chromedriver 启动失败（DLL 初始化/资源瞬时错误），%ss 后重试 %s/3：%s",
                    delay,
                    attempt + 1,
                    str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
                )
                time.sleep(delay)
        raise last or RuntimeError("chromedriver 启动失败")

    if opened.debugger_address:
        logger.info("[Roxy] Selenium 连接 debuggerAddress=%s", opened.debugger_address)
        options = Options()
        # 页面里长轮询/风控脚本偶尔会让 driver.get 等到超时；eager 只等 DOMContentLoaded。
        options.page_load_strategy = "eager"
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        options.add_experimental_option("debuggerAddress", opened.debugger_address)
        driver_path = ""
        try:
            raw_data = opened.raw.get("data") if isinstance(opened.raw, dict) else {}
            if isinstance(raw_data, dict):
                driver_path = str(raw_data.get("driver") or raw_data.get("driverPath") or raw_data.get("driver_path") or "").strip()
        except Exception:
            driver_path = ""
        if driver_path:
            logger.info("[Roxy] 使用 Roxy chromedriver=%s", driver_path)
            driver = _launch_with_retry(driver_path)
        else:
            driver = _launch_with_retry()
        _apply_browser_automation_mask(driver)
        return driver

    if opened.webdriver_url:
        logger.info("[Roxy] Selenium 连接 webdriver_url=%s", opened.webdriver_url)
        options = Options()
        options.page_load_strategy = "eager"
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        driver = RemoteWebDriver(command_executor=opened.webdriver_url, options=options)
        _apply_browser_automation_mask(driver)
        return driver

    raise RuntimeError("Roxy 未返回可连接的 Selenium 地址")


def _start_traffic_optimizer(driver) -> RoxyTrafficOptimizer:
    cache_dir = Path(str(getattr(_cfg, "ROXY_CACHE_DIR", "data/browser_static_cache") or "data/browser_static_cache"))
    if not cache_dir.is_absolute():
        cache_dir = Path(__file__).resolve().parent.parent / cache_dir
    optimizer = RoxyTrafficOptimizer(
        driver,
        low_traffic=bool(getattr(_cfg, "ROXY_LOW_TRAFFIC", True)),
        static_cache=bool(getattr(_cfg, "ROXY_STATIC_CACHE", True)),
        capture=bool(getattr(_cfg, "ROXY_TRAFFIC_CAPTURE", True)),
        cache_dir=cache_dir,
        cache_max_age=int(getattr(_cfg, "ROXY_CACHE_MAX_AGE", 604800) or 604800),
        cache_max_item_bytes=int(getattr(_cfg, "ROXY_CACHE_MAX_ITEM_BYTES", 8388608) or 8388608),
        cache_refresh_rate=float(getattr(_cfg, "ROXY_CACHE_REFRESH_RATE", 0.12) or 0),
        cache_refresh_budget_bytes=int(getattr(_cfg, "ROXY_CACHE_REFRESH_BUDGET_BYTES", 262144) or 0),
        cache_refresh_max_item_bytes=int(getattr(_cfg, "ROXY_CACHE_REFRESH_MAX_ITEM_BYTES", 65536) or 0),
        budget_bytes=int(getattr(_cfg, "ROXY_TRAFFIC_BUDGET_BYTES", 3145728) or 3145728),
    )
    optimizer.install()
    logger.info(
        "[Roxy流量] 已安装：low_traffic=%s static_cache=%s capture=%s cache_dir=%s",
        optimizer.low_traffic,
        optimizer.static_cache_enabled,
        optimizer.capture,
        cache_dir,
    )
    return optimizer


def _finish_traffic_optimizer(optimizer: RoxyTrafficOptimizer | None) -> dict:
    if optimizer is None:
        return {}
    try:
        summary = optimizer.finalize()
        logger.info(
            "[Roxy流量] downloaded=%s logical=%s cached=%s hits=%s misses=%s blocked=%s requests=%s within_budget=%s errors=%s",
            summary.get("downloaded", 0),
            summary.get("logical_downloaded", 0),
            summary.get("cache_saved_bytes", 0),
            summary.get("cache_hits", 0),
            summary.get("cache_misses", 0),
            summary.get("blocked", 0),
            summary.get("network_requests", 0),
            summary.get("within_budget"),
            len(summary.get("errors") or []),
        )
        logger.info(
            "[Roxy流量] detail blocked_by_reason=%s by_host=%s by_path=%s degraded_reason=%s",
            json.dumps(summary.get("blocked_by_reason") or {}, ensure_ascii=False, separators=(",", ":")),
            json.dumps(dict(list((summary.get("by_host") or {}).items())[:5]), ensure_ascii=False, separators=(",", ":")),
            json.dumps(dict(list((summary.get("by_path") or {}).items())[:5]), ensure_ascii=False, separators=(",", ":")),
            summary.get("degraded_reason") or "none",
        )
        return summary
    except Exception as exc:
        logger.warning("[Roxy流量] 汇总失败，注册结果不受影响：%s: %s", type(exc).__name__, exc)
        return {"enabled": True, "errors": [f"finalize: {type(exc).__name__}: {exc}"]}


def _should_retry_email_entry_without_optimization(driver, exc: BaseException) -> bool:
    """Match the documented auth/error-page failure mode before normal-network retry."""
    message = str(exc or "").lower()
    if "找不到邮箱输入框/邮箱入口" not in message and "email input" not in message:
        return False
    try:
        url = str(driver.current_url or "").lower()
    except Exception:
        url = ""
    return "/auth/error" in url or "/auth/login" in url


def _retry_email_entry_after_traffic_fallback(driver, email: str, optimizer: RoxyTrafficOptimizer) -> str:
    optimizer.disable_for_recovery("email_submit_auth_error")
    logger.warning("%s 邮箱提交后进入错误页，已按故障排查顺序关闭优化并重载登录页", _log_prefix(driver))
    _safe_get(
        driver,
        "https://chatgpt.com/auth/login",
        timeout=min(45, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
        attempts=2,
        accept_hosts=("chatgpt.com", "auth.openai.com"),
    )
    human_delay("navigate")
    _page_warmup(driver, reason="email_submit_recovery")
    _maybe_accept(driver)
    return _submit_email_and_wait_next(driver, email, attempts=2)


def _center_browser_window(driver) -> None:
    """把可见的 Roxy 窗口移动到 Windows 主屏工作区中央。"""
    if bool(getattr(_cfg, "ROXY_OPEN_HEADLESS", False)):
        return
    try:
        import platform
        if platform.system().lower() != "windows":
            return
        import ctypes

        class _Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        work_area = _Rect()
        if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0):
            raise OSError("无法读取 Windows 工作区")
        size = driver.get_window_size()
        width = max(1, int(size.get("width") or 1))
        height = max(1, int(size.get("height") or 1))
        x = int(work_area.left + max(0, (work_area.right - work_area.left - width) // 2))
        y = int(work_area.top + max(0, (work_area.bottom - work_area.top - height) // 2))
        driver.set_window_position(x, y)
        logger.info("[Roxy] 浏览器窗口已居中：x=%s y=%s width=%s height=%s", x, y, width, height)
    except Exception as exc:
        logger.warning("[Roxy] 浏览器窗口居中失败，继续执行：%s", exc)


def _wait(driver, timeout: int | None = None):
    from selenium.webdriver.support.ui import WebDriverWait
    return WebDriverWait(driver, timeout or int(_cfg.ROXY_SELENIUM_TIMEOUT))


def _safe_get(driver, url: str, *, timeout: int = 45, attempts: int = 2, accept_hosts: tuple[str, ...] = ()) -> None:
    """带容错的页面跳转。

    Roxy/Chrome 150 偶发 `Timed out receiving message from renderer`，实际页面可能已经可用。
    这里超时后先 `window.stop()`，只要当前 URL/DOM 已进入目标页就继续；否则重试一次。
    """
    from selenium.common.exceptions import TimeoutException, WebDriverException

    last_exc: Exception | None = None
    old_timeout = int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)
    hosts = tuple(h.lower() for h in (accept_hosts or ()))
    for attempt in range(1, max(1, attempts) + 1):
        try:
            try:
                driver.set_page_load_timeout(max(10, int(timeout)))
                driver.set_script_timeout(8)
            except Exception:
                pass
            driver.get(url)
            return
        except TimeoutException as exc:
            last_exc = exc
            logger.warning(
                "%s 页面加载超时，尝试停止加载后检查 DOM：url=%s attempt=%s/%s error=%s",
                _log_prefix(driver), url, attempt, attempts, str(exc).splitlines()[0] if str(exc) else "TimeoutException",
            )
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            time.sleep(1.0)
            try:
                current = str(driver.current_url or "").lower()
            except Exception:
                current = ""
            try:
                ready = str(driver.execute_script("return document.readyState || ''") or "")
                has_body = bool(driver.execute_script("return !!document.body"))
            except Exception:
                ready = ""
                has_body = False
            target_ok = any(h in current for h in hosts) if hosts else (url.split("/", 3)[2].lower() in current)
            if target_ok and has_body:
                logger.info(
                    "%s 页面加载虽超时但 DOM 可用，继续流程：current=%s readyState=%s",
                    _log_prefix(driver), current[:180], ready or "-",
                )
                return
            if attempt < attempts:
                try:
                    driver.get("about:blank")
                except Exception:
                    pass
                time.sleep(1.5 * attempt)
                continue
        except WebDriverException as exc:
            last_exc = exc
            if attempt < attempts:
                logger.warning("%s 页面跳转失败，准备重试：url=%s attempt=%s/%s error=%s", _log_prefix(driver), url, attempt, attempts, exc)
                time.sleep(1.5 * attempt)
                continue
            raise
        finally:
            try:
                driver.set_page_load_timeout(old_timeout)
            except Exception:
                pass
    raise last_exc or RuntimeError(f"页面跳转失败: {url}")


def _visible(el) -> bool:
    try:
        return el.is_displayed() and el.is_enabled()
    except Exception:
        return False


def _browser_actions_enabled() -> bool:
    try:
        from config import humanize as _hcfg
        return bool(getattr(_hcfg, "ENABLE_HUMANIZE_BROWSER_ACTIONS", True))
    except Exception:
        return True


def _apply_browser_automation_mask(driver) -> None:
    """连接 Selenium 后尽量降低明显自动化特征；失败不影响主流程。"""
    if not _browser_actions_enabled():
        return
    try:
        script = r"""
        Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined});
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
        const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (originalQuery) {
          window.navigator.permissions.query = (parameters) => (
            parameters && parameters.name === 'notifications'
              ? Promise.resolve({ state: Notification.permission })
              : originalQuery(parameters)
          );
        }
        """
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        try:
            driver.execute_script(script)
        except Exception:
            pass
        logger.info("%s 已注入浏览器自动化特征弱化脚本", _log_prefix(driver))
    except Exception as exc:
        logger.debug("%s 注入自动化特征弱化脚本失败：%s", _log_prefix(driver), exc)


def _human_scroll_to(driver, el) -> None:
    try:
        block = random.choice(["center", "nearest", "center"])
        driver.execute_script("arguments[0].scrollIntoView({block: arguments[1], inline:'nearest'});", el, block)
        if _browser_actions_enabled():
            time.sleep(random.uniform(0.08, 0.35))
            # 轻微滚动抖动，避免每次都精准居中。
            driver.execute_script("window.scrollBy(0, arguments[0]);", random.randint(-90, 90))
            time.sleep(random.uniform(0.05, 0.22))
            driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'nearest'});", el)
    except Exception:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass


def _human_click(driver, el, *, label: str = "") -> None:
    """快速人工化点击。

    之前用 ActionChains 在 Roxy/Chrome 150 上偶发卡住 1-2 分钟，导致邮箱提交很慢。
    这里改为 CDP 派发鼠标事件；没有 CDP 时再用 JS/原生 click 兜底。
    """
    _human_scroll_to(driver, el)
    if not _browser_actions_enabled():
        time.sleep(0.2)
        el.click()
        return
    try:
        human_delay("click")
        point = driver.execute_script(r"""
        const el = arguments[0];
        const r = el.getBoundingClientRect();
        const x = r.left + r.width * (0.30 + Math.random() * 0.40);
        const y = r.top + r.height * (0.35 + Math.random() * 0.30);
        return {x, y, w:r.width, h:r.height};
        """, el) or {}
        x = float(point.get("x") or 0)
        y = float(point.get("y") or 0)
        if hasattr(driver, "execute_cdp_cmd") and x > 0 and y > 0:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            time.sleep(random.uniform(0.05, 0.22))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            time.sleep(random.uniform(0.035, 0.13))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        else:
            driver.execute_script(r"""
            const el = arguments[0];
            el.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, cancelable:true, pointerType:'mouse'}));
            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
            el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
            el.click();
            """, el)
    except Exception as exc:
        logger.debug("%s 人工化点击失败，回退 el.click label=%s err=%s", _log_prefix(driver), label, exc)
        time.sleep(random.uniform(0.12, 0.45))
        try:
            driver.execute_script("arguments[0].click();", el)
        except Exception:
            el.click()


def _human_type_text(driver, el, value: str, *, clear: bool = True) -> None:
    """按字符/小段输入，触发真实 key events；失败时回退 JS setter。"""
    if not _browser_actions_enabled():
        if clear:
            try:
                el.clear()
            except Exception:
                pass
        el.send_keys(value)
        return
    try:
        _human_scroll_to(driver, el)
        try:
            _human_click(driver, el, label="input_focus")
        except Exception:
            driver.execute_script("arguments[0].focus();", el)
        if clear:
            from selenium.webdriver.common.keys import Keys
            mod = Keys.COMMAND
            try:
                import platform
                if platform.system().lower() != "darwin":
                    mod = Keys.CONTROL
            except Exception:
                pass
            try:
                el.send_keys(mod, "a")
                time.sleep(random.uniform(0.04, 0.16))
                el.send_keys(Keys.BACKSPACE)
            except Exception:
                try:
                    el.clear()
                except Exception:
                    pass
        text = str(value)
        i = 0
        while i < len(text):
            # 邮箱/密码整体仍逐字符，但偶尔 2 字符一组，节奏更自然。
            step = 2 if random.random() < 0.12 and i + 1 < len(text) else 1
            el.send_keys(text[i:i + step])
            i += step
            human_delay("keystroke")
            if i < len(text) and random.random() < 0.08:
                human_delay("typing_pause")
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            el,
        )
    except Exception as exc:
        logger.debug("%s 人工化输入失败，回退 JS setter err=%s", _log_prefix(driver), exc)
        _set_element_value(driver, el, value)


def _page_warmup(driver, *, reason: str = "") -> None:
    if not _browser_actions_enabled():
        return
    try:
        human_delay("page_warmup")
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": random.randint(80, 360),
                "y": random.randint(80, 260),
            })
    except Exception:
        pass


def _find_any(driver, selectors: list[str], timeout: int | None = None):
    from selenium.webdriver.common.by import By

    end = time.time() + (timeout or int(_cfg.ROXY_SELENIUM_TIMEOUT))
    last = None
    while time.time() < end:
        for selector in selectors:
            try:
                by = By.XPATH if selector.startswith("//") else By.CSS_SELECTOR
                items = driver.find_elements(by, selector)
                for item in items:
                    if _visible(item):
                        return item
            except Exception as exc:
                last = exc
        time.sleep(0.4)
    raise RuntimeError(f"找不到页面元素: {selectors}; last={last}")


def _click_any(driver, selectors: list[str], timeout: int | None = None) -> None:
    el = _find_any(driver, selectors, timeout)
    _human_click(driver, el, label="click_any")


def _type_any(driver, selectors: list[str], value: str, timeout: int | None = None, clear: bool = True) -> None:
    el = _find_any(driver, selectors, timeout)
    _human_type_text(driver, el, value, clear=clear)


_EMAIL_INPUT_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "input#email-input",
    "input[autocomplete='email']",
]


def _email_entry_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const attrText = el => [
          el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
          el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
          el.getAttribute('data-auth-provider'), el.getAttribute('href'), el.getAttribute('action'),
          el.getAttribute('formaction'), el.getAttribute('value')
        ].filter(Boolean).join(' ').toLowerCase();
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''
        })).slice(0, 30);
        const actions = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')]
          .filter(visible).map(el => ({tag: el.tagName, type: el.getAttribute('type') || '', attrs: attrText(el)})).slice(0, 40);
        return {url: location.href, title: document.title, inputs, actions};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_empty_login_shell(state: dict | None) -> bool:
    state = state or {}
    url = str(state.get("url") or "").lower()
    return (
        "chatgpt.com/auth/login" in url
        and not (state.get("inputs") or [])
        and not (state.get("actions") or [])
    )


def _reload_empty_login_shell(driver) -> None:
    """Reload once when ChatGPT exposes a body/title but no interactive DOM."""
    logger.warning("%s 登录页 DOM 持续为空，执行一次干净重载", _log_prefix(driver))
    try:
        driver.get("about:blank")
    except Exception:
        pass
    time.sleep(1.0)
    _safe_get(
        driver,
        "https://chatgpt.com/auth/login",
        timeout=min(45, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
        attempts=2,
        accept_hosts=("chatgpt.com", "auth.openai.com"),
    )
    _page_warmup(driver, reason="empty_login_shell_reload")


def _find_visible_email_input_js(driver):
    return driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const selectors = [
      'input[type="email"]',
      'input[name="email"]',
      'input[name="username"]',
      'input#email-input',
      'input[autocomplete="email"]'
    ];
    for (const sel of selectors) {
      const el = [...document.querySelectorAll(sel)].find(visible);
      if (el) return el;
    }
    return null;
    """)


def _is_oauth_consent_like(driver) -> bool:
    """检测是否已到 OAuth 授权/consent 页。这里不能再点任何邮箱分支或全局提交按钮。"""
    try:
        return bool(driver.execute_script(r"""
        const url = String(location.href || '').toLowerCase();
        if (/oauth|authorize|consent/.test(url) && !/login|signup|identifier|email-verification/.test(url)) return true;
        const formsWithEmail = [...document.querySelectorAll('form')]
          .some(form => form.querySelector('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]'));
        if (formsWithEmail) return false;
        const actions = [...document.querySelectorAll('button,a,[role="button"],input[type="submit"],input[type="button"]')]
          .map(el => [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('href'),
            el.getAttribute('formaction'), el.value, el.className].filter(Boolean).join(' ').toLowerCase())
          .join(' ');
        return /oauth|authorize|consent|grant|allow/.test(actions) && !/email|username/.test(actions);
        """))
    except Exception:
        return False


def _is_external_idp_url(url: str) -> bool:
    u = str(url or '').lower()
    return any(x in u for x in (
        'accounts.google.', 'google.com/o/oauth', 'appleid.apple.', 'login.microsoftonline.',
        'login.live.', 'github.com/login/oauth', 'facebook.com/', 'saml', 'sso'
    ))


def _assert_not_external_idp(driver, label: str = '') -> None:
    try:
        current = str(driver.current_url or '')
    except Exception:
        current = ''
    if _is_external_idp_url(current):
        raise RuntimeError(f"误入第三方账号授权页（{label}）：{current}")


def _click_email_entry_option(driver) -> bool:
    """点击“邮箱方式”入口；只看 DOM 技术属性，不看按钮可见文案，并显式排除 Google 等第三方。"""
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，跳过邮箱入口兜底点击", _log_prefix(driver))
        return False
    target = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const attrText = el => {
      const own = [
        el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
        el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
        el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'), el.getAttribute('href'), el.getAttribute('action'),
        el.getAttribute('formaction'), el.getAttribute('value'), el.getAttribute('aria-label'), el.className
      ].filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' ')).join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
    const good = /(^|[^a-z])(email|mail|username|passwordless|otp|magic)([^a-z]|$)/;
    const candidates = [...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')]
      .filter(visible)
      .map(el => ({el, attrs: attrText(el), hasLogo: !!el.querySelector('img,svg,use')}))
      .filter(x => good.test(x.attrs) && !bad.test(x.attrs) && !x.hasLogo);
    if (candidates.length !== 1) return null;
    candidates[0].el.scrollIntoView({block:'center'});
    return candidates[0].el;
    """)
    if target:
        _human_click(driver, target, label="email_entry")
        return True
    return False


def _type_email_address(driver, email: str, timeout: int | None = None) -> str | None:
    """进入邮箱登录/注册方式并填写邮箱。全程不依赖页面可见文字，避免非日本出口本地化后误点 Google。"""
    end = time.time() + (timeout or int(_cfg.ROXY_SELENIUM_TIMEOUT))
    last_state = None
    clicked_email_option = False
    empty_shell_since = None
    empty_shell_reloaded = False
    while time.time() < end:
        # A late SPA/server transition can arrive after the caller's previous
        # observation budget.  Detect that state before looking for an email
        # input again so an existing OTP/password page is never misreported as
        # a missing email field.
        el = _find_visible_email_input_js(driver)
        if el:
            _human_type_text(driver, el, email, clear=True)
            return None
        if _is_email_verification_page(driver):
            return "otp"
        if _is_signup_password_page(driver):
            return "password"
        if _is_login_password_page(driver):
            return "login_password"
        if _has_access_token(driver):
            return "logged_in"
        last_state = _email_entry_state(driver)
        if _is_empty_login_shell(last_state):
            empty_shell_since = empty_shell_since or time.time()
            if not empty_shell_reloaded and time.time() - empty_shell_since >= 6.0:
                empty_shell_reloaded = True
                _reload_empty_login_shell(driver)
                # The reload itself can consume the original 20 second element
                # budget. Give the fresh document one complete short wait.
                end = max(end, time.time() + 20.0)
                continue
        else:
            empty_shell_since = None
        if not clicked_email_option and _click_email_entry_option(driver):
            clicked_email_option = True
            time.sleep(1.0)
            _assert_not_external_idp(driver, "点击邮箱入口后")
            continue
        time.sleep(0.4)
    raise RuntimeError(f"找不到邮箱输入框/邮箱入口（未使用文字识别），state={last_state}")


def _submit_nearest_form_for_active_input(driver) -> bool:
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，禁止执行邮箱提交", _log_prefix(driver))
        return False
    result = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]')]
      .find(visible);
    if (!input) return {ok:false, reason:'missing_email_input'};
    const value = String(input.value || '').trim();
    if (!value || !value.includes('@')) return {ok:false, reason:'email_value_not_ready', value};
    const form = input.closest('form');
    if (!form) return {ok:false, reason:'missing_form'};

    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|sso|saml|idp|provider|authorize|consent|grant|allow/;
    const attrText = el => {
      const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
        el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
        el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
        .filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' '))
        .join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const inputRect = input.getBoundingClientRect();
    const formId = form.getAttribute('id') || '';
    const scopedButtons = [
      ...form.querySelectorAll('button,input[type="submit"]'),
      ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
    ].filter((el, idx, arr) => arr.indexOf(el) === idx);
    const rawButtons = scopedButtons
      .filter(visible)
      .map((el, idx) => {
        const r = el.getBoundingClientRect();
        const attrs = attrText(el);
        const hasLogo = !!el.querySelector('img,svg,use');
        const isBad = bad.test(attrs) || hasLogo;
        const belowInput = r.top >= inputRect.bottom - 10;
        const distance = Math.max(0, r.top - inputRect.bottom) + Math.abs((r.left + r.right) / 2 - (inputRect.left + inputRect.right) / 2) / 10;
        const cls = String(el.className || '').toLowerCase();
        const type = String(el.getAttribute('type') || '').toLowerCase();
        // ChatGPT 新版邮箱页的主按钮形如：
        // <button class="... btn-primary ... w-full ..." type="submit"><div>続行</div></button>
        // 优先选择同 form 下的 primary submit，而不是因为多个按钮距离接近误判歧义。
        const isPrimarySubmit = (el.tagName === 'BUTTON' || el.tagName === 'INPUT') && type === 'submit'
          && (/\bbtn-primary\b/.test(cls) || /\b_primary_/.test(cls) || /\bw-full\b/.test(cls));
        const score = (isPrimarySubmit ? 1000 : 0) + (type === 'submit' ? 100 : 0) - distance;
        return {el, idx, attrs, isBad, hasLogo, belowInput, distance, score, isPrimarySubmit, tag: el.tagName, type};
      });
    const safe = rawButtons.filter(x => !x.isBad && x.belowInput)
      .sort((a,b) => b.score - a.score || a.distance - b.distance || a.idx - b.idx);
    if (!safe.length) {
      return {ok:false, reason:'no_safe_submit', buttons: rawButtons.map(x => ({idx:x.idx, isBad:x.isBad, hasLogo:x.hasLogo, belowInput:x.belowInput, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    // 多个安全按钮时，若没有明确 primary submit，且距离接近，才认为页面歧义。
    if (!safe[0].isPrimarySubmit && safe.length > 1 && Math.abs(safe[0].distance - safe[1].distance) < 8) {
      return {ok:false, reason:'ambiguous_submit', buttons: safe.slice(0,3).map(x => ({idx:x.idx, distance:x.distance, score:x.score, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    const target = safe[0].el;
    target.scrollIntoView({block:'center'});
    window.__roxy_email_submit_debug = {at: Date.now(), targetAttrs: safe[0].attrs.slice(0,240), buttonCount: rawButtons.length, primary:safe[0].isPrimarySubmit};
    return {ok:true, reason:safe[0].isPrimarySubmit ? 'primary_submit' : 'safe_submit', target, targetAttrs:safe[0].attrs.slice(0,160), primary:safe[0].isPrimarySubmit};
    """) or {}
    if result.get("ok"):
        target = result.get("target")
        if target:
            _human_click(driver, target, label="email_submit")
        else:
            logger.warning("%s 邮箱提交未返回目标元素，回退 requestSubmit", _log_prefix(driver))
            driver.execute_script("document.querySelector('form')?.requestSubmit?.();")
        logger.info("%s 邮箱表单安全提交：%s", _log_prefix(driver), result)
        time.sleep(0.8)
        _assert_not_external_idp(driver, "提交邮箱后")
        return True
    logger.warning("%s 未执行邮箱提交：%s", _log_prefix(driver), result)
    return False


def _current_email_input_value(driver) -> str:
    try:
        state = _email_input_value_state(driver)
        for item in state.get("inputs") or []:
            value = str(item.get("value") or "").strip()
            if "@" in value:
                return value
    except Exception:
        pass
    return ""


def _stabilize_email_input_before_submit(driver, email: str) -> dict:
    """提交前把 DOM value / React 受控状态 / blur-change 状态统一稳定下来。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.scrollIntoView({block:'center', inline:'nearest'});
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;

        // 让 React/表单校验尽量收到完整输入链路。
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        input.focus();

        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        return {
          ok:true,
          value: input.value,
          active: document.activeElement === input,
          hasForm: !!form,
          hasSubmit: !!submit,
          submitDisabled: submit ? (!!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true') : null,
          url: location.href
        };
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_form_stable(driver, email: str) -> dict:
    """第一次提交就按“补交成功”的方式执行：稳定 value 后 Enter + DOM click。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        const editable = el => visible(el) && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(editable);
        if (!input) return {ok:false, reason:'missing_email_input'};
        if (!email || !email.includes('@')) return {ok:false, reason:'empty_email', value: email};

        const form = input.closest('form');
        if (!form) return {ok:false, reason:'missing_form'};

        const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
        const attrText = el => {
          const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
            el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
            .filter(Boolean).join(' ');
          const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
            .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
              x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
              .filter(Boolean).join(' '))
            .join(' ');
          return `${own} ${desc}`.toLowerCase();
        };

        const formId = form.getAttribute('id') || '';
        const buttons = [
          ...form.querySelectorAll('button,input[type="submit"]'),
          ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
        ].filter((el, idx, arr) => arr.indexOf(el) === idx)
          .filter(el => visible(el) && !bad.test(attrText(el)) && !el.querySelector('img,svg,use'));
        const submit = buttons.find(el => (el.getAttribute('type') || '').toLowerCase() === 'submit') || buttons[0] || null;
        if (!submit) return {ok:false, reason:'missing_safe_submit'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.scrollIntoView({block:'center', inline:'nearest'});
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        input.focus();

        submit.scrollIntoView({block:'center', inline:'nearest'});

        // 不要在 execute_script 同步执行 submit.click()：
        // ChromeDriver 会等前端 submit/navigation，Roxy/Chrome 150 上可能卡到 page/script timeout。
        // setTimeout 让 Selenium 先返回，点击在页面事件循环里异步发生，和补交逻辑一致。
        setTimeout(() => {
          try {
            input.focus();
            input.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keypress', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            if (submit && !submit.disabled) submit.click();
            else if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
          } catch (_) {}
        }, 80);

        window.__roxy_email_submit_debug = {
          at: Date.now(),
          mode: 'stable_async_enter_click',
          value: input.value,
          submitAttrs: attrText(submit).slice(0, 240)
        };
        return {
          ok:true,
          reason:'stable_async_enter_click',
          value: input.value,
          submitDisabled: !!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          submitAttrs: attrText(submit).slice(0, 180),
          url: location.href
        };
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_step(driver, email: str | None = None) -> None:
    # 不再优先走浏览器内 NextAuth fetch：
    # Roxy/Chrome 150 下 execute_async_script + fetch 偶发卡到 script timeout；
    # 实测 UI 首次提交后若停在 /auth/login?email=...，由 _recover_email_submit_if_stuck 补交表单更稳定。
    email_value = str(email or _current_email_input_value(driver) or "").strip()
    stable = _stabilize_email_input_before_submit(driver, email_value)
    logger.info("%s 邮箱提交前状态稳定：%s", _log_prefix(driver), stable)
    time.sleep(random.uniform(0.8, 1.8) if _browser_actions_enabled() else 0.4)

    stable_submit = _submit_email_form_stable(driver, email_value)
    if stable_submit.get("ok"):
        logger.info("%s 邮箱稳定表单提交：%s", _log_prefix(driver), stable_submit)
        time.sleep(1.0)
        _assert_not_external_idp(driver, "稳定表单提交邮箱后")
        return
    logger.warning("%s 邮箱稳定表单提交失败，回退 UI 点击提交：%s", _log_prefix(driver), stable_submit)
    if _submit_nearest_form_for_active_input(driver):
        return
    raise RuntimeError(f"无法提交邮箱步骤（拒绝按页面文字或首个 submit 兜底，避免误点第三方登录），state={_email_entry_state(driver)}")


def _recover_email_submit_if_stuck(driver, email: str) -> dict:
    """邮箱提交后停在 /auth/login?email= 且输入框被清空时，补一次原生表单提交。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email}));
        input.dispatchEvent(new Event('change', {bubbles:true}));
        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        setTimeout(() => {
          try {
            input.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            if (submit && !submit.disabled) submit.click();
            else if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
          } catch (_) {}
        }, 80);
        return {ok:true, reason:'resubmitted_email_form', value: input.value, hasForm: !!form, hasSubmit: !!submit};
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_via_browser_nextauth(driver, email: str) -> dict:
    """在 Roxy 浏览器上下文里调用 ChatGPT NextAuth signin。

    UI submit 在 Roxy/Chrome 150 上会偶发只跳到 `/auth/login?email=...` 后停住。
    这里改走浏览器页面内 fetch，仍使用当前 Roxy 浏览器的 cookie / 指纹环境，
    拿到 auth.openai.com authorize URL 后让浏览器跳转。
    """
    try:
        current = str(getattr(driver, "current_url", "") or "")
        if "chatgpt.com" not in current:
            return {"ok": False, "reason": "not_on_chatgpt", "url": current[:180]}
    except Exception:
        current = ""

    did = str(uuid.uuid4())
    auth_log_id = str(uuid.uuid4())
    old_script_timeout = int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)
    try:
        try:
            driver.set_script_timeout(25)
        except Exception:
            pass
        result = driver.execute_async_script(r"""
        const email = String(arguments[0] || '').trim();
        const did = String(arguments[1] || '');
        const authLogId = String(arguments[2] || '');
        const done = arguments[arguments.length - 1];
        (async () => {
          try {
            const csrfResp = await fetch('/api/auth/csrf', {
              method: 'GET',
              credentials: 'include',
              headers: {
                'accept': 'application/json',
                'cache-control': 'no-cache',
                'pragma': 'no-cache'
              }
            });
            const csrfText = await csrfResp.text();
            let csrfData = {};
            try { csrfData = JSON.parse(csrfText); } catch (_) {}
            const csrfToken = csrfData.csrfToken || '';
            if (!csrfResp.ok || !csrfToken) {
              done({ok:false, stage:'csrf', status:csrfResp.status, body:csrfText.slice(0, 500)});
              return;
            }

            const q = new URLSearchParams({
              prompt: 'login',
              'ext-oai-did': did,
              auth_session_logging_id: authLogId,
              'ext-passkey-client-capabilities': '11111',
              screen_hint: 'login_or_signup',
              login_hint: email
            });
            const body = new URLSearchParams({
              callbackUrl: 'https://chatgpt.com/',
              csrfToken,
              json: 'true'
            });
            const resp = await fetch('/api/auth/signin/openai?' + q.toString(), {
              method: 'POST',
              credentials: 'include',
              headers: {
                'accept': 'application/json',
                'content-type': 'application/x-www-form-urlencoded',
                'cache-control': 'no-cache',
                'pragma': 'no-cache'
              },
              body: body.toString()
            });
            const text = await resp.text();
            let data = {};
            try { data = JSON.parse(text); } catch (_) {}
            let url = data.url || '';
            if (!resp.ok || !url) {
              done({ok:false, stage:'signin', status:resp.status, body:text.slice(0, 700)});
              return;
            }

            try {
              const u = new URL(url, location.href);
              if (!u.searchParams.get('screen_hint')) u.searchParams.set('screen_hint', 'login_or_signup');
              if (!u.searchParams.get('login_hint')) u.searchParams.set('login_hint', email);
              if (!u.searchParams.get('ext-oai-did')) u.searchParams.set('ext-oai-did', did);
              if (!u.searchParams.get('auth_session_logging_id')) u.searchParams.set('auth_session_logging_id', authLogId);
              url = u.toString();
            } catch (_) {}
            window.location.assign(url);
            done({ok:true, stage:'redirect', url:url.slice(0, 260)});
          } catch (e) {
            done({ok:false, stage:'exception', error:String(e && (e.stack || e.message) || e).slice(0, 700)});
          }
        })();
        """, email, did, auth_log_id) or {}
        return result if isinstance(result, dict) else {"ok": False, "reason": "invalid_result", "result": str(result)[:300]}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            driver.set_script_timeout(old_script_timeout)
        except Exception:
            pass


def _email_input_value_state(driver) -> dict:
    """读取当前可见邮箱框状态，用于提交后确认是否真的进入下一步。"""
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .filter(visible)
          .map(el => ({type: el.getAttribute('type') || '', name: el.name || '', id: el.id || '', autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''}));
        return {url: location.href, inputs};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_email_login_page_still_present(driver) -> bool:
    state = _email_input_value_state(driver)
    return bool(state.get("inputs"))


def _wait_email_submit_next_state(driver, email: str, timeout: int = 18) -> str:
    """邮箱提交后等待进入 password / otp / logged_in；仍停留邮箱页则返回 email_page。

    Cloak/Playwright 路径里，点击 submit 后页面经常先发生一次 SPA 导航：
    `chatgpt.com/auth/login?email=...`，同时 React 会短暂把 email input 清空。
    旧逻辑一看到空 input 就立刻返回 `email_cleared`，导致在真正跳到
    `auth.openai.com/...` 前过早重填，形成“提交 -> 清空 -> 重填”的循环。
    这里对 email_cleared 做去抖：只记录并继续观察几秒；若期间进入
    password/otp/login_password/logged_in 则按真实状态返回，持续清空才让上层重试。
    """
    end = time.time() + timeout
    last = None
    cleared_seen_at: float | None = None
    cleared_last_log_at = 0.0
    cleared_recover_done = False
    expected_email = str(email or "").strip().lower()
    while time.time() < end:
        if _has_access_token(driver):
            return "logged_in"
        if _is_login_password_page(driver):
            return "login_password"
        if _is_email_verification_page(driver):
            return "otp"
        if _is_signup_password_page(driver):
            return "password"
        state = _email_input_value_state(driver)
        last = state
        inputs = state.get("inputs") or []
        if inputs:
            values = [str(i.get("value") or "") for i in inputs]
            url = str(state.get("url") or "")
            has_blank = any(v == "" for v in values)
            has_expected = any(v.strip().lower() == expected_email for v in values)
            if has_blank and not has_expected:
                now = time.time()
                if cleared_seen_at is None:
                    cleared_seen_at = now
                # URL 已带 email 查询参数时更像是提交后的中间态，给它更长观察窗口。
                debounce = 18.0 if ("/auth/login" in url and "email=" in url) else 5.0
                if now - cleared_last_log_at > 2.0:
                    logger.info(
                        "%s 邮箱提交后检测到输入框短暂清空，继续等待跳转：elapsed=%.1fs debounce=%.1fs url=%s",
                        _log_prefix(driver), now - cleared_seen_at, debounce, url[:180],
                    )
                    cleared_last_log_at = now
                if (
                    not cleared_recover_done
                    and "/auth/login" in url
                    and "email=" in url
                    and now - cleared_seen_at >= 2.0
                ):
                    recover = _recover_email_submit_if_stuck(driver, email)
                    cleared_recover_done = True
                    logger.info("%s 邮箱提交后仍停留在 login?email，中途补交一次表单：%s", _log_prefix(driver), recover)
                if now - cleared_seen_at >= debounce:
                    return "email_cleared"
            else:
                cleared_seen_at = None
            # 仍是当前邮箱页，继续短等。
        time.sleep(0.8)
    logger.info("%s 邮箱提交后等待下一步超时，最后邮箱页状态=%s", _log_prefix(driver), last)
    return "email_page" if _is_email_login_page_still_present(driver) else "unknown"


def _submit_email_and_wait_next(driver, email: str, attempts: int = 3) -> str:
    """填写并提交邮箱，必须确认进入 password/otp/logged_in 才返回。"""
    last_state = None
    nextauth_fallback_used = False
    wait_timeout = max(5, int(getattr(_cfg, "ROXY_EMAIL_SUBMIT_TIMEOUT", 25) or 25))
    for attempt in range(1, attempts + 1):
        if _is_email_verification_page(driver):
            logger.info("%s 重试填写邮箱前确认已进入 OTP 页面，停止重填", _log_prefix(driver))
            return "otp"
        if _is_signup_password_page(driver):
            return "password"
        if _has_access_token(driver):
            return "logged_in"
        typed_state = _type_email_address(driver, email, timeout=20)
        if typed_state == "login_password":
            raise RuntimeError(
                f"Email transitioned to an existing-account password page: "
                f"url={getattr(driver, 'current_url', '') or 'https://auth.openai.com/log-in/password'}"
            )
        if typed_state in ("password", "otp", "logged_in"):
            logger.info("%s email advanced while preparing a retry: %s", _log_prefix(driver), typed_state)
            return typed_state
        state = _email_input_value_state(driver)
        last_state = state
        values = [str(i.get("value") or "") for i in (state.get("inputs") or [])]
        if not any(v.strip().lower() == email.strip().lower() for v in values):
            logger.warning("%s 邮箱写入校验失败，准备重试：attempt=%s/%s state=%s", _log_prefix(driver), attempt, attempts, state)
            time.sleep(0.8)
            continue
        logger.info("%s 已填写邮箱并校验通过：%s", _log_prefix(driver), email)
        human_delay("form")
        _submit_email_step(driver, email)
        logger.info("%s 已提交邮箱，等待进入密码页或验证码页（%s/%s）", _log_prefix(driver), attempt, attempts)
        state_name = _wait_email_submit_next_state(driver, email, timeout=wait_timeout)
        if state_name == "login_password":
            raise RuntimeError(f"邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: url={getattr(driver, 'current_url', '') or 'https://auth.openai.com/log-in/password'}")
        if state_name in ("password", "otp", "logged_in"):
            logger.info("%s 邮箱提交后已进入下一步：%s", _log_prefix(driver), state_name)
            return state_name

        # 新版 ChatGPT 登录页偶发只完成前端的 ``?email=`` 路由更新，随后把
        # input 清空，却没有真正发起 OpenAI authorize 跳转。重复点击同一个
        # React 表单通常只会复现该状态，因此只做一次浏览器上下文内的
        # NextAuth signin 兜底；它仍复用当前 Roxy profile 的 cookie/指纹。
        if state_name in ("email_cleared", "email_page") and not nextauth_fallback_used:
            nextauth_fallback_used = True
            fallback = _submit_email_via_browser_nextauth(driver, email)
            logger.info("%s 邮箱 UI 提交停滞，尝试一次 NextAuth 跳转兜底：%s", _log_prefix(driver), fallback)
            fallback_state = _wait_email_submit_next_state(driver, email, timeout=wait_timeout)
            if fallback_state == "login_password":
                raise RuntimeError(
                    f"邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: "
                    f"url={getattr(driver, 'current_url', '') or 'https://auth.openai.com/log-in/password'}"
                )
            if fallback_state in ("password", "otp", "logged_in"):
                logger.info("%s NextAuth 兜底后已进入下一步：%s", _log_prefix(driver), fallback_state)
                return fallback_state
            state_name = fallback_state
        logger.warning("%s 邮箱提交后仍未进入下一步：%s，准备重填重试 state=%s", _log_prefix(driver), state_name, _email_input_value_state(driver))
        time.sleep(1.0)
    raise RuntimeError(f"邮箱提交后未进入密码页/验证码页，最后状态={last_state}")


def _wait_for_otp_input(driver, timeout: int = 30) -> str | None:
    """Wait for the live OTP DOM control after a resend/navigation redraw."""
    end = time.time() + max(1, int(timeout))
    while time.time() < end:
        state = {}
        try:
            if _is_email_verification_page(driver):
                state = _email_otp_page_state(driver)
                if _email_otp_verified_success(state):
                    logger.info("%s[OTP] 检测到 Email verified 确认页，不再等待验证码输入框", _log_prefix(driver))
                    return "email_verified"
                if state.get("inputs"):
                    return
        except Exception:
            pass
        terminal_error = _email_otp_terminal_error(state)
        if terminal_error:
            raise RuntimeError(
                f"OpenAI 返回 {terminal_error}：该邮箱对应的账号已删除或停用，禁止继续注册"
            )
        time.sleep(0.8)
    state = _email_otp_page_state(driver)
    if _email_otp_verified_success(state):
        logger.info("%s[OTP] 超时检查发现 Email verified 确认页，按已验证处理", _log_prefix(driver))
        return "email_verified"
    terminal_error = _email_otp_terminal_error(state)
    if terminal_error:
        raise RuntimeError(
            f"OpenAI 返回 {terminal_error}：该邮箱对应的账号已删除或停用，禁止继续注册"
        )
    raise RuntimeError(f"等待 OTP 输入框超时: url={state.get('url')}; inputs={state.get('inputs')}")


def _is_browser_navigation_error(driver) -> bool:
    try:
        return str(driver.current_url or "").lower().startswith("chrome-error://")
    except Exception:
        return False


def _restart_email_otp_from_login(driver, email: str) -> str:
    """Recover an OTP retry after Chromium replaces the page with an error URL."""
    logger.warning("%s[OTP] 浏览器进入导航错误页，重新打开登录入口并申请新验证码", _log_prefix(driver))
    _safe_get(
        driver,
        "https://chatgpt.com/auth/login",
        timeout=45,
        attempts=2,
        accept_hosts=("chatgpt.com", "auth.openai.com"),
    )
    _page_warmup(driver, reason="otp_navigation_error_recovery")
    state = _submit_email_and_wait_next(driver, email, attempts=2)
    if state == "otp":
        wait_state = _wait_for_otp_input(driver, timeout=30)
        return wait_state if wait_state in {"email_verified", "profile", "logged_in"} else "otp"
    if state == "logged_in":
        return "logged_in"
    advanced = _otp_flow_advanced_state(driver)
    if advanced in ("profile", "logged_in", "email_verified"):
        return advanced

    if _is_signup_password_page(driver):
        logger.warning("%s[OTP] 页面退回创建密码步骤，重新提交密码后恢复验证码页", _log_prefix(driver))
        _fill_password_page_if_present(driver, email, timeout=25)
        wait_state = _wait_for_otp_input(driver, timeout=30)
        return wait_state if wait_state in {"email_verified", "profile", "logged_in"} else "otp"
    raise RuntimeError(
        f"OTP 导航错误恢复后未进入验证码页: state={state}, snapshot={_email_otp_page_state(driver)}"
    )


def _type_otp(driver, code: str) -> None:
    code = str(code or "").strip()
    if not code:
        raise RuntimeError("OTP 为空")

    # 单输入框优先使用 WebDriver 的真实键盘事件。OpenAI 当前 OTP 控件在输入
    # 第 6 位后会自动提交；JS setter 虽能改变视觉值，但 React 表单状态可能仍
    # 保留空值/旧值，表现为邮件里的正确验证码连续被拒绝。
    try:
        from selenium.webdriver.common.by import By

        single_seen: set[str] = set()
        for selector in (
            "input[autocomplete='one-time-code']",
            "input[name='code']",
            "input[inputmode='numeric']",
            "input[type='tel']",
        ):
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    element_id = str(getattr(el, "id", "") or id(el))
                    if element_id in single_seen or not _visible(el):
                        continue
                    single_seen.add(element_id)
                    max_length = str(el.get_attribute("maxlength") or "").strip()
                    if max_length == "1":
                        continue
                    try:
                        el.clear()
                    except Exception:
                        driver.execute_script(
                            "const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;"
                            "if(s)s.call(arguments[0],'');else arguments[0].value='';"
                            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));",
                            el,
                        )
                    # 必须逐字符发送。一次 ``send_keys('001414')`` 在新版 React
                    # OTP 控件上偶尔会在重渲染时吞掉开头的连续 0；随后用 JS setter
                    # 补视觉值又不会同步 React 表单状态，点击继续不会真正提交。
                    for index, character in enumerate(code):
                        el.send_keys(character)
                        if index < len(code) - 1:
                            time.sleep(0.06)
                    # 第 6 位可能立即销毁输入框并导航；send_keys 已成功返回时，
                    # 不再读取旧 WebElement，只从新 DOM 做一次宽松校验。
                    time.sleep(0.2)
                    state = _email_otp_page_state(driver)
                    otp_values = [
                        str(item.get("value") or "")
                        for item in (state.get("inputs") or [])
                        if re.search(
                            r"one-time|otp|code|numeric|tel",
                            " ".join(str(item.get(k) or "") for k in ("type", "name", "id", "autocomplete", "inputmode")),
                            flags=re.IGNORECASE,
                        )
                    ]
                    if otp_values and code not in otp_values:
                        logger.warning("%s[OTP] 真实按键输入后 DOM 值不一致，回退原子写入", _log_prefix(driver))
                        break
                    logger.info("%s[OTP] 验证码输入布局=single-keys，已通过真实按键写入", _log_prefix(driver))
                    return
                except Exception as exc:
                    # 自动提交造成 stale/navigation 时，按输入已完成处理；仍停留在
                    # 有空 OTP 输入框的页面才回退下一种写入方式。
                    state = _email_otp_page_state(driver)
                    has_live_otp = any(
                        re.search(
                            r"one-time|otp|code|numeric|tel",
                            " ".join(str(item.get(k) or "") for k in ("type", "name", "id", "autocomplete", "inputmode")),
                            flags=re.IGNORECASE,
                        )
                        for item in (state.get("inputs") or [])
                    )
                    if not has_live_otp:
                        logger.info("%s[OTP] 真实按键输入后页面已自动提交", _log_prefix(driver))
                        return
                    logger.debug(
                        "%s[OTP] 真实按键输入失败，回退原子写入：%s",
                        _log_prefix(driver),
                        redact_otp_text(exc),
                    )
                    break
    except Exception as exc:
        logger.debug(
            "%s[OTP] 初始化真实按键输入失败，回退原子写入：%s",
            _log_prefix(driver),
            redact_otp_text(exc),
        )

    for _ in range(3):
        try:
            result = driver.execute_script(r"""
            const code = String(arguments[0] || '');
            const visible = el => {
              if (!el || el.disabled || el.readOnly) return false;
              const s = getComputedStyle(el), r = el.getBoundingClientRect();
              return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
            };
            const attrs = el => [el.type, el.name, el.id, el.autocomplete, el.inputMode,
              el.getAttribute('aria-label'), el.getAttribute('data-testid')].filter(Boolean).join(' ').toLowerCase();
            const inputs = [...document.querySelectorAll('input')].filter(visible);
            const candidates = inputs.filter(el => /one-time|otp|verification|numeric|passcode/.test(attrs(el)));
            const setValue = (el, value) => {
              const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
              if (setter) setter.call(el, value); else el.value = value;
              el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:String(value)}));
              el.dispatchEvent(new Event('change', {bubbles:true}));
            };
            const boxes = candidates.filter(el => (el.maxLength === 1 || el.getAttribute('maxlength') === '1')).slice(0, code.length);
            // 必须先判断分格框。旧逻辑先取第一个“不带 code-1 后缀”的元素，
            // 会把 6 位验证码整串写进第一个 maxlength=1 的框，页面看到的实际值不完整。
            if (boxes.length >= code.length) {
              boxes.forEach((el, i) => setValue(el, code[i]));
              boxes[boxes.length - 1].focus();
              return {ok:true, mode:'segmented', values:boxes.map(el => el.value)};
            }
            const single = candidates.find(el => el.maxLength >= code.length)
              || (candidates.length === 1 ? candidates[0] : null);
            if (single) {
              setValue(single, code);
              single.focus();
              return {ok:true, mode:'single', values:[single.value]};
            }
            // 有些页面的分格框不声明 maxlength，但会暴露 6 个 numeric/otp 控件。
            if (candidates.length >= code.length) {
              const inferredBoxes = candidates.slice(0, code.length);
              inferredBoxes.forEach((el, i) => setValue(el, code[i]));
              inferredBoxes[inferredBoxes.length - 1].focus();
              return {ok:true, mode:'segmented-inferred', values:inferredBoxes.map(el => el.value)};
            }
            return {ok:false, count:inputs.length, candidates:candidates.length};
            """, code) or {}
            if result.get("ok"):
                values = [str(value or "") for value in (result.get("values") or [])]
                mode = str(result.get("mode") or "unknown")
                actual = values[0] if mode == "single" and values else "".join(values)
                if actual != code:
                    logger.warning(
                        "%s[OTP] 验证码写入后 DOM 校验不一致：layout=%s expected_len=%s actual_len=%s，准备重试",
                        _log_prefix(driver), mode, len(code), len(actual),
                    )
                    time.sleep(0.4)
                    continue
                logger.info(
                    "%s[OTP] 验证码输入布局=%s，已写入 %s 个控件",
                    _log_prefix(driver),
                    mode,
                    len(values),
                )
                return
        except Exception:
            pass
        time.sleep(0.8)

    from selenium.webdriver.common.by import By

    # 单输入框
    for selector in [
        "input[autocomplete='one-time-code']",
        "input[name='code']",
        "input[inputmode='numeric']",
        "input[type='tel']",
    ]:
        els = [e for e in driver.find_elements(By.CSS_SELECTOR, selector) if _visible(e)]
        if len(els) == 1:
            _human_type_text(driver, els[0], code, clear=True)
            return

    # 6 个分格输入框
    boxes = [e for e in driver.find_elements(By.CSS_SELECTOR, "input") if _visible(e)]
    numeric_boxes = []
    for e in boxes:
        attrs = " ".join(str(e.get_attribute(k) or "") for k in ("inputmode", "autocomplete", "aria-label", "name", "id", "type"))
        if any(x in attrs.lower() for x in ("numeric", "one-time", "code", "otp", "tel")):
            numeric_boxes.append(e)
    if len(numeric_boxes) >= len(code):
        for e, ch in zip(numeric_boxes, code):
            if _browser_actions_enabled():
                _human_scroll_to(driver, e)
                time.sleep(random.uniform(0.04, 0.18))
            e.send_keys(ch)
            if _browser_actions_enabled():
                human_delay("keystroke")
        return

    raise RuntimeError("找不到 OTP 输入框")


def _email_otp_page_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', inputmode: el.getAttribute('inputmode') || '',
          ariaInvalid: el.getAttribute('aria-invalid') || '', value: el.value || ''
        }));
        const buttons = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')].filter(visible).map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', value: el.getAttribute('value') || '',
          action: el.getAttribute('data-dd-action-name') || '', aria: el.getAttribute('aria-label') || '',
          disabled: !!el.disabled || String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)
        }));
        const errors = [...document.querySelectorAll('.react-aria-FieldError,[slot="errorMessage"],[id$="-error"],[aria-invalid="true"] + *,[class*="error"]')]
          .filter(visible).map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
        return {url: location.href, title: document.title, inputs, buttons, errors, text: (document.body?.innerText || '').slice(0, 1200)};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, 'current_url', ''), "error": f"{type(exc).__name__}: {exc}"}


def _is_email_verification_page(driver) -> bool:
    try:
        url = str(driver.current_url or '').lower()
    except Exception:
        url = ''
    if '/log-in/password' in url:
        return False
    if 'email-verification' in url:
        return True
    state = _email_otp_page_state(driver)
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete','inputmode')) for i in (state.get('inputs') or [])).lower()
    return 'one-time-code' in attrs or 'otp' in attrs or 'code' in attrs


def _email_otp_terminal_error(state: dict | None) -> str | None:
    """识别验证码提交后的不可恢复账号错误，避免把它当成验证码错误重试。"""
    state = state or {}
    page_text = " ".join(
        str(value or "")
        for value in (
            state.get("title"),
            state.get("text"),
            " ".join(str(item or "") for item in (state.get("errors") or [])),
        )
    ).lower()
    normalized = re.sub(r"[\s-]+", "_", page_text)
    if "account_deactivated" in normalized:
        return "account_deactivated"
    return None


def _email_otp_verified_success(state: dict | None) -> bool:
    """Recognize the terminal confirmation page shown after a successful OTP."""
    state = state or {}
    page_text = " ".join(
        str(value or "")
        for value in (state.get("title"), state.get("text"))
    ).lower()
    return (
        "email verified" in page_text
        or "email has been verified" in page_text
        or "already been verified" in page_text
        or "メールが確認されました" in page_text
        or "すでに確認済み" in page_text
        or "邮箱已验证" in page_text
        or "電子郵件已驗證" in page_text
        or "이메일이 확인되었습니다" in page_text
    )


def _clear_otp_inputs(driver) -> None:
    try:
        driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).filter(el => {
          const attrs = [el.type, el.name, el.id, el.autocomplete, el.inputMode, el.getAttribute('aria-label')].join(' ').toLowerCase();
          return /one-time|otp|code|numeric|tel/.test(attrs);
        });
        for (const el of inputs) {
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(el, ''); else el.value = '';
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """)
    except Exception:
        pass


def _wait_for_resend_acknowledgement(
    driver,
    *,
    before_state: dict | None = None,
    timeout: int = 4,
) -> bool:
    """Confirm that a resend click changed the live OTP page state."""
    before_state = before_state or {}
    initial_url = str(before_state.get("url") or getattr(driver, "current_url", "") or "")
    before_text = str(before_state.get("text") or "")
    end = time.time() + max(1, int(timeout))
    status_pattern = re.compile(
        r"sent|sent successfully|again in|resend in|wait(?:ing)?|seconds?|try again later",
        flags=re.IGNORECASE,
    )
    resend_pattern = re.compile(r"resend|send.*new|new.*code|send.*again|retry|try.?again", flags=re.IGNORECASE)

    while time.time() < end:
        state = _email_otp_page_state(driver)
        current_url = str(state.get("url") or getattr(driver, "current_url", "") or "")
        if initial_url and current_url and current_url != initial_url and "email-verification" not in current_url.lower():
            return True
        if not _is_email_verification_page(driver):
            return True

        buttons = state.get("buttons") or []
        resend_buttons = []
        for button in buttons:
            marker = " ".join(str(button.get(key) or "") for key in ("action", "aria", "text", "value", "type"))
            if resend_pattern.search(marker):
                resend_buttons.append(button)
        if any(bool(button.get("disabled")) for button in resend_buttons):
            return True

        button_text = " ".join(str(button.get("text") or "") for button in resend_buttons)
        page_text = str(state.get("text") or "")
        if status_pattern.search(button_text) and button_text.lower() != "resend email":
            return True
        if page_text != before_text and status_pattern.search(page_text):
            return True
        time.sleep(0.25)
    return False


def _reload_stuck_otp_page(driver, *, timeout: int = 30) -> str:
    """Reload the visible OTP page once when resend has no observable acknowledgement."""
    current_url = str(getattr(driver, "current_url", "") or "")
    lowered_url = current_url.lower()
    if not current_url or not lowered_url.startswith(("https://auth.openai.com/", "https://chatgpt.com/")):
        raise RuntimeError(f"OTP 重发无页面确认且当前页面不可刷新: url={current_url[:180]}")
    logger.warning("%s[OTP] 重发后页面无可见确认，刷新当前登录窗口并重新检查 OTP 页面", _log_prefix(driver))
    _safe_get(
        driver,
        current_url,
        timeout=timeout,
        attempts=1,
        accept_hosts=("chatgpt.com", "auth.openai.com"),
    )
    _page_warmup(driver, reason="otp_resend_stuck_recovery")
    wait_state = _wait_for_otp_input(driver, timeout=20)
    return "email_verified" if wait_state == "email_verified" else "otp"


def _click_resend_email_otp(driver, timeout: int = 20) -> dict:
    """点击重新发送邮箱验证码。优先按 DOM 属性识别，文本仅兜底。"""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            before_state = _email_otp_page_state(driver)
            atomic = driver.execute_script(r"""
            const visible = el => !!el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
            const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
            const all = [...document.querySelectorAll('button,a,[role=button],[role=link],input[type=button],input[type=submit]')].filter(visible);
            const target = all.find(el => {
              if (!enabled(el)) return false;
              const attrs = [el.id, el.getAttribute('name'), el.getAttribute('value'), el.getAttribute('data-testid'), el.getAttribute('data-dd-action-name'), el.getAttribute('aria-label'), el.getAttribute('title')].filter(Boolean).join(' ').toLowerCase();
              const text = String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
              return ((el.getAttribute('name') || '').toLowerCase() === 'intent' && (el.getAttribute('value') || '').toLowerCase() === 'resend')
                || /resend|send.*new|new.*code|send.*again/.test(attrs)
                || /resend|send\s+(?:a\s+)?new\s+code|send\s+again|try\s*again|重新发送|重新发送电子邮件|重发|再次发送|重试|重試|再试|再試|もう一度試|再送信|新しい|届かない/.test(text);
            });
            if (!target) return {ok:false};
            target.scrollIntoView({block:'center'});
            const text = String(target.innerText || target.value || target.getAttribute('aria-label') || '').trim();
            const marker = [text, target.id, target.getAttribute('name'), target.getAttribute('value'),
              target.getAttribute('data-dd-action-name'), target.getAttribute('aria-label'), target.getAttribute('title'),
              target.getAttribute('data-testid')].filter(Boolean).join(' ').toLowerCase();
            const kind = /try.?again|retry|もう一度試|重试|重試|再试|再試/.test(marker) ? 'retry' : 'resend';
            // 点击可能立刻触发 React 路由切换并销毁节点。必须先快照 text/kind，
            // 再点击并直接返回，避免 Selenium 随后读取旧 WebElement 触发 stale。
            target.click();
            return {ok:true, text, kind};
            """) or {}
            if atomic.get("ok"):
                logger.info("%s[OTP] 已通过 DOM 原子点击重新发送按钮：%s", _log_prefix(driver), atomic.get("text") or "-")
                time.sleep(random.uniform(1.1, 2.4) if _browser_actions_enabled() else 1.5)
                atomic["acknowledged"] = _wait_for_resend_acknowledgement(
                    driver,
                    before_state=before_state,
                    timeout=min(4, max(1, timeout)),
                )
                if not atomic["acknowledged"]:
                    logger.warning("%s[OTP] DOM resend click had no disabled/countdown/sent acknowledgement", _log_prefix(driver))
                return atomic
            btn = driver.execute_script(r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
            const candidates = [...document.querySelectorAll('button,a,[role=button],[role=link],input[type=button],input[type=submit]')].filter(visible);
            const attrHit = candidates.find(el => {
              if (!enabled(el)) return false;
              const attrs = [el.id, el.getAttribute('name'), el.getAttribute('value'), el.getAttribute('data-dd-action-name'), el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('data-testid')]
                .join(' ').toLowerCase();
              const name = String(el.getAttribute('name') || '').toLowerCase();
              const value = String(el.getAttribute('value') || '').toLowerCase();
              if (name === 'intent' && value === 'resend') return true;
              return /resend|send.*new|new.*code|again/.test(attrs);
            });
            if (attrHit) return attrHit;
            // 兜底：多语言文本，避免因页面没有稳定属性时卡死。
            return candidates.find(el => enabled(el) && /resend|send\s+(?:a\s+)?new\s+code|send\s+again|重新发送|重新发送电子邮件|重发|再次发送|再送信|新しい|届かない/.test((el.innerText || el.textContent || '').toLowerCase())) || null;
            """)
            if btn:
                text = str(btn.text or btn.get_attribute('value') or btn.get_attribute('data-dd-action-name') or '').strip()
                _human_click(driver, btn, label="resend_otp")
                logger.info("%s[OTP] 已点击重新发送验证码按钮：%s", _log_prefix(driver), text or '-')
                time.sleep(random.uniform(1.1, 2.4) if _browser_actions_enabled() else 1.5)
                marker = " ".join(filter(None, [
                    text,
                    str(btn.get_attribute('id') or ''),
                    str(btn.get_attribute('name') or ''),
                    str(btn.get_attribute('value') or ''),
                    str(btn.get_attribute('data-dd-action-name') or ''),
                    str(btn.get_attribute('aria-label') or ''),
                    str(btn.get_attribute('title') or ''),
                    str(btn.get_attribute('data-testid') or ''),
                ])).lower()
                kind = "retry" if re.search(r"try.?again|retry|もう一度試|重试|重試|再试|再試", marker) else "resend"
                acknowledged = _wait_for_resend_acknowledgement(
                    driver,
                    before_state=before_state,
                    timeout=min(4, max(1, timeout)),
                )
                if not acknowledged:
                    logger.warning("%s[OTP] Selenium resend click had no disabled/countdown/sent acknowledgement", _log_prefix(driver))
                return {"ok": True, "text": text, "kind": kind, "acknowledged": acknowledged}
        except Exception as exc:
            last = exc
        time.sleep(0.5)
    raise RuntimeError(f"找不到可点击的重新发送验证码按钮: last={last}, state={_email_otp_page_state(driver)}")


def _prepare_next_email_otp_attempt(driver, email: str) -> str:
    """为下一轮 OTP 恢复正确页面，返回 otp/profile/logged_in。

    OpenAI 拒绝验证码后可能直接把页面送回 ``/log-in`` 的空邮箱表单。
    这种情况下不能继续寻找 OTP 页的“重新发送”按钮，必须重新提交邮箱。
    """
    if _is_browser_navigation_error(driver):
        return _restart_email_otp_from_login(driver, email)

    terminal_error = _email_otp_terminal_error(_email_otp_page_state(driver))
    if terminal_error:
        raise RuntimeError(
            f"OpenAI 返回 {terminal_error}：该邮箱对应的账号已删除或停用，禁止继续注册"
        )

    advanced = _otp_flow_advanced_state(driver)
    if advanced in ("profile", "logged_in", "email_verified"):
        return advanced

    if _is_signup_password_page(driver):
        logger.warning("%s[OTP] 页面退回创建密码步骤，重新提交密码后恢复验证码页", _log_prefix(driver))
        _fill_password_page_if_present(driver, email, timeout=25)
        wait_state = _wait_for_otp_input(driver, timeout=30)
        return wait_state if wait_state in {"email_verified", "profile", "logged_in"} else "otp"

    if _is_email_login_page_still_present(driver):
        logger.warning("%s[OTP] 页面已退回邮箱登录页，重新填写邮箱并申请新验证码", _log_prefix(driver))
        state = _submit_email_and_wait_next(driver, email, attempts=2)
        if state == "otp":
            wait_state = _wait_for_otp_input(driver, timeout=30)
            return wait_state if wait_state in {"email_verified", "profile", "logged_in"} else "otp"
        if state == "logged_in":
            return "logged_in"
        advanced = _otp_flow_advanced_state(driver)
        if advanced in ("profile", "logged_in"):
            return advanced
        raise RuntimeError(
            f"OTP 重试后重新提交邮箱未进入验证码页: state={state}, snapshot={_email_otp_page_state(driver)}"
        )

    if _is_email_verification_page(driver):
        action = _click_resend_email_otp(driver, timeout=25)
        if action.get("acknowledged") is False:
            reloaded_state = _reload_stuck_otp_page(driver)
            if reloaded_state in ("profile", "logged_in", "email_verified"):
                return reloaded_state
            retry_action = _click_resend_email_otp(driver, timeout=15)
            if retry_action.get("acknowledged") is False:
                raise RuntimeError("OTP resend had no acknowledgement after one page reload; stopping blind polling")
            action = retry_action
        # “Try again / もう一度試す”不是重发验证码。它会离开错误页并回到
        # 空邮箱表单；先观察跳转，再决定是继续等 OTP 还是重新提交邮箱。
        if action.get("kind") == "retry":
            end = time.time() + 30
            while time.time() < end:
                advanced = _otp_flow_advanced_state(driver)
                if advanced in ("profile", "logged_in"):
                    return advanced
                if _is_email_login_page_still_present(driver):
                    logger.warning("%s[OTP] 点击再试一次后回到邮箱登录页，重新填写邮箱并申请新验证码", _log_prefix(driver))
                    state = _submit_email_and_wait_next(driver, email, attempts=2)
                    if state == "otp":
                        wait_state = _wait_for_otp_input(driver, timeout=30)
                        return wait_state if wait_state in {"email_verified", "profile", "logged_in"} else "otp"
                    if state == "logged_in":
                        return "logged_in"
                    raise RuntimeError(
                        f"OTP 再试后重新提交邮箱未进入验证码页: state={state}, snapshot={_email_otp_page_state(driver)}"
                    )
                if _is_email_verification_page(driver):
                    snapshot = _email_otp_page_state(driver)
                    if _email_otp_verified_success(snapshot):
                        return "email_verified"
                    has_input = bool(snapshot.get("inputs"))
                    has_error = bool(snapshot.get("errors")) or any(
                        str(i.get("ariaInvalid") or "").lower() == "true"
                        for i in (snapshot.get("inputs") or [])
                    )
                    if has_input and not has_error:
                        return "otp"
                time.sleep(0.8)
        try:
            wait_state = _wait_for_otp_input(driver, timeout=30)
            if wait_state in {"email_verified", "profile", "logged_in"}:
                return wait_state
        except RuntimeError as exc:
            snapshot = _email_otp_page_state(driver)
            snapshot_url = str(snapshot.get("url") or "").lower()
            advanced = _otp_flow_advanced_state(driver)
            if advanced in ("profile", "logged_in", "email_verified"):
                return advanced
            if (
                _is_browser_navigation_error(driver)
                or snapshot_url.startswith("chrome-error://")
                or "chrome-error://" in str(exc).lower()
                or "等待 otp 输入框超时" in str(exc).lower()
            ):
                return _restart_email_otp_from_login(driver, email)
            raise
        return "otp"

    raise RuntimeError(f"OTP 重试时页面既不是验证码页也不是邮箱登录页: state={_email_otp_page_state(driver)}")


def _wait_after_email_otp_submit(driver, timeout: int = 45) -> str:
    """提交 OTP 后等待页面状态，并区分成功前进与退回邮箱登录页。"""
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        time.sleep(0.5)
        if not _is_email_verification_page(driver):
            if _is_email_login_page_still_present(driver):
                return 'email_login'
            return 'accepted'
        last = _email_otp_page_state(driver)
        terminal_error = _email_otp_terminal_error(last)
        if terminal_error:
            logger.error(
                "%s[OTP] OpenAI 返回不可恢复账号错误：%s；停止验证码重试",
                _log_prefix(driver),
                redact_otp_text(terminal_error),
            )
            return terminal_error
        if _email_otp_verified_success(last):
            return 'email_verified'
        invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in (last.get('inputs') or []))
        if invalid or (last.get('errors') or []):
            return 'invalid'
    if _is_email_verification_page(driver):
        last = _email_otp_page_state(driver)
        if _email_otp_verified_success(last):
            return 'email_verified'
        logger.info(
            "%s[OTP] 提交后仍在验证码页且没有明确错误，保持 pending 状态 snapshot=%s",
            _log_prefix(driver),
            redact_otp_text(last),
        )
        return 'pending'
    if _is_email_login_page_still_present(driver):
        return 'email_login'
    return 'accepted'


def _require_confirmed_otp_submit(outcome: str, observed_seconds: int) -> str:
    """Never convert an unchanged verification form into a successful OTP submit."""
    if outcome == "pending":
        raise RuntimeError(
            f"OTP submit stayed on the verification page for {max(0, int(observed_seconds))}s "
            "without an acceptance signal; profile progression was stopped"
        )
    return outcome


def _reload_and_resubmit_otp_once(driver, otp: str, *, timeout: int = 6) -> str:
    """参考 FlowPilot 的 unchanged-form 恢复：刷新、重填、再提交一次。"""
    current_url = str(getattr(driver, "current_url", "") or "")
    if "email-verification" not in current_url.lower():
        return "accepted"
    logger.warning("%s[OTP] Continue 后表单未变化，刷新验证页并复用同一验证码重试一次", _log_prefix(driver))
    try:
        driver.refresh()
    except Exception:
        _safe_get(
            driver,
            current_url,
            timeout=max(3, int(timeout)),
            attempts=1,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
    ready = _wait_for_otp_input(driver, timeout=max(2, min(6, int(timeout))))
    if ready == "email_verified":
        return "email_verified"
    _clear_otp_inputs(driver)
    _type_otp(driver, otp)
    _click_continue(driver)
    logger.info("%s[OTP] 刷新后已重新填写并提交验证码", _log_prefix(driver))
    return "submitted"


def _click_continue(driver) -> None:
    """只点击 OTP 表单自己的提交按钮，禁止在跳转后的登录页误点第三方登录。"""
    _click_any(driver, [
        "button[data-dd-action-name='Continue']",
        "form:has(input[autocomplete='one-time-code']) button[type='submit']",
        "form:has(input[name='code']) button[type='submit']",
        "//input[@name='code']/ancestor::form[1]//button[@type='submit']",
        "//input[@autocomplete='one-time-code']/ancestor::form[1]//button[@type='submit']",
    ], timeout=4)


def _maybe_accept(driver) -> None:
    # 只处理明确的 cookie/consent 弹层按钮；不要用 “Continue” 兜底，
    # 非日本出口时 “Continue with Google” 也会命中，导致误点 Google 登录。
    for selectors in ([
        "button#onetrust-accept-btn-handler",
        "button[data-testid='cookie-accept']",
        "button[data-testid='accept-cookies']",
        "//button[contains(., 'Accept')]",
        "//button[contains(., '同意')]",
        "//button[contains(., 'Agree')]",
    ],):
        try:
            _click_any(driver, selectors, timeout=3)
            time.sleep(0.5)
        except Exception:
            pass


def _page_snapshot(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const inputs = [...document.querySelectorAll('input,select,textarea')].map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', placeholder: el.getAttribute('placeholder') || '',
          autocomplete: el.getAttribute('autocomplete') || '', aria: el.getAttribute('aria-label') || '',
          value: el.value || '', visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).filter(x => x.visible).slice(0, 30);
        const buttons = [...document.querySelectorAll('button,a[role=button],input[type=submit]')].map(el => ({
          text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim(),
          type: el.getAttribute('type') || '', visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
          disabled: !!el.disabled
        })).filter(x => x.visible).slice(0, 30);
        const widgets = [...document.querySelectorAll('[role=spinbutton], .react-aria-Select, [data-testid="hidden-select-container"] select')].map(el => ({
          tag: el.tagName, role: el.getAttribute('role') || '', dataType: el.getAttribute('data-type') || '',
          aria: el.getAttribute('aria-label') || '', text: (el.innerText || el.textContent || '').trim().slice(0, 80),
          visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).slice(0, 30);
        return {url: location.href, title: document.title, text: (document.body?.innerText || '').slice(0, 2000), inputs, buttons, widgets};
        """) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": getattr(driver, 'current_url', '')}


def _has_access_token(driver) -> bool:
    try:
        if "chatgpt.com" not in str(driver.current_url or "").lower():
            return False
    except Exception:
        return False
    try:
        result = driver.execute_async_script(r"""
        const timeoutMs = Number(arguments[0]) || 6000;
        const done = arguments[1];
        const controller = new AbortController();
        let finished = false;
        const finish = value => {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          done(Boolean(value));
        };
        const timer = setTimeout(() => { controller.abort(); finish(false); }, timeoutMs);
        fetch('/api/auth/session', {
          credentials:'include', cache:'no-store', signal:controller.signal,
          headers:{'accept':'application/json','cache-control':'no-cache'}
        })
          .then(r => r.json()).then(j => finish(j && j.accessToken))
          .catch(() => finish(false));
        """, _session_request_timeout_ms())
        return bool(result)
    except Exception:
        return False


def _is_profile_like(snapshot: dict) -> bool:
    """资料页识别：兼容 about-you/profile；年龄/生日控件可能不是 input，而是 React Aria widget。"""
    url = str(snapshot.get('url') or '').lower()
    inputs = snapshot.get('inputs') or []
    widgets = snapshot.get('widgets') or []
    attrs = ' '.join(
        ' '.join(str(i.get(k) or '') for k in ('name', 'id', 'placeholder', 'autocomplete', 'aria', 'type')).lower()
        for i in inputs
    )
    widget_attrs = ' '.join(
        ' '.join(str(i.get(k) or '') for k in ('role', 'dataType', 'aria', 'text', 'tag')).lower()
        for i in widgets
    )
    has_profile_url = any(x in url for x in ('about-you', 'profile', 'signup/profile', 'create-account/profile'))
    has_name_field = (
        'autocomplete name' in attrs
        or ' name ' in f' {attrs} '
        or 'fullname' in attrs
        or 'full_name' in attrs
        or 'firstname' in attrs
        or 'lastname' in attrs
    )
    has_age_or_birth_field = any(x in f' {attrs} {widget_attrs} ' for x in (
        ' age', '-age', '_age', 'birth', 'birthday', 'birthdate',
        ' month', '-month', '_month', 'data-type month',
        ' day', '-day', '_day', 'data-type day',
        ' year', '-year', '_year', 'data-type year',
        'spinbutton', 'react-aria-select', 'type number',
    ))
    # about-you/profile URL 本身已经足够强；部分新版页面会用无 name 的 React Aria 控件。
    return has_profile_url and (has_name_field or has_age_or_birth_field or bool(inputs) or bool(widgets))


def _otp_flow_advanced_state(driver) -> str | None:
    """判断 OTP 流程是否前进，或是否被退回邮箱登录页。"""
    if _is_email_verification_page(driver):
        if _email_otp_verified_success(_email_otp_page_state(driver)):
            return "email_verified"
        return None
    snapshot = _page_snapshot(driver)
    if _is_profile_like(snapshot):
        return "profile"
    if _has_access_token(driver):
        return "logged_in"
    if _is_email_login_page_still_present(driver):
        return "email_login"
    return None


def _set_element_value(driver, el, value: str) -> None:
    """兼容 React 受控输入框：用原生 setter 设置值并派发 input/change。"""
    driver.execute_script(r"""
    const el = arguments[0];
    const value = String(arguments[1]);
    const tag = (el.tagName || '').toLowerCase();
    el.scrollIntoView({block:'center'});
    el.focus();
    if (tag === 'select') {
      el.value = value;
    } else {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, value);
      else el.value = value;
    }
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.blur();
    """, el, value)


def _select_or_type(driver, selectors: list[str], value: str, timeout: int = 3) -> bool:
    try:
        el = _find_any(driver, selectors, timeout=timeout)
    except Exception:
        return False
    try:
        tag = (el.tag_name or '').lower()
        if tag == 'select':
            if el.__class__.__name__ == 'CloakElement':
                driver.execute_script(r"""
                const el = arguments[0], value = String(arguments[1]);
                const n = parseInt(value, 10);
                const opts = [...el.options];
                const match = opts.find(o => o.value === value)
                  || opts.find(o => (o.textContent || '').trim() === value)
                  || opts[Math.max(0, n - 1)];
                if (match) el.value = match.value; else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                """, el, str(value))
            else:
                from selenium.webdriver.support.ui import Select
                sel = Select(el)
                try:
                    sel.select_by_value(str(int(value)))
                except Exception:
                    try:
                        sel.select_by_visible_text(str(int(value)))
                    except Exception:
                        # 月份 select 可能是 0-based，也可能是 1-based；先 value/text，不行再 index。
                        sel.select_by_index(max(0, int(value)-1))
                driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", el)
        else:
            _human_type_text(driver, el, str(value), clear=True)
        return True
    except Exception as exc:
        logger.debug('%s 填写字段失败 selectors=%s value=%s err=%s', _log_prefix(driver), selectors, value, exc)
        return False


def _fill_birthday_or_age(driver, birthday: str, age: int) -> str | None:
    """填写 about-you 的年龄/生日控件。

    参考 FlowPilot：优先处理直接年龄 input；否则兼容 hidden birthday/date、原生年月日
    select/input、React Aria hidden native select、role=spinbutton[data-type=year/month/day]。
    返回 age / birthday / ymd / react_select / spinbutton / None。
    """
    y, m, d = birthday.split('-')
    result = driver.execute_script(r"""
    const birthday = String(arguments[0]);
    const year = String(arguments[1]);
    const month = String(Number(arguments[2]));
    const month2 = String(arguments[2]).padStart(2, '0');
    const day = String(Number(arguments[3]));
    const day2 = String(arguments[3]).padStart(2, '0');
    const age = String(arguments[4]);
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const setValue = (el, value) => {
      if (!el) return false;
      el.scrollIntoView?.({block:'center'});
      el.focus?.();
      const tag = (el.tagName || '').toLowerCase();
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype
        : tag === 'select' ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, String(value)); else el.value = String(value);
      if (tag === 'select') {
        [...el.options].forEach(opt => { opt.selected = String(opt.value) === String(value); });
      }
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      el.blur?.();
      return true;
    };
    const ageInput = [...document.querySelectorAll('input[name="age"], input#age, input[id$="-age"], input[type="number"]')]
      .find(visible);
    if (ageInput && setValue(ageInput, age)) return {ok:true, mode:'age'};

    const dateInput = [...document.querySelectorAll('input[name="birthdate"], input[type="date"], input[name="birthday"]')]
      .find(el => visible(el) || String(el.getAttribute('type') || '').toLowerCase() === 'date');
    if (dateInput && setValue(dateInput, birthday)) return {ok:true, mode:'birthday'};

    const setFirst = (selectors, values) => {
      for (const sel of selectors) {
        for (const el of [...document.querySelectorAll(sel)]) {
          if (!visible(el)) continue;
          for (const val of values) {
            if (el.tagName === 'SELECT') {
              const has = [...el.options].some(o => String(o.value) === String(val) || String(o.textContent || '').trim() === String(val));
              if (!has) continue;
            }
            if (setValue(el, val)) return true;
          }
        }
      }
      return false;
    };
    const yOk = setFirst(['select[name="year"]','input[name="year"]','select[id*="year"]','input[id*="year"]'], [year]);
    const mOk = setFirst(['select[name="month"]','input[name="month"]','select[id*="month"]','input[id*="month"]'], [month, month2]);
    const dOk = setFirst(['select[name="day"]','input[name="day"]','select[id*="day"]','input[id*="day"]'], [day, day2]);
    if (yOk && mOk && dOk) {
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'ymd'};
    }

    // React Aria Select 通常有 hidden native select；不依赖标签文字，按 option 数值范围和 DOM 顺序推断年/月/日。
    const selects = [...document.querySelectorAll('[data-testid="hidden-select-container"] select, .react-aria-Select select, select')]
      .filter(el => !el.disabled);
    const nums = sel => [...sel.options].map(o => Number(o.value)).filter(Number.isFinite);
    const maxNum = sel => Math.max(...nums(sel), -Infinity);
    const minNum = sel => Math.min(...nums(sel), Infinity);
    const hasOption = (sel, val) => [...sel.options].some(o => String(o.value) === String(val));
    const yearSelects = selects.filter(sel => hasOption(sel, year) && maxNum(sel) > 1900);
    const smallSelects = selects.filter(sel => !yearSelects.includes(sel));
    const monthSelects = smallSelects.filter(sel => (hasOption(sel, month) || hasOption(sel, month2)) && minNum(sel) <= 1 && maxNum(sel) <= 12);
    const daySelects = smallSelects.filter(sel => (hasOption(sel, day) || hasOption(sel, day2)) && maxNum(sel) >= 28);
    if (yearSelects.length && monthSelects.length && daySelects.length) {
      const ys = yearSelects[0];
      let ms = monthSelects[0];
      let ds = daySelects.find(x => x !== ms) || daySelects[0];
      setValue(ys, year);
      setValue(ms, hasOption(ms, month) ? month : month2);
      setValue(ds, hasOption(ds, day) ? day : day2);
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'react_select'};
    }

    const spinYear = document.querySelector('[role="spinbutton"][data-type="year"]');
    const spinMonth = document.querySelector('[role="spinbutton"][data-type="month"]');
    const spinDay = document.querySelector('[role="spinbutton"][data-type="day"]');
    if (spinYear && spinMonth && spinDay) return {ok:false, mode:'spinbutton_needed'};
    return {ok:false, mode:'missing'};
    """, birthday, y, m, d, str(age)) or {}
    if result.get('ok'):
        return str(result.get('mode') or 'birthday')
    if result.get('mode') != 'spinbutton_needed':
        return None

    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        mod = Keys.COMMAND
        try:
            import platform
            if platform.system().lower() != 'darwin':
                mod = Keys.CONTROL
        except Exception:
            pass
        for selector, value in [
            ('[role="spinbutton"][data-type="year"]', y),
            ('[role="spinbutton"][data-type="month"]', str(m).zfill(2)),
            ('[role="spinbutton"][data-type="day"]', str(d).zfill(2)),
        ]:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", el)
            time.sleep(0.1)
            el.send_keys(mod, 'a')
            time.sleep(0.05)
            el.send_keys(str(value))
            time.sleep(0.1)
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true})); arguments[0].dispatchEvent(new Event('change', {bubbles:true})); arguments[0].blur();", el)
        driver.execute_script(r"""
        const hidden = document.querySelector('input[name="birthday"]');
        if (hidden) {
          const value = arguments[0];
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(hidden, value); else hidden.value = value;
          hidden.dispatchEvent(new Event('input', {bubbles:true}));
          hidden.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """, birthday)
        return 'spinbutton'
    except Exception as exc:
        logger.debug('%s spinbutton 生日填写失败：%s', _log_prefix(driver), exc)
        return None


def _generate_roxy_password() -> str:
    """参考 FlowPilot 密码策略：8~64 位，含大小写、数字、符号。"""
    upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    lower = 'abcdefghjkmnpqrstuvwxyz'
    digits = '23456789'
    symbols = '!@#$%^&*?_-+=' 
    groups = [upper, lower, digits, symbols]
    all_chars = ''.join(groups)
    chars = [random.choice(g) for g in groups]
    while len(chars) < 14:
        chars.append(random.choice(all_chars))
    random.shuffle(chars)
    return ''.join(chars)


def _registration_password() -> str:
    try:
        from config import register as _register_cfg
        configured = str(getattr(_register_cfg, 'REGISTER_PASSWORD', '') or '').strip()
        if configured:
            return configured
    except Exception:
        pass
    return _generate_roxy_password()


def _password_page_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input')].map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', visible: visible(el), value: el.type === 'password' ? '<password>' : (el.value || '')
        })).slice(0, 30);
        const forms = [...document.querySelectorAll('form')].map(f => ({action: f.getAttribute('action') || ''}));
        const buttons = [...document.querySelectorAll('button,input[type="submit"]')].map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          disabled: !!el.disabled, visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).slice(0, 30);
        const errors = [...document.querySelectorAll('[role="alert"],[aria-live="assertive"],[data-error],.error,.text-error')]
          .filter(visible)
          .map(el => (el.textContent || el.getAttribute('data-error') || '').trim())
          .filter(Boolean)
          .slice(0, 12);
        return {url: location.href, inputs, forms, buttons, errors};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_signup_password_page(driver) -> bool:
    state = _password_page_state(driver)
    url = str(state.get('url') or '').lower()
    if any(x in url for x in ('/create-account/password', '/u/signup/password', '/signup/password')):
        return True
    if '/log-in/password' in url:
        return False
    inputs = state.get('inputs') or []
    return any(
        i.get('visible') and (
            str(i.get('type') or '').lower() == 'password'
            or 'password' in str(i.get('name') or '').lower()
            or str(i.get('autocomplete') or '').lower() == 'new-password'
        )
        for i in inputs
    )


def _is_login_password_page(driver) -> bool:
    try:
        url = str(driver.current_url or '').lower()
    except Exception:
        url = ''
    if '/log-in/password' in url:
        return True
    state = _password_page_state(driver)
    url = str(state.get('url') or '').lower()
    return '/log-in/password' in url


def _password_page_targets(driver) -> dict:
    """Return the live password input and its exact form submit control."""
    return driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const input = [...document.querySelectorAll('input[type="password"],input[name*="password" i],input[autocomplete="new-password"]')]
      .find(visible);
    if (!input) return {ok:false, reason:'missing_password_input'};
    const form = input.closest('form');
    const scope = form || document;
    const buttons = [...scope.querySelectorAll('button,input[type="submit"]')]
      .filter(el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
      .map((el, idx) => {
        const r = el.getBoundingClientRect();
        const ir = input.getBoundingClientRect();
        const type = String(el.getAttribute('type') || '').toLowerCase();
        const cls = String(el.className || '').toLowerCase();
        const isSubmit = type === 'submit';
        const isPrimary = /\bbtn-primary\b|\b_primary_|\bw-full\b/.test(cls);
        const dist = Math.max(0, r.top - ir.bottom) + Math.abs((r.left+r.right-ir.left-ir.right)/2)/10;
        const score = (isSubmit ? 1000 : 0) + (isPrimary ? 100 : 0) - dist;
        return {el, idx, type, isSubmit, isPrimary, below: r.top >= ir.bottom - 10, dist, score};
      })
      .filter(x => x.below)
      .sort((a,b) => b.score - a.score || a.idx - b.idx);
    if (!buttons.length) return {ok:false, reason:'missing_submit'};
    buttons[0].el.scrollIntoView({block:'center'});
    return {ok:true, reason:'password_targets', input, button: buttons[0].el, buttonType: buttons[0].type, isSubmit: buttons[0].isSubmit};
    """) or {}


def _click_passwordless_signup_if_present(driver) -> dict:
    """
    新版注册/登录流在 password 页可能默认要求密码。
    如果页面提供“使用一次性验证码”按钮，优先点击进入邮箱 OTP 页面。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
        const norm = s => String(s || '').replace(/\s+/g, '').toLowerCase();
        const candidates = [...document.querySelectorAll('button,a,input[type="submit"],[role="button"],[role="link"]')].filter(el => visible(el) && enabled(el));
        const isPasswordlessOtp = el => {
          const name = String(el.getAttribute('name') || '').toLowerCase();
          const value = String(el.getAttribute('value') || '').toLowerCase();
          const attrs = [
            el.id, name, value, el.getAttribute('aria-label'), el.getAttribute('title'),
            el.getAttribute('data-testid'), el.getAttribute('data-dd-action-name'), el.className, el.textContent
          ].join(' ').toLowerCase();
          const text = norm(el.textContent || el.getAttribute('value') || '');
          return (
            (name === 'intent' && value.includes('passwordless') && value.includes('send_otp')) ||
            (name === 'intent' && value.includes('passwordless') && value.includes('otp')) ||
            (name === 'intent' && value === 'passwordless_signup_send_otp') ||
            (name === 'intent' && value === 'passwordless_login_send_otp') ||
            attrs.includes('passwordless_signup_send_otp') ||
            attrs.includes('passwordless_login_send_otp') ||
            /passwordless.*otp|otp.*passwordless|one[-_\s]?time.*code|code.*one[-_\s]?time/.test(attrs) ||
            text.includes('使用一次性验证码注册') ||
            text.includes('使用一次性验证码登录') ||
            text.includes('使用一次性验证码') ||
            text.includes('使用一次性驗證碼註冊') ||
            text.includes('使用一次性驗證碼登入') ||
            text.includes('一次性验证码') ||
            text.includes('一次性驗證碼') ||
            text.includes('メールでコード') ||
            text.includes('ワンタイムコード') ||
            text.includes('認証コード') ||
            text.includes('useonetimeregistrationcode') ||
            text.includes('useaone-timecodetosignup') ||
            text.includes('useaone-timecodetoregister') ||
            text.includes('useaone-timecodetologin') ||
            text.includes('continuewithaone-timecode') ||
            text.includes('loginwithaone-timecode') ||
            text.includes('signupwithaone-timecode') ||
            text.includes('one-timecode')
          );
        };
        const btn = candidates.find(isPasswordlessOtp);
        if (!btn) return {ok:false, reason:'missing_passwordless_button'};
        btn.scrollIntoView({block:'center'});
        return {
          ok:true,
          reason:'passwordless_send_otp_target',
          button: btn,
          name: btn.getAttribute('name') || '',
          value: btn.getAttribute('value') || '',
          text: (btn.textContent || '').trim().slice(0, 80)
        };
        """) or {"ok": False, "reason": "empty_result"}
        if result.get("ok") and result.get("button"):
            _human_click(driver, result.get("button"), label="passwordless_otp")
            result["reason"] = "clicked_passwordless_send_otp"
            result.pop("button", None)
        return result
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _fill_password_page_if_present(driver, email: str, timeout: int = 25) -> str | None:
    """邮箱提交后兼容 create-account/password。返回本次设置的 OpenAI 账号密码；未遇到密码页返回 None。"""
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        if _is_email_verification_page(driver):
            return None
        if _has_access_token(driver):
            return None
        last = _password_page_state(driver)
        is_signup_password = _is_signup_password_page(driver)
        is_login_password = _is_login_password_page(driver)
        if not (is_signup_password or is_login_password):
            time.sleep(0.5)
            continue
        passwordless = _click_passwordless_signup_if_present(driver)
        if passwordless.get('ok'):
            logger.info("%s 检测到 password 页，已点击一次性验证码入口：email=%s detail=%s", _log_prefix(driver), email, passwordless)
            wait_end = time.time() + 20
            while time.time() < wait_end:
                if _is_email_verification_page(driver):
                    logger.info("%s 一次性验证码入口已进入邮箱验证码页", _log_prefix(driver))
                    return None
                if _has_access_token(driver):
                    logger.info("%s 一次性验证码入口后已检测到登录态", _log_prefix(driver))
                    return None
                last = _password_page_state(driver)
                errors = [str(x) for x in (last.get("errors") or []) if str(x).strip()]
                if errors:
                    raise RuntimeError(f"一次性验证码入口提交被拒绝: errors={errors} state={last}")
                time.sleep(0.5)
            raise RuntimeError(f"一次性验证码入口提交后仍停留在密码页: state={last}")
        if is_login_password:
            raise RuntimeError(f"当前是登录密码页且无一次性验证码入口，邮箱按已注册/不可用处理: state={last}")
        password = _registration_password()
        logger.info("%s 检测到 create-account/password，准备设置密码（%s 位）：email=%s", _log_prefix(driver), len(password), email)
        result = _password_page_targets(driver)
        if not result.get('ok'):
            raise RuntimeError(f"密码页处理失败：{result} state={last}")
        submit_timeout = max(6, int(getattr(_cfg, "ROXY_PASSWORD_SUBMIT_TIMEOUT", 16) or 16))
        submit_attempts = max(1, min(2, int(getattr(_cfg, "ROXY_PASSWORD_SUBMIT_ATTEMPTS", 2) or 2)))
        observe_per_attempt = max(4, submit_timeout // submit_attempts)
        for submit_attempt in range(1, submit_attempts + 1):
            if submit_attempt > 1:
                result = _password_page_targets(driver)
                if not result.get('ok'):
                    raise RuntimeError(f"密码页重试处理失败：{result} state={last}")
                logger.warning("%s 密码提交无响应，重新定位同一表单并进行第 %s/%s 次提交", _log_prefix(driver), submit_attempt, submit_attempts)
            _human_type_text(driver, result.get("input"), password, clear=True)
            human_delay("form", minimum=0.2, maximum=0.8)
            _human_click(driver, result.get("button"), label=f"password_submit_{submit_attempt}")
            logger.info("%s 已填写并提交密码页（%s/%s）", _log_prefix(driver), submit_attempt, submit_attempts)
            wait_end = time.time() + observe_per_attempt
            while time.time() < wait_end:
                if _is_email_verification_page(driver):
                    logger.info("%s 密码提交后已进入邮箱验证码页", _log_prefix(driver))
                    return password
                if _has_access_token(driver):
                    logger.info("%s 密码提交后已检测到登录态", _log_prefix(driver))
                    return password
                if not _is_signup_password_page(driver):
                    return password
                last = _password_page_state(driver)
                errors = [str(x) for x in (last.get("errors") or []) if str(x).strip()]
                if errors:
                    raise RuntimeError(f"创建账号密码提交被拒绝: errors={errors} state={last}")
                time.sleep(0.35)
        raise RuntimeError(f"创建账号密码提交后仍停留在密码页: state={last}")
    logger.info("%s 未检测到密码页，继续后续流程 last=%s", _log_prefix(driver), last)
    return None


def _accept_profile_consents(driver) -> int:
    """about-you/profile 下出现韩国/日本个人信息同意协议时，默认全部勾选。

    不依赖可见文字；优先处理 allCheckboxes，再处理所有必选 consent checkbox。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const isChecked = el => el.checked === true || String(el.getAttribute('aria-checked') || el.closest('[role="checkbox"]')?.getAttribute('aria-checked') || '').toLowerCase() === 'true';
        const mark = el => {
          if (!el || isChecked(el)) return false;
          const label = el.closest('label');
          try {
            (label && visible(label) ? label : el).scrollIntoView({block:'center'});
            (label && visible(label) ? label : el).click();
          } catch (_) {}
          if (!isChecked(el)) {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked')?.set;
            if (setter) setter.call(el, true); else el.checked = true;
            el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
          }
          return isChecked(el);
        };
        const all = [...document.querySelectorAll('input[type="checkbox"]')]
          .filter(el => visible(el) || visible(el.closest('label')));
        if (!all.length) return {count:0, names:[]};
        const byName = name => all.find(el => String(el.name || '').toLowerCase() === name.toLowerCase());
        const ordered = [];
        const add = el => { if (el && !ordered.includes(el)) ordered.push(el); };
        add(byName('allCheckboxes'));
        for (const name of ['personalInfoConsent', 'thirdPartyConsent', 'overseasTransferConsent']) add(byName(name));
        for (const el of all) {
          const n = String(el.name || '').toLowerCase();
          const id = String(el.id || '').toLowerCase();
          if (/consent|checkbox|agree|required|personal|third|overseas/.test(`${n} ${id}`)) add(el);
        }
        // about-you/profile 页面里的 checkbox 基本都是必选 consent；剩余可见 checkbox 也全部勾选。
        for (const el of all) add(el);
        const clicked = [];
        for (const el of ordered) {
          if (mark(el)) clicked.push(el.name || el.id || 'checkbox');
        }
        return {count: clicked.length, names: clicked};
        """) or {}
        count = int(result.get('count') or 0)
        if count:
            logger.info("%s 已勾选 about-you/profile 同意协议复选框：%s", _log_prefix(driver), result.get('names'))
        return count
    except Exception as exc:
        logger.debug('%s 勾选 profile consent 失败：%s', _log_prefix(driver), exc)
        return 0


def _complete_profile_page(driver, name: str, birthday: str, timeout: int = 45) -> bool:
    """等待并完成姓名/生日页；若已经登录成功则返回 False，不把它当失败。"""
    end = time.time() + timeout
    y, m, d = birthday.split('-')
    from datetime import date
    today = date.today()
    age = today.year - int(y) - ((today.month, today.day) < (int(m), int(d)))
    last_snapshot = {}
    stalled_signature = None
    stalled_count = 0
    stall_limit = max(1, int(getattr(_cfg, "ROXY_PROFILE_STALL_LIMIT", 3) or 3))
    while time.time() < end:
        time.sleep(1)
        if _has_access_token(driver):
            logger.info('%s 已检测到登录态，资料页可能已跳过', _log_prefix(driver))
            return False
        snap = _page_snapshot(driver)
        last_snapshot = snap
        if not _is_profile_like(snap):
            logger.info('%s 等待资料页中：url=%s', _log_prefix(driver), snap.get('url'))
            continue

        logger.info('%s 检测到资料页，开始填写姓名生日：url=%s inputs=%s', _log_prefix(driver), snap.get('url'), snap.get('inputs'))
        name_ok = False
        # 常见单姓名字段
        for selectors in [
            ["input[name='name']", "input[name='fullName']", "input[name='full_name']", "input[autocomplete='name']"],
            ["input[placeholder*='Name']", "input[placeholder*='name']", "input[aria-label*='Name']", "input[aria-label*='name']"],
        ]:
            if _select_or_type(driver, selectors, name, timeout=3):
                logger.info("%s 已填写姓名字段：%s", _log_prefix(driver), name)
                name_ok = True
                break
        # 兼容 first/last 分开
        if not name_ok:
            parts = name.split(' ', 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else 'User'
            first_ok = _select_or_type(driver, ["input[name='firstName']", "input[name='first_name']", "input[placeholder*='First']", "input[aria-label*='First']"], first, timeout=2)
            last_ok = _select_or_type(driver, ["input[name='lastName']", "input[name='last_name']", "input[placeholder*='Last']", "input[aria-label*='Last']"], last, timeout=2)
            name_ok = first_ok or last_ok

        birth_mode = _fill_birthday_or_age(driver, birthday, age)
        birth_ok = bool(birth_mode)
        if birth_ok:
            if birth_mode == 'age':
                logger.info("%s 已填写年龄字段：%s", _log_prefix(driver), age)
            else:
                logger.info("%s 已填写生日字段 mode=%s value=%s", _log_prefix(driver), birth_mode, birthday)

        if not name_ok or not birth_ok:
            logger.warning('%s 资料页字段未填完整 name_ok=%s birth_ok=%s snapshot=%s', _log_prefix(driver), name_ok, birth_ok, snap)
            signature = (
                str(snap.get("url") or ""),
                tuple(
                    (str(item.get("name") or ""), str(item.get("type") or ""), str(item.get("value") or ""))
                    for item in (snap.get("inputs") or [])
                ),
                tuple(
                    (str(item.get("role") or ""), str(item.get("dataType") or ""), str(item.get("aria") or ""))
                    for item in (snap.get("widgets") or [])
                ),
                bool(name_ok),
                bool(birth_ok),
            )
            stalled_count = stalled_count + 1 if signature == stalled_signature else 1
            stalled_signature = signature
            if stalled_count >= stall_limit:
                raise RuntimeError(
                    f'资料页连续 {stalled_count} 次没有可操作字段变化，已快速结束；'
                    f'name_ok={name_ok} birth_ok={birth_ok} snapshot={snap}'
                )
            continue

        stalled_signature = None
        stalled_count = 0

        _accept_profile_consents(driver)
        human_delay('form')
        for _ in range(3):
            if _click_if_enabled_submit(driver):
                logger.info('%s 已点击资料页提交按钮，等待 OAuth 跳转', _log_prefix(driver))
                return True
            time.sleep(1)
        logger.warning('%s 找不到可点击的资料页提交按钮 snapshot=%s', _log_prefix(driver), _page_snapshot(driver))
    raise RuntimeError(f'等待/填写资料页超时，最后页面：{last_snapshot}')


def _click_if_enabled_submit(driver) -> bool:
    """提交资料页：优先 form.requestSubmit/button[type=submit]，不依赖按钮文字。"""
    try:
        target = driver.execute_script(r"""
        const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const forms = [...document.querySelectorAll('form')].filter(visible);
        for (const form of forms) {
          const submit = form.querySelector('button[type="submit"], input[type="submit"]');
          if (submit && visible(submit) && !submit.disabled) {
            submit.scrollIntoView({block:'center'});
            return submit;
          }
          if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
            return 'submitted_by_requestSubmit';
          }
        }
        const submitters = [...document.querySelectorAll('button[type="submit"], input[type="submit"]')]
          .filter(el => visible(el) && !el.disabled);
        if (submitters.length) {
          submitters[0].scrollIntoView({block:'center'});
          return submitters[0];
        }
        // 兜底：页面只有一个可点击 button 时点击它，但仍不读文字。
        const buttons = [...document.querySelectorAll('button:not([disabled])')].filter(visible);
        if (buttons.length === 1) {
          buttons[0].scrollIntoView({block:'center'});
          return buttons[0];
        }
        return null;
        """)
        if not target:
            return False
        if isinstance(target, str):
            return True
        _human_click(driver, target, label="profile_submit")
        return True
    except Exception:
        return False


def _read_chatgpt_session_once(driver) -> dict | None:
    """当前页面必须在 chatgpt.com；读取并返回完整 session 响应。"""
    script = r"""
    const timeoutMs = Number(arguments[0]) || 6000;
    const done = arguments[1];
    const controller = new AbortController();
    let finished = false;
    const finish = value => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      done(value);
    };
    const timer = setTimeout(() => {
      controller.abort();
      finish({ok:false, error:'session fetch timeout'});
    }, timeoutMs);
    fetch('/api/auth/session', {
      credentials: 'include',
      cache: 'no-store',
      signal: controller.signal,
      headers: {'accept': 'application/json', 'cache-control': 'no-cache'}
    })
      .then(async r => {
        const text = await r.text();
        let data = null;
        try { data = JSON.parse(text); } catch (_) { data = {text: text.slice(0, 500)}; }
        finish({ok: true, status: r.status, data});
      })
      .catch(e => finish({ok: false, error: String(e)}));
    """
    result = driver.execute_async_script(script, _session_request_timeout_ms())
    if result and result.get("ok"):
        data = result.get("data") or {}
        if not isinstance(data, dict):
            data = {"value": data}
        data.setdefault("_http_status", result.get("status"))
        status = int(result.get("status") or 0)
        if status in (401, 403) and not data.get("accessToken"):
            data["_session_expired"] = True
        if data.get("accessToken"):
            logger.info("%s /api/auth/session 已返回 accessToken", _log_prefix(driver))
        return data
    return {"_error": str((result or {}).get("error") or "session fetch failed")}


def _hard_refresh_chatgpt(driver, *, reason: str) -> None:
    """保留登录 Cookie，无缓存刷新 ChatGPT，让 callback/session cookie 重新落地。"""
    logger.warning("%s %s，立即无缓存刷新 ChatGPT", _log_prefix(driver), reason)
    try:
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Page.reload", {"ignoreCache": True})
        else:
            driver.refresh()
        time.sleep(3)
        return
    except Exception as exc:
        logger.warning("%s 无缓存刷新失败，重新打开 ChatGPT：首页：%s", _log_prefix(driver), str(exc)[:180])
    _safe_get(driver, "https://chatgpt.com/", timeout=35, attempts=2, accept_hosts=("chatgpt.com",))
    time.sleep(3)


def _resume_chatgpt_login_callback(driver, email: str | None = None) -> str:
    """利用 auth.openai.com 已有登录态重新进入 NextAuth callback，不重复注册。"""
    logger.warning("%s 刷新后仍无 accessToken，主动打开 ChatGPT 登录入口恢复 session callback", _log_prefix(driver))
    _safe_get(
        driver,
        "https://chatgpt.com/auth/login",
        timeout=45,
        attempts=2,
        accept_hosts=("chatgpt.com", "auth.openai.com"),
    )
    time.sleep(4)
    if email and _is_email_login_page_still_present(driver):
        logger.warning("%s callback 恢复停在空邮箱登录页，重新提交同一邮箱", _log_prefix(driver))
        return _submit_email_and_wait_next(driver, email, attempts=2)
    advanced = _otp_flow_advanced_state(driver)
    return advanced or "pending"


def _switch_to_chatgpt_window_if_any(driver) -> bool:
    """有些浏览器/适配层会在新窗口完成 callback；尝试切到已有 chatgpt.com 句柄。"""
    try:
        handles = list(getattr(driver, "window_handles", []) or [])
        current_handle = None
        try:
            current_handle = getattr(driver, "current_window_handle", None)
        except Exception:
            current_handle = None
        for handle in handles:
            try:
                driver.switch_to.window(handle)
                if "chatgpt.com" in str(getattr(driver, "current_url", "") or ""):
                    return True
            except Exception:
                continue
        if current_handle is not None:
            try:
                driver.switch_to.window(current_handle)
            except Exception:
                pass
    except Exception:
        pass
    return False


def _fetch_chatgpt_session_once(
    driver,
    timeout: int = 90,
    auto_jump_wait: int = 15,
    stop_check=None,
) -> dict:
    """等待页面完成跳转并从 ChatGPT 页面内读取登录 session/accessToken。

    旧逻辑会在 auth.openai.com 上一直等到总超时，Cloak/部分 Chromium 场景下
    实际账号已创建成功但当前句柄 URL 没及时更新，导致白等 120 秒。现在只给
    自动跳转 `auto_jump_wait` 秒；超过后立即主动打开 chatgpt.com 读 session。
    """
    end = time.time() + timeout
    auto_jump_end = time.time() + max(3, int(auto_jump_wait or 15))
    last_data = None
    forced_chatgpt_open = False
    warning_banner_count = 0

    while time.time() < end:
        if callable(stop_check):
            stop_check()
        try:
            current = str(driver.current_url or '')
        except Exception:
            current = ''

        if 'chatgpt.com' not in current:
            if _switch_to_chatgpt_window_if_any(driver):
                current = str(getattr(driver, "current_url", "") or "")
            elif time.time() >= auto_jump_end and not forced_chatgpt_open:
                try:
                    logger.info("%s 未在 %ss 内观察到当前窗口跳转 chatgpt.com，主动打开 ChatGPT 内读取 session", _log_prefix(driver), int(auto_jump_wait or 15))
                    _safe_get(
                        driver,
                        "https://chatgpt.com/",
                        timeout=max(10, int(getattr(_cfg, "ROXY_SESSION_WAIT_TIMEOUT", 25) or 25)),
                        attempts=1,
                        accept_hosts=("chatgpt.com",),
                    )
                    forced_chatgpt_open = True
                    time.sleep(1)
                    current = str(getattr(driver, "current_url", "") or "")
                except Exception as exc:
                    last_data = f"{type(exc).__name__}: {exc}"
            else:
                time.sleep(1)
                continue

        if 'chatgpt.com' in current:
            try:
                data = _read_chatgpt_session_once(driver)
                if data.get("accessToken"):
                    return data
                last_data = data
                logger.info(
                    "%s 等待 ChatGPT session 写入 accessToken，当前响应 keys=%s status=%s",
                    _log_prefix(driver),
                    list(data.keys()),
                    data.get("_http_status"),
                )
                if data.get("_session_expired"):
                    raise ChatGPTSessionExpiredError(
                        f"/api/auth/session 返回未登录状态: http={data.get('_http_status')}"
                    )
                if "WARNING_BANNER" in data:
                    warning_banner_count += 1
                    if warning_banner_count >= 2:
                        raise ChatGPTSessionExpiredError(
                            "session 连续返回 WARNING_BANNER，判定当前登录态已失效"
                        )
            except ChatGPTSessionExpiredError:
                raise
            except Exception as exc:
                last_data = f"{type(exc).__name__}: {exc}"
        time.sleep(2)

    raise RuntimeError(f"等待 /api/auth/session accessToken 超时，最后响应: {str(last_data)[:800]}")


def _fetch_chatgpt_session(
    driver,
    timeout: int = 90,
    auto_jump_wait: int = 15,
    refresh_attempts: int = 2,
    stop_check=None,
) -> dict:
    """超时后刷新并重新按 DOM/session 信号等待，不因单轮超时直接终止。"""
    last_error = None
    for attempt in range(max(0, int(refresh_attempts)) + 1):
        if callable(stop_check):
            stop_check()
        try:
            return _fetch_chatgpt_session_once(
                driver,
                timeout=timeout,
                auto_jump_wait=auto_jump_wait,
                stop_check=stop_check,
            )
        except ChatGPTSessionExpiredError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= max(0, int(refresh_attempts)):
                break
            logger.warning("%s accessToken 本轮等待结束，刷新页面后继续等待（第 %s/%s 次）：%s", _log_prefix(driver), attempt + 1, refresh_attempts, exc)
            try:
                _hard_refresh_chatgpt(driver, reason=f"accessToken 第 {attempt + 1} 轮等待结束")
            except Exception:
                pass
    raise RuntimeError(f"accessToken 多轮等待仍未完成: {last_error}")


def _recover_chatgpt_session_in_browser(driver, email: str, *, should_stop=None) -> dict:
    """Re-authenticate once in the current visible Roxy window after confirmed logout."""
    if callable(should_stop):
        should_stop()
    logger.warning("%s 检测到登录态失效，切回当前 Roxy 窗口执行一次邮箱 OTP 恢复", _log_prefix(driver))
    otp_after_ts = time.time()
    state = _resume_chatgpt_login_callback(driver, email=email)
    if state == "otp":
        wait_state = _wait_for_otp_input(driver, timeout=30)
        if wait_state == "email_verified":
            state = _resume_chatgpt_login_callback(driver, email=email)
        else:
            state = "otp"
    if state == "otp":
        code = wait_for_otp(
            email,
            after_ts=otp_after_ts,
            max_wait=_ROXY_OTP_MAX_WAIT,
            poll_interval=_ROXY_OTP_POLL_INTERVAL,
            settle_seconds=_ROXY_OTP_SETTLE_SECONDS,
            should_stop=should_stop,
        )
        _type_otp(driver, code)
        if _is_email_verification_page(driver):
            _click_continue(driver)
        outcome = _wait_after_email_otp_submit(
            driver,
            timeout=max(5, int(getattr(_cfg, "ROXY_OTP_SUBMIT_TIMEOUT", 35) or 35)),
        )
        if outcome not in {"accepted", "email_verified", "profile", "logged_in"}:
            raise RuntimeError(f"可见窗口 OTP 恢复未完成: {outcome}")
        if outcome == "email_verified":
            state = _resume_chatgpt_login_callback(driver, email=email)
            if state == "otp":
                raise RuntimeError("Email verified callback still requires OTP")
    elif state not in {"logged_in", "profile", "email_verified"}:
        raise RuntimeError(f"可见窗口登录恢复未进入下一步: {state}")
    return _fetch_chatgpt_session(
        driver,
        timeout=25,
        auto_jump_wait=5,
        refresh_attempts=0,
        stop_check=should_stop,
    )


def _fetch_or_recover_chatgpt_session(
    driver,
    *,
    email: str,
    proxy: str | None,
    registration_created: bool,
    should_stop=None,
    profile_name: str | None = None,
    profile_birthday: str | None = None,
) -> dict:
    """优先从当前浏览器取 AT；已创建账号则用邮箱 OTP 登录恢复 AT。"""
    def stop_check() -> None:
        if callable(should_stop) and should_stop():
            raise RuntimeError("AT 获取已停止")

    # OTP 成功后，auth.openai.com 偶尔会短暂停留在“Email verified / already
    # been verified”确认页，而不是立刻跳回 ChatGPT。这个页面代表邮箱验证
    # 已完成，不应当被当成错误页继续等待 OTP 或直接判定跳转失败。
    try:
        if _otp_flow_advanced_state(driver) == "email_verified":
            logger.info(
                "%s 检测到 Email verified 确认页，主动恢复 ChatGPT OAuth callback",
                _log_prefix(driver),
            )
            _resume_chatgpt_login_callback(driver, email=email)
    except Exception as exc:
        logger.warning(
            "%s Email verified 确认页 callback 恢复失败，继续使用 session 检查：%s",
            _log_prefix(driver), str(exc)[:240],
        )

    try:
        return _fetch_chatgpt_session(
            driver,
            timeout=max(10, int(getattr(_cfg, "ROXY_SESSION_WAIT_TIMEOUT", 25) or 25)),
            auto_jump_wait=max(3, int(getattr(_cfg, "ROXY_SESSION_AUTO_JUMP_WAIT", 8) or 8)),
            refresh_attempts=0,
            stop_check=stop_check if callable(should_stop) else None,
        )
    except Exception as browser_error:
        if not registration_created:
            raise
        if isinstance(browser_error, ChatGPTSessionExpiredError):
            try:
                return _recover_chatgpt_session_in_browser(driver, email, should_stop=should_stop)
            except Exception as visible_error:
                logger.warning(
                    "%s 可见窗口 OTP 恢复失败，改用一次后台协议登录：%s",
                    _log_prefix(driver),
                    str(visible_error)[:240],
                )
        logger.warning(
            "%s 浏览器 session 已失效；停止重复获取 AT，直接执行一次后台邮箱 OTP 登录恢复：%s",
            _log_prefix(driver),
            str(browser_error)[:300],
        )
        if callable(should_stop) and should_stop():
            raise
        from core.account_liveness import check_account_liveness

        recovered = check_account_liveness(
            email,
            proxy=proxy,
            clear_log=False,
            should_stop=should_stop,
            repair_profile_name=profile_name,
            repair_profile_birthday=profile_birthday,
        )
        if recovered.get("ok") and recovered.get("access_token"):
            session_info = dict(recovered.get("session") or {})
            session_info["accessToken"] = recovered["access_token"]
            session_info["_at_recovery"] = "email_otp_relogin"
            logger.info("%s 邮箱 OTP 重新登录成功，已恢复最新 accessToken：%s", _log_prefix(driver), email)
            return session_info
        raise RuntimeError(
            "账号已创建但 AT 恢复失败；"
            f"浏览器 session={str(browser_error)[:220]}；"
            f"邮箱 OTP 重登录={str(recovered.get('error') or recovered.get('status') or 'unknown')[:220]}"
        ) from browser_error


def _check_manual_stop() -> None:
    try:
        from core.registration_service import check_stop_requested
        check_stop_requested()
    except ImportError:
        return


def _registration_stop_requested() -> bool:
    try:
        from core.registration_service import is_stop_requested
        return bool(is_stop_requested())
    except ImportError:
        return False


def _release_roxy_registration_email_failure(
    email: str,
    exc: Exception,
    *,
    create_acknowledged: bool,
) -> str:
    """Apply the pool transition once, before the service observes the result."""
    from core.email_provider import release_email, release_email_if_unconsumed

    error_text = f"{type(exc).__name__}: {exc}"
    lowered = error_text.lower()
    if any(marker in lowered for marker in (
        "account_deactivated", "account_disabled", "account_banned",
    )):
        release_email(email, status="disabled", note=f"Roxy registration failed: {error_text[:180]}")
        return "disabled"
    if create_acknowledged:
        release_email(email, status="failed", note=f"Roxy registration failed after account creation: {error_text[:180]}")
        return "failed"
    mailbox_failure = any(marker in lowered for marker in (
        "genericapimailerror",
        "genericapitransporterror",
        "domainapimailerror",
        "inboxmatemailerror",
    ))
    release_email_if_unconsumed(
        email,
        note=f"Retryable Roxy registration failure: {error_text[:180]}",
        count_failure=mailbox_failure,
    )
    return "mailbox_failure" if mailbox_failure else "available"


def run_roxy_registration(
    email: str,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    skip_proxy_preflight: bool = False,
) -> dict:
    """Roxy 指纹浏览器自动化注册入口。"""
    client = RoxyBrowserClient(profile_proxy=proxy)
    try:
        from core.registration_service import bind_roxy_profile
    except ImportError:
        bind_roxy_profile = None
    opened = client.open_profile(
        on_profile_ready=bind_roxy_profile,
        require_proxy_exit_ip=not skip_proxy_preflight,
        proxy_probe_stop_check=_check_manual_stop,
    )
    driver = None
    traffic_optimizer: RoxyTrafficOptimizer | None = None
    traffic_summary: dict | None = None
    create_acknowledged = False
    openai_password: str | None = None
    registration_exit_geo: dict = {}
    try:
        driver = _build_driver(opened)
        _center_browser_window(driver)
        driver.set_page_load_timeout(int(_cfg.ROXY_SELENIUM_TIMEOUT))
        try:
            driver.set_script_timeout(int(_cfg.ROXY_SELENIUM_TIMEOUT))
        except Exception:
            pass
        configured_exit_attempts = getattr(_cfg, "ROXY_BROWSER_EXIT_IP_ATTEMPTS", 0)
        registration_exit_geo = probe_selenium_driver_exit_geo(
            driver,
            label="Roxy注册",
            restore_page_load_timeout=int(_cfg.ROXY_SELENIUM_TIMEOUT),
            restore_script_timeout=int(_cfg.ROXY_SELENIUM_TIMEOUT),
            attempts=int(configured_exit_attempts if configured_exit_attempts is not None else 0),
            retry_delay=float(getattr(_cfg, "ROXY_BROWSER_EXIT_IP_RETRY_DELAY", 2) or 2),
            stop_check=_check_manual_stop,
        )
        if not registration_exit_geo.get("ip"):
            raise RuntimeError("Roxy 窗口已启动但未能复核实际出口 IP，已终止注册，未继续提交账号")
        logger.info("[Roxy注册] 开始：%s，profile=%s", email, opened.profile_id)
        traffic_optimizer = _start_traffic_optimizer(driver)

        rejected_otps: set[str] = set()
        resolved_email_source = resolve_email_source(email)
        if otp_code is None and resolved_email_source in {"generic_api", "inbox_mate"}:
            try:
                snapshot_current_otp = (
                    __import__("core.inbox_mate_mail_client", fromlist=["snapshot_current_otp"]).snapshot_current_otp
                    if resolved_email_source == "inbox_mate"
                    else __import__("core.generic_api_mail_client", fromlist=["snapshot_current_otp"]).snapshot_current_otp
                )

                historical_otp = snapshot_current_otp(email)
                if historical_otp:
                    rejected_otps.add(str(historical_otp))
                    logger.info(
                        "[Roxy注册][OTP] 已记录并排除取码接口历史验证码：%s",
                        mask_otp(historical_otp),
                    )
            except Exception as exc:
                logger.debug(
                    "[Roxy注册][OTP] 历史验证码快照失败，继续注册：%s",
                    redact_otp_text(exc),
                )

        otp_after_ts = time.time()
        logger.info("[Roxy注册] 打开登录页：https://chatgpt.com/auth/login")
        _safe_get(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=min(45, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        human_delay("navigate")
        _page_warmup(driver, reason="login_page")
        logger.info("[Roxy注册] 登录页加载完成，准备填写邮箱")
        _maybe_accept(driver)
        _check_manual_stop()

        # 填邮箱。OpenAI UI 会随出口 IP/语言变化；这里只按 DOM 技术属性找邮箱入口，
        # 并排除 Google/Apple/Microsoft 等第三方入口，不依赖按钮可见文字。
        try:
            next_state = _submit_email_and_wait_next(
                driver,
                email,
                attempts=max(1, int(getattr(_cfg, "ROXY_EMAIL_SUBMIT_ATTEMPTS", 2) or 2)),
            )
        except RuntimeError as exc:
            if traffic_optimizer is None or not _should_retry_email_entry_without_optimization(driver, exc):
                raise
            next_state = _retry_email_entry_after_traffic_fallback(driver, email, traffic_optimizer)
        _check_manual_stop()

        # 新版注册流可能先进入 /create-account/password；参考 FlowPilot 的 fill-password 步骤，
        # 先设置密码并提交，然后再等待邮箱验证码页。
        openai_password = None if next_state == "otp" else _fill_password_page_if_present(driver, email, timeout=25)
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = _ROXY_OTP_MAX_ATTEMPTS
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info("[Roxy注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(
                        email,
                        after_ts=otp_after_ts,
                        max_wait=_ROXY_OTP_MAX_WAIT,
                        poll_interval=_ROXY_OTP_POLL_INTERVAL,
                        settle_seconds=_ROXY_OTP_SETTLE_SECONDS,
                        exclude_codes=rejected_otps,
                        should_stop=lambda: _otp_flow_advanced_state(driver) is not None,
                    )
                except Exception as exc:
                    advanced_state = _otp_flow_advanced_state(driver)
                    if advanced_state in ("profile", "logged_in", "email_verified"):
                        logger.info("[Roxy注册][OTP] 取码期间页面已进入下一步：%s，停止继续取旧验证码", advanced_state)
                        if advanced_state == "email_verified":
                            callback_state = _resume_chatgpt_login_callback(driver, email=email)
                            if callback_state == "otp":
                                if otp_attempt >= max_otp_attempts:
                                    raise RuntimeError("邮箱验证后 callback 仍要求 OTP，已达到最大重试次数")
                                otp_after_ts = time.time()
                                current_otp = None
                                continue
                        break
                    if otp_attempt >= max_otp_attempts:
                        raise
                    if isinstance(exc, GenericApiTransportError):
                        logger.warning(
                            "[Roxy注册][OTP] 取码端点经短重试仍不可达，停止重发 OTP：%s",
                            redact_otp_text(str(exc)[:240]),
                        )
                        raise
                    if (
                        isinstance(exc, GenericApiMailError)
                        and not bool(getattr(_cfg, "ROXY_OTP_RETRY_ON_MAIL_TIMEOUT", False))
                    ):
                        logger.warning(
                            "[Roxy注册][OTP] 单轮取码已结束，快速模式不再盲目重发 OTP：%s",
                            redact_otp_text(str(exc)[:240]),
                        )
                        raise
                    if advanced_state == "email_login":
                        logger.warning(
                            "[Roxy注册][OTP] 取码期间页面退回邮箱登录页，重新提交邮箱后继续（下一轮 %s/%s）",
                            otp_attempt + 1,
                            max_otp_attempts,
                        )
                    else:
                        logger.warning(
                            "[Roxy注册][OTP] 一直未收到验证码，恢复验证码页面后继续等待（下一轮 %s/%s）：%s: %s",
                            otp_attempt + 1,
                            max_otp_attempts,
                            type(exc).__name__,
                            redact_otp_text(str(exc)[:180]),
                        )
                    otp_after_ts = time.time()
                    if current_otp:
                        rejected_otps.add(str(current_otp))
                    retry_state = _prepare_next_email_otp_attempt(driver, email)
                    if retry_state in ("profile", "logged_in", "email_verified"):
                        if retry_state == "email_verified":
                            callback_state = _resume_chatgpt_login_callback(driver, email=email)
                            if callback_state == "otp":
                                if otp_attempt >= max_otp_attempts:
                                    raise RuntimeError("邮箱验证后 callback 仍要求 OTP，已达到最大重试次数")
                                otp_after_ts = time.time()
                                current_otp = None
                                continue
                        break
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Roxy注册][OTP] 收到验证码：%s", mask_otp(current_otp))
            otp_ready = _wait_for_otp_input(driver, timeout=30)
            if otp_ready == "email_verified":
                logger.info("[Roxy][OTP] Email verified page appeared before OTP input; resuming ChatGPT callback")
                callback_state = _resume_chatgpt_login_callback(driver, email=email)
                if callback_state == "otp":
                    if otp_attempt >= max_otp_attempts:
                        raise RuntimeError("Email verified callback still requires OTP after maximum retries")
                    otp_after_ts = time.time()
                    current_otp = None
                    continue
                break
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            logger.info("[Roxy注册][OTP] 已填写邮箱验证码")
            _check_manual_stop()
            human_delay("otp_input")
            if _is_email_verification_page(driver):
                state_before_submit = _email_otp_page_state(driver)
                has_otp_input = any(
                    re.search(
                        r"one-time|otp|code|numeric|tel",
                        " ".join(str(item.get(k) or "") for k in ("type", "name", "id", "autocomplete", "inputmode")),
                        flags=re.IGNORECASE,
                    )
                    for item in (state_before_submit.get("inputs") or [])
                )
                if has_otp_input:
                    try:
                        _click_continue(driver)
                        logger.info("[Roxy注册][OTP] 已点击 OTP 表单提交按钮，等待资料页或登录态")
                    except Exception as exc:
                        logger.info(
                            "[Roxy注册][OTP] OTP 可能已自动提交，不再跨页面寻找按钮：%s",
                            redact_otp_text(str(exc)[:120]),
                        )
                else:
                    logger.info("[Roxy注册][OTP] 输入后 OTP 控件已消失，判定页面已自动提交")
            else:
                logger.info("[Roxy注册][OTP] 输入后已离开验证码页，判定页面已自动提交")

            otp_submit_timeout = max(5, int(getattr(_cfg, "ROXY_OTP_SUBMIT_TIMEOUT", 15) or 15))
            otp_submit_attempts = max(
                1,
                min(2, int(getattr(_cfg, "ROXY_OTP_SUBMIT_ATTEMPTS", 2) or 2)),
            )
            pending_grace = max(0, int(getattr(_cfg, "ROXY_OTP_PENDING_GRACE", 10) or 0))
            first_observe = otp_submit_timeout
            if otp_submit_attempts > 1:
                first_observe = max(5, otp_submit_timeout // 2)
            outcome = _wait_after_email_otp_submit(driver, timeout=first_observe)
            observed_seconds = first_observe
            if outcome == 'pending' and otp_submit_attempts > 1:
                retry_state = _reload_and_resubmit_otp_once(
                    driver,
                    current_otp,
                    timeout=max(3, otp_submit_timeout - first_observe),
                )
                if retry_state == "email_verified":
                    outcome = "email_verified"
                elif retry_state == "accepted":
                    outcome = "accepted"
                else:
                    retry_observe = max(4, otp_submit_timeout - first_observe)
                    outcome = _wait_after_email_otp_submit(driver, timeout=retry_observe)
                    observed_seconds += retry_observe
            if outcome == 'pending':
                if pending_grace:
                    logger.info("[Roxy][OTP] No explicit error; waiting %s more seconds before any resend", pending_grace)
                    outcome = _wait_after_email_otp_submit(driver, timeout=pending_grace)
                    observed_seconds += pending_grace
                if outcome == 'pending':
                    logger.info(
                        "[Roxy][OTP] 等待预算结束仍无接受信号；停止误进入资料页"
                    )
            outcome = _require_confirmed_otp_submit(
                outcome,
                observed_seconds,
            )
            if outcome == 'accepted':
                break
            if outcome == 'email_verified':
                logger.info("[Roxy注册][OTP] 邮箱已验证，重新进入 ChatGPT 登录入口完成回调")
                callback_state = _resume_chatgpt_login_callback(driver, email=email)
                if callback_state == "otp":
                    if otp_attempt >= max_otp_attempts:
                        raise RuntimeError("邮箱验证后 callback 仍要求 OTP，已达到最大重试次数")
                    otp_after_ts = time.time()
                    current_otp = None
                    continue
                break
            if outcome == 'account_deactivated':
                raise RuntimeError(
                    "OpenAI 返回 account_deactivated：该邮箱对应的账号已删除或停用，禁止继续注册"
                )
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            if outcome == 'email_login':
                logger.warning("[Roxy注册][OTP] 验证码被拒绝且页面退回邮箱登录页，准备重新提交邮箱（%s/%s）", otp_attempt + 1, max_otp_attempts)
            else:
                logger.warning("[Roxy注册][OTP] 验证码错误/过期，准备重新发送并重新获取验证码（%s/%s）", otp_attempt + 1, max_otp_attempts)
            otp_after_ts = time.time()
            if current_otp:
                rejected_otps.add(str(current_otp))
            retry_state = _prepare_next_email_otp_attempt(driver, email)
            if retry_state in ("profile", "logged_in", "email_verified"):
                if retry_state == "email_verified":
                    callback_state = _resume_chatgpt_login_callback(driver, email=email)
                    if callback_state == "otp":
                        if otp_attempt >= max_otp_attempts:
                            raise RuntimeError("邮箱验证后 callback 仍要求 OTP，已达到最大重试次数")
                        otp_after_ts = time.time()
                        current_otp = None
                        continue
                break
            human_delay("api")
            current_otp = None

        # about-you / profile 信息页：必须完成或确认已有登录态，不能静默跳过。
        logger.info("[Roxy注册] 开始等待资料页/登录态")
        _check_manual_stop()
        profile_submitted = _complete_profile_page(
            driver,
            name,
            birthday,
            timeout=max(10, int(getattr(_cfg, "ROXY_PROFILE_TIMEOUT", 35) or 35)),
        )
        if profile_submitted:
            create_acknowledged = True
            # 给 OAuth 回调 / session cookie 写入一点时间。
            human_delay("post_auth")

        if traffic_optimizer is not None:
            traffic_optimizer.set_session_only(True)

        logger.info("[Roxy注册] 等待 ChatGPT 跳转并写入 session/accessToken")
        _check_manual_stop()
        session_info = _fetch_or_recover_chatgpt_session(
            driver,
            email=email,
            proxy=client.profile_proxy or proxy,
            registration_created=create_acknowledged,
            should_stop=lambda: _registration_stop_requested(),
            profile_name=name,
            profile_birthday=birthday,
        )
        access_token = session_info["accessToken"]
        logger.info("[Roxy注册] 已拿到 accessToken：%s", email)
        _check_manual_stop()

        totp_secret = None
        twofa_result = None
        twofa_session = None
        twofa_status = "skipped"
        twofa_error = None
        twofa_validation = None
        twofa_proxy_continuity = False
        twofa_proxy_source = None
        if _twofa_cfg.ENABLE_2FA:
            twofa_status = "failed"
            logger.info("[Roxy注册][2FA] ENABLE_2FA=True，复用当前浏览器会话设置 2FA")
            try:
                from core.account_export import maybe_setup_2fa_result
                twofa_proxy = resolve_twofa_proxy(
                    getattr(client, "profile_proxy", None),
                    proxy,
                    source="RoxyBrowser",
                )
                twofa_session = build_twofa_session(twofa_proxy, source="RoxyBrowser")
                twofa_proxy_continuity = True
                twofa_proxy_source = "registration_profile"
                twofa_result = maybe_setup_2fa_result(twofa_session, email, driver=driver)
                twofa_error = getattr(twofa_session, "_twofa_last_error", None)
                if twofa_result:
                    totp_secret = twofa_result.secret
                    access_token = twofa_result.access_token
                    twofa_validation = getattr(twofa_result, "validation", None)
                    twofa_status = "success" if bool(getattr(twofa_result, "validation_ok", True)) else "partial_success"
                    logger.info("[Roxy注册][2FA] 已完成，Token 校验=%s", twofa_status == "success")
                else:
                    if not twofa_error:
                        twofa_error = {
                            "stage": "totp_setup",
                            "code": "totp_setup_failed",
                            "http_status": None,
                            "message": "2FA 未完成",
                        }
                    logger.warning("[Roxy注册][2FA] 未完成，账号仍保存")
            except Exception as exc:
                twofa_error = twofa_failure_payload(exc, default_stage="totp_proxy")
                logger.warning("[Roxy注册][2FA] 执行失败，账号仍保存：%s", type(exc).__name__)
            finally:
                if twofa_session is not None:
                    twofa_session.close()

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                # 注册流程本身已创建 Roxy 一号一环境。这里不能再新建第二个 Roxy 环境；
                # 复用当前注册窗口，先清理 Cookie/session/localStorage/cache，再开始 Codex 授权。
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                logger.info("[Roxy注册][Codex] ENABLE_CODEX_AUTO=True，复用当前注册 Roxy 窗口执行 Codex 授权，不创建新环境")
                _check_manual_stop()
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
            else:
                logger.info("[Roxy注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        traffic_summary = _finish_traffic_optimizer(traffic_optimizer)
        traffic_optimizer = None

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=proxy or None,
            batch_dir=batch_dir,
            registration_name=name,
            birth_date=birthday,
            registration_exit_ip=registration_exit_geo.get("ip"),
            registration_exit_country=registration_exit_geo.get("country"),
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": (twofa_result.expires if twofa_result and twofa_result.expires else session_info.get("expires")),
                "roxybrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "codex": codex_result,
                "registration_traffic": traffic_summary,
                "twofa": {
                    "status": twofa_status,
                    "validated": bool(twofa_result and getattr(twofa_result, "validation_ok", True)),
                    "validation_status": getattr(twofa_result, "validation_status", None) if twofa_result else None,
                    "validation": twofa_validation,
                    "activated_at": getattr(twofa_result, "activated_at", None) if twofa_result else None,
                    "proxy_continuity": twofa_proxy_continuity,
                    "proxy_source": twofa_proxy_source,
                    "error": twofa_error,
                },
            },
        )
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {
            "success": bool(codex_ok),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "traffic": traffic_summary,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except Exception as exc:
        logger.error("[Roxy注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Roxy注册] 失败详情", exc_info=True)
        # 在返回 service 前一次完成邮箱状态转换。旧逻辑先放回
        # available，导致 service 无法累加失败次数，同一个不出码邮箱被反复领取。
        try:
            release_state = _release_roxy_registration_email_failure(
                email,
                exc,
                create_acknowledged=create_acknowledged,
            )
            logger.info("[Roxy注册] 失败邮箱状态已收敛：state=%s", release_state)
        except Exception:
            logger.exception("[Roxy注册] 失败邮箱状态收敛失败")
        if traffic_summary is None:
            traffic_summary = _finish_traffic_optimizer(traffic_optimizer)
            traffic_optimizer = None
        return {
            "success": False,
            "email": email,
            "traffic": traffic_summary,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    finally:
        if traffic_optimizer is not None:
            _finish_traffic_optimizer(traffic_optimizer)
        if driver and not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
        if not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
            client.cleanup_profile(opened)
