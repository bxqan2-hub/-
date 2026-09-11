"""Keep DB-backed tests and WebUI startup recovery away from runtime data."""
from pathlib import Path

import pytest

from core import db


_DB_PROJECT_ROOT = db._PROJECT_ROOT
_DB_PATHS = {
    name: value.relative_to(_DB_PROJECT_ROOT)
    for name, value in vars(db).items()
    if name.startswith("_")
    and isinstance(value, Path)
    and value.is_relative_to(_DB_PROJECT_ROOT)
}


@pytest.fixture(scope="session", autouse=True)
def _isolated_db_session(tmp_path_factory):
    # unittest setUpClass runs before function fixtures; it needs a safe base.
    root = tmp_path_factory.mktemp("db-session")
    with pytest.MonkeyPatch.context() as patch:
        for name, relative in _DB_PATHS.items():
            patch.setattr(db, name, root / relative)
        yield


@pytest.fixture(autouse=True)
def _isolated_db_storage(_isolated_db_session, tmp_path, monkeypatch):
    # Existing test-specific overrides still take precedence inside each case.
    root = tmp_path / "db"
    for name, relative in _DB_PATHS.items():
        monkeypatch.setattr(db, name, root / relative)
