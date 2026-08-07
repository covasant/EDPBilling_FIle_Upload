"""Client for the "NSDL Speedy" settlement file upload API - the third,
unrelated upstream in this service, switchable between real and mock via
NSDL_SPEEDE_MODE (see app/core/config.py).

Not a variant of app/clients/dp_upload_client.py. Different host, different
endpoint names, no session header (LOGINID travels in the body), and three
structural differences that make sharing code with it a mistake:

  * There is NO server-side validate call. The DP API's Step 3
    (uploadfilevalidate) has no counterpart here, so a mis-shaped file chunks
    fine, finalizes fine, returns a TranId, and then fails (or silently
    mis-processes) inside the stored procedure. Format checking is ours -
    see validate_file() - and it must happen before finalize_upload().
  * The GUID is a first-class form field, not a filename prefix. The DP API
    encodes it as "{guid}_{name}" and finalizes on a ChunkFullPath; here the
    Guid is posted alongside each chunk and finalize takes it as
    UPLOADFOLDERNAME.
  * Finalize also TRIGGERS the backend process. There is no separate
    "process" step to gate on process_required - SaveSettlementPromodalUploadFile
    does both, which is why upload and process statuses are polled as a pair.

The six calls (this repo's caller, app/services/nsdl_speede_service.py, runs
2-5 per file; call 1 is run once per request to resolve names -> ids):

  1 getcommonuploadbydate                      -> every NSDL category + its status for a date
  2 GetSettlementPromodalUploadSettings        -> expected extension / filename token / column count
  3 SaveSettlementPromodalUploadChunkFile      -> one multipart call per chunk, sequential
  4 SaveSettlementPromodalUploadFile           -> finalize AND trigger; returns TRANID
  5 StatusSettlementPromodalUploadFileDetails  -> poll one TranId (upload + process status)
  6 GetSettlementPromodalUploadFileHistory     -> per-category history; diagnostics only

--------------------------------------------------------------------------
The interface
--------------------------------------------------------------------------

Callers use the methods on BaseNsdlSpeedeClient and nothing else - they get
parsed dataclasses, never the API's raw envelope, and never its HTML.

An adapter supplies one raw call per API. The chunk loop, the header strip,
the poll loop and the status classification live once on the base class and
are shared by both adapters:

  NsdlSpeedeClient     - the actual HTTP calls.
  MockNsdlSpeedeClient - canned dicts of the same shape, no network.
"""

import html
import json
import logging
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import requests

from app.clients.cbos_client import _redact, _summarise
from app.core.config import settings

logger = logging.getLogger("nsdl_speede_client")

GET_COMMON_UPLOAD_PATH = "/getcommonuploadbydate"
GET_UPLOAD_SETTINGS_PATH = "/GetSettlementPromodalUploadSettings"
UPLOAD_CHUNK_PATH = "/SaveSettlementPromodalUploadChunkFile"
FINALIZE_UPLOAD_PATH = "/SaveSettlementPromodalUploadFile"
GET_STATUS_PATH = "/StatusSettlementPromodalUploadFileDetails"
GET_HISTORY_PATH = "/GetSettlementPromodalUploadFileHistory"

# Call 3's per-chunk verdicts.
_CHUNK_UPLOADED = "ChunkUploaded"
_FILE_UPLOADED = "FileUploaded"

# Call 5's verdicts, derived from two free-text (HTML) status fields rather
# than a status code. UPLOAD STATUS reads "IN PROGRESS - Records Uploaded {n}"
# then "SUCCESS - Records Uploaded {n}"; PROCESS STATUS reads "PENDING" then
# "SUCCESS".
POLL_SUCCESS = "success"
POLL_FAILED = "failed"
POLL_IN_PROGRESS = "in_progress"

_SUCCESS_MARKER = "SUCCESS"
# No confirmed failure sample exists yet (every response in the API doc is a
# success), so this list is a best guess and is applied conservatively: a
# status matching none of these is IN PROGRESS, never FAILED. Getting that
# backwards would report a still-running upload as a failure and - because
# this API appends rather than replaces - invite a retry that duplicates every
# row. Mirrors cbos_client._FAILURE_STATUSES' same caveat.
_FAILURE_MARKERS = ("FAIL", "ERROR", "REJECT", "INVALID")

# Header scan budget. The widest real header seen is ~2.5KB (the 45-column
# View_Margin_Pledge_Instructions export), so this is a runaway guard, not a
# limit anyone should hit.
_HEADER_SCAN_BYTES = 65_536
_UTF8_BOM = b"\xef\xbb\xbf"

_TAG_RE = re.compile(r"<[^>]+>")


class NsdlSpeedeError(Exception):
    pass


@dataclass(frozen=True)
class UploadCategory:
    """One row of call 1 - an NSDL upload category and its state for the
    requested date."""

    upload_id: int
    upload_name: str
    procedure_name: str
    upload_status: str
    upload_date: str
    upload_by: str
    last_upload_file_name: str
    # The only place the API spells out WHY a category is FAILED - observed
    # 2026-08-06 as "UPLOADED FILE NAME NOT MATCHING(LIKE)" on UPLOADID 6.
    # Call 5 carries no equivalent, so this is worth keeping.
    upload_status_desc: str = ""


@dataclass(frozen=True)
class UploadSettings:
    """Call 2's expected file format. With no server-side validate call, these
    three fields are the only description of what the API will accept."""

    upload_id: int
    name: str
    file_name_contains: str
    file_extension: str
    column_count: int
    raw: dict


@dataclass(frozen=True)
class UploadStatus:
    """Call 5's reading for one TranId, with the HTML stripped out."""

    tran_id: int
    upload_status: str
    process_status: str
    upload_date: str
    process_start_date: str | None
    upload_by: str
    verdict: str  # POLL_SUCCESS | POLL_FAILED | POLL_IN_PROGRESS


def _strip_html(value: object) -> str:
    """Both status fields arrive as inline-styled HTML
    (<DIV STYLE="COLOR:BLUE;">IN PROGRESS...</DIV>), so nothing can be
    compared against them until the tags and entities are gone."""
    if value is None:
        return ""
    text = _TAG_RE.sub("", str(value))
    return " ".join(html.unescape(text).split())


def _classify(upload_status: str, process_status: str) -> str:
    """The pair of statuses reduced to one verdict.

    Success needs BOTH: finalize triggers the process, so an upload that
    landed but whose stored procedure has not finished is not done. Failure
    needs a recognised marker - see _FAILURE_MARKERS on why the unknown case
    resolves to IN PROGRESS rather than FAILED.
    """
    combined = f"{upload_status} {process_status}".upper()
    if any(marker in combined for marker in _FAILURE_MARKERS):
        return POLL_FAILED
    if _SUCCESS_MARKER in upload_status.upper() and _SUCCESS_MARKER in process_status.upper():
        return POLL_SUCCESS
    return POLL_IN_PROGRESS


def _parse_status_row(tran_id: str, row: dict) -> UploadStatus:
    upload_status = _strip_html(row.get("UPLOAD STATUS") or row.get("Upload Status"))
    process_status = _strip_html(row.get("PROCESS STATUS") or row.get("Process Status"))
    return UploadStatus(
        tran_id=int(row.get("TRANID") or row.get("TranId") or tran_id),
        upload_status=upload_status,
        process_status=process_status,
        upload_date=str(row.get("UPLOAD DATE") or row.get("Upload Date") or ""),
        process_start_date=row.get("PROCESS START DATE"),
        upload_by=str(row.get("UPLOAD BY") or row.get("Upload By") or ""),
        verdict=_classify(upload_status, process_status),
    )


def data_offset(file_path: Path) -> int:
    """Byte offset of the first data row: past a UTF-8 BOM and past the single
    header line.

    Returns the file size for a header-only export - "no newline anywhere" is
    a normal shape here, not a malformed file. Every SPEED-e export ends
    without a trailing newline, and a quiet day genuinely produces a file that
    is nothing but its header (login_3's confiscate/unpledge, 466 and 436
    bytes, in the 03-08-2026 sample set).
    """
    with file_path.open("rb") as handle:
        head = handle.read(_HEADER_SCAN_BYTES)

    start = len(_UTF8_BOM) if head.startswith(_UTF8_BOM) else 0
    newline = head.find(b"\n", start)
    if newline != -1:
        return newline + 1
    if len(head) < _HEADER_SCAN_BYTES:
        return file_path.stat().st_size
    raise NsdlSpeedeError(
        f"{file_path.name}: no line ending in the first {_HEADER_SCAN_BYTES} bytes - "
        f"this does not look like the expected single-header CSV"
    )


def ends_with_newline(file_path: Path, size: int) -> bool:
    """Whether the file's last byte is a line terminator.

    No SPEED-e export has one - the last data row simply ends. That matters
    because CBOS discards the final unterminated line: UPLOADID 24 loaded
    72,921 rows from a file carrying 72,922 (UAT, 2026-08-06, TRANID 339086).
    """
    if size <= 0:
        return True
    with file_path.open("rb") as handle:
        handle.seek(size - 1)
        return handle.read(1) == b"\n"


def read_header(file_path: Path) -> str:
    """The header line as text, BOM removed. Used for the column-count check
    and logged when a file is rejected, so a mismatch is diagnosable without
    re-fetching the file."""
    with file_path.open("rb") as handle:
        head = handle.read(_HEADER_SCAN_BYTES)
    if head.startswith(_UTF8_BOM):
        head = head[len(_UTF8_BOM) :]
    line, _, _ = head.partition(b"\n")
    return line.rstrip(b"\r").decode("utf-8", errors="replace")


class BaseNsdlSpeedeClient(ABC):
    # ---- step logging, mirrors dp_upload_client.BaseDPUploadClient._call ----

    def _call(self, step, api: str, raw_call, level: int = logging.INFO, **params):
        logger.log(level, "Call %s %s REQUEST  %s", step, api, _redact(params))
        try:
            response = raw_call()
        except Exception as exc:
            logger.error("Call %s %s FAILED   %s", step, api, exc)
            raise
        logger.log(level, "Call %s %s RESPONSE %s", step, api, _summarise(response))
        return response

    # ---- the raw API calls an adapter must provide --------------------------

    @abstractmethod
    def _get_common_uploads(self, trade_date: str) -> dict:
        """Call 1 raw."""

    @abstractmethod
    def _get_upload_settings(self, upload_id: int) -> dict:
        """Call 2 raw."""

    @abstractmethod
    def _upload_chunk(
        self, guid: str, file_name: str, chunk_bytes: bytes, current_chunk: int, total_chunks: int
    ) -> dict:
        """Call 3 raw, one per chunk."""

    @abstractmethod
    def _finalize_upload(self, upload_id: int, file_name: str, guid: str, param1: str) -> dict:
        """Call 4 raw."""

    @abstractmethod
    def _get_upload_status(self, tran_id: str) -> dict:
        """Call 5 raw, one poll."""

    @abstractmethod
    def _get_upload_history(self, upload_id: int) -> dict:
        """Call 6 raw."""

    # ---- the interface callers use ------------------------------------------

    def list_categories(self, trade_date: str) -> list[UploadCategory]:
        """Call 1. Every NSDL category and its status for `trade_date`.

        This is how UPLOADID is resolved: the ids differ between UAT and
        production but the names do not, so callers look categories up by
        UPLOADNAME and never hardcode a number.
        """
        raw = self._call(
            1, "getcommonuploadbydate", lambda: self._get_common_uploads(trade_date), trade_date=trade_date
        )
        result = raw.get("Result") or []
        if not result:
            raise NsdlSpeedeError(f"getcommonuploadbydate returned no categories for {trade_date}")
        return [
            UploadCategory(
                upload_id=int(row.get("UPLOADID") or 0),
                upload_name=str(row.get("UPLOADNAME") or ""),
                procedure_name=str(row.get("PROCEDURENAME") or ""),
                upload_status=str(row.get("UPLOADSTATUS") or ""),
                upload_date=str(row.get("UPLOAD_DATE") or ""),
                upload_by=str(row.get("UPLOAD_BY") or ""),
                last_upload_file_name=str(row.get("LASTUPLOADFILENAME") or ""),
                upload_status_desc=str(row.get("UPLOADSTATUSDESC") or ""),
            )
            for row in result
        ]

    def get_upload_settings(self, upload_id: int) -> UploadSettings:
        """Call 2. The expected format for one category."""
        raw = self._call(
            2,
            "GetSettlementPromodalUploadSettings",
            lambda: self._get_upload_settings(upload_id),
            upload_id=upload_id,
        )
        result = raw.get("Result") or []
        if not result:
            raise NsdlSpeedeError(
                f"GetSettlementPromodalUploadSettings returned no settings for UPLOADID={upload_id}"
            )
        row = result[0]
        return UploadSettings(
            upload_id=int(row.get("ID") or upload_id),
            name=str(row.get("NAME") or ""),
            file_name_contains=str(row.get("FILE NAME (CONTAINS)") or "").strip(),
            file_extension=str(row.get("FILEEXTENSION") or "").strip().lstrip("."),
            column_count=int(row.get("NO. OF COLUMNS") or 0),
            raw=row,
        )

    def validate_file(self, file_path: Path, transmit_name: str, config: UploadSettings) -> None:
        """The check this API does not do for us. Raises NsdlSpeedeError, which
        callers must treat as "do not upload" - reaching call 4 with a bad file
        burns a TranId and appends whatever the stored procedure makes of it.

        Column counting is a plain split: every SPEED-e export is unquoted
        (verified across all four report types), so no CSV reader is needed and
        an embedded-comma false positive is not a live risk.
        """
        expected_ext = config.file_extension.lower()
        actual_ext = file_path.suffix.lstrip(".").lower()
        if expected_ext and actual_ext != expected_ext:
            raise NsdlSpeedeError(
                f"{file_path.name}: extension is .{actual_ext}, API expects .{expected_ext}"
            )

        if config.file_name_contains and config.file_name_contains.lower() not in transmit_name.lower():
            raise NsdlSpeedeError(
                f"transmitted name {transmit_name!r} does not contain "
                f"{config.file_name_contains!r} as UPLOADID={config.upload_id} requires"
            )

        if not settings.nsdl_speede_validate_columns or not config.column_count:
            return

        header = read_header(file_path)
        if not header:
            raise NsdlSpeedeError(f"{file_path.name}: file is empty - no header row to validate")
        columns = len(header.split(","))
        if columns != config.column_count:
            raise NsdlSpeedeError(
                f"{file_path.name}: header has {columns} columns, UPLOADID={config.upload_id} "
                f"expects {config.column_count} (header: {header[:200]!r})"
            )

    def upload_chunks(self, file_path: Path, transmit_name: str) -> tuple[str, int, int]:
        """Call 3. Streams the file's DATA rows in sequential chunks under one
        client-generated GUID. Returns (guid, total_chunks, bytes_sent) - the
        byte count is what went on the wire, which is the header-stripped body
        plus a closing newline, not the size on disk.

        The header row is dropped here rather than on disk, because the strip
        has to happen exactly once and only an in-memory strip can guarantee
        that: a file stripped at download time and then re-processed - by a
        retry, a re-run, an ops re-drop - would silently lose its first DATA
        row instead. NSDL_SPEEDE_STRIP_HEADER=false sends the file whole.

        Chunks are read off disk rather than into one buffer: the Open Holdings
        export was 58MB uncompressed in the 03-08-2026 sample set.

        A header-only export sends one empty chunk rather than being skipped -
        the stored procedure still runs and records a zero-row day.
        """
        offset = data_offset(file_path) if settings.nsdl_speede_strip_header else 0
        size = file_path.stat().st_size
        data_bytes = max(0, size - offset)

        # Close the last row. Skipped for a header-only export, where a lone
        # newline would be a phantom empty row rather than a terminator.
        terminator = (
            b"\n"
            if (
                settings.nsdl_speede_append_trailing_newline
                and data_bytes > 0
                and not ends_with_newline(file_path, size)
            )
            else b""
        )
        send_bytes = data_bytes + len(terminator)

        chunk_size = max(1, settings.nsdl_speede_chunk_size_kb) * 1024
        total_chunks = max(1, (send_bytes + chunk_size - 1) // chunk_size)
        guid = str(uuid.uuid4())

        logger.info(
            "Call 3 - chunk upload: %s as %s, %d byte(s) to send from %d on disk, "
            "in %d chunk(s) of <=%d KB (header %s, trailing newline %s, GUID=%s)",
            file_path.name,
            transmit_name,
            send_bytes,
            size,
            total_chunks,
            settings.nsdl_speede_chunk_size_kb,
            f"stripped, {offset} bytes" if offset else "kept",
            "added" if terminator else "not needed",
            guid,
        )

        with file_path.open("rb") as handle:
            handle.seek(offset)
            for current_chunk in range(total_chunks):
                chunk_bytes = handle.read(chunk_size)
                if current_chunk == total_chunks - 1:
                    chunk_bytes += terminator
                response = self._call(
                    3,
                    "SaveSettlementPromodalUploadChunkFile",
                    lambda _b=chunk_bytes, _c=current_chunk: self._upload_chunk(
                        guid, transmit_name, _b, _c, total_chunks
                    ),
                    level=logging.DEBUG,
                    guid=guid,
                    file_name=transmit_name,
                    current_chunk=current_chunk,
                    total_chunks=total_chunks,
                    chunk_bytes=len(chunk_bytes),
                )
                status = str(response.get("Status") or "")
                is_last = current_chunk == total_chunks - 1

                if is_last:
                    if status != _FILE_UPLOADED:
                        raise NsdlSpeedeError(
                            f"chunk upload: last chunk reported {status!r}, expected "
                            f"{_FILE_UPLOADED!r}: {response}"
                        )
                    f_count = int(response.get("fCount") or -1)
                    if f_count != total_chunks:
                        raise NsdlSpeedeError(
                            f"chunk upload: fCount={f_count} != totalChunks={total_chunks}, "
                            f"chunks are missing server-side: {response}"
                        )
                elif status != _CHUNK_UPLOADED:
                    raise NsdlSpeedeError(
                        f"chunk upload: chunk {current_chunk}/{total_chunks} reported "
                        f"{status!r}, expected {_CHUNK_UPLOADED!r}: {response}"
                    )

        logger.info("Call 3 complete: %s uploaded under GUID=%s", transmit_name, guid)
        return guid, total_chunks, send_bytes

    def finalize_upload(self, upload_id: int, transmit_name: str, guid: str, param1: str) -> str:
        """Call 4. Registers the reassembled file AND fires the category's
        stored procedure - there is no separate process step. Returns the
        TRANID both statuses are then polled against.

        Every call here appends: a second call for the same file duplicates
        every row it carries. Callers must not reach this method for a file
        that already has a TranId.
        """
        response = self._call(
            4,
            "SaveSettlementPromodalUploadFile",
            lambda: self._finalize_upload(upload_id, transmit_name, guid, param1),
            upload_id=upload_id,
            file_name=transmit_name,
            guid=guid,
            param1=param1,
        )
        result = response.get("Result") or []
        if not result:
            raise NsdlSpeedeError(f"SaveSettlementPromodalUploadFile returned no Result: {response}")
        tran_id = str(result[0].get("TRANID") or "")
        if not tran_id:
            raise NsdlSpeedeError(f"SaveSettlementPromodalUploadFile returned no TRANID: {response}")
        return tran_id

    def check_status_once(self, tran_id: str) -> UploadStatus:
        """One status call, no loop - used by the re-poll endpoint and, in a
        loop, by poll_status below."""
        raw = self._call(
            5,
            "StatusSettlementPromodalUploadFileDetails",
            lambda: self._get_upload_status(tran_id),
            tran_id=tran_id,
        )
        result = raw.get("Result") or []
        if not result:
            logger.warning("Call 5 returned no Result for TRANID=%s", tran_id)
            return UploadStatus(int(tran_id), "", "", "", None, "", POLL_IN_PROGRESS)
        return _parse_status_row(tran_id, result[0])

    def poll_status(self, tran_id: str) -> UploadStatus:
        """Call 5, polled up to NSDL_SPEEDE_POLL_MAX_ATTEMPTS times. Returns
        the last reading - its verdict is POLL_IN_PROGRESS if nothing terminal
        arrived inside the budget, which is a "check again later", not a
        failure."""
        status = self.check_status_once(tran_id)
        max_attempts = max(1, settings.nsdl_speede_poll_max_attempts)
        for attempt in range(2, max_attempts + 1):
            if status.verdict != POLL_IN_PROGRESS:
                break
            time.sleep(settings.nsdl_speede_poll_interval_seconds)
            status = self.check_status_once(tran_id)
            logger.info(
                "Call 5 poll %d/%d TRANID=%s upload=%r process=%r -> %s",
                attempt,
                max_attempts,
                tran_id,
                status.upload_status,
                status.process_status,
                status.verdict,
            )
        return status

    def get_history(self, upload_id: int) -> list[dict]:
        """Call 6. Diagnostics only - call 5 already answers "is this TranId
        done", and nothing in the upload flow depends on this."""
        raw = self._call(
            6, "GetSettlementPromodalUploadFileHistory", lambda: self._get_upload_history(upload_id),
            upload_id=upload_id,
        )
        return list(raw.get("Result") or [])


# --------------------------------------------------------------------------
# NsdlSpeedeClient - the actual HTTP calls.
# --------------------------------------------------------------------------


class NsdlSpeedeClient(BaseNsdlSpeedeClient):
    def __init__(self) -> None:
        required = ("nsdl_speede_base_url", "nsdl_speede_login_id")
        missing = [name.upper() for name in required if not getattr(settings, name)]
        if missing:
            raise NsdlSpeedeError(
                f"NSDL_SPEEDE_MODE=REAL requires {', '.join(missing)} in .env (no committed defaults)"
            )

    def _url(self, path: str) -> str:
        prefix = settings.nsdl_speede_api_prefix.rstrip("/")
        return f"{settings.nsdl_speede_base_url.rstrip('/')}{prefix}{path}"

    def _handle(self, url: str, response) -> dict:
        logger.debug(
            "Response <- %s: status=%s body=%s", url, response.status_code, response.text[:1000]
        )
        if not response.ok:
            logger.error("Response <- %s failed: %s %s", url, response.status_code, response.text)
            raise NsdlSpeedeError(f"{url} failed: {response.status_code} {response.text}")
        try:
            body = response.json()
        except ValueError as exc:
            raise NsdlSpeedeError(f"{url} returned non-JSON response: {response.text}") from exc
        # The chunk endpoint answers with a JSON *string* holding the object
        # rather than the object itself (called out in the API doc), so one
        # decode can leave a str behind.
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError as exc:
                raise NsdlSpeedeError(f"{url} returned an unparseable JSON string: {body!r}") from exc
        if not isinstance(body, dict):
            raise NsdlSpeedeError(f"{url} returned {type(body).__name__}, expected a JSON object")
        return body

    def _post(self, url: str, payload: dict) -> dict:
        logger.debug("Request -> %s: %s", url, _redact(payload))
        try:
            response = requests.post(
                url, json=payload, timeout=settings.nsdl_speede_timeout_seconds
            )
        except requests.RequestException as exc:
            logger.error("Request -> %s failed: %s", url, exc)
            raise NsdlSpeedeError(f"Request to {url} failed: {exc}") from exc
        return self._handle(url, response)

    def _post_multipart(self, url: str, data: dict, files: dict) -> dict:
        logger.debug("Request -> %s: data=%s", url, _redact(data))
        try:
            response = requests.post(
                url, data=data, files=files, timeout=settings.nsdl_speede_upload_timeout_seconds
            )
        except requests.RequestException as exc:
            logger.error("Request -> %s failed: %s", url, exc)
            raise NsdlSpeedeError(f"Request to {url} failed: {exc}") from exc
        return self._handle(url, response)

    def _get_common_uploads(self, trade_date: str) -> dict:
        return self._post(
            self._url(GET_COMMON_UPLOAD_PATH),
            {
                "TAG": "UPLOADNAME",
                "LOGINID": settings.nsdl_speede_login_id,
                "GROUPNAME": settings.nsdl_speede_group_name,
                "DATE": trade_date,
            },
        )

    def _get_upload_settings(self, upload_id: int) -> dict:
        return self._post(self._url(GET_UPLOAD_SETTINGS_PATH), {"UPLOADID": int(upload_id)})

    def _upload_chunk(
        self, guid: str, file_name: str, chunk_bytes: bytes, current_chunk: int, total_chunks: int
    ) -> dict:
        data = {
            "CurrentChunk": str(current_chunk),
            "TotalChunks": str(total_chunks),
            "Guid": guid,
            "FileName": file_name,
        }
        files = {"file": (file_name, chunk_bytes, "application/octet-stream")}
        return self._post_multipart(self._url(UPLOAD_CHUNK_PATH), data, files)

    def _finalize_upload(self, upload_id: int, file_name: str, guid: str, param1: str) -> dict:
        return self._post(
            self._url(FINALIZE_UPLOAD_PATH),
            {
                "UPLOADID": int(upload_id),
                "LOGINID": settings.nsdl_speede_login_id,
                "UPLOADFILENAME": file_name,
                "UPLOADFOLDERNAME": guid,
                "PARAM1": param1,
                "ChunkFileUpload": "Yes",
            },
        )

    def _get_upload_status(self, tran_id: str) -> dict:
        return self._post(self._url(GET_STATUS_PATH), {"TRANID": int(tran_id)})

    def _get_upload_history(self, upload_id: int) -> dict:
        return self._post(self._url(GET_HISTORY_PATH), {"UPLOADID": int(upload_id)})


# --------------------------------------------------------------------------
# MockNsdlSpeedeClient - canned responses of the same shape, no network.
# Scenario rules, checked against the transmitted file name (case-insensitively),
# same convention as MockDPUploadClient:
#   contains "fail" -> finalize succeeds, then the status poll reports FAILED
#   otherwise       -> SUCCESS after NSDL_SPEEDE_MOCK_PENDING_POLLS pending reads
# --------------------------------------------------------------------------


# Tokens and column counts read off UAT on 2026-08-06, not invented - a mock
# that disagrees with the live API is worse than no mock. Note 8 and 9: the API
# wants "_Initiated" exports of 57 and 55 columns, while the SPEED-e bot's
# Invoke/Release screens produce 30- and 29-column files. That mismatch is real
# and unresolved; the mock reproduces it so it cannot be forgotten.
_MOCK_CATEGORIES = (
    (6, "NSDL CMPA OPEN HOLDING", "usp_Nsdl_CMPA_OpenHolding", "View_Open_Holdings", 18),
    (7, "NSDL MARGIN PLEDGE", "USP_SETT_NSDL_MarginPledgeRepledge", "View_Margin_Pledge_Instructions", 45),
    (8, "NSDL MARGIN CONFISCATE", "USP_SETT_NSDL_MarginConfiscate", "910_Pledge_Invocation_Initiated", 57),
    (9, "NSDL MARGIN UNPLEDGE", "USP_SETT_NSDL_MarginUnPledge", "911_Unilateral_Closure_Initiated", 55),
    (24, "NSDL CMFA OPEN HOLDING", "usp_Nsdl_CMFA_OpenHolding", "View_Open_Holdings", 18),
    (25, "NSDL NARNO OPEN HOLDING", "usp_Nsdl_Narno_OpenHolding", "View_Open_Holdings", 18),
)


class MockNsdlSpeedeClient(BaseNsdlSpeedeClient):
    def __init__(self) -> None:
        self._next_tran_id = 339077
        self._poll_attempts: dict[str, int] = {}
        self._tran_names: dict[str, str] = {}
        self.chunk_calls: list[tuple[str, int, int]] = []  # (guid, chunk, bytes), for assertions

    def _get_common_uploads(self, trade_date: str) -> dict:
        return {
            "Status": "Success",
            "Result": [
                {
                    "UPLOADID": upload_id,
                    "UPLOADNAME": name,
                    "PROCEDURENAME": procedure,
                    "UPLOADSTATUS": "PENDING",
                    "UPLOAD_DATE": "",
                    "UPLOAD_BY": "",
                    "UPLOADSTATUSDESC": "",
                    "LASTUPLOADFILENAME": "",
                }
                for upload_id, name, procedure, _token, _columns in _MOCK_CATEGORIES
            ],
        }

    def _get_upload_settings(self, upload_id: int) -> dict:
        for candidate, name, _procedure, token, columns in _MOCK_CATEGORIES:
            if candidate == int(upload_id):
                return {
                    "Status": "Success",
                    "Result": [
                        {
                            "ID": candidate,
                            "NAME": name,
                            "SAMPLE FILE": '<a href="https://example.invalid/sample.csv">Download</a>',
                            "FILE NAME (CONTAINS)": token,
                            "FILEEXTENSION": "csv",
                            "NO. OF COLUMNS": columns,
                        }
                    ],
                }
        return {"Status": "Success", "Result": []}

    def _upload_chunk(
        self, guid: str, file_name: str, chunk_bytes: bytes, current_chunk: int, total_chunks: int
    ) -> dict:
        self.chunk_calls.append((guid, current_chunk, len(chunk_bytes)))
        is_last = current_chunk == total_chunks - 1
        return {
            "Status": _FILE_UPLOADED if is_last else _CHUNK_UPLOADED,
            "Guid": guid,
            "FileName": file_name,
            "currentChunk": str(current_chunk),
            "totalChunks": str(total_chunks),
            "fCount": str(current_chunk + 1),
        }

    def _finalize_upload(self, upload_id: int, file_name: str, guid: str, param1: str) -> dict:
        tran_id = self._next_tran_id
        self._next_tran_id += 1
        self._tran_names[str(tran_id)] = file_name
        return {"Status": "Success", "Result": [{"TRANID": tran_id}]}

    def _get_upload_status(self, tran_id: str) -> dict:
        key = str(tran_id)
        seen = self._poll_attempts.get(key, 0)
        self._poll_attempts[key] = seen + 1

        if "fail" in self._tran_names.get(key, "").lower():
            upload_status = '<DIV STYLE="COLOR:RED;">FAILED - Invalid file format</DIV>'
            process_status = '<DIV STYLE="COLOR:RED;">FAILED</DIV>'
        elif seen < settings.nsdl_speede_mock_pending_polls:
            upload_status = '<DIV STYLE="COLOR:BLUE;">IN PROGRESS - Records Uploaded 0</DIV>'
            process_status = "PENDING"
        else:
            upload_status = '<DIV STYLE="COLOR:GREEN;">SUCCESS - Records Uploaded 84408</DIV>'
            process_status = '<DIV STYLE="COLOR:GREEN;">SUCCESS</DIV>'

        return {
            "Status": "Success",
            "Result": [
                {
                    "TRANID": int(tran_id),
                    "UPLOAD STATUS": upload_status,
                    "PROCESS STATUS": process_status,
                    "PROCESS PARAMS": "2026-08-05        ",
                    "UPLOAD DATE": "05 Aug 2026 17:46:37",
                    "PROCESS START DATE": None,
                    "UPLOAD BY": settings.nsdl_speede_login_id or "21429",
                }
            ],
        }

    def _get_upload_history(self, upload_id: int) -> dict:
        return {
            "Status": "Success",
            "Result": [
                {
                    "TranId": self._next_tran_id - 1,
                    "Upload Status": '<DIV STYLE="COLOR:GREEN;">SUCCESS - Records Uploaded 84408</DIV>',
                    "Process Status": '<DIV STYLE="COLOR:GREEN;">SUCCESS</DIV>',
                    "Process Parameter": "2026-08-05        ",
                    "Process Response": "-",
                    "Upload Date": "05 Aug 2026 17:59:12",
                    "Upload By": settings.nsdl_speede_login_id or "21429",
                }
            ],
        }


_client: BaseNsdlSpeedeClient | None = None


def get_nsdl_speede_client() -> BaseNsdlSpeedeClient:
    global _client
    if _client is None:
        mode = settings.nsdl_speede_mode.strip().upper()
        if mode == "REAL":
            _client = NsdlSpeedeClient()
        elif mode == "MOCK":
            _client = MockNsdlSpeedeClient()
        else:
            raise NsdlSpeedeError(
                f"Invalid NSDL_SPEEDE_MODE '{settings.nsdl_speede_mode}' - must be MOCK or REAL"
            )
        logger.info(
            "nsdl_speede_client: using %s (NSDL_SPEEDE_MODE=%s)", type(_client).__name__, mode
        )
    return _client


def set_nsdl_speede_client(client: BaseNsdlSpeedeClient | None) -> None:
    """Inject a specific client (e.g. a mock in a test), bypassing the
    NSDL_SPEEDE_MODE factory."""
    global _client
    _client = client


def reset_nsdl_speede_client() -> None:
    """Clear the cached client so the next get_nsdl_speede_client() rebuilds
    from NSDL_SPEEDE_MODE. Call between tests."""
    global _client
    _client = None
