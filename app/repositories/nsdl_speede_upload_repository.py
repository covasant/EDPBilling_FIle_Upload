import logging

from sqlalchemy.orm import Session

from app.models.nsdl_speede_upload import NsdlSpeedeUpload

logger = logging.getLogger("nsdl_speede_upload_repository")


class NsdlSpeedeUploadRepository:
    """Reader/writer for the nsdl_speede_uploads table.

    Unlike SettlementUploadRepository this is NOT a pure audit log: find() is
    the idempotency lookup the service runs before every upload, because the
    NSDL Speedy API appends and a second finalize would duplicate every row
    (see NsdlSpeedeUpload's docstring). Callers own the Session lifecycle,
    same convention as the other repositories.
    """

    def __init__(self, session: Session):
        self.session = session

    def find(self, trade_date: str, account: str, report: str) -> NsdlSpeedeUpload | None:
        """The row for one file on one day, if it exists - the answer to "has
        this already been sent?"."""
        return (
            self.session.query(NsdlSpeedeUpload)
            .filter(
                NsdlSpeedeUpload.trade_date == trade_date,
                NsdlSpeedeUpload.account == account,
                NsdlSpeedeUpload.report == report,
            )
            .one_or_none()
        )

    def get(self, nsdl_upload_id: int) -> NsdlSpeedeUpload | None:
        return self.session.get(NsdlSpeedeUpload, nsdl_upload_id)

    def insert(self, **fields) -> NsdlSpeedeUpload:
        record = NsdlSpeedeUpload(**fields)
        self.session.add(record)
        self.session.flush()
        logger.debug(
            "insert: new record id=%s %s/%s/%s status=%s",
            record.id,
            record.trade_date,
            record.account,
            record.report,
            record.status,
        )
        return record

    def update(self, record: NsdlSpeedeUpload, **fields) -> NsdlSpeedeUpload:
        logger.debug("update: record id=%s <- %s", record.id, fields)
        for key, value in fields.items():
            setattr(record, key, value)
        self.session.flush()
        return record

    def commit(self) -> None:
        self.session.commit()
        logger.debug("commit: transaction committed")
