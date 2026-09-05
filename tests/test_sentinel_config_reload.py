from core import sentinel
from config import browser as browser_cfg
from config import openai_protocol as protocol_cfg
from types import SimpleNamespace
from unittest.mock import Mock
import logging
import pytest
from core import sentinel_runner


def test_fingerprint_reads_reloaded_config_modules(monkeypatch):
    monkeypatch.setattr(browser_cfg, "SCREEN_WIDTH", 1111)
    monkeypatch.setattr(browser_cfg, "SCREEN_HEIGHT", 2222)
    monkeypatch.setattr(protocol_cfg, "SENTINEL_SV", "reload-test")

    payload = sentinel.generate_fingerprint_data("device", profile={})
    values = payload
    assert values[0] == 3333
    assert "reload-test/sdk.js" in values[5]


def test_runner_uses_new_defaults_but_preserves_explicit_profile(monkeypatch, caplog):
    monkeypatch.setattr(browser_cfg, "SCREEN_WIDTH", 1111)
    monkeypatch.setattr(protocol_cfg, "SENTINEL_SV", "reload-test")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='{"p":"test","c":"test","id":"test","flow":"test"}', stderr=""))
    monkeypatch.setattr(sentinel_runner.subprocess, "run", run)
    with caplog.at_level(logging.DEBUG, logger=sentinel_runner.__name__):
        sentinel_runner.generate_sentinel_token({}, "test", "device", cookie="private-cookie-marker")
    args = run.call_args.args[0]
    assert args[args.index("--width") + 1] == "1111"
    assert "reload-test/sdk.js" in args[args.index("--script-src") + 1]
    assert "private-cookie-marker" not in caplog.text
    sentinel_runner.generate_sentinel_token({}, "test", "device", browser_profile={"screen_width": 900})
    args = run.call_args.args[0]
    assert args[args.index("--width") + 1] == "900"


@pytest.mark.parametrize("returncode,stdout", [(1, "private-token-marker"), (0, "invalid-private-token-marker"), (0, '[]'), (0, '{"p":"private-token-marker"}')])
def test_runner_errors_do_not_expose_output(monkeypatch, returncode, stdout):
    run = Mock(return_value=SimpleNamespace(returncode=returncode, stdout=stdout, stderr="private-token-marker"))
    monkeypatch.setattr(sentinel_runner.subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        sentinel_runner.generate_sentinel_token({}, "test", "device")
    assert "private-token-marker" not in str(error.value)
