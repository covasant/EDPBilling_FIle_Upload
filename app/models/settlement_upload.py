from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SettlementUpload(Base):
    """Audit log for one settlement (DP File Upload API) upload attempt - one
    row per POST /settlements/uploads call. Mirrors uploaded_files' audit-log
    shape (app/models/uploaded_file.py), with DP-upload-specific fields in
    place of CBOS trade-upload's. See app/clients/dp_upload_client.py and
    app/services/settlement_service.py for the 7-step flow this records.

    No unique constraint on file_name: v1 allows re-uploading the same file
    freely - idempotency is the calling orchestrator's job, not this table's.
    """

    __tablename__ = "settlement_uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    upload_id: Mapped[str] = mapped_column(String, nullable=False)
    upload_display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    depository: Mapped[str | None] = mapped_column(String, nullable=True)

    file_name: Mapped[str] = mapped_column(String, nullable=False)
    guid: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # per-session UUID prefixed onto file_name for uploadchunks (Step 4)
    unique_identifier: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # unix-timestamp string shared across Steps 3/4/5
    chunk_full_path: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # FileName returned by the last uploadchunks response, echoed into Step 5

    tran_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Tran_Id from uploadfilemaster (Step 5), used for status polling (Step 6)
    process_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # echoed from Step 2 config; gates Step 7

    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending"
    )  # pending|validating|uploading|uploaded|registered|polling|success|processed|failed|timed_out
    last_step: Mapped[str | None] = mapped_column(String, nullable=True)
    status_code: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # GetFileUploadStatus's 0-4 code

    total_chunks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunks_uploaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )  # restart-from-Step-3 attempts on chunk failure

    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    request_log: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON list of {step, request/response} for every DP upload API call made
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
