"""Client for CBOS's trade-upload API, switchable between a real
implementation and a mock one via CBOS_MODE (see app/core/config.py).

Endpoints are split across two hosts, per
EDP_Trade_Process_API_Documentation_v4.docx:

  GTG host  (settings.cbos_gtg_base_url)  - file_process_status GTG/CHK calls
  CORE host (settings.cbos_core_base_url) - process/brokerage CORE calls

This repo owns the UPLOAD lane only - see CONTEXT.md for the handoff. Our job
ends at "make FILEUPLOAD go TRUE"; the trigger (Step 10) and everything
downstream belong to the EDP_Billing scheduler.

One file-upload batch (one segment + one trade date):

  Step 2 - getNewTradeProcess (PROCESSID=0)              -> PROCESSID + Table2 (all UploadID candidates)
  Step 3 - CheckProcessIDExist                           -> confirms Step 2's PROCESSID registered
  Step 4 - GetNewTradeProcessPromodalUploadSettings      -> per-UploadID pattern/extension/column rules
  Step 5 - SaveTradePromodalUploadChunkFile              -> one call per CHUNK_SIZE_KB chunk of a matched file
  Step 6 - getdropdown(EXISTINGPROCESSID)                -> optional confirmation lookup, not on the critical path
  Step 7 - SaveNewTradeProcessPromodalUploadFile         -> registers each uploaded file
  Step 8 - UpdateNewTradeProcessProcessDetailsIsMandatory-> mark each empty non-zero slot optional
  Step 9 - file_process_status (FILEUPLOAD)              -> our own confirmation the files landed

--------------------------------------------------------------------------
The interface
--------------------------------------------------------------------------

Callers use the eight methods on BaseCBOSClient and nothing else. They return
real values - a ProcessReservation, a bool - never CBOS's raw response
envelope, so no caller needs to know that a PROCESSID arrives at
Result.Table1[0].PROCESSID or that a GTG verdict arrives at Data[0].MSG.

An adapter supplies the eight `_`-prefixed raw calls, one per CBOS endpoint.
Everything else - envelope parsing, the chunk loop, the poll loop - lives once
on the base class and is shared by both adapters:

  CBOSClient     - makes the actual HTTP calls against the two CBOS hosts.
  MockCBOSClient - returns canned dicts with the exact same shape, so the
                   orchestration above runs unmodified in either mode.

get_cbos_client() is the factory; it reads settings.cbos_mode once and returns
the matching singleton.
"""

import json
import logging
import random
import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

from app.core.config import settings

logger = logging.getLogger("cbos_client")

# GTG host paths (settings.cbos_gtg_base_url).
FILE_PROCESS_STATUS_PATH = "/api/edp/file_process_status"
GET_EXPECTED_FILENAME_PATH = "/api/edp/get_expected_filename"

# CORE host paths (settings.cbos_core_base_url).
GET_NEW_TRADE_PROCESS_PATH = "/v1/api/process/getNewTradeProcess"
GET_UPLOAD_SETTINGS_PATH = "/v1/api/process/GetNewTradeProcessPromodalUploadSettings"
UPLOAD_CHUNK_PATH = "/v1/api/process/SaveTradePromodalUploadChunkFile"
SAVE_UPLOAD_FILE_PATH = "/v1/api/process/SaveNewTradeProcessPromodalUploadFile"
GET_EXISTING_PROCESS_ID_PATH = "/v1/api/brokerage/getdropdown"
UPDATE_IS_MANDATORY_PATH = "/v1/api/process/UpdateNewTradeProcessProcessDetailsIsMandatory"

# ProcessName values for the shared file_process_status (GTG) endpoint.
PROCESS_NAME_CHECK_PROCESS_ID = "CheckProcessIDExist"
PROCESS_NAME_FILE_UPLOAD_STATUS = "FILEUPLOAD"


class CBOSUploadError(Exception):
    pass


def _to_cbos_date(folder_date: str) -> str:
    """Reformats a trade date (settings.date_folder_format, e.g. dd-mm-yyyy)
    into the yyyy-mm-dd shape CBOS's TRADEDATE/paraM1 fields require. Only
    affects the outgoing CBOS payload - folder names and DB records are
    untouched."""
    return datetime.strptime(folder_date, settings.date_folder_format).strftime("%Y-%m-%d")


def _server_ip() -> str:
    """Best-effort local IP for the documented "ipaddress" field on Step 7.
    Falls back to an empty string rather than failing an upload over a
    non-critical field."""
    try:
        return socket.gethostbyname(socket.gethostname())
    except socket.error:
        return ""


# Payload keys whose values must never reach a log line or the DB (H8).
_SECRET_KEYS = {"password", "pwd", "passwd", "api_key", "apikey", "token", "secret"}


def _redact(payload: dict) -> dict:
    """A shallow copy of a request payload with secret values masked, for safe
    logging. The password never appears in cleartext."""
    if not isinstance(payload, dict):
        return payload
    return {k: ("***" if str(k).lower() in _SECRET_KEYS else v) for k, v in payload.items()}


# A response body can be a large encoded envelope (Step 4 returns every column
# rule). Long enough to see Status and the first fields, short enough that a
# day's log stays greppable.
_LOG_BODY_LIMIT = 600


def _summarise(response) -> str:
    """A response rendered for a log line: single-line and length-capped.

    Truncation is marked rather than silent, so a body that got cut is never
    mistaken for one that genuinely ended there - LOG_LEVEL=DEBUG gives the
    untruncated wire body in REAL mode.
    """
    text = str(response).replace("\n", " ")
    if len(text) <= _LOG_BODY_LIMIT:
        return text
    return f"{text[:_LOG_BODY_LIMIT]}... [+{len(text) - _LOG_BODY_LIMIT} chars, LOG_LEVEL=DEBUG for full body]"


# Response bodies that indicate a business-level failure even though the HTTP
# call itself returned 200. Only trips on a recognized failure marker - an
# unrecognized/absent Status is treated as success, since we don't have a
# confirmed real-CBOS failure shape yet.
_FAILURE_STATUSES = {"FAILED", "FAILURE", "ERROR"}

# Step 9 stand-in for "CBOS never gave a verdict inside the attempt budget".
# Not a value CBOS sends - the empty-string case is covered by it too.
POLL_TIMED_OUT = "POLL_TIMED_OUT"

# Step 9 answers that are CBOS's final word rather than "still pending", so
# polling stops on them. Real CBOS returned SKIP for a whole MCX batch on
# 2026-07-21 and we polled it thirty times before reporting a timeout; what SKIP
# means is a question for the CBOS team, but it plainly isn't "not yet".
_TERMINAL_POLL_MESSAGES = {"SKIP"} | _FAILURE_STATUSES


# How many times a body may be re-encoded before we give up. Steps 2/4 need one
# pass after requests' .json(); Step 5 needs two. Headroom for the next surprise.
_MAX_ENCODING_LAYERS = 4


def _decode_body(raw, source: str) -> dict:
    """Normalise a CBOS response into a dict, or raise CBOSUploadError.

    CBOS returns its payload double-encoded: the body is a JSON *string*
    holding a JSON document, rather than the document itself -

        "{\\"Status\\":\\"Success\\",\\"Result\\":{...}}"

    - which is what a server does when it serialises an already-serialised
    string. requests' .json() hands back a str for that, and every .get() call
    downstream then dies with AttributeError. AttributeError is not
    CBOSUploadError, so it escapes process_batch's setup retry loop, the files
    are never routed to uploadFailed/, and the next scan rediscovers them
    forever (the H4 loop).

    Hence both jobs here: unwrap the extra layer, and guarantee callers get a
    dict or a CBOSUploadError - never a surprise type.
    """
    # Observed depths differ per endpoint: Steps 2 and 4 arrive double-encoded,
    # Step 5 triple-encoded. requests' .json() strips the first layer, so the
    # budget here is generous rather than exact - each pass is guarded, and a
    # layer that doesn't decode to a dict still ends in CBOSUploadError.
    body = raw
    for _ in range(_MAX_ENCODING_LAYERS):
        if not isinstance(body, str):
            break
        try:
            body = json.loads(body)
        except ValueError as exc:
            raise CBOSUploadError(
                f"{source} returned a string that is not JSON: {body[:200]}"
            ) from exc

    if not isinstance(body, dict):
        raise CBOSUploadError(
            f"{source} returned {type(body).__name__}, expected a JSON object: {str(body)[:200]}"
        )
    return body


def _raise_on_failed_status(path: str, body: dict) -> None:
    status = str(body.get("Status", "")).strip().upper()
    if status in _FAILURE_STATUSES:
        logger.error("Response <- %s reported Status=%s: %s", path, status, body)
        raise CBOSUploadError(f"{path} returned Status={status}: {body}")


# --------------------------------------------------------------------------
# What the interface hands back.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class UploadCandidate:
    """One Table2 slot from a batch's reservation. A non-zero upload_id means
    CBOS expects a file at this step today."""
    upload_id: str
    step_no: object | None
    name: str

    @property
    def expects_a_file(self) -> bool:
        return self.upload_id not in ("0", "")


@dataclass(frozen=True)
class ProcessReservation:
    """The result of reserving a batch (Step 2): the PROCESSID every file in
    the batch shares, plus every UploadID slot the segment's pipeline offers."""
    process_id: str
    candidates: list[UploadCandidate]


@dataclass(frozen=True)
class UploadRule:
    """One UploadID's file-name pattern, extension and column count (Step 4),
    decoded off the wire.

    Every CBOS field name lives on this side of the interface;
    app/services/upload_matching.py works only with these attributes and never
    sees a raw settings row. raw_settings keeps the original for the audit log.
    """
    upload_id: str
    name: str
    file_name_pattern: str
    compare_operator: str
    extension: str
    column_count: int | None
    raw_settings: dict


def _extract_pattern(setting: dict) -> tuple[str, str]:
    """Find the file-name pattern in a Step-4 row, and any match operator that
    came with it. Returns (pattern, operator_from_key).

    Real CBOS bakes the semantics into the key name and sends no separate
    operator field at all:

        "FILE NAME (CONTAINS)": "MCX_PRODUCTMASTER"

    So the parenthetical IS the operator. Any key beginning "FILE NAME" is
    treated as the pattern field and its parenthetical (if any) as the
    operator, which covers (CONTAINS) and whatever other variants CBOS uses
    without needing a code change per variant. The older documented spellings
    ("FILE NAME", "FileNameToCompare") still work and simply yield no operator
    hint, falling back to CBOS's default containment behaviour.
    """
    for key, value in setting.items():
        if not str(key).strip().upper().startswith("FILE NAME"):
            continue
        pattern = str(value or "").strip()
        if not pattern:
            continue
        operator = ""
        if "(" in key and ")" in key:
            operator = key[key.index("(") + 1:key.rindex(")")].strip()
        return pattern, operator

    # Documented alternate spelling, no operator baked in.
    return str(setting.get("FileNameToCompare") or "").strip(), ""


def _parse_upload_rule(upload_id: str, setting: dict, fallback_name: str = "") -> UploadRule | None:
    """Decode one raw Step-4 settings row into an UploadRule.

    Returns None if the row can't produce a usable rule, which is a skip and
    never an error: a slot with no pattern or no extension can't match anything.

    Tolerances that exist because CBOS's rows aren't uniform:
      - the pattern key carries its own operator - "FILE NAME (CONTAINS)" -
        and real CBOS sends no separate operator field at all (see
        _extract_pattern); the documented "FILE NAME" / "FileNameToCompare"
        spellings still work
      - the extension as "FILEEXTENSION" or "FileExtension", and may carry a
        leading dot or any case
      - an explicit FileNameCompareOperator, if CBOS ever sends one, wins over
        the key's parenthetical; with neither, CBOS's default containment
        behaviour (LIKE) applies
      - the column count may be absent, blank, "-", or non-numeric; any of
        those means "don't check columns" rather than "reject this slot"
    """
    pattern, operator_from_key = _extract_pattern(setting)
    compare_operator = str(
        setting.get("FileNameCompareOperator") or operator_from_key or "LIKE"
    ).strip()
    extension = str(setting.get("FILEEXTENSION") or setting.get("FileExtension") or "").strip().lstrip(".").upper()

    if not pattern or not extension:
        logger.warning("Incomplete upload settings for UPLOADID=%s (%s), skipping", upload_id, setting)
        return None

    raw_columns = setting.get("NO. OF COLUMNS")
    column_count = None
    if raw_columns not in (None, "", "-"):
        try:
            column_count = int(raw_columns)
        except (TypeError, ValueError):
            logger.warning("Non-numeric column count %r for UPLOADID=%s, ignoring", raw_columns, upload_id)

    return UploadRule(
        upload_id=upload_id,
        name=str(setting.get("NAME") or fallback_name or ""),
        file_name_pattern=pattern,
        compare_operator=compare_operator,
        extension=extension,
        column_count=column_count,
        raw_settings=setting,
    )


# --------------------------------------------------------------------------
# BaseCBOSClient - the single interface. Adapters supply the eight raw calls;
# parsing, chunking and polling are implemented once, here.
# --------------------------------------------------------------------------

class BaseCBOSClient(ABC):

    # ---- step logging -----------------------------------------------------

    def _call(self, step, api: str, raw_call, level: int = logging.INFO, **params):
        """Run one raw CBOS call, logging what we sent and what came back.

        This lives on the base class deliberately: the adapters' own logging
        only ever covers one mode each, so before this existed a MOCK run - the
        mode used for local development - showed the step names but never a
        payload. Now both modes narrate identically, and the only thing DEBUG
        adds in REAL mode is the literal wire traffic.

        params are the call's arguments, already the safe-to-log view: they are
        what we asked for rather than the encoded body, so they read the same in
        either mode. Passed through _redact in case a caller ever adds a
        credential-bearing argument.

        level lets the noisy per-item steps (5's chunks, 9's polls) log at DEBUG
        while their once-per-file/batch summary stays at INFO.
        """
        logger.log(level, "Step %s %s REQUEST  %s", step, api, _redact(params))
        try:
            response = raw_call()
        except Exception as exc:
            # ERROR regardless of `level`: a failed call is never routine, and
            # some callers treat the failure as non-fatal, so this may be the
            # only record that it happened.
            logger.error("Step %s %s FAILED   %s", step, api, exc)
            raise
        logger.log(level, "Step %s %s RESPONSE %s", step, api, _summarise(response))
        return response

    # ---- the raw CBOS calls an adapter must provide -----------------------

    @abstractmethod
    def _get_new_trade_process(self, segment: str, trade_date: str) -> dict:
        """Step 2 raw call."""

    @abstractmethod
    def _check_process_id_exist(self, segment: str) -> dict:
        """Step 3 raw call."""

    @abstractmethod
    def _get_upload_settings(self, upload_id: str) -> dict:
        """Step 4 raw call."""

    @abstractmethod
    def _upload_chunk(self, upload_id: str, guid: str, file_name: str, chunk_bytes: bytes,
                      current_chunk: int, total_chunks: int) -> dict:
        """Step 5 raw call, one per chunk."""

    @abstractmethod
    def _get_existing_process_id(self, segment: str, trade_date: str) -> dict:
        """Step 6 raw call."""

    @abstractmethod
    def _create_file_entry(self, upload_id: str, guid: str, file_name: str, process_id: str,
                           trade_date: str) -> dict:
        """Step 7 raw call."""

    @abstractmethod
    def _update_step_optional(self, process_id: str, step_no) -> dict:
        """Step 8 raw call, one per empty slot."""

    @abstractmethod
    def _file_upload_status(self, segment: str) -> dict:
        """Step 9 raw call, one poll."""

    # ---- the interface callers use ----------------------------------------

    def reserve_process(self, segment: str, trade_date: str) -> ProcessReservation:
        """Step 2. Reserve one PROCESSID for this batch and read back every
        UploadID slot its segment expects.

        Raises CBOSUploadError if CBOS returns no PROCESSID or an empty Table2 -
        either makes the batch unprocessable.
        """
        raw = self._call(2, "getNewTradeProcess",
                         lambda: self._get_new_trade_process(segment, trade_date),
                         segment=segment, trade_date=trade_date)
        response = _decode_body(raw, "getNewTradeProcess")
        result = response.get("Result") or {}

        table1 = result.get("Table1") or []
        if not table1 or table1[0].get("PROCESSID") is None:
            raise CBOSUploadError(f"getNewTradeProcess response had no Table1[0].PROCESSID: {response}")
        process_id = str(table1[0]["PROCESSID"])

        table2 = result.get("Table2") or []
        if not table2:
            raise CBOSUploadError(f"getNewTradeProcess response had no Table2 upload candidates: {response}")

        candidates = [
            UploadCandidate(
                upload_id=str(row.get("UPLOADID", "0")),
                step_no=row.get("STEPNO"),
                name=str(row.get("NAME") or ""),
            )
            for row in table2
        ]
        # The Table2 slot list is the batch's whole plan - which UploadIDs CBOS
        # expects a file at today. Logged once here so a run's later "why is
        # FILEUPLOAD still FALSE" can be answered from the log alone.
        expects = [c.upload_id for c in candidates if c.expects_a_file]
        logger.info("Step 2 reserved ProcessID=%s segment=%s: %d Table2 slot(s), %d expecting a file %s",
                    process_id, segment, len(candidates), len(expects), expects)
        return ProcessReservation(process_id=process_id, candidates=candidates)

    def check_process_exists(self, segment: str) -> str:
        """Step 3. Confirmation that Step 2's PROCESSID registered. Diagnostic
        only - never a gate. Returns CBOS's message."""
        return self._gtg_msg(self._call(3, "CheckProcessIDExist",
                                        lambda: self._check_process_id_exist(segment),
                                        segment=segment))

    def upload_settings(self, upload_id: str, fallback_name: str = "") -> UploadRule | None:
        """Step 4. This UploadID's matching rule, decoded. Returns None if CBOS
        offered no settings for it, or the row can't produce a usable rule.

        fallback_name is used when the settings row carries no NAME - pass the
        Table2 slot's label.
        """
        raw = self._call(4, "GetNewTradeProcessPromodalUploadSettings",
                         lambda: self._get_upload_settings(upload_id),
                         upload_id=upload_id)
        response = _decode_body(raw, "GetNewTradeProcessPromodalUploadSettings")
        result = response.get("Result") or []
        if not result:
            logger.warning("No upload settings returned for UPLOADID=%s", upload_id)
            return None
        return _parse_upload_rule(upload_id, result[0], fallback_name)

    def upload_file(self, file_path: Path, upload_id: str, guid: str) -> None:
        """Step 5. Stream the file to CBOS in chunk_size_kb-sized chunks
        (0-indexed CurrentChunk, TotalChunks=N, one GUID for the whole file), so
        a large file is never loaded into memory whole and a slow link isn't
        bound by the short JSON timeout.

        Each chunk retries up to cbos_chunk_retry_attempts on a transient
        CBOSUploadError before failing the file.
        """
        chunk_size = max(1, settings.chunk_size_kb) * 1024
        retries = max(1, settings.cbos_chunk_retry_attempts)
        file_size = file_path.stat().st_size
        total_chunks = max(1, (file_size + chunk_size - 1) // chunk_size)
        logger.info(
            "Step 5 - SaveTradePromodalUploadChunkFile: %s (%d bytes) in %d chunk(s) of <=%d KB, "
            "upload_id=%s guid=%s",
            file_path.name, file_size, total_chunks, settings.chunk_size_kb, upload_id, guid,
        )

        with file_path.open("rb") as fh:
            for current_chunk in range(total_chunks):
                chunk_bytes = fh.read(chunk_size)
                for attempt in range(1, retries + 1):
                    try:
                        # DEBUG: a large file is hundreds of chunks, and at INFO
                        # they would bury the batch narrative. The Step 5
                        # summary lines either side stay at INFO.
                        self._call(5, "SaveTradePromodalUploadChunkFile",
                                   lambda: self._upload_chunk(upload_id, guid, file_path.name,
                                                              chunk_bytes, current_chunk, total_chunks),
                                   level=logging.DEBUG,
                                   upload_id=upload_id, guid=guid, file_name=file_path.name,
                                   current_chunk=current_chunk, total_chunks=total_chunks,
                                   chunk_bytes=len(chunk_bytes))
                        break
                    except CBOSUploadError as exc:
                        logger.warning("Step 5: chunk %d/%d of %s failed (attempt %d/%d): %s",
                                       current_chunk + 1, total_chunks, file_path.name,
                                       attempt, retries, exc)
                        if attempt >= retries:
                            raise

        logger.info("Step 5 complete: %s uploaded in %d chunk(s)", file_path.name, total_chunks)

    def existing_process(self, segment: str, trade_date: str) -> None:
        """Step 6. Confirmation lookup, not on the critical path - it also
        confirms EDP_Billing will be able to find our PROCESSID via
        getdropdown."""
        self._call(6, "getdropdown(EXISTINGPROCESSID)",
                   lambda: self._get_existing_process_id(segment, trade_date),
                   segment=segment, trade_date=trade_date)

    def register_file(self, upload_id: str, guid: str, file_name: str, process_id: str,
                      trade_date: str) -> None:
        """Step 7. Register an uploaded file against its UploadID and PROCESSID."""
        self._call(7, "SaveNewTradeProcessPromodalUploadFile",
                   lambda: self._create_file_entry(upload_id, guid, file_name, process_id, trade_date),
                   upload_id=upload_id, guid=guid, file_name=file_name,
                   process_id=process_id, trade_date=trade_date)

    def mark_step_optional(self, process_id: str, step_no) -> None:
        """Step 8. Mark an empty slot optional so it doesn't hold FILEUPLOAD at
        FALSE. One call per empty non-zero slot, after Steps 5+7."""
        self._call(8, "UpdateNewTradeProcessProcessDetailsIsMandatory",
                   lambda: self._update_step_optional(process_id, step_no),
                   process_id=process_id, step_no=step_no)

    def confirm_upload(self, segment: str) -> str:
        """Step 9. Poll FILEUPLOAD per SEGMENT (matching the documented payload
        shape - it carries no PROCESSID/UPLOADID/GUID), up to
        cbos_poll_max_attempts times.

        Returns CBOS's own last message, uppercased - "TRUE", "FALSE", "SKIP",
        or POLL_TIMED_OUT if the budget ran out. Callers must not read anything
        into a non-TRUE value beyond what CBOS said: only TRUE is documented to
        mean good-to-go, and EDP_Billing is the authoritative poller, so any
        other answer may still become TRUE later.

        Returning the message rather than a bool is deliberate. CBOS distinguishes
        FALSE ("still pending") from SKIP, and collapsing both to False cost us a
        day: SKIP was polled thirty times and reported as a timeout, while the
        log asserted "a file CBOS expects is still unregistered" - our
        interpretation, printed as though it were CBOS's.
        """
        # Bound before the loop: a configured attempt budget of 0 skips the body
        # entirely, and the timeout line below reads msg.
        msg = ""
        for attempt in range(1, settings.cbos_poll_max_attempts + 1):
            # DEBUG on the raw call: the poll can run for many attempts, and the
            # attempt line below already carries the verdict, which is the part
            # that matters.
            raw = self._call(9, "file_process_status(FILEUPLOAD)",
                             lambda: self._file_upload_status(segment),
                             level=logging.DEBUG, segment=segment,
                             attempt=attempt, max_attempts=settings.cbos_poll_max_attempts)
            msg = self._gtg_msg(raw).upper()
            logger.info("Step 9 FILEUPLOAD poll %d/%d segment=%s MSG=%s",
                        attempt, settings.cbos_poll_max_attempts, segment, msg)

            if msg == "TRUE":
                return msg
            if msg in _TERMINAL_POLL_MESSAGES:
                # Not "still pending" - CBOS has given its answer, so polling it
                # another 29 times only delays the batch and buries the message
                # under identical lines.
                logger.error("Step 9 - FILEUPLOAD returned %s for segment=%s; this is a verdict, not a "
                             "pending state, so polling stops here", msg, segment)
                return msg
            time.sleep(settings.cbos_poll_interval_seconds)

        logger.error("Step 9 - file_process_status polling timed out after %d attempts (segment=%s), "
                     "last MSG=%s", settings.cbos_poll_max_attempts, segment, msg)
        return POLL_TIMED_OUT

    @staticmethod
    def _gtg_msg(response) -> str:
        rows = _decode_body(response, "file_process_status").get("Data") or []
        return str(rows[0].get("MSG", "")).strip() if rows else ""


# --------------------------------------------------------------------------
# CBOSClient - the actual HTTP calls against the two CBOS hosts.
# --------------------------------------------------------------------------

class CBOSClient(BaseCBOSClient):
    def __init__(self) -> None:
        required = ("cbos_core_base_url", "cbos_gtg_base_url", "cbos_login_id", "cbos_password")
        missing = [name.upper() for name in required if not getattr(settings, name)]
        if missing:
            raise CBOSUploadError(
                f"CBOS_MODE=REAL requires {', '.join(missing)} in .env (no committed defaults)"
            )

    def _gtg_url(self, path: str) -> str:
        return f"{settings.cbos_gtg_base_url.rstrip('/')}{path}"

    def _core_url(self, path: str) -> str:
        return f"{settings.cbos_core_base_url.rstrip('/')}{path}"

    def _handle(self, url: str, response) -> dict:
        logger.debug("Response <- %s: status=%s body=%s", url, response.status_code, response.text[:1000])
        if not response.ok:
            logger.error("Response <- %s failed: %s %s", url, response.status_code, response.text)
            raise CBOSUploadError(f"{url} failed: {response.status_code} {response.text}")

        try:
            parsed = response.json()
        except ValueError as exc:
            raise CBOSUploadError(f"{url} returned non-JSON response: {response.text}") from exc

        body = _decode_body(parsed, url)
        _raise_on_failed_status(url, body)
        # DEBUG, not INFO: BaseCBOSClient._call already narrates every step in
        # both modes. These lines are the literal wire view - full URL, HTTP
        # status, untruncated body - which is what you want when the narrative
        # itself looks wrong. Failure paths above stay at ERROR.
        logger.debug("Response <- %s: %s", url, body)
        return body

    def _post(self, url: str, payload: dict) -> dict:
        logger.debug("Request -> %s: %s", url, _redact(payload))
        try:
            response = requests.post(url, json=payload, timeout=settings.cbos_timeout_seconds)
        except requests.RequestException as exc:
            logger.error("Request -> %s failed: %s", url, exc)
            raise CBOSUploadError(f"Request to {url} failed: {exc}") from exc
        return self._handle(url, response)

    def _post_multipart(self, url: str, data: dict, files: dict) -> dict:
        logger.debug("Request -> %s: data=%s file=%s", url, _redact(data), files.get("file", (None,))[0])
        try:
            response = requests.post(url, data=data, files=files,
                                     timeout=settings.cbos_upload_timeout_seconds)
        except requests.RequestException as exc:
            logger.error("Request -> %s failed: %s", url, exc)
            raise CBOSUploadError(f"Request to {url} failed: {exc}") from exc
        return self._handle(url, response)

    def _get_new_trade_process(self, segment: str, trade_date: str) -> dict:
        payload = {
            "GROUPNAME": segment,
            "LOGINID": settings.cbos_login_id,
            "PASSWORD": settings.cbos_password,
            "TRADEDATE": _to_cbos_date(trade_date),
            "PROCESSID": "0",  # "0" = create a new process; read back from Table1/Table2
        }
        return self._post(self._core_url(GET_NEW_TRADE_PROCESS_PATH), payload)

    def _check_process_id_exist(self, segment: str) -> dict:
        payload = {"Segment": segment, "ProcessName": PROCESS_NAME_CHECK_PROCESS_ID,
                   "UserID": settings.cbos_login_id}
        return self._post(self._gtg_url(FILE_PROCESS_STATUS_PATH), payload)

    def _get_upload_settings(self, upload_id: str) -> dict:
        return self._post(self._core_url(GET_UPLOAD_SETTINGS_PATH), {"UPLOADID": upload_id})

    def _upload_chunk(self, upload_id: str, guid: str, file_name: str, chunk_bytes: bytes,
                      current_chunk: int, total_chunks: int) -> dict:
        # Exactly the five multipart fields the API doc specifies for Step 5.
        # UPLOADID is deliberately NOT among them: the doc doesn't list it here
        # (it belongs to Steps 4 and 7), and real CBOS answered every chunk with
        # a 500 "Object reference not set to an instance of an object" while we
        # were sending it. The chunk endpoint keys off Guid alone - the UploadID
        # is bound to the GUID folder later, by Step 7's uploadid/uploadfoldername.
        data = {
            "CurrentChunk": str(current_chunk),
            "TotalChunks": str(total_chunks),
            "Guid": guid,
            "FileName": file_name,
        }
        files = {"file": (file_name, chunk_bytes)}
        return self._post_multipart(self._core_url(UPLOAD_CHUNK_PATH), data, files)

    def _get_existing_process_id(self, segment: str, trade_date: str) -> dict:
        payload = {
            "TAG": "EXISTINGPROCESSID",
            "LOGINID": settings.cbos_login_id,
            "FILTER1": segment,
            "FILTER2": _to_cbos_date(trade_date),
            "extraoption2": "",
            "extraoption3": "",
        }
        return self._post(self._core_url(GET_EXISTING_PROCESS_ID_PATH), payload)

    def _create_file_entry(self, upload_id: str, guid: str, file_name: str, process_id: str,
                           trade_date: str) -> dict:
        payload = {
            "uploadid": upload_id,
            "loginid": settings.cbos_login_id,
            "uploadfoldername": guid,
            "uploadfilename": file_name,
            "ipaddress": _server_ip(),
            "file": "",
            "paraM1": _to_cbos_date(trade_date),
            "paraM2": "", "paraM3": "", "paraM4": "", "paraM5": "",
            "paraM6": "", "paraM7": "", "paraM8": "",
            "paraM9": process_id,
            "chunkFileUpload": "YES",
        }
        return self._post(self._core_url(SAVE_UPLOAD_FILE_PATH), payload)

    def _update_step_optional(self, process_id: str, step_no) -> dict:
        # ISOPTIONAL="0" is the doc's (counter-intuitive) value for "make this
        # step optional / not mandatory". Unverified against real CBOS - if it
        # wants "1", this is the one line to change.
        payload = {"PROCESSID": process_id, "STEPNO": step_no, "ISOPTIONAL": "0"}
        return self._post(self._core_url(UPDATE_IS_MANDATORY_PATH), payload)

    def _file_upload_status(self, segment: str) -> dict:
        payload = {"Segment": segment, "ProcessName": PROCESS_NAME_FILE_UPLOAD_STATUS,
                   "UserID": settings.cbos_login_id}
        return self._post(self._gtg_url(FILE_PROCESS_STATUS_PATH), payload)

    def get_expected_filename(self, segment: str, upload_id: str) -> dict:
        """Step 39 - optional cross-check against upload_matching's own pattern
        engine. Not on the critical path, so not part of the interface."""
        return self._post(self._gtg_url(GET_EXPECTED_FILENAME_PATH),
                          {"segment": segment, "uploadid": upload_id})


# --------------------------------------------------------------------------
# MockCBOSClient - canned responses, same shape as real CBOS, no network.
#
# Table2 and the upload settings come from the shared mock dataset
# (mock_cbos/data.py), so the in-process mock and the standalone mock server
# agree on each segment's pipeline.
#
# Scenario rules (checked against the file name, case-insensitively):
#   contains "success" -> always succeeds
#   contains "fail"    -> always fails (at Step 9, like a real processing
#                         rejection would)
#   neither            -> random, per CBOS_MOCK_RANDOM_SUCCESS_RATE
# --------------------------------------------------------------------------

class MockCBOSClient(BaseCBOSClient):
    def __init__(self) -> None:
        self._next_process_id = 17658
        self._segment_poll_state: dict[str, dict] = {}
        self._segment_file_names: dict[str, list[str]] = {}
        self.marked_optional: list[tuple] = []  # (process_id, step_no) per Step-8 call, for assertions
        self.upload_calls: list[tuple] = []     # (upload_id, file_name) per Step-5 chunk, for assertions
        self.reserve_calls = 0                  # count of Step-2 reservations, for assertions

    def _decide_outcome(self, file_names: list[str]) -> bool:
        """True = succeed, False = fail. See class docstring for the rules."""
        joined = " ".join(file_names).lower()
        if "success" in joined:
            return True
        if "fail" in joined:
            return False
        return random.random() < settings.cbos_mock_random_success_rate

    def _get_new_trade_process(self, segment: str, trade_date: str) -> dict:
        from mock_cbos import data

        self.reserve_calls += 1
        process_id = self._next_process_id
        self._next_process_id += 1
        logger.debug("[MOCK] Process ID created: PROCESSID=%s (GROUPNAME=%s, TRADEDATE=%s)",
                    process_id, segment, trade_date)
        return {
            "Status": "Success",
            "Result": {
                "Table1": [{"PROCESSID": process_id, "ISRUNNABLE": True, "ISAUTOUPLOAD": True}],
                "Table2": data.table2_for(segment),
            },
        }

    def _check_process_id_exist(self, segment: str) -> dict:
        pid = self._next_process_id - 1
        logger.debug("[MOCK] Check process id exist: segment=%s -> PROCESSID=%s", segment, pid)
        return {"Status": "Success", "Data": [{"MSG": f"PROCESS ID ALREADY GENERATED : {pid}"}]}

    def _get_upload_settings(self, upload_id: str) -> dict:
        from mock_cbos import data

        setting = data.upload_setting(str(upload_id))
        uid = int(upload_id) if str(upload_id).isdigit() else upload_id
        logger.debug("[MOCK] Upload settings fetched: UPLOADID=%s -> %s", upload_id, setting)
        return {"Status": "Success", "Result": [{"ID": uid, **setting}]}

    def _upload_chunk(self, upload_id: str, guid: str, file_name: str, chunk_bytes: bytes,
                      current_chunk: int, total_chunks: int) -> dict:
        self.upload_calls.append((str(upload_id), file_name))
        logger.debug("[MOCK] Chunk uploaded: %s chunk %d/%d (upload_id=%s, guid=%s)",
                    file_name, current_chunk, total_chunks, upload_id, guid)
        return {"Status": "ChunkUploaded", "Guid": guid}

    def _get_existing_process_id(self, segment: str, trade_date: str) -> dict:
        logger.debug("[MOCK] Existing process id lookup: segment=%s trade_date=%s", segment, trade_date)
        return {"Status": "Success",
                "Result": [{"_KEY": self._next_process_id - 1, "_DESC": f"{segment} - {trade_date}"}]}

    def _create_file_entry(self, upload_id: str, guid: str, file_name: str, process_id: str,
                           trade_date: str) -> dict:
        logger.debug("[MOCK] File entry created: %s (upload_id=%s, guid=%s)", file_name, upload_id, guid)
        self._segment_file_names.setdefault(trade_date, []).append(file_name)
        return {"Status": "Success", "Result": "File entry saved successfully"}

    def _update_step_optional(self, process_id: str, step_no) -> dict:
        self.marked_optional.append((str(process_id), step_no))
        logger.debug("[MOCK] Marked step optional: process_id=%s stepno=%s", process_id, step_no)
        return {"Status": "Success", "Result": {"Table1": [{"MSG": "Updated Successfully"}]}}

    def _file_upload_status(self, segment: str) -> dict:
        # Poll state is tracked per segment (the real Step 9 payload carries no
        # per-batch trade_date handle either).
        state = self._segment_poll_state.setdefault(segment, {"attempts": 0, "outcome": None})
        state["attempts"] += 1

        if state["attempts"] <= settings.cbos_mock_pending_polls:
            logger.debug("[MOCK] FILEUPLOAD FALSE/pending (attempt %d/%d, segment=%s)",
                        state["attempts"], settings.cbos_mock_pending_polls, segment)
            return {"Status": "Success", "Data": [{"MSG": "FALSE"}]}

        if state["outcome"] is None:
            file_names = [n for names in self._segment_file_names.values() for n in names]
            state["outcome"] = "TRUE" if self._decide_outcome(file_names) else "FALSE"

        logger.debug("[MOCK] FILEUPLOAD %s (segment=%s)", state["outcome"], segment)
        return {"Status": "Success", "Data": [{"MSG": state["outcome"]}]}

    def get_expected_filename(self, segment: str, upload_id: str) -> dict:
        from mock_cbos import data

        return {"Status": "Success",
                "Data": [{"UploadID": upload_id, "ExpectedFileNamePattern1": data.expected_pattern(upload_id)}]}


# --------------------------------------------------------------------------
# Factory - CBOS_MODE picks the implementation once per process.
# --------------------------------------------------------------------------

_client: BaseCBOSClient | None = None


def get_cbos_client() -> BaseCBOSClient:
    global _client
    if _client is None:
        mode = settings.cbos_mode.strip().upper()
        if mode == "REAL":
            _client = CBOSClient()
        elif mode == "MOCK":
            _client = MockCBOSClient()
        else:
            raise CBOSUploadError(f"Invalid CBOS_MODE '{settings.cbos_mode}' - must be MOCK or REAL")
        logger.info("cbos_client: using %s (CBOS_MODE=%s)", type(_client).__name__, mode)
    return _client


def set_cbos_client(client: BaseCBOSClient | None) -> None:
    """Inject a specific client (e.g. a MockCBOSClient in a test), bypassing the
    CBOS_MODE factory. Pass None via reset_cbos_client() to clear it."""
    global _client
    _client = client


def reset_cbos_client() -> None:
    """Clear the cached client so the next get_cbos_client() rebuilds from
    CBOS_MODE. Call between tests."""
    global _client
    _client = None
