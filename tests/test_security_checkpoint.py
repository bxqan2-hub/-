from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from core import account_export, db
from core import registration_password
from core.registration_password import persist_confirmed_registration_password


def _save_checkpoint_in_child(root: str, email: str, start_event) -> None:
    """spawn 子进程入口：验证 WebUI/CLI 并发写不会 lost update。"""
    from core import db as child_db

    folder = Path(root)
    child_db._DATA_DIR = folder
    child_db._LOG_DIR = folder / "logs"
    child_db._SECURITY_CHECKPOINTS_JSON = folder / "security-checkpoints.json"
    child_db._SECURITY_CHECKPOINTS_LOCK = folder / "security-checkpoints.lock"
    if not start_event.wait(10):
        raise RuntimeError("checkpoint concurrency start timeout")
    for index in range(8):
        child_db.save_security_checkpoint(
            email,
            registration_password=f"Stable-pass-{index}!",
        )


def test_security_checkpoint_runtime_files_are_gitignored() -> None:
    ignore_text = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
    patterns = {line.strip() for line in ignore_text.splitlines()}
    assert "注册安全凭据待完成.json" in patterns
    assert "注册安全凭据待完成.json.*.tmp" in patterns
    assert "注册安全凭据待完成.lock" in patterns


def test_default_test_database_paths_never_target_the_runtime_tree() -> None:
    runtime_root = Path(db.__file__).resolve().parents[1]
    paths = [value for name, value in vars(db).items() if name.startswith("_") and isinstance(value, Path)]
    assert len(paths) >= 24
    assert all(value.is_relative_to(db._PROJECT_ROOT) for value in paths)
    assert all(not value.is_relative_to(runtime_root) for value in paths)


def _isolate_storage(monkeypatch, tmp_path) -> None:
    paths = {
        "_DATA_DIR": tmp_path,
        "_LOG_DIR": tmp_path / "logs",
        "_SECURITY_CHECKPOINTS_JSON": tmp_path / "security-checkpoints.json",
        "_SECURITY_CHECKPOINTS_LOCK": tmp_path / "security-checkpoints.lock",
        "_ACCOUNTS_JSON": tmp_path / "accounts.json",
        "_LEGACY_ACCOUNTS_JSON": tmp_path / "legacy-accounts.json",
        "_OUTLOOK_JSON": tmp_path / "outlook.json",
        "_LEGACY_OUTLOOK_JSON": tmp_path / "legacy-outlook.json",
        "_OUTLOOK_TXT": tmp_path / "outlook.txt",
        "_GENERIC_API_EMAIL_JSON": tmp_path / "generic.json",
        "_GENERIC_API_EMAIL_TXT": tmp_path / "generic.txt",
        "_DOMAIN_EMAIL_JSON": tmp_path / "domain.json",
        "_ACCOUNTS_TXT": tmp_path / "accounts.txt",
        "_TOKENS_TXT": tmp_path / "tokens.txt",
        "_VIEWER_HTML": tmp_path / "viewer.html",
    }
    for name, value in paths.items():
        monkeypatch.setattr(db, name, value)


def test_checkpoint_is_idempotent_and_does_not_complete_email_pool(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    outlook_path = tmp_path / "outlook.json"
    outlook_path.write_text(
        json.dumps([{"id": 1, "email": "User@Example.com", "status": "available"}]),
        encoding="utf-8",
    )

    first = db.save_security_checkpoint(
        " User@Example.com ",
        registration_password="Stable-pass-1!",
    )
    second = db.save_security_checkpoint(
        "user@example.com",
        registration_password="Stable-pass-1!",
        totp_secret="JBSWY3DPEHPK3PXP",
        access_token="fresh-token",
    )

    assert first and second
    assert second["email"] == "user@example.com"
    assert second["registration_password"] == "Stable-pass-1!"
    assert second["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert second["access_token"] == "fresh-token"
    rows = json.loads((tmp_path / "security-checkpoints.json").read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert not (tmp_path / "accounts.json").exists()
    pool = json.loads(outlook_path.read_text(encoding="utf-8"))
    assert pool == [{"id": 1, "email": "User@Example.com", "status": "available"}]
    assert "completed_at" not in pool[0]


def test_checkpoint_malformed_json_is_never_silently_overwritten(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    path = tmp_path / "security-checkpoints.json"
    original = "{malformed-sensitive-checkpoint"
    path.write_text(original, encoding="utf-8")

    import pytest

    with pytest.raises(RuntimeError, match="不可读|损坏"):
        db.save_security_checkpoint(
            "user@example.com",
            registration_password="Stable-pass-1!",
        )

    assert path.read_text(encoding="utf-8") == original


def test_checkpoint_read_error_is_reported_without_overwrite(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    path = tmp_path / "security-checkpoints.json"
    path.write_text("[]", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_checkpoint_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("simulated read denial")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_checkpoint_read)
    import pytest

    with pytest.raises(RuntimeError, match="不可读|损坏"):
        db.get_security_checkpoint("user@example.com")


def test_checkpoint_cross_process_writes_preserve_both_emails(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    workers = [
        context.Process(
            target=_save_checkpoint_in_child,
            args=(str(tmp_path), email, start_event),
        )
        for email in ("first@example.com", "second@example.com")
    ]
    for worker in workers:
        worker.start()
    start_event.set()
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0

    rows = db._load_security_checkpoints()
    assert {row["email"] for row in rows} == {"first@example.com", "second@example.com"}


def test_final_save_merges_checkpoint_and_clears_pending(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    db.save_security_checkpoint(
        "user@example.com",
        registration_password="Stable-pass-1!",
        totp_secret="JBSWY3DPEHPK3PXP",
        access_token="fresh-token",
    )
    archived = {}
    monkeypatch.setattr(
        account_export,
        "_append_batch_archive",
        lambda **kwargs: archived.update(kwargs) or tmp_path / "batch",
    )
    import core.plan_check_service as plan_check_service

    monkeypatch.setattr(
        plan_check_service,
        "enqueue_account_plan_check",
        lambda **kwargs: {"accepted": False, "busy": False, "error": "disabled-in-test"},
    )

    row_id = account_export.save_account_data(
        email="user@example.com",
        access_token="",
        totp_secret=None,
        extra={"codex": {"status": "skipped"}},
    )

    row = db.get_account(row_id)
    assert row["access_token"] == "fresh-token"
    assert row["totp_secret"] == "JBSWY3DPEHPK3PXP"
    extra = json.loads(row["extra_json"])
    assert extra["registration_password"] == "Stable-pass-1!"
    assert archived["access_token"] == "fresh-token"
    assert archived["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert archived["extra"]["registration_password"] == "Stable-pass-1!"
    assert db.get_security_checkpoint("user@example.com") is None
    assert not (tmp_path / "security-checkpoints.json").exists()


def test_final_save_uses_single_db_merge_and_archives_latest_checkpoint(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    db.save_security_checkpoint(
        "user@example.com",
        registration_password="Old-pass-1!",
        totp_secret="JBSWY3DPEHPK3PXP",
        access_token="old-token",
    )
    archived = {}
    monkeypatch.setattr(
        account_export,
        "_append_batch_archive",
        lambda **kwargs: archived.update(kwargs) or tmp_path / "batch",
    )
    import core.plan_check_service as plan_check_service

    queued = {}
    monkeypatch.setattr(
        plan_check_service,
        "enqueue_account_plan_check",
        lambda **kwargs: queued.update(kwargs) or {"accepted": False, "busy": False, "error": "disabled"},
    )
    original_insert = db.insert_account
    original_get_checkpoint = db.get_security_checkpoint

    def update_checkpoint_then_insert(**kwargs):
        db.save_security_checkpoint(
            "user@example.com",
            registration_password="New-pass-2!",
            totp_secret="KRUGS4ZANFZSAYJA",
            access_token="new-token",
        )
        return original_insert(**kwargs)

    monkeypatch.setattr(db, "insert_account", update_checkpoint_then_insert)
    monkeypatch.setattr(
        db,
        "get_security_checkpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("lock-free pre-read is forbidden")),
    )

    row_id = account_export.save_account_data(
        email="user@example.com",
        access_token="",
        totp_secret=None,
        extra={"codex": {"status": "skipped"}},
    )

    row = db.get_account(row_id)
    assert row["access_token"] == "new-token"
    assert row["totp_secret"] == "KRUGS4ZANFZSAYJA"
    assert json.loads(row["extra_json"])["registration_password"] == "New-pass-2!"
    assert archived["access_token"] == "new-token"
    assert archived["totp_secret"] == "KRUGS4ZANFZSAYJA"
    assert archived["extra"]["registration_password"] == "New-pass-2!"
    assert queued["access_token"] == "new-token"
    assert original_get_checkpoint("user@example.com") is None


def test_final_explicit_credentials_win_over_stale_checkpoint(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    db.save_security_checkpoint(
        "user@example.com",
        registration_password="Old-pass-1!",
        totp_secret="JBSWY3DPEHPK3PXP",
        access_token="old-token",
    )

    row_id = db.insert_account(
        email="user@example.com",
        access_token="new-token",
        totp_secret="KRUGS4ZANFZSAYJA",
        extra={"registration_password": "New-pass-2!"},
    )

    row = db.get_account(row_id)
    assert row["access_token"] == "new-token"
    assert row["totp_secret"] == "KRUGS4ZANFZSAYJA"
    assert json.loads(row["extra_json"])["registration_password"] == "New-pass-2!"
    assert db.get_security_checkpoint("user@example.com") is None


def test_empty_final_totp_does_not_erase_confirmed_checkpoint_secret(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    db.save_security_checkpoint(
        "user@example.com",
        totp_secret="JBSWY3DPEHPK3PXP",
        access_token="fresh-token",
    )

    row_id = db.insert_account(
        email="user@example.com",
        access_token="",
        totp_secret="",
        extra=None,
    )

    row = db.get_account(row_id)
    assert row["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert row["access_token"] == "fresh-token"
    assert db.get_security_checkpoint("user@example.com") is None


def test_final_merge_canonicalizes_email_whitespace(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    db.save_security_checkpoint(
        "user@example.com",
        registration_password="Stable-pass-1!",
        access_token="fresh-token",
    )

    row_id = db.insert_account(
        email=" User@Example.com ",
        access_token="",
        extra=None,
    )

    row = db.get_account(row_id)
    assert row["email"] == "User@Example.com"
    assert row["access_token"] == "fresh-token"
    assert json.loads(row["extra_json"])["registration_password"] == "Stable-pass-1!"
    assert db.get_security_checkpoint("user@example.com") is None


def test_checkpoint_merge_preserves_existing_account_metadata(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    row_id = db.insert_account(
        email="user@example.com",
        access_token="old-token",
        extra={
            "user": {"id": "u1"},
            "codex": {"status": "success"},
            "custom": "keep",
            "registration_password": "Old-pass-1!",
        },
    )
    db.save_security_checkpoint(
        "user@example.com",
        registration_password="Stable-pass-2!",
        access_token="fresh-token",
    )

    same_row_id = db.insert_account(
        email="user@example.com",
        access_token="",
        extra=None,
    )

    assert same_row_id == row_id
    row = db.get_account(row_id)
    assert row["access_token"] == "fresh-token"
    extra = json.loads(row["extra_json"])
    assert extra == {
        "user": {"id": "u1"},
        "codex": {"status": "success"},
        "custom": "keep",
        "registration_password": "Stable-pass-2!",
    }
    assert db.get_security_checkpoint("user@example.com") is None


def test_checkpoint_cleanup_failure_does_not_reverse_durable_account_save(monkeypatch, tmp_path) -> None:
    _isolate_storage(monkeypatch, tmp_path)
    db.save_security_checkpoint(
        "user@example.com",
        registration_password="Stable-pass-1!",
        access_token="fresh-token",
    )
    original_save = db._save_security_checkpoints

    def fail_only_when_clearing(rows):
        if not rows:
            raise PermissionError("simulated cleanup failure")
        return original_save(rows)

    monkeypatch.setattr(db, "_save_security_checkpoints", fail_only_when_clearing)

    row_id = db.insert_account(
        email="user@example.com",
        access_token="",
        extra=None,
    )

    row = db.get_account(row_id)
    assert row["access_token"] == "fresh-token"
    assert json.loads(row["extra_json"])["registration_password"] == "Stable-pass-1!"
    checkpoint_rows = json.loads((tmp_path / "security-checkpoints.json").read_text(encoding="utf-8"))
    assert len(checkpoint_rows) == 1


def test_confirmed_password_helper_only_forwards_nonempty_values(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(db, "save_security_checkpoint", lambda email, **kwargs: calls.append((email, kwargs)))

    assert persist_confirmed_registration_password("User@Example.com", "Stable-pass-1!") is True
    assert persist_confirmed_registration_password("User@Example.com", "") is False
    assert calls == [
        (
            "user@example.com",
            {"registration_password": "Stable-pass-1!"},
        )
    ]


def test_confirmed_password_checkpoint_retries_and_exposes_failure(monkeypatch) -> None:
    calls = []

    def fail_save(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("simulated disk failure")

    monkeypatch.setattr(db, "save_security_checkpoint", fail_save)
    monkeypatch.setattr(registration_password.time, "sleep", lambda *_args: None)

    assert persist_confirmed_registration_password("user@example.com", "Stable-pass-1!") is False
    assert len(calls) == 3


def test_activated_totp_checkpoint_retries_and_exposes_failure(monkeypatch) -> None:
    calls = []

    def fail_save(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("simulated disk failure")

    monkeypatch.setattr(db, "save_security_checkpoint", fail_save)
    monkeypatch.setattr(account_export.time, "sleep", lambda *_args: None)

    assert account_export._persist_activated_totp_checkpoint(
        "user@example.com",
        "JBSWY3DPEHPK3PXP",
        "fresh-token",
    ) is False
    assert len(calls) == 3


def test_atomic_json_retries_windows_busy_without_rewriting_temp(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    path = tmp_path / "accounts.json"
    path.write_text("[]", encoding="utf-8")
    original_replace = Path.replace
    attempts, waits, sources = [], [], []

    def replace(source, target):
        attempts.append(target)
        sources.append((source, source.read_bytes()))
        if len(attempts) <= 3:
            assert path.read_text(encoding="utf-8") == "[]"
            error = PermissionError("synthetic Windows sharing failure")
            error.winerror = (5, 32, 33)[len(attempts) - 1]
            raise error
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(db.time, "sleep", waits.append)
    db._write_json(path, [{"id": 1}])
    assert json.loads(path.read_text(encoding="utf-8")) == [{"id": 1}]
    assert len(attempts) == 4 and waits == [0.05, 0.1, 0.2]
    assert len(set(sources)) == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_json_permanent_busy_preserves_account_and_checkpoint(monkeypatch, tmp_path):
    import pytest

    _isolate_storage(monkeypatch, tmp_path)
    db.save_security_checkpoint("user@example.test", registration_password="confirmed-password", totp_secret="JBSWY3DPEHPK3PXP", access_token="confirmed-token")
    path = tmp_path / "accounts.json"
    original = b"[ ]\n"
    path.write_bytes(original)
    calls, waits = [], []
    error = PermissionError("synthetic permanent Windows sharing failure")
    error.winerror = 5

    def replace(source, target):
        calls.append(target)
        raise error

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(db.time, "sleep", waits.append)
    with pytest.raises(PermissionError) as caught:
        db.insert_account(email="user@example.test", access_token="confirmed-token")
    assert caught.value is error
    assert len(calls) == 6 and len(waits) == 5 and sum(waits) == 1.55
    assert path.read_bytes() == original
    assert db.get_security_checkpoint("user@example.test")["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_json_other_errors_and_cleanup_keep_original_failure(monkeypatch, tmp_path):
    import errno
    import pytest

    _isolate_storage(monkeypatch, tmp_path)
    path = tmp_path / "accounts.json"
    path.write_text("[]", encoding="utf-8")
    waits = []
    monkeypatch.setattr(db.time, "sleep", waits.append)
    original_unlink = Path.unlink
    for error in (PermissionError("non-Windows permission failure"), OSError(errno.ENOSPC, "disk full")):
        calls = []

        def replace(source, target):
            calls.append(target)
            raise error

        def unlink(source, **kwargs):
            raise PermissionError("synthetic cleanup failure")

        monkeypatch.setattr(Path, "replace", replace)
        monkeypatch.setattr(Path, "unlink", unlink)
        with pytest.raises(type(error)) as caught:
            db._write_json(path, [{"id": 1}])
        assert caught.value is error and len(calls) == 1 and waits == []
        assert path.read_text(encoding="utf-8") == "[]"
        for temporary in tmp_path.glob("*.tmp"):
            original_unlink(temporary)


def test_atomic_json_recovers_after_real_windows_delete_share_lock(monkeypatch, tmp_path):
    import ctypes
    import os
    import pytest
    from ctypes import wintypes

    if os.name != "nt":
        pytest.skip("Windows file sharing semantics")
    _isolate_storage(monkeypatch, tmp_path)
    path = tmp_path / "accounts.json"
    path.write_text("[]", encoding="utf-8")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 128, None)
    assert handle != ctypes.c_void_p(-1).value
    waits = []

    def release(delay):
        nonlocal handle
        waits.append(delay)
        assert path.read_text(encoding="utf-8") == "[]"
        assert kernel.CloseHandle(handle)
        handle = None

    monkeypatch.setattr(db.time, "sleep", release)
    try:
        db._write_json(path, [{"id": 1}])
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
    assert waits == [0.05]
    assert json.loads(path.read_text(encoding="utf-8")) == [{"id": 1}]
    assert list(tmp_path.glob("*.tmp")) == []
