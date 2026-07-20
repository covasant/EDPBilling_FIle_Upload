"""Unit tests for the CBOS client interface in MOCK mode (no network).

These drive the eight methods callers actually use, not the raw calls
underneath them - the parsing, chunking and polling loops live on
BaseCBOSClient and are what's being exercised here.
"""

import pytest

from app.clients import cbos_client
from app.clients.cbos_client import CBOSUploadError, MockCBOSClient


def test_reserve_process_returns_a_process_id_and_candidates():
    reservation = cbos_client.get_cbos_client().reserve_process("MCX", "14-07-2026")
    assert reservation.process_id.isdigit()
    assert reservation.candidates, "Table2 should offer at least one upload candidate"
    assert all(isinstance(c.upload_id, str) for c in reservation.candidates)


def test_reserve_process_raises_when_cbos_returns_no_process_id():
    """The envelope parsing is the client's job - a malformed Step 2 response
    must fail here, not surface as a None somewhere downstream."""

    class _NoProcessId(MockCBOSClient):
        def _get_new_trade_process(self, segment, trade_date):
            return {"Status": "Success", "Result": {"Table1": [], "Table2": [{"UPLOADID": 81}]}}

    with pytest.raises(CBOSUploadError, match="PROCESSID"):
        _NoProcessId().reserve_process("MCX", "14-07-2026")


def test_reserve_process_raises_when_table2_is_empty():
    class _NoTable2(MockCBOSClient):
        def _get_new_trade_process(self, segment, trade_date):
            return {"Status": "Success", "Result": {"Table1": [{"PROCESSID": 1}], "Table2": []}}

    with pytest.raises(CBOSUploadError, match="Table2"):
        _NoTable2().reserve_process("MCX", "14-07-2026")


def test_candidate_knows_whether_a_file_is_expected():
    """A zero UPLOADID is a pipeline step that takes no file; only non-zero
    slots can be left empty and need marking optional at Step 8."""
    reservation = cbos_client.get_cbos_client().reserve_process("MCX", "14-07-2026")
    expecting = [c for c in reservation.candidates if c.expects_a_file]
    assert expecting
    assert all(c.upload_id not in ("0", "") for c in expecting)


def test_upload_settings_strips_the_envelope():
    """The key is "FILE NAME (CONTAINS)" - real CBOS bakes the match operator
    into the key name. See tests/test_real_cbos_payloads.py."""
    setting = cbos_client.get_cbos_client().upload_settings("81")
    assert setting["FILE NAME (CONTAINS)"] == "SCRIP"
    assert "FILEEXTENSION" in setting


def test_upload_settings_returns_none_when_cbos_offers_nothing():
    class _NoSettings(MockCBOSClient):
        def _get_upload_settings(self, upload_id):
            return {"Status": "Success", "Result": []}

    assert _NoSettings().upload_settings("999") is None


def test_upload_file_streams_in_multiple_chunks(monkeypatch, tmp_path):
    """A file larger than chunk_size_kb is streamed as N chunks (not one whole
    read), one chunk call per chunk under a single GUID."""
    monkeypatch.setenv("CHUNK_SIZE_KB", "1")  # 1 KB chunks
    from app.core.config import get_settings

    get_settings.cache_clear()
    client = cbos_client.get_cbos_client()

    f = tmp_path / "big.csv"
    f.write_bytes(b"x" * 4500)  # ceil(4500 / 1024) = 5 chunks
    client.upload_file(f, "127", "guid-big")

    assert len([c for c in client.upload_calls if c[1] == "big.csv"]) == 5


def test_upload_file_retries_a_failing_chunk(monkeypatch, tmp_path):
    monkeypatch.setenv("CBOS_CHUNK_RETRY_ATTEMPTS", "3")
    from app.core.config import get_settings

    get_settings.cache_clear()

    class _FlakyChunk(MockCBOSClient):
        attempts = 0

        def _upload_chunk(self, *args, **kwargs):
            _FlakyChunk.attempts += 1
            if _FlakyChunk.attempts < 3:
                raise CBOSUploadError("transient blip")
            return super()._upload_chunk(*args, **kwargs)

    f = tmp_path / "small.csv"
    f.write_text("a,b\n")
    _FlakyChunk().upload_file(f, "127", "guid-1")  # must not raise
    assert _FlakyChunk.attempts == 3


def test_confirm_upload_resolves_true_for_a_success_file(monkeypatch):
    """Exercises the real poll loop against the mock. Pending polls + interval
    are zeroed so it resolves on the first attempt with no sleeping."""
    monkeypatch.setenv("CBOS_MOCK_PENDING_POLLS", "0")
    monkeypatch.setenv("CBOS_POLL_INTERVAL_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    client = cbos_client.get_cbos_client()
    client.register_file("81", "guid-1", "success_file.txt", "17658", "14-07-2026")
    assert client.confirm_upload("MCX") is True


def test_confirm_upload_returns_false_after_exhausting_attempts(monkeypatch):
    monkeypatch.setenv("CBOS_POLL_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("CBOS_POLL_INTERVAL_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    class _AlwaysFalse(MockCBOSClient):
        polls = 0

        def _file_upload_status(self, segment):
            _AlwaysFalse.polls += 1
            return {"Status": "Success", "Data": [{"MSG": "FALSE"}]}

    assert _AlwaysFalse().confirm_upload("MCX") is False
    assert _AlwaysFalse.polls == 3, "should poll exactly the configured number of times"


def test_uploader_exposes_no_trigger():
    """This repo owns the upload lane only - the CBOS trigger (Step 10) belongs
    to EDP_Billing. See CONTEXT.md's handoff."""
    assert not hasattr(cbos_client, "trigger_process")
    assert not hasattr(cbos_client.get_cbos_client(), "trigger_process")
