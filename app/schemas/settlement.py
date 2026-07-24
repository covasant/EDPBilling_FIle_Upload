from pydantic import BaseModel, Field


class SettlementUploadRequest(BaseModel):
    """POST /settlements/uploads body. No file bytes - the settlement
    download bot has already placed the file on the shared folder
    (settings.cbos_setl_shared_folder_path); this service looks it up by
    file_name."""

    upload_id: int = Field(description="DP upload master Upload_Id for this file type")
    file_name: str = Field(description="Name of the file already on the shared folder")
    correlation_id: str | None = Field(default=None, description="End-to-end run id, if the caller has one")


class SettlementUploadResponse(BaseModel):
    settlement_upload_id: int
    upload_id: str
    tran_id: str | None
    status: str
    final_step: str | None
    detail: str | None
    correlation_id: str | None
