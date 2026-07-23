from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from app.core.database import Base


class UploadedFile(Base):
    """Pure audit log for one CBOS upload attempt. Nothing reads this table
    to make skip/retry/dedup decisions - it exists purely as a record of
    what was attempted and what CBOS said. See
    repositories/uploaded_file_repository.py."""

    __tablename__ = "uploaded_files"
    __table_args__ = (UniqueConstraint("file_path", name="uq_uploaded_files_file_path"),)

    id = Column(Integer, primary_key=True)
    file_name = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    folder_date = Column(String, nullable=False)
    segment = Column(String, nullable=False)
    exchange = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending")  # pending | uploaded | failed
    cbos_response = Column(String, nullable=True)   # final outcome (Step 7 result, or the error that failed the sequence)
    retry_count = Column(Integer, nullable=False, default=0)

    # CBOS trade-upload API tracking (see cbos_client.py Steps 1-9)
    process_id = Column(String, nullable=True)      # PROCESSID from getNewTradeProcess (Step 2), shared by every file in the batch
    cbos_upload_id = Column(String, nullable=True)  # UPLOADID resolved for THIS file by upload_matching.match_file (Step 4)
    matched_pattern = Column(String, nullable=True)  # the UploadID's NAME/pattern this file matched against, for audit
    cbos_upload_settings = Column(Text, nullable=True)  # full raw Step 4 (GetNewTradeProcessPromodalUploadSettings) response for the matched UploadID
    guid = Column(String, nullable=True)            # upload folder GUID used for chunking (Step 5) + registration (Step 7)
    correlation_id = Column(String, nullable=True)  # manifest's end-to-end run id (ticket 11)
    validation_error = Column(Text, nullable=True)  # why upload_matching rejected this file (no match / column mismatch), if applicable
    request_log = Column(Text, nullable=True)       # JSON list of {step, request/response} for every CBOS call made

    uploaded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
