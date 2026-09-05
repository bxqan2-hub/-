from core import sentinel
from config import browser as browser_cfg
from config import openai_protocol as protocol_cfg


def test_fingerprint_reads_reloaded_config_modules(monkeypatch):
    monkeypatch.setattr(browser_cfg, "SCREEN_WIDTH", 1111)
    monkeypatch.setattr(browser_cfg, "SCREEN_HEIGHT", 2222)
    monkeypatch.setattr(protocol_cfg, "SENTINEL_SV", "reload-test")

    payload = sentinel.generate_fingerprint_data("device", profile={})
    values = payload
    assert values[0] == 3333
    assert "reload-test/sdk.js" in values[5]
