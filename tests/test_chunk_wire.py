"""End-to-end Step 5 over real HTTP, against the mock CBOS server.

Every other test drives MockCBOSClient, which is an in-process Python object:
it never encodes a multipart body, never opens a socket, and throws the chunk
bytes away. So it can only ever confirm that we *call* the chunk API - never
that the bytes survive the trip.

Here the REAL CBOSClient talks to mock_cbos over a real socket, the server
reassembles the chunks in CurrentChunk order, and the test compares the SHA-256
of the reassembled file against the source. A wrong chunk size, an off-by-one
in the loop, a dropped tail chunk or a mis-encoded multipart body all change
that digest.

What this still does NOT prove: that real CBOS reassembles the way mock_cbos
does. Both sides were written from the same doc by the same author, so they can
agree with each other and both be wrong. This closes the transport gap, not the
did-we-understand-CBOS gap.

The server is started here in a background thread on an ephemeral port and torn
down after - nothing to run by hand.
"""

import hashlib
import socket
import threading
import time
from pathlib import Path

import pytest
import requests
import uvicorn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mock_server():
    """Run mock_cbos.app on an ephemeral port for the duration of the module."""
    from mock_cbos.app import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="mock-cbos")
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(100):  # ~5s budget for startup
        try:
            if requests.get(f"{base}/health", timeout=0.5).ok:
                break
        except requests.RequestException:
            time.sleep(0.05)
    else:
        server.should_exit = True
        pytest.fail("mock CBOS server did not come up")

    yield base

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def real_client(monkeypatch, mock_server):
    """A REAL CBOSClient (multipart over HTTP) pointed at the mock server."""
    requests.post(f"{mock_server}/__mock/reset", timeout=5)

    monkeypatch.setenv("CBOS_MODE", "REAL")
    monkeypatch.setenv("CBOS_CORE_BASE_URL", mock_server)
    monkeypatch.setenv("CBOS_GTG_BASE_URL", mock_server)
    monkeypatch.setenv("CBOS_LOGIN_ID", "CV0001")
    monkeypatch.setenv("CBOS_PASSWORD", "test-only-not-a-real-secret")

    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.clients.cbos_client import CBOSClient

    return CBOSClient()


def _state(mock_server) -> dict:
    return requests.get(f"{mock_server}/__mock/state", timeout=5).json()


def _make_file(tmp_path: Path, name: str, size: int) -> Path:
    """A file of `size` pseudo-random bytes - incompressible and order-sensitive,
    so a swapped or duplicated chunk changes the digest."""
    import random

    rnd = random.Random(1234)
    p = tmp_path / name
    p.write_bytes(bytes(rnd.getrandbits(8) for _ in range(size)))
    return p


@pytest.mark.parametrize(
    "size_kb, chunk_kb, expected_chunks",
    [
        (5, 512, 1),      # single chunk, smaller than the chunk size
        (512, 512, 1),    # exactly one chunk - the off-by-one boundary
        (513, 512, 2),    # one byte over: a tail chunk must be sent
        (2048, 512, 4),   # several whole chunks
        (2049, 512, 5),   # several chunks plus a 1-byte tail
    ],
)
def test_chunks_reassemble_to_the_same_bytes(monkeypatch, mock_server, real_client,
                                             tmp_path, size_kb, chunk_kb, expected_chunks):
    """The file CBOS ends up with must be byte-identical to the one we sent."""
    monkeypatch.setenv("CHUNK_SIZE_KB", str(chunk_kb))
    from app.core.config import get_settings

    get_settings.cache_clear()

    src = _make_file(tmp_path, f"Trade_MCX_{size_kb}kb.csv", size_kb * 1024)
    guid = f"guid-{size_kb}-{chunk_kb}"

    real_client.upload_file(src, upload_id="535", guid=guid)

    entry = _state(mock_server)["guids"][guid]["files"][src.name]
    assert entry["missing_chunks"] == [], "server never received every chunk"
    assert entry["complete"] is True
    assert entry["total_chunks"] == expected_chunks
    assert entry["total_bytes"] == src.stat().st_size
    assert entry["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest(), (
        "reassembled file differs from the source - the bytes did not survive Step 5"
    )


def test_empty_file_still_produces_one_chunk(monkeypatch, mock_server, real_client, tmp_path):
    """A 0-byte file must still be sent as a single empty chunk, not skipped -
    otherwise Step 7 registers a GUID folder CBOS has nothing in."""
    monkeypatch.setenv("CHUNK_SIZE_KB", "512")
    from app.core.config import get_settings

    get_settings.cache_clear()

    src = tmp_path / "Trade_MCX_empty.csv"
    src.write_bytes(b"")
    guid = "guid-empty"

    real_client.upload_file(src, upload_id="535", guid=guid)

    entry = _state(mock_server)["guids"][guid]["files"][src.name]
    assert entry["total_chunks"] == 1
    assert entry["total_bytes"] == 0
    assert entry["sha256"] == hashlib.sha256(b"").hexdigest()


def test_missing_chunk_is_detected(mock_server):
    """Guard on the guard: if a chunk never arrives, the server must report the
    file incomplete and refuse to produce a digest. Without this, a green
    checksum could just mean 'the server accepted whatever it got'."""
    requests.post(f"{mock_server}/__mock/reset", timeout=5)
    url = f"{mock_server}/v1/api/process/SaveTradePromodalUploadChunkFile"

    # Send chunks 0 and 2 of a declared 3, skipping 1.
    for idx in (0, 2):
        requests.post(
            url,
            data={"UPLOADID": "535", "CurrentChunk": str(idx), "TotalChunks": "3",
                  "Guid": "guid-gap", "FileName": "gappy.csv"},
            files={"file": ("gappy.csv", b"x" * 10)},
            timeout=5,
        ).raise_for_status()

    entry = _state(mock_server)["guids"]["guid-gap"]["files"]["gappy.csv"]
    assert entry["missing_chunks"] == [1]
    assert entry["complete"] is False
    assert entry["sha256"] is None, "an incomplete file must not report a digest"
