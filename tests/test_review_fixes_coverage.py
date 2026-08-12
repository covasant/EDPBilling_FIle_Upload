"""Tests for fixes that were previously verified by inspection only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

# ── M6: an unknown status must be refused, not written silently ──────────────


def test_a_typod_status_is_refused_rather_than_written():
    """update() is a blind setattr over **fields. Because the state checks elsewhere
    match the EXACT string, a typo'd literal made the row permanently invisible to every
    state-based query — silently, and with nothing raised anywhere."""
    from app.core import database
    from app.repositories.uploaded_file_repository import UploadedFileRepository

    database.init_db()
    session = database.get_sessionmaker()()
    try:
        repo = UploadedFileRepository(session)
        rec = repo.create_audit_record("/x/y/typo.csv", "17-07-2026", "MCX", "NA")
        repo.commit()
        with pytest.raises(ValueError, match="unknown status"):
            repo.update(rec, status="uplaoded")  # the classic transposition
        repo.update(rec, status="uploaded")  # the real one still works
    finally:
        session.close()


def test_the_settlement_vocabulary_is_enforced_too():
    from app.models.settlement_upload import STATUSES

    assert "polling" in STATUSES and "uplaoded" not in STATUSES


# ── L5: a full queue must answer 503, never block the request thread ─────────


def test_a_full_queue_raises_rather_than_blocking():
    """There is no backpressure anywhere else between intake and the single draining
    worker, so an unbounded queue accepted work forever while falling further behind."""
    from app.core.queue import BatchQueue, QueueFullError, SegmentBatchTask

    q = BatchQueue(maxsize=1)
    q.enqueue(SegmentBatchTask(folder_date="17-07-2026", segment="MCX", batch_id="a"))
    with pytest.raises(QueueFullError):
        q.enqueue(SegmentBatchTask(folder_date="17-07-2026", segment="MCX", batch_id="b"))


def test_a_rejected_enqueue_releases_its_in_flight_guard():
    """The guard is taken before the put. Leaving it held on a full queue would make
    that segment/date un-enqueueable for the life of the process."""
    from app.core.queue import BatchQueue, QueueFullError, SegmentBatchTask

    q = BatchQueue(maxsize=1)
    q.enqueue(SegmentBatchTask(folder_date="17-07-2026", segment="MCX", batch_id="a"))
    rejected = SegmentBatchTask(folder_date="17-07-2026", segment="MCX", batch_id="b")
    with pytest.raises(QueueFullError):
        q.enqueue(rejected)
    assert not q.is_queued(rejected.key), "guard left held; this batch could never requeue"


# ── M2: both settlement endpoints must agree on what upstream failure looks like ──


def test_the_status_endpoint_maps_an_upstream_failure_to_502(monkeypatch, tmp_path):
    """submit_upload mapped DPUploadError to 502; get_upload_status had no handler, so a
    routine re-poll during a DP blip surfaced as an opaque 500."""
    monkeypatch.setenv("FILE_ROOT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")
    monkeypatch.setenv("CBOS_SETL_MODE", "MOCK")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.clients.dp_upload_client import DPUploadError
    from app.main import app
    from app.services import settlement_service

    def boom(*a, **k):
        raise DPUploadError("DP upload host refused the connection")

    monkeypatch.setattr(settlement_service, "check_status", boom)
    with TestClient(app) as client:
        resp = client.get("/settlements/uploads/1/status")
    assert resp.status_code == 502, f"got {resp.status_code}, expected 502 like submit_upload"


# ── M4: the engine's pool must be disposed, not just dereferenced ────────────


def test_reset_engine_disposes_the_pool_before_dropping_it():
    """Clearing the lru_cache alone only drops the reference; the pool's connections stay
    open until the GC happens to collect it. The suite clears this around every test."""
    from app.core import database

    engine = database.get_engine()
    disposed = {"n": 0}
    original = engine.dispose
    engine.dispose = lambda *a, **k: (disposed.__setitem__("n", disposed["n"] + 1), original())[1]

    database.reset_engine()
    assert disposed["n"] == 1, "the pool was dropped without being disposed"


# ── L6: every persisted timestamp comes from one timezone-aware clock ────────


def test_all_three_models_stamp_the_same_aware_utc():
    """batch.py stamped timezone-aware while the other two stamped naive, so the same
    conceptual field serialised two ways and any comparison across them raised
    TypeError: can't subtract offset-naive and offset-aware datetimes."""
    from app.models.batch import _utcnow as batch_now
    from app.models.settlement_upload import _utcnow as setl_now
    from app.models.uploaded_file import _utcnow as file_now

    for fn in (batch_now, setl_now, file_now):
        stamped = fn()
        assert stamped.tzinfo is not None, f"{fn.__module__} still stamps naive datetimes"
        assert stamped.utcoffset() == datetime.now(UTC).utcoffset()

    # And they are literally the same function, so they cannot drift apart again.
    assert batch_now is setl_now is file_now


# ── L2: chunk retries must back off ──────────────────────────────────────────


def test_chunk_retries_back_off_instead_of_hammering(monkeypatch, tmp_path):
    """Each failed chunk retried immediately, so a degraded or rate-limiting CBOS
    endpoint got hit hardest exactly when it was least able to answer."""
    monkeypatch.setenv("CBOS_MODE", "MOCK")
    monkeypatch.setenv("CBOS_RETRY_DELAY_SECONDS", "2")
    monkeypatch.setenv("CBOS_CHUNK_RETRY_ATTEMPTS", "3")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.clients import cbos_client
    from app.clients.cbos_client import CBOSUploadError, MockCBOSClient

    slept: list[float] = []
    monkeypatch.setattr(cbos_client.time, "sleep", lambda s: slept.append(s))

    class _ChunkAlwaysFails(MockCBOSClient):
        def _upload_chunk(self, *a, **k):
            raise CBOSUploadError("simulated chunk failure")

    f = tmp_path / "f.csv"
    f.write_text("a,b\n")
    with pytest.raises(CBOSUploadError):
        _ChunkAlwaysFails().upload_file(f, "127", "guid-1")

    assert slept, "chunk retries did not back off at all"
    assert all(s > 0 for s in slept)
