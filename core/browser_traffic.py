# -*- coding: utf-8 -*-
"""Roxy/Selenium registration traffic filtering, cache replay, and metering."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SECURITY_SUFFIXES = (
    "arkoselabs.com",
    "challenges.cloudflare.com",
    "hcaptcha.com",
    "recaptcha.net",
    "recaptcha.google.com",
    "sentinel.openai.com",
)
TELEMETRY_SUFFIXES = (
    "browser-intake-datadoghq.com",
    "statsigapi.net",
    "featuregates.org",
    "segment.io",
    "segment.com",
    "sentry.io",
)
TELEMETRY_PATH_MARKERS = (
    "/rum",
    "/analytics",
    "/telemetry",
)
OPTIONAL_IDENTITY_SUFFIXES = (
    "accounts.google.com",
    "appleid.apple.com",
    "login.microsoftonline.com",
)
FIRST_PARTY_CACHE_SUFFIXES = (
    "chatgpt.com",
    "cdn.openai.com",
)
SESSION_REQUIRED_PREFIXES = (
    "/api/auth/callback/",
    "/api/auth/session",
    "/backend-api/accounts/check",
)
HEAVY_EXTENSIONS = (
    ".avif", ".gif", ".ico", ".jpeg", ".jpg", ".mp3", ".mp4",
    ".ogg", ".otf", ".png", ".svg", ".ttf", ".webm", ".webp", ".woff", ".woff2",
)
REPLAY_STRIPPED_HEADERS = {
    "content-encoding",
    "content-length",
    "set-cookie",
    "transfer-encoding",
}
CACHE_SCHEMA_VERSION = 2
CACHE_PRIVATE_REQUEST_HEADERS = {"authorization", "cookie", "proxy-authorization"}
CACHE_PRIVATE_RESPONSE_HEADERS = {"set-cookie", "www-authenticate"}
CACHE_LOAD_WAIT_SECONDS = 30.0
PUBLIC_STATIC_PATH_PREFIXES = (
    "/assets/",
    "/cdn/assets/",
    "/_next/static/",
    "/unauth-mweb/assets/",
)


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    host = str(host or "").lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _resource_name(resource_type) -> str:
    value = getattr(resource_type, "value", resource_type)
    return str(value or "").strip().lower()


def _header_values(headers) -> dict[str, str]:
    values: dict[str, str] = {}
    source = headers.items() if isinstance(headers, dict) else headers or []
    for item in source:
        if isinstance(headers, dict):
            name, value = item
        elif isinstance(item, dict):
            name, value = item.get("name"), item.get("value")
        else:
            name, value = getattr(item, "name", ""), getattr(item, "value", "")
        name = str(name or "").strip().lower()
        if name:
            values[name] = str(value or "")
    return values


def block_reason(url: str, resource_type: str = "", *, session_only: bool = False) -> str:
    """Return an explicit block reason; an empty string means allow."""
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return ""
    host = str(parsed.hostname or "").lower()
    path = str(parsed.path or "/").lower()
    resource = _resource_name(resource_type)

    # Authentication and challenge traffic always wins over optimization rules.
    if _host_matches(host, SECURITY_SUFFIXES):
        return ""
    if "/cdn-cgi/challenge-platform/" in path or "/sentinel/" in path:
        return ""
    if _host_matches(host, TELEMETRY_SUFFIXES):
        return "telemetry"
    if host == "auth.openai.com" and any(marker in path for marker in TELEMETRY_PATH_MARKERS):
        return "telemetry"
    if _host_matches(host, OPTIONAL_IDENTITY_SUFFIXES):
        return "optional_identity"
    if resource in {"image", "media", "font", "manifest"}:
        return resource
    if session_only and host in {"chatgpt.com", "www.chatgpt.com"}:
        if resource == "document" or path.startswith(SESSION_REQUIRED_PREFIXES):
            return ""
        return "post_auth_" + (resource or "other")
    return ""


def is_cacheable_request(url: str, method: str, resource_type: str, headers=None) -> bool:
    """Return whether Roxy may replay the request from the shared cache.

    Authentication pages deliberately stay on the normal network path.  In
    production Roxy runs, replaying ``auth.openai.com`` / ``oaistatic.com``
    bundles under high concurrency intermittently left OTP/profile pages with
    only browser-default styling or incomplete interactive DOM.  ChatGPT's
    post-auth application shell remains cacheable because an appearance issue
    there cannot prevent registration or access-token acquisition.
    """
    if str(method or "").upper() != "GET":
        return False
    if _resource_name(resource_type) not in {"script", "stylesheet"}:
        return False
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    host = str(parsed.hostname or "").lower()
    path = str(parsed.path or "").lower()
    if parsed.scheme not in {"http", "https"} or not _host_matches(host, FIRST_PARTY_CACHE_SUFFIXES):
        return False
    if path.endswith("/service-worker.js") or path.endswith("/sw.js"):
        return False
    private_headers = set(_header_values(headers)) & CACHE_PRIVATE_REQUEST_HEADERS
    if private_headers & {"authorization", "proxy-authorization"}:
        return False
    # Same-origin public bundles naturally carry the browser's Cookie header.
    # Only allow that header on immutable/static paths; response cache controls
    # and Set-Cookie/Vary checks still decide whether the body may be stored.
    if "cookie" in private_headers and not path.startswith(PUBLIC_STATIC_PATH_PREFIXES):
        return False
    return True


def is_cacheable_response(headers) -> bool:
    """Accept only responses that cannot carry account-specific state."""
    values = _header_values(headers)
    if set(values) & CACHE_PRIVATE_RESPONSE_HEADERS:
        return False
    cache_control = values.get("cache-control", "").lower()
    if "private" in cache_control or "no-store" in cache_control:
        return False
    vary = {part.strip().lower() for part in values.get("vary", "").split(",")}
    return not ({"cookie", "authorization"} & vary)


def _sanitize_headers(headers) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for header in headers or []:
        if isinstance(header, dict):
            name = str(header.get("name") or "")
            value = str(header.get("value") or "")
        else:
            name = str(getattr(header, "name", "") or "")
            value = str(getattr(header, "value", "") or "")
        if name and name.lower() not in REPLAY_STRIPPED_HEADERS:
            cleaned.append({"name": name, "value": value})
    return cleaned


class StaticResourceCache:
    """TTL cache with atomic metadata/body replacement and digest validation."""

    _coordinators_lock = threading.RLock()
    _coordinators: dict[str, "_CacheLoadCoordinator"] = {}

    def __init__(self, root: Path, *, max_age: int, max_item_bytes: int):
        self.root = Path(root)
        self.max_age = max(0, int(max_age))
        self.max_item_bytes = max(1, int(max_item_bytes))
        self._lock = threading.RLock()
        with self._coordinators_lock:
            cache_root = str(self.root.resolve())
            self._coordinator = self._coordinators.setdefault(cache_root, _CacheLoadCoordinator())

    @staticmethod
    def cache_key(url: str) -> str:
        return hashlib.sha256(str(url).encode("utf-8")).hexdigest()

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = self.cache_key(url)
        return self.root / f"{key}.json", self.root / f"{key}.bin"

    def read(self, url: str) -> dict | None:
        meta_path, body_path = self._paths(url)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if int(meta.get("schema_version") or 0) != CACHE_SCHEMA_VERSION:
                return None
            if self.max_age and time.time() - float(meta.get("saved_at") or 0) > self.max_age:
                return None
            body = body_path.read_bytes()
            if len(body) > self.max_item_bytes:
                return None
            if hashlib.sha256(body).hexdigest() != str(meta.get("body_sha256") or ""):
                return None
            if str(meta.get("url") or "") != str(url):
                return None
            return {
                "status": int(meta.get("status") or 200),
                "phrase": str(meta.get("phrase") or "OK"),
                "headers": _sanitize_headers(meta.get("headers") or []),
                "body": body,
                "saved_at": float(meta.get("saved_at") or 0),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def write(self, url: str, *, status: int, phrase: str, headers, body: bytes) -> bool:
        if not body or len(body) > self.max_item_bytes:
            return False
        meta_path, body_path = self._paths(url)
        token = f"{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
        meta_tmp = meta_path.with_name(meta_path.name + "." + token + ".tmp")
        body_tmp = body_path.with_name(body_path.name + "." + token + ".tmp")
        meta = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "url": str(url),
            "status": int(status or 200),
            "phrase": str(phrase or "OK"),
            "headers": _sanitize_headers(headers),
            "saved_at": time.time(),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_bytes": len(body),
        }
        try:
            with self._lock:
                self.root.mkdir(parents=True, exist_ok=True)
                body_tmp.write_bytes(body)
                meta_tmp.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                os.replace(body_tmp, body_path)
                os.replace(meta_tmp, meta_path)
            return True
        except OSError:
            return False
        finally:
            for path in (body_tmp, meta_tmp):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def claim_load(self, url: str) -> bool:
        """Claim one cache miss so parallel profiles do not fetch the same public asset."""
        return self._coordinator.claim(str(url))

    def wait_for_load(self, url: str, *, timeout: float = CACHE_LOAD_WAIT_SECONDS) -> dict | None:
        """Wait briefly for another profile to atomically populate a missing entry."""
        return self._coordinator.wait_for_cache(self, str(url), timeout=timeout)

    def release_load(self, url: str) -> None:
        self._coordinator.release(str(url))


class _CacheLoadCoordinator:
    """Coordinates cache misses across Roxy profiles sharing one cache directory."""

    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._loading: dict[str, float] = {}

    def claim(self, url: str) -> bool:
        now = time.monotonic()
        with self._condition:
            expires_at = self._loading.get(url, 0.0)
            if expires_at > now:
                return False
            self._loading[url] = now + CACHE_LOAD_WAIT_SECONDS
            return True

    def wait_for_cache(self, cache: StaticResourceCache, url: str, *, timeout: float) -> dict | None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while True:
                cached = cache.read(url)
                if cached:
                    return cached
                now = time.monotonic()
                expires_at = self._loading.get(url, 0.0)
                if expires_at <= now or now >= deadline:
                    return None
                self._condition.wait(min(deadline - now, expires_at - now))

    def release(self, url: str) -> None:
        with self._condition:
            self._loading.pop(url, None)
            self._condition.notify_all()


def summarize_performance_logs(entries: list[dict | str], *, cached_bytes: int = 0, cache_hits: int = 0,
                               cache_misses: int = 0, cached_request_urls=(), cached_request_ids=(),
                               budget_bytes: int = 3 * 1024 * 1024) -> dict:
    requests: dict[str, str] = {}
    request_resource_types: dict[str, str] = {}
    exact_cached_request_ids = {str(request_id) for request_id in (cached_request_ids or ()) if request_id}
    replayed_request_ids: set[str] = set()
    blocked_request_ids: set[str] = set()
    cached_url_counts = Counter(str(url) for url in (cached_request_urls or ()) if url)
    downloaded = 0
    started = 0
    blocked_by_reason: dict[str, int] = defaultdict(int)
    by_host: dict[str, int] = defaultdict(int)
    by_path: dict[str, int] = defaultdict(int)

    for raw in entries:
        try:
            outer = json.loads(raw) if isinstance(raw, str) else raw
            payload = outer.get("message", outer)
            if isinstance(payload, str):
                payload = json.loads(payload)
            message = payload.get("message", payload)
            method = str(message.get("method") or "")
            params = message.get("params") or {}
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            continue
        request_id = str(params.get("requestId") or "")
        if method == "Network.requestWillBeSent":
            url = str((params.get("request") or {}).get("url") or "")
            if url.startswith(("http://", "https://")):
                requests[request_id] = url
                request_resource_types[request_id] = str(params.get("type") or "")
                started += 1
                # Fetch.RequestPaused exposes Network.requestId on current
                # Chromium builds.  Prefer that exact identity so a network
                # miss followed by a replay of the same URL cannot subtract
                # the wrong response from encodedDataLength.  URL matching is
                # retained only as a compatibility fallback for older builds.
                if request_id and request_id in exact_cached_request_ids:
                    replayed_request_ids.add(request_id)
                elif cached_url_counts[url] > 0:
                    cached_url_counts[url] -= 1
                    replayed_request_ids.add(request_id)
        elif method == "Network.loadingFinished":
            if request_id in replayed_request_ids:
                continue
            size = max(0, int(float(params.get("encodedDataLength") or 0)))
            downloaded += size
            parsed = urlparse(requests.get(request_id, ""))
            if parsed.hostname:
                by_host[parsed.hostname.lower()] += size
                by_path[f"{parsed.hostname.lower()}{parsed.path or '/'}"] += size
        elif method == "Network.loadingFailed" and params.get("blockedReason"):
            reason = block_reason(
                requests.get(request_id, ""), request_resource_types.get(request_id, ""),
            ) or str(params.get("blockedReason") or "blocked")
            blocked_by_reason[reason] += 1
            if request_id:
                blocked_request_ids.add(request_id)

    top_paths = sorted(by_path.items(), key=lambda item: item[1], reverse=True)[:20]
    return {
        "downloaded": downloaded,
        "logical_downloaded": downloaded + max(0, int(cached_bytes)),
        "cached_downloaded": max(0, int(cached_bytes)),
        "cache_saved_bytes": max(0, int(cached_bytes)),
        "cache_hits": max(0, int(cache_hits)),
        "cache_misses": max(0, int(cache_misses)),
        # A blocked request and a Fetch-fulfilled cache replay never reached
        # the proxy/network and therefore must not inflate this counter.
        "network_requests": max(0, started - len(replayed_request_ids) - len(blocked_request_ids)),
        "blocked": sum(blocked_by_reason.values()),
        "blocked_by_reason": dict(sorted(blocked_by_reason.items())),
        "by_host": dict(sorted(by_host.items(), key=lambda item: item[1], reverse=True)),
        "by_path": dict(top_paths),
        "budget_bytes": max(0, int(budget_bytes)),
        "within_budget": downloaded <= max(0, int(budget_bytes)),
    }


class RoxyTrafficOptimizer:
    """Installs conservative CDP blocking, shared cache replay, and traffic capture."""

    def __init__(self, driver, *, low_traffic: bool, static_cache: bool, capture: bool,
                 cache_dir: Path, cache_max_age: int, cache_max_item_bytes: int,
                 cache_refresh_rate: float, cache_refresh_budget_bytes: int,
                 cache_refresh_max_item_bytes: int, budget_bytes: int):
        self.driver = driver
        self.low_traffic = bool(low_traffic)
        self.static_cache_enabled = bool(static_cache)
        self.capture = bool(capture)
        self.budget_bytes = max(0, int(budget_bytes))
        self.cache = StaticResourceCache(
            Path(cache_dir), max_age=cache_max_age, max_item_bytes=cache_max_item_bytes,
        )
        self.refresh_rate = min(1.0, max(0.0, float(cache_refresh_rate)))
        self.refresh_budget = max(0, int(cache_refresh_budget_bytes))
        self.refresh_max_item = max(0, int(cache_refresh_max_item_bytes))
        self._refresh_used = 0
        self._salt = random.randbytes(16) if hasattr(random, "randbytes") else os.urandom(16)
        self._devtools = None
        self._connection = None
        self._fetch_enabled = False
        self._session_only = False
        self._lock = threading.RLock()
        self._stats = {"cache_hits": 0, "cache_misses": 0, "cached_bytes": 0, "cache_errors": 0}
        self._cached_urls: list[str] = []
        self._cached_request_ids: list[str] = []
        self._loading_requests: dict[str, str] = {}
        self._install_errors: list[str] = []
        self._degraded_reason = ""

    def install(self) -> None:
        if not (self.low_traffic or self.static_cache_enabled or self.capture):
            return
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
            self.driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": False})
            self.driver.execute_cdp_cmd("Network.setBypassServiceWorker", {"bypass": False})
            if self.capture:
                try:
                    self.driver.get_log("performance")
                except Exception as exc:
                    self._install_errors.append(f"performance_log: {type(exc).__name__}: {exc}")
            if self.low_traffic:
                self._apply_blocked_urls()
        except Exception as exc:
            self._install_errors.append(f"network_cdp: {type(exc).__name__}: {exc}")
        if self.static_cache_enabled:
            self._install_fetch_cache()

    def _base_block_patterns(self) -> list[str]:
        patterns = []
        for host in TELEMETRY_SUFFIXES:
            patterns.extend([f"*://{host}/*", f"*://*.{host}/*"])
        patterns.extend(f"*://{host}/*" for host in OPTIONAL_IDENTITY_SUFFIXES)
        for host in ("chatgpt.com", "www.chatgpt.com", "cdn.openai.com", "oaistatic.com"):
            patterns.extend(f"*://{host}/*{extension}*" for extension in HEAVY_EXTENSIONS)
        patterns.extend(f"*://auth.openai.com/*{extension}*" for extension in (".woff", ".woff2", ".ttf", ".otf"))
        patterns.append("*://auth.openai.com/awe/api/v2/rum*")
        return patterns

    def _apply_blocked_urls(self) -> None:
        patterns = self._base_block_patterns()
        if self._session_only:
            patterns.extend([
                "*://chatgpt.com/_next/static/*",
                "*://www.chatgpt.com/_next/static/*",
                "*://chatgpt.com/cdn/assets/*",
                "*://www.chatgpt.com/cdn/assets/*",
                "*://chatgpt.com/unauth-mweb/assets/*",
                "*://www.chatgpt.com/unauth-mweb/assets/*",
            ])
        self.driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": sorted(set(patterns))})

    def set_session_only(self, enabled: bool = True) -> None:
        self._session_only = bool(enabled)
        if self.low_traffic:
            try:
                self._apply_blocked_urls()
            except Exception as exc:
                self._install_errors.append(f"session_only: {type(exc).__name__}: {exc}")

    def disable_for_recovery(self, reason: str) -> None:
        """Restore normal networking when optimization correlates with a flow failure."""
        reason = str(reason or "registration_recovery").strip()[:200]
        self._degraded_reason = reason
        self._session_only = False
        if self._fetch_enabled and self._devtools and self._connection:
            try:
                self._connection.execute(self._devtools.fetch.disable())
            except Exception as exc:
                self._install_errors.append(f"recovery_fetch_disable: {type(exc).__name__}: {exc}")
            self._fetch_enabled = False
        if self.low_traffic:
            try:
                self.driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": []})
            except Exception as exc:
                self._install_errors.append(f"recovery_unblock: {type(exc).__name__}: {exc}")
        self.low_traffic = False
        self.static_cache_enabled = False
        logger.warning("[Roxy流量] 注册恢复已降级为正常联网：reason=%s", reason)

    def _install_fetch_cache(self) -> None:
        try:
            devtools, connection = self.driver.start_devtools()
            patterns = [
                devtools.fetch.RequestPattern(
                    url_pattern="*", resource_type=devtools.network.ResourceType.SCRIPT,
                    request_stage=devtools.fetch.RequestStage.REQUEST,
                ),
                devtools.fetch.RequestPattern(
                    url_pattern="*", resource_type=devtools.network.ResourceType.STYLESHEET,
                    request_stage=devtools.fetch.RequestStage.REQUEST,
                ),
            ]
            self._devtools = devtools
            self._connection = connection
            connection.add_callback(devtools.fetch.RequestPaused, self._on_request_paused)
            connection.execute(devtools.fetch.enable(patterns=patterns, handle_auth_requests=False))
            self._fetch_enabled = True
        except Exception as exc:
            self._install_errors.append(f"static_cache: {type(exc).__name__}: {exc}")

    def _should_refresh(self, url: str, body_bytes: int) -> bool:
        if not self.refresh_rate or body_bytes > self.refresh_max_item:
            return False
        if self._refresh_used + body_bytes > self.refresh_budget:
            return False
        digest = hashlib.sha256(self._salt + str(url).encode("utf-8")).digest()
        selected = int.from_bytes(digest[:8], "big") / float(2**64) < self.refresh_rate
        if selected:
            self._refresh_used += body_bytes
        return selected

    def _on_request_paused(self, event) -> None:
        devtools = self._devtools
        connection = self._connection
        if not devtools or not connection:
            return
        request_id = event.request_id
        claimed_url = ""
        try:
            if event.response_status_code is not None:
                self._handle_response(event)
                return
            url = str(getattr(event.request, "url", "") or "")
            method = str(getattr(event.request, "method", "") or "")
            resource = _resource_name(event.resource_type)
            if not is_cacheable_request(url, method, resource, getattr(event.request, "headers", None)):
                connection.execute(devtools.fetch.continue_request(request_id))
                return
            cached = self.cache.read(url)
            if cached and not self._should_refresh(url, len(cached["body"])):
                self._fulfill_cached_request(
                    request_id, url, cached, network_id=getattr(event, "network_id", None),
                )
                return
            if not cached and not self.cache.claim_load(url):
                cached = self.cache.wait_for_load(url)
                if cached:
                    self._fulfill_cached_request(
                        request_id, url, cached, network_id=getattr(event, "network_id", None),
                    )
                    return
            elif not cached:
                claimed_url = url
                with self._lock:
                    self._loading_requests[str(request_id)] = url
            with self._lock:
                self._stats["cache_misses"] += 1
            connection.execute(devtools.fetch.continue_request(request_id, intercept_response=True))
        except Exception:
            if claimed_url:
                self._release_loading_request(request_id, claimed_url)
            with self._lock:
                self._stats["cache_errors"] += 1
            try:
                connection.execute(devtools.fetch.continue_request(request_id))
            except Exception:
                pass

    def _fulfill_cached_request(self, request_id, url: str, cached: dict, *, network_id=None) -> None:
        devtools = self._devtools
        connection = self._connection
        if not devtools or not connection:
            raise RuntimeError("Fetch cache is not connected")
        headers = [devtools.fetch.HeaderEntry(name=item["name"], value=item["value"]) for item in cached["headers"]]
        connection.execute(devtools.fetch.fulfill_request(
            request_id,
            response_code=cached["status"],
            response_headers=headers,
            body=base64.b64encode(cached["body"]).decode("ascii"),
            response_phrase=cached["phrase"],
        ))
        with self._lock:
            self._stats["cache_hits"] += 1
            self._stats["cached_bytes"] += len(cached["body"])
            if network_id:
                self._cached_request_ids.append(str(network_id))
            else:
                self._cached_urls.append(url)

    def _release_loading_request(self, request_id, fallback_url: str = "") -> None:
        with self._lock:
            url = self._loading_requests.pop(str(request_id), fallback_url)
        if url:
            self.cache.release_load(url)

    def _handle_response(self, event) -> None:
        devtools = self._devtools
        connection = self._connection
        if not devtools or not connection:
            return
        request_id = event.request_id
        try:
            url = str(getattr(event.request, "url", "") or "")
            method = str(getattr(event.request, "method", "") or "")
            resource = _resource_name(event.resource_type)
            status = int(event.response_status_code or 0)
            if status == 200 and is_cacheable_request(url, method, resource, getattr(event.request, "headers", None)) and is_cacheable_response(event.response_headers or []):
                payload, encoded = connection.execute(devtools.fetch.get_response_body(request_id))
                body = base64.b64decode(payload) if encoded else str(payload).encode("utf-8")
                if not self.cache.write(
                    url,
                    status=status,
                    phrase=str(event.response_status_text or "OK"),
                    headers=event.response_headers or [],
                    body=body,
                ):
                    with self._lock:
                        self._stats["cache_errors"] += 1
            connection.execute(devtools.fetch.continue_response(request_id))
        except Exception:
            with self._lock:
                self._stats["cache_errors"] += 1
            try:
                connection.execute(devtools.fetch.continue_response(request_id))
            except Exception:
                pass
        finally:
            self._release_loading_request(request_id)

    def finalize(self) -> dict:
        with self._lock:
            loading_request_ids = list(self._loading_requests)
        for request_id in loading_request_ids:
            self._release_loading_request(request_id)
        if self._fetch_enabled and self._devtools and self._connection:
            try:
                self._connection.execute(self._devtools.fetch.disable())
            except Exception as exc:
                self._install_errors.append(f"fetch_disable: {type(exc).__name__}: {exc}")
            self._fetch_enabled = False
        entries: list[dict | str] = []
        if self.capture:
            try:
                entries = list(self.driver.get_log("performance") or [])
            except Exception as exc:
                self._install_errors.append(f"performance_drain: {type(exc).__name__}: {exc}")
        with self._lock:
            stats = dict(self._stats)
            cached_urls = list(self._cached_urls)
            cached_request_ids = list(self._cached_request_ids)
        summary = summarize_performance_logs(
            entries,
            cached_bytes=stats["cached_bytes"],
            cache_hits=stats["cache_hits"],
            cache_misses=stats["cache_misses"],
            cached_request_urls=cached_urls,
            cached_request_ids=cached_request_ids,
            budget_bytes=self.budget_bytes,
        )
        summary.update({
            "metrics_version": 3,
            "downloaded_excludes_cache_replay": True,
            "enabled": self.low_traffic or self.static_cache_enabled,
            "low_traffic": self.low_traffic,
            "static_cache": self.static_cache_enabled,
            "traffic_capture": self.capture,
            "cache_errors": stats["cache_errors"],
            "refresh_bytes": self._refresh_used,
            "session_only": self._session_only,
            "degraded_reason": self._degraded_reason,
            "errors": list(self._install_errors),
        })
        return summary
