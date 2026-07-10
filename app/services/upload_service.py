"""Coordinates file discovery, queueing, CBOS upload, database updates, and
file movement.

The only decision point in this pipeline is the CBOS upload result: every
discovered file is attempted through the full sequence with no
pre-upload validation, and whether that sequence succeeds or fails is the
sole thing that decides uploaded/ vs uploadFailed/. See
handle_upload_success()/handle_upload_failure() - every exit point of
process_task funnels through exactly one of those two functions.

The scheduler only calls discover_and_enqueue(); the worker only calls
process_task(). Discovery never touches the database - dedup is
filesystem-only (a file already moved into uploaded/ or uploadFailed/ can't
be rediscovered, see file_service.list_subdirs) plus the in-memory
in-flight guard in app/core/queue.py. The database is written to for audit
purposes only; nothing reads it to make a processing decision."""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from app.clients import cbos_client
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.queue import FileTask, enqueue
from app.repositories.uploaded_file_repository import UploadedFileRepository
from app.services import file_service

logger = logging.getLogger("upload_service")
settings = get_settings()


# --------------------------------------------------------------------------
# Discovery: scan the filesystem and enqueue new files. No CBOS calls, no DB
# reads.
# --------------------------------------------------------------------------

def discover_and_enqueue() -> None:
    """Walk {FILE_ROOT_PATH}/{date}/{segment}/{exchange}/ for T and the
    configured scan_days_back further, and push every file found directly
    in those folders onto the upload queue. Files already inside uploaded/
    or uploadFailed/ are structurally excluded by file_service.list_subdirs
    - they are never considered "discovered". Every file is queued
    regardless of extension or size - no file type or 0-byte validation is
    performed at discovery time, so any file type received from upstream
    systems (.csv, .xlsx, .xls, .txt, .dat, .gz, etc.) is picked up without
    code changes."""
    root = file_service.get_root()
    dates = file_service.get_processing_dates()
    logger.info("discover_and_enqueue: starting scan of %s for dates=%s", root, dates)
    logger.info("discover_and_enqueue: file type and file size validations are skipped - all files are considered for upload")

    for folder_date in dates:
        _discover_date(root, folder_date)
    logger.info("discover_and_enqueue: scan complete")


def _discover_date(root: Path, folder_date: str) -> None:
    logger.info("Processing date: %s", folder_date)

    files_found = 0
    for file_path, segment, exchange in file_service.discover_files_for_date(root, folder_date):
        logger.info("Found file: %s (extension=%s, size=%d bytes)",
                     file_path.name, file_path.suffix or "(none)", file_path.stat().st_size)
        files_found += 1
        _maybe_enqueue(file_path, folder_date, segment, exchange)

    logger.info("Found %d files for %s", files_found, folder_date)


def _maybe_enqueue(file_path: Path, folder_date: str, segment: str, exchange: str) -> None:
    added = enqueue(FileTask(file_path=str(file_path), folder_date=folder_date, segment=segment, exchange=exchange))
    if not added:
        logger.debug("Skipping already-queued/in-flight file %s", file_path)


# --------------------------------------------------------------------------
# Manual upload endpoint support
# --------------------------------------------------------------------------

def save_manual_upload(content: bytes, file_name: str, segment: str, exchange: str, repo: UploadedFileRepository):
    """Used by POST /upload: save the file to the standard location and mark
    it pending. Never talks to CBOS directly - the scheduler's next pass
    discovers and enqueues it like any other file."""
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
# Centralized success/failure handlers. These are the ONLY two places that
# move a file on disk and write its final audit status - every code path in
# process_task ends in exactly one of these.
# --------------------------------------------------------------------------

def handle_upload_success(repo: UploadedFileRepository, record, file_path: Path, response: dict, request_log: list) -> Path:
    """ confirmed processing finished -> move to uploaded/, record the
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
        "handle_upload_success: file=%s extension=%s status=uploaded response=%s destination=%s",
        file_path.name, file_path.suffix or "(none)", response, dest_path,
    )
    return dest_path


def handle_upload_failure(repo: UploadedFileRepository, record, file_path: Path, error: Exception, request_log: list) -> Path:
    """Any failure anywhere in the CBOS sequence -> move to uploadFailed/,
    record why."""
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
        "handle_upload_failure: file=%s extension=%s status=failed response=%s destination=%s",
        file_path.name, file_path.suffix or "(none)", error, dest_path,
    )
    return dest_path


# --------------------------------------------------------------------------
# Worker: process a single queued file. CBOS's result is the only decision
# point - no pre-upload validation, no post-upload business rules.
# --------------------------------------------------------------------------

def process_task(task: FileTask) -> None:
    """Attempt CBOS Steps 2->7 for one file and let CBOS decide the outcome.
    Called by the worker loop for one queue item at a time."""
    file_path = Path(task.file_path)
    logger.debug("process_task: dequeued %s (segment=%s, exchange=%s, date=%s)",
                 file_path, task.segment, task.exchange, task.folder_date)
    if not file_path.exists():
        logger.warning("Queued file no longer exists, skipping: %s", file_path)
        return

    session = SessionLocal()
    try:
        repo = UploadedFileRepository(session)
        record = repo.create_audit_record(file_path, task.folder_date, task.segment, task.exchange)
        logger.debug("process_task: db record id=%s for %s", record.id, file_path.name)

        logger.info("Processing file: %s (extension=%s)", file_path.name, file_path.suffix or "(none)")
        logger.info(
            "process_task: file type and file size validations skipped for %s - upload is attempted regardless of extension or size",
            file_path.name,
        )
        request_log: list = []

        try:
            login_id = settings.cbos_login_id
            trade_date = task.folder_date

            # Step 2: getNewTradeProcess - obtain PROCESSID + Table2 candidates
            step2_response = cbos_client.get_new_trade_process(task.segment, login_id, trade_date)
            request_log.append({"step": "getNewTradeProcess", "response": step2_response})
            process_id = cbos_client.extract_process_id(step2_response)
            table2 = cbos_client.extract_upload_candidates(step2_response)
            repo.update(record, process_id=process_id)
            repo.commit()

            # Step 3: pick + validate the UPLOADID this file belongs to
            upload_id, upload_settings_response = cbos_client.select_upload_id(table2, file_path.name)
            request_log.append({
                "step": "GetNewTradeProcessPromodalUploadSettings",
                "upload_id": upload_id,
                "response": upload_settings_response,
            })
            repo.update(record, cbos_upload_id=upload_id)
            repo.commit()

            # Step 4: chunked file upload under a fresh GUID
            guid = str(uuid.uuid4())
            cbos_client.upload_file_chunks(file_path, upload_id, guid)
            request_log.append({"step": "SaveTradePromodalUploadChunkFile", "guid": guid})
            repo.update(record, guid=guid)
            repo.commit()

            # Step 6: register the uploaded chunks as one file
            step6_response = cbos_client.save_trade_process_upload_file(
                upload_id, guid, file_path.name, login_id, process_id
            )
            request_log.append({"step": "SaveNewTradeProcessPromodalUploadFile", "response": step6_response})

            # Step 7: poll CBOS until it confirms processing finished (or times out)
            status_result = cbos_client.poll_file_process_status(process_id, upload_id, guid)
            request_log.append({"step": "file_process_status", "response": status_result})

            logger.info(
                "CBOS upload sequence successful: %s (process_id=%s, upload_id=%s)",
                file_path.name, process_id, upload_id,
            )
            handle_upload_success(repo, record, file_path, status_result, request_log)

        except Exception as exc:
            # The single decision point: process-id creation, upload-settings
            # lookup, chunk upload, file registration, or status polling -
            # any failure anywhere in the sequence routes here.
            logger.error("CBOS upload sequence failed for %s: %s", file_path.name, exc)
            request_log.append({"step": "error", "error": str(exc)})
            handle_upload_failure(repo, record, file_path, exc, request_log)

    finally:
        session.close()
        logger.debug("process_task: session closed for %s", file_path.name)
