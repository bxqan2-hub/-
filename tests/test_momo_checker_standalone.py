from __future__ import annotations

from unittest.mock import patch

from momo_qualification_checker import app as checker
from momo_qualification_checker.momo_detector import mask_proxy, mask_token


def test_masking_never_returns_full_credentials():
    token = "eyJ" + "A" * 80 + "tail"
    proxy = "http://user:password@example.test:8080"
    assert mask_token(token) != token
    assert "password" not in mask_proxy(proxy)
    assert "example.test:8080" in mask_proxy(proxy)


def test_create_job_requires_tokens_and_proxy_pool():
    client = checker.app.test_client()
    assert client.post("/api/jobs", json={"tokens": "", "proxies": "http://p:1"}).status_code == 400
    assert client.post("/api/jobs", json={"tokens": "token", "proxies": ""}).status_code == 400


def test_create_job_parses_labels_and_completes_with_mocked_detector():
    client = checker.app.test_client()
    with patch.object(checker, "check_momo", return_value={"ok": True, "momo": True, "detection_outcome": "qualified"}):
        response = client.post("/api/jobs", json={
            "tokens": "first----token-a\ntoken-b",
            "proxies": "VN|http://proxy-a:8080\nhttp://proxy-b:8080",
            "workers": 2,
            "max_retries": 2,
        })
        assert response.status_code == 200
        job_id = response.get_json()["job_id"]
        import time
        for _ in range(50):
            payload = client.get(f"/api/jobs/{job_id}").get_json()
            if payload["status"] == "done":
                break
            time.sleep(0.02)
        assert payload["completed"] == 2
        assert all(item["momo"] is True for item in payload["results"])


def test_health_endpoint():
    payload = checker.app.test_client().get("/healthz").get_json()
    assert payload == {"ok": True, "service": "momo-qualification-checker"}
