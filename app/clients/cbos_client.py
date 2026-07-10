"""Client for CBOS's trade-upload API, switchable between a real
implementation and a mock one via CBOS_MODE (see app/core/config.py).

The upload of one file is a 5-call sequence (numbered to match the step
numbers CBOS's own API docs use - there is no Step 1/5 call, those belong to
other flows this service doesn't use):

  Step 2 - getNewTradeProcess                          -> PROCESSID + Table2 candidates
  Step 3 - GetNewTradeProcessPromodalUploadSettings     -> settings for one UPLOADID (no longer used for extension validation - the first Table2 candidate is always selected)
  Step 4 - SaveTradePromodalUploadChunkFile             -> the file itself, chunked
  Step 6 - SaveNewTradeProcessPromodalUploadFile        -> registers the uploaded chunks
  Step 7 - file_process_status                          -> poll until CBOS finishes processing

Two implementations share one interface (BaseCBOSClient):

  RealCBOSClient - makes the actual HTTP calls against CBOS_BASE_URL.
  MockCBOSClient - returns canned responses with the exact same shape, so
                   upload_service.py's orchestration logic (and everything
                   above it - queue, worker, scheduler) runs unmodified in
                   either mode.

get_cbos_client() is the factory: it reads settings.cbos_mode once and
returns the matching singleton. upload_service.py never imports the classes
directly - it calls the module-level functions below (get_new_trade_process,
select_upload_id, upload_file_chunks, save_trade_process_upload_file,
poll_file_process_status), which delegate to whichever client the factory
picked. This keeps the service layer, worker, queue, and scheduler
completely unaware that mock/real switching exists.
"""

import logging
import random
import time
from abc import ABC, abstractmethod
from pathlib import Path

import requests

from app.core.config import get_settings

logger = logging.getLogger("cbos_client")
settings = get_settings()

GET_NEW_TRADE_PROCESS_PATH = "/getNewTradeProcess"
GET_UPLOAD_SETTINGS_PATH = "/GetNewTradeProcessPromodalUploadSettings"
UPLOAD_CHUNK_PATH = "/SaveTradePromodalUploadChunkFile"
SAVE_UPLOAD_FILE_PATH = "/SaveNewTradeProcessPromodalUploadFile"
FILE_PROCESS_STATUS_PATH = "/file_process_status"


class CBOSUploadError(Exception):
    pass


# --------------------------------------------------------------------------
# Shared interface - both clients implement exactly these 5 calls, with
# exactly the same request args and the same response envelope shape:
#   {"Status": "Success", "Result": ...}  /  {"Status": "Success", "Data": ...}
# --------------------------------------------------------------------------

class BaseCBOSClient(ABC):
    @abstractmethod
    def get_new_trade_process(self, segment: str, login_id: str, trade_date: str) -> dict:
        """Step 2."""

    @abstractmethod
    def get_upload_settings(self, upload_id: str) -> dict:
        """Step 3."""

    @abstractmethod
    def upload_chunk(self, upload_id: str, guid: str, file_name: str, chunk_bytes: bytes,
                      current_chunk: int, total_chunks: int) -> dict:
        """Step 4, one call per chunk."""

    @abstractmethod
    def create_file_entry(self, upload_id: str, guid: str, file_name: str, login_id: str, process_id: str) -> dict:
        """Step 6."""

    @abstractmethod
    def file_upload_status(self, process_id: str, upload_id: str, guid: str) -> dict:
        """Step 7, one poll call."""


# --------------------------------------------------------------------------
# RealCBOSClient - the actual HTTP calls against CBOS_BASE_URL.
# --------------------------------------------------------------------------

class RealCBOSClient(BaseCBOSClient):
    def _url(self, path: str) -> str:
        return f"{settings.cbos_base_url.rstrip('/')}{path}"

    def _post(self, path: str, payload: dict) -> dict:
        url = self._url(path)
        logger.info("Request -> %s: %s", path, payload)
        try:
            response = requests.post(url, json=payload, timeout=settings.cbos_timeout_seconds)
        except requests.RequestException as exc:
            logger.error("Request -> %s failed: %s", path, exc)
            raise CBOSUploadError(f"Request to {path} failed: {exc}") from exc

        logger.debug("Response <- %s: status=%s body=%s", path, response.status_code, response.text[:1000])
        if not response.ok:
            logger.error("Response <- %s failed: %s %s", path, response.status_code, response.text)
            raise CBOSUploadError(f"{path} failed: {response.status_code} {response.text}")

        try:
            body = response.json()
        except ValueError as exc:
            raise CBOSUploadError(f"{path} returned non-JSON response: {response.text}") from exc

        logger.info("Response <- %s: %s", path, body)
        return body

    def _post_multipart(self, path: str, data: dict, files: dict) -> dict:
        url = self._url(path)
        logger.info("Request -> %s: data=%s file=%s", path, data, files.get("file", (None,))[0])
        try:
            response = requests.post(url, data=data, files=files, timeout=settings.cbos_timeout_seconds)
        except requests.RequestException as exc:
            logger.error("Request -> %s failed: %s", path, exc)
            raise CBOSUploadError(f"Request to {path} failed: {exc}") from exc

        logger.debug("Response <- %s: status=%s body=%s", path, response.status_code, response.text[:1000])
        if not response.ok:
            logger.error("Response <- %s failed: %s %s", path, response.status_code, response.text)
            raise CBOSUploadError(f"{path} failed: {response.status_code} {response.text}")

        try:
            body = response.json()
        except ValueError as exc:
            raise CBOSUploadError(f"{path} returned non-JSON response: {response.text}") from exc

        logger.info("Response <- %s: %s", path, body)
        return body

    def get_new_trade_process(self, segment: str, login_id: str, trade_date: str) -> dict:
        payload = {
            "GROUPNAME": segment,
            "LOGINID": login_id,
            "TRADEDATE": trade_date,
            "PROCESSID": "0",
        }
        return self._post(GET_NEW_TRADE_PROCESS_PATH, payload)

    def get_upload_settings(self, upload_id: str) -> dict:
        payload = {"UPLOADID": upload_id}
        return self._post(GET_UPLOAD_SETTINGS_PATH, payload)

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
        return self._post_multipart(UPLOAD_CHUNK_PATH, data, files)

    def create_file_entry(self, upload_id: str, guid: str, file_name: str, login_id: str, process_id: str) -> dict:
        payload = {
            "uploadid": upload_id,
            "uploadfoldername": guid,
            "uploadfilename": file_name,
            "loginid": login_id,
            "paraM9": process_id,
        }
        return self._post(SAVE_UPLOAD_FILE_PATH, payload)

    def file_upload_status(self, process_id: str, upload_id: str, guid: str) -> dict:
        payload = {"ProcessName": "FILEUPLOAD", "PROCESSID": process_id, "UPLOADID": upload_id, "GUID": guid}
        return self._post(FILE_PROCESS_STATUS_PATH, payload)


# --------------------------------------------------------------------------
# MockCBOSClient - canned responses, same shape as real CBOS, no network.
#
# Scenario rules (checked against the file name, case-insensitively):
#   contains "success" -> always succeeds
#   contains "fail"     -> always fails (at Step 7, like a real processing
#                          rejection would)
#   neither             -> random, per CBOS_MOCK_RANDOM_SUCCESS_RATE
#                          (Scenario 3 - exercises the retry path)
#
# Steps 2/3/4/6 always return their canned success response in every
# scenario - only Step 7 (file_upload_status) resolves TRUE/FALSE, since
# that's the realistic point where CBOS reports final outcome. Step 7 also
# stays PENDING for CBOS_MOCK_PENDING_POLLS calls first, so the real
# poll/retry loop in poll_file_process_status() is actually exercised.
# --------------------------------------------------------------------------

class MockCBOSClient(BaseCBOSClient):
    def __init__(self) -> None:
        self._next_process_id = 17658
        self._poll_state: dict[str, dict] = {}  # guid -> {"attempts": int, "outcome": "TRUE"/"FALSE"|None}

    def _decide_outcome(self, file_name: str) -> bool:
        """True = succeed, False = fail. See class docstring for the rules."""
        name = file_name.lower()
        if "success" in name:
            return True
        if "fail" in name:
            return False
        return random.random() < settings.cbos_mock_random_success_rate

    def get_new_trade_process(self, segment: str, login_id: str, trade_date: str) -> dict:
        process_id = self._next_process_id
        self._next_process_id += 1
        response = {
            "Status": "Success",
            "Result": {
                "Table1": [{"PROCESSID": process_id, "ISRUNNABLE": True}],
                "Table2": [{"UPLOADID": 81, "NAME": "BSE SCRIP", "STATUS": "PENDING"}],
            },
        }
        logger.info("[MOCK] Process ID created: PROCESSID=%s (GROUPNAME=%s, LOGINID=%s, TRADEDATE=%s)",
                    process_id, segment, login_id, trade_date)
        return response

    def get_upload_settings(self, upload_id: str) -> dict:
        response = {
            "Status": "Success",
            "Result": [
                {"ID": int(upload_id), "NAME": "BSE SCRIP", "FILEEXTENSION": "CSV"},
                {"ID": int(upload_id), "NAME": "BSE SCRIP", "FILEEXTENSION": "XLSX"},
                {"ID": int(upload_id), "NAME": "BSE SCRIP", "FILEEXTENSION": "TXT"},
            ],
        }
        logger.info("[MOCK] Upload settings fetched: UPLOADID=%s", upload_id)
        return response

    def upload_chunk(self, upload_id: str, guid: str, file_name: str, chunk_bytes: bytes,
                      current_chunk: int, total_chunks: int) -> dict:
        response = {"Status": "ChunkUploaded", "Guid": guid}
        logger.info("[MOCK] Chunk uploaded: %s chunk %d/%d (guid=%s)", file_name, current_chunk, total_chunks, guid)
        return response

    def create_file_entry(self, upload_id: str, guid: str, file_name: str, login_id: str, process_id: str) -> dict:
        response = {"Status": "Success", "Result": "File entry created successfully"}
        logger.info("[MOCK] File entry created: %s (upload_id=%s, guid=%s)", file_name, upload_id, guid)
        return response

    def file_upload_status(self, process_id: str, upload_id: str, guid: str) -> dict:
        state = self._poll_state.setdefault(guid, {"attempts": 0, "outcome": None, "file_name": None})
        state["attempts"] += 1

        if state["attempts"] <= settings.cbos_mock_pending_polls:
            logger.info("[MOCK] FILEUPLOAD PENDING (attempt %d/%d, guid=%s)",
                        state["attempts"], settings.cbos_mock_pending_polls, guid)
            return {"Status": "Success", "Data": [{"MSG": "PENDING"}]}

        if state["outcome"] is None:
            # file_upload_status only receives process_id/upload_id/guid, not
            # the file name, so the scenario decision is made against the
            # guid's associated file name recorded by upload_chunk() via
            # record_file_name() - see poll_file_process_status() below.
            state["outcome"] = "TRUE" if self._decide_outcome(state.get("file_name") or "") else "FALSE"

        if state["outcome"] == "TRUE":
            logger.info("[MOCK] FILEUPLOAD TRUE (guid=%s)", guid)
        else:
            logger.info("[MOCK] FILEUPLOAD FALSE (guid=%s)", guid)

        return {"Status": "Success", "Data": [{"MSG": state["outcome"]}]}

    def record_file_name(self, guid: str, file_name: str) -> None:
        """Called by upload_file_chunks() so file_upload_status() can apply
        the filename-based success/fail/random scenario at poll time, even
        though the real file_upload_status request doesn't carry a filename."""
        self._poll_state.setdefault(guid, {"attempts": 0, "outcome": None, "file_name": None})
        self._poll_state[guid]["file_name"] = file_name


# --------------------------------------------------------------------------
# Factory - CBOS_MODE picks the implementation once per process.
# --------------------------------------------------------------------------

_client: BaseCBOSClient | None = None


def get_cbos_client() -> BaseCBOSClient:
    global _client
    if _client is None:
        mode = settings.cbos_mode.strip().upper()
        if mode == "REAL":
            _client = RealCBOSClient()
        elif mode == "MOCK":
            _client = MockCBOSClient()
        else:
            raise CBOSUploadError(f"Invalid CBOS_MODE '{settings.cbos_mode}' - must be MOCK or REAL")
        logger.info("cbos_client: using %s (CBOS_MODE=%s)", type(_client).__name__, mode)
    return _client


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


def get_upload_settings(upload_id: str) -> dict:
    logger.info("Step 3 - GetNewTradeProcessPromodalUploadSettings: UPLOADID=%s", upload_id)
    return get_cbos_client().get_upload_settings(upload_id)


def select_upload_id(table2: list[dict], file_name: str) -> tuple[str, dict]:
    """Pick the first Table2 candidate's UPLOADID (step 3). No file
    extension/type validation is performed - every file is processed
    regardless of extension."""
    for candidate in table2:
        upload_id = candidate.get("UPLOADID")
        if upload_id is None:
            continue
        upload_id = str(upload_id)
        upload_settings_response = get_upload_settings(upload_id)
        logger.info("Step 3 - selected UPLOADID=%s for %s", upload_id, file_name)
        return upload_id, upload_settings_response

    raise CBOSUploadError(f"No UPLOADID with a non-null value found in Table2 for file '{file_name}'")


def upload_file_chunks(file_path: Path, upload_id: str, guid: str) -> None:
    """No chunking - the whole file is always sent as a single chunk
    (CurrentChunk=1, TotalChunks=1), regardless of file size."""
    client = get_cbos_client()
    if isinstance(client, MockCBOSClient):
        client.record_file_name(guid, file_path.name)

    file_size = file_path.stat().st_size
    logger.info(
        "Step 4 - SaveTradePromodalUploadChunkFile: %s (%d bytes) as a single chunk, guid=%s",
        file_path.name, file_size, guid,
    )

    file_bytes = file_path.read_bytes()
    client.upload_chunk(upload_id, guid, file_path.name, file_bytes, 1, 1)

    logger.info("Step 4 complete: %s uploaded in one chunk", file_path.name)


def save_trade_process_upload_file(upload_id: str, guid: str, file_name: str, login_id: str, process_id: str) -> dict:
    logger.info("Step 6 - SaveNewTradeProcessPromodalUploadFile: %s (upload_id=%s, guid=%s)", file_name, upload_id, guid)
    return get_cbos_client().create_file_entry(upload_id, guid, file_name, login_id, process_id)


def poll_file_process_status(process_id: str, upload_id: str, guid: str) -> dict:
    client = get_cbos_client()

    for attempt in range(1, settings.cbos_poll_max_attempts + 1):
        logger.debug("Step 7 - file_process_status attempt %d/%d", attempt, settings.cbos_poll_max_attempts)
        result = client.file_upload_status(process_id, upload_id, guid)
        rows = result.get("Data") or []
        msg = str(rows[0].get("MSG", "")).strip().upper() if rows else ""
        logger.info("Step 7 - file_process_status attempt %d: MSG=%s", attempt, msg)

        if msg == "TRUE":
            return result
        if msg in ("FALSE", "FAILED", "ERROR"):
            raise CBOSUploadError(f"file_process_status reported failure: {result}")

        time.sleep(settings.cbos_poll_interval_seconds)

    raise CBOSUploadError(
        f"file_process_status polling timed out after {settings.cbos_poll_max_attempts} attempts "
        f"waiting for MSG=TRUE (process_id={process_id}, upload_id={upload_id})"
    )
