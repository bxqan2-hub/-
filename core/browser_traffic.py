# -*- coding: utf-8 -*-
"""Roxy/Selenium registration traffic filtering, cache replay, and metering."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from collections import Counter, defaultdict
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PUBLIC_CDN_HOST = "cdn.openai.com"
OPTIONAL_MEDIA_EXTENSIONS = (".mp3", ".mp4", ".ogg", ".webm")
# Only body/representation headers are replayable, never origin/profile state,
# tracing identifiers, CSP nonces, or client-hint negotiation.
REPLAY_ALLOWED_HEADERS = {
    "content-type", "cache-control", "access-control-allow-origin",
    "cross-origin-resource-policy", "x-content-type-options",
}
CACHE_SCHEMA_VERSION = 3
CACHE_PRIVATE_REQUEST_HEADERS = {"authorization", "cookie", "proxy-authorization", "range"}
CACHE_PRIVATE_RESPONSE_HEADERS = {
    "set-cookie", "www-authenticate", "authentication-info", "proxy-authenticate",
    "proxy-authentication-info", "clear-site-data", "accept-ch", "critical-ch",
    "origin-trial", "content-security-policy", "content-security-policy-report-only",
}
STATIC_RESOURCE_MIME_TYPES = {
    "script": {"application/javascript", "text/javascript", "application/x-javascript"},
    "stylesheet": {"text/css"},
}
SAFE_VARY_HEADERS = {"accept-encoding"}
# A cold cache can make every concurrent Profile request the same large public
# bundle.  Single-flight only the validated public asset load; registration
# workers and Profile/browser concurrency remain unchanged.  A short timeout
# lets a stalled Profile fall back to its own live request.
CACHE_LOAD_WAIT_SECONDS = 8.0
PUBLIC_STATIC_PATH_PREFIXES = (
    "/assets/",
    "/cdn/assets/",
    "/_next/static/",
    "/unauth-mweb/assets/",
)

# Chromium keeps Network-domain event data until the DevTools client drains
# it.  A registration Profile only needs small request metadata and the
# Fetch-domain response body; an unbounded Network buffer therefore grows in
# every concurrent Roxy process without improving the registration flow.
# These limits are per Profile and do not change the number of workers or
# renderer processes.  Older Roxy/CDP builds may reject optional limits, so
# the caller falls back to the normal Network.enable payload.
_NETWORK_ENABLE_LIMITS = {
    "maxTotalBufferSize": 2 * 1024 * 1024,
    "maxResourceBufferSize": 512 * 1024,
    "maxPostDataSize": 4 * 1024,
}


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
            value = str(value or "")
            if name in values and values[name]:
                values[name] += "," + value
            else:
                values[name] = value
    return values


def block_reason(url: str, resource_type: str = "", *, session_only: bool = False) -> str:
    """Block only optional CDN media, never configuration/authentication traffic."""
    try:
        parsed = urlparse(str(url or ""))
        if (parsed.scheme != "https" or parsed.hostname != PUBLIC_CDN_HOST
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            return ""
    except ValueError:
        return ""
    path = parsed.path
    if _resource_name(resource_type) != "media" or not path.startswith("/assets/"):
        return ""
    if ("%" in path or "\\" in path or "?" in str(url) or "#" in str(url)
            or any(segment in {".", "..", "sentinel", "recaptcha", "hcaptcha", "captcha", "challenge"} for segment in path.lower().split("/"))
            or "/cdn-cgi/challenge-platform/" in path.lower()):
        return ""
    return "optional_media" if path.endswith(OPTIONAL_MEDIA_EXTENSIONS) else ""


def is_cacheable_request(url: str, method: str, resource_type: str, headers=None) -> bool:
    """Share only credential-free, content-addressed files on the exact public CDN.

    Same-origin app/auth scripts, config, challenges and authenticated traffic
    stay in their own Profile's network stack and native HTTP cache. Identical
    public source bytes do not imply identical per-browser execution state.
    """
    resource = _resource_name(resource_type)
    if str(method or "").upper() != "GET" or resource not in STATIC_RESOURCE_MIME_TYPES:
        return False
    try:
        parsed = urlparse(str(url or ""))
        if (parsed.scheme != "https" or parsed.hostname != PUBLIC_CDN_HOST
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            return False
    except ValueError:
        return False
    path = parsed.path
    if ("%" in path or "\\" in path or "?" in str(url) or "#" in str(url)
            or any(segment in {".", "..", "sentinel", "recaptcha", "hcaptcha", "captcha", "challenge"} for segment in path.lower().split("/"))
            or "/cdn-cgi/challenge-platform/" in path.lower()
            or not path.startswith(PUBLIC_STATIC_PATH_PREFIXES)):
        return False
    extension = ".js" if resource == "script" else ".css"
    filename = path.rsplit("/", 1)[-1]
    if (not filename.endswith(extension)
            or not re.search(r"(?:^|[._-])[0-9a-fA-F]{8,}(?=[._-]|$)", filename[:-len(extension)])):
        return False
    if any(marker in filename.lower() for marker in ("service-worker", "serviceworker", "sw.")):
        return False
    request_headers = _header_values(headers)
    if (set(request_headers) & CACHE_PRIVATE_REQUEST_HEADERS
            or any(name.startswith(("if-", "x-")) for name in request_headers)):
        return False
    request_cache_control = request_headers.get("cache-control", "").lower()
    if (any(token in request_cache_control for token in ("no-cache", "no-store", "private"))
            or re.search(r'(?:s-maxage|max-age)\s*=\s*"?0(?:"|\s|,|$)', request_cache_control)
            or "no-cache" in request_headers.get("pragma", "").lower()):
        return False
    return True


def _cache_freshness_seconds(headers, *, now: float | None = None) -> float:
    """Remaining origin freshness; reject ambiguous/expired shared directives."""
    values = _header_values(headers)
    directives = {}
    for part in values.get("cache-control", "").lower().split(","):
        name, _, value = part.strip().partition("=")
        name = name.strip()
        if not name or name in directives:
            return 0.0
        directives[name] = value.strip().strip('"')
    if (not {"public", "immutable"}.issubset(directives)
            or set(directives) & {"private", "no-cache", "no-store", "must-revalidate", "proxy-revalidate"}):
        return 0.0
    lifetime = directives.get("s-maxage", directives.get("max-age", ""))
    age = values.get("age", "0").strip()
    if not re.fullmatch(r"[0-9]+", lifetime) or not re.fullmatch(r"[0-9]+", age):
        return 0.0
    try:
        freshness = float(lifetime)
        current_age = float(age)
        if "date" in values:
            date = parsedate_to_datetime(values["date"])
            if date.tzinfo is None:
                return 0.0
            current_age = max(current_age, (time.time() if now is None else now) - date.timestamp())
        if not math.isfinite(freshness) or not math.isfinite(current_age):
            return 0.0
        return max(0.0, freshness - current_age)
    except (ValueError, TypeError, OverflowError):
        return 0.0


def is_cacheable_response(headers, *, resource_type: str = "") -> bool:
    """Require an immutable, fresh, non-varying JS/CSS public representation."""
    values = _header_values(headers)
    if set(values) & CACHE_PRIVATE_RESPONSE_HEADERS or _cache_freshness_seconds(headers) <= 0:
        return False
    mime = values.get("content-type", "").split(";", 1)[0].strip().lower()
    resource = _resource_name(resource_type)
    if resource and resource not in STATIC_RESOURCE_MIME_TYPES:
        return False
    allowed_mimes = STATIC_RESOURCE_MIME_TYPES.get(resource, set().union(*STATIC_RESOURCE_MIME_TYPES.values()))
    if mime not in allowed_mimes:
        return False
    if (values.get("access-control-allow-origin", "*").strip() != "*"
            or values.get("access-control-allow-credentials", "false").strip().lower() != "false"):
        return False
    vary = {part.strip().lower() for part in values.get("vary", "").split(",") if part.strip()}
    # Decoded bodies normalize Accept-Encoding; other variants remain live.
    return not (vary - SAFE_VARY_HEADERS)


def _sanitize_headers(headers) -> list[dict[str, str]]:
    return [
        {"name": name, "value": value}
        for name, value in _header_values(headers).items()
        if name in REPLAY_ALLOWED_HEADERS
    ]


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
        resource = "stylesheet" if str(url).endswith(".css") else "script"
        if not is_cacheable_request(url, "GET", resource):
            return None
        meta_path, body_path = self._paths(url)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if int(meta.get("schema_version") or 0) != CACHE_SCHEMA_VERSION:
                return None
            status = int(meta.get("status") or 0)
            if status != 200:
                return None
            now = time.time()
            saved_at = float(meta.get("saved_at") or 0)
            expires_at = float(meta.get("expires_at") or 0)
            if (not all(math.isfinite(value) for value in (saved_at, expires_at))
                    or saved_at > now or expires_at <= saved_at or now >= expires_at
                    or (self.max_age and now - saved_at >= self.max_age)):
                return None
            body = body_path.read_bytes()
            if len(body) > self.max_item_bytes:
                return None
            if hashlib.sha256(body).hexdigest() != str(meta.get("body_sha256") or ""):
                return None
            if str(meta.get("url") or "") != str(url):
                return None
            headers = meta.get("headers") or []
            if (not is_cacheable_response(headers, resource_type=resource)
                    or expires_at > saved_at + _cache_freshness_seconds(headers, now=saved_at)):
                return None
            return {
                "status": status,
                "phrase": str(meta.get("phrase") or "OK"),
                "headers": _sanitize_headers(headers),
                "body": body,
                "saved_at": saved_at,
                "expires_at": expires_at,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def write(self, url: str, *, status: int, phrase: str, headers, body: bytes) -> bool:
        resource = "stylesheet" if str(url).endswith(".css") else "script"
        if (status != 200 or not is_cacheable_request(url, "GET", resource)
                or not is_cacheable_response(headers, resource_type=resource)
                or not body or len(body) > self.max_item_bytes):
            return False
        saved_at = time.time()
        freshness = _cache_freshness_seconds(headers, now=saved_at)
        if self.max_age:
            freshness = min(freshness, self.max_age)
        if freshness <= 0:
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
            "saved_at": saved_at,
            "expires_at": saved_at + freshness,
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
        """Claim one public cache miss so parallel Profiles avoid duplicate fetches."""
        return self._coordinator.claim(str(url))

    def wait_for_load(self, url: str, *, timeout: float = CACHE_LOAD_WAIT_SECONDS) -> dict | None:
        """Wait briefly for another Profile to populate a validated entry."""
        return self._coordinator.wait_for_cache(self, str(url), timeout=timeout)

    def release_load(self, url: str) -> None:
        self._coordinator.release(str(url))


class _CacheLoadCoordinator:
    """Single-flight coordination for one shared public-cache directory."""

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
        # A per-Profile cryptographic salt keeps the bounded refresh sample
        # independent even when worker processes share the same cache root.
        self._salt = secrets.token_bytes(16)
        self._devtools = None
        self._connection = None
        self._fetch_enabled = False
        self._session_only = False
        self._lock = threading.RLock()
        self._stats = {
            "cache_hits": 0,
            "cache_misses": 0,
            "cache_candidates": 0,
            "cache_writes": 0,
            "cached_bytes": 0,
            "cache_errors": 0,
        }
        self._cached_urls: list[str] = []
        self._cached_request_ids: list[str] = []
        self._loading_requests: dict[str, str] = {}
        self._install_errors: list[str] = []
        self._degraded_reason = ""

    def _enable_network_domain(self) -> None:
        """Enable CDP Network with a bounded per-Profile event buffer."""
        try:
            self.driver.execute_cdp_cmd("Network.enable", dict(_NETWORK_ENABLE_LIMITS))
            return
        except Exception as exc:
            # Keep compatibility with older Roxy runtimes that implement
            # Network.enable but do not expose the optional buffer fields.
            logger.info(
                "[Roxy流量] Network 缓冲上限不被当前 CDP 接受，回退默认配置：%s",
                type(exc).__name__,
            )
        self.driver.execute_cdp_cmd("Network.enable", {})

    def install(self) -> None:
        if not (self.low_traffic or self.static_cache_enabled or self.capture):
            return
        try:
            self._enable_network_domain()
            self.driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": False})
            self.driver.execute_cdp_cmd("Network.setBypassServiceWorker", {"bypass": False})
            if self.capture:
                try:
                    self.driver.get_log("performance")
                except Exception as exc:
                    self._install_errors.append(f"performance_log: {type(exc).__name__}: {exc}")
            # Remove legacy broad URL globs. Precise media filtering uses the
            # existing Fetch request handler, which parses path/type/query.
            self.driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": []})
        except Exception as exc:
            self._install_errors.append(f"network_cdp: {type(exc).__name__}: {exc}")
        if self.static_cache_enabled or self.low_traffic:
            self._install_fetch_cache()

    def set_session_only(self, enabled: bool = True) -> None:
        # Keep the application shell available for session/password/MFA steps.
        # This existing phase marker now stops shared cache reads and writes.
        with self._lock:
            self._session_only = bool(enabled)
            pending = list(self._loading_requests) if enabled else []
        for request_id in pending:
            self._release_loading_request(request_id)

    def disable_for_recovery(self, reason: str) -> None:
        """Restore normal networking when optimization correlates with a flow failure."""
        reason = str(reason or "registration_recovery").strip()[:200]
        self._degraded_reason = reason
        with self._lock:
            self._session_only = False
            self.static_cache_enabled = False
            was_fetch_enabled = self._fetch_enabled
            self._fetch_enabled = False
            pending = list(self._loading_requests)
        for request_id in pending:
            self._release_loading_request(request_id)
        if was_fetch_enabled and self._devtools and self._connection:
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
            patterns = []
            if self.static_cache_enabled:
                for resource in (devtools.network.ResourceType.SCRIPT, devtools.network.ResourceType.STYLESHEET):
                    patterns.append(devtools.fetch.RequestPattern(
                        url_pattern="https://cdn.openai.com/*", resource_type=resource,
                        request_stage=devtools.fetch.RequestStage.REQUEST,
                    ))
            if self.low_traffic:
                patterns.append(devtools.fetch.RequestPattern(
                    url_pattern="https://cdn.openai.com/assets/*", resource_type=devtools.network.ResourceType.MEDIA,
                    request_stage=devtools.fetch.RequestStage.REQUEST,
                ))
            self._devtools = devtools
            self._connection = connection
            connection.add_callback(devtools.fetch.RequestPaused, self._on_request_paused)
            self._fetch_enabled = True
            connection.execute(devtools.fetch.enable(patterns=patterns, handle_auth_requests=False))
        except Exception as exc:
            self._fetch_enabled = False
            self._install_errors.append(f"traffic_fetch: {type(exc).__name__}: {exc}")

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
        if not devtools or not connection or not self._fetch_enabled:
            return
        request_id = event.request_id
        claimed_url = ""
        try:
            if event.response_status_code is not None or getattr(event, "response_error_reason", None) is not None:
                self._handle_response(event)
                return
            url = str(getattr(event.request, "url", "") or "")
            method = str(getattr(event.request, "method", "") or "")
            resource = _resource_name(event.resource_type)
            if self.low_traffic and block_reason(url, resource):
                connection.execute(devtools.fetch.fail_request(request_id, devtools.network.ErrorReason.BLOCKED_BY_CLIENT))
                return
            if (self._session_only or not self.static_cache_enabled or not self._fetch_enabled
                    or not is_cacheable_request(url, method, resource, getattr(event.request, "headers", None))):
                connection.execute(devtools.fetch.continue_request(request_id))
                return
            with self._lock:
                self._stats["cache_candidates"] += 1
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
                if not self._fetch_enabled:
                    self._release_loading_request(request_id, claimed_url)
                    return
                if self._session_only or not self.static_cache_enabled:
                    self._release_loading_request(request_id, claimed_url)
                    connection.execute(devtools.fetch.continue_request(request_id))
                    return
                self._stats["cache_misses"] += 1
                connection.execute(devtools.fetch.continue_request(request_id, intercept_response=True))
        except Exception:
            if claimed_url:
                self._release_loading_request(request_id, claimed_url)
            with self._lock:
                if not self._fetch_enabled:
                    return
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
        with self._lock:
            if not self._fetch_enabled:
                return
            # Phase changes/expiry may happen during disk I/O or a cold wait.
            if (self._session_only or not self.static_cache_enabled
                    or time.time() >= cached["expires_at"]):
                connection.execute(devtools.fetch.continue_request(request_id))
                return
            headers = [
                devtools.fetch.HeaderEntry(name=item["name"], value=item["value"])
                for item in _sanitize_headers(cached["headers"])
                if item["name"] != "cache-control"
            ]
            # Do not restart origin max-age in the Profile's native cache.
            # Live responses retain their original HTTP caching/compression.
            headers.append(devtools.fetch.HeaderEntry(name="cache-control", value="no-store"))
            connection.execute(devtools.fetch.fulfill_request(
                request_id,
                response_code=cached["status"],
                response_headers=headers,
                body=base64.b64encode(cached["body"]).decode("ascii"),
                response_phrase=cached["phrase"],
            ))
            self._stats["cache_hits"] += 1
            self._stats["cached_bytes"] += len(cached["body"])
            if network_id:
                self._cached_request_ids.append(str(network_id))
            else:
                self._cached_urls.append(url)

    def _handle_response(self, event) -> None:
        devtools = self._devtools
        connection = self._connection
        if not devtools or not connection or not self._fetch_enabled:
            return
        request_id = event.request_id
        try:
            url = str(getattr(event.request, "url", "") or "")
            method = str(getattr(event.request, "method", "") or "")
            resource = _resource_name(event.resource_type)
            status = int(event.response_status_code or 0)
            if (not self._session_only and self.static_cache_enabled and self._fetch_enabled and status == 200
                    and is_cacheable_request(url, method, resource, getattr(event.request, "headers", None))
                    and is_cacheable_response(event.response_headers or [], resource_type=resource)):
                payload, encoded = connection.execute(devtools.fetch.get_response_body(request_id))
                body = base64.b64decode(payload) if encoded else str(payload).encode("utf-8")
                with self._lock:
                    # Recheck after get_response_body yields to another callback.
                    if not self._session_only and self.static_cache_enabled and self._fetch_enabled:
                        if self.cache.write(
                            url,
                            status=status,
                            phrase=str(event.response_status_text or "OK"),
                            headers=event.response_headers or [],
                            body=body,
                        ):
                            self._stats["cache_writes"] += 1
                        else:
                            self._stats["cache_errors"] += 1
            with self._lock:
                if self._fetch_enabled:
                    connection.execute(devtools.fetch.continue_response(request_id))
        except Exception:
            with self._lock:
                if not self._fetch_enabled:
                    return
                self._stats["cache_errors"] += 1
            try:
                connection.execute(devtools.fetch.continue_response(request_id))
            except Exception:
                pass
        finally:
            self._release_loading_request(request_id)

    def _release_loading_request(self, request_id, fallback_url: str = "") -> None:
        with self._lock:
            url = self._loading_requests.pop(str(request_id), fallback_url)
        if url:
            self.cache.release_load(url)

    def finalize(self) -> dict:
        with self._lock:
            was_fetch_enabled = self._fetch_enabled
            self._fetch_enabled = False
            loading_request_ids = list(self._loading_requests)
        for request_id in loading_request_ids:
            self._release_loading_request(request_id)
        if was_fetch_enabled and self._devtools and self._connection:
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
            "cache_candidates": stats["cache_candidates"],
            "cache_writes": stats["cache_writes"],
            "refresh_bytes": self._refresh_used,
            "session_only": self._session_only,
            "degraded_reason": self._degraded_reason,
            "errors": list(self._install_errors),
        })
        return summary
