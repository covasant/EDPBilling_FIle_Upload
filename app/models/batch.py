from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from app.core.database import Base


class Batch(Base):
    """One manifest-declared upload batch (see docs/BATCH_HANDOFF_CONTRACT.md).

    Unlike uploaded_files (write-mostly audit), this table IS read for two
    decisions: POST /batches idempotency (a known batch_id is acknowledged,
    not re-queued) and POST /batches/rescan (a manifest whose batch_id is
    already here is not re-queued). Everything else about it is status
    reporting for GET /batches/{batch_id}.
    """

    __tablename__ = "batches"
    __table_args__ = (UniqueConstraint("batch_id", name="uq_batches_batch_id"),)

    id = Column(Integer, primary_key=True)
    batch_id = Column(String, nullable=False)          # e.g. MCX-2026-07-20-a3f8c2d1
    segment = Column(String, nullable=False)
    trade_date = Column(String, nullable=False)        # ISO YYYY-MM-DD (manifest form)
    folder_date = Column(String, nullable=False)       # DD-MM-YYYY (folder form)
    manifest_path = Column(String, nullable=False)
    correlation_id = Column(String, nullable=True)

    # queued -> uploading -> confirmed | unconfirmed | incomplete | failed.
    # rejected = checksum/schema failure at intake (never queued).
    status = Column(String, nullable=False, default="queued")
    status_detail = Column(Text, nullable=True)        # JSON, e.g. missing_slots for incomplete

    # Audited force-proceed (POST /batches/{batch_id}/proceed).
    proceed_slots = Column(Text, nullable=True)        # JSON list of ops-chosen UploadIDs
    proceed_reason = Column(Text, nullable=True)
    proceeded_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                        nullable=False)
