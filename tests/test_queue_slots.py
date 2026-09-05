import importlib
import threading
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize(
    "service_name,enqueue_name,claim_name,kwargs",
    [
        ("plan_check_service", "enqueue_account_plan_check", "claim_account_plan_check", {"email": "queue@example.com", "access_token": "test-token"}),
        ("live_check_service", "enqueue_account_live_check", "claim_account_live_check", {"email": "queue@example.com"}),
        ("account_security_service", "enqueue_account_security_setup", "claim_account_security_setup", {}),
        ("checkout_kind_service", "enqueue", "claim_account_checkout_kind", {"access_token": "test-token"}),
        ("gcash_service", "enqueue", "claim_account_gcash", {"access_token": "test-token"}),
        ("gopay_service", "enqueue", "claim_account_gopay", {"access_token": "test-token", "proxies": ["http://127.0.0.1:9999"]}),
        ("momo_service", "enqueue", "claim_account_momo", {"access_token": "test-token", "proxies": ["http://127.0.0.1:9999"]}),
    ],
)
@pytest.mark.parametrize("claim_raises", [False, True])
def test_failed_claim_keeps_queue_capacity(monkeypatch, service_name, enqueue_name, claim_name, kwargs, claim_raises):
    service = importlib.import_module(f"core.{service_name}")
    slots = threading.BoundedSemaphore(1)
    executor = Mock()
    claim = Mock(side_effect=OSError("claim failed")) if claim_raises else Mock(return_value=False)
    monkeypatch.setattr(service, "_QUEUE_SLOTS", slots)
    monkeypatch.setattr(service.db, claim_name, claim)
    monkeypatch.setattr(service.db, "get_account", lambda *_args: {"id": 1, "email": "queue@example.com"})
    if hasattr(service, "_append_log"):
        monkeypatch.setattr(service, "_append_log", Mock())
    if hasattr(service, "get_executor"):
        monkeypatch.setattr(service, "get_executor", lambda: executor)
    else:
        monkeypatch.setattr(service, "_EXECUTOR", executor)
    enqueue = getattr(service, enqueue_name)

    for _ in range(3):
        if claim_raises:
            with pytest.raises(OSError, match="claim failed"):
                enqueue(account_id=1, trigger="manual", **kwargs)
        else:
            result = enqueue(account_id=1, trigger="manual", **kwargs)
            assert result["accepted"] is False
            assert result["busy"] is True
    executor.submit.assert_not_called()

    claim.side_effect = None
    claim.return_value = True
    assert enqueue(account_id=1, trigger="manual", **kwargs)["accepted"] is True
    executor.submit.assert_called_once()
    assert slots.acquire(blocking=False) is False
    slots.release()
