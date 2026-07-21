import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import UploadedFile

logger = logging.getLogger("uploaded_file_repository")

# Appended to a superseded row's file_path so it stays UNIQUE without pretending
# to be a location on disk. Contains characters Windows forbids in a path, so it
# can never collide with a real file. See claim_file_path.
_SUPERSEDED_MARKER = " <superseded by row "


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

    def claim_file_path(self, record: UploadedFile, new_path) -> None:
        """Point `record` at new_path, first releasing any OTHER row that still
        claims it.

        file_path is UNIQUE, so two rows can never name the same location. That
        normally holds by itself: a file lives at one path, and the row that owns
        it moves with it. It breaks when a human intervenes between runs -
        move a file back out of uploaded/ to retry it, and the source folder is
        occupied again while the earlier run's row still records the uploaded/
        path. This attempt then finishes, moves the file to that same uploaded/
        path, and two rows want it.

        _move_file guards the FILESYSTEM against this (it suffixes _2 if a file
        is already sitting at the destination), but a moved-away file leaves the
        destination empty, so that guard never fires and only the UNIQUE
        constraint notices - as an IntegrityError mid-batch that kills every
        remaining file in it.

        The older row is stale, not wrong: its file genuinely was at that path
        once, and the audit trail should keep saying so. So it is retired rather
        than deleted - its path gets a marker that no real path can collide with,
        which frees the name for the row that now owns the file on disk.
        """
        new_path = str(new_path)
        stale = (
            self.session.query(UploadedFile)
            .filter(UploadedFile.file_path == new_path, UploadedFile.id != record.id)
            .one_or_none()
        )
        if stale is not None:
            retired = f"{new_path}{_SUPERSEDED_MARKER}{record.id}>"
            logger.warning(
                "claim_file_path: row id=%s still claimed %s (its file was moved away between runs); "
                "retiring that claim as %s so row id=%s can take the path",
                stale.id, new_path, retired, record.id,
            )
            stale.file_path = retired
            self.session.flush()

        record.file_path = new_path
        self.session.flush()

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
