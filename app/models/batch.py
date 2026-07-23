from datetime import UTC, datetime

from edpb_core.batch_api import BatchStatus
from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Batch(Base):
    """One manifest-declared upload batch (see docs/BATCH_HANDOFF_CONTRACT.md).

    Unlike uploaded_files (write-mostly audit), this table IS read for two
    decisions: POST /batches idempotency (a known batch_id is acknowledged,
    not re-queued blindly) and rescan (known batch_ids are only re-queued
    while still QUEUED). Everything else about it is status reporting for
    GET /batches/{batch_id}.

    status holds edpb_core.batch_api.BatchStatus values (stored as strings;
    the vocabulary is shared with the EDP_Billing engine so the two sides
    cannot drift).
    """

    __tablename__ = "batches"
    __table_args__ = (UniqueConstraint("batch_id", name="uq_batches_batch_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String)          # e.g. MCX-2026-07-20-a3f8c2d1
    segment: Mapped[str] = mapped_column(String)
    trade_date: Mapped[str] = mapped_column(String)        # ISO YYYY-MM-DD (manifest form)
    folder_date: Mapped[str] = mapped_column(String)       # DD-MM-YYYY (folder form)
    manifest_path: Mapped[str] = mapped_column(String)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)

    status: Mapped[str] = mapped_column(String, default=BatchStatus.QUEUED)
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Audited force-proceed (POST /batches/{batch_id}/proceed).
    proceed_slots: Mapped[str | None] = mapped_column(Text, nullable=True)
    proceed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    proceeded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
