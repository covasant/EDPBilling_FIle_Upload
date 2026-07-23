from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UploadedFile(Base):
    """Pure audit log for one CBOS upload attempt. Nothing reads this table
    to make skip/retry/dedup decisions - it exists purely as a record of
    what was attempted and what CBOS said. See
    repositories/uploaded_file_repository.py."""

    __tablename__ = "uploaded_files"
    __table_args__ = (UniqueConstraint("file_path", name="uq_uploaded_files_file_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_name: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    folder_date: Mapped[str] = mapped_column(String, nullable=False)
    segment: Mapped[str] = mapped_column(String, nullable=False)
    exchange: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending"
    )  # pending | uploaded | failed
    cbos_response: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # final outcome (Step 7 result, or the error that failed the sequence)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # CBOS trade-upload API tracking (see cbos_client.py Steps 1-9)
    process_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # PROCESSID from getNewTradeProcess (Step 2), shared by every file in the batch
    cbos_upload_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # UPLOADID resolved for THIS file by upload_matching.match_file (Step 4)
    matched_pattern: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # the UploadID's NAME/pattern this file matched against, for audit
    cbos_upload_settings: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # full raw Step 4 (upload settings) response for the matched UploadID
    guid: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # upload folder GUID used for chunking (Step 5) + registration (Step 7)
    correlation_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # manifest's end-to-end run id (ticket 11)
    batch_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # the manifest batch this attempt belonged to
    validation_error: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # why upload_matching rejected this file (no match / column mismatch), if applicable
    request_log: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON list of {step, request/response} for every CBOS call made

    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
