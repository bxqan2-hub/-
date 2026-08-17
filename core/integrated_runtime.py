# -*- coding: utf-8 -*-
"""Load the two vendored payment projects inside the main WebUI process."""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import sys
import threading
from http import HTTPStatus
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Callable

from werkzeug.wrappers import Request


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
INTEGRATIONS_ROOT = ROOT / "integrations"
PAY153_ROOT = INTEGRATIONS_ROOT / "pay153_checkout"
PAYPAL_ROOT = INTEGRATIONS_ROOT / "paypal_agreement_protocol"
sys.dont_write_bytecode = True

_LOAD_LOCK = threading.RLock()
_PAY153_MODULE: ModuleType | None = None
_PAYPAL_MODULE: ModuleType | None = None
_PAYPAL_WSGI: Callable | None = None
_MAIN_PORT = 5000


def _load_file(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载集成模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def get_pay153_module() -> ModuleType:
    global _PAY153_MODULE
    with _LOAD_LOCK:
        if _PAY153_MODULE is not None:
            return _PAY153_MODULE
        if not PAY153_ROOT.is_dir():
            raise RuntimeError(f"PAY.153 集成目录不存在：{PAY153_ROOT}")
        sys.path.insert(0, str(PAY153_ROOT))
        try:
            _PAY153_MODULE = _load_file("_turb_integrated_pay153", PAY153_ROOT / "app.py")
        finally:
            try:
                sys.path.remove(str(PAY153_ROOT))
            except ValueError:
                pass
        return _PAY153_MODULE


def _load_paypal_module() -> ModuleType:
    global _PAYPAL_MODULE
    with _LOAD_LOCK:
        if _PAYPAL_MODULE is not None:
            return _PAYPAL_MODULE
        if not PAYPAL_ROOT.is_dir():
            raise RuntimeError(f"PayPal 集成目录不存在：{PAYPAL_ROOT}")

        existing_paypal = sys.modules.get("paypal")
        if existing_paypal is not None:
            existing_path = Path(str(getattr(existing_paypal, "__file__", ""))).resolve()
            if PAYPAL_ROOT.resolve() not in existing_path.parents:
                raise RuntimeError(f"检测到冲突的 paypal Python 包：{existing_path}")

        paypal_config = _load_file(
            "_turb_integrated_paypal_config", PAYPAL_ROOT / "config.py"
        )
        previous_config = sys.modules.get("config")
        sys.modules["config"] = paypal_config
        sys.path.insert(0, str(PAYPAL_ROOT))
        try:
            _PAYPAL_MODULE = _load_file(
                "_turb_integrated_paypal_web", PAYPAL_ROOT / "web.py"
            )
        finally:
            try:
                sys.path.remove(str(PAYPAL_ROOT))
            except ValueError:
                pass
            if previous_config is None:
                sys.modules.pop("config", None)
            else:
                sys.modules["config"] = previous_config

        _PAYPAL_MODULE.PAY153_INTERNAL_BASE = f"http://127.0.0.1:{_MAIN_PORT}/pay153"

        def pay153_internal_post(path: str, payload: dict):
            from werkzeug.test import Client
            from werkzeug.wrappers import Response

            response = Client(
                get_pay153_module().app, Response, use_cookies=False,
            ).post(str(path), json=payload)
            return SimpleNamespace(
                status_code=response.status_code,
                text=response.get_data(as_text=True),
                json=lambda: response.get_json(silent=True) or {},
            )

        _PAYPAL_MODULE.PAY153_INTERNAL_POST = pay153_internal_post
        _PAYPAL_MODULE.configure_logging()
        _PAYPAL_MODULE.STATIC_DIR.mkdir(exist_ok=True)
        return _PAYPAL_MODULE


def configure_single_port(port: int) -> None:
    """Configure internal cross-project callbacks to use the sole public port."""
    global _MAIN_PORT
    _MAIN_PORT = int(port)
    os.environ["PAYPAL_WEB_PAY153_INTERNAL_BASE"] = f"http://127.0.0.1:{_MAIN_PORT}/pay153"
    with _LOAD_LOCK:
        if _PAYPAL_MODULE is not None:
            _PAYPAL_MODULE.PAY153_INTERNAL_BASE = os.environ["PAYPAL_WEB_PAY153_INTERNAL_BASE"]


def _build_paypal_wsgi() -> Callable:
    module = _load_paypal_module()

    class InProcessPayPalHandler(module.WebHandler):
        def send_response(self, code, message=None):
            self._status_code = int(code)
            self._response_headers = []

        def send_header(self, keyword, value):
            self._response_headers.append((str(keyword), str(value)))

        def end_headers(self):
            return None

        def log_request(self, code="-", size="-"):
            return None

    def paypal_wsgi(environ, start_response):
        req = Request(environ)
        handler = InProcessPayPalHandler.__new__(InProcessPayPalHandler)
        handler.command = req.method.upper()
        handler.path = req.full_path[:-1] if req.full_path.endswith("?") else req.full_path
        handler.request_version = str(environ.get("SERVER_PROTOCOL") or "HTTP/1.1")
        handler.requestline = f"{handler.command} {handler.path} {handler.request_version}"
        handler.client_address = (str(environ.get("REMOTE_ADDR") or "127.0.0.1"), 0)
        handler.server = SimpleNamespace(
            server_name=str(environ.get("SERVER_NAME") or "localhost"),
            server_port=int(environ.get("SERVER_PORT") or _MAIN_PORT),
        )
        handler.headers = req.headers
        handler.rfile = io.BytesIO(req.get_data(cache=True))
        handler.wfile = io.BytesIO()
        handler.close_connection = True
        handler._status_code = 200
        handler._response_headers = []
        try:
            if handler.command in {"GET", "HEAD"}:
                handler.do_GET()
            elif handler.command == "POST":
                handler.do_POST()
            else:
                handler.send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "请求方法不支持")
            body = b"" if handler.command == "HEAD" else handler.wfile.getvalue()
        except Exception as exc:
            logger.exception("PayPal 单端口处理异常")
            handler._status_code = 500
            body = json.dumps(
                {"ok": False, "error": f"PayPal 服务异常：{type(exc).__name__}: {str(exc)[:300]}"},
                ensure_ascii=False,
            ).encode("utf-8")
            handler._response_headers = [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ]
        status = HTTPStatus(handler._status_code)
        start_response(f"{status.value} {status.phrase}", handler._response_headers)
        return [body]

    return paypal_wsgi


def get_wsgi_app(service: str) -> Callable:
    global _PAYPAL_WSGI
    if service == "pay153":
        return get_pay153_module().app
    if service == "paypal-agreement":
        with _LOAD_LOCK:
            if _PAYPAL_WSGI is None:
                _PAYPAL_WSGI = _build_paypal_wsgi()
            return _PAYPAL_WSGI
    raise ValueError("未知集成服务")


def status() -> dict[str, dict[str, object]]:
    """Return same-process integration health without probing extra ports."""
    result: dict[str, dict[str, object]] = {}
    for name, path in (("pay153", "/api/health"), ("paypal-agreement", "/api/health")):
        try:
            from werkzeug.test import Client
            from werkzeug.wrappers import Response

            response = Client(get_wsgi_app(name), Response, use_cookies=False).get(path)
            payload = response.get_json(silent=True) or {}
            healthy = response.status_code == 200 and payload.get("ok") is True
            error = None
        except Exception as exc:
            healthy = False
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
        result[name] = {
            "enabled": True,
            "url": "/pay153/" if name == "pay153" else "/paypal-pay/",
            "healthy": healthy,
            "managed": True,
            "in_process": True,
            "port": _MAIN_PORT,
            "error": error,
        }
    return result


__all__ = [
    "configure_single_port", "get_pay153_module", "get_wsgi_app", "status",
]
