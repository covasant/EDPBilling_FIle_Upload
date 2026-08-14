from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _utcnow() -> datetime:
    """Same helper as app/models/batch.py. Not settlement_upload.py's
    datetime.utcnow, which 3.12 deprecates."""
    return datetime.now(UTC)


class NsdlSpeedeUpload(Base):
    """One SPEED-e report's journey into the NSDL Speedy upload API - see
    app/clients/nsdl_speede_client.py and app/services/nsdl_speede_service.py.

    Unlike settlement_uploads (a pure audit log, where re-uploading the same
    file freely is fine), this table is LOAD-BEARING for correctness. The NSDL
    Speedy API appends rather than replaces: finalizing the same file twice
    duplicates every row it carries, with no error from either side. So the
    unique constraint below is the record of "this file has already been sent",
    and the service consults it before every upload - a workflow retry re-polls
    the stored tran_id instead of chunking the file again.

    One row per (trade_date, account, report, file_version) - the download bot
    never overwrites a same-day report, it numbers each trigger's file instead
    ("NSDL <code> <label> <n>.csv", see the download repo's
    src/portals/nsdl_speede/reports.py), and operations re-triggers this portal
    many times a day. So "already uploaded" has to mean "this exact numbered
    file", not "something for this (account, report) today": a later trigger
    that produced a NEW version must still upload, even though an earlier
    version of the same report already succeeded today. file_version is NULL
    on rows predating this column, and on a row created for a file that
    couldn't be located at all (there's no version to key on yet - see
    _process_entry). retry_count carries how many attempts one exact version
    took. Three accounts' pledge files share one UPLOADID, which is exactly
    why the key is the (account, report) pair and not the upload_id.
    """

    __tablename__ = "nsdl_speede_uploads"
    __table_args__ = (
        UniqueConstraint(
            "trade_date", "account", "report", "file_version", name="uq_nsdl_speede_file_per_day"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    trade_date: Mapped[str] = mapped_column(String, nullable=False)  # YYYY-MM-DD
    account: Mapped[str] = mapped_column(String, nullable=False)  # CMPA | CMFA | NARNO
    report: Mapped[str] = mapped_column(
        String, nullable=False
    )  # OPEN HOLDING | pledge | unpledge | confiscate
    # The "<n>" from the file the download bot wrote - see _locate() in
    # nsdl_speede_service.py. NULL for pre-versioning rows and for a row
    # created because the file couldn't be located at all.
    file_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    upload_name: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # the UPLOADNAME matched in call 1 - stable across UAT/prod, unlike the id
    upload_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # resolved from upload_name per run; recorded for traceability only

    file_name: Mapped[str] = mapped_column(String, nullable=False)  # on disk, as the bot wrote it
    transmit_file_name: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # what was sent as FileName/UPLOADFILENAME - carries the API's required token

    guid: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # per-upload GUID shared by every chunk, echoed to finalize as UPLOADFOLDERNAME
    tran_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # TRANID from finalize - the re-poll handle, and the "already sent" marker

    total_chunks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data_bytes: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # bytes actually sent, i.e. file size less the stripped header

    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending"
    )  # pending|validating|uploading|uploaded|registered|polling|success|in_progress|failed
    last_step: Mapped[str | None] = mapped_column(String, nullable=True)

    upload_status: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # call 5's UPLOAD STATUS, HTML stripped
    process_status: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # call 5's PROCESS STATUS, HTML stripped

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )
