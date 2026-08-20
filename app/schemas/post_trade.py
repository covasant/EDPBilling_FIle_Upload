"""Wire shapes for POST /post-trade/uploads.

No file bytes on the request, matching the settlement path: the download bot has already put the
file under the trade date's POSTTRADE folder and this service looks it up by name.
"""

from pydantic import BaseModel, Field


class PostTradeUploadRequest(BaseModel):
    upload_id: str = Field(
        description=(
            "The CBOS UploadID for this file. Per-FILE, not per-process, and it selects the "
            "parser and destination table as well as the name rule — 547 is CASH MG02 in UDIFF "
            "form, 554 is the CASH PEAK file with an identical name pattern and a different "
            "destination. The caller holds it in configuration; this service does not guess it."
        )
    )
    file_name: str = Field(
        description="Name of the file already under <root>/<trade_date>/POSTTRADE/"
    )
    trade_date: str = Field(
        description="The FOLDER date, %d-%m-%Y (e.g. 18-08-2026) — not ISO. Names the folder."
    )


class PostTradeUploadResponse(BaseModel):
    upload_id: str
    file_name: str
    trade_date: str
    guid: str = Field(description="The upload folder GUID CBOS binds the chunks and the entry by")
    rule_name: str = Field(description="CBOS's own name for this UploadID, echoed for the log")
    status: str = "uploaded"
