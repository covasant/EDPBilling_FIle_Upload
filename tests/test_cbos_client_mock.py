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
        def _get_new_trade_process(self, segment, trade_date, process_id="0"):
            return {"Status": "Success", "Result": {"Table1": [], "Table2": [{"UPLOADID": 81}]}}

    with pytest.raises(CBOSUploadError, match="PROCESSID"):
        _NoProcessId().reserve_process("MCX", "14-07-2026")


def test_reserve_process_raises_when_table2_is_empty():
    class _NoTable2(MockCBOSClient):
        def _get_new_trade_process(self, segment, trade_date, process_id="0"):
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


def test_upload_settings_returns_a_decoded_rule():
    """Callers get an UploadRule, never a raw settings row - every CBOS field
    name stays inside this module. See tests/test_real_cbos_payloads.py."""
    rule = cbos_client.get_cbos_client().upload_settings("81")
    assert rule.upload_id == "81"
    assert rule.file_name_pattern == "SCRIP"
    assert rule.extension == "TXT"
    assert rule.compare_operator == "CONTAINS"


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
    assert client.confirm_upload("MCX") == "TRUE"


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

    assert _AlwaysFalse().confirm_upload("MCX") == cbos_client.POLL_TIMED_OUT
    assert _AlwaysFalse.polls == 3, "should poll exactly the configured number of times"


def test_uploader_exposes_no_trigger():
    """This repo owns the upload lane only - the CBOS trigger (Step 10) belongs
    to EDP_Billing. See CONTEXT.md's handoff."""
    assert not hasattr(cbos_client, "trigger_process")
    assert not hasattr(cbos_client.get_cbos_client(), "trigger_process")


def test_skip_is_a_verdict_and_stops_polling(monkeypatch):
    """SKIP is CBOS's answer, not "not yet", so the poll returns it immediately
    instead of spending the whole attempt budget on it.

    Real CBOS answered SKIP for every one of 30 polls on 2026-07-21 and the
    batch was reported as a timeout - the message that would have explained the
    run was buried under 29 identical repeats of itself.
    """
    monkeypatch.setenv("CBOS_POLL_MAX_ATTEMPTS", "30")
    monkeypatch.setenv("CBOS_POLL_INTERVAL_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    class _AlwaysSkip(MockCBOSClient):
        polls = 0

        def _file_upload_status(self, segment):
            _AlwaysSkip.polls += 1
            return {"Status": "Success", "Data": [{"MSG": "SKIP"}]}

    assert _AlwaysSkip().confirm_upload("MCX") == "SKIP"
    assert _AlwaysSkip.polls == 1, "SKIP must not be retried - it is not a pending state"


def test_zero_poll_attempts_does_not_crash(monkeypatch):
    """A configured budget of 0 skips the loop body, so the timeout log line has
    no message to report. Guarding the unbound-name crash that would otherwise
    take out the batch AFTER every file had already uploaded successfully."""
    monkeypatch.setenv("CBOS_POLL_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("CBOS_POLL_INTERVAL_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    assert MockCBOSClient().confirm_upload("MCX") == cbos_client.POLL_TIMED_OUT


def test_upload_ip_address_prefers_the_configured_value(monkeypatch):
    """Step 7's ipaddress comes from .env when set, so it can be pointed at
    whichever address CBOS turns out to want without a code change.

    The API doc's own example carries the CORE host's address rather than the
    caller's, and we had been sending the client machine's IP without ever
    checking - hence configurable rather than a new hardcoded guess.
    """
    from app.clients.cbos_client import _upload_ip_address
    from app.core.config import get_settings

    monkeypatch.setenv("CBOS_UPLOAD_IP_ADDRESS", "10.167.202.164")
    get_settings.cache_clear()
    assert _upload_ip_address() == "10.167.202.164"

    # Unset falls back to the detected local IP - the previous behaviour, so
    # nothing changes for a deployment that doesn't set it.
    monkeypatch.setenv("CBOS_UPLOAD_IP_ADDRESS", "")
    get_settings.cache_clear()
    assert _upload_ip_address() != "10.167.202.164"
