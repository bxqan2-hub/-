import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from config import email as email_cfg
from webui import app as webui


@pytest.fixture
def summary_app(monkeypatch):
    for name in dir(webui.db):
        if name.startswith("recover_interrupted_"):
            monkeypatch.setattr(webui.db, name, lambda: 0)
    monkeypatch.setattr(webui.at_validity_scheduler, "ensure_started", lambda: None)
    monkeypatch.setattr(email_cfg, "EMAIL_SOURCE", "cloudflare_domain")
    pool = Mock(return_value={"total": 5, "available": 4})
    monkeypatch.setattr(webui.db, "domain_email_pool_summary", pool)
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(webui, "time", SimpleNamespace(monotonic=clock))
    app = webui.create_app(auth_code="test-auth")
    app.config["TESTING"] = True
    return app, pool, clock


def get_summary(app):
    with app.test_client() as client:
        return client.get("/api/summary", headers={"X-Auth-Code": "test-auth"})


def test_concurrent_misses_compute_once(summary_app, monkeypatch):
    app, pool, _ = summary_app
    barrier = threading.Barrier(8)

    def count():
        time.sleep(0.04)
        return 7

    counter = Mock(side_effect=count)
    monkeypatch.setattr(webui.db, "count_accounts", counter)

    def fetch(_):
        barrier.wait(timeout=5)
        return get_summary(app).get_json()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(fetch, range(8)))
    assert all(result["accounts"] == 7 for result in results)
    counter.assert_called_once()
    pool.assert_called_once()


def test_expiry_and_write_invalidate_summary(summary_app, monkeypatch):
    app, _, clock = summary_app
    counter = Mock(side_effect=[1, 2, 3])
    monkeypatch.setattr(webui.db, "count_accounts", counter)
    app.add_url_rule("/test-write", "write", lambda: ("", 204), methods=["POST"])
    assert get_summary(app).get_json()["accounts"] == 1
    clock.return_value = 100.74
    assert get_summary(app).get_json()["accounts"] == 1
    clock.return_value = 100.75
    assert get_summary(app).get_json()["accounts"] == 2
    with app.test_client() as client:
        assert client.post("/test-write", headers={"X-Auth-Code": "test-auth"}).status_code == 204
    assert get_summary(app).get_json()["accounts"] == 3


def test_failed_refresh_is_retried_and_source_reload_is_immediate(summary_app, monkeypatch):
    app, pool, _ = summary_app
    counter = Mock(side_effect=[OSError("read failed"), 3, 4])
    monkeypatch.setattr(webui.db, "count_accounts", counter)
    with pytest.raises(OSError):
        get_summary(app)
    assert get_summary(app).get_json()["outlook_total"] == 5
    monkeypatch.setattr(email_cfg, "EMAIL_SOURCE", "gptmail")
    assert get_summary(app).get_json()["outlook_total"] == 0
    assert pool.call_count == 3
    with app.test_client() as client:
        assert client.get("/api/summary").status_code == 401
