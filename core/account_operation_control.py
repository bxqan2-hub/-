# -*- coding: utf-8 -*-
"""账号页后台操作的协作式全局停止控制。"""
from __future__ import annotations

import threading
import time


class AccountOperationStopped(RuntimeError):
    """账号页全局停止后由后台 worker 抛出的轻量控制异常。"""


_LOCK = threading.Lock()
_GENERATION = 0


def snapshot() -> int:
    """取得当前操作代次；新入队任务绑定到这个代次。"""
    with _LOCK:
        return _GENERATION


def request_stop_all() -> int:
    """推进代次，使所有旧的账号页操作在下一个检查点停止。"""
    global _GENERATION
    with _LOCK:
        _GENERATION += 1
        return _GENERATION


def is_cancelled(generation: int) -> bool:
    with _LOCK:
        return int(generation) != _GENERATION


def raise_if_cancelled(generation: int) -> None:
    if is_cancelled(generation):
        raise AccountOperationStopped("账号页操作已停止")


def wait(seconds: float, generation: int, *, quantum: float = 0.2) -> bool:
    """可被全局停止打断的短等待；返回是否完整等待结束。"""
    deadline = time.monotonic() + max(0.0, float(seconds or 0.0))
    while True:
        if is_cancelled(generation):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(max(0.01, quantum), remaining))


__all__ = [
    "AccountOperationStopped",
    "snapshot",
    "request_stop_all",
    "is_cancelled",
    "raise_if_cancelled",
    "wait",
]
