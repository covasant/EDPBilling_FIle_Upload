import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import UploadedFile

logger = logging.getLogger("uploaded_file_repository")


class UploadedFileRepository:
    """Audit-log writer for the uploaded_files table. This is pure
    record-keeping - nothing here drives skip/retry/dedup decisions. Every
    processing attempt gets its own row; dedup is handled entirely by the
    filesystem (a moved file can't be rediscovered) and the in-memory
    in-flight guard in app/core/queue.py. Callers own the Session lifecycle
    (create it, commit/close it) - this class only knows how to write rows
    through the session it's given."""

    def __init__(self, session: Session):
        self.session = session

    def insert(self, **fields) -> UploadedFile:
        record = UploadedFile(**fields)
        self.session.add(record)
        self.session.flush()
        logger.debug("insert: new record id=%s file_path=%s status=%s", record.id, record.file_path, record.status)
        return record

    def create_audit_record(self, file_path, folder_date: str, segment: str, exchange: str) -> UploadedFile:
        """Called once per file at the start of batch processing - one
        fresh 'pending' audit row per processing attempt, no lookup/reuse
        of prior rows."""
        return self.insert(
            file_name=Path(file_path).name,
            file_path=str(file_path),
            folder_date=folder_date,
            segment=segment,
            exchange=exchange,
            status="pending",
        )

    def create_pending_record(self, file_path, folder_date: str, segment: str, exchange: str) -> UploadedFile:
        """Used by POST /upload - one fresh 'pending' audit row for the
        manually-saved file."""
        record = self.insert(
            file_name=Path(file_path).name,
            file_path=str(file_path),
            folder_date=folder_date,
            segment=segment,
            exchange=exchange,
            status="pending",
        )
        self.commit()
        self.session.refresh(record)
        return record

    def update(self, record: UploadedFile, **fields) -> UploadedFile:
        logger.debug("update: record id=%s <- %s", record.id, fields)
        for key, value in fields.items():
            setattr(record, key, value)
        return record

    def commit(self) -> None:
        self.session.commit()
        logger.debug("commit: transaction committed")
