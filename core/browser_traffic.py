# -*- coding: utf-8 -*-
"""Roxy/Selenium registration traffic filtering, cache replay, and metering."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
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
# The registration flow does not use these optional browser services.  Keep
# challenge/authentication hosts outside this list; block_reason() performs the
# security-path allow check before applying the low-traffic policy.
TELEMETRY_SUFFIXES = (
    "browser-intake-datadoghq.com", "statsigapi.net", "featuregates.org",
    "segment.io", "segment.com", "sentry.io",
)
OPTIONAL_IDENTITY_SUFFIXES = (
    "accounts.google.com", "appleid.apple.com", "login.microsoftonline.com",
)
LOW_TRAFFIC_FIRST_PARTY_HOSTS = {
    "chatgpt.com", "auth.openai.com", "cdn.openai.com", "auth-cdn.oaistatic.com",
    "oaistatic.com",
}
LOW_TRAFFIC_RESOURCE_TYPES = {"image", "media", "font", "manifest"}
# After the account session is established, only these ChatGPT auth endpoints
# remain live.  The application shell and background API polling otherwise
# keep downloading bundles while the worker is finishing export/2FA.
SESSION_REQUIRED_PREFIXES = (
    "/api/auth/callback/", "/api/auth/signin/",
)
SESSION_REQUIRED_PATHS = {
    "/api/auth/session", "/api/auth/csrf",
    "/backend-api/accounts/mfa/enroll",
    "/backend-api/accounts/mfa/user/activate_enrollment",
}
# Auth RUM is a browser telemetry batch and is not part of registration,
# password, OTP, or MFA state.  Its payloads are several megabytes per
# account, so the low-traffic policy intercepts only this exact endpoint.
AUTH_RUM_PATH = "/awe/api/v2/rum"
SECURITY_SUFFIXES = (
    "arkoselabs.com", "challenges.cloudflare.com", "hcaptcha.com",
    "recaptcha.net", "sentinel.openai.com",
)
# Only body/representation headers are replayable, never origin/profile state,
# tracing identifiers, CSP nonces, or client-hint negotiation.
REPLAY_ALLOWED_HEADERS = {
    "content-type", "cache-control", "access-control-allow-origin",
    "cross-origin-resource-policy", "x-content-type-options",
}
CACHE_SCHEMA_VERSION = 3
CACHE_PRIVATE_REQUEST_HEADERS = {"authorization", "proxy-authorization", "range"}
CACHE_PRIVATE_X_HEADERS = {
    "x-api-key", "x-csrf-token", "x-device-id", "x-session-id",
    "x-openai-token", "x-openai-account-id", "x-auth-token", "x-session-token",
}
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
CACHE_ALLOWED_ORIGINS = {"*", "https://chatgpt.com", "https://www.chatgpt.com", "https://auth.openai.com"}
# A cold cache can make every concurrent Profile request the same large public
# bundle.  Single-flight only the validated public asset load; registration
# workers and Profile/browser concurrency remain unchanged.  A short timeout
# lets a stalled Profile fall back to its own live request.
CACHE_LOAD_WAIT_SECONDS = 8.0
PUBLIC_STATIC_PATH_PREFIXES = {
    PUBLIC_CDN_HOST: ("/assets/", "/cdn/assets/", "/_next/static/", "/unauth-mweb/assets/"),
    "chatgpt.com": ("/cdn/assets/",),
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
    """Block optional registration resources while allowing security/auth flows."""
    try:
        parsed = urlparse(str(url or ""))
        host = str(parsed.hostname or "").lower()
        if (parsed.scheme != "https" or not host
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None):
            return ""
    except ValueError:
        return ""
    path = parsed.path
    resource = _resource_name(resource_type)
    host_matches = lambda suffix: host == suffix or host.endswith("." + suffix)
    lower_path = path.lower()
    if host == "auth.openai.com" and lower_path == AUTH_RUM_PATH:
        return "auth_rum"
    if any(host_matches(suffix) for suffix in SECURITY_SUFFIXES):
        return ""
    if ("/cdn-cgi/challenge-platform/" in lower_path or "/sentinel/" in lower_path
            or any(segment in {".", "..", "recaptcha", "hcaptcha", "captcha", "challenge"}
                   for segment in lower_path.split("/"))):
        return ""
    if any(host_matches(suffix) for suffix in TELEMETRY_SUFFIXES):
        return "telemetry"
    if any(host_matches(suffix) for suffix in OPTIONAL_IDENTITY_SUFFIXES):
        return "optional_identity"
    if host in LOW_TRAFFIC_FIRST_PARTY_HOSTS and resource in LOW_TRAFFIC_RESOURCE_TYPES:
        if (parsed.query or parsed.fragment or "?" in str(url) or "#" in str(url)
                or "%" in path or "\\" in path
                or any(segment in {".", ".."} for segment in path.split("/"))):
            return ""
        if resource == "media" and host == PUBLIC_CDN_HOST and not lower_path.startswith("/assets/"):
            return ""
        if resource == "media" and not lower_path.endswith(OPTIONAL_MEDIA_EXTENSIONS):
            return "optional_media"
        return "optional_" + resource
    if (host == PUBLIC_CDN_HOST and resource == "media" and path.startswith("/assets/")
            and path.endswith(OPTIONAL_MEDIA_EXTENSIONS)):
        return "optional_media"
    if session_only and host in {"chatgpt.com", "www.chatgpt.com"}:
        if (resource == "document" or lower_path in SESSION_REQUIRED_PATHS
                or lower_path.startswith(SESSION_REQUIRED_PREFIXES)):
            return ""
        return "post_auth_" + (resource or "other")
    return ""


def is_cacheable_request(url: str, method: str, resource_type: str, headers=None) -> bool:
    """Share only versioned public JS/CSS on explicit static asset routes.

    Ambient Cookie headers alone do not make a public file private; neither
    request cookies nor response state enter the cache. Response public/TTL,
    MIME, Set-Cookie and Vary gates must pass before any body is shared.
    Authentication, configuration and every other same-origin path stay live.
    """
    resource = _resource_name(resource_type)
    if str(method or "").upper() != "GET" or resource not in STATIC_RESOURCE_MIME_TYPES:
        return False
    try:
        parsed = urlparse(str(url or ""))
        if (parsed.scheme != "https" or parsed.hostname not in PUBLIC_STATIC_PATH_PREFIXES
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            return False
    except ValueError:
        return False
    path = parsed.path
    if ("%" in path or "\\" in path or "?" in str(url) or "#" in str(url)
            or any(segment in {".", "..", "sentinel", "recaptcha", "hcaptcha", "captcha", "challenge"} for segment in path.lower().split("/"))
            or "/cdn-cgi/challenge-platform/" in path.lower()
            or not path.startswith(PUBLIC_STATIC_PATH_PREFIXES[parsed.hostname])):
        return False
    extension = ".js" if resource == "script" else ".css"
    filename = path.rsplit("/", 1)[-1]
    if (not filename.endswith(extension)
            or not re.search(r"(?:^|[._-])[0-9a-fA-F]{8,64}$|[._-](?=[a-z0-9]*[0-9])(?:[a-z0-9]{8}|[a-z0-9]{16})$", filename[:-len(extension)])):
        return False
    if any(marker in filename.lower() for marker in ("service-worker", "serviceworker", "sw.", "sentinel", "captcha", "challenge")):
        return False
    request_headers = _header_values(headers)
    if (set(request_headers) & CACHE_PRIVATE_REQUEST_HEADERS
            or any(name.startswith("if-") for name in request_headers)
            or (set(request_headers) & CACHE_PRIVATE_X_HEADERS)):
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
    if ("public" not in directives
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
    """Require a fresh, non-varying JS/CSS public representation."""
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
    origin = values.get("access-control-allow-origin", "").strip().lower()
    if ((origin and origin not in CACHE_ALLOWED_ORIGINS)
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
                               blocked_request_ids=(),
                               budget_bytes: int = 3 * 1024 * 1024) -> dict:
    """Estimate external browser payload bytes, not proxy-billed wire traffic.

    CDP omits transport framing/retransmits and can omit POST bodies. Native
    cache hits and loopback traffic are not external transfers. Redirects reuse
    requestId, so each hop must be settled independently before its replacement.
    """
    requests: dict[str, dict] = {}
    hops: list[dict] = []
    websocket_urls: dict[str, str] = {}
    exact_cached_request_ids = {str(request_id) for request_id in (cached_request_ids or ()) if request_id}
    exact_blocked_request_ids = {str(request_id) for request_id in (blocked_request_ids or ()) if request_id}
    cached_url_counts = Counter(str(url) for url in (cached_request_urls or ()) if url)
    downloaded = 0
    uploaded = 0
    websocket_received = 0
    websocket_sent = 0
    network_requests = 0
    blocked_by_reason: dict[str, int] = defaultdict(int)
    by_host: dict[str, int] = defaultdict(int)
    by_path: dict[str, int] = defaultdict(int)
    uploaded_by_host: dict[str, int] = defaultdict(int)
    uploaded_by_path: dict[str, int] = defaultdict(int)

    def external_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme not in {"http", "https", "ws", "wss"} or not host:
                return False
            if host == "localhost" or host.endswith(".localhost"):
                return False
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                return True
            mapped = getattr(address, "ipv4_mapped", None)
            return not (address.is_loopback or (mapped and mapped.is_loopback))
        except ValueError:
            return False

    def byte_count(value) -> int:
        try:
            return max(0, int(float(value or 0)))
        except (TypeError, ValueError, OverflowError):
            return 0

    def new_hop(request_id: str) -> dict:
        hop = {"id": request_id, "url": "", "resource": "", "started": False,
               "upload": 0, "received": 0, "finished": None, "response": False,
               "response_bytes": 0, "native_cache": False, "revalidated": False,
               "replayed": False, "redirected": False,
               "blocked_reason": "", "error": ""}
        requests[request_id] = hop
        hops.append(hop)
        return hop

    def record_response(hop: dict, response: dict) -> None:
        hop["response"] = True
        hop["response_bytes"] = max(hop["response_bytes"], byte_count(response.get("encodedDataLength")))
        if not hop["url"]:
            hop["url"] = str(response.get("url") or "")
        # A service worker may fetch live data: fromServiceWorker alone is not
        # proof of a cache hit. Only explicit local response sources qualify.
        hop["native_cache"] |= bool(
            response.get("fromDiskCache") or response.get("fromPrefetchCache")
            or response.get("serviceWorkerResponseSource") in {"cache-storage", "http-cache", "fallback-code"}
        )

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
            request = params.get("request") or {}
            url = str(request.get("url") or "")
            hop = requests.get(request_id)
            redirect = params.get("redirectResponse")
            if redirect is not None:
                hop = hop or new_hop(request_id)
                record_response(hop, redirect)
                hop["finished"] = byte_count(redirect.get("encodedDataLength"))
                hop["redirected"] = True
                hop = None
            hop = hop or new_hop(request_id)
            hop.update(url=url, resource=str(params.get("type") or ""), started=True)
            post_data = request.get("postData")
            hop["upload"] = len(str(post_data).encode("utf-8")) if post_data else 0
            # Exact Fetch identities apply to the final hop, not a preceding
            # redirect with the same id. URL fallback is for older CDP builds.
            if request_id not in exact_cached_request_ids and cached_url_counts[url] > 0:
                cached_url_counts[url] -= 1
                hop["replayed"] = True
        elif method == "Network.responseReceived":
            hop = requests.get(request_id) or new_hop(request_id)
            record_response(hop, params.get("response") or {})
        elif method == "Network.requestServedFromCache":
            hop = requests.get(request_id) or new_hop(request_id)
            hop["native_cache"] = True
        elif method == "Network.responseReceivedExtraInfo" and params.get("statusCode") == 304:
            hop = requests.get(request_id) or new_hop(request_id)
            hop["revalidated"] = True
        elif method == "Network.loadingFinished":
            hop = requests.get(request_id) or new_hop(request_id)
            hop["finished"] = byte_count(params.get("encodedDataLength"))
        elif method == "Network.dataReceived":
            hop = requests.get(request_id) or new_hop(request_id)
            hop["received"] += byte_count(params.get("encodedDataLength"))
        elif method == "Network.loadingFailed":
            hop = requests.get(request_id) or new_hop(request_id)
            hop["blocked_reason"] = str(params.get("blockedReason") or "")
            hop["error"] = str(params.get("errorText") or "")
        elif method == "Network.webSocketCreated":
            socket_id = str(params.get("requestId") or "")
            if socket_id:
                websocket_urls[socket_id] = str(params.get("url") or "")
        elif method in {"Network.webSocketFrameSent", "Network.webSocketFrameReceived"}:
            if request_id in websocket_urls and not external_url(websocket_urls[request_id]):
                continue
            frame = params.get("response") or {}
            payload = str(frame.get("payloadData") or "")
            if frame.get("opcode", 1) == 1:
                size = len(payload.encode("utf-8"))
            else:
                try:
                    size = len(base64.b64decode(payload, validate=True))
                except (ValueError, TypeError):
                    size = 0
            if method == "Network.webSocketFrameSent":
                websocket_sent += size
            else:
                websocket_received += size

    data_received_fallback = 0
    for hop in hops:
        url = hop["url"]
        if url and not external_url(url):
            continue
        request_id = hop["id"]
        replayed = hop["replayed"] or (not hop["redirected"] and request_id in exact_cached_request_ids)
        known_blocked = not hop["redirected"] and request_id in exact_blocked_request_ids
        block = hop["blocked_reason"]
        client_block = hop["error"] in {"net::ERR_BLOCKED_BY_CLIENT", "net::ERR_BLOCKED_BY_ADMINISTRATOR"}
        if block or client_block or known_blocked:
            reason = block_reason(url, hop["resource"]) or block or "inspector"
            blocked_by_reason[reason] += 1
        # Response-side CORP/CORS/integrity rejection is not free traffic.
        # Suppress a body only for a known pre-request rejection and only while
        # there is no evidence that this particular hop reached the network.
        sent = hop["response"] or hop["received"] > 0 or hop["finished"] is not None
        blocked_before_send = not sent and (
            known_blocked or client_block or block in {"inspector", "csp", "mixed-content", "subresource-filter"}
        )
        if replayed or (hop["native_cache"] and not hop["revalidated"]) or blocked_before_send:
            continue
        network_requests += int(hop["started"])
        size = hop["finished"]
        if size is None:
            size = max(hop["received"], hop["response_bytes"])
            data_received_fallback += size
        downloaded += size
        uploaded += hop["upload"]
        parsed = urlparse(url)
        if parsed.hostname:
            host = parsed.hostname.lower()
            path = f"{host}{parsed.path or '/'}"
            if size:
                by_host[host] += size
                by_path[path] += size
            if hop["upload"]:
                uploaded_by_host[host] += hop["upload"]
                uploaded_by_path[path] += hop["upload"]

    observed = downloaded + uploaded + websocket_sent + websocket_received
    top_paths = sorted(by_path.items(), key=lambda item: item[1], reverse=True)[:20]
    return {
        "downloaded": downloaded,
        "uploaded": uploaded,
        "websocket_sent": websocket_sent,
        "websocket_received": websocket_received,
        "data_received_fallback": data_received_fallback,
        "observed_transport_bytes": observed,
        "logical_downloaded": downloaded + max(0, int(cached_bytes)),
        "cached_downloaded": max(0, int(cached_bytes)),
        "cache_saved_bytes": max(0, int(cached_bytes)),
        "cache_hits": max(0, int(cache_hits)),
        "cache_misses": max(0, int(cache_misses)),
        "network_requests": network_requests,
        "blocked": sum(blocked_by_reason.values()),
        "blocked_by_reason": dict(sorted(blocked_by_reason.items())),
        "by_host": dict(sorted(by_host.items(), key=lambda item: item[1], reverse=True)),
        "by_path": dict(top_paths),
        "uploaded_by_host": dict(sorted(uploaded_by_host.items(), key=lambda item: item[1], reverse=True)),
        "uploaded_by_path": dict(sorted(uploaded_by_path.items(), key=lambda item: item[1], reverse=True)[:20]),
        "budget_bytes": max(0, int(budget_bytes)),
        "within_budget": observed <= max(0, int(budget_bytes)),
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
        self._blocked_request_ids: set[str] = set()
        self._loading_requests: dict[str, str] = {}
        self._install_errors: list[str] = []
        self._degraded_reason = ""
        self._performance_entries: list[dict | str] = []
        self._performance_drain_stop = threading.Event()
        self._performance_drain_thread: threading.Thread | None = None

    def _drain_performance_log(self) -> None:
        if not self.capture:
            return
        try:
            entries = list(self.driver.get_log("performance") or [])
        except Exception as exc:
            with self._lock:
                self._install_errors.append(f"performance_drain: {type(exc).__name__}: {exc}")
            return
        if entries:
            with self._lock:
                self._performance_entries.extend(entries)

    def _performance_log_pump(self) -> None:
        # Selenium's performance log is a finite ring buffer.  Drain it while
        # the Profile is running instead of waiting until finalize(), otherwise
        # concurrent registration drops most loadingFinished events.
        while not self._performance_drain_stop.wait(0.5):
            self._drain_performance_log()

    def _start_performance_log_pump(self) -> None:
        if not self.capture or self._performance_drain_thread is not None:
            return
        self._performance_drain_stop.clear()
        self._performance_drain_thread = threading.Thread(
            target=self._performance_log_pump,
            name="roxy-performance-drain",
            daemon=True,
        )
        self._performance_drain_thread.start()

    def _enable_network_domain(self) -> None:
        """Enable Network with runtime defaults; payload buffers are not wire meters."""
        self.driver.execute_cdp_cmd("Network.enable", {})

    def install(self) -> None:
        if not (self.low_traffic or self.static_cache_enabled or self.capture):
            return
        try:
            self._enable_network_domain()
            self.driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": False})
            self.driver.execute_cdp_cmd("Network.setBypassServiceWorker", {"bypass": False})
            if self.capture:
                self._drain_performance_log()
                self._start_performance_log_pump()
            # Remove legacy broad URL globs. Precise media filtering uses the
            # existing Fetch request handler, which parses path/type/query.
            self.driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": []})
        except Exception as exc:
            self._install_errors.append(f"network_cdp: {type(exc).__name__}: {exc}")
        if self.static_cache_enabled or self.low_traffic:
            self._install_fetch_cache()

    def set_session_only(self, enabled: bool = True) -> None:
        # The existing Fetch callback reads this phase marker. Password/MFA
        # use native fetch on the retained auth/MFA endpoints, not app bundles.
        with self._lock:
            self._session_only = bool(enabled)

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
                for host, prefixes in PUBLIC_STATIC_PATH_PREFIXES.items():
                    for prefix in prefixes:
                        for resource in (devtools.network.ResourceType.SCRIPT, devtools.network.ResourceType.STYLESHEET):
                            patterns.append(devtools.fetch.RequestPattern(
                                url_pattern=f"https://{host}{prefix}*", resource_type=resource,
                                request_stage=devtools.fetch.RequestStage.REQUEST,
                            ))
            if self.low_traffic:
                # Register the whole ChatGPT request surface once, so phase
                # changes also reach scripts/XHR when shared cache is off.
                # The classifier still allows them until session-only begins.
                for host in ("chatgpt.com", "www.chatgpt.com"):
                    patterns.append(devtools.fetch.RequestPattern(
                        url_pattern=f"https://{host}/*",
                        request_stage=devtools.fetch.RequestStage.REQUEST,
                    ))
                # Pause optional first-party resources and known telemetry /
                # social-login hosts.  The handler keeps challenge and auth
                # paths live, so the broad patterns do not become a second
                # security or session classifier.  Use a URL-only manifest
                # pattern: older Roxy/CDP builds reject the Manifest resource
                # enum and then discard every Fetch rule.
                for host in LOW_TRAFFIC_FIRST_PARTY_HOSTS:
                    if host == "chatgpt.com":
                        continue  # Already covered by its URL-only pattern.
                    for resource in (
                        devtools.network.ResourceType.IMAGE,
                        devtools.network.ResourceType.MEDIA,
                        devtools.network.ResourceType.FONT,
                    ):
                        patterns.append(devtools.fetch.RequestPattern(
                            url_pattern=f"https://{host}/*", resource_type=resource,
                            request_stage=devtools.fetch.RequestStage.REQUEST,
                        ))
                    patterns.append(devtools.fetch.RequestPattern(
                        url_pattern=f"https://{host}/*manifest*",
                        request_stage=devtools.fetch.RequestStage.REQUEST,
                    ))
                for suffix in TELEMETRY_SUFFIXES + OPTIONAL_IDENTITY_SUFFIXES:
                    for host_pattern in (f"https://{suffix}/*", f"https://*.{suffix}/*"):
                        # A URL-only pattern covers XHR/fetch/script/document
                        # variants and avoids a large per-resource pattern
                        # matrix that older Roxy CDP builds reject.
                        patterns.append(devtools.fetch.RequestPattern(
                            url_pattern=host_pattern,
                            request_stage=devtools.fetch.RequestStage.REQUEST,
                        ))
                # The Auth RUM batch is not required for registration and is
                # the dominant per-account upload (about 3 MB).  Keep this
                # one exact path separate from auth pages and challenge URLs.
                patterns.append(devtools.fetch.RequestPattern(
                    url_pattern="https://auth.openai.com/awe/api/v2/rum*",
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
            if self.low_traffic and block_reason(url, resource, session_only=self._session_only):
                connection.execute(devtools.fetch.fail_request(request_id, devtools.network.ErrorReason.BLOCKED_BY_CLIENT))
                network_id = getattr(event, "network_id", None)
                if network_id:
                    with self._lock:
                        self._blocked_request_ids.add(str(network_id))
                return
            if (not self.static_cache_enabled or not self._fetch_enabled
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
                if not self.static_cache_enabled:
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
            if (not self.static_cache_enabled
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
            if (self.static_cache_enabled and self._fetch_enabled and status == 200
                    and is_cacheable_request(url, method, resource, getattr(event.request, "headers", None))
                    and is_cacheable_response(event.response_headers or [], resource_type=resource)):
                payload, encoded = connection.execute(devtools.fetch.get_response_body(request_id))
                body = base64.b64decode(payload) if encoded else str(payload).encode("utf-8")
                with self._lock:
                    # Recheck after get_response_body yields to another callback.
                    if self.static_cache_enabled and self._fetch_enabled:
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
        self._performance_drain_stop.set()
        drain_thread = self._performance_drain_thread
        if drain_thread is not None:
            drain_thread.join(timeout=2.0)
            self._performance_drain_thread = None
        self._drain_performance_log()
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
            with self._lock:
                entries = list(self._performance_entries)
        with self._lock:
            stats = dict(self._stats)
            cached_urls = list(self._cached_urls)
            cached_request_ids = list(self._cached_request_ids)
            blocked_request_ids = set(self._blocked_request_ids)
        summary = summarize_performance_logs(
            entries,
            cached_bytes=stats["cached_bytes"],
            cache_hits=stats["cache_hits"],
            cache_misses=stats["cache_misses"],
            cached_request_urls=cached_urls,
            cached_request_ids=cached_request_ids,
            blocked_request_ids=blocked_request_ids,
            budget_bytes=self.budget_bytes,
        )
        summary.update({
            "metrics_version": 4,
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
