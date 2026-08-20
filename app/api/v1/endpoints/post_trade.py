"""The post-trade upload API — a THIN router, every decision in
app/services/post_trade_upload_service.py, same convention as batches.py and settlements.py.

POST /post-trade/uploads   -> upload ONE post-trade input file into CBOS, synchronously.

Deliberately not a batch. Post-trade files belong to no trade process and no manifest; the
completeness gate that governs a segment batch must not reach them (see the service's docstring).
One call, one file, one answer.
"""

import logging

from fastapi import APIRouter, HTTPException

from app.clients.cbos_client import CBOSUploadError
from app.schemas.post_trade import PostTradeUploadRequest, PostTradeUploadResponse
from app.services import post_trade_upload_service as service

logger = logging.getLogger("post_trade_endpoint")
router = APIRouter(prefix="/post-trade", tags=["post-trade"])


@router.post("/uploads", response_model=PostTradeUploadResponse)
def upload_post_trade_file(body: PostTradeUploadRequest) -> PostTradeUploadResponse:
    """Put one post-trade file into CBOS and answer when it is there.

    The status codes distinguish three failures the caller must treat differently:

    * **404** the file is not on disk yet. The bot has not fetched it, or it has not published.
      The caller should wait and ask again — this is the common case in the small hours.
    * **422** the UploadID is unknown to CBOS, or the filename does not match its rule. Both are
      configuration, not timing, and retrying will not fix either.
    * **502** CBOS refused or could not be reached. Transient; retry.

    Collapsing these into one error is what makes a dead UploadID look like a slow exchange.
    """
    try:
        result = service.upload_one(
            upload_id=body.upload_id,
            file_name=body.file_name,
            trade_date=body.trade_date,
        )
    except service.PostTradeFileNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (service.UnknownUploadId, service.FileNameRejected) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CBOSUploadError as exc:
        logger.warning("post-trade upload failed against CBOS: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return PostTradeUploadResponse(
        upload_id=result.upload_id,
        file_name=result.file_name,
        trade_date=result.trade_date,
        guid=result.guid,
        rule_name=result.rule_name,
    )
