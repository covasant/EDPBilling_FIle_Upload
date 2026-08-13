from pydantic import BaseModel, Field


class NsdlSpeedeFileSelector(BaseModel):
    """One of the 12 SPEED-e reports, named the way the download bot names
    them - never a path and never an UPLOADID. The service resolves both."""

    account: str = Field(description="SPEED-e account code: CMPA, CMFA or NARNO")
    report: str = Field(
        description="Report label: 'OPEN HOLDING', 'pledge', 'unpledge' or 'confiscate'"
    )


class NsdlSpeedeUploadRequest(BaseModel):
    """POST /settlement/nsdl_speede_upload body. No file bytes and no file
    names - the SPEED-e download bot has already placed the 12 reports on the
    shared folder (settings.nsdl_speede_shared_folder_path) under their
    "NSDL <code> <label> <n>.csv" names; the service picks whichever <n> is
    highest for each report."""

    # Pattern-checked because this string does three jobs - call 1's DATE,
    # call 4's PARAM1 (what the stored procedure runs against), and the dated
    # download folder - and a DD-MM-YYYY slip would silently resolve to the
    # wrong folder or the wrong business date.
    trade_date: str = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Business date, YYYY-MM-DD. Feeds call 1's DATE, call 4's PARAM1, "
        "and the nsdl_speede_<DDMMYYYY> folder the files are read from",
    )
    files: list[NsdlSpeedeFileSelector] | None = Field(
        default=None,
        description="Which reports to upload. Omit for all 12, in the fixed catalogue order.",
    )
    force: bool = Field(
        default=False,
        description=(
            "Re-upload the selected files even though a TRANID already exists for them "
            "(including one stuck 'in_progress' indefinitely). This API has no 'replace' "
            "call, only 'append': if the stuck/earlier attempt already wrote any rows, "
            "this WILL duplicate them in NSDL. Use only to break a file out of a TRANID "
            "that will never resolve — never as a routine retry."
        ),
    )
    correlation_id: str | None = Field(
        default=None, description="The orchestrator's run id, stamped on every log line for this call"
    )


class NsdlSpeedeFileResult(BaseModel):
    settlement_upload_id: int
    account: str
    report: str
    file_name: str
    upload_name: str | None
    upload_id: int | None
    transmit_file_name: str | None
    guid: str | None
    tran_id: str | None
    total_chunks: int | None
    data_bytes: int | None
    upload_status: str | None
    process_status: str | None
    status: str
    detail: str | None


class NsdlSpeedeUploadSummary(BaseModel):
    total: int
    success: int
    in_progress: int
    failed: int


class NsdlSpeedeUploadResponse(BaseModel):
    trade_date: str
    status: str  # success | partial | in_progress | failed
    summary: NsdlSpeedeUploadSummary
    files: list[NsdlSpeedeFileResult]
    correlation_id: str | None
