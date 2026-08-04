"""Orchestrates one settlement upload (Steps 2-7 of the DP File Upload API,
see app/clients/dp_upload_client.py) synchronously, for POST
/settlements/uploads.

Unlike billing's upload_service.process_batch (queued, background-worker
driven), this runs entirely inside the request: the settlement automation
orchestrator (a separate service) already owns scheduling/retry/SLA tracking
for the settlement day and expects a call-and-get-result contract - see the
plan's "Design judgment call". Step 1 (the Upload_Id dropdown) is skipped
here; the orchestrator always supplies upload_id directly.
"""

import logging
import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.clients.dp_upload_client import (
    STATUS_ERROR,
    STATUS_SUCCESS,
    BaseDPUploadClient,
    DPUploadError,
    UploadMasterConfig,
    get_dp_upload_client,
)
from app.core.config import settings
from app.core.correlation import batch_context
from app.models.settlement_upload import SettlementUpload
from app.repositories.settlement_upload_repository import SettlementUploadRepository

logger = logging.getLogger("settlement_service")


class SettlementFileNotFoundError(Exception):
    """The named file isn't on the shared folder (settings.cbos_setl_shared_folder_path)
    yet - most likely the download bot hasn't dropped it there."""


class UnknownSettlementUploadError(Exception):
    """No settlement_uploads row exists for the given id."""


def _locate_file(file_name: str) -> Path:
    root = Path(settings.cbos_setl_shared_folder_path)
    file_path = root / file_name
    if not file_path.is_file():
        raise SettlementFileNotFoundError(f"{file_name} not found under {root}")
    return file_path


def _extract_guid(chunk_full_path: str) -> str | None:
    """chunk_full_path is "{guid}_{original_filename}" (see
    BaseDPUploadClient.upload_chunks) - the guid is whatever precedes the
    first underscore, regardless of underscores inside the original name."""
    if "_" not in chunk_full_path:
        return None
    return chunk_full_path.split("_", 1)[0]


def _run_upload_attempt(
    client: BaseDPUploadClient,
    repo: SettlementUploadRepository,
    record: SettlementUpload,
    config: UploadMasterConfig,
    file_path: Path,
    unique_identifier: str,
) -> str:
    """Steps 3-5 for one attempt. Returns the Tran_Id. Raises DPUploadError on
    failure - the caller decides whether to retry (restart from Step 3, per
    the doc) or give up."""
    client.validate_file(config, file_path.name, unique_identifier)
    repo.update(record, status="uploading", last_step="uploadfilevalidate")
    repo.commit()

    file_bytes = file_path.read_bytes()
    chunk_size = max(1, settings.chunk_setl_size_kb) * 1024
    total_chunks = max(1, (len(file_bytes) + chunk_size - 1) // chunk_size)

    chunk_full_path = client.upload_chunks(file_bytes, file_path.name)
    repo.update(
        record,
        status="uploaded",
        last_step="uploadchunks",
        chunk_full_path=chunk_full_path,
        guid=_extract_guid(chunk_full_path),
        total_chunks=total_chunks,
        chunks_uploaded=total_chunks,
    )
    repo.commit()

    tran_id = client.finalize_upload(config, file_path.name, chunk_full_path, unique_identifier)
    repo.update(record, status="registered", last_step="uploadfilemaster", tran_id=tran_id)
    repo.commit()
    return tran_id


def process_upload(
    session: Session, upload_id: int, file_name: str, correlation_id: str | None = None
) -> dict:
    """The whole flow: locate file -> Step 2 config -> Steps 3-5 (with
    restart-from-3 retry on chunk/registration failure) -> Step 6 poll ->
    Step 7 if required. Returns a dict matching SettlementUploadResponse.

    Every step updates the SettlementUpload audit row so a crash mid-flow
    still leaves a readable trail of exactly where it stopped (see the plan's
    "one thing IS still reused" note) - nothing resumes automatically, the
    caller (orchestrator) is expected to retry the whole call.
    """
    repo = SettlementUploadRepository(session)
    key = f"settlement|{upload_id}|{file_name}"

    with batch_context(key, correlation_id):
        record = repo.insert(
            upload_id=str(upload_id), file_name=file_name, status="pending", correlation_id=correlation_id
        )
        repo.commit()

        try:
            file_path = _locate_file(file_name)
        except SettlementFileNotFoundError as exc:
            repo.update(record, status="failed", last_step="locate_file", error_detail=str(exc))
            repo.commit()
            raise

        client = get_dp_upload_client()

        config = client.get_upload_master_config(str(upload_id))
        repo.update(
            record,
            status="validating",
            last_step="getdetailsuploadmaster",
            upload_display_name=config.display_name,
            depository=config.depository,
            process_required=config.process_required,
        )
        repo.commit()

        unique_identifier = str(time.time())
        max_attempts = max(1, settings.cbos_setl_max_retries + 1)
        tran_id: str | None = None
        last_error: DPUploadError | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                tran_id = _run_upload_attempt(client, repo, record, config, file_path, unique_identifier)
                break
            except DPUploadError as exc:
                last_error = exc
                logger.warning(
                    "settlement_service: attempt %d/%d failed for %s: %s",
                    attempt,
                    max_attempts,
                    file_name,
                    exc,
                )
                repo.update(record, retry_count=attempt, error_detail=str(exc))
                repo.commit()
                if attempt < max_attempts:
                    time.sleep(settings.cbos_setl_retry_delay_seconds)

        if tran_id is None:
            repo.update(record, status="failed", last_step="uploadfilevalidate/uploadchunks/uploadfilemaster")
            repo.commit()
            return {
                "settlement_upload_id": record.id,
                "upload_id": str(upload_id),
                "tran_id": None,
                "status": "failed",
                "final_step": record.last_step,
                "detail": str(last_error) if last_error else "upload failed",
                "correlation_id": correlation_id,
            }

        repo.update(record, status="polling", last_step="GetFileUploadStatus")
        repo.commit()
        status_code, description = client.poll_status(tran_id)
        repo.update(record, status_code=status_code)

        if status_code == STATUS_SUCCESS:
            if config.process_required:
                process_response = client.run_process(config, unique_identifier, tran_id)
                repo.update(record, status="processed", last_step="uploadprocess", error_detail=None)
                repo.commit()
                return {
                    "settlement_upload_id": record.id,
                    "upload_id": str(upload_id),
                    "tran_id": tran_id,
                    "status": "processed",
                    "final_step": "uploadprocess",
                    "detail": process_response,
                    "correlation_id": correlation_id,
                }
            repo.update(record, status="success", last_step="GetFileUploadStatus")
            repo.commit()
            return {
                "settlement_upload_id": record.id,
                "upload_id": str(upload_id),
                "tran_id": tran_id,
                "status": "success",
                "final_step": "GetFileUploadStatus",
                "detail": description,
                "correlation_id": correlation_id,
            }

        if status_code == STATUS_ERROR:
            repo.update(record, status="failed", last_step="GetFileUploadStatus", error_detail=description)
            repo.commit()
            return {
                "settlement_upload_id": record.id,
                "upload_id": str(upload_id),
                "tran_id": tran_id,
                "status": "failed",
                "final_step": "GetFileUploadStatus",
                "detail": description,
                "correlation_id": correlation_id,
            }

        # POLL_TIMED_OUT - v1 does not auto-fire Step 7 later (see plan's open
        # question 5); caller can re-check via GET /settlements/uploads/{id}/status.
        repo.update(record, status="in_progress", last_step="GetFileUploadStatus")
        repo.commit()
        return {
            "settlement_upload_id": record.id,
            "upload_id": str(upload_id),
            "tran_id": tran_id,
            "status": "in_progress",
            "final_step": "GetFileUploadStatus",
            "detail": description,
            "correlation_id": correlation_id,
        }


def get_upload_details(session: Session, settlement_upload_id: int) -> dict:
    """GET /settlements/uploads/{id} - read back the audit row as-is."""
    repo = SettlementUploadRepository(session)
    record = repo.get(settlement_upload_id)
    if record is None:
        raise UnknownSettlementUploadError(f"unknown settlement_upload_id {settlement_upload_id}")
    return {
        "settlement_upload_id": record.id,
        "upload_id": record.upload_id,
        "upload_display_name": record.upload_display_name,
        "depository": record.depository,
        "file_name": record.file_name,
        "guid": record.guid,
        "chunk_full_path": record.chunk_full_path,
        "tran_id": record.tran_id,
        "process_required": record.process_required,
        "status": record.status,
        "last_step": record.last_step,
        "status_code": record.status_code,
        "total_chunks": record.total_chunks,
        "chunks_uploaded": record.chunks_uploaded,
        "retry_count": record.retry_count,
        "correlation_id": record.correlation_id,
        "error_detail": record.error_detail,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


def check_status(session: Session, settlement_upload_id: int) -> dict | None:
    """GET /settlements/uploads/{id}/status - re-polls GetFileUploadStatus
    against the stored tran_id without replaying Steps 3-5. Returns None if
    the record is unknown or has no tran_id yet."""
    repo = SettlementUploadRepository(session)
    record = repo.get(settlement_upload_id)
    if record is None or not record.tran_id:
        return None

    client = get_dp_upload_client()
    status_code, description = client.check_status_once(record.tran_id)
    repo.update(record, status_code=status_code)
    if status_code == STATUS_SUCCESS:
        repo.update(record, status="success")
    elif status_code == STATUS_ERROR:
        repo.update(record, status="failed", error_detail=description)
    repo.commit()

    return {
        "settlement_upload_id": record.id,
        "upload_id": record.upload_id,
        "tran_id": record.tran_id,
        "status": record.status,
        "final_step": record.last_step,
        "detail": description,
        "correlation_id": record.correlation_id,
    }
