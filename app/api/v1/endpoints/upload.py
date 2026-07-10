import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.database import get_db_session
from app.repositories.uploaded_file_repository import UploadedFileRepository
from app.schemas.upload import UploadResponse
from app.services import upload_service

logger = logging.getLogger("upload_endpoint")
router = APIRouter(tags=["upload"])


def get_uploaded_file_repository(session: Session = Depends(get_db_session)) -> UploadedFileRepository:
    return UploadedFileRepository(session)


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    segment: str = Form(...),
    exchange: str = Form(...),
    repo: UploadedFileRepository = Depends(get_uploaded_file_repository),
):
    """Manual upload edge-case: saves the file into the standard segment/exchange/date
    folder and marks it pending, so the scheduler picks it up and queues it
    for upload on its next run. This endpoint never talks to CBOS directly.
    """
    logger.info("POST /upload received: filename=%s segment=%s exchange=%s", file.filename, segment, exchange)

    segment = segment.strip()
    if not segment:
        logger.warning("POST /upload rejected: segment is required")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="segment is required")

    exchange = exchange.strip()
    if not exchange:
        logger.warning("POST /upload rejected: exchange is required")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="exchange is required")

    if not file.filename:
        logger.warning("POST /upload rejected: file name is required")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="file name is required")

    content = await file.read()
    if not content:
        logger.warning("POST /upload rejected: %s is empty", file.filename)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    logger.debug("POST /upload: read %d bytes from %s", len(content), file.filename)
    record = upload_service.save_manual_upload(content, file.filename, segment, exchange, repo)

    logger.info("POST /upload complete: %s -> record id=%s status=%s", file.filename, record.id, record.status)
    return UploadResponse(message="File uploaded successfully", status=record.status)
