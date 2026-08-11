"""Regression tests for H7 (no committed credentials) and H8 (no password in logs)."""

import pytest

from app.clients import cbos_client
from app.clients.cbos_client import CBOSUploadError, _redact


def test_no_credentials_committed_in_defaults():
    """With nothing in the environment for them, the CBOS creds/hosts are empty -
    no real values baked into the code."""
    from app.core.config import Settings, reveal

    s = Settings(_env_file=None, file_root_path="/x", database_url="sqlite://")
    assert s.cbos_login_id == ""
    # Through reveal(): cbos_password is a SecretStr, so `== ""` compares against the
    # wrapper and is True for ANY password. Comparing the revealed text is the only
    # form of this assertion that still fails if a credential is ever committed.
    assert reveal(s.cbos_password) == ""
    assert reveal(s.cbos_setl_seskey) == ""
    assert s.cbos_gtg_base_url == ""
    assert s.cbos_core_base_url == ""


def test_credential_fields_are_not_rendered_by_repr():
    """The point of SecretStr: a stray logger.debug(settings), a ValidationError
    traceback, or an error tracker that serialises locals must not print the password."""
    from app.core.config import Settings

    s = Settings(
        _env_file=None,
        file_root_path="/x",
        database_url="sqlite://",
        cbos_password="hunter2",
        cbos_setl_seskey="seskey-secret",
    )
    rendered = f"{s!r} {s!s}"
    assert "hunter2" not in rendered
    assert "seskey-secret" not in rendered


def test_real_mode_without_credentials_fails_fast(monkeypatch):
    monkeypatch.setenv("CBOS_MODE", "REAL")
    # ensure no creds leak in from anywhere
    for k in ("CBOS_LOGIN_ID", "CBOS_PASSWORD", "CBOS_CORE_BASE_URL", "CBOS_GTG_BASE_URL"):
        monkeypatch.setenv(k, "")
    from app.core.config import get_settings

    get_settings.cache_clear()
    cbos_client.reset_cbos_client()
    with pytest.raises(CBOSUploadError) as exc:
        cbos_client.get_cbos_client()
    assert "REAL" in str(exc.value)


def test_redact_masks_password_keys():
    payload = {
        "GROUPNAME": "MCX",
        "LOGINID": "CV0001",
        "PASSWORD": "Master#123",
        "TRADEDATE": "2026-07-17",
    }
    red = _redact(payload)
    assert red["PASSWORD"] == "***"
    assert red["GROUPNAME"] == "MCX"  # non-secret untouched
    assert "Master#123" not in str(red)  # the secret is nowhere in the logged form


def test_redact_is_noop_for_non_dict():
    assert _redact("not-a-dict") == "not-a-dict"
