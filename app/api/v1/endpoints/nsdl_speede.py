"""The NSDL SPEED-e upload API - a THIN router, every decision in
app/services/nsdl_speede_service.py, same convention as batches.py and
settlements.py.

POST /settlement/nsdl_speede_upload        -> upload the selected SPEED-e
                                              reports (omit `files` for all
                                              12), synchronously, and report
                                              where each one ended up
GET  /settlement/nsdl_speede_upload/{id}   -> re-poll one file's stored TranId
                                              without re-uploading it

Deliberately separate from /settlements (the DP File Upload API): different
upstream, different flow, no shared state. Nothing here touches it.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.clients.nsdl_speede_client import NsdlSpeedeError
from app.core.database import get_db_session
from app.schemas.nsdl_speede import NsdlSpeedeUploadRequest, NsdlSpeedeUploadResponse
from app.services import nsdl_speede_service
from app.services.nsdl_speede_service import (
    UnknownNsdlSpeedeUploadError,
    UnknownSpeedeFileError,
)

logger = logging.getLogger("nsdl_speede_endpoint")
router = APIRouter(prefix="/settlement", tags=["nsdl-speede"])


@router.post("/nsdl_speede_upload", response_model=NsdlSpeedeUploadResponse)
def submit_upload(req: NsdlSpeedeUploadRequest, session: Session = Depends(get_db_session)):
    selectors = [selector.model_dump() for selector in req.files] if req.files else None
    try:
        return nsdl_speede_service.process_upload(
            session, req.trade_date, selectors, req.correlation_id
        )
    except UnknownSpeedeFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NsdlSpeedeError as exc:
        # Only the once-per-request category lookup reaches here - a per-file
        # failure is that file's verdict, not the call's.
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/nsdl_speede_upload/{nsdl_upload_id}", response_model=NsdlSpeedeUploadResponse)
def get_upload_status(nsdl_upload_id: int, session: Session = Depends(get_db_session)):
    try:
        return nsdl_speede_service.check_status(session, nsdl_upload_id)
    except UnknownNsdlSpeedeUploadError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NsdlSpeedeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
