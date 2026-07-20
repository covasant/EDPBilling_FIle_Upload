"""Regression tests for B1 (manual-upload UNIQUE loop) and H4/H5 (retry &
idempotency) - all against the MOCK client, no network."""

from pathlib import Path

import pytest

from app.clients import cbos_client
from app.clients.cbos_client import CBOSUploadError, MockCBOSClient


def _fast(monkeypatch):
    monkeypatch.setenv("CBOS_MOCK_RANDOM_SUCCESS_RATE", "1.0")
    monkeypatch.setenv("CBOS_MOCK_PENDING_POLLS", "0")
    monkeypatch.setenv("CBOS_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("CBOS_RETRY_DELAY_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()


def _write(folder: Path, name: str, cols: int = 46) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(",".join(str(i) for i in range(cols)) + "\n")
    return p


def _root():
    from app.core.config import settings

    return Path(settings.file_root_path)


def _batch(date="17-07-2026", segment="MCX", exchange="NA", files=None):
    from app.core.queue import SegmentBatchTask

    return SegmentBatchTask(folder_date=date, segment=segment, exchange=exchange, file_paths=files or [])


# --- B1 -----------------------------------------------------------------------

def test_create_audit_record_is_idempotent():
    """Calling create_audit_record twice for the same source path reuses the row
    instead of hitting the file_path UNIQUE constraint (the old infinite-loop)."""
    from app.core import database
    from app.repositories.uploaded_file_repository import UploadedFileRepository

    database.init_db()
    session = database.get_sessionmaker()()
    try:
        repo = UploadedFileRepository(session)
        r1 = repo.create_audit_record("/x/y/f.csv", "17-07-2026", "MCX", "NA")
        repo.commit()
        r2 = repo.create_audit_record("/x/y/f.csv", "17-07-2026", "MCX", "NA")  # would IntegrityError before
        repo.commit()
        assert r1.id == r2.id
    finally:
        session.close()


def test_manual_upload_then_process_does_not_collide(monkeypatch):
    """POST /upload writes no DB row, so the batch's create_audit_record owns it
    and the file uploads cleanly (the B1 scenario, end to end)."""
    _fast(monkeypatch)
    from app.core import database
    from app.models.uploaded_file import UploadedFile
    from app.services import upload_service

    database.init_db()
    content = (",".join(str(i) for i in range(68)) + "\n").encode()  # UploadID 127 expects 68 cols
    dest = upload_service.save_manual_upload(content, "MCX_ProductMaster.csv", "MCX", "NA")
    assert dest.exists()

    upload_service.process_batch(_batch(date=dest.parent.parent.parent.name, files=[str(dest)]))

    session = database.get_sessionmaker()()
    try:
        rows = session.query(UploadedFile).all()
        assert len(rows) == 1 and rows[0].status == "uploaded"
    finally:
        session.close()


# --- H4: transient setup failure must not hot-loop -----------------------------

class _ReserveFails(MockCBOSClient):
    def get_new_trade_process(self, segment, login_id, trade_date):
        raise CBOSUploadError("simulated transient CBOS blip")


def test_setup_failure_routes_to_failed_not_loop(monkeypatch):
    _fast(monkeypatch)
    from app.core import database
    from app.models.uploaded_file import UploadedFile
    from app.services import upload_service

    database.init_db()
    cbos_client.set_cbos_client(_ReserveFails())

    folder = _root() / "17-07-2026" / "MCX" / "NA"
    f = _write(folder, "Position_MCXCCL_CO_0_CM_55930_20260717_F_0000.csv")

    upload_service.process_batch(_batch(files=[str(f)]))  # must NOT raise

    assert not f.exists(), "file should have been moved out of source (no rediscovery loop)"
    assert (folder / "uploadFailed" / f.name).exists()
    session = database.get_sessionmaker()()
    try:
        rows = session.query(UploadedFile).all()
        assert len(rows) == 1 and rows[0].status == "failed"
    finally:
        session.close()


# --- H5: FILEUPLOAD FALSE after upload must not route to uploadFailed ----------

class _GtgFalse(MockCBOSClient):
    def file_upload_status(self, segment, login_id):
        return {"Status": "Success", "Data": [{"MSG": "FALSE"}]}


def test_unconfirmed_upload_goes_to_uploaded_not_failed(monkeypatch):
    _fast(monkeypatch)
    from app.core import database
    from app.models.uploaded_file import UploadedFile
    from app.services import upload_service

    database.init_db()
    cbos_client.set_cbos_client(_GtgFalse())

    folder = _root() / "17-07-2026" / "MCX" / "NA"
    f = _write(folder, "Position_MCXCCL_CO_0_CM_55930_20260717_F_0000.csv")

    upload_service.process_batch(_batch(files=[str(f)]))

    # File is in CBOS (Steps 5+7 done) - it must land in uploaded/, not uploadFailed/.
    assert (folder / "uploaded" / f.name).exists()
    assert not (folder / "uploadFailed" / f.name).exists()
    session = database.get_sessionmaker()()
    try:
        row = session.query(UploadedFile).one()
        assert row.status == "uploaded"
        assert "not confirmed" in (row.cbos_response or "").lower()
    finally:
        session.close()


# --- idempotency: a re-dropped, already-uploaded file is not sent twice --------

def test_idempotent_reupload_skips(monkeypatch):
    _fast(monkeypatch)
    from app.core import database
    from app.services import upload_service

    database.init_db()
    client = cbos_client.get_cbos_client()

    folder = _root() / "17-07-2026" / "MCX" / "NA"
    name = "Position_MCXCCL_CO_0_CM_55930_20260717_F_0000.csv"

    f1 = _write(folder, name)
    upload_service.process_batch(_batch(files=[str(f1)]))
    assert len(client.upload_calls) == 1  # uploaded once

    # Re-drop the same file (same segment/date/UploadID/name) and reprocess.
    f2 = _write(folder, name)
    upload_service.process_batch(_batch(files=[str(f2)]))

    assert len(client.upload_calls) == 1, "already-uploaded file must not be re-uploaded"
    assert (folder / "uploaded" / name).exists()
