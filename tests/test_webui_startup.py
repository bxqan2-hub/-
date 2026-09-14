import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not (ROOT / ".venv" / "Scripts" / "python.exe").is_file(),
    reason="Windows launcher requires the project virtual environment",
)


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_launcher_rejects_invalid_port(port):
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "start-webui.bat", port],
        cwd=ROOT, input="", capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1
    assert "port is not an integer from 1 to 65535" in result.stdout
    assert "Starting WebUI" not in result.stdout


def test_launchers_replace_then_wait_for_http_and_use_one_restart_path():
    start = (ROOT / "start-webui.bat").read_text(encoding="ascii")
    stop = (ROOT / "stop-webui.bat").read_text(encoding="ascii")
    restart = (ROOT / "restart-webui.bat").read_text(encoding="ascii")
    assert 'set "PORT=5002"' in start
    assert 'set "PORT=5001"' not in start
    assert "Startup cancelled; port unchanged." in start
    assert start.count('call "%~dp0stop-webui.bat"') == 1
    assert start.index("tools\\check_integrations.py") < start.index('call "%~dp0stop-webui.bat"')
    assert start.index('call "%~dp0stop-webui.bat"') < start.index("Invoke-CimMethod")
    assert start.index("r.status==200") < start.index('start "" "http://')
    assert "Start-Process" not in start
    assert "ShowWindow=[uint16]0" in start
    assert "EnvironmentVariables=$environment" in start
    assert "[Environment]::GetEnvironmentVariables()" in start
    assert "$child.ReturnValue -ne 0 -or -not $child.ProcessId" in start
    assert " -X utf8 " in start
    assert "Stop-Process -Id $owner -Force" in stop
    assert "Orphaned listener remains" in stop
    assert "foreach($pid " not in stop.lower()
    assert "-le 4" in stop
    assert 'call "%~dp0start-webui.bat" %*' in restart
    assert "stop-webui.bat" not in restart
    for name in ("start-webui.bat", "stop-webui.bat", "restart-webui.bat"):
        raw = (ROOT / name).read_bytes()
        assert b"\n" not in raw.replace(b"\r\n", b"")
        assert "pause" not in raw.decode("ascii").lower()


@pytest.mark.parametrize("port", [None, "54321"])
def test_start_keeps_requested_port_and_aborts_when_stop_fails(tmp_path, port):
    fixture = tmp_path / "launcher fixture"
    fixture.mkdir()
    (fixture / "start-webui.bat").write_bytes((ROOT / "start-webui.bat").read_bytes())
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(fixture / ".venv")],
        check=True, capture_output=True, text=True, timeout=30,
    )
    (fixture / "tools").mkdir()
    (fixture / "tools" / "check_integrations.py").write_text("", encoding="ascii")
    (fixture / "stop-webui.bat").write_bytes(
        b'@echo off\r\necho %~1>>"%~dp0stop-ports.txt"\r\nexit /b 1\r\n'
    )
    # A regression must not launch PowerShell or touch any real listener.
    (fixture / "powershell.cmd").write_bytes(
        b'@echo off\r\necho invoked>"%~dp0unexpected-start.txt"\r\nexit /b 1\r\n'
    )
    args = ["cmd.exe", "/d", "/c", "start-webui.bat"]
    if port is not None:
        args.append(port)
    result = subprocess.run(
        args, cwd=fixture, input="", capture_output=True, text=True, timeout=15,
    )
    expected_port = port or "5002"
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"Failed to stop WebUI on port {expected_port}" in result.stdout
    assert "Startup cancelled; port unchanged." in result.stdout
    assert "Starting WebUI" not in result.stdout
    assert (fixture / "stop-ports.txt").read_text(encoding="ascii").splitlines() == [expected_port]
    assert not (fixture / "unexpected-start.txt").exists()


def test_detached_start_survives_launcher_job_exit_and_keeps_environment(tmp_path):
    fixture = tmp_path / "detached launcher fixture"
    fixture.mkdir()
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(fixture / ".venv")],
        check=True, capture_output=True, text=True, timeout=30,
    )
    for name in ("logs", "run"):
        (fixture / name).mkdir()
    # This probe never binds a port; it only records the detached process state.
    (fixture / "web.py").write_text("""import ctypes, json, os, sys, time
from ctypes import wintypes as W
from pathlib import Path
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k.OpenProcess.restype = W.HANDLE
k.IsProcessInJob.argtypes = [W.HANDLE, W.HANDLE, ctypes.POINTER(W.BOOL)]
k.CloseHandle.argtypes = [W.HANDLE]
k.GetConsoleWindow.restype = W.HWND
u = ctypes.WinDLL('user32', use_last_error=True)
u.IsWindowVisible.argtypes = [W.HWND]
parent = k.OpenProcess(0x1000, False, os.getppid())
in_job = W.BOOL()
assert parent and k.IsProcessInJob(parent, None, ctypes.byref(in_job))
k.CloseHandle(parent)
Path('state.json').write_text(json.dumps({
    'parent_in_job': bool(in_job.value),
    'console_visible': bool(u.IsWindowVisible(k.GetConsoleWindow())),
    'env': os.environ.get('WEBUI_LAUNCHER_TEST_MARKER'),
    'utf8_mode': sys.flags.utf8_mode, 'args': sys.argv[1:]}))
print('fixture stdout', flush=True)
print('fixture stderr', file=sys.stderr, flush=True)
for _ in range(200):
    if Path('release').exists():
        Path('survived.json').write_text('true')
        break
    time.sleep(0.1)
""", encoding="ascii")
    start = (ROOT / "start-webui.bat").read_text(encoding="ascii")
    launch = next(line for line in start.splitlines() if "Invoke-CimMethod" in line)
    (fixture / "launch.bat").write_bytes(("@echo off\r\n" + launch + "\r\n").encode("ascii"))
    # Kill-on-close is applied only to this helper and its own descendants.
    # A WMI child survives helper exit; an inherited Start-Process child does not.
    (fixture / "launcher_host.py").write_text("""import ctypes, subprocess
from ctypes import wintypes as W
class Basic(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
        ('flags', W.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
        ('active_limit', W.DWORD), ('affinity', ctypes.c_size_t),
        ('priority', W.DWORD), ('scheduling', W.DWORD)]
class Extended(ctypes.Structure):
    _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
        ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateJobObjectW.argtypes = [ctypes.c_void_p, W.LPCWSTR]
k.CreateJobObjectW.restype = W.HANDLE
k.SetInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
k.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
k.GetCurrentProcess.restype = W.HANDLE
job = k.CreateJobObjectW(None, None)
assert job, ctypes.get_last_error()
limits = Extended()
limits.basic.flags = 0x2000
assert k.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)), ctypes.get_last_error()
assert k.AssignProcessToJobObject(job, k.GetCurrentProcess()), ctypes.get_last_error()
result = subprocess.run(['cmd.exe', '/d', '/c', 'launch.bat'], timeout=15)
raise SystemExit(result.returncode)
""", encoding="ascii")
    env = dict(os.environ, PORT="54321", WEBUI_LAUNCHER_TEST_MARKER="fixture inherited")
    try:
        result = subprocess.run(
            [sys.executable, str(fixture / "launcher_host.py")], cwd=fixture,
            env=env, capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        deadline = time.monotonic() + 10
        while not (fixture / "state.json").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        state = json.loads((fixture / "state.json").read_text(encoding="ascii"))
        assert state == {
            "parent_in_job": False, "console_visible": False, "env": "fixture inherited",
            "utf8_mode": 1, "args": ["--host", "127.0.0.1", "--port", "54321"],
        }
        # Release only after the owning launcher host has fully exited.
        (fixture / "release").touch()
        while not (fixture / "survived.json").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert (fixture / "survived.json").read_text(encoding="ascii") == "true"
        assert "fixture stdout" in (fixture / "logs" / "webui-54321.log").read_text()
        assert "fixture stderr" in (fixture / "logs" / "webui-54321.err.log").read_text()
    finally:
        (fixture / "release").touch()


def test_stop_force_replaces_only_requested_listener_and_project(tmp_path):
    # Real subprocess listeners keep pytest outside the forced-stop target.
    # Only the stop script is copied into a disposable path-with-spaces fixture.
    fixture = tmp_path / "launcher fixture"
    fixture.mkdir()
    (fixture / "stop-webui.bat").write_bytes((ROOT / "stop-webui.bat").read_bytes())
    server = """import socket, time
s = socket.socket()
for port in range(8100, 8200):
    try:
        s.bind(('127.0.0.1', port))
        break
    except OSError:
        continue
else:
    raise SystemExit('no fixture port')
s.listen(1)
print(s.getsockname()[1], flush=True)
time.sleep(120)
"""
    # This project instance is on a different port and must also be replaced.
    (fixture / "web.py").write_text(server, encoding="utf-8")
    children = []
    try:
        for args in (["-c", server], ["-c", server], [str(fixture / "web.py")]):
            child = subprocess.Popen(
                [sys.executable, *args], cwd=fixture, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            children.append(child)
        ports = [int(child.stdout.readline().strip()) for child in children]
        (fixture / "run").mkdir()
        # A stale PID pointing at an unrelated server must never determine the target.
        (fixture / "run" / "webui.pid").write_text(str(children[1].pid), encoding="ascii")
        invalid = subprocess.run(
            ["cmd.exe", "/d", "/c", "stop-webui.bat", "invalid"],
            cwd=fixture, input="", capture_output=True, text=True, timeout=15,
        )
        assert invalid.returncode != 0
        assert all(child.poll() is None for child in children)
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "stop-webui.bat", f"0{ports[0]}"],
            cwd=fixture, input="", capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Stopped PID=" in result.stdout
        children[0].wait(timeout=5)
        children[2].wait(timeout=5)
        assert children[1].poll() is None
        assert not (fixture / "run" / "webui.pid").exists()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
