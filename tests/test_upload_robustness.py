"""Regression tests for B1 (manual-upload UNIQUE loop) and H4/H5 (retry &
idempotency) - all against the MOCK client, no network."""

import json
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

    return SegmentBatchTask(
        folder_date=date, segment=segment, files=[(p, exchange) for p in (files or [])]
    )


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
        r2 = repo.create_audit_record(
            "/x/y/f.csv", "17-07-2026", "MCX", "NA"
        )  # would IntegrityError before
        repo.commit()
        assert r1.id == r2.id
    finally:
        session.close()


# --- H4: transient setup failure must not hot-loop -----------------------------


class _ReserveFails(MockCBOSClient):
    def _get_new_trade_process(self, segment, trade_date, process_id="0"):
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
    def _file_upload_status(self, segment, trade_date):
        return {"Status": "Success", "Data": [{"MSG": "FALSE"}]}


def test_unconfirmed_upload_goes_to_uploaded_not_failed(monkeypatch):
    _fast(monkeypatch)
    from app.core import database
    from app.models.uploaded_file import UploadedFile
    from app.services import upload_service

    database.init_db()
    cbos_client.set_cbos_client(_GtgFalse())

    # The FULL mandatory MCX set (127/534/535; 320 is allowlisted-optional), so
    # the completeness gate passes and Step 9's FALSE is what decides.
    folder = _root() / "17-07-2026" / "MCX" / "NA"
    files = [
        _write(folder, "MCX_ProductMaster.csv", cols=68),
        _write(folder, "Position_MCXCCL_CO_0_CM_55930_20260717_F_0000.csv"),
        _write(folder, "Trade_MCX_CO_0_CM_55930_20260717_F_0000.csv"),
    ]

    upload_service.process_batch(_batch(files=[str(f) for f in files]))

    session = database.get_sessionmaker()()
    try:
        rows = session.query(UploadedFile).all()
        assert len(rows) == 3
        for row in rows:
            # Files are in CBOS (Steps 5+7 done) - uploaded/, not uploadFailed/.
            assert (folder / "uploaded" / row.file_name).exists()
            assert not (folder / "uploadFailed" / row.file_name).exists()
            assert row.status == "uploaded"
            # A FALSE Step 9 read does NOT make the file unconfirmed: it is in
            # CBOS. FILEUPLOAD cannot be TRUE here anyway (the engine triggers
            # after we return), so the reading is kept as context only.
            response = (row.cbos_response or "").lower()
            assert "uploaded and registered in cbos" in response
            assert "false" in response
    finally:
        session.close()


# --- Step 9 poll FAILING must not strand a fully-uploaded batch ---------------


class _ConfirmDies(MockCBOSClient):
    """Step 9 blows up, exactly like a transient link drop on the final call."""

    def confirm_upload(self, segment, trade_date):
        raise CBOSUploadError("file-process-status failed: 503 upstream unavailable")


def test_a_failed_step_9_poll_still_records_and_moves_the_files(monkeypatch):
    """The most-complete batch possible must not be the one that gets stranded.

    By Step 9 every file is chunked, registered and past the completeness gate. The poll
    is diagnostics — from_poll_result() confirms on our own work whatever it reads — so
    its failure decides nothing. Unguarded it decided everything: the raise unwound past
    the outcome-recording loop into the worker's blanket `except Exception`, leaving the
    files in the intake tree, every row at 'pending', and the batch at UPLOADING with no
    way back but manual DB surgery."""
    _fast(monkeypatch)
    from app.core import database
    from app.models.uploaded_file import UploadedFile
    from app.services import upload_service

    database.init_db()
    cbos_client.set_cbos_client(_ConfirmDies())

    folder = _root() / "21-07-2026" / "MCX" / "NA"
    files = [
        _write(folder, "MCX_ProductMaster.csv", cols=68),
        _write(folder, "Position_MCXCCL_CO_0_CM_55930_20260721_F_0000.csv"),
        _write(folder, "Trade_MCX_CO_0_CM_55930_20260721_F_0000.csv"),
    ]

    # Must NOT raise — that is the whole regression.
    upload_service.process_batch(_batch(date="21-07-2026", files=[str(f) for f in files]))

    session = database.get_sessionmaker()()
    try:
        rows = session.query(UploadedFile).all()
        assert len(rows) == 3
        for row in rows:
            assert row.status == "uploaded", "a failed poll must not leave rows at 'pending'"
            assert (folder / "uploaded" / row.file_name).exists(), "file never left the intake tree"
            assert not (folder / "uploadFailed" / row.file_name).exists()
    finally:
        session.close()


def test_the_failed_poll_is_recorded_without_the_upstream_body(monkeypatch):
    """poll_message lands in batch status_detail and in every file's request_log, and
    CBOSUploadError carries the raw upstream response — so the marker goes in and the
    detail stays in the log. Widening the blast radius of an unredacted upstream body
    is the open finding H3; this fix must not feed it."""
    _fast(monkeypatch)
    from app.core import database
    from app.models.uploaded_file import UploadedFile
    from app.services import upload_service

    database.init_db()
    cbos_client.set_cbos_client(_ConfirmDies())

    folder = _root() / "22-07-2026" / "MCX" / "NA"
    files = [
        _write(folder, "MCX_ProductMaster.csv", cols=68),
        _write(folder, "Position_MCXCCL_CO_0_CM_55930_20260722_F_0000.csv"),
        _write(folder, "Trade_MCX_CO_0_CM_55930_20260722_F_0000.csv"),
    ]
    upload_service.process_batch(_batch(date="22-07-2026", files=[str(f) for f in files]))

    session = database.get_sessionmaker()()
    try:
        logs = " ".join((r.request_log or "") for r in session.query(UploadedFile).all())
        assert "Step 9 poll failed" in logs, "the failure must be visible in the audit trail"
        assert "upstream unavailable" not in logs, "the upstream body must not be persisted"
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


def test_no_exchange_file_uploads_and_moves_beside_itself(monkeypatch):
    """A segment-level file goes through the full lane and lands in
    MCX/uploaded/ - which list_subdirs must not then mistake for an exchange."""
    _fast(monkeypatch)
    from app.core import database
    from app.models.uploaded_file import UploadedFile
    from app.services import upload_service

    database.init_db()
    segment_folder = _root() / "17-07-2026" / "MCX"
    files = [
        _write(segment_folder, "MCX_ProductMaster.csv", cols=68),
        _write(segment_folder, "Position_MCXCCL_CO_0_CM_55930_20260717_F_0000.csv"),
        _write(segment_folder, "Trade_MCX_CO_0_CM_55930_20260717_F_0000.csv"),
    ]

    upload_service.process_batch(_batch(exchange="NA", files=[str(f) for f in files]))

    session = database.get_sessionmaker()()
    try:
        for row in session.query(UploadedFile).all():
            assert (segment_folder / "uploaded" / row.file_name).exists()
            assert row.status == "uploaded"
    finally:
        session.close()


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

    task = SegmentBatchTask(
        folder_date="17-07-2026", segment="EQ", files=[(str(bse), "BSE"), (str(nse), "NSE")]
    )
    assert task.key == "17-07-2026|EQ|upload|scan"  # exchange is NOT in the batch key
    upload_service.process_batch(task)

    assert client.reserve_calls == 1, "one PROCESSID per segment/date, not per exchange"
    session = database.get_sessionmaker()()
    try:
        rows = session.query(UploadedFile).all()
        assert len(rows) == 2
        assert {r.status for r in rows} == {"uploaded"}
        assert len({r.process_id for r in rows}) == 1  # both under the SAME pid
        assert {r.exchange for r in rows} == {"BSE", "NSE"}  # per-file exchange preserved
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


def test_holiday_skips_the_batch_without_reserving_a_processid(monkeypatch):
    """Step 1 gates the batch. On a holiday nothing is reserved, nothing is
    uploaded, and the files stay put for the next scan.

    Reserving first would be the expensive mistake: Step 2 mints a NEW
    PROCESSID on every attempt, so a scheduler ticking every 30 seconds through
    a holiday would leave CBOS a trail of empty processes for a day that should
    have produced none.
    """
    _fast(monkeypatch)
    monkeypatch.setenv("CBOS_MOCK_HOLIDAY", "true")
    monkeypatch.setenv("CBOS_HOLIDAY_CHECK_ENFORCED", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.core import database
    from app.services import upload_service

    database.init_db()
    client = cbos_client.get_cbos_client()

    folder = _root() / "19-07-2026" / "MCX" / "NA"
    name = "Position_MCXCCL_CO_0_CM_55930_20260719_F_0000.csv"
    src = _write(folder, name)

    upload_service.process_batch(_batch(date="19-07-2026", files=[str(src)]))

    assert client.upload_calls == [], "no file may be uploaded on a holiday"
    assert src.exists(), (
        "the file must be left where it is, not moved to uploaded/ or uploadFailed/"
    )
    assert not (folder / "uploaded").exists()
    assert not (folder / "uploadFailed").exists()


def test_skip_means_proceed_not_skip(monkeypatch):
    """The inversion, pinned: CBOS answers SKIP to mean "carry on". Reading it
    as "skip this batch" would stop the uploader on every working day - a
    failure that looks exactly like normal quiet operation."""
    _fast(monkeypatch)
    monkeypatch.setenv("CBOS_MOCK_HOLIDAY", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()

    client = cbos_client.get_cbos_client()
    assert client.may_begin_upload("MCX", "14-07-2026") is True


def test_holiday_check_is_observe_only_by_default(monkeypatch):
    """A holiday answer must NOT stop a batch unless enforcement is switched on.

    "Any message except SKIP means holiday" comes from one line of the API doc
    and no real BeginFileUpload reply has ever been seen. If CBOS words its
    working-day answer differently, enforcing by default would halt every
    upload - silently, and looking exactly like a day with no files to process.
    So the default reports and carries on.
    """
    _fast(monkeypatch)
    monkeypatch.setenv("CBOS_MOCK_HOLIDAY", "true")  # CBOS says "holiday"
    monkeypatch.setenv("CBOS_HOLIDAY_CHECK_ENFORCED", "false")  # but we only observe
    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.core import database
    from app.services import upload_service

    database.init_db()
    client = cbos_client.get_cbos_client()

    folder = _root() / "16-07-2026" / "MCX" / "NA"
    name = "Position_MCXCCL_CO_0_CM_55930_20260716_F_0000.csv"
    src = _write(folder, name)

    upload_service.process_batch(_batch(date="16-07-2026", files=[str(src)]))

    assert len(client.upload_calls) == 1, "observe-only must not block the upload"
    assert (folder / "uploaded" / name).exists()


# --- Bad inputs must arrive as CBOSUploadError, not as a bare ValueError -------


def test_a_malformed_trade_date_is_a_cbos_error_not_a_bare_value_error(monkeypatch):
    """_to_cbos_date runs inside Steps 1/2/3/7/9, and upload_service's retry loop
    catches CBOSUploadError only. A bare ValueError from strptime sailed straight past
    it and out of process_batch, so a malformed folder date skipped the intended
    "exhaust retries -> move to uploadFailed/" path entirely."""
    _fast(monkeypatch)
    from app.clients.cbos_client import _to_cbos_date

    with pytest.raises(CBOSUploadError) as excinfo:
        _to_cbos_date("2026-07-17")  # ISO, but date_folder_format is dd-mm-yyyy

    assert "date_folder_format" in str(excinfo.value)


def test_a_slot_with_no_stepno_is_refused_rather_than_sent_as_the_string_none(monkeypatch):
    """STEPNO is stringified for the payload, so a slot that came back without one was
    sent as the literal "None". Best case CBOS rejects it; worst case it coerces to a
    step number and marks the WRONG step optional — silent, and nothing unwinds it."""
    _fast(monkeypatch)
    client = MockCBOSClient()

    with pytest.raises(CBOSUploadError) as excinfo:
        client.mark_step_optional("17649", None)

    assert "STEPNO" in str(excinfo.value)


def test_the_settlement_poll_does_not_sleep_after_its_final_attempt(monkeypatch):
    """The loop slept unconditionally, including after the last poll — so every timeout
    paid one extra interval before telling the caller POLL_TIMED_OUT. The settlement
    endpoint is called synchronously by the orchestrator, so that sat on its critical
    path for nothing."""
    monkeypatch.setenv("CBOS_SETL_MODE", "MOCK")
    monkeypatch.setenv("CBOS_SETL_POLL_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("CBOS_SETL_POLL_INTERVAL_SECONDS", "7")
    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.clients import dp_upload_client

    sleeps: list[float] = []
    monkeypatch.setattr(dp_upload_client.time, "sleep", lambda s: sleeps.append(s))

    class _AlwaysPending(dp_upload_client.MockDPUploadClient):
        def _get_upload_status(self, tran_id):
            # "Result" is the envelope key check_status_once reads.
            return {
                "Result": [{"Status": dp_upload_client.STATUS_IN_PROCESS, "Description": "busy"}]
            }

    status, _ = _AlwaysPending().poll_status("123")

    assert status == dp_upload_client.POLL_TIMED_OUT
    assert len(sleeps) == 2, f"3 attempts should sleep twice, not {len(sleeps)} times"


def test_a_non_numeric_tran_id_is_a_dp_error_not_a_bare_value_error(monkeypatch):
    """Same shape as the trade-date fix, on the settlement side: settlement_service's
    retry loop catches DPUploadError only, so int() raising ValueError left the
    settlement_uploads row stuck at 'pending' with no error detail."""
    # The real adapter's __init__ refuses to build without these, and that refusal is
    # itself a DPUploadError — set them, or the assertion below passes on the
    # constructor's error and proves nothing about the cast.
    monkeypatch.setenv("CBOS_SETL_MODE", "REAL")
    monkeypatch.setenv("CBOS_SETL_BASE_URL", "http://dp.invalid")
    monkeypatch.setenv("CBOS_SETL_SESKEY", "k")
    monkeypatch.setenv("CBOS_SETL_USER_ID", "u")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.clients.dp_upload_client import DPUploadClient, DPUploadError, _as_int

    # The helper itself.
    with pytest.raises(DPUploadError):
        _as_int("not-a-number", "Tran_Id")

    # And that the call site actually goes through it. The payload is built before
    # _post runs, so this raises on the cast without any network call.
    with pytest.raises(DPUploadError) as excinfo:
        DPUploadClient()._get_upload_status("not-a-number")
    assert "Tran_Id" in str(excinfo.value), "raised, but not from the id cast"


# --- M11: a crashed batch must be visible as FAILED, not left mid-flight ------


def test_a_crashed_batch_is_recorded_as_failed_not_left_uploading(monkeypatch, tmp_path):
    """The worker's blanket `except Exception` keeps the WORKER alive, which is right.
    But it left the BATCH at whatever status it held when the exception fired — normally
    UPLOADING — so through GET /batches/{id} a dead batch was indistinguishable from one
    still running, and only the worker log said otherwise."""
    _fast(monkeypatch)
    from edpb_core.batch_api import BatchStatus

    from app.core import database
    from app.core.queue import BatchQueue
    from app.models.batch import Batch
    from app.repositories.batch_repository import BatchRepository
    from app.services import upload_service
    from app.workers import upload_worker

    database.init_db()

    task = _batch(date="23-07-2026", files=[])
    task.batch_id = "MCX-2026-07-23-deadbeef"
    session = database.get_sessionmaker()()
    try:
        BatchRepository(session).create(
            batch_id=task.batch_id,
            segment="MCX",
            trade_date="2026-07-23",
            folder_date="23-07-2026",
            manifest_path="/nowhere/manifest.json",
            correlation_id="c-1",
        )
        session.commit()
    finally:
        session.close()

    def boom(_task):
        raise RuntimeError("simulated crash deep in process_batch")

    monkeypatch.setattr(upload_service, "process_batch", boom)

    # Drive ONE worker iteration, then break out of its infinite loop.
    queue = BatchQueue()
    queue.enqueue(task)
    original_get = queue.get
    calls = {"n": 0}

    def get_once():
        calls["n"] += 1
        if calls["n"] > 1:
            raise KeyboardInterrupt  # unwinds run()'s while True
        return original_get()

    monkeypatch.setattr(queue, "get", get_once)
    with pytest.raises(KeyboardInterrupt):
        upload_worker.run(queue)

    session = database.get_sessionmaker()()
    try:
        row = session.query(Batch).filter_by(batch_id=task.batch_id).one()
        assert row.status == BatchStatus.FAILED, f"crashed batch left at {row.status!r}"
        assert "simulated crash" in json.dumps(row.status_detail or {})
    finally:
        session.close()
