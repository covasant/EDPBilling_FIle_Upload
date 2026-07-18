"""Coordinates file discovery, queueing, CBOS upload, database updates, and
file movement.

Unlike the earlier per-file design, one CBOS "batch" is one segment + one
trade date + one exchange folder (see app/core/queue.py's SegmentBatchTask).
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
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from app.clients import cbos_client
from app.clients.cbos_client import CBOSUploadError
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.queue import SegmentBatchTask, enqueue
from app.repositories.uploaded_file_repository import UploadedFileRepository
from app.services import file_service, upload_matching
from app.services.upload_matching import FileRejected

logger = logging.getLogger("upload_service")
settings = get_settings()


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

    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    files_found = 0
    for file_path, segment, exchange in file_service.discover_files_for_date(root, folder_date):
        logger.info("File discovered = %s (segment=%s, exchange=%s, extension=%s, size=%d bytes)",
                     file_path.name, segment, exchange, file_path.suffix or "(none)", file_path.stat().st_size)
        files_found += 1
        groups[(segment, exchange)].append(str(file_path))

    for (segment, exchange), file_paths in groups.items():
        _maybe_enqueue(folder_date, segment, exchange, file_paths)

    logger.info("Found %d file(s) across %d segment/exchange batch(es) for %s", files_found, len(groups), folder_date)


def _maybe_enqueue(folder_date: str, segment: str, exchange: str, file_paths: list[str]) -> None:
    task = SegmentBatchTask(folder_date=folder_date, segment=segment, exchange=exchange, file_paths=file_paths)
    added = enqueue(task)
    if not added:
        logger.debug("Skipping already-queued/in-flight batch %s", task.key)


# --------------------------------------------------------------------------
# Manual upload endpoint support
# --------------------------------------------------------------------------

def save_manual_upload(content: bytes, file_name: str, segment: str, exchange: str, repo: UploadedFileRepository):
    """Used by POST /upload: save the file to the standard location and mark
    it pending. Never talks to CBOS directly - the scheduler's next pass
    discovers and enqueues it as part of that segment/date/exchange batch,
    like any other file."""
    logger.info("save_manual_upload: %s (segment=%s, exchange=%s)", file_name, segment, exchange)
    dest_path = file_service.save_uploaded_file(content, file_name, segment, exchange)
    record = repo.create_pending_record(
        dest_path,
        folder_date=file_service.get_today_folder_name(),
        segment=segment,
        exchange=exchange,
    )
    logger.info("save_manual_upload: record id=%s marked pending at %s", record.id, dest_path)
    return record


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

def process_batch(task: SegmentBatchTask) -> None:
    """Attempt CBOS Steps 2->9 for one segment/date/exchange batch. Called
    by the worker loop for one queue item at a time."""
    file_paths = [Path(p) for p in task.file_paths if Path(p).exists()]
    if not file_paths:
        logger.warning("Batch %s: no files still exist on disk, skipping", task.key)
        return

    login_id = settings.cbos_login_id
    trade_date = task.folder_date
    logger.info("Processing batch %s: %d file(s)", task.key, len(file_paths))

    session = SessionLocal()
    try:
        repo = UploadedFileRepository(session)

        # Step 2: create the process ID for this segment/date, shared by
        # every file in the batch.
        step2_response = cbos_client.get_new_trade_process(task.segment, login_id, trade_date)
        process_id = cbos_client.extract_process_id(step2_response)
        table2 = cbos_client.extract_upload_candidates(step2_response)
        logger.info("ProcessID = %s (batch=%s, %d UploadID candidate(s))", process_id, task.key, len(table2))

        # Step 3: confirmation check - logged, never blocks the batch (the
        # doc frames this as a sanity check, not a gate).
        try:
            cbos_client.check_process_id_exist(task.segment, login_id)
        except CBOSUploadError as exc:
            logger.warning("Batch %s: CheckProcessIDExist failed (non-fatal): %s", task.key, exc)

        # Step 4: fetch every UploadID's matching rule ONCE, up front.
        rules = upload_matching.fetch_upload_rules(table2)
        if not rules:
            logger.error("Batch %s: no usable UploadID rules resolved from Table2 - failing every file", task.key)

        uploaded_candidates: list[tuple] = []  # (record, dest_file_path, response, request_log)

        for file_path in file_paths:
            record = repo.create_audit_record(file_path, task.folder_date, task.segment, task.exchange)
            request_log: list = []

            try:
                rule = upload_matching.match_file(file_path, rules)
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
            except Exception as exc:
                logger.error("Batch %s: upload sequence failed for %s: %s", task.key, file_path.name, exc)
                request_log.append({"step": "error", "error": str(exc)})
                handle_upload_failure(repo, record, file_path, exc, request_log)

        if not uploaded_candidates:
            logger.info("Batch %s: no files uploaded, skipping trigger/poll", task.key)
            return

        # Step 6: existing-process confirmation lookup, once per batch.
        # Non-fatal - purely diagnostic.
        try:
            cbos_client.get_existing_process_id(task.segment, login_id, trade_date)
        except CBOSUploadError as exc:
            logger.warning("Batch %s: getdropdown(EXISTINGPROCESSID) failed (non-fatal): %s", task.key, exc)

        # Step 8: trigger execution, once, only after every matched file in
        # the batch has completed Steps 5+7.
        try:
            if settings.cbos_trigger_after_upload:
                cbos_client.trigger_process(login_id, task.segment, trade_date, process_id)
        except CBOSUploadError as exc:
            logger.error("Batch %s: Step 8 trigger failed: %s - failing all %d uploaded file(s)",
                         task.key, exc, len(uploaded_candidates))
            for record, file_path, request_log in uploaded_candidates:
                request_log.append({"step": "trigger_error", "error": str(exc)})
                handle_upload_failure(repo, record, file_path, exc, request_log)
            return

        # Step 9: poll once per batch (segment-level, not per file/guid).
        succeeded = cbos_client.poll_file_upload_status(task.segment, login_id)
        for record, file_path, request_log in uploaded_candidates:
            request_log.append({"step": "file_process_status", "result": succeeded})
            if succeeded:
                handle_upload_success(repo, record, file_path, {"MSG": "TRUE"}, request_log)
            else:
                handle_upload_failure(
                    repo, record, file_path,
                    CBOSUploadError("Step 9 file_process_status did not confirm MSG=TRUE"),
                    request_log,
                )

        logger.info(
            "Batch %s complete: %d uploaded, %d rejected/failed",
            task.key, len(uploaded_candidates) if succeeded else 0,
            len(file_paths) - (len(uploaded_candidates) if succeeded else 0),
        )

    finally:
        session.close()
        logger.debug("process_batch: session closed for %s", task.key)
