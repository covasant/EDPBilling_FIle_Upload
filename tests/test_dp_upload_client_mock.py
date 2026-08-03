"""Unit tests for the settlement DP upload client in MOCK mode (no network).

Mirrors tests/test_cbos_client_mock.py's structure: drives the public
interface methods (BaseDPUploadClient), not the raw calls underneath them.
"""

import pytest

from app.clients import dp_upload_client
from app.clients.dp_upload_client import (
    POLL_TIMED_OUT,
    STATUS_SUCCESS,
    DPUploadError,
    MockDPUploadClient,
)


def test_list_upload_masters_returns_summaries():
    masters = dp_upload_client.get_dp_upload_client().list_upload_masters()
    assert masters
    assert all(m.id and m.display_name for m in masters)


def test_get_upload_master_config_parses_process_required_and_columns():
    config = dp_upload_client.get_dp_upload_client().get_upload_master_config("22")
    assert config.upload_id == "22"
    assert config.process_required is True
    assert "SETTLEMENT_NUMBER" in config.columns_name


def test_get_upload_master_config_raises_when_no_result():
    class _NoConfig(MockDPUploadClient):
        def _get_upload_master_details(self, upload_id):
            return {"Status": "Success", "Result": []}

    with pytest.raises(DPUploadError, match="no config"):
        _NoConfig().get_upload_master_config("999")


def test_validate_file_raises_on_error_marker_in_description_list():
    """Integration guide's error shape: Description is a list whose first
    element is "Error_msg"."""

    class _ListError(MockDPUploadClient):
        def _validate_file(self, config, file_name, unique_identifier):
            return {"Status": "Success", "Description": ["Error_msg"], "Result": "[]"}

    config = _ListError().get_upload_master_config("22")
    with pytest.raises(DPUploadError):
        _ListError().validate_file(config, "bad.csv", "123.456")


def test_validate_file_raises_on_error_marker_as_bare_string():
    """Actual-requests doc's error shape: Description is a bare string."""

    class _StringError(MockDPUploadClient):
        def _validate_file(self, config, file_name, unique_identifier):
            return {"Status": "Success", "Description": "Error_msg", "Result": "Success"}

    config = _StringError().get_upload_master_config("22")
    with pytest.raises(DPUploadError):
        _StringError().validate_file(config, "bad.csv", "123.456")


def test_validate_file_succeeds_on_ok_response():
    client = dp_upload_client.get_dp_upload_client()
    config = client.get_upload_master_config("22")
    client.validate_file(config, "success_file.csv", "123.456")  # must not raise


def test_upload_chunks_streams_in_multiple_chunks(monkeypatch):
    """A file larger than chunk_setl_size_kb is streamed as N sequential
    chunks under one guid-prefixed name."""
    monkeypatch.setenv("CHUNK_SETL_SIZE_KB", "1")  # 1 KB chunks
    from app.core.config import get_settings

    get_settings.cache_clear()
    client = dp_upload_client.get_dp_upload_client()

    file_bytes = b"x" * 4500  # ceil(4500 / 1024) = 5 chunks
    chunk_full_path = client.upload_chunks(file_bytes, "big.csv")

    assert len([c for c in client.chunk_calls]) == 5
    assert chunk_full_path.endswith("_big.csv")


def test_upload_chunks_raises_on_failed_status():
    class _FailingChunk(MockDPUploadClient):
        def _upload_chunk(self, file_name, chunk_bytes, current_chunk, total_chunks):
            return {"Status": "Failed", "FileName": file_name}

    with pytest.raises(DPUploadError, match="failed on chunk"):
        _FailingChunk().upload_chunks(b"small file", "small.csv")


def test_upload_chunks_raises_on_fcount_mismatch():
    class _MissingChunks(MockDPUploadClient):
        def _upload_chunk(self, file_name, chunk_bytes, current_chunk, total_chunks):
            return {
                "Status": "FileUploaded",
                "FileName": file_name,
                "currentChunk": str(current_chunk),
                "totalChunks": str(total_chunks),
                "fCount": "0",  # wrong on purpose
            }

    with pytest.raises(DPUploadError, match="fCount"):
        _MissingChunks().upload_chunks(b"small file", "small.csv")


def test_finalize_upload_returns_tran_id():
    client = dp_upload_client.get_dp_upload_client()
    config = client.get_upload_master_config("22")
    tran_id = client.finalize_upload(config, "f.csv", "guid_f.csv", "123.456")
    assert tran_id


def test_finalize_upload_raises_when_no_tran_id():
    class _NoTranId(MockDPUploadClient):
        def _finalize_upload(self, config, file_name, chunk_full_path, unique_identifier):
            return {"Status": "Success", "Result": [{"Status": 0}], "TranId": None}

    config = _NoTranId().get_upload_master_config("22")
    with pytest.raises(DPUploadError, match="Tran_Id"):
        _NoTranId().finalize_upload(config, "f.csv", "guid_f.csv", "123.456")


def test_poll_status_resolves_to_success(monkeypatch):
    monkeypatch.setenv("CBOS_SETL_MOCK_PENDING_POLLS", "0")
    monkeypatch.setenv("CBOS_SETL_POLL_INTERVAL_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    client = dp_upload_client.get_dp_upload_client()
    status_code, description = client.poll_status("168530")
    assert status_code == STATUS_SUCCESS
    assert "success" in description.lower()


def test_poll_status_times_out_after_max_attempts(monkeypatch):
    monkeypatch.setenv("CBOS_SETL_POLL_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("CBOS_SETL_POLL_INTERVAL_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    class _AlwaysPending(MockDPUploadClient):
        polls = 0

        def _get_upload_status(self, tran_id):
            _AlwaysPending.polls += 1
            return {"Status": "Success", "Result": [{"Tran_Id": tran_id, "Status": 1, "Description": "In-Process"}]}

    status_code, _ = _AlwaysPending().poll_status("1")
    assert status_code == POLL_TIMED_OUT
    assert _AlwaysPending.polls == 3


def test_run_process_returns_response_text():
    client = dp_upload_client.get_dp_upload_client()
    config = client.get_upload_master_config("22")
    response = client.run_process(config, "123.456", "168530")
    assert "Processed successfully" in response


def test_factory_rejects_invalid_mode(monkeypatch):
    monkeypatch.setenv("CBOS_SETL_MODE", "BOGUS")
    from app.core.config import get_settings

    get_settings.cache_clear()
    dp_upload_client.reset_dp_upload_client()
    with pytest.raises(DPUploadError, match="Invalid CBOS_SETL_MODE"):
        dp_upload_client.get_dp_upload_client()
