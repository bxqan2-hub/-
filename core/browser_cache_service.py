# -*- coding: utf-8 -*-
"""Roxy/browser cache inspection and safe cleanup for the local WebUI.

Only disposable web-cache directories are removed.  Profile cookies,
localStorage, fingerprint data, proxy state and bundled browser runtime files
stay in place.  The shared public JS/CSS cache is reported separately and is
kept by the browser-cache button so the next registration does not incur a
full cold download.
"""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from config import roxybrowser as _roxy_cfg
from core import db

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROFILE_CACHE_DIR_NAMES = frozenset({
    "Cache",
    "CacheStorage",
    "Code Cache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GPUCache",
    "GrShaderCache",
    "Media Cache",
    "ShaderCache",
})
_ACTIVE_JOB_STATES = frozenset({"pending", "running", "stopping"})
_PROFILE_DIR_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
_CLEAR_LOCK = threading.Lock()


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _roxy_root() -> Path:
    appdata = str(os.environ.get("APPDATA") or "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return (base / "RoxyBrowser").resolve(strict=False)


def _project_static_cache_root() -> Path:
    raw = str(getattr(_roxy_cfg, "ROXY_CACHE_DIR", "data/browser_static_cache") or "data/browser_static_cache").strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path.resolve(strict=False)


def _iter_files(root: Path):
    if not root.is_dir() or _is_link(root):
        return
    def _ignore_walk_error(_error):
        return None
    for current, dirs, files in os.walk(root, topdown=True, onerror=_ignore_walk_error, followlinks=False):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if not _is_link(current_path / name)]
        for name in files:
            path = current_path / name
            if _is_link(path):
                continue
            yield path


def _measure(root: Path) -> dict[str, int]:
    total = 0
    files = 0
    for path in _iter_files(root) or ():
        try:
            total += max(0, int(path.stat().st_size))
            files += 1
        except OSError:
            continue
    return {"bytes": total, "files": files}


def _profile_cache_dirs(root: Path, profile_ids: set[str] | None = None) -> list[Path]:
    if not root.is_dir() or _is_link(root):
        return []
    found: list[Path] = []
    profiles = [
        child for child in root.iterdir()
        if child.is_dir() and not _is_link(child)
    ]
    for profile in profiles:
        if profile_ids is not None and profile.name.lower() not in profile_ids:
            continue
        for current, dirs, _files in os.walk(profile, topdown=True, followlinks=False):
            current_path = Path(current)
            dirs[:] = [name for name in dirs if not _is_link(current_path / name)]
            if current_path.name in _PROFILE_CACHE_DIR_NAMES:
                found.append(current_path)
                # A matched cache root is cleared as one unit; do not add
                # nested cache directories a second time.
                dirs[:] = []
    return found


def _profile_inventory() -> tuple[set[str], bool, str]:
    """Read Roxy's official Profile list before deleting a whole directory."""
    try:
        from core.roxybrowser_client import RoxyBrowserClient
        from config import roxybrowser as _cfg

        client = RoxyBrowserClient()
        page = 1
        page_size = 100
        profile_ids: set[str] = set()
        total = None
        while page <= 10:
            response = client.http.get(
                client.api_base.rstrip("/") + "/browser/list_v3",
                params={
                    "workspaceId": getattr(_cfg, "ROXY_WORKSPACE_ID", ""),
                    "page_index": page,
                    "page_size": page_size,
                },
                timeout=3,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or int(payload.get("code") or 0) != 0:
                return set(), False, "profile_list_response"
            data = payload.get("data")
            if not isinstance(data, dict):
                return set(), False, "profile_list_data"
            rows = data.get("rows")
            if not isinstance(rows, list):
                return set(), False, "profile_list_rows"
            if total is None:
                try:
                    total = max(0, int(data.get("total") or 0))
                except (TypeError, ValueError):
                    total = 0
            for row in rows:
                if isinstance(row, dict):
                    profile_id = str(row.get("dirId") or row.get("dir_id") or row.get("id") or "").strip().lower()
                    if profile_id:
                        profile_ids.add(profile_id)
            if not rows or len(profile_ids) >= total or len(rows) < page_size:
                break
            page += 1
        if total is not None and total > len(profile_ids) and page >= 10:
            return set(), False, "profile_list_too_large"
        return profile_ids, True, ""
    except Exception as exc:
        return set(), False, type(exc).__name__


def _orphan_profile_dirs(root: Path, profile_ids: set[str], inventory_ok: bool) -> list[Path]:
    if not inventory_ok or not root.is_dir() or _is_link(root):
        return []
    return [
        child for child in root.iterdir()
        if child.is_dir()
        and not _is_link(child)
        and _PROFILE_DIR_PATTERN.fullmatch(child.name)
        and child.name.lower() not in profile_ids
    ]


def _dedupe_roots(roots: list[Path]) -> list[Path]:
    result: list[Path] = []
    for raw in roots:
        path = raw.resolve(strict=False)
        if any(path == existing or _path_within(path, existing) for existing in result):
            continue
        result = [existing for existing in result if not _path_within(existing, path)]
        result.append(path)
    return result


def _scope(label: str, roots: list[Path], *, clearable: bool, reason: str = "") -> dict:
    roots = _dedupe_roots(roots)
    measured = {"bytes": 0, "files": 0}
    for root in roots:
        current = _measure(root)
        measured["bytes"] += current["bytes"]
        measured["files"] += current["files"]
    return {
        "label": label,
        "bytes": measured["bytes"],
        "files": measured["files"],
        "directories": len(roots),
        "clearable": bool(clearable),
        "reason": reason,
        "_roots": roots,
    }


def _process_counts() -> tuple[dict[str, int], str]:
    counts = {"RoxyBrowser": 0, "RoxyChrome": 0, "chromedriver": 0}
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            if completed.returncode != 0:
                return counts, f"tasklist_exit_{completed.returncode}"
            rows = csv.reader(io.StringIO(completed.stdout or ""))
            names = []
            for row in rows:
                if row:
                    names.append(str(row[0] or "").strip().lower())
        else:
            completed = subprocess.run(
                ["ps", "-A", "-o", "comm="],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                check=False,
            )
            if completed.returncode != 0:
                return counts, f"ps_exit_{completed.returncode}"
            names = [line.strip().lower() for line in (completed.stdout or "").splitlines()]
    except (OSError, subprocess.SubprocessError) as exc:
        return counts, type(exc).__name__

    for name in names:
        if name == "roxybrowser.exe" or name == "roxybrowser":
            counts["RoxyBrowser"] += 1
        elif name == "roxychrome.exe" or name == "roxychrome":
            counts["RoxyChrome"] += 1
        elif name == "chromedriver.exe" or name == "chromedriver":
            counts["chromedriver"] += 1
    return counts, ""


def _active_job_count() -> int:
    rows = db.list_jobs(limit=1_000_000)
    return sum(1 for row in rows if str(row.get("status") or "").lower() in _ACTIVE_JOB_STATES)


def _build_snapshot() -> dict:
    roxy_root = _roxy_root()
    profile_root = roxy_root / "browser-cache"
    profile_ids, inventory_ok, inventory_error = _profile_inventory()
    profile_cache_roots = _profile_cache_dirs(profile_root, profile_ids if inventory_ok else set())
    orphan_profile_roots = _orphan_profile_dirs(profile_root, profile_ids, inventory_ok)
    app_cache_root = roxy_root / "Cache"
    static_root = _project_static_cache_root()

    profile_store = _measure(profile_root)
    scopes = {
        "profile_web_cache": _scope("Roxy Profile 网页缓存", profile_cache_roots, clearable=True),
        "orphan_profile_store": _scope("Roxy 孤儿 Profile 目录", orphan_profile_roots, clearable=inventory_ok),
        "roxy_app_cache": _scope("Roxy 管理器缓存", [app_cache_root], clearable=True),
        "shared_static_cache": _scope(
            "注册公开 JS/CSS 缓存",
            [static_root],
            clearable=True,
            reason="保留以避免下一批注册重新冷启动下载",
        ),
    }
    processes, process_error = _process_counts()
    try:
        active_jobs = _active_job_count()
        job_error = ""
    except Exception as exc:
        active_jobs = -1
        job_error = type(exc).__name__
    active_browser_processes = processes["RoxyChrome"] + processes["chromedriver"]
    blockers: list[str] = []
    if active_jobs != 0:
        blockers.append("有注册任务正在排队或运行") if active_jobs > 0 else blockers.append("注册任务状态读取失败")
    if active_browser_processes:
        blockers.append("有 Roxy 浏览器自动化进程正在运行")
    if process_error:
        blockers.append("浏览器进程状态读取失败")
    if job_error:
        blockers.append("注册任务状态读取失败")
    if not inventory_ok:
        blockers.append("Roxy Profile 列表核对失败")
    reclaimable = sum(
        int(item["bytes"])
        for item in scopes.values()
        if item["clearable"] and item["label"] != "注册公开 JS/CSS 缓存"
    )
    reclaimable_files = sum(
        int(item["files"])
        for item in scopes.values()
        if item["clearable"] and item["label"] != "注册公开 JS/CSS 缓存"
    )
    shared_reclaimable = scopes["shared_static_cache"]
    public_scopes = {
        key: {k: v for k, v in value.items() if not k.startswith("_")}
        for key, value in scopes.items()
    }
    return {
        "ok": True,
        "safe_to_clear": not blockers,
        "blockers": blockers,
        "active_jobs": max(0, active_jobs),
        "active_browser_processes": active_browser_processes,
        "processes": processes,
        "profile_inventory": {
            "verified": inventory_ok,
            "known_profiles": len(profile_ids),
            "orphan_profiles": len(orphan_profile_roots),
            "error": inventory_error,
        },
        "profile_store": profile_store,
        "scopes": public_scopes,
        "reclaimable_bytes": reclaimable,
        "reclaimable_files": reclaimable_files,
        "browser_reclaimable_bytes": reclaimable,
        "browser_reclaimable_files": reclaimable_files,
        "shared_reclaimable_bytes": int(shared_reclaimable["bytes"]),
        "shared_reclaimable_files": int(shared_reclaimable["files"]),
        "total_cache_bytes": profile_store["bytes"] + int(scopes["roxy_app_cache"]["bytes"]) + int(scopes["shared_static_cache"]["bytes"]),
        "total_cache_files": profile_store["files"] + int(scopes["roxy_app_cache"]["files"]) + int(scopes["shared_static_cache"]["files"]),
    }


def cache_status() -> dict:
    return _build_snapshot()


def _clear_directory_contents(root: Path) -> tuple[int, int, list[str]]:
    deleted_bytes = 0
    deleted_files = 0
    errors: list[str] = []
    root = root.resolve(strict=False)
    if not root.exists():
        return 0, 0, errors
    if not root.is_dir() or _is_link(root):
        return 0, 0, ["cache_root_invalid"]
    for child in list(root.iterdir()):
        if _is_link(child):
            errors.append("symlink_skipped")
            continue
        try:
            measured = _measure(child) if child.is_dir() else {
                "bytes": max(0, int(child.stat().st_size)) if child.is_file() else 0,
                "files": 1 if child.is_file() else 0,
            }
            if child.is_dir():
                if not _path_within(child, root):
                    errors.append("path_boundary")
                    continue
                shutil.rmtree(child)
            else:
                if not _path_within(child, root):
                    errors.append("path_boundary")
                    continue
                child.unlink(missing_ok=True)
            deleted_bytes += measured["bytes"]
            deleted_files += measured["files"]
        except OSError as exc:
            errors.append(type(exc).__name__)
    return deleted_bytes, deleted_files, errors


def _remove_directory_tree(root: Path, parent: Path) -> tuple[int, int, list[str]]:
    root = root.resolve(strict=False)
    parent = parent.resolve(strict=False)
    if not _path_within(root, parent) or _is_link(root):
        return 0, 0, ["path_boundary"]
    measured = _measure(root)
    try:
        shutil.rmtree(root)
    except OSError as exc:
        return 0, 0, [type(exc).__name__]
    return measured["bytes"], measured["files"], []


def _activity_status() -> dict:
    processes, process_error = _process_counts()
    try:
        active_jobs = _active_job_count()
        job_error = ""
    except Exception as exc:
        active_jobs = -1
        job_error = type(exc).__name__
    active_browser_processes = processes["RoxyChrome"] + processes["chromedriver"]
    blockers: list[str] = []
    if active_jobs != 0:
        blockers.append("有注册任务正在排队或运行") if active_jobs > 0 else blockers.append("注册任务状态读取失败")
    if active_browser_processes:
        blockers.append("有 Roxy 浏览器自动化进程正在运行")
    if process_error:
        blockers.append("浏览器进程状态读取失败")
    if job_error:
        blockers.append("注册任务状态读取失败")
    return {
        "active_jobs": max(0, active_jobs),
        "active_browser_processes": active_browser_processes,
        "processes": processes,
        "blockers": blockers,
    }


def clear_cache() -> dict:
    if not _CLEAR_LOCK.acquire(blocking=False):
        return {"ok": False, "http_status": 409, "error": "缓存清理正在进行，请稍候"}
    try:
        before = _build_snapshot()
        if not before["safe_to_clear"]:
            return {
                "ok": False,
                "http_status": 409,
                "error": "当前有注册任务或浏览器自动化进程，清理已跳过",
                "status": before,
            }

        roxy_root = _roxy_root()
        profile_root = roxy_root / "browser-cache"
        profile_ids, inventory_ok, _inventory_error = _profile_inventory()
        if not inventory_ok:
            return {
                "ok": False,
                "http_status": 409,
                "error": "Roxy Profile 列表核对失败，已跳过 Profile 目录清理",
                "status": before,
            }
        orphan_targets = _orphan_profile_dirs(profile_root, profile_ids, inventory_ok)
        profile_cache_targets = _profile_cache_dirs(profile_root, profile_ids)
        deleted_bytes = 0
        deleted_files = 0
        errors: list[str] = []
        for root in _dedupe_roots(orphan_targets):
            size, files, root_errors = _remove_directory_tree(root, profile_root)
            deleted_bytes += size
            deleted_files += files
            errors.extend(root_errors)
        for root in _dedupe_roots(profile_cache_targets + [roxy_root / "Cache"]):
            if not _path_within(root, roxy_root):
                errors.append("path_boundary")
                continue
            size, files, root_errors = _clear_directory_contents(root)
            deleted_bytes += size
            deleted_files += files
            errors.extend(root_errors)
        after = _build_snapshot()
        return {
            "ok": True,
            "partial": bool(errors),
            "deleted_bytes": deleted_bytes,
            "deleted_files": deleted_files,
            "errors": errors[:20],
            "message": "浏览器缓存已清理" if not errors else "浏览器缓存已清理，部分占用文件已跳过",
            "before": before,
            "status": after,
        }
    finally:
        _CLEAR_LOCK.release()


def clear_shared_static_cache() -> dict:
    """Clear only the shared public JS/CSS cache after a separate confirmation."""
    if not _CLEAR_LOCK.acquire(blocking=False):
        return {"ok": False, "http_status": 409, "error": "缓存清理正在进行，请稍候"}
    try:
        activity = _activity_status()
        if activity["blockers"]:
            return {
                "ok": False,
                "http_status": 409,
                "error": "当前有注册任务或浏览器自动化进程，清理已跳过",
                "status": {"ok": True, **activity},
            }
        root = _project_static_cache_root()
        if not _path_within(root, _PROJECT_ROOT):
            return {
                "ok": False,
                "http_status": 409,
                "error": "共享静态缓存路径不在项目目录内，清理已跳过",
            }
        before = _measure(root)
        deleted_bytes, deleted_files, errors = _clear_directory_contents(root)
        after = _measure(root)
        return {
            "ok": True,
            "partial": bool(errors),
            "deleted_bytes": deleted_bytes,
            "deleted_files": deleted_files,
            "errors": errors[:20],
            "message": "共享公开 JS/CSS 缓存已清理" if not errors else "共享公开 JS/CSS 缓存已清理，部分占用文件已跳过",
            "before": before,
            "after": after,
            "status": {"ok": True, **activity},
        }
    finally:
        _CLEAR_LOCK.release()
