import logging

from sqlalchemy.orm import Session

from app.models.settlement_upload import STATUSES, SettlementUpload

logger = logging.getLogger("settlement_upload_repository")



def _check_status(fields: dict) -> None:
    """Reject a status outside the model's vocabulary.

    update() is a blind setattr over **fields, so a typo'd literal used to be written
    without complaint. Because the state checks elsewhere match the exact string, that
    row then became invisible to every state-based query — silently, and permanently.
    Raising here turns a silent data-integrity bug into an immediate, obvious one.
    """
    status = fields.get("status")
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {sorted(STATUSES)}")


class SettlementUploadRepository:
    """Reader/writer for the settlement_uploads table. Pure audit log - no
    idempotency lookups (v1 allows re-uploading the same file_name freely,
    see SettlementUpload's docstring). Callers own the Session lifecycle
    (create it, commit/close it), same convention as UploadedFileRepository."""

    def __init__(self, session: Session):
        self.session = session

    def insert(self, **fields) -> SettlementUpload:
        record = SettlementUpload(**fields)
        self.session.add(record)
        self.session.flush()
        logger.debug(
            "insert: new record id=%s upload_id=%s file_name=%s status=%s",
            record.id,
            record.upload_id,
            record.file_name,
            record.status,
        )
        return record

    def get(self, settlement_upload_id: int) -> SettlementUpload | None:
        return self.session.get(SettlementUpload, settlement_upload_id)

    def update(self, record: SettlementUpload, **fields) -> SettlementUpload:
        _check_status(fields)
        logger.debug("update: record id=%s <- %s", record.id, fields)
        for key, value in fields.items():
            setattr(record, key, value)
        self.session.flush()
        return record

    def commit(self) -> None:
        self.session.commit()
        logger.debug("commit: transaction committed")
