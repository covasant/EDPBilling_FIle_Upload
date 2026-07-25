"""Shared test setup.

The autouse fixture supplies a throwaway env (temp sqlite DB, MOCK CBOS) and
clears every lazy/cached singleton so each test starts from a clean, overridable
state - the whole point of removing the import-time singletons.
"""

import pytest


@pytest.fixture(autouse=True)
def test_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FILE_ROOT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("CBOS_MODE", "MOCK")

    # Settlement segment - separate upstream, same env/process (see
    # app/clients/dp_upload_client.py). Shared folder defaults to a
    # settlement/ subdir under the same tmp_path so tests don't need their
    # own fixture just to exercise the file-lookup step.
    monkeypatch.setenv("CBOS_SETL_MODE", "MOCK")
    monkeypatch.setenv("CBOS_SETL_SHARED_FOLDER_PATH", str(tmp_path / "settlement"))
    (tmp_path / "settlement").mkdir(exist_ok=True)

    from app.clients import cbos_client, dp_upload_client
    from app.core import database
    from app.core.config import get_settings

    def _clear():
        get_settings.cache_clear()
        database.get_engine.cache_clear()
        database.get_sessionmaker.cache_clear()
        cbos_client.reset_cbos_client()
        dp_upload_client.reset_dp_upload_client()

    _clear()  # this test's env wins
    yield
    _clear()  # don't leak into the next test
