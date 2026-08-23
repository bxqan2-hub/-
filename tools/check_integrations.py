#!/usr/bin/env python3
"""Fail-fast runtime check for the bundled PAY.153 extraction integration."""
from __future__ import annotations

import argparse
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PAY153 = ROOT / "integrations" / "pay153_checkout"


def fail(message: str) -> None:
    raise RuntimeError(message)


def check_files() -> None:
    required = (
        PAY153 / "app.py",
        PAY153 / "gen_token_jsdom.js",
        PAY153 / "sentinel_sdk_full.js",
        PAY153 / "package.json",
        PAY153 / "package-lock.json",
        PAY153 / "paypal_oaics_adapter.py",
        PAY153 / "paypal_oaics_link_pp" / "engine.py",
        PAY153 / "paypal_oaics_link_pp" / "protocol" / "stripe_checkout.py",
    )
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        fail("集成文件缺失：" + ", ".join(missing))
    print("[OK] PAY.153 开源提链项目文件完整")


def check_python() -> None:
    if sys.version_info < (3, 10):
        fail(f"Python 版本过低：{sys.version.split()[0]}，需要 Python 3.10+")
    modules = {
        "flask": "Flask",
        "curl_cffi": "curl_cffi",
        "playwright": "playwright",
    }
    missing: list[str] = []
    for module, package in modules.items():
        try:
            importlib.import_module(module)
        except Exception as exc:
            missing.append(f"{package} ({type(exc).__name__}: {exc})")
    if missing:
        fail("Python 集成依赖不可用：" + "; ".join(missing))
    print(f"[OK] Python {sys.version.split()[0]} 集成依赖")


def run_checked(command: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        fail(f"命令不存在：{command[0]}")
    except subprocess.TimeoutExpired:
        fail(f"命令运行超时：{' '.join(command)}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "无错误详情").strip()
        fail(f"{' '.join(command[:2])} 失败：{detail[:600]}")
    return result.stdout.strip()


def check_node() -> None:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        fail("缺少 Node.js/npm；请安装 Node.js 22.19+ 后重新运行一键安装.bat")
    version = run_checked([node, "-p", "process.versions.node"], cwd=PAY153)
    try:
        parts = tuple(int(part) for part in version.split(".")[:3])
    except ValueError:
        fail(f"无法识别 Node.js 版本：{version}")
    if parts < (22, 19, 0):
        fail(f"Node.js 版本过低：{version}，需要 22.19+")
    probe = (
        "const p=require('./package.json');"
        "const j=require('jsdom/package.json');"
        "const u=require('undici/package.json');"
        "const {JSDOM}=require('jsdom');"
        "const d=new JSDOM('<p>ok</p>');"
        "if(d.window.document.querySelector('p').textContent!=='ok')process.exit(2);"
        "console.log(JSON.stringify({declared:p.dependencies.jsdom,installed:j.version,undiciDeclared:p.dependencies.undici,undiciInstalled:u.version}));"
    )
    raw = run_checked([node, "-e", probe], cwd=PAY153)
    versions = json.loads(raw)
    if versions["installed"] != versions["declared"]:
        fail(
            "jsdom 版本与锁定版本不一致："
            f"声明 {versions['declared']}，已安装 {versions['installed']}"
        )
    if versions["undiciInstalled"] != versions["undiciDeclared"]:
        fail(
            "undici 版本与锁定版本不一致："
            f"声明 {versions['undiciDeclared']}，已安装 {versions['undiciInstalled']}"
        )
    sentinel_raw = run_checked([node, "gen_token_jsdom.js", "--self-check"], cwd=PAY153)
    sentinel = json.loads(sentinel_raw)
    if sentinel.get("ok") is not True:
        fail("Sentinel Node 自检未返回 ok=true")
    print(
        f"[OK] Node.js {version} / jsdom {versions['installed']} / "
        f"undici {versions['undiciInstalled']} Sentinel 运行时"
    )


def check_playwright(launch_browser: bool) -> None:
    # Playwright 1.62 on Python 3.14 can emit an asyncio cleanup warning when
    # its sync API stops. Isolate the probe so startup logs remain clean while
    # preserving the exact return code and stderr on actual failures.
    probe = """
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    executable = Path(playwright.chromium.executable_path)
    if not executable.is_file():
        raise SystemExit('PLAYWRIGHT_CHROMIUM_MISSING:' + str(executable))
    if LAUNCH_BROWSER:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<title>runtime-ok</title>')
            if page.title() != 'runtime-ok':
                raise SystemExit('PLAYWRIGHT_PAGE_PROBE_FAILED')
        finally:
            browser.close()
""".replace("LAUNCH_BROWSER", "True" if launch_browser else "False")
    try:
        run_checked([sys.executable, "-c", probe], cwd=ROOT)
    except RuntimeError as exc:
        message = str(exc)
        if "PLAYWRIGHT_CHROMIUM_MISSING" in message:
            fail("Playwright Chromium 未安装；请重新运行一键安装.bat")
        fail(message)
    suffix = "并已实际启动" if launch_browser else "文件存在"
    print(f"[OK] Playwright Chromium {suffix}")


def check_apps() -> None:
    sys.path.insert(0, str(ROOT))
    from core.integrated_runtime import status

    health = status()
    failed = {
        name: item.get("error") or "health endpoint returned unhealthy"
        for name, item in health.items()
        if not item.get("healthy")
    }
    if failed:
        fail("单端口集成加载失败：" + json.dumps(failed, ensure_ascii=False))
    if set(health) != {"pay153"}:
        fail("集成健康状态包含非 PAY.153 服务：" + ", ".join(sorted(health)))
    print("[OK] PAY.153 Checkout 可在主进程加载")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--launch-browser",
        action="store_true",
        help="实际启动一次 Chromium（安装完成验证使用）",
    )
    args = parser.parse_args()
    try:
        check_files()
        check_python()
        check_node()
        check_playwright(args.launch_browser)
        check_apps()
    except Exception as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        print("请运行项目根目录的 一键安装.bat 修复运行环境。", file=sys.stderr)
        return 1
    print("[OK] PAY.153 开源提链项目运行环境全部通过自检")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
