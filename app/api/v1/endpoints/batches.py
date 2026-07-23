"""The batch intake API — a THIN router (ADR 3): sessions via the documented
dependency, every decision in app/services/batch_service.py.

POST /batches            {"manifest_path": ...}  -> 202 queued / 200 known
GET  /batches/{batch_id}                          -> status + per-file outcomes
POST /batches/rescan                              -> queue unconsumed manifests
POST /batches/{batch_id}/proceed {slots, reason}  -> audited force-proceed
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db_session
from app.services import batch_service
from app.services.batch_service import ProceedNotAllowedError, UnknownBatchError
from app.services.manifest_service import ChecksumMismatchError, ManifestError

logger = logging.getLogger("batches_endpoint")
router = APIRouter(prefix="/batches", tags=["batches"])


class BatchSubmission(BaseModel):
    manifest_path: str


class ProceedRequest(BaseModel):
    slots: list[str] = Field(min_length=1, description="UploadIDs to mark optional")
    reason: str = Field(min_length=1, description="Why billing may proceed without them")


@router.post("", status_code=202)
def submit_batch(submission: BatchSubmission, request: Request,
                 session: Session = Depends(get_db_session)):
    try:
        result = batch_service.submit_manifest(
            session, request.app.state.batch_queue, Path(submission.manifest_path))
    except ManifestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ChecksumMismatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    body = {"batch_id": result.batch_id, "status": result.status}
    if result.already_known:
        return JSONResponse(status_code=200, content=body)
    return body


@router.get("/{batch_id}")
def get_batch(batch_id: str, session: Session = Depends(get_db_session)):
    try:
        return batch_service.get_batch_details(session, batch_id)
    except UnknownBatchError as exc:
        raise HTTPException(status_code=404, detail=f"unknown batch_id {batch_id}") from exc


@router.post("/rescan", status_code=202)
def rescan(request: Request, session: Session = Depends(get_db_session)):
    return batch_service.rescan(session, request.app.state.batch_queue)


@router.post("/{batch_id}/proceed", status_code=202)
def proceed(batch_id: str, req: ProceedRequest, request: Request,
            session: Session = Depends(get_db_session)):
    try:
        return batch_service.request_proceed(
            session, request.app.state.batch_queue, batch_id, req.slots, req.reason)
    except UnknownBatchError as exc:
        raise HTTPException(status_code=404, detail=f"unknown batch_id {batch_id}") from exc
    except ProceedNotAllowedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
