"""Browserless, persistent Node V8 runtime for OpenAI Sentinel."""
from __future__ import annotations

import atexit
from collections import deque
from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urljoin
import uuid


class SentinelVMError(RuntimeError):
    pass


@dataclass(frozen=True)
class SentinelSDK:
    path: Path
    version: str
    url: str


_RUNTIME_DIR = Path(__file__).with_name("sentinel_vm")
_VERSION_PATTERN = re.compile(
    r"[\"']([^\"']*/sentinel/([A-Za-z0-9._-]+)/sdk\.js)[\"']",
    re.IGNORECASE,
)


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _response_text(response: Any, url: str) -> str:
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        raise SentinelVMError(f"Sentinel SDK request failed: HTTP {status} ({url})")
    text = str(getattr(response, "text", "") or "")
    if not text:
        raise SentinelVMError(f"Sentinel SDK request returned an empty body ({url})")
    return text


class SentinelSDKManager:
    """Discover and cache the current Sentinel SDK with a bundled fallback."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        refresh_seconds: float | None = None,
    ):
        configured_cache = os.environ.get("SENTINEL_SDK_CACHE_DIR", "").strip()
        self.cache_dir = cache_dir or (
            Path(configured_cache)
            if configured_cache
            else Path("data") / "sentinel-sdk"
        )
        if refresh_seconds is None:
            try:
                refresh_seconds = float(
                    os.environ.get("SENTINEL_SDK_REFRESH_SECONDS", "3600") or 3600
                )
            except ValueError:
                refresh_seconds = 3600
        self.refresh_seconds = max(float(refresh_seconds), 0.0)
        self._lock = threading.Lock()
        self._active: SentinelSDK | None = None
        self._checked_at = 0.0
        self.last_error = ""

    @staticmethod
    def _sdk_url(version: str) -> str:
        base = os.environ.get(
            "SENTINEL_BASE_URL", "https://sentinel.openai.com"
        ).rstrip("/")
        return f"{base}/sentinel/{version}/sdk.js"

    @staticmethod
    def _validate_sdk(code: str) -> None:
        if len(code) < 1000 or "SentinelSDK" not in code or ".token" not in code:
            raise SentinelVMError("Downloaded Sentinel SDK failed content validation")

    @staticmethod
    def _version_from(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _bundled_sdk(self) -> SentinelSDK:
        path = _RUNTIME_DIR / "sdk.js"
        version = self._version_from(_RUNTIME_DIR / "version.txt")
        if not path.is_file() or not version:
            raise SentinelVMError("Bundled Sentinel SDK or version.txt is missing")
        return SentinelSDK(path=path, version=version, url=self._sdk_url(version))

    def _cached_sdk(self) -> SentinelSDK | None:
        version = self._version_from(self.cache_dir / "version.txt")
        if not version or not re.fullmatch(r"[A-Za-z0-9._-]+", version):
            return None
        path = self.cache_dir / f"sdk-{version}.js"
        if not path.is_file():
            return None
        try:
            self._validate_sdk(path.read_text(encoding="utf-8"))
        except (OSError, SentinelVMError):
            return None
        return SentinelSDK(path=path, version=version, url=self._sdk_url(version))

    def _explicit_sdk(self) -> SentinelSDK | None:
        configured = os.environ.get("SENTINEL_SDK_PATH", "").strip()
        if not configured:
            return None
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise SentinelVMError(f"Configured Sentinel SDK does not exist: {path}")
        code = path.read_text(encoding="utf-8")
        self._validate_sdk(code)
        version = os.environ.get("SENTINEL_SDK_VERSION", "").strip()
        if not version:
            version = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
        return SentinelSDK(path=path, version=version, url=self._sdk_url(version))

    @staticmethod
    def _get(session: Any, url: str) -> str:
        response = session.get(
            url,
            headers={"accept": "text/html,application/javascript,*/*;q=0.8"},
            timeout=30,
        )
        return _response_text(response, url)

    def _store(self, version: str, url: str, code: str) -> SentinelSDK:
        self._validate_sdk(code)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        sdk_path = self.cache_dir / f"sdk-{version}.js"
        sdk_tmp = self.cache_dir / f".{sdk_path.stem}.{uuid.uuid4().hex}.tmp.js"
        version_path = self.cache_dir / "version.txt"
        version_tmp = self.cache_dir / f".version.{uuid.uuid4().hex}.tmp"
        try:
            sdk_tmp.write_text(code, encoding="utf-8")
            node = shutil.which(os.environ.get("SENTINEL_NODE_BINARY", "node"))
            if node:
                checked = subprocess.run(
                    [node, "--check", str(sdk_tmp)],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                if checked.returncode:
                    detail = (checked.stderr or checked.stdout).strip()[:300]
                    raise SentinelVMError(f"Downloaded Sentinel SDK is invalid: {detail}")
            os.replace(sdk_tmp, sdk_path)
            version_tmp.write_text(version, encoding="utf-8")
            os.replace(version_tmp, version_path)
        finally:
            for temporary in (sdk_tmp, version_tmp):
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return SentinelSDK(path=sdk_path, version=version, url=url)

    def resolve(self, session: Any) -> SentinelSDK:
        explicit = self._explicit_sdk()
        if explicit is not None:
            return explicit

        with self._lock:
            now = time.monotonic()
            current = self._active or self._cached_sdk() or self._bundled_sdk()
            if not _env_truthy("SENTINEL_SDK_AUTO_UPDATE", True):
                self._active = current
                return current
            if self._checked_at and now - self._checked_at < self.refresh_seconds:
                return current

            self._checked_at = now
            discovery_url = os.environ.get(
                "SENTINEL_SDK_DISCOVERY_URL",
                "https://sentinel.openai.com/backend-api/sentinel/frame.html",
            ).strip()
            try:
                frame = self._get(session, discovery_url)
                match = _VERSION_PATTERN.search(frame)
                if not match:
                    raise SentinelVMError(
                        "Sentinel frame did not advertise an SDK version"
                    )
                sdk_url = urljoin(discovery_url, match.group(1))
                version = match.group(2)
                if current.version == version:
                    self._active = current
                else:
                    code = self._get(session, sdk_url)
                    self._active = self._store(version, sdk_url, code)
                self.last_error = ""
            except Exception as exc:
                self.last_error = str(exc)
                self._active = current
            return self._active


def _configured_worker_count() -> int:
    # 默认 3 个 Node 进程，每进程通过事件循环并发处理多个请求。
    # 不再用 os.cpu_count() —— 进程数由部署者显式设置，而非服务器核数。
    default = 3
    try:
        value = int(
            os.environ.get("CHATGPT_SENTINEL_VM_WORKERS", str(default)) or default
        )
    except ValueError:
        value = default
    return min(max(value, 1), 8)


class _NodeSentinelWorker:
    def __init__(self, *, timeout: float = 40.0, timezone: str | None = None):
        self.timeout = max(float(timeout or 40.0), 1.0)
        self.timezone = timezone or os.environ.get(
            "CHATGPT_SENTINEL_TIMEZONE", "America/New_York"
        )
        # 生命周期锁：仅保护 start/stop/restart，不锁 _request()。
        # _request() 每次创建独立 HTTPConnection，天然线程安全。
        self._lifecycle_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._port = 0
        self._stderr = deque(maxlen=30)

    @staticmethod
    def _server_script() -> Path:
        return _RUNTIME_DIR / "sentinel-server.js"

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            self._stderr.append(line.rstrip())

    def _start(self) -> None:
        node_binary = str(os.environ.get("SENTINEL_NODE_BINARY", "node") or "node")
        resolved_node = shutil.which(node_binary)
        if not resolved_node:
            raise SentinelVMError("Node.js is required for the Sentinel V8 runtime")
        server_script = self._server_script()
        if not server_script.is_file():
            raise SentinelVMError(f"Sentinel V8 server is missing: {server_script}")

        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = os.environ.copy()
        env["SENTINEL_SERVER_PORT"] = "0"
        env["SENTINEL_TZ"] = self.timezone
        self._stderr.clear()
        process = subprocess.Popen(
            [resolved_node, str(server_script)],
            cwd=str(_RUNTIME_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags,
        )
        self._process = process
        threading.Thread(
            target=self._read_stderr,
            args=(process,),
            daemon=True,
            name="sentinel-v8-stderr",
        ).start()

        ready: queue.Queue[str] = queue.Queue(maxsize=1)

        def read_ready() -> None:
            line = process.stdout.readline() if process.stdout else ""
            ready.put(line)

        threading.Thread(target=read_ready, daemon=True).start()
        try:
            line = ready.get(timeout=min(self.timeout, 15.0))
            payload = json.loads(line)
            port = int(payload.get("port") or 0)
            if not payload.get("ready") or port <= 0:
                raise ValueError("invalid ready response")
            self._port = port
        except Exception as exc:
            detail = "; ".join(self._stderr) or "no ready response"
            self._stop()
            raise SentinelVMError(
                f"Sentinel V8 server failed to start: {detail}"
            ) from exc

    def _stop(self) -> None:
        process = self._process
        self._process = None
        self._port = 0
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def close(self) -> None:
        with self._lifecycle_lock:
            self._stop()

    def _ensure_started(self) -> None:
        """快速检查：进程已运行则立即返回，不阻塞并发请求。"""
        if self._process is not None and self._process.poll() is None and self._port:
            return
        with self._lifecycle_lock:
            # 双重检查：可能另一个线程刚好启动了进程
            if self._process is not None and self._process.poll() is None and self._port:
                return
            self._stop()
            self._start()

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self._port, timeout=self.timeout
        )
        try:
            connection.request(
                "POST",
                "/token",
                body=body,
                headers={
                    "content-type": "application/json",
                    "content-length": str(len(body)),
                },
            )
            response = connection.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if response.status >= 400 or not data.get("ok"):
                raise SentinelVMError(
                    str(data.get("error") or f"Sentinel V8 HTTP {response.status}")
                )
            token_text = str(data.get("token") or "")
            token = json.loads(token_text)
            if not isinstance(token, dict):
                raise SentinelVMError("Sentinel V8 server returned an invalid token")
            return token
        finally:
            connection.close()

    def execute(self, **payload: Any) -> dict[str, Any]:
        """发送请求到 Node 进程。不持锁——Node 事件循环天然并发。"""
        self._ensure_started()
        try:
            return self._request(payload)
        except SentinelVMError:
            raise
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            # 连接层错误：重启进程后重试一次
            with self._lifecycle_lock:
                self._stop()
                self._start()
            try:
                return self._request(payload)
            except SentinelVMError:
                raise
            except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc2:
                detail = "; ".join(self._stderr) or str(exc2)
                raise SentinelVMError(
                    f"Sentinel V8 worker failed: {detail}"
                ) from exc2


class SentinelVMPool:
    def __init__(self, worker_count: int | None = None):
        count = _configured_worker_count() if worker_count is None else worker_count
        count = min(max(int(count), 1), 8)
        self._workers = [_NodeSentinelWorker() for _ in range(count)]
        self._next = 0
        self._next_lock = threading.Lock()

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    def execute(self, **payload: Any) -> dict[str, Any]:
        # 轮询分发，不限流 —— 每个 worker 内部 Node 事件循环天然并发。
        # 不阻塞：25+ 个并发请求均匀分到 2 个 worker，各自独立处理。
        with self._next_lock:
            worker = self._workers[self._next % len(self._workers)]
            self._next += 1
        return worker.execute(**payload)

    def close(self) -> None:
        for worker in self._workers:
            worker.close()


_sdk_manager = SentinelSDKManager()
_pool_lock = threading.Lock()
_pool: SentinelVMPool | None = None


def get_sentinel_sdk(session: Any) -> SentinelSDK:
    return _sdk_manager.resolve(session)


def get_sentinel_vm_pool() -> SentinelVMPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = SentinelVMPool()
        return _pool


def close_sentinel_vm_pool() -> None:
    global _pool
    with _pool_lock:
        pool = _pool
        _pool = None
    if pool is not None:
        pool.close()


atexit.register(close_sentinel_vm_pool)
