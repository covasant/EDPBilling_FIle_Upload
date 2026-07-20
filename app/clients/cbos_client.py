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


# Response bodies that indicate a business-level failure even though the HTTP
# call itself returned 200. Only trips on a recognized failure marker - an
# unrecognized/absent Status is treated as success, since we don't have a
# confirmed real-CBOS failure shape yet.
_FAILURE_STATUSES = {"FAILED", "FAILURE", "ERROR"}


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


# --------------------------------------------------------------------------
# BaseCBOSClient - the single interface. Adapters supply the eight raw calls;
# parsing, chunking and polling are implemented once, here.
# --------------------------------------------------------------------------

class BaseCBOSClient(ABC):

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
        logger.info("Step 2 - getNewTradeProcess: segment=%s trade_date=%s", segment, trade_date)
        response = self._get_new_trade_process(segment, trade_date)
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
        logger.info("ProcessID = %s (segment=%s, %d UploadID candidate(s))",
                    process_id, segment, len(candidates))
        return ProcessReservation(process_id=process_id, candidates=candidates)

    def check_process_exists(self, segment: str) -> str:
        """Step 3. Confirmation that Step 2's PROCESSID registered. Diagnostic
        only - never a gate. Returns CBOS's message."""
        logger.info("Step 3 - CheckProcessIDExist: segment=%s", segment)
        return self._gtg_msg(self._check_process_id_exist(segment))

    def upload_settings(self, upload_id: str) -> dict | None:
        """Step 4. The raw settings row for one UploadID, envelope stripped.
        Returns None if CBOS offered no settings for it.

        The row's fields are interpreted by app/services/upload_matching.py -
        this only unwraps it.
        """
        logger.info("Step 4 - GetNewTradeProcessPromodalUploadSettings: UPLOADID=%s", upload_id)
        result = self._get_upload_settings(upload_id).get("Result") or []
        if not result:
            logger.warning("No upload settings returned for UPLOADID=%s", upload_id)
            return None
        return result[0]

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
                        self._upload_chunk(upload_id, guid, file_path.name, chunk_bytes,
                                           current_chunk, total_chunks)
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
        logger.info("Step 6 - getdropdown(EXISTINGPROCESSID): segment=%s trade_date=%s", segment, trade_date)
        self._get_existing_process_id(segment, trade_date)

    def register_file(self, upload_id: str, guid: str, file_name: str, process_id: str,
                      trade_date: str) -> None:
        """Step 7. Register an uploaded file against its UploadID and PROCESSID."""
        logger.info("Step 7 - SaveNewTradeProcessPromodalUploadFile: %s (upload_id=%s, guid=%s)",
                    file_name, upload_id, guid)
        self._create_file_entry(upload_id, guid, file_name, process_id, trade_date)

    def mark_step_optional(self, process_id: str, step_no) -> None:
        """Step 8. Mark an empty slot optional so it doesn't hold FILEUPLOAD at
        FALSE. One call per empty non-zero slot, after Steps 5+7."""
        logger.info("Step 8 - UpdateNewTradeProcessProcessDetailsIsMandatory: process_id=%s stepno=%s",
                    process_id, step_no)
        self._update_step_optional(process_id, step_no)

    def confirm_upload(self, segment: str) -> bool:
        """Step 9. Poll FILEUPLOAD per SEGMENT (matching the documented payload
        shape - it carries no PROCESSID/UPLOADID/GUID), up to
        cbos_poll_max_attempts times.

        Returns True on MSG=TRUE; False on FALSE/FAILED/ERROR or timeout. False
        does NOT mean the files failed - EDP_Billing is the authoritative poller
        and may see TRUE later.
        """
        for attempt in range(1, settings.cbos_poll_max_attempts + 1):
            msg = self._gtg_msg(self._file_upload_status(segment)).upper()
            logger.info("Step 9 - file_process_status attempt %d/%d: segment=%s MSG=%s",
                        attempt, settings.cbos_poll_max_attempts, segment, msg)

            if msg == "TRUE":
                return True
            # FALSE means "still pending" per the doc; keep polling until the
            # attempt budget runs out, then treat it the same as a timeout.
            time.sleep(settings.cbos_poll_interval_seconds)

        logger.error("Step 9 - file_process_status polling timed out after %d attempts (segment=%s)",
                     settings.cbos_poll_max_attempts, segment)
        return False

    @staticmethod
    def _gtg_msg(response: dict) -> str:
        rows = response.get("Data") or []
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
            body = response.json()
        except ValueError as exc:
            raise CBOSUploadError(f"{url} returned non-JSON response: {response.text}") from exc

        _raise_on_failed_status(url, body)
        logger.info("Response <- %s: %s", url, body)
        return body

    def _post(self, url: str, payload: dict) -> dict:
        logger.info("Request -> %s: %s", url, _redact(payload))
        try:
            response = requests.post(url, json=payload, timeout=settings.cbos_timeout_seconds)
        except requests.RequestException as exc:
            logger.error("Request -> %s failed: %s", url, exc)
            raise CBOSUploadError(f"Request to {url} failed: {exc}") from exc
        return self._handle(url, response)

    def _post_multipart(self, url: str, data: dict, files: dict) -> dict:
        logger.info("Request -> %s: data=%s file=%s", url, _redact(data), files.get("file", (None,))[0])
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
        data = {
            "UPLOADID": upload_id,
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
        logger.info("[MOCK] Process ID created: PROCESSID=%s (GROUPNAME=%s, TRADEDATE=%s)",
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
        logger.info("[MOCK] Check process id exist: segment=%s -> PROCESSID=%s", segment, pid)
        return {"Status": "Success", "Data": [{"MSG": f"PROCESS ID ALREADY GENERATED : {pid}"}]}

    def _get_upload_settings(self, upload_id: str) -> dict:
        from mock_cbos import data

        setting = data.upload_setting(str(upload_id))
        uid = int(upload_id) if str(upload_id).isdigit() else upload_id
        logger.info("[MOCK] Upload settings fetched: UPLOADID=%s -> %s", upload_id, setting)
        return {"Status": "Success", "Result": [{"ID": uid, **setting}]}

    def _upload_chunk(self, upload_id: str, guid: str, file_name: str, chunk_bytes: bytes,
                      current_chunk: int, total_chunks: int) -> dict:
        self.upload_calls.append((str(upload_id), file_name))
        logger.info("[MOCK] Chunk uploaded: %s chunk %d/%d (upload_id=%s, guid=%s)",
                    file_name, current_chunk, total_chunks, upload_id, guid)
        return {"Status": "ChunkUploaded", "Guid": guid}

    def _get_existing_process_id(self, segment: str, trade_date: str) -> dict:
        logger.info("[MOCK] Existing process id lookup: segment=%s trade_date=%s", segment, trade_date)
        return {"Status": "Success",
                "Result": [{"_KEY": self._next_process_id - 1, "_DESC": f"{segment} - {trade_date}"}]}

    def _create_file_entry(self, upload_id: str, guid: str, file_name: str, process_id: str,
                           trade_date: str) -> dict:
        logger.info("[MOCK] File entry created: %s (upload_id=%s, guid=%s)", file_name, upload_id, guid)
        self._segment_file_names.setdefault(trade_date, []).append(file_name)
        return {"Status": "Success", "Result": "File entry saved successfully"}

    def _update_step_optional(self, process_id: str, step_no) -> dict:
        self.marked_optional.append((str(process_id), step_no))
        logger.info("[MOCK] Marked step optional: process_id=%s stepno=%s", process_id, step_no)
        return {"Status": "Success", "Result": {"Table1": [{"MSG": "Updated Successfully"}]}}

    def _file_upload_status(self, segment: str) -> dict:
        # Poll state is tracked per segment (the real Step 9 payload carries no
        # per-batch trade_date handle either).
        state = self._segment_poll_state.setdefault(segment, {"attempts": 0, "outcome": None})
        state["attempts"] += 1

        if state["attempts"] <= settings.cbos_mock_pending_polls:
            logger.info("[MOCK] FILEUPLOAD FALSE/pending (attempt %d/%d, segment=%s)",
                        state["attempts"], settings.cbos_mock_pending_polls, segment)
            return {"Status": "Success", "Data": [{"MSG": "FALSE"}]}

        if state["outcome"] is None:
            file_names = [n for names in self._segment_file_names.values() for n in names]
            state["outcome"] = "TRUE" if self._decide_outcome(file_names) else "FALSE"

        logger.info("[MOCK] FILEUPLOAD %s (segment=%s)", state["outcome"], segment)
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
