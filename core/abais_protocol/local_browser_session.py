"""Protocol-only HTTP adapter backed by one local Roxy Chromium profile.

No page-click registration, curl impersonation, or separate Node fingerprint VM.
The existing protocol worker borrows this transport and its native SDK runtime.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from requests import Response
from requests.structures import CaseInsensitiveDict

from config import roxybrowser as roxy_cfg
from core.browser_traffic import summarize_performance_logs
from core.roxybrowser_client import RoxyBrowserClient
from core.registration_service import bind_roxy_profile, check_stop_requested
from .constants import CHATGPT_APP, OPENAI_AUTH

logger = logging.getLogger(__name__)
_ALLOWED_ORIGINS = frozenset((CHATGPT_APP, OPENAI_AUTH, "https://sentinel.openai.com"))
_FINGERPRINT_JS = """async () => ({
  user_agent: navigator.userAgent, language: navigator.language,
  languages: Array.from(navigator.languages), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  platform: navigator.platform, hardware_concurrency: navigator.hardwareConcurrency,
  device_memory: navigator.deviceMemory, screen_width: screen.width, screen_height: screen.height,
  webdriver: navigator.webdriver,
  ua_full_version: (await navigator.userAgentData?.getHighEntropyValues(['uaFullVersion']))?.uaFullVersion
})"""
_FETCH_JS = """async ({url,method,headers,body,referrer,timeout}) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, {method, headers, body, referrer,
      credentials:'include', cache:'no-store', redirect:'manual', signal:controller.signal});
    return {status:response.status, headers:Object.fromEntries(response.headers), text:await response.text()};
  } finally { clearTimeout(timer); }
}"""


class _BrowserCookies:
    def __init__(self, owner):
        self.owner = owner

    def get_dict(self):
        return {item["name"]: item["value"] for item in self.owner.context.cookies()}

    def get(self, name, default=None):
        # Prefer the current origin; do not re-import flattened cookies.
        urls = [self.owner._last_url, CHATGPT_APP, OPENAI_AUTH]
        for url in dict.fromkeys(urls):
            for item in self.owner.context.cookies([url]):
                if item["name"] == name:
                    return item["value"]
        return default


class LocalBrowserSession:
    """Synchronous requests-like interface; all remote traffic uses Chromium."""

    def __init__(self, *, proxy: str, email: str):
        self.proxy = str(proxy or "").strip()
        if not self.proxy:
            raise RuntimeError("stage=protocol_proxy_missing")
        self.email = str(email or "").strip().casefold()
        self.requested_core = str(roxy_cfg.ROXY_CORE_VERSION or "latest").strip().lower()
        self.client = RoxyBrowserClient(local_component=True, profile_proxy=self.proxy)
        self.opened = None
        self._playwright = None
        self.browser = None
        self.context = None
        self._pages = {}
        self._traffic_sessions = {}
        self._traffic_events = []
        self._bootstrap_response = None
        self.exit_geo = {}
        self.traffic = None
        self._last_url = CHATGPT_APP
        self._identity_verified = False
        self._verified_token = ""
        self.stage = "initializing"
        self.http_status = None
        self._closed = False
        self.profile = {}
        self.cookies = _BrowserCookies(self)

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        from core.browser_exit_geo import probe_playwright_context_exit_geo
        from core.roxy_registration import _verify_registration_exit_geo

        check_stop_requested()
        try:
            self.opened = self.client.open_profile(
                headless=True, require_proxy_exit_ip=True,
                on_profile_ready=bind_roxy_profile, proxy_probe_stop_check=check_stop_requested,
            )
            if not self.opened.created_by_run or not self.opened.profile_id.startswith("local-"):
                raise RuntimeError("stage=protocol_profile_isolation")
            self._playwright = sync_playwright().start()
            self.browser = self._playwright.chromium.connect_over_cdp(
                "http://" + self.opened.debugger_address, timeout=30000,
            )
            self.context = self.browser.contexts[0]
            self.context.on("page", self._observe_page)
            for page in self.context.pages:
                self._observe_page(page)
            actual = str(self.browser.version)
            selected = str((self.opened.raw.get("data") or {}).get("coreVersion") or "")
            if (actual.split(".")[0] != selected.split(".")[0]
                    or (self.requested_core != "latest" and actual.split(".")[0] != self.requested_core)):
                raise RuntimeError("stage=protocol_core_mismatch")
            geo = _verify_registration_exit_geo(
                self.opened, probe_playwright_context_exit_geo(self.context, label="协议内核"),
            )
            self.exit_geo = dict(geo)
            self._bootstrap_response = self.get(CHATGPT_APP)
            if self._bootstrap_response.status_code != 200:
                raise RuntimeError("stage=protocol_homepage")
            page, _cdp = self._page(CHATGPT_APP)
            self.profile = page.evaluate(_FINGERPRINT_JS)
            ua = re.search(r"Chrome/(\d+)", self.profile.get("user_agent", ""))
            expected_locale = self.client._last_profile_create_summary.get("locale")
            if (not ua or ua.group(1) != actual.split(".")[0]
                    or self.profile.get("ua_full_version") != actual
                    or self.profile.get("timezone") != geo.get("timezone")
                    or (expected_locale and self.profile.get("language") != expected_locale)):
                raise RuntimeError("stage=protocol_fingerprint_mismatch")
            self.profile.update(
                name=self.opened.profile_id, core_version=actual, requested_core=self.requested_core,
                transport="local_chromium", headless=True, country=geo.get("country"),
            )
            logger.info("[协议内核] profile=%s requested=%s actual=%s locale=%s timezone=%s headless=True",
                        self.opened.profile_id, self.requested_core, actual,
                        self.profile["language"], self.profile["timezone"])
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _origin(url):
        parsed = urlsplit(str(url))
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if parsed.username or parsed.password or origin not in _ALLOWED_ORIGINS:
            raise RuntimeError("stage=protocol_target_origin")
        return origin

    def _observe_page(self, page):
        cdp = self._observe_target(page)
        page.on("frameattached", self._observe_frame)
        page.on("framenavigated", self._observe_frame)
        return cdp

    def _observe_frame(self, frame):
        if frame.parent_frame is not None:
            try:
                # Same-process frames are included by the page's Network domain;
                # only out-of-process frames accept a separate CDP session.
                self._observe_target(frame)
            except Exception:
                pass

    def _observe_target(self, target):
        if target in self._traffic_sessions:
            return self._traffic_sessions[target]
        cdp = self.context.new_cdp_session(target)
        self._traffic_sessions[target] = cdp
        for method in ("requestWillBeSent", "responseReceived", "requestServedFromCache",
                       "responseReceivedExtraInfo", "dataReceived", "loadingFinished", "loadingFailed",
                       "webSocketCreated", "webSocketFrameSent", "webSocketFrameReceived"):
            cdp.on("Network." + method, lambda params, event=method: self._traffic_event(target, event, params))
        cdp.send("Network.enable")
        return cdp

    def _traffic_event(self, target, event, params):
        # Keep only what the existing estimator consumes. No cookies, bearer
        # headers, URL queries, OTPs, passwords, or response bodies are retained.
        clean = {k: params[k] for k in ("requestId", "type", "encodedDataLength", "statusCode",
                                      "blockedReason", "errorText") if k in params}
        def url(value):
            parsed = urlsplit(str(value or ""))
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if event == "requestWillBeSent":
            req = params.get("request") or {}
            clean["request"] = {"url": url(req.get("url")), "postData": "x" * len(str(req.get("postData") or "").encode("utf-8"))}
        for key in ("response", "redirectResponse"):
            if key in params:
                resp = params[key]
                clean[key] = {k: resp[k] for k in ("encodedDataLength", "fromDiskCache", "fromPrefetchCache",
                                                 "serviceWorkerResponseSource") if k in resp}
                if "url" in resp:
                    clean[key]["url"] = url(resp["url"])
                if "payloadData" in resp:
                    import base64
                    raw = str(resp["payloadData"])
                    size = len(raw.encode("utf-8")) if resp.get("opcode", 1) == 1 else len(base64.b64decode(raw))
                    clean[key].update(opcode=1, payloadData="x" * size)
        if event == "webSocketCreated":
            clean["url"] = url(params.get("url"))
        self._traffic_events.append({"method": "Network." + event, "params": clean})

    def _page(self, url):
        origin = self._origin(url)
        if origin not in self._pages:
            page = self.context.new_page()
            cdp = self._observe_target(page)
            self._pages[origin] = (page, cdp)
        return self._pages[origin]

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def request(self, method, url, *, headers=None, data=None, json=None,
                params=None, allow_redirects=True, timeout=60, proxies=None, **kwargs):
        if self._closed:
            raise RuntimeError("stage=protocol_session_closed")
        if kwargs or (proxies and any(value != self.proxy for value in proxies.values())):
            raise RuntimeError("stage=protocol_transport_override")
        method = str(method).upper()
        if method not in {"GET", "POST"}:
            raise RuntimeError("stage=protocol_method")
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        parsed = urlsplit(url)
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
        headers = CaseInsensitiveDict(headers or {})
        if json is not None:
            if data is not None:
                raise ValueError("stage=protocol_duplicate_body")
            from json import dumps
            data = dumps(json, separators=(",", ":"))
            headers.setdefault("content-type", "application/json")
        elif isinstance(data, dict):
            data = urlencode(data, doseq=True)
            headers.setdefault("content-type", "application/x-www-form-urlencoded")
        if data is not None and not isinstance(data, str):
            raise ValueError("stage=protocol_body_type")
        history = []
        for _ in range(16):
            check_stop_requested()
            origin = self._origin(url)
            path = urlsplit(url).path
            self.stage = {
                "/": "homepage", "/api/auth/csrf": "csrf", "/api/auth/signin/openai": "signin",
                "/api/accounts/authorize/continue": "authorize_continue",
                "/api/accounts/user/register": "password_register",
                "/api/accounts/email-otp/send": "email_otp_send",
                "/api/accounts/email-otp/validate": "email_otp_validate",
                "/api/accounts/create_account": "create_account", "/api/auth/session": "session_read",
                "/log-in/password": "login_password_page",
                "/api/accounts/mfa/verify": "login_totp_verify",
                "/backend-api/accounts/mfa/enroll": "mfa_enroll",
                "/backend-api/accounts/mfa/user/activate_enrollment": "mfa_activate",
            }.get(path, "protocol_request")
            self.http_status = None
            session_read = origin == CHATGPT_APP and path == "/api/auth/session"
            if session_read:
                # Invalidate before I/O: timeout, invalid JSON and HTTP errors
                # must never leave a previous identity eligible for MFA writes.
                self._identity_verified = False
                self._verified_token = ""
            if origin == CHATGPT_APP and path.startswith("/backend-api/accounts/mfa"):
                if not self._identity_verified:
                    raise RuntimeError("stage=protocol_mfa_identity_unverified")
                authorization = str(headers.get("authorization") or "")
                if not authorization.startswith("Bearer ") or not authorization[7:].strip():
                    raise RuntimeError("stage=protocol_mfa_token_missing")
                if authorization[7:] != self._verified_token:
                    raise RuntimeError("stage=protocol_mfa_token_mismatch")
            response = self._fetch(method, url, headers, data, float(timeout))
            self.http_status = response.status_code
            logger.info("[协议内核] stage=%s http=%s", self.stage, self.http_status)
            response.history = list(history)
            self._last_url = url
            if session_read:
                try:
                    payload = response.json() if response.status_code == 200 else {}
                    if not isinstance(payload, dict):
                        raise ValueError()
                    user = payload.get("user") or {}
                    if not isinstance(user, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise RuntimeError("stage=protocol_session_payload") from None
                token = payload.get("accessToken")
                if isinstance(token, str) and token.strip():
                    actual_email = str(user.get("email") or "").strip().casefold()
                    self._identity_verified = bool(self.email and actual_email == self.email)
                    if not self._identity_verified:
                        raise RuntimeError("stage=protocol_session_account_mismatch")
                    self._verified_token = token.strip()
            location = response.headers.get("location")
            if not allow_redirects or response.status_code not in (301, 302, 303, 307, 308) or not location:
                return response
            target = urljoin(url, location)
            if self._origin(target) != origin:
                if data is not None and response.status_code in (307, 308):
                    raise RuntimeError("stage=protocol_cross_origin_body_redirect")
                # No bearer/opaque proof leakage across origins on redirects.
                for name in list(headers):
                    if name.lower() in {"authorization", "cookie"} or name.lower().startswith("openai-sentinel-"):
                        del headers[name]
            if response.status_code == 303 or (response.status_code in (301, 302) and method == "POST"):
                method, data = "GET", None
                headers.pop("content-type", None)
            history.append(response)
            parsed = urlsplit(target)
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
        raise RuntimeError("stage=protocol_redirect_limit")

    def _fetch(self, method, url, headers, data, timeout):
        if self._bootstrap_response is not None:
            bootstrap, self._bootstrap_response = self._bootstrap_response, None
            if method == "GET" and url == bootstrap.url and not headers and data is None:
                return bootstrap
        page, cdp = self._page(url)
        origin, path = self._origin(url), urlsplit(url).path
        document = method == "GET" and (
            (origin == CHATGPT_APP and (path == "/" or path.startswith("/api/auth/callback/")))
            or (origin == OPENAI_AUTH and (
                not path.startswith("/api/")
                or path in {"/api/accounts/authorize", "/api/oauth/oauth2/auth"}
            ))
        )
        observed = {}
        failure = []

        def paused(event):
            request_id = event["requestId"]
            try:
                status = int(event.get("responseStatusCode") or 0)
                if event["request"]["url"] == url and event["request"]["method"] == method:
                    self.http_status = status or None
                    observed.update(status=status, headers={h["name"]: h["value"] for h in event.get("responseHeaders", [])})
                    if 300 <= status < 400:
                        # Fetch's opaque manual redirect hides Location. Read
                        # the original response via CDP, then settle fetch
                        # without following it; Chromium already applied cookies.
                        cdp.send("Fetch.fulfillRequest", {
                            "requestId": request_id, "responseCode": 200, "body": "",
                            "responseHeaders": [{"name": "content-type", "value": "text/html" if document else "text/plain"}],
                        })
                        return
                cdp.send("Fetch.continueResponse", {"requestId": request_id})
            except Exception:
                failure.append(True)
                try:
                    cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Failed"})
                except Exception:
                    pass

        cdp.on("Fetch.requestPaused", paused)
        phase = "capture_enable"
        try:
            cdp.send("Fetch.enable", {"patterns": [{"urlPattern": url, "requestStage": "Response"}]})
            # Native Chromium owns UA / Client Hints / Fetch Metadata / cookies.
            forwarded = {k: v for k, v in headers.items() if not k.lower().startswith("sec-")
                         and k.lower() not in {"user-agent", "accept-language", "cookie", "host", "origin", "referer", "content-length"}}
            referrer = headers.get("referer")
            if referrer and self._origin(referrer) != self._origin(url):
                referrer = None
            request_timeout = int(max(1, min(timeout, 120)) * 1000)
            if document:
                # Real document navigation loads the site's own DOM and scripts.
                # Redirect interception still gives the worker one bounded hop
                # at a time, including callbacks it must inspect before following.
                phase = "document_navigation"
                native = page.goto(url, referer=referrer, wait_until="domcontentloaded", timeout=request_timeout)
                if native is None:
                    raise RuntimeError("stage=protocol_document_response")
                phase = "document_body"
                value = {"text": native.text()}
            else:
                phase = "api_fetch"
                if self._origin(page.url) != origin:
                    raise RuntimeError("stage=protocol_document_missing")
                # The current document is authoritative for API Referer. A
                # worker's old template must not claim an unvisited page.
                value = page.evaluate(_FETCH_JS, {
                    "url": url, "method": method, "headers": forwarded, "body": data,
                    "referrer": page.url, "timeout": request_timeout,
                })
            if failure or not observed.get("status"):
                raise RuntimeError("stage=protocol_response_capture")
            response = Response()
            response.status_code = observed["status"]
            response.headers = CaseInsensitiveDict(observed["headers"])
            response.url = url
            response.encoding = "utf-8"
            response._content = value["text"].encode("utf-8")
            response._content_consumed = True
            return response
        except Exception as exc:
            check_stop_requested()
            # Browser errors can embed request arguments: never log raw payloads.
            own_stage = re.fullmatch(r"stage=(protocol_[a-z_]+)", str(exc))
            stage = own_stage.group(1) if own_stage else "protocol_browser_fetch"
            net_error = re.search(r"net::ERR_[A-Z_]+", str(exc))
            reason = "unknown"
            for marker, code in (("Execution context was destroyed", "context_destroyed"),
                                 ("Content Security Policy", "document_csp"),
                                 ("Failed to fetch", "fetch_failed"),
                                 ("has been closed", "target_closed")):
                if marker in str(exc):
                    reason = code
                    break
            detail = (f"stage={stage} operation={self.stage} phase={phase} method={method}"
                      f" http_status={self.http_status or '-'} type={type(exc).__name__} reason={reason}"
                      f" net_error={net_error.group(0) if net_error else '-'}")
            logger.warning("[协议内核] %s", detail)
            raise RuntimeError(detail) from None
        finally:
            try:
                cdp.send("Fetch.disable")
            except Exception:
                pass
            cdp.remove_listener("Fetch.requestPaused", paused)

    def build_headers(self, device_id, flow):
        """Execute the real SDK in this profile, not a synthesized Node VM."""
        check_stop_requested()
        try:
            page, _cdp = self._page(OPENAI_AUTH)
            self.stage, self.http_status = "sentinel_sdk", None
            if self._closed or self._origin(page.url) != OPENAI_AUTH:
                raise RuntimeError("stage=protocol_document_missing")
            # Use the SDK loaded by the actual auth document. Its build and
            # lifecycle now belong to that page instead of a shared SDK cache.
            ready = page.evaluate("""async () => {
              const deadline = performance.now() + 30000;
              while (typeof window.SentinelSDK?.token !== 'function') {
                if (performance.now() >= deadline) return false;
                await new Promise(resolve => setTimeout(resolve, 100));
              }
              return true;
            }""")
            if not ready:
                raise RuntimeError("stage=protocol_browser_sdk")
            values = page.evaluate("""async ({flow,device}) => {
              const work = async () => {
                const token = await window.SentinelSDK.token(flow);
                const parsed = JSON.parse(token);
                if (parsed.id !== device || parsed.flow !== flow || !parsed.c || parsed.e)
                  throw new Error('identity or token incomplete');
                const headers = {'openai-sentinel-token': token};
                if (typeof window.SentinelSDK.sessionObserverToken === 'function') {
                  const so = await window.SentinelSDK.sessionObserverToken(flow);
                  if (so) headers['openai-sentinel-so-token'] = typeof so === 'string' ? so : JSON.stringify(so);
                }
                return headers;
              };
              let timer;
              try { return await Promise.race([work(), new Promise((_,reject) => {
                timer = setTimeout(() => reject(new Error('SDK timeout')), 45000);
              })]); } finally { clearTimeout(timer); }
            }""", {"flow": flow, "device": device_id})
        except Exception:
            check_stop_requested()
            raise RuntimeError("stage=protocol_browser_sdk") from None
        return values

    def build_header(self, device_id, flow):
        return self.build_headers(device_id, flow)["openai-sentinel-token"]

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.context is not None:
            # Settle queued CDP callbacks before taking the final per-account
            # snapshot. This includes geo probes and SDK iframe requests.
            try:
                if self.context.pages:
                    self.context.pages[0].evaluate("0")
            except Exception:
                pass
        self._identity_verified = False
        self._verified_token = ""
        try:
            self.traffic = summarize_performance_logs(
                self._traffic_events,
                budget_bytes=int(roxy_cfg.ROXY_TRAFFIC_BUDGET_BYTES),
            )
            self.traffic.update(metrics_version=4, downloaded_excludes_cache_replay=True,
                                traffic_capture=True, capture_source="local_chromium_cdp",
                                captured_targets=len(self._traffic_sessions))
        except Exception as exc:
            self.traffic = None
            logger.warning("[协议内核] stage=protocol_traffic_summary type=%s", type(exc).__name__)
        finally:
            self._traffic_events.clear()
        # Each resource is independent. An estimator/driver/close failure must
        # not skip deletion, leak the HTTP client or replace the registration error.
        actions = []
        if self.browser is not None:
            actions.append(("cdp_close", self.browser.close))
        if self._playwright is not None:
            actions.append(("playwright_stop", self._playwright.stop))
        if self.opened and self.opened.created_by_run:
            actions.extend([
                ("profile_close", lambda: self.client.close_profile(self.opened.profile_id)),
                ("profile_delete", lambda: self.client.delete_profile(self.opened.profile_id)),
            ])
        actions.append(("http_close", self.client.http.close))
        for stage, action in actions:
            try:
                result = action()
                logger.info("[协议内核] cleanup=%s ok=%s", stage, result is not False)
            except Exception as exc:
                logger.warning("[协议内核] cleanup=%s ok=False type=%s", stage, type(exc).__name__)
