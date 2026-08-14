"""Orchestrates the NSDL Speedy upload (calls 1-5 of
app/clients/nsdl_speede_client.py) for one or more of the 12 SPEED-e Margin
Pledge reports, synchronously, for POST /settlement/nsdl_speede_upload.

Same call-and-get-result contract as settlement_service.process_upload: the
settlement automation orchestrator owns scheduling, retry and SLA. In practice
its workflow.json drives this one file per step, so a call carries a single
selector and the whole 12 is only used for manual re-runs - but the catalogue
order below is the sequence either way.

--------------------------------------------------------------------------
Why the DB row is consulted before every upload
--------------------------------------------------------------------------

This API APPENDS. Finalizing the same file twice inserts every one of its rows
a second time, and neither side reports a problem - the second call returns a
fresh TRANID and a cheerful SUCCESS. Since the orchestrator retries a step
whose response was not `status: success` (and a slow stored procedure returns
`in_progress`), a naive implementation would duplicate a 56,000-row pledge
file on the first slow day.

So _resolve_existing() runs first, and a row that already carries a tran_id
never reaches finalize_upload() again - it is re-polled instead. That is the
single most important rule in this module.

--------------------------------------------------------------------------
Why UPLOADID is never hardcoded
--------------------------------------------------------------------------

The ids in the API doc are UAT's and will differ in production; the UPLOADNAMEs
will not. So the catalogue maps (account, report) -> UPLOADNAME, and call 1
resolves the name to today's id on every request. An unresolvable name fails
that file loudly rather than guessing a number.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.clients.nsdl_speede_client import (
    POLL_FAILED,
    POLL_IN_PROGRESS,
    POLL_SUCCESS,
    BaseNsdlSpeedeClient,
    NsdlSpeedeError,
    UploadCategory,
    UploadSettings,
    get_nsdl_speede_client,
)
from app.core.config import settings
from app.core.correlation import batch_context
from app.models.nsdl_speede_upload import NsdlSpeedeUpload
from app.repositories.nsdl_speede_upload_repository import NsdlSpeedeUploadRepository

logger = logging.getLogger("nsdl_speede_service")


class UnknownNsdlSpeedeUploadError(Exception):
    """No nsdl_speede_uploads row exists for the given id."""


class UnknownSpeedeFileError(Exception):
    """The requested (account, report) pair is not one of the 12."""


# --------------------------------------------------------------------------
# The catalogue: the 12 files the SPEED-e bot produces, in upload order.
#
# Open Holdings has one category per account (the depository keeps CMPA, CMFA
# and NARNO holdings apart); pledge/unpledge/confiscate do not - all three
# accounts feed the same category, three separate appends. Order is
# account-major so the shared categories are never fed concurrently.
# --------------------------------------------------------------------------

ACCOUNTS = ("CMPA", "CMFA", "NARNO")
OPEN_HOLDING = "OPEN HOLDING"

# The reports each account contributes, in order. Matches the download bot's
# ReportStep.file_label values (src/portals/nsdl_speede/constants.py).
REPORTS = (OPEN_HOLDING, "pledge", "unpledge", "confiscate")

# Categories shared by all three accounts, keyed by report label.
_SHARED_UPLOAD_NAMES = {
    "pledge": "NSDL MARGIN PLEDGE",
    "unpledge": "NSDL MARGIN UNPLEDGE",
    "confiscate": "NSDL MARGIN CONFISCATE",
}


@dataclass(frozen=True)
class SpeedeFile:
    """One catalogue entry: what the bot wrote, and which category it feeds."""

    account: str
    report: str
    upload_name: str
    file_name: str

    @property
    def key(self) -> str:
        return f"{self.account}/{self.report}"


def _upload_name(account: str, report: str) -> str:
    if report == OPEN_HOLDING:
        return f"NSDL {account} OPEN HOLDING"
    return _SHARED_UPLOAD_NAMES[report]


CATALOGUE: tuple[SpeedeFile, ...] = tuple(
    SpeedeFile(
        account=account,
        report=report,
        upload_name=_upload_name(account, report),
        # The catalogue's base name, e.g. "NSDL CMFA confiscate.csv" - a
        # display default and the seed for _transmit_name's fallback token.
        # It is NOT what's read from disk: the download bot numbers every
        # file it writes ("NSDL <code> <label> <n>.csv", never overwriting -
        # see _report_path in the download repo's
        # src/portals/nsdl_speede/reports.py), so _locate() below always
        # resolves to whichever "<n>" is highest on disk for this entry.
        file_name=f"NSDL {account} {report}.csv",
    )
    for account in ACCOUNTS
    for report in REPORTS
)

_BY_KEY = {(entry.account.upper(), entry.report.lower()): entry for entry in CATALOGUE}


def resolve_selection(selectors: list[dict] | None) -> list[tuple[SpeedeFile, int | None]]:
    """The catalogue entries a request asks for, paired with the version
    requested for each (None = latest - see _locate), in catalogue order.
    None or an empty list means all 12, each at its latest version.

    A selector may repeat the same (account, report) with a different version
    each time - e.g. to upload both "... 2.csv" and "... 3.csv" of the same
    report in one call - so the same catalogue entry can appear more than once
    in the result.
    """
    if not selectors:
        return [(entry, None) for entry in CATALOGUE]

    requested: dict[SpeedeFile, list[int | None]] = {}
    for selector in selectors:
        account = str(selector.get("account", "")).strip().upper()
        report = str(selector.get("report", "")).strip().lower()
        entry = _BY_KEY.get((account, report))
        if entry is None:
            raise UnknownSpeedeFileError(
                f"unknown SPEED-e file account={selector.get('account')!r} "
                f"report={selector.get('report')!r} - expected one of "
                f"{sorted({e.report for e in CATALOGUE})} for {list(ACCOUNTS)}"
            )
        requested.setdefault(entry, []).append(selector.get("version"))

    # Catalogue order regardless of how the caller listed them, so a multi-file
    # call can never interleave two DIFFERENT entries' rows into one shared
    # category. Multiple versions of the SAME entry are the same category
    # either way, so they're just emitted together, in the order requested.
    return [
        (entry, version)
        for entry in CATALOGUE
        if entry in requested
        for version in requested[entry]
    ]


def _normalise(name: str) -> str:
    """Category names are matched case- and whitespace-insensitively: the API's
    own list mixes casing ("NSDL COD" next to "NSDL Holding (SOH)"), and a
    stray double space should not cost us a file."""
    return " ".join(str(name).split()).upper()


def _resolve_category(entry: SpeedeFile, categories: list[UploadCategory]) -> UploadCategory:
    wanted = _normalise(entry.upload_name)
    for category in categories:
        if _normalise(category.upload_name) == wanted:
            return category
    raise NsdlSpeedeError(
        f"{entry.key}: no category named {entry.upload_name!r} in getcommonuploadbydate "
        f"(saw {[c.upload_name for c in categories]})"
    )


def _transmit_name(entry: SpeedeFile, config: UploadSettings) -> str:
    """The name sent as FileName / UPLOADFILENAME.

    It has to contain the API's FILE NAME (CONTAINS) token, which the on-disk
    name deliberately does not: the bot's names are account-qualified and
    stable, while the portal's are neither (Open Holdings carries a generation
    timestamp, and login_1/login_3 both produce "..._TMCM.csv"). Since the
    transmitted name is independent of the file it is read from, it is built
    from the live token instead - so a token change on CBOS's side needs no
    change here - with the account appended to keep LASTUPLOADFILENAME and the
    history log unambiguous where three accounts feed one category.
    """
    token = config.file_name_contains or Path(entry.file_name).stem
    extension = config.file_extension or "csv"
    return f"{token}_{entry.account}.{extension}"


def day_folder(trade_date: str) -> Path:
    """The dated folder this trade_date's files live in.

    The download bot writes each run into its own
    <root>/nsdl_speede_<DDMMYYYY>/ (see the download repo's
    src/portals/nsdl_speede/run.py), so the configured path is the ROOT and the
    day is appended here. Both sides derive the folder from the same
    trade_date, which is what keeps them in step without either importing the
    other's naming.

    NSDL_SPEEDE_DATE_FOLDER_FORMAT="" reads straight out of the root, for a
    folder assembled by hand.
    """
    root = Path(settings.nsdl_speede_shared_folder_path)
    pattern = settings.nsdl_speede_date_folder_format.strip()
    if not pattern:
        return root
    try:
        day = datetime.strptime(trade_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            f"trade_date {trade_date!r} is not YYYY-MM-DD, so the dated folder "
            f"({pattern}) cannot be resolved"
        ) from exc
    return root / day.strftime(pattern)


def _locate(
    entry: SpeedeFile, trade_date: str, version: int | None = None
) -> tuple[Path, int]:
    """The file for this entry, and its version number.

    With `version=None` (the default): the highest-numbered
    "NSDL <code> <label> <n>.csv" on disk. The download bot never overwrites -
    each trigger writes its own numbered copy (see _report_path in the
    download repo's src/portals/nsdl_speede/reports.py). So "today's file"
    isn't a fixed name; it's whichever "<n>" is highest on disk right now.
    This is re-derived from the folder on every call - nothing here counts how
    many times the bot or this upload endpoint has been triggered - so a
    caller never has to say "this is the 3rd run"; the highest number already
    on disk says that for them.

    With an explicit `version`: that EXACT file, nothing else - the escape
    hatch for uploading two specific versions of the same report in one call
    (e.g. "... 2.csv" and "... 3.csv" together), which "always take the
    latest" can't express. The version is returned alongside the path either
    way because the caller keys its own idempotency row on it too (see
    _process_entry) - no point re-deriving the same number a second time.
    """
    folder = day_folder(trade_date)
    if not folder.is_dir():
        raise FileNotFoundError(
            f"{folder} does not exist - the download bot has not run for {trade_date}, "
            f"or NSDL_SPEEDE_SHARED_FOLDER_PATH points at a day's folder rather than "
            f"its parent"
        )

    stem = f"NSDL {entry.account} {entry.report}"

    if version is not None:
        file_path = folder / f"{stem} {version}.csv"
        if not file_path.is_file():
            raise FileNotFoundError(f"'{stem} {version}.csv' not found under {folder}")
        return file_path, version

    numbered = re.compile(rf"^{re.escape(stem)} (\d+)\.csv$", re.IGNORECASE)
    candidates = [
        (int(match.group(1)), path)
        for path in folder.glob(f"{stem} *.csv")
        if (match := numbered.match(path.name))
    ]
    if not candidates:
        raise FileNotFoundError(f"no '{stem} <n>.csv' file found under {folder}")
    picked_version, file_path = max(candidates, key=lambda pair: pair[0])
    return file_path, picked_version


def _result(record: NsdlSpeedeUpload, entry: SpeedeFile, detail: str | None) -> dict:
    return {
        "settlement_upload_id": record.id,
        "account": entry.account,
        "report": entry.report,
        "file_name": record.file_name,
        "file_version": record.file_version,
        "upload_name": record.upload_name,
        "upload_id": record.upload_id,
        "transmit_file_name": record.transmit_file_name,
        "guid": record.guid,
        "tran_id": record.tran_id,
        "total_chunks": record.total_chunks,
        "data_bytes": record.data_bytes,
        "upload_status": record.upload_status,
        "process_status": record.process_status,
        "status": record.status,
        "detail": detail,
    }


_VERDICT_STATUS = {
    POLL_SUCCESS: "success",
    POLL_FAILED: "failed",
    POLL_IN_PROGRESS: "in_progress",
}


def _apply_status(repo: NsdlSpeedeUploadRepository, record: NsdlSpeedeUpload, status) -> None:
    repo.update(
        record,
        status=_VERDICT_STATUS[status.verdict],
        last_step="StatusSettlementPromodalUploadFileDetails",
        upload_status=status.upload_status,
        process_status=status.process_status,
        error_detail=status.upload_status if status.verdict == POLL_FAILED else None,
    )
    repo.commit()


def _upload_one(
    repo: NsdlSpeedeUploadRepository,
    client: BaseNsdlSpeedeClient,
    record: NsdlSpeedeUpload,
    entry: SpeedeFile,
    category: UploadCategory,
    config: UploadSettings,
    trade_date: str,
    file_path: Path,
) -> dict:
    """Calls 3-5 for one file: validate locally, chunk, finalize, poll.

    Only reached for a record with no tran_id - see _resolve_existing.
    `file_path` is whatever _process_entry already located (and keyed
    `record.file_version` on) - resolved once, not re-derived here.
    """
    transmit_name = _transmit_name(entry, config)
    repo.update(
        record,
        # Overwrite the catalogue's generic placeholder with the actual
        # numbered file _locate() picked - the row should say which one it was.
        file_name=file_path.name,
        upload_name=category.upload_name,
        upload_id=category.upload_id,
        transmit_file_name=transmit_name,
        status="validating",
        last_step="GetSettlementPromodalUploadSettings",
    )
    repo.commit()

    # The check this API does not do for us. Must happen before finalize: a
    # rejected-by-the-procedure file still consumes a TRANID and still appends
    # whatever it managed to parse.
    client.validate_file(file_path, transmit_name, config)

    repo.update(record, status="uploading", last_step="SaveSettlementPromodalUploadChunkFile")
    repo.commit()
    guid, total_chunks, data_bytes = client.upload_chunks(file_path, transmit_name)
    repo.update(
        record,
        status="uploaded",
        last_step="SaveSettlementPromodalUploadChunkFile",
        guid=guid,
        total_chunks=total_chunks,
        data_bytes=data_bytes,
    )
    repo.commit()

    # Finalize both registers the file and fires the stored procedure. The
    # tran_id is committed immediately: if the poll below dies, the row must
    # still say "this file was sent", or a retry would append it again.
    tran_id = client.finalize_upload(category.upload_id, transmit_name, guid, trade_date)
    repo.update(
        record, status="registered", last_step="SaveSettlementPromodalUploadFile", tran_id=tran_id
    )
    repo.commit()

    repo.update(record, status="polling")
    repo.commit()
    _apply_status(repo, record, client.poll_status(tran_id))
    return _result(record, entry, record.upload_status or None)


def _record_locate_failure(
    repo: NsdlSpeedeUploadRepository,
    entry: SpeedeFile,
    trade_date: str,
    correlation_id: str | None,
    exc: FileNotFoundError,
) -> dict:
    """The row for a report whose file couldn't be found at all today.

    Keyed on file_version=None, same as every other "not there yet" call for
    this (trade_date, account, report) - so retrying before the file shows up
    reuses one placeholder row instead of inserting a new one each time.
    """
    record = repo.find(trade_date, entry.account, entry.report, None)
    if record is None:
        record = repo.insert(
            trade_date=trade_date,
            account=entry.account,
            report=entry.report,
            file_version=None,
            file_name=entry.file_name,
            status="pending",
            correlation_id=correlation_id,
        )
    else:
        repo.update(record, correlation_id=correlation_id or record.correlation_id)

    logger.error("%s: %s", entry.key, exc)
    repo.update(record, status="failed", last_step="locate_file", error_detail=str(exc))
    repo.commit()
    return _result(record, entry, str(exc))


def _process_entry(
    repo: NsdlSpeedeUploadRepository,
    client: BaseNsdlSpeedeClient,
    entry: SpeedeFile,
    trade_date: str,
    correlation_id: str | None,
    categories: list[UploadCategory],
    settings_cache: dict[int, UploadSettings],
    force: bool = False,
    requested_version: int | None = None,
) -> dict:
    """One catalogue entry, end to end, never raising - a failure is this
    file's verdict, not the whole call's.

    The file is located FIRST, before touching the DB: the row this entry maps
    to is keyed on (trade_date, account, report, file_version), not just
    (trade_date, account, report) - see NsdlSpeedeUpload's docstring for why.
    `requested_version` pins that lookup to an exact "<n>" instead of
    whatever's highest on disk - see resolve_selection/_locate.
    """
    try:
        file_path, file_version = _locate(entry, trade_date, requested_version)
    except FileNotFoundError as exc:
        return _record_locate_failure(repo, entry, trade_date, correlation_id, exc)

    record = repo.find(trade_date, entry.account, entry.report, file_version)
    if record is None:
        record = repo.insert(
            trade_date=trade_date,
            account=entry.account,
            report=entry.report,
            file_version=file_version,
            file_name=file_path.name,
            status="pending",
            correlation_id=correlation_id,
        )
        repo.commit()
    else:
        repo.update(record, correlation_id=correlation_id or record.correlation_id)

    # Already finished FOR THIS EXACT FILE VERSION: answer from the row
    # without touching the API. A later trigger's newer version is a
    # DIFFERENT row (different file_version), so an earlier version's success
    # never shadows it - that's the whole point of keying on the version.
    if record.status == "success" and not force:
        logger.info(
            "%s: already uploaded on %s (version=%s, TRANID=%s)",
            entry.key,
            trade_date,
            record.file_version,
            record.tran_id,
        )
        return _result(record, entry, "already uploaded")

    try:
        if record.tran_id and force:
            # Deliberate operator override, not automatic status-based logic
            # — there's no reliable way to tell "stuck forever" from "just
            # slow" from inside this service, so the caller decides. NSDL has
            # no "replace" call, only "append": if the old TRANID's stored
            # procedure already wrote any rows (a poll stuck at in_progress
            # after partially processing, or one that later fails), this
            # retry's rows land ALONGSIDE them, not in place of them.
            logger.warning(
                "%s: force=true - clearing existing TRANID=%s (status=%s) and "
                "re-uploading as a fresh attempt. NSDL will carry this attempt's rows "
                "alongside the previous one's if it wrote any before getting stuck/failing.",
                entry.key,
                record.tran_id,
                record.status,
            )
            repo.update(record, tran_id=None, guid=None, status="pending", error_detail=None)
            repo.commit()
        elif record.tran_id:
            # Already sent but not yet confirmed: re-poll the existing TRANID
            # rather than re-uploading — this API appends, so a second upload
            # would duplicate every row. This is the branch a workflow retry
            # lands in for a slow-but-fine upload; it's also where a TRANID
            # stuck at "in_progress" forever gets stuck too — that's what
            # `force` is for.
            logger.info(
                "%s: TRANID=%s already exists (status=%s) - re-polling rather than "
                "re-uploading (this API appends; a second upload would duplicate every "
                "row). Pass force=true to override.",
                entry.key,
                record.tran_id,
                record.status,
            )
            _apply_status(repo, record, client.check_status_once(record.tran_id))
            return _result(record, entry, record.upload_status or None)

        repo.update(record, retry_count=(record.retry_count or 0) + 1)
        repo.commit()
        category = _resolve_category(entry, categories)
        if category.upload_id not in settings_cache:
            settings_cache[category.upload_id] = client.get_upload_settings(category.upload_id)
        return _upload_one(
            repo,
            client,
            record,
            entry,
            category,
            settings_cache[category.upload_id],
            trade_date,
            file_path,
        )

    except NsdlSpeedeError as exc:
        logger.error("%s: %s", entry.key, exc)
        repo.update(record, status="failed", error_detail=str(exc))
        repo.commit()
        return _result(record, entry, str(exc))


def process_upload(
    session: Session,
    trade_date: str,
    selectors: list[dict] | None = None,
    correlation_id: str | None = None,
    force: bool = False,
) -> dict:
    """Upload the selected SPEED-e reports, sequentially, in catalogue order.

    Every file is attempted even if an earlier one failed: whether a missing
    CMPA pledge should stop CMFA's is the orchestrator's call (its step's
    `critical` flag), not ours. The aggregate `status` is `success` only when
    every selected file reached SUCCESS on both upload and process.

    `force` re-uploads every selected file even if it already has a TRANID
    (including one stuck "in_progress" forever) — see _process_entry for why
    this can duplicate rows in NSDL and should only be used deliberately.

    A selector's optional `version` pins that file to an exact numbered
    download instead of the latest - e.g. requesting the same (account,
    report) twice with versions 2 and 3 uploads both in this one call.
    """
    requests = resolve_selection(selectors)
    repo = NsdlSpeedeUploadRepository(session)
    key = f"nsdl_speede|{trade_date}|{len(requests)}"

    with batch_context(key, correlation_id):
        client = get_nsdl_speede_client()

        # Call 1, once per request: the live UPLOADNAME -> UPLOADID map. A
        # failure here is fatal for every file, since nothing can be addressed
        # without it.
        try:
            categories = client.list_categories(trade_date)
        except NsdlSpeedeError as exc:
            logger.error("nsdl_speede: category lookup failed for %s: %s", trade_date, exc)
            raise

        settings_cache: dict[int, UploadSettings] = {}
        results = [
            _process_entry(
                repo,
                client,
                entry,
                trade_date,
                correlation_id,
                categories,
                settings_cache,
                force,
                requested_version=version,
            )
            for entry, version in requests
        ]

    counts = {"success": 0, "in_progress": 0, "failed": 0}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1

    if counts["success"] == len(results):
        overall = "success"
    elif counts["success"]:
        overall = "partial"
    elif counts["failed"]:
        overall = "failed"
    else:
        overall = "in_progress"

    logger.info(
        "nsdl_speede %s: %d file(s) - %d success, %d in progress, %d failed -> %s",
        trade_date,
        len(results),
        counts["success"],
        counts["in_progress"],
        counts["failed"],
        overall,
    )

    return {
        "trade_date": trade_date,
        "status": overall,
        "summary": {
            "total": len(results),
            "success": counts["success"],
            "in_progress": counts["in_progress"],
            "failed": counts["failed"],
        },
        "files": results,
        "correlation_id": correlation_id,
    }


def check_status(session: Session, nsdl_upload_id: int) -> dict:
    """GET /settlement/nsdl_speede_upload/{id} - re-polls the stored TRANID
    without re-uploading anything. The way an `in_progress` file is resolved.
    """
    repo = NsdlSpeedeUploadRepository(session)
    record = repo.get(nsdl_upload_id)
    if record is None:
        raise UnknownNsdlSpeedeUploadError(f"unknown nsdl_speede_upload id {nsdl_upload_id}")

    entry = _BY_KEY.get((record.account.upper(), record.report.lower()))
    if entry is None:  # a row written before a catalogue change - report it as stored
        entry = SpeedeFile(record.account, record.report, record.upload_name or "", record.file_name)

    if record.tran_id and record.status not in ("success", "failed"):
        with batch_context(f"nsdl_speede|{record.trade_date}", record.correlation_id):
            _apply_status(repo, record, get_nsdl_speede_client().check_status_once(record.tran_id))

    result = _result(record, entry, record.upload_status or None)
    return {
        "trade_date": record.trade_date,
        "status": record.status,
        "summary": {
            "total": 1,
            "success": 1 if record.status == "success" else 0,
            "in_progress": 1 if record.status == "in_progress" else 0,
            "failed": 1 if record.status == "failed" else 0,
        },
        "files": [result],
        "correlation_id": record.correlation_id,
    }
