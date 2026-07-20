"""Unit tests for the CBOS client seam in MOCK mode (no network)."""

from app.clients import cbos_client


def test_mock_reserve_yields_process_id_and_candidates():
    resp = cbos_client.get_new_trade_process("MCX", "CV0001", "14-07-2026")
    pid = cbos_client.extract_process_id(resp)
    assert pid and pid.isdigit()
    candidates = cbos_client.extract_upload_candidates(resp)
    assert candidates, "Table2 should offer at least one upload candidate"


def test_mock_upload_settings_shape():
    resp = cbos_client.get_upload_settings("81")
    row = resp["Result"][0]
    assert "FILE NAME" in row and "FILEEXTENSION" in row


def test_upload_streams_in_multiple_chunks(monkeypatch, tmp_path):
    """A file larger than chunk_size_kb is streamed as N chunks (not one whole
    read), one upload_chunk call per chunk under a single GUID."""
    monkeypatch.setenv("CHUNK_SIZE_KB", "1")  # 1 KB chunks
    from app.core.config import get_settings

    get_settings.cache_clear()
    client = cbos_client.get_cbos_client()

    f = tmp_path / "big.csv"
    f.write_bytes(b"x" * 4500)  # ceil(4500 / 1024) = 5 chunks
    cbos_client.upload_file_chunks(f, "127", "guid-big")

    calls = [c for c in client.upload_calls if c[1] == "big.csv"]
    assert len(calls) == 5


def test_mock_gtg_poll_resolves_true_for_success_file(monkeypatch):
    """Exercises the real poll loop against the mock. Pending polls + interval
    are zeroed so it resolves on the first attempt with no sleeping."""
    monkeypatch.setenv("CBOS_MOCK_PENDING_POLLS", "0")
    monkeypatch.setenv("CBOS_POLL_INTERVAL_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    client = cbos_client.get_cbos_client()
    client.create_file_entry("81", "guid-1", "success_file.txt", "CV0001", "17658", "14-07-2026")
    assert cbos_client.poll_file_upload_status("MCX", "CV0001") is True
