import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def test_launcher_normalizes_port_and_preserves_listener():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        for port in range(8100, 8200):
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                continue
            break
        else:
            pytest.skip("No free four-digit port for the launcher test")
        listener.listen(1)
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "start-webui.bat", f"0{port}"],
            cwd=ROOT, input="", capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 1
        assert f"Port {port} is already in use" in result.stdout
        assert "Starting WebUI" not in result.stdout
        listener.settimeout(2)
        probe, _ = listener.accept()
        probe.close()
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            connection, _ = listener.accept()
            connection.close()


@pytest.mark.parametrize("status", [200, 302])
def test_launcher_does_not_identify_an_unrelated_http_server_as_webui(status):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/another-service")
            self.end_headers()
            self.wfile.write(b"not the project WebUI")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "start-webui.bat", str(port)],
            cwd=ROOT, input="", capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 1
        assert "identity check failed or timed out" in result.stdout
        assert "WebUI is already running" not in result.stdout
        assert len(requests) == 1
        assert requests[0][0] == "/login"
        assert not any("auth" in key.lower() or key.lower() == "cookie" for key in requests[0][1])
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
