import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.schemas.upload import UploadResponse
from app.services import upload_service

logger = logging.getLogger("upload_endpoint")
router = APIRouter(tags=["upload"])


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    segment: str = Form(...),
    exchange: str = Form(...),
):
    """Manual upload edge-case: saves the file into the standard segment/exchange/date
    folder, so the scheduler picks it up and queues it for upload on its next
    run. Writes no DB row and never talks to CBOS directly.
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
    dest_path = upload_service.save_manual_upload(content, file.filename, segment, exchange)

    logger.info("POST /upload complete: %s -> %s (queued for next scan)", file.filename, dest_path)
    return UploadResponse(message="File saved; queued for upload on the next scan", status="pending")
