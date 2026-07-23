"""Data access for the batches table (see app/models/batch.py).

Mirrors UploadedFileRepository's session-owned style: the caller supplies the
session and decides transaction boundaries; helpers here stay thin.
"""

import json
import logging
from datetime import UTC, datetime

from edpb_core.batch_api import BatchStatus
from sqlalchemy.orm import Session

from app.models.batch import Batch

logger = logging.getLogger("batch_repository")


class BatchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def find_by_batch_id(self, batch_id: str) -> Batch | None:
        return self.session.query(Batch).filter(Batch.batch_id == batch_id).one_or_none()

    def create(self, *, batch_id: str, segment: str, trade_date: str, folder_date: str,
               manifest_path: str, correlation_id: str | None,
               status: BatchStatus = BatchStatus.QUEUED,
               status_detail: str | None = None) -> Batch:
        batch = Batch(
            batch_id=batch_id, segment=segment, trade_date=trade_date,
            folder_date=folder_date, manifest_path=manifest_path,
            correlation_id=correlation_id, status=status, status_detail=status_detail,
        )
        self.session.add(batch)
        self.session.commit()
        logger.info("Batch %s recorded (status=%s)", batch_id, status)
        return batch

    def set_status(self, batch: Batch, status: BatchStatus, detail: dict | None = None) -> None:
        batch.status = status
        if detail is not None:
            batch.status_detail = json.dumps(detail, default=str)
        self.session.commit()
        logger.info("Batch %s -> %s%s", batch.batch_id, status, f" {detail}" if detail else "")

    def record_proceed(self, batch: Batch, slots: list[str], reason: str) -> None:
        batch.proceed_slots = json.dumps(slots)
        batch.proceed_reason = reason
        batch.proceeded_at = datetime.now(UTC)
        self.session.commit()
        logger.info("Batch %s: force-proceed recorded (slots=%s, reason=%r)",
                    batch.batch_id, slots, reason)
