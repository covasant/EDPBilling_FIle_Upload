"""The regression tests for the import-time-singleton fix itself."""


def test_settings_are_lazy_and_overridable(monkeypatch):
    """settings.x reads through the cached get_settings(), so an env change +
    cache_clear takes effect - impossible when settings was captured at import."""
    from app.core.config import get_settings, settings

    assert settings.cbos_mode == "MOCK"  # from the autouse fixture

    monkeypatch.setenv("CBOS_MODE", "REAL")
    get_settings.cache_clear()
    assert settings.cbos_mode == "REAL"


def test_db_engine_is_lazy_not_built_at_import():
    """get_engine() builds on first call, not at import; init_db() works against
    the temp sqlite DB from the fixture."""
    from app.core import database

    database.init_db()
    session = database.get_sessionmaker()()
    try:
        # a trivial round-trip proves the engine is live
        from sqlalchemy import text

        assert session.execute(text("SELECT 1")).scalar() == 1
    finally:
        session.close()


def test_cbos_client_factory_and_injection():
    from app.clients import cbos_client

    assert type(cbos_client.get_cbos_client()).__name__ == "MockCBOSClient"

    sentinel = cbos_client.MockCBOSClient()
    cbos_client.set_cbos_client(sentinel)
    assert cbos_client.get_cbos_client() is sentinel

    cbos_client.reset_cbos_client()
    assert cbos_client.get_cbos_client() is not sentinel
