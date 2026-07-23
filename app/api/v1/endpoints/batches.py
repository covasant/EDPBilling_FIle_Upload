"""The batch intake API - the ONLY way work enters the uploader
(docs/BATCH_HANDOFF_CONTRACT.md). Callers: the download bot's post-
finalization callback today, the EDP_Billing engine later, and ops by hand.

POST /batches            {"manifest_path": ...}  -> 202 queued / 200 known
GET  /batches/{batch_id}                          -> status + per-file outcomes
POST /batches/rescan                              -> queue unconsumed manifests
POST /batches/{batch_id}/proceed {slots, reason}  -> audited force-proceed
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_sessionmaker
from app.core.queue import BatchQueue, SegmentBatchTask
from app.models.uploaded_file import UploadedFile
from app.repositories.batch_repository import BatchRepository
from app.services import file_service, manifest_service
from app.services.manifest_service import ChecksumMismatchError, ManifestError

logger = logging.getLogger("batches_endpoint")
router = APIRouter(prefix="/batches", tags=["batches"])


class BatchSubmission(BaseModel):
    manifest_path: str


class ProceedRequest(BaseModel):
    slots: list[str] = Field(min_length=1, description="UploadIDs to mark optional")
    reason: str = Field(min_length=1, description="Why billing may proceed without them")


def _intake(manifest_path: Path, queue: BatchQueue,
            session: Session) -> tuple[int, dict]:
    """Shared intake lane for POST /batches and rescan: validate -> verify ->
    record -> enqueue. Returns (http_status, body)."""
    repo = BatchRepository(session)

    try:
        manifest = manifest_service.load_manifest(manifest_path)
    except ManifestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing = repo.find_by_batch_id(manifest.batch_id)
    if existing is not None:
        logger.info("POST /batches: batch %s already known (status=%s)",
                    manifest.batch_id, existing.status)
        return 200, {"batch_id": existing.batch_id, "status": existing.status}

    try:
        manifest_service.verify_checksums(manifest_path)
    except ChecksumMismatchError as exc:
        # Recorded for audit (GET /batches/{id} answers "why was it rejected"),
        # files left in place - a superseding manifest is the fix.
        repo.create(
            batch_id=manifest.batch_id, segment=manifest.segment,
            trade_date=manifest.trade_date, folder_date=manifest.folder_date,
            manifest_path=str(manifest_path), correlation_id=manifest.correlation_id,
            status="rejected", status_detail=json.dumps({"checksum_error": str(exc)}),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    repo.create(
        batch_id=manifest.batch_id, segment=manifest.segment,
        trade_date=manifest.trade_date, folder_date=manifest.folder_date,
        manifest_path=str(manifest_path), correlation_id=manifest.correlation_id,
    )
    queue.enqueue(manifest_service.to_task(manifest))
    return 202, {"batch_id": manifest.batch_id, "status": "queued"}


@router.post("", status_code=202)
def submit_batch(submission: BatchSubmission, request: Request):
    session = get_sessionmaker()()
    try:
        status, body = _intake(Path(submission.manifest_path),
                               request.app.state.batch_queue, session)
    finally:
        session.close()
    if status == 200:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=200, content=body)
    return body


@router.get("/{batch_id}")
def get_batch(batch_id: str):
    session = get_sessionmaker()()
    try:
        batch = BatchRepository(session).find_by_batch_id(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail=f"unknown batch_id {batch_id}")
        files = (
            session.query(UploadedFile)
            .filter(UploadedFile.folder_date == batch.folder_date,
                    UploadedFile.segment == batch.segment)
            .all()
        )
        return {
            "batch_id": batch.batch_id,
            "segment": batch.segment,
            "trade_date": batch.trade_date,
            "status": batch.status,
            "status_detail": json.loads(batch.status_detail) if batch.status_detail else None,
            "correlation_id": batch.correlation_id,
            "proceed": (
                {"slots": json.loads(batch.proceed_slots), "reason": batch.proceed_reason,
                 "at": batch.proceeded_at.isoformat() if batch.proceeded_at else None}
                if batch.proceed_slots else None
            ),
            "files": [
                {"name": f.file_name, "status": f.status, "outcome": f.cbos_response,
                 "cbos_upload_id": f.cbos_upload_id, "process_id": f.process_id}
                for f in files
            ],
        }
    finally:
        session.close()


@router.post("/rescan", status_code=202)
def rescan(request: Request):
    """Queue every manifest on disk whose batch_id isn't known yet - the
    manual ops path, and catch-up for callbacks that never arrived."""
    queue = request.app.state.batch_queue
    session = get_sessionmaker()()
    queued: list[str] = []
    skipped = 0
    try:
        known = BatchRepository(session).known_batch_ids()
        for manifest_path in manifest_service.find_manifests(file_service.get_root()):
            try:
                manifest = manifest_service.load_manifest(manifest_path)
            except ManifestError as exc:
                logger.warning("rescan: skipping unreadable manifest %s: %s", manifest_path, exc)
                skipped += 1
                continue
            if manifest.batch_id in known:
                continue
            try:
                status, body = _intake(manifest_path, queue, session)
            except HTTPException as exc:
                logger.warning("rescan: manifest %s rejected: %s", manifest_path, exc.detail)
                skipped += 1
                continue
            if status == 202:
                queued.append(body["batch_id"])
    finally:
        session.close()
    logger.info("POST /batches/rescan: queued=%s skipped=%d", queued, skipped)
    return {"queued": queued, "skipped": skipped}


@router.post("/{batch_id}/proceed", status_code=202)
def proceed(batch_id: str, req: ProceedRequest, request: Request):
    """Audited force-proceed for an INCOMPLETE batch: ops explicitly names the
    unfilled slots billing may run without. Recorded on the Batch row, then the
    worker re-runs the mark-optional + confirm lane (upload_service.proceed_batch)."""
    session = get_sessionmaker()()
    try:
        repo = BatchRepository(session)
        batch = repo.find_by_batch_id(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail=f"unknown batch_id {batch_id}")
        if batch.status != "incomplete":
            raise HTTPException(
                status_code=409,
                detail=f"batch {batch_id} is {batch.status!r} - "
                       "proceed applies only to 'incomplete'")

        repo.record_proceed(batch, req.slots, req.reason)
        task = SegmentBatchTask(
            folder_date=batch.folder_date, segment=batch.segment,
            batch_id=batch.batch_id, correlation_id=batch.correlation_id,
            mode="proceed", proceed_slots=list(req.slots), proceed_reason=req.reason,
        )
        request.app.state.batch_queue.enqueue(task)
        return {"batch_id": batch_id, "status": "proceed_queued", "slots": req.slots}
    finally:
        session.close()
