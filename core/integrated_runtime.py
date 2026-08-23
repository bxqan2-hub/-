# -*- coding: utf-8 -*-
"""Load the vendored PAY.153 extraction service inside the main WebUI process."""
from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Callable


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
INTEGRATIONS_ROOT = ROOT / "integrations"
PAY153_ROOT = INTEGRATIONS_ROOT / "pay153_checkout"
sys.dont_write_bytecode = True

_LOAD_LOCK = threading.RLock()
_PAY153_MODULE: ModuleType | None = None
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


def configure_single_port(port: int) -> None:
    """Record the main WebUI port reported by the integrated health endpoint."""
    global _MAIN_PORT
    _MAIN_PORT = int(port)


def get_wsgi_app(service: str) -> Callable:
    if service == "pay153":
        return get_pay153_module().app
    raise ValueError("未知集成服务")


def status() -> dict[str, dict[str, object]]:
    """Return same-process PAY.153 health without probing an extra port."""
    try:
        from werkzeug.test import Client
        from werkzeug.wrappers import Response

        response = Client(get_wsgi_app("pay153"), Response, use_cookies=False).get("/api/health")
        payload = response.get_json(silent=True) or {}
        healthy = response.status_code == 200 and payload.get("ok") is True
        error = None
    except Exception as exc:
        healthy = False
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    return {
        "pay153": {
            "enabled": True,
            "url": "/pay153/",
            "healthy": healthy,
            "managed": True,
            "in_process": True,
            "port": _MAIN_PORT,
            "error": error,
        }
    }


__all__ = [
    "configure_single_port", "get_pay153_module", "get_wsgi_app", "status",
]
