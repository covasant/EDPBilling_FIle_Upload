"""Client for CBOS's trade-upload API, switchable between a real
implementation and a mock one via CBOS_MODE (see app/core/config.py).

Endpoints are split across two hosts, per
EDP_Trade_Process_API_Documentation_v4.pdf:

  GTG host  (settings.cbos_gtg_base_url)  - file_process_status GTG/CHK calls
  CORE host (settings.cbos_core_base_url) - process/brokerage CORE calls

One file-upload batch (one segment + one trade date) is an 8-step sequence
(numbered 2-9; there is no Step 1 - the holiday check was removed):

  Step 2 - getNewTradeProcess (PROCESSID=0)              -> PROCESSID + Table2 (all UploadID candidates)
  Step 3 - CheckProcessIDExist                           -> confirms Step 2's PROCESSID registered
  Step 4 - GetNewTradeProcessPromodalUploadSettings      -> per-UploadID pattern/extension/column rules
                                                             (called once per Table2 candidate - see
                                                             app/services/upload_matching.py)
  Step 5 - SaveTradePromodalUploadChunkFile              -> one call per matched file (chunking disabled,
                                                             sent as a single CurrentChunk=0/TotalChunks=1 call)
  Step 6 - getdropdown(EXISTINGPROCESSID)                -> optional confirmation lookup, not on the critical path
  Step 7 - SaveNewTradeProcessPromodalUploadFile         -> registers each uploaded file
  Step 8 - getTradeProcess                               -> triggers the batch once, after every matched file
                                                             in the segment/date has been uploaded+registered
  Step 9 - file_process_status (FILEUPLOAD)              -> poll per SEGMENT (not per file/guid) until MSG=TRUE

Two implementations share one interface (BaseCBOSClient):

  CBOSClient - makes the actual HTTP calls against the two CBOS hosts.
  MockCBOSClient - returns canned responses with the exact same shape, so
                   upload_service.py's orchestration logic (and everything
                   above it - queue, worker, scheduler) runs unmodified in
                   either mode.

get_cbos_client() is the factory: it reads settings.cbos_mode once and
returns the matching singleton. upload_service.py never imports the classes
directly - it calls the module-level functions below, which delegate to
whichever client the factory picked.
"""

import logging
import socket

import random
import time
from abc import ABC, abstractmethod
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
TRIGGER_TRADE_PROCESS_PATH = "/v1/api/process/getTradeProcess"

# ProcessName values for the shared file_process_status (GTG) endpoint.
PROCESS_NAME_CHECK_PROCESS_ID = "CheckProcessIDExist"
PROCESS_NAME_FILE_UPLOAD_STATUS = "FILEUPLOAD"


def _to_cbos_date(folder_date: str) -> str:
    """Reformats a folder_date (settings.date_folder_format, e.g. dd-mm-yyyy)
    into the yyyy-mm-dd shape CBOS's TRADEDATE/paraM1 fields require. Only
    affects the outgoing CBOS payload - folder names, DB records, and
    business date logic elsewhere are untouched."""
    return datetime.strptime(folder_date, settings.date_folder_format).strftime("%Y-%m-%d")


def _server_ip() -> str:
    """Best-effort local IP for the documented "ipaddress" field on Step 7.
    Falls back to an empty string if it can't be resolved, rather than
    failing the whole upload over a non-critical field."""
    try:
        return socket.gethostbyname(socket.gethostname())
    except socket.error:
        return ""


class CBOSUploadError(Exception):
    pass


# Response bodies that indicate a business-level failure even though the
# HTTP call itself returned 200. Only trips when the body actually carries a
# recognized failure marker - an unrecognized/absent Status is treated as
# success, since we don't have a confirmed real-CBOS failure shape yet.
_FAILURE_STATUSES = {"FAILED", "FAILURE", "ERROR"}


def _raise_on_failed_status(path: str, body: dict) -> None:
    status = str(body.get("Status", "")).strip().upper()
    if status in _FAILURE_STATUSES:
        logger.error("Response <- %s reported Status=%s: %s", path, status, body)
        raise CBOSUploadError(f"{path} returned Status={status}: {body}")


def _extract_gtg_msg(response: dict) -> str:
    rows = response.get("Data") or []
    return str(rows[0].get("MSG", "")).strip() if rows else ""


# --------------------------------------------------------------------------
# Shared interface - both clients implement exactly these calls, with
# exactly the same request args and the same response envelope shape:
#   {"Status": "Success", "Result": ...}  /  {"Status": "Success", "Data": ...}
# --------------------------------------------------------------------------

class BaseCBOSClient(ABC):
    @abstractmethod
    def get_new_trade_process(self, segment: str, login_id: str, trade_date: str) -> dict:
        """Step 2."""

    @abstractmethod
    def check_process_id_exist(self, segment: str, login_id: str) -> dict:
        """Step 3."""

    @abstractmethod
    def get_upload_settings(self, upload_id: str) -> dict:
        """Step 4."""

    @abstractmethod
    def upload_chunk(self, upload_id: str, guid: str, file_name: str, chunk_bytes: bytes,
                      current_chunk: int, total_chunks: int) -> dict:
        """Step 5, one call per chunk."""

    @abstractmethod
    def get_existing_process_id(self, segment: str, login_id: str, trade_date: str) -> dict:
        """Step 6. Not on the critical path today - see
        get_existing_process_id() below."""

    @abstractmethod
    def create_file_entry(self, upload_id: str, guid: str, file_name: str, login_id: str, process_id: str,
                           trade_date: str) -> dict:
        """Step 7."""

    @abstractmethod
    def trigger_process(self, login_id: str, segment: str, trade_date: str, process_id: str) -> dict:
        """Step 8 - distinct endpoint from Step 2's getNewTradeProcess. Called
        once per segment/date batch, never per file."""

    @abstractmethod
    def file_upload_status(self, segment: str, login_id: str) -> dict:
        """Step 9, one poll call - segment-level, not per file/guid."""


# --------------------------------------------------------------------------
# CBOSClient - the actual HTTP calls against the two CBOS hosts.
# --------------------------------------------------------------------------

class CBOSClient(BaseCBOSClient):
    def __init__(self) -> None:
        if not settings.cbos_login_id or not settings.cbos_password:
            raise CBOSUploadError(
                "CBOS_LOGIN_ID and CBOS_PASSWORD are mandatory when CBOS_MODE=REAL"
            )

    def _gtg_url(self, path: str) -> str:
        return f"{settings.cbos_gtg_base_url.rstrip('/')}{path}"

    def _core_url(self, path: str) -> str:
        return f"{settings.cbos_core_base_url.rstrip('/')}{path}"

    def _post(self, url: str, payload: dict) -> dict:
        logger.info("Request -> %s: %s", url, payload)
        try:
            response = requests.post(url, json=payload, timeout=settings.cbos_timeout_seconds)
        except requests.RequestException as exc:
            logger.error("Request -> %s failed: %s", url, exc)
            raise CBOSUploadError(f"Request to {url} failed: {exc}") from exc

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

    def _post_multipart(self, url: str, data: dict, files: dict) -> dict:
        logger.info("Request -> %s: data=%s file=%s", url, data, files.get("file", (None,))[0])
        try:
            response = requests.post(url, data=data, files=files, timeout=settings.cbos_timeout_seconds)
        except requests.RequestException as exc:
            logger.error("Request -> %s failed: %s", url, exc)
            raise CBOSUploadError(f"Request to {url} failed: {exc}") from exc

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

    def get_new_trade_process(self, segment: str, login_id: str, trade_date: str) -> dict:
        payload = {
            "GROUPNAME": segment,
            "LOGINID": login_id,
            "PASSWORD": settings.cbos_password,
            "TRADEDATE": _to_cbos_date(trade_date),
            "PROCESSID": "0",  # "0" = create a new process; PROCESSID/UPLOADID are read back from Table1/Table2
        }
        return self._post(self._core_url(GET_NEW_TRADE_PROCESS_PATH), payload)

    def check_process_id_exist(self, segment: str, login_id: str) -> dict:
        payload = {"Segment": segment, "ProcessName": PROCESS_NAME_CHECK_PROCESS_ID, "UserID": login_id}
        return self._post(self._gtg_url(FILE_PROCESS_STATUS_PATH), payload)

    def get_upload_settings(self, upload_id: str) -> dict:
        payload = {"UPLOADID": upload_id}
        return self._post(self._core_url(GET_UPLOAD_SETTINGS_PATH), payload)

    def upload_chunk(self, upload_id: str, guid: str, file_name: str, chunk_bytes: bytes,
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

    def get_existing_process_id(self, segment: str, login_id: str, trade_date: str) -> dict:
        payload = {
            "TAG": "EXISTINGPROCESSID",
            "LOGINID": login_id,
            "FILTER1": segment,
            "FILTER2": _to_cbos_date(trade_date),
            "extraoption2": "",
            "extraoption3": "",
        }
        return self._post(self._core_url(GET_EXISTING_PROCESS_ID_PATH), payload)

    def create_file_entry(self, upload_id: str, guid: str, file_name: str, login_id: str, process_id: str,
                           trade_date: str) -> dict:
        payload = {
            "uploadid": upload_id,
            "loginid": login_id,
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

    def trigger_process(self, login_id: str, segment: str, trade_date: str, process_id: str) -> dict:
        payload = {
            "loginid": login_id,
            "segment": segment,
            "tradedate": _to_cbos_date(trade_date),
            "processid": process_id,
        }
        return self._post(self._core_url(TRIGGER_TRADE_PROCESS_PATH), payload)

    def file_upload_status(self, segment: str, login_id: str) -> dict:
        payload = {"Segment": segment, "ProcessName": PROCESS_NAME_FILE_UPLOAD_STATUS, "UserID": login_id}
        return self._post(self._gtg_url(FILE_PROCESS_STATUS_PATH), payload)

    def get_expected_filename(self, segment: str, upload_id: str) -> dict:
        """Step 39 - optional cross-check against upload_matching's own
        pattern engine. Not on the critical path."""
        payload = {"segment": segment, "uploadid": upload_id}
        return self._post(self._gtg_url(GET_EXPECTED_FILENAME_PATH), payload)


# --------------------------------------------------------------------------
# MockCBOSClient - canned responses, same shape as real CBOS, no network.
#
# Table2 mirrors a small, representative slice of the real 28-step upload
# pipeline (see the PDF's page 5 table) so upload_matching.py's per-file
# UploadID resolution has more than one candidate to choose between, instead
# of the old single hardcoded UploadID=81 for every file.
#
# Scenario rules (checked against the file name, case-insensitively):
#   contains "success" -> always succeeds
#   contains "fail"     -> always fails (at Step 9, like a real processing
#                          rejection would)
#   neither             -> random, per CBOS_MOCK_RANDOM_SUCCESS_RATE
# --------------------------------------------------------------------------

_MOCK_UPLOAD_SETTINGS = {
    "81": {"NAME": "BSE SCRIP", "FILE NAME": "SCRIP", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "TXT", "NO. OF COLUMNS": 30},
    "82": {"NAME": "NSE SCRIP", "FILE NAME": "SCRIP", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "TXT", "NO. OF COLUMNS": 30},
    "85": {"NAME": "BSE TRADE FILE", "FILE NAME": "VN", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "CSV", "NO. OF COLUMNS": None},
    "94": {"NAME": "STT NOT TO CHARGE", "FILE NAME": "BR", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "TXT", "NO. OF COLUMNS": None},
    "172": {"NAME": "POSITION VARIATION", "FILE NAME": "C_VAR1_", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "DAT", "NO. OF COLUMNS": None},
    "201": {"NAME": "BILLING WORKBOOK", "FILE NAME": "VN", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "XLSX", "NO. OF COLUMNS": None},
    "202": {"NAME": "LEGACY SCRIP MASTER", "FILE NAME": "SCRIP", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "XLS", "NO. OF COLUMNS": None},
}


class MockCBOSClient(BaseCBOSClient):
    def __init__(self) -> None:
        self._next_process_id = 17658
        self._segment_poll_state: dict[str, dict] = {}  # "segment|date" -> {"attempts": int, "outcome": str|None}
        self._segment_file_names: dict[str, list[str]] = {}

    def _decide_outcome(self, file_names: list[str]) -> bool:
        """True = succeed, False = fail. See class docstring for the rules."""
        joined = " ".join(file_names).lower()
        if "success" in joined:
            return True
        if "fail" in joined:
            return False
        return random.random() < settings.cbos_mock_random_success_rate

    def get_new_trade_process(self, segment: str, login_id: str, trade_date: str) -> dict:
        process_id = self._next_process_id
        self._next_process_id += 1
        # Table2 is sourced from the shared mock dataset (mock_cbos/data.py), so
        # the in-process mock and the standalone mock server agree on each
        # segment's pipeline - MCX yields 127/534/535/320, EQ its 12 steps, etc.
        from mock_cbos import data
        table2 = data.table2_for(segment)
        response = {
            "Status": "Success",
            "Result": {
                "Table1": [{"PROCESSID": process_id, "ISRUNNABLE": True, "ISAUTOUPLOAD": True}],
                "Table2": table2,
            },
        }
        logger.info("[MOCK] Process ID created: PROCESSID=%s (GROUPNAME=%s, LOGINID=%s, TRADEDATE=%s)",
                    process_id, segment, login_id, trade_date)
        return response

    def check_process_id_exist(self, segment: str, login_id: str) -> dict:
        pid = self._next_process_id - 1
        logger.info("[MOCK] Check process id exist: segment=%s -> PROCESSID=%s", segment, pid)
        return {"Status": "Success", "Data": [{"MSG": f"PROCESS ID ALREADY GENERATED : {pid}"}]}

    def get_upload_settings(self, upload_id: str) -> dict:
        from mock_cbos import data
        setting = data.upload_setting(str(upload_id))
        uid = int(upload_id) if str(upload_id).isdigit() else upload_id
        result = [{"ID": uid, **setting}]
        logger.info("[MOCK] Upload settings fetched: UPLOADID=%s -> %s", upload_id, setting)
        return {"Status": "Success", "Result": result}

    def upload_chunk(self, upload_id: str, guid: str, file_name: str, chunk_bytes: bytes,
                      current_chunk: int, total_chunks: int) -> dict:
        response = {"Status": "ChunkUploaded", "Guid": guid}
        logger.info("[MOCK] Chunk uploaded: %s chunk %d/%d (upload_id=%s, guid=%s)",
                     file_name, current_chunk, total_chunks, upload_id, guid)
        return response

    def get_existing_process_id(self, segment: str, login_id: str, trade_date: str) -> dict:
        response = {"Status": "Success", "Result": [{"_KEY": self._next_process_id - 1, "_DESC": f"{login_id} - {trade_date}"}]}
        logger.info("[MOCK] Existing process id lookup: segment=%s trade_date=%s", segment, trade_date)
        return response

    def create_file_entry(self, upload_id: str, guid: str, file_name: str, login_id: str, process_id: str,
                           trade_date: str) -> dict:
        response = {"Status": "Success", "Result": "File entry saved successfully"}
        logger.info("[MOCK] File entry created: %s (upload_id=%s, guid=%s)", file_name, upload_id, guid)
        key = f"{trade_date}"
        self._segment_file_names.setdefault(key, []).append(file_name)
        return response

    def trigger_process(self, login_id: str, segment: str, trade_date: str, process_id: str) -> dict:
        logger.info("[MOCK] Trigger process: segment=%s process_id=%s trade_date=%s", segment, process_id, trade_date)
        return {"Status": "Success", "Result": {"MSG": "Process triggered"}}

    def file_upload_status(self, segment: str, login_id: str) -> dict:
        # Poll state is tracked per segment (mock has no per-batch trade_date
        # handle at this call, matching the real Step 9 payload shape).
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
        setting = _MOCK_UPLOAD_SETTINGS.get(str(upload_id), {})
        pattern = setting.get("FILE NAME", "")
        ext = setting.get("FILEEXTENSION", "TXT")
        return {"Status": "Success", "Data": [{"UploadID": upload_id, "ExpectedFileNamePattern1": f"{pattern}_DDMMYY.{ext.lower()}"}]}


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


# --------------------------------------------------------------------------
# Module-level orchestration functions - upload_service.py's integration
# point. These never change shape between MOCK and REAL; they just delegate
# to whichever client get_cbos_client() picked, plus interpret the shared
# response envelope ({"Status": ..., "Result"/"Data": ...}).
# --------------------------------------------------------------------------

def get_new_trade_process(segment: str, login_id: str, trade_date: str) -> dict:
    logger.info("Step 2 - getNewTradeProcess: segment=%s login_id=%s trade_date=%s", segment, login_id, trade_date)
    return get_cbos_client().get_new_trade_process(segment, login_id, trade_date)


def extract_process_id(response: dict) -> str:
    result = response.get("Result") or {}
    table1 = result.get("Table1") or []
    if table1 and table1[0].get("PROCESSID") is not None:
        return str(table1[0]["PROCESSID"])
    raise CBOSUploadError(f"getNewTradeProcess response had no Table1[0].PROCESSID: {response}")


def extract_upload_candidates(response: dict) -> list[dict]:
    result = response.get("Result") or {}
    table2 = result.get("Table2") or []
    if not table2:
        raise CBOSUploadError(f"getNewTradeProcess response had no Table2 upload candidates: {response}")
    return table2


def check_process_id_exist(segment: str, login_id: str) -> dict:
    logger.info("Step 3 - CheckProcessIDExist: segment=%s", segment)
    return get_cbos_client().check_process_id_exist(segment, login_id)


def get_upload_settings(upload_id: str) -> dict:
    logger.info("Step 4 - GetNewTradeProcessPromodalUploadSettings: UPLOADID=%s", upload_id)
    return get_cbos_client().get_upload_settings(upload_id)


def upload_file_chunks(file_path: Path, upload_id: str, guid: str) -> None:
    """Step 5. Chunking is disabled - the whole file is always sent as a
    single chunk (CurrentChunk=0, TotalChunks=1), regardless of file size."""
    client = get_cbos_client()
    file_size = file_path.stat().st_size
    logger.info(
        "Step 5 - SaveTradePromodalUploadChunkFile: %s (%d bytes) as a single chunk, upload_id=%s guid=%s",
        file_path.name, file_size, upload_id, guid,
    )

    file_bytes = file_path.read_bytes()
    client.upload_chunk(upload_id, guid, file_path.name, file_bytes, 0, 1)

    logger.info("Step 5 complete: %s uploaded in one chunk", file_path.name)


def get_existing_process_id(segment: str, login_id: str, trade_date: str) -> dict:
    """Step 6 - confirmation lookup, not on the critical path. Failures here
    are logged but never abort the batch."""
    logger.info("Step 6 - getdropdown(EXISTINGPROCESSID): segment=%s trade_date=%s", segment, trade_date)
    return get_cbos_client().get_existing_process_id(segment, login_id, trade_date)


def save_trade_process_upload_file(upload_id: str, guid: str, file_name: str, login_id: str, process_id: str,
                                    trade_date: str) -> dict:
    logger.info("Step 7 - SaveNewTradeProcessPromodalUploadFile: %s (upload_id=%s, guid=%s)", file_name, upload_id, guid)
    return get_cbos_client().create_file_entry(upload_id, guid, file_name, login_id, process_id, trade_date)


def trigger_process(login_id: str, segment: str, trade_date: str, process_id: str) -> dict:
    """Step 8. Called ONCE per segment/date batch, only after every matched
    file in that batch has completed Steps 5+7."""
    logger.info("Step 8 - getTradeProcess (trigger): segment=%s process_id=%s trade_date=%s",
                segment, process_id, trade_date)
    return get_cbos_client().trigger_process(login_id, segment, trade_date, process_id)


def poll_file_upload_status(segment: str, login_id: str) -> bool:
    """Step 9. Polls per SEGMENT (matches the documented payload shape - no
    PROCESSID/UPLOADID/GUID field), up to cbos_poll_max_attempts times.
    Returns True on MSG=TRUE, False on MSG=FALSE/FAILED/ERROR or timeout."""
    client = get_cbos_client()

    for attempt in range(1, settings.cbos_poll_max_attempts + 1):
        logger.debug("Step 9 - file_process_status(FILEUPLOAD) attempt %d/%d, segment=%s",
                     attempt, settings.cbos_poll_max_attempts, segment)
        result = client.file_upload_status(segment, login_id)
        msg = _extract_gtg_msg(result).strip().upper()
        logger.info("Step 9 - file_process_status attempt %d: segment=%s MSG=%s", attempt, segment, msg)

        if msg == "TRUE":
            return True
        if msg in ("FALSE", "FAILED", "ERROR"):
            # FALSE means "still pending" earlier in the pipeline per the
            # doc, but this poll only starts after Step 8 has already been
            # triggered, so a still-FALSE result after enough attempts is
            # treated the same as a timeout below - keep polling until then.
            time.sleep(settings.cbos_poll_interval_seconds)
            continue

        time.sleep(settings.cbos_poll_interval_seconds)

    logger.error("Step 9 - file_process_status polling timed out after %d attempts (segment=%s)",
                 settings.cbos_poll_max_attempts, segment)
    return False
