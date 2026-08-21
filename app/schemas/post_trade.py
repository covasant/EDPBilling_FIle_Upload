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
        description="Name of the file already under <root>/<trade_date>/POSTTRADE/[<folder>/]"
    )
    folder: str = Field(
        default="",
        description=(
            "The process subfolder the download bot filed this file into — `COLVAL`, "
            "`COLALLOC`, or `COMMON` for a file both processes need. The bot MOVES a required "
            "file there, so the flat POSTTRADE root holds only what belongs to no process.\n\n"
            "Optional: omit it and the root is searched, which is what older dates and "
            "hand-placed files need. Naming it searches the folder first and the root second, "
            "so a caller is never worse off for passing it."
        ),
    )
    trade_date: str = Field(
        description="The FOLDER date, %d-%m-%Y (e.g. 18-08-2026) — not ISO. Names the folder."
    )
    segment: str = Field(
        default="",
        description=(
            "The file's own segment, as CBOS reports it in steps 41/42 (EQ, MF, DR, CUR, "
            "MCX, NCDEX, NSECOM). Only used for the Step 40 lookup behind translate_name; "
            "defaults to EQ."
        ),
    )
    translate_name: bool = Field(
        default=False,
        description=(
            "Declare a DIFFERENT name to CBOS from the one on disk, derived from CBOS's own "
            "Step 40 patterns. For files where CBOS's expected name cannot match what the "
            "exchange publishes — the two UDIFF bhavcopies, where CBOS wants DDMMYYYY and "
            "the exchange publishes YYYYMMDD. **Opt in per file, never globally**: for the "
            "files whose pattern matches several real files a rename would let the wrong "
            "one through. Bytes and the on-disk name are untouched; both names are logged."
        ),
    )


class PostTradeUploadResponse(BaseModel):
    upload_id: str
    file_name: str
    trade_date: str
    guid: str = Field(description="The upload folder GUID CBOS binds the chunks and the entry by")
    declared_name: str = Field(
        default="",
        description=(
            "The name DECLARED to CBOS, when translate_name changed it. Empty when it "
            "equals file_name. Reported rather than only logged: this is the one point "
            "where what CBOS holds stops matching what is on disk, and a caller "
            "reconciling the two needs to see both."
        ),
    )
    rule_name: str = Field(description="CBOS's own name for this UploadID, echoed for the log")
    status: str = "uploaded"
