import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import UploadedFile

logger = logging.getLogger("uploaded_file_repository")


class UploadedFileRepository:
    """Reader/writer for the uploaded_files table. Mostly an audit log, with
    one deliberate exception: find_completed() reads it for idempotency (so a
    re-dropped, already-uploaded file isn't sent to CBOS twice - see ADR 6).
    Callers own the Session lifecycle (create it, commit/close it)."""

    def __init__(self, session: Session):
        self.session = session

    def insert(self, **fields) -> UploadedFile:
        record = UploadedFile(**fields)
        self.session.add(record)
        self.session.flush()
        logger.debug("insert: new record id=%s file_path=%s status=%s", record.id, record.file_path, record.status)
        return record

    def create_audit_record(self, file_path, folder_date: str, segment: str, exchange: str) -> UploadedFile:
        """Get-or-create the audit row for this file_path, then reset it to
        'pending' for a fresh attempt. Idempotent: if a prior attempt left a row
        at this exact source path (a crash before the file was moved, or a manual
        POST /upload followed by discovery), reuse it instead of inserting a
        duplicate - the file_path UNIQUE constraint would otherwise raise
        (the bug that made every such file reprocess forever)."""
        existing = self.session.query(UploadedFile).filter_by(file_path=str(file_path)).one_or_none()
        if existing is not None:
            logger.debug("create_audit_record: reusing existing row id=%s for %s", existing.id, file_path)
            existing.status = "pending"
            existing.folder_date = folder_date
            existing.segment = segment
            existing.exchange = exchange
            self.session.flush()
            return existing
        return self.insert(
            file_name=Path(file_path).name,
            file_path=str(file_path),
            folder_date=folder_date,
            segment=segment,
            exchange=exchange,
            status="pending",
        )

    def find_completed(self, segment: str, folder_date: str, upload_id, file_name: str) -> UploadedFile | None:
        """Idempotency lookup: a prior row for this (segment, date, UploadID,
        file name) that already reached 'uploaded'. If present, the file is
        already in CBOS and must not be re-uploaded."""
        return (
            self.session.query(UploadedFile)
            .filter_by(
                segment=segment,
                folder_date=folder_date,
                cbos_upload_id=str(upload_id),
                file_name=file_name,
                status="uploaded",
            )
            .first()
        )

    def update(self, record: UploadedFile, **fields) -> UploadedFile:
        logger.debug("update: record id=%s <- %s", record.id, fields)
        for key, value in fields.items():
            setattr(record, key, value)
        return record

    def commit(self) -> None:
        self.session.commit()
        logger.debug("commit: transaction committed")
