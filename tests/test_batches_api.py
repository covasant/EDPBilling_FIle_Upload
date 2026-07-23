"""Contract tests for the batches API + completeness gate + force-proceed
(docs/BATCH_HANDOFF_CONTRACT.md). All against the in-process MOCK client;
the worker is driven synchronously (process_batch called directly on dequeued
tasks) so outcomes are deterministic.
"""

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _fast(monkeypatch):
    monkeypatch.setenv("CBOS_MOCK_RANDOM_SUCCESS_RATE", "1.0")
    monkeypatch.setenv("CBOS_MOCK_PENDING_POLLS", "0")
    monkeypatch.setenv("CBOS_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("CBOS_RETRY_DELAY_SECONDS", "0")
    from app.core.config import get_settings
    get_settings.cache_clear()


# The full mandatory MCX set (127/534/535; 320 is allowlisted-optional).
FULL_MCX_FILES = [
    ("MCX_ProductMaster.csv", 68),
    ("Position_MCXCCL_CO_0_CM_55930_20260720_F_0000.csv", 46),
    ("Trade_MCX_CO_0_CM_55930_20260720_F_0000.csv", 46),
]


def _make_batch_dir(root: Path, *, segment="MCX", trade_date="2026-07-20",
                    folder_date="20-07-2026", files=None, batch_id=None) -> Path:
    """Write real files + a valid manifest.json, exactly as the bot's
    finalization protocol produces them."""
    files = FULL_MCX_FILES if files is None else files
    seg_dir = root / folder_date / segment
    seg_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, cols in files:
        body = (",".join(str(i) for i in range(cols)) + "\n").encode()
        (seg_dir / name).write_bytes(body)
        entries.append({
            "name": name,
            "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body),
            "exchange": "MCX",
        })
    manifest = {
        "manifest_version": 1,
        "batch_id": batch_id or f"{segment}-{trade_date}-{uuid.uuid4().hex[:8]}",
        "segment": segment,
        "trade_date": trade_date,
        "correlation_id": "test-corr-1",
        "producer": {"name": "mofsl_file_download_rpa_bot", "version": "test", "action": "all"},
        "created_at": f"{trade_date}T03:12:45+05:30",
        "files": entries,
        "download_outcome": {"status": "success", "no_data": [], "failed": []},
    }
    (seg_dir / "manifest.json").write_text(json.dumps(manifest))
    return seg_dir / "manifest.json"


@pytest.fixture()
def client(monkeypatch):
    """TestClient with the real lifespan, but the background worker replaced by
    a manual pump so tests control exactly when batches process."""
    _fast(monkeypatch)
    import app.main as main_module

    def _no_worker(queue):  # replaces the forever-loop thread body
        return

    monkeypatch.setattr(main_module, "run_worker", _no_worker)
    with TestClient(main_module.app) as c:
        yield c


def _pump(client) -> None:
    """Process everything queued, synchronously."""
    from app.services import upload_service
    queue = client.app.state.batch_queue
    while not queue.empty():
        task = queue.get()
        try:
            upload_service.process_batch(task)
        finally:
            queue.task_done()
            queue.release(task.key)


def _root() -> Path:
    from app.core.config import settings
    return Path(settings.file_root_path)


# --- intake ------------------------------------------------------------------

def test_submit_full_batch_confirms(client):
    manifest_path = _make_batch_dir(_root())
    resp = client.post("/batches", json={"manifest_path": str(manifest_path)})
    assert resp.status_code == 202
    batch_id = resp.json()["batch_id"]

    _pump(client)

    status = client.get(f"/batches/{batch_id}").json()
    assert status["status"] == "confirmed"
    assert status["correlation_id"] == "test-corr-1"
    assert len(status["files"]) == 3
    assert all(f["status"] == "uploaded" for f in status["files"])


def test_submit_is_idempotent_on_batch_id(client):
    manifest_path = _make_batch_dir(_root(), batch_id="MCX-2026-07-20-aaaaaaaa")
    first = client.post("/batches", json={"manifest_path": str(manifest_path)})
    assert first.status_code == 202
    again = client.post("/batches", json={"manifest_path": str(manifest_path)})
    assert again.status_code == 200          # known, not re-queued
    assert again.json()["batch_id"] == "MCX-2026-07-20-aaaaaaaa"
    assert client.app.state.batch_queue.size == 1, "second POST must not enqueue"


def test_schema_invalid_manifest_is_400(client):
    bad = _root() / "20-07-2026" / "MCX"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text(json.dumps({"manifest_version": 1, "batch_id": "nope"}))
    resp = client.post("/batches", json={"manifest_path": str(bad / 'manifest.json')})
    assert resp.status_code == 400


def test_checksum_mismatch_is_422_and_recorded(client):
    manifest_path = _make_batch_dir(_root(), batch_id="MCX-2026-07-20-bbbbbbbb")
    # Corrupt one file after finalization.
    (manifest_path.parent / FULL_MCX_FILES[0][0]).write_bytes(b"tampered")
    resp = client.post("/batches", json={"manifest_path": str(manifest_path)})
    assert resp.status_code == 422

    status = client.get("/batches/MCX-2026-07-20-bbbbbbbb").json()
    assert status["status"] == "rejected"
    # Files stay in place - a superseding manifest is the fix.
    assert (manifest_path.parent / FULL_MCX_FILES[1][0]).exists()


def test_rescan_queues_unknown_manifests_only(client):
    known = _make_batch_dir(_root(), batch_id="MCX-2026-07-20-cccccccc")
    client.post("/batches", json={"manifest_path": str(known)})
    _pump(client)

    # A second day's manifest the callback never delivered.
    _make_batch_dir(_root(), trade_date="2026-07-21", folder_date="21-07-2026",
                    batch_id="MCX-2026-07-21-dddddddd")

    resp = client.post("/batches/rescan")
    assert resp.status_code == 202
    assert resp.json()["queued"] == ["MCX-2026-07-21-dddddddd"]

    again = client.post("/batches/rescan")
    assert again.json()["queued"] == [], "rescan must be idempotent"


# --- completeness gate -------------------------------------------------------

def test_incomplete_batch_parks_and_fileupload_stays_false(client):
    """Only 2 of MCX's 3 mandatory slots get files -> the gate must park the
    batch INCOMPLETE, never mark slot 127 optional, and FILEUPLOAD must NOT
    be confirmed."""
    manifest_path = _make_batch_dir(_root(), files=FULL_MCX_FILES[1:],  # no ProductMaster (127)
                                    batch_id="MCX-2026-07-20-eeeeeeee")
    client.post("/batches", json={"manifest_path": str(manifest_path)})
    _pump(client)

    status = client.get("/batches/MCX-2026-07-20-eeeeeeee").json()
    assert status["status"] == "incomplete"
    missing = [s["upload_id"] for s in status["status_detail"]["missing_slots"]]
    assert missing == ["127"]
    # The files that DID upload are safely in CBOS and moved to uploaded/.
    for f in status["files"]:
        assert f["status"] == "uploaded"
        assert "INCOMPLETE" in f["outcome"]

    from app.clients import cbos_client
    mock = cbos_client.get_cbos_client()
    # Slot 127's step must never have been marked optional by the gate.
    marked_steps = {step for (_pid, step) in mock.marked_optional}
    assert 1 not in marked_steps, "slot 127 (STEPNO 1) must not be auto-marked optional"


def test_superseding_manifest_completes_incomplete_batch(client):
    """The normal fix for INCOMPLETE: a fresh manifest with the missing file.
    Already-uploaded slots idempotent-skip via CBOS STATUS readback; the new
    file fills the gap; FILEUPLOAD confirms."""
    first = _make_batch_dir(_root(), files=FULL_MCX_FILES[1:], batch_id="MCX-2026-07-20-f1f1f1f1")
    client.post("/batches", json={"manifest_path": str(first)})
    _pump(client)
    assert client.get("/batches/MCX-2026-07-20-f1f1f1f1").json()["status"] == "incomplete"

    # Bot re-runs: full set this time (fresh batch_id, superseding manifest).
    second = _make_batch_dir(_root(), batch_id="MCX-2026-07-20-f2f2f2f2")
    client.post("/batches", json={"manifest_path": str(second)})
    _pump(client)

    assert client.get("/batches/MCX-2026-07-20-f2f2f2f2").json()["status"] == "confirmed"


# --- audited force-proceed ---------------------------------------------------

def test_force_proceed_marks_named_slots_and_confirms(client):
    manifest_path = _make_batch_dir(_root(), files=FULL_MCX_FILES[1:],  # 127 missing
                                    batch_id="MCX-2026-07-20-abababab")
    client.post("/batches", json={"manifest_path": str(manifest_path)})
    _pump(client)
    assert client.get("/batches/MCX-2026-07-20-abababab").json()["status"] == "incomplete"

    resp = client.post("/batches/MCX-2026-07-20-abababab/proceed",
                       json={"slots": ["127"],
                             "reason": "exchange declared no product master today"})
    assert resp.status_code == 202
    _pump(client)

    status = client.get("/batches/MCX-2026-07-20-abababab").json()
    assert status["status"] == "confirmed"
    assert status["proceed"]["slots"] == ["127"]
    assert "no product master" in status["proceed"]["reason"]


def test_force_proceed_rejected_unless_incomplete(client):
    manifest_path = _make_batch_dir(_root(), batch_id="MCX-2026-07-20-cdcdcdcd")
    client.post("/batches", json={"manifest_path": str(manifest_path)})
    _pump(client)

    resp = client.post("/batches/MCX-2026-07-20-cdcdcdcd/proceed",
                       json={"slots": ["127"], "reason": "nope"})
    assert resp.status_code == 409


def test_force_proceed_with_wrong_slots_stays_incomplete(client):
    manifest_path = _make_batch_dir(_root(), files=FULL_MCX_FILES[1:],
                                    batch_id="MCX-2026-07-20-efefefef")
    client.post("/batches", json={"manifest_path": str(manifest_path)})
    _pump(client)

    # 534 is FILLED - naming it means ops looked at stale info.
    resp = client.post("/batches/MCX-2026-07-20-efefefef/proceed",
                       json={"slots": ["534"], "reason": "mistake"})
    assert resp.status_code == 202
    _pump(client)

    status = client.get("/batches/MCX-2026-07-20-efefefef").json()
    assert status["status"] == "incomplete"
    assert "not unfilled" in status["status_detail"]["proceed_error"]


# --- manifest exchange metadata ----------------------------------------------

def test_manifest_entry_without_exchange_gets_placeholder(client, monkeypatch):
    from app.services import manifest_service
    manifest_path = _make_batch_dir(_root(), batch_id="MCX-2026-07-20-99999999")
    data = json.loads(manifest_path.read_text())
    for f in data["files"]:
        f.pop("exchange")
    manifest_path.write_text(json.dumps(data))

    loaded = manifest_service.load_manifest(manifest_path)
    assert all(exchange == "NA" for _path, exchange in loaded.files)
