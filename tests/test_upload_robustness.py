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

    return SegmentBatchTask(folder_date=date, segment=segment,
                            files=[(p, exchange) for p in (files or [])])


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
    def _get_new_trade_process(self, segment, trade_date):
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
    def _file_upload_status(self, segment):
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


# --- GUID is persisted BEFORE the chunks go out --------------------------------

class _ChunkDiesMidFile(MockCBOSClient):
    """Fails partway through Step 5, exactly like a link drop mid-file."""

    def _upload_chunk(self, upload_id, guid, file_name, chunk_bytes, current_chunk, total_chunks):
        raise CBOSUploadError("simulated link drop mid-chunk")


def test_guid_persisted_even_when_chunk_upload_fails(monkeypatch):
    """A failed Step 5 leaves inert chunks in a CBOS drop folder. We must have
    written that folder's GUID down BEFORE uploading, or it is unfindable.
    Persisting the GUID only after a successful upload loses it exactly when
    it matters."""
    _fast(monkeypatch)
    from app.core import database
    from app.models.uploaded_file import UploadedFile
    from app.services import upload_service

    database.init_db()
    cbos_client.set_cbos_client(_ChunkDiesMidFile())

    folder = _root() / "17-07-2026" / "MCX" / "NA"
    f = _write(folder, "Position_MCXCCL_CO_0_CM_55930_20260717_F_0000.csv")

    upload_service.process_batch(_batch(files=[str(f)]))

    session = database.get_sessionmaker()()
    try:
        row = session.query(UploadedFile).one()
        assert row.status == "failed"
        assert row.guid, "GUID of the abandoned CBOS drop folder must be recorded"
        assert (folder / "uploadFailed" / f.name).exists()
    finally:
        session.close()


# --- the downloader omits the exchange level for segments that lack one -------

def test_discovers_files_with_no_exchange_folder(monkeypatch):
    """The RPA bot writes {date}/MCX/file.csv (no exchange level) but
    {date}/EQ/BSE/file.csv (with one). Requiring the exchange folder made every
    MCX file invisible to discovery - silently unbilled, no error anywhere."""
    _fast(monkeypatch)
    from app.services import file_service

    root = _root()
    loose = _write(root / "17-07-2026" / "MCX", "Position_MCXCCL_CO_0_CM_55930_20260717_F_0000.csv")
    nested = _write(root / "17-07-2026" / "EQ" / "BSE", "Trade_BSE_CM_0_TM_446_20260717_F_0000.csv")

    found = {p.name: (seg, exc) for p, seg, exc in
             file_service.discover_files_for_date(root, "17-07-2026")}

    assert found[loose.name] == ("MCX", "NA"), "segment-level file must be found"
    assert found[nested.name] == ("EQ", "BSE"), "exchange-level file keeps its exchange"


def test_no_exchange_file_uploads_and_moves_beside_itself(monkeypatch):
    """A segment-level file goes through the full lane and lands in
    MCX/uploaded/ - which list_subdirs must not then mistake for an exchange."""
    _fast(monkeypatch)
    from app.core import database
    from app.models.uploaded_file import UploadedFile
    from app.services import file_service, upload_service

    database.init_db()
    segment_folder = _root() / "17-07-2026" / "MCX"
    f = _write(segment_folder, "Position_MCXCCL_CO_0_CM_55930_20260717_F_0000.csv")

    upload_service.process_batch(_batch(exchange="NA", files=[str(f)]))

    assert (segment_folder / "uploaded" / f.name).exists()
    session = database.get_sessionmaker()()
    try:
        assert session.query(UploadedFile).one().status == "uploaded"
    finally:
        session.close()

    # The uploaded/ folder must not now look like an exchange holding a source file.
    assert list(file_service.discover_files_for_date(_root(), "17-07-2026")) == []


# --- idempotency: a re-dropped, already-uploaded file is not sent twice --------

def test_multi_exchange_segment_reserves_one_pid(monkeypatch):
    """H1: EQ files from BSE + NSE folders are ONE batch -> exactly one PROCESSID,
    both exchanges' files under it. Slicing by exchange would reserve two."""
    _fast(monkeypatch)
    from app.core import database
    from app.core.queue import SegmentBatchTask
    from app.models.uploaded_file import UploadedFile
    from app.services import upload_service

    database.init_db()
    client = cbos_client.get_cbos_client()

    root = _root()
    bse = _write(root / "17-07-2026" / "EQ" / "BSE", "Trade_BSE_CM_0_TM_446_20260717_F_0000.csv")
    nse = _write(root / "17-07-2026" / "EQ" / "NSE", "Trade_NSE_CM_0_TM_10412_20260717_F_0000.csv")

    task = SegmentBatchTask(folder_date="17-07-2026", segment="EQ",
                            files=[(str(bse), "BSE"), (str(nse), "NSE")])
    assert task.key == "17-07-2026|EQ"  # exchange is NOT in the batch key
    upload_service.process_batch(task)

    assert client.reserve_calls == 1, "one PROCESSID per segment/date, not per exchange"
    session = database.get_sessionmaker()()
    try:
        rows = session.query(UploadedFile).all()
        assert len(rows) == 2
        assert {r.status for r in rows} == {"uploaded"}
        assert len({r.process_id for r in rows}) == 1          # both under the SAME pid
        assert {r.exchange for r in rows} == {"BSE", "NSE"}     # per-file exchange preserved
        assert {r.cbos_upload_id for r in rows} == {"545", "546"}
    finally:
        session.close()


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


def test_retry_after_operator_moves_file_back_out_of_uploaded(monkeypatch):
    """The operator retry loop: a batch completes, the file lands in uploaded/,
    then a human moves it back to the source folder to run it again.

    test_idempotent_reupload_skips re-drops a COPY, leaving the pass-1 file in
    uploaded/ - so _move_file finds the destination occupied and renames to
    <stem>_2, and the DB is never asked to write a duplicate path. Moving the
    file back instead empties the destination, _move_file uses the plain name,
    and it collides with the pass-1 row that still owns that exact file_path.
    The filesystem is clean; only the UNIQUE constraint notices.

    This is what broke on the VDI: every retry died on
    "UNIQUE constraint failed: uploaded_files.file_path" at the commit right
    after matching, so no file ever reached Step 5 or Step 7 and FILEUPLOAD
    stayed FALSE.
    """
    _fast(monkeypatch)
    from app.core import database
    from app.services import upload_service

    database.init_db()

    folder = _root() / "18-07-2026" / "MCX" / "NA"
    name = "Trade_MCX_CO_0_CM_55930_20260718_F_0000.csv"

    src = _write(folder, name)
    upload_service.process_batch(_batch(date="18-07-2026", files=[str(src)]))

    landed = folder / "uploaded" / name
    assert landed.exists(), "pass 1 must leave the file in uploaded/"

    # The operator MOVES it back - the destination is now empty, so _move_file
    # has no filename collision to protect us with.
    landed.rename(src)
    assert not landed.exists()

    upload_service.process_batch(_batch(date="18-07-2026", files=[str(src)]))

    assert not src.exists(), "pass 2 must move the file out of the source folder"


def test_processid_mismatch_is_logged_loudly(caplog):
    """Step 3 names the PROCESSID CBOS's good-to-go side tracks for a segment.
    If it isn't ours, Step 9 will describe a different process and FILEUPLOAD
    can never confirm our files - so the mismatch must be impossible to miss.

    On 2026-07-21 this call answered 17741 while the batch filled 17747. Its
    reply was discarded, so the run looked healthy right up to a poll that could
    never succeed.
    """
    import logging

    from app.core.queue import SegmentBatchTask
    from app.services.upload_service import _warn_if_process_id_differs

    task = SegmentBatchTask(folder_date="20-07-2026", segment="MCX", files=[])

    with caplog.at_level(logging.ERROR):
        _warn_if_process_id_differs("PROCESS ID ALREADY GENERATED : 17741", "17747", task)
    assert "PROCESSID MISMATCH" in caplog.text
    assert "17741" in caplog.text and "17747" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.ERROR):
        _warn_if_process_id_differs("PROCESS ID ALREADY GENERATED : 17747", "17747", task)
    assert caplog.text == "", "a matching PROCESSID must stay quiet"

    caplog.clear()
    with caplog.at_level(logging.ERROR):
        _warn_if_process_id_differs("PROCESS ID CREATED", "17747", task)
    assert caplog.text == "", "an unrecognised phrasing is not evidence of a mismatch"
