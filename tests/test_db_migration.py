import sqlite3
from unittest.mock import MagicMock

from core import db


def test_unchanged_source_is_retried_after_transient_failure(tmp_path, monkeypatch):
    source = tmp_path / "registrations.db"
    source.write_bytes(b"legacy")
    monkeypatch.setattr(db, "_LEGACY_SQLITE", source)
    monkeypatch.setattr(db, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(db, "_OUTLOOK_TXT", tmp_path / "outlook.txt")
    migrate = MagicMock(side_effect=[{"sqlite_error": "locked"}, {"sqlite_accounts_imported": 1}])
    monkeypatch.setattr(db, "_migrate_legacy_sqlite", migrate)
    assert db.migrate_legacy_files()["sqlite_error"] == "locked"
    assert db.migrate_legacy_files()["sqlite_accounts_imported"] == 1


def test_sqlite_connection_closes_on_migration_error(tmp_path, monkeypatch):
    source = tmp_path / "registrations.db"
    source.touch()
    monkeypatch.setattr(db, "_LEGACY_SQLITE", source)
    conn = MagicMock()
    conn.execute.side_effect = sqlite3.OperationalError("locked")
    monkeypatch.setattr(db.sqlite3, "connect", lambda _: conn)
    assert "locked" in db._migrate_legacy_sqlite()["sqlite_error"]
    conn.close.assert_called_once()


def test_bad_sqlite_account_does_not_block_later_records(tmp_path, monkeypatch):
    source = tmp_path / "registrations.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE registered_accounts (email, access_token, totp_secret, user_id, "
                     "user_name, plan_type, expires_at, device_id, proxy_used, email_source, extra_json)")
        conn.executemany("INSERT INTO registered_accounts VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?)",
                         [("bad@example.test", "test-token", "{}"),
                          ("good@example.test", "test-token", "not-json")])
    monkeypatch.setattr(db, "_LEGACY_SQLITE", source)
    insert = MagicMock(side_effect=[ValueError("bad account"), None])
    monkeypatch.setattr(db, "insert_account", insert)
    result = db._migrate_legacy_sqlite()
    assert result["sqlite_accounts_imported"] == 1
    assert result["sqlite_accounts_skipped"] == 1
    assert insert.call_args.kwargs["email"] == "good@example.test"
    assert insert.call_args.kwargs["extra"] is None
