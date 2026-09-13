import os
import subprocess
import sys
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
    assert 'if not defined PORT set "PORT=5002"' in start
    assert start.index("tools\\check_integrations.py") < start.index('call "%~dp0stop-webui.bat"')
    assert start.index('call "%~dp0stop-webui.bat"') < start.index("Start-Process")
    assert start.index("r.status==200") < start.index('start "" "http://')
    assert "-WindowStyle Hidden" in start
    assert "Stop-Process -Id $owner -Force" in stop
    assert "foreach($pid " not in stop.lower()
    assert "-le 4" in stop
    assert 'call "%~dp0start-webui.bat" %*' in restart
    assert "stop-webui.bat" not in restart
    for name in ("start-webui.bat", "stop-webui.bat", "restart-webui.bat"):
        raw = (ROOT / name).read_bytes()
        assert b"\n" not in raw.replace(b"\r\n", b"")
        assert "pause" not in raw.decode("ascii").lower()


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
