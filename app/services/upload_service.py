"""Coordinates file discovery, queueing, CBOS upload, database updates, and
file movement.

Unlike the earlier per-file design, one CBOS "batch" is one segment + one
trade date - across every exchange sub-folder (see app/core/queue.py's
SegmentBatchTask). One PROCESSID is reserved per segment/date; exchange is
per-file metadata, not a partition key.
That's required by the documented workflow itself: Holiday Check (Step 1),
Process ID creation (Step 2), and the trigger (Step 8) all happen exactly
ONCE per segment/date - never once per file. Each file discovered under
that one folder is then independently matched to the correct UploadID
(app/services/upload_matching.py) using the settings CBOS returns for every
Table2 candidate (Step 4), instead of blindly uploading everything under
whichever UploadID happened to be listed first.

The scheduler only calls discover_and_enqueue(); the worker only calls
process_batch(). Discovery never touches the database - dedup is
filesystem-only (a file already moved into uploaded/ or uploadFailed/ can't
be rediscovered, see file_service.list_subdirs) plus the in-memory
in-flight guard in app/core/queue.py keyed by segment/date/exchange. The
database is written to for audit purposes only; nothing reads it to make a
processing decision."""

import json
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from app.clients import cbos_client
from app.clients.cbos_client import CBOSUploadError
from app.core.config import settings
from app.core.database import get_sessionmaker
from app.core.queue import SegmentBatchTask, enqueue
from app.repositories.uploaded_file_repository import UploadedFileRepository
from app.services import file_service, upload_matching
from app.services.upload_matching import FileRejected

logger = logging.getLogger("upload_service")


# --------------------------------------------------------------------------
# Discovery: scan the filesystem and enqueue new batches. No CBOS calls, no
# DB reads. Files are grouped by (date, segment, exchange) into ONE batch
# task, since that's the unit CBOS's own workflow operates on.
# --------------------------------------------------------------------------

def discover_and_enqueue() -> None:
    """Walk {FILE_ROOT_PATH}/{date}/{segment}/{exchange}/ for T and the
    configured scan_days_back further, and push every (date, segment,
    exchange) group found as one batch onto the upload queue. Files already
    inside uploaded/ or uploadFailed/ are structurally excluded by
    file_service.list_subdirs - they are never considered "discovered"."""
    root = file_service.get_root()
    dates = file_service.get_processing_dates()
    logger.info("discover_and_enqueue: starting scan of %s for dates=%s", root, dates)
    logger.info("discover_and_enqueue: file type and file size validations are skipped at discovery time - "
                "matching against CBOS's UploadID rules happens per-batch in process_batch")

    for folder_date in dates:
        _discover_date(root, folder_date)
    logger.info("discover_and_enqueue: scan complete")


def _discover_date(root: Path, folder_date: str) -> None:
    logger.info("Processing date: %s", folder_date)

    # Group by SEGMENT only - all exchange sub-folders for a segment become one
    # batch (one PROCESSID per segment/date). Each file keeps its exchange.
    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    files_found = 0
    for file_path, segment, exchange in file_service.discover_files_for_date(root, folder_date):
        logger.info("File discovered = %s (segment=%s, exchange=%s, extension=%s, size=%d bytes)",
                     file_path.name, segment, exchange, file_path.suffix or "(none)", file_path.stat().st_size)
        files_found += 1
        groups[segment].append((str(file_path), exchange))

    for segment, files in groups.items():
        _maybe_enqueue(folder_date, segment, files)

    logger.info("Found %d file(s) across %d segment batch(es) for %s", files_found, len(groups), folder_date)


def _maybe_enqueue(folder_date: str, segment: str, files: list[tuple[str, str]]) -> None:
    task = SegmentBatchTask(folder_date=folder_date, segment=segment, files=files)
    added = enqueue(task)
    if not added:
        logger.debug("Skipping already-queued/in-flight batch %s", task.key)


# --------------------------------------------------------------------------
# Manual upload endpoint support
# --------------------------------------------------------------------------

def save_manual_upload(content: bytes, file_name: str, segment: str, exchange: str) -> Path:
    """Used by POST /upload: save the file to the standard location so the
    scheduler's next scan discovers and enqueues it, like any downloaded file.
    Deliberately writes NO DB row - the batch's create_audit_record owns the
    audit record; writing one here would collide with it on the file_path
    UNIQUE constraint and wedge the file into an endless reprocess loop."""
    logger.info("save_manual_upload: %s (segment=%s, exchange=%s)", file_name, segment, exchange)
    dest_path = file_service.save_uploaded_file(content, file_name, segment, exchange)
    logger.info("save_manual_upload: saved %s, awaiting next scan", dest_path)
    return dest_path


# --------------------------------------------------------------------------
# Centralized success/failure/rejection handlers. Every code path in
# process_batch ends in exactly one of these for a given file.
# --------------------------------------------------------------------------

def handle_upload_success(repo: UploadedFileRepository, record, file_path: Path, response: dict, request_log: list) -> Path:
    """Step 9 confirmed processing finished -> move to uploaded/, record the
    outcome."""
    dest_path = file_service.move_to_uploaded(file_path)
    repo.update(
        record,
        status="uploaded",
        cbos_response=str(response),
        request_log=json.dumps(request_log, default=str),
        uploaded_at=datetime.utcnow(),
        file_path=str(dest_path),
    )
    repo.commit()
    logger.info(
        "handle_upload_success: file=%s upload_id=%s destination=%s",
        file_path.name, record.cbos_upload_id, dest_path,
    )
    return dest_path


def handle_upload_failure(repo: UploadedFileRepository, record, file_path: Path, error: Exception, request_log: list) -> Path:
    """Any CBOS-call failure (Steps 5/7) or a batch-level Step 9 rejection
    -> move to uploadFailed/, record why."""
    dest_path = file_service.move_to_failed(file_path)
    repo.update(
        record,
        status="failed",
        cbos_response=str(error),
        request_log=json.dumps(request_log, default=str),
        retry_count=(record.retry_count or 0) + 1,
        file_path=str(dest_path),
    )
    repo.commit()
    logger.error(
        "handle_upload_failure: file=%s upload_id=%s response=%s destination=%s",
        file_path.name, record.cbos_upload_id, error, dest_path,
    )
    return dest_path


def handle_file_rejected(repo: UploadedFileRepository, record, file_path: Path, error: FileRejected) -> Path:
    """A file was rejected locally (Step 4 checks), before any Step 5/7 CBOS
    call was made for it. The application never fails/crashes over this -
    it's a per-file outcome, same as success/failure.

    Every rejection reason (no UploadID pattern/extension matched, a
    validation failure such as ColumnCountMismatch, etc.) moves the file to
    uploadFailed/ - there is no separate unmatched-file destination."""
    dest_path = file_service.move_to_failed(file_path)

    repo.update(
        record,
        status="failed",
        validation_error=str(error),
        cbos_response=f"Rejected before upload: {error}",
        retry_count=(record.retry_count or 0) + 1,
        file_path=str(dest_path),
    )
    repo.commit()
    logger.warning("handle_file_rejected: file=%s reason=%s destination=%s", file_path.name, error, dest_path)
    return dest_path


# --------------------------------------------------------------------------
# Worker: process one queued segment/date/exchange batch. Steps 2/3/4/8/9
# run once for the whole batch; Steps 5/6/7 run once per matched file.
# --------------------------------------------------------------------------

def handle_upload_unconfirmed(repo: UploadedFileRepository, record, file_path: Path, request_log: list) -> Path:
    """Steps 5+7 succeeded (the file IS in CBOS) but our Step-9 FILEUPLOAD read
    didn't confirm good-to-go. Move to uploaded/ - re-dropping would duplicate -
    and flag confirmation as pending. EDP_Billing is the authoritative poller and
    triggers once CBOS reports TRUE."""
    dest_path = file_service.move_to_uploaded(file_path)
    repo.update(
        record,
        status="uploaded",
        cbos_response="Registered in CBOS; FILEUPLOAD good-to-go not confirmed by uploader",
        request_log=json.dumps(request_log, default=str),
        uploaded_at=datetime.utcnow(),
        file_path=str(dest_path),
    )
    repo.commit()
    logger.warning("handle_upload_unconfirmed: file=%s moved to uploaded/ (FILEUPLOAD not yet TRUE)", file_path.name)
    return dest_path


def _fail_all_files(repo: UploadedFileRepository, files: list, task: SegmentBatchTask, error: Exception) -> None:
    """Batch setup (reserve / fetch-rules) failed before any upload - route every
    file to uploadFailed/ so the batch fails cleanly instead of being rediscovered
    and retried forever."""
    for file_path, exchange in files:
        record = repo.create_audit_record(file_path, task.folder_date, task.segment, exchange)
        handle_upload_failure(repo, record, file_path, error,
                              [{"step": "batch_setup_error", "error": str(error)}])


def process_batch(task: SegmentBatchTask) -> None:
    """Attempt the CBOS upload lane (Steps 2->9) for one segment/date/exchange
    batch. Called by the worker loop for one queue item at a time."""
    files = [(Path(p), exch) for p, exch in task.files if Path(p).exists()]
    if not files:
        logger.warning("Batch %s: no files still exist on disk, skipping", task.key)
        return

    login_id = settings.cbos_login_id
    trade_date = task.folder_date
    logger.info("Processing batch %s: %d file(s)", task.key, len(files))

    session = get_sessionmaker()()
    try:
        repo = UploadedFileRepository(session)

        # Steps 2 + 4: reserve the PROCESSID (shared by every file in the batch)
        # and fetch each UploadID's matching rule, up front. Bounded retry so a
        # transient CBOS blip doesn't hot-loop the batch forever - after
        # cbos_max_retries attempts the files are routed to uploadFailed.
        process_id = table2 = rules = None
        for attempt in range(1, settings.cbos_max_retries + 1):
            try:
                step2_response = cbos_client.get_new_trade_process(task.segment, login_id, trade_date)
                process_id = cbos_client.extract_process_id(step2_response)
                table2 = cbos_client.extract_upload_candidates(step2_response)
                rules = upload_matching.fetch_upload_rules(table2)
                break
            except CBOSUploadError as exc:
                logger.warning("Batch %s: setup attempt %d/%d failed: %s",
                               task.key, attempt, settings.cbos_max_retries, exc)
                if attempt < settings.cbos_max_retries:
                    time.sleep(settings.cbos_retry_delay_seconds)
                else:
                    logger.error("Batch %s: setup exhausted %d attempt(s) - routing files to uploadFailed",
                                 task.key, settings.cbos_max_retries)
                    _fail_all_files(repo, files, task, exc)
                    return
        logger.info("ProcessID = %s (batch=%s, %d UploadID candidate(s))", process_id, task.key, len(table2))

        # Step 3: confirmation check - non-fatal sanity check, never a gate.
        try:
            cbos_client.check_process_id_exist(task.segment, login_id)
        except CBOSUploadError as exc:
            logger.warning("Batch %s: CheckProcessIDExist failed (non-fatal): %s", task.key, exc)

        if not rules:
            logger.error("Batch %s: no usable UploadID rules resolved from Table2 - every file will be rejected", task.key)

        uploaded_candidates: list[tuple] = []  # (record, dest_file_path, request_log)
        filled_upload_ids: set[str] = set()    # UploadIDs that actually received a file (Steps 5+7)

        for file_path, exchange in files:
            record = repo.create_audit_record(file_path, task.folder_date, task.segment, exchange)
            request_log: list = []

            try:
                rule = upload_matching.match_file(file_path, rules, exchange=exchange)
            except FileRejected as exc:
                handle_file_rejected(repo, record, file_path, exc)
                continue

            repo.update(
                record,
                cbos_upload_id=rule.upload_id,
                matched_pattern=rule.name,
                process_id=process_id,
                cbos_upload_settings=json.dumps(rule.raw_settings, default=str),
            )
            repo.commit()

            # Idempotency: if this exact file already reached CBOS for this
            # segment/date/UploadID (e.g. a re-drop after a Step-9 timeout), it's
            # already there - skip the re-upload, just move it out of the source.
            if repo.find_completed(task.segment, task.folder_date, rule.upload_id, file_path.name):
                logger.info("Idempotent skip: %s already uploaded (segment=%s date=%s UploadID=%s)",
                            file_path.name, task.segment, task.folder_date, rule.upload_id)
                request_log.append({"step": "idempotent_skip"})
                dest_path = file_service.move_to_uploaded(file_path)
                repo.update(record, status="uploaded",
                            cbos_response="Skipped - already uploaded (idempotent)",
                            request_log=json.dumps(request_log, default=str),
                            file_path=str(dest_path))
                repo.commit()
                filled_upload_ids.add(str(rule.upload_id))
                continue

            try:
                # Step 5: upload the file under its correctly-matched UploadID.
                logger.info("Upload started = %s (UploadID=%s)", file_path.name, rule.upload_id)
                guid = str(uuid.uuid4())
                cbos_client.upload_file_chunks(file_path, rule.upload_id, guid)
                request_log.append({"step": "SaveTradePromodalUploadChunkFile", "upload_id": rule.upload_id, "guid": guid})
                repo.update(record, guid=guid)
                repo.commit()
                logger.info("GUID received = %s (%s)", guid, file_path.name)

                # Step 7: register the uploaded file (Step 6's existing-process
                # lookup is a non-critical confirmation call, done once per
                # batch below rather than once per file).
                step7_response = cbos_client.save_trade_process_upload_file(
                    rule.upload_id, guid, file_path.name, login_id, process_id, trade_date
                )
                request_log.append({"step": "SaveNewTradeProcessPromodalUploadFile", "response": step7_response})
                logger.info("CBOS entry created = %s (UploadID=%s, GUID=%s)", file_path.name, rule.upload_id, guid)

                uploaded_candidates.append((record, file_path, request_log))
                filled_upload_ids.add(str(rule.upload_id))
            except Exception as exc:
                logger.error("Batch %s: upload sequence failed for %s: %s", task.key, file_path.name, exc)
                request_log.append({"step": "error", "error": str(exc)})
                handle_upload_failure(repo, record, file_path, exc, request_log)

        if not uploaded_candidates:
            logger.info("Batch %s: no files uploaded, skipping mark-optional/poll", task.key)
            return

        # Step 6: existing-process confirmation lookup, once per batch. Non-fatal -
        # purely diagnostic (it also confirms EDP_Billing will be able to find our
        # PROCESSID via getdropdown).
        try:
            cbos_client.get_existing_process_id(task.segment, login_id, trade_date)
        except CBOSUploadError as exc:
            logger.warning("Batch %s: getdropdown(EXISTINGPROCESSID) failed (non-fatal): %s", task.key, exc)

        # Step 8: mark every non-zero Table2 slot that received NO file optional,
        # so CBOS's FILEUPLOAD good-to-go isn't held waiting on a file that doesn't
        # exist today. Non-fatal per slot. NOTE: the trigger (Step 10) and every
        # downstream step are owned by the EDP_Billing scheduler, not this repo -
        # our job ends at "make FILEUPLOAD go TRUE". See docs/CBOS_HANDOFF_CONTRACT.md.
        empty_slots = [
            row for row in table2
            if str(row.get("UPLOADID", "0")) not in ("0", "")
            and str(row.get("UPLOADID")) not in filled_upload_ids
        ]
        for row in empty_slots:
            stepno = row.get("STEPNO")
            try:
                cbos_client.mark_step_optional(process_id, stepno)
            except CBOSUploadError as exc:
                logger.warning("Batch %s: mark-optional STEPNO=%s failed (non-fatal): %s", task.key, stepno, exc)

        # Step 9: our own confirmation that the files landed (EDP_Billing is the
        # authoritative FILEUPLOAD poller + the one that triggers). Per segment.
        succeeded = cbos_client.poll_file_upload_status(task.segment, login_id)
        for record, file_path, request_log in uploaded_candidates:
            request_log.append({"step": "file_process_status", "result": succeeded})
            if succeeded:
                handle_upload_success(repo, record, file_path, {"MSG": "TRUE"}, request_log)
            else:
                # Files ARE in CBOS (Steps 5+7 done); FILEUPLOAD just isn't TRUE
                # yet. Routing to uploadFailed would invite a duplicate re-upload.
                handle_upload_unconfirmed(repo, record, file_path, request_log)

        logger.info(
            "Batch %s complete: %d file(s) in CBOS (%s)",
            task.key, len(uploaded_candidates),
            "FILEUPLOAD confirmed" if succeeded else "FILEUPLOAD not yet confirmed",
        )

    finally:
        session.close()
        logger.debug("process_batch: session closed for %s", task.key)
