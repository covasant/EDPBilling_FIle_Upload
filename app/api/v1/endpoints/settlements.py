"""The settlement upload API - a THIN router, every decision in
app/services/settlement_service.py, same convention as batches.py.

POST /settlements/uploads                        -> the orchestrator's one and
                                                      only call: runs the whole
                                                      DP File Upload flow for
                                                      one file, synchronously.
GET  /settlements/uploads/{id}                    -> read back the audit row
GET  /settlements/uploads/{id}/status             -> re-poll GetFileUploadStatus
                                                      against the stored Tran_Id

GET  /settlements/upload-masters                  -> ops/manual only (Step 1
GET  /settlements/upload-masters/{upload_id}          and Step 2 passthroughs -
                                                       the orchestrator already
                                                       supplies upload_id, so
                                                       it never calls these)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.clients.dp_upload_client import DPUploadError, get_dp_upload_client
from app.core.database import get_db_session
from app.schemas.settlement import SettlementUploadRequest, SettlementUploadResponse
from app.services import settlement_service
from app.services.settlement_service import SettlementFileNotFoundError, UnknownSettlementUploadError

logger = logging.getLogger("settlements_endpoint")
router = APIRouter(prefix="/settlements", tags=["settlements"])


@router.get("/upload-masters")
def list_upload_masters():
    try:
        masters = get_dp_upload_client().list_upload_masters()
    except DPUploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [{"id": m.id, "display_name": m.display_name, "depository": m.depository} for m in masters]


@router.get("/upload-masters/{upload_id}")
def get_upload_master(upload_id: int):
    try:
        config = get_dp_upload_client().get_upload_master_config(str(upload_id))
    except DPUploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return config.raw


@router.post("/uploads", response_model=SettlementUploadResponse)
def submit_upload(req: SettlementUploadRequest, session: Session = Depends(get_db_session)):
    try:
        result = settlement_service.process_upload(
            session, req.upload_id, req.file_name, req.correlation_id
        )
    except SettlementFileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DPUploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result


@router.get("/uploads/{settlement_upload_id}")
def get_upload(settlement_upload_id: int, session: Session = Depends(get_db_session)):
    try:
        return settlement_service.get_upload_details(session, settlement_upload_id)
    except UnknownSettlementUploadError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/uploads/{settlement_upload_id}/status")
def get_upload_status(settlement_upload_id: int, session: Session = Depends(get_db_session)):
    result = settlement_service.check_status(session, settlement_upload_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown settlement_upload_id {settlement_upload_id} (or no Tran_Id yet)",
        )
    return result
