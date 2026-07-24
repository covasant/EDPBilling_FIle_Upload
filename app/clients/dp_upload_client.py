"""Client for CAMS's "DP File Upload API" - a shared upload-master system used
by the settlement segment, switchable between a real implementation and a mock
one via CBOS_SETL_MODE (see app/core/config.py).

Unrelated to app/clients/cbos_client.py's CBOS trade-upload API: different
host, different auth (a Session-Value header, not LOGINID/PASSWORD-in-body),
different step vocabulary. Kept as a separate client rather than extending
cbos_client.py, per DP_FileUpload_API_Integration_Guide.docx and
DP_FileUpload_API_Actual_Requests.docx.

One settlement upload = one file, driven through 7 steps (this repo's caller,
app/services/settlement_service.py, always starts at Step 2 - the orchestrator
supplies upload_id directly, so Step 1's dropdown lookup is ops/manual-only):

  Step 1 - getdetailsuploadmaster(FILL_DISPLAYNAME_DROPDOWN) -> list of {ID, DISPLAY_NAME, DEPOSITORY}
  Step 2 - getdetailsuploadmaster(GET, Upload_Id)  -> full per-type config, echoed into Steps 3+5
  Step 3 - uploadfilevalidate                      -> validates before upload; error stops here
  Step 4 - uploadchunks (loop)                      -> one call per chunk, sequential
  Step 5 - uploadfilemaster                         -> registers the file, returns Tran_Id
  Step 6 - GetFileUploadStatus (poll)                -> 0/1/2 pending, 3 success, 4 error
  Step 7 - uploadprocess (conditional)                -> only if process_required

--------------------------------------------------------------------------
The interface
--------------------------------------------------------------------------

Callers use the methods on BaseDPUploadClient and nothing else - they get
parsed dataclasses/values, never the DP API's raw response envelope.

An adapter supplies one raw call per step. Everything else (envelope parsing,
the chunk loop, the poll loop) lives once on the base class and is shared by
both adapters:

  DPUploadClient     - makes the actual HTTP calls against the DP upload host.
  MockDPUploadClient - returns canned dicts with the exact same shape, so the
                       orchestration above runs unmodified in either mode.

get_dp_upload_client() is the factory; it reads settings.cbos_setl_mode once
and returns the matching singleton.
"""

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests

from app.clients.cbos_client import _redact, _summarise
from app.core.config import settings

logger = logging.getLogger("dp_upload_client")

# The two source docs disagree on host/prefix (see open question in the plan);
# both left blank in config, resolved into one prefixed path here so a single
# confirmed value fixes every endpoint at once.
GET_UPLOAD_MASTER_PATH = "/getdetailsuploadmaster"  # both Step 1 (dropdown) and Step 2 (details)
VALIDATE_FILE_PATH = "/uploadfilevalidate"
UPLOAD_CHUNK_PATH = "/uploadchunks"
FINALIZE_UPLOAD_PATH = "/uploadfilemaster"
GET_STATUS_PATH = "/GetFileUploadStatus"
RUN_PROCESS_PATH = "/uploadprocess"

# Description[0] (or a bare Description string) that marks Step 3 as a
# validation failure. Both docs' response shapes are handled - see
# _is_validation_error.
_ERROR_MARKER = "ERROR_MSG"

# Step 4's per-chunk verdicts.
_CHUNK_UPLOADED = "ChunkUploaded"
_FILE_UPLOADED = "FileUploaded"
_CHUNK_FAILED = "Failed"

# Step 6's status codes (GetFileUploadStatus).
STATUS_STARTED = 0
STATUS_IN_PROCESS = 1
STATUS_INSERTING = 2
STATUS_SUCCESS = 3
STATUS_ERROR = 4
_PENDING_STATUS_CODES = {STATUS_STARTED, STATUS_IN_PROCESS, STATUS_INSERTING}

# Step 6 stand-in for "no terminal status inside the attempt budget" - not a
# value the API sends, mirrors cbos_client.POLL_TIMED_OUT.
POLL_TIMED_OUT = -1


class DPUploadError(Exception):
    pass


@dataclass(frozen=True)
class UploadMasterSummary:
    """One row of Step 1's dropdown list."""

    id: str
    display_name: str
    depository: str


@dataclass(frozen=True)
class UploadMasterConfig:
    """Step 2's full per-Upload_Id config. Every field named here must be
    echoed verbatim (renamed to the DP API's PascalCase field names) into
    Steps 3 and 5 - see _config_payload_fields."""

    upload_id: str
    display_name: str
    table_name: str
    db_name: str
    sp_name: str
    separator: str
    file_extension: str
    insert_type: str
    top_rows_skip: int
    bottom_rows_skip: int
    column_count: int
    column_validator: int
    process_required: bool
    upload_file_size_greater: str
    module: str
    depository: str
    upload_is_transid_required: int
    is_first_col_identity: int
    upload_save_file_ext: int
    group_col: str
    file_name_compare_operator: str
    file_name_to_compare: str
    columns_name: str
    raw: dict


def _parse_process_required(value: object) -> bool:
    """process_required arrives as "1"/1/"0"/0 across the two docs' samples -
    a strict truthy check rather than bool(), since bool("0") is True in
    Python (same trap cbos_client._parse_isoptional guards against)."""
    return str(value).strip() in ("1", "true", "True", "TRUE")


def _parse_upload_master_config(row: dict) -> UploadMasterConfig:
    return UploadMasterConfig(
        upload_id=str(row.get("id") or row.get("Upload_Id") or ""),
        display_name=str(row.get("display_name") or ""),
        table_name=str(row.get("table_name") or ""),
        db_name=str(row.get("db_name") or ""),
        sp_name=str(row.get("sp_name") or ""),
        separator=str(row.get("separator") or ""),
        file_extension=str(row.get("file_extension") or ""),
        insert_type=str(row.get("insert_type") or ""),
        top_rows_skip=int(row.get("top_rows_skip") or 0),
        bottom_rows_skip=int(row.get("bottom_rows_skip") or 0),
        column_count=int(row.get("column_count") or 0),
        column_validator=int(row.get("column_validator") or 0),
        process_required=_parse_process_required(row.get("process_required")),
        upload_file_size_greater=str(row.get("Upload_File_Size_Greater") or "No"),
        module=str(row.get("module") or ""),
        depository=str(row.get("depository") or ""),
        upload_is_transid_required=int(row.get("Upload_Is_Transid_Required") or 0),
        is_first_col_identity=int(row.get("is_first_col_identity") or 0),
        upload_save_file_ext=int(row.get("Upload_Save_File_Ext") or 0),
        group_col=str(row.get("group_col") or ""),
        file_name_compare_operator=str(row.get("file_name_compare_operator") or ""),
        file_name_to_compare=str(row.get("file_name_to_compare") or ""),
        columns_name=str(row.get("Columns_Name") or ""),
        raw=row,
    )


def _config_payload_fields(config: UploadMasterConfig, file_name: str) -> dict:
    """The config fields Steps 3 and 5 both echo back verbatim (PascalCase),
    per the actual-requests doc's captured payloads. Shared here so the two
    steps can never drift apart from each other."""
    return {
        "Display_Name": config.display_name,
        "Table_Name": config.table_name,
        "DB_Name": config.db_name,
        "SP_Name": config.sp_name,
        "Separator": config.separator,
        "File_Extension": config.file_extension,
        "Columns_Name": config.columns_name,
        "Upload_Id": config.upload_id,
        "Insert_Type": config.insert_type,
        "Top_Rows_Skip": config.top_rows_skip,
        "Is_Column_Row_Same": 1,
        "Bottom_Rows_Skip": config.bottom_rows_skip,
        "Module": config.module,
        "Upload_File_Size_Greater": config.upload_file_size_greater,
        "Column_Count": config.column_count,
        "Upload_Column_Validator": config.column_validator,
        "File_Name": file_name,
        "Is_First_ColIdentity": config.is_first_col_identity,
        "Upload_Save_File_Ext": config.upload_save_file_ext,
        "Upload_Is_TransId_Required": config.upload_is_transid_required,
        "File_Type_Upload": "All",
        "FileNameCompareOperator": config.file_name_compare_operator,
        "FileNameToCompare": config.file_name_to_compare,
        "Group_Col": config.group_col,
    }


def _is_validation_error(response: dict) -> bool:
    """Step 3's error shape is inconsistent between the two docs: Description
    is a list (["Error_msg"]) in the integration guide, a bare string ("" or
    an error message) in the actual-requests doc's real sample. Both are
    checked so neither doc's shape is silently missed."""
    description = response.get("Description")
    if isinstance(description, list):
        return bool(description) and str(description[0]).strip().upper() == _ERROR_MARKER
    if isinstance(description, str):
        return description.strip().upper() == _ERROR_MARKER
    return False


class BaseDPUploadClient(ABC):
    # ---- step logging, mirrors cbos_client.BaseCBOSClient._call -----------

    def _call(self, step, api: str, raw_call, level: int = logging.INFO, **params):
        logger.log(level, "Step %s %s REQUEST  %s", step, api, _redact(params))
        try:
            response = raw_call()
        except Exception as exc:
            logger.error("Step %s %s FAILED   %s", step, api, exc)
            raise
        logger.log(level, "Step %s %s RESPONSE %s", step, api, _summarise(response))
        return response

    # ---- the raw DP upload API calls an adapter must provide ---------------

    @abstractmethod
    def _list_upload_masters(self) -> dict:
        """Step 1 raw call (ops/manual only - see module docstring)."""

    @abstractmethod
    def _get_upload_master_details(self, upload_id: str) -> dict:
        """Step 2 raw call."""

    @abstractmethod
    def _validate_file(self, config: UploadMasterConfig, file_name: str, unique_identifier: str) -> dict:
        """Step 3 raw call."""

    @abstractmethod
    def _upload_chunk(
        self, file_name: str, chunk_bytes: bytes, current_chunk: int, total_chunks: int
    ) -> dict:
        """Step 4 raw call, one per chunk."""

    @abstractmethod
    def _finalize_upload(
        self, config: UploadMasterConfig, file_name: str, chunk_full_path: str, unique_identifier: str
    ) -> dict:
        """Step 5 raw call."""

    @abstractmethod
    def _get_upload_status(self, tran_id: str) -> dict:
        """Step 6 raw call, one poll."""

    @abstractmethod
    def _run_process(self, config: UploadMasterConfig, unique_identifier: str, tran_id: str) -> dict:
        """Step 7 raw call."""

    # ---- the interface callers use -----------------------------------------

    def list_upload_masters(self) -> list[UploadMasterSummary]:
        """Step 1. Ops/manual dropdown lookup - the orchestrator's one call
        (POST /settlements/uploads) never uses this, it already supplies
        upload_id directly."""
        raw = self._call(1, "getdetailsuploadmaster(FILL_DISPLAYNAME_DROPDOWN)", self._list_upload_masters)
        result = raw.get("Result") or []
        return [
            UploadMasterSummary(
                id=str(row.get("ID", "")),
                display_name=str(row.get("DISPLAY_NAME", "")),
                depository=str(row.get("DEPOSITORY", "")),
            )
            for row in result
        ]

    def get_upload_master_config(self, upload_id: str) -> UploadMasterConfig:
        """Step 2. Full config for one Upload_Id - the settlement flow's
        actual starting point. Raises DPUploadError if the API returns no
        matching row."""
        raw = self._call(
            2,
            "getdetailsuploadmaster(GET)",
            lambda: self._get_upload_master_details(upload_id),
            upload_id=upload_id,
        )
        result = raw.get("Result") or []
        if not result:
            raise DPUploadError(f"getdetailsuploadmaster(GET) returned no config for Upload_Id={upload_id}")
        return _parse_upload_master_config(result[0])

    def validate_file(self, config: UploadMasterConfig, file_name: str, unique_identifier: str) -> None:
        """Step 3. Raises DPUploadError if the API reports a validation
        failure - callers must not proceed to Step 4 in that case."""
        response = self._call(
            3,
            "uploadfilevalidate",
            lambda: self._validate_file(config, file_name, unique_identifier),
            upload_id=config.upload_id,
            file_name=file_name,
            unique_identifier=unique_identifier,
        )
        if _is_validation_error(response):
            raise DPUploadError(f"uploadfilevalidate rejected {file_name}: {response}")

    def upload_chunks(self, file_bytes: bytes, file_name: str) -> str:
        """Step 4. Streams file_bytes in chunk_setl_size_kb-sized chunks,
        sequentially (never in parallel, per the doc), under one GUID-prefixed
        chunked name generated once per call. Returns the ChunkFullPath
        (the FileName from the last chunk's response) for Step 5.

        Raises DPUploadError on a Failed chunk or a fCount/TotalChunks
        mismatch on the last chunk - per the doc, that means "abort and
        restart the whole flow from Step 3", which is the caller's job
        (app/services/settlement_service.py), not this method's.
        """
        chunk_size = max(1, settings.chunk_setl_size_kb) * 1024
        guid = uuid.uuid4().hex
        chunked_name = f"{guid}_{file_name}"
        total_chunks = max(1, (len(file_bytes) + chunk_size - 1) // chunk_size)

        logger.info(
            "Step 4 - uploadchunks: %s (%d bytes) as %s in %d chunk(s) of <=%d KB",
            file_name,
            len(file_bytes),
            chunked_name,
            total_chunks,
            settings.chunk_setl_size_kb,
        )

        chunk_full_path = None
        for current_chunk in range(total_chunks):
            start = current_chunk * chunk_size
            chunk_bytes = file_bytes[start : start + chunk_size]
            response = self._call(
                4,
                "uploadchunks",
                lambda _bytes=chunk_bytes, _chunk=current_chunk: self._upload_chunk(
                    chunked_name, _bytes, _chunk, total_chunks
                ),
                level=logging.DEBUG,
                file_name=chunked_name,
                current_chunk=current_chunk,
                total_chunks=total_chunks,
                chunk_bytes=len(chunk_bytes),
            )
            status = str(response.get("Status", ""))
            if status == _CHUNK_FAILED:
                raise DPUploadError(f"uploadchunks failed on chunk {current_chunk}/{total_chunks}: {response}")

            is_last = current_chunk == total_chunks - 1
            if is_last:
                if status != _FILE_UPLOADED:
                    raise DPUploadError(
                        f"uploadchunks: last chunk did not report {_FILE_UPLOADED}: {response}"
                    )
                f_count = int(response.get("fCount", -1))
                if f_count != total_chunks:
                    raise DPUploadError(
                        f"uploadchunks: fCount={f_count} != totalChunks={total_chunks}, "
                        f"some chunks missing: {response}"
                    )
                chunk_full_path = str(response.get("FileName") or chunked_name)
            elif status != _CHUNK_UPLOADED:
                logger.warning(
                    "Step 4 - unexpected Status=%s on non-last chunk %d/%d: %s",
                    status,
                    current_chunk,
                    total_chunks,
                    response,
                )

        if chunk_full_path is None:
            # Defensive - total_chunks >= 1 always executes the loop body once,
            # so this only trips if the last chunk's branch was somehow skipped.
            raise DPUploadError("uploadchunks completed without a ChunkFullPath")

        logger.info("Step 4 complete: %s uploaded as %s", file_name, chunk_full_path)
        return chunk_full_path

    def finalize_upload(
        self, config: UploadMasterConfig, file_name: str, chunk_full_path: str, unique_identifier: str
    ) -> str:
        """Step 5. Registers the uploaded file, returns Tran_Id for Step 6.
        Raises DPUploadError if the API doesn't return one."""
        response = self._call(
            5,
            "uploadfilemaster",
            lambda: self._finalize_upload(config, file_name, chunk_full_path, unique_identifier),
            upload_id=config.upload_id,
            file_name=file_name,
            chunk_full_path=chunk_full_path,
            unique_identifier=unique_identifier,
        )
        tran_id = response.get("TranId")
        if tran_id in (None, ""):
            result = response.get("Result") or []
            if result and result[0].get("Tran_Id") is not None:
                tran_id = result[0]["Tran_Id"]
        if tran_id in (None, ""):
            raise DPUploadError(f"uploadfilemaster returned no Tran_Id/TranId: {response}")
        return str(tran_id)

    def check_status_once(self, tran_id: str) -> tuple[int, str]:
        """One GetFileUploadStatus call, no loop/sleep - used by the manual
        status-poll endpoint (GET /settlements/uploads/{id}/status) and, in a
        loop, by poll_status below."""
        raw = self._call(6, "GetFileUploadStatus", lambda: self._get_upload_status(tran_id), tran_id=tran_id)
        result = raw.get("Result") or []
        if not result:
            logger.warning("Step 6 - GetFileUploadStatus returned no Result for Tran_Id=%s", tran_id)
            return POLL_TIMED_OUT, ""
        row = result[0]
        return int(row.get("Status", POLL_TIMED_OUT)), str(row.get("Description") or "")

    def poll_status(self, tran_id: str) -> tuple[int, str]:
        """Step 6. Polls GetFileUploadStatus up to cbos_setl_poll_max_attempts
        times, cbos_setl_poll_interval_seconds apart. Returns (status_code,
        description) - status_code is POLL_TIMED_OUT if no terminal status
        (3 or 4) arrived inside the attempt budget."""
        status_code = POLL_TIMED_OUT
        description = ""
        max_attempts = settings.cbos_setl_poll_max_attempts
        for attempt in range(1, max_attempts + 1):
            status_code, description = self.check_status_once(tran_id)
            logger.info(
                "Step 6 poll %d/%d Tran_Id=%s Status=%d Description=%s",
                attempt,
                max_attempts,
                tran_id,
                status_code,
                description,
            )
            if status_code not in _PENDING_STATUS_CODES:
                return status_code, description
            time.sleep(settings.cbos_setl_poll_interval_seconds)

        logger.error(
            "Step 6 - GetFileUploadStatus polling timed out after %d attempts (Tran_Id=%s), last Status=%s",
            max_attempts,
            tran_id,
            status_code,
        )
        return POLL_TIMED_OUT, description

    def run_process(self, config: UploadMasterConfig, unique_identifier: str, tran_id: str) -> str:
        """Step 7. Only called when config.process_required is True. Returns
        the API's RESPONSE text."""
        response = self._call(
            7,
            "uploadprocess",
            lambda: self._run_process(config, unique_identifier, tran_id),
            upload_id=config.upload_id,
            tran_id=tran_id,
        )
        result = response.get("Result") or []
        if not result:
            raise DPUploadError(f"uploadprocess returned no Result: {response}")
        return str(result[0].get("RESPONSE") or "")


# --------------------------------------------------------------------------
# DPUploadClient - the actual HTTP calls against the DP upload host.
# --------------------------------------------------------------------------


class DPUploadClient(BaseDPUploadClient):
    def __init__(self) -> None:
        required = ("cbos_setl_base_url", "cbos_setl_seskey", "cbos_setl_user_id")
        missing = [name.upper() for name in required if not getattr(settings, name)]
        if missing:
            raise DPUploadError(
                f"CBOS_SETL_MODE=REAL requires {', '.join(missing)} in .env (no committed defaults)"
            )

    def _url(self, path: str) -> str:
        prefix = settings.cbos_setl_api_prefix.rstrip("/")
        return f"{settings.cbos_setl_base_url.rstrip('/')}{prefix}{path}"

    def _headers(self) -> dict:
        return {"Session-Value": f"{settings.cbos_setl_seskey}|{settings.cbos_setl_user_id}"}

    def _handle(self, url: str, response) -> dict:
        logger.debug(
            "Response <- %s: status=%s body=%s", url, response.status_code, response.text[:1000]
        )
        if not response.ok:
            logger.error("Response <- %s failed: %s %s", url, response.status_code, response.text)
            raise DPUploadError(f"{url} failed: {response.status_code} {response.text}")
        try:
            body = response.json()
        except ValueError as exc:
            raise DPUploadError(f"{url} returned non-JSON response: {response.text}") from exc
        if not isinstance(body, dict):
            raise DPUploadError(f"{url} returned {type(body).__name__}, expected a JSON object")
        logger.debug("Response <- %s: %s", url, body)
        return body

    def _post(self, url: str, payload: dict) -> dict:
        logger.debug("Request -> %s: %s", url, _redact(payload))
        try:
            response = requests.post(
                url, json=payload, headers=self._headers(), timeout=settings.cbos_setl_timeout_seconds
            )
        except requests.RequestException as exc:
            logger.error("Request -> %s failed: %s", url, exc)
            raise DPUploadError(f"Request to {url} failed: {exc}") from exc
        return self._handle(url, response)

    def _post_multipart(self, url: str, data: dict, files: dict) -> dict:
        logger.debug("Request -> %s: data=%s", url, _redact(data))
        try:
            response = requests.post(
                url,
                data=data,
                files=files,
                headers=self._headers(),
                timeout=settings.cbos_setl_upload_timeout_seconds,
            )
        except requests.RequestException as exc:
            logger.error("Request -> %s failed: %s", url, exc)
            raise DPUploadError(f"Request to {url} failed: {exc}") from exc
        return self._handle(url, response)

    def _list_upload_masters(self) -> dict:
        return self._post(
            self._url(GET_UPLOAD_MASTER_PATH), {"Method_Name": "FILL_DISPLAYNAME_DROPDOWN", "Group_Col": ""}
        )

    def _get_upload_master_details(self, upload_id: str) -> dict:
        return self._post(self._url(GET_UPLOAD_MASTER_PATH), {"Method_Name": "GET", "Upload_Id": int(upload_id)})

    def _validate_file(self, config: UploadMasterConfig, file_name: str, unique_identifier: str) -> dict:
        payload = {
            "Method_Name": "Upload",
            **_config_payload_fields(config, file_name),
            "uniqueidentifier": unique_identifier,
            "Created_By": settings.cbos_setl_created_by,
        }
        return self._post(self._url(VALIDATE_FILE_PATH), payload)

    def _upload_chunk(
        self, file_name: str, chunk_bytes: bytes, current_chunk: int, total_chunks: int
    ) -> dict:
        data = {"CurrentChunk": str(current_chunk), "TotalChunks": str(total_chunks), "FileType": "All"}
        files = {"file": (file_name, chunk_bytes)}
        # FileName is a form field alongside `file`, per the doc's FormData shape.
        data["FileName"] = file_name
        return self._post_multipart(self._url(UPLOAD_CHUNK_PATH), data, files)

    def _finalize_upload(
        self, config: UploadMasterConfig, file_name: str, chunk_full_path: str, unique_identifier: str
    ) -> dict:
        payload = {
            "Method_Name": "Upload",
            **_config_payload_fields(config, file_name),
            "uniqueidentifier": unique_identifier,
            "Created_By": settings.cbos_setl_created_by,
            "ChunkFullPath": chunk_full_path,
        }
        return self._post(self._url(FINALIZE_UPLOAD_PATH), payload)

    def _get_upload_status(self, tran_id: str) -> dict:
        return self._post(self._url(GET_STATUS_PATH), {"Id": int(tran_id), "Method_Name": "Details"})

    def _run_process(self, config: UploadMasterConfig, unique_identifier: str, tran_id: str) -> dict:
        payload = {
            "Method_Name": "UPLOAD",
            "Upload_Id": config.upload_id,
            "DB_Name": config.db_name,
            "SP_Name": config.sp_name,
            "Columns_Name": config.columns_name,
            "uniqueidentifier": unique_identifier,
            "Created_By": settings.cbos_setl_created_by,
            "TranId": tran_id,
        }
        return self._post(self._url(RUN_PROCESS_PATH), payload)


# --------------------------------------------------------------------------
# MockDPUploadClient - canned responses, same shape as the real API, no
# network. Scenario rules (checked against the file name, case-insensitively),
# same convention as cbos_client.MockCBOSClient:
#   contains "success" -> always succeeds
#   contains "fail"    -> always fails at Step 3 (validation)
#   neither            -> random, per CBOS_SETL_MOCK_RANDOM_SUCCESS_RATE
#                         (decided at Step 3; a "random fail" fails validation
#                         the same way an explicit "fail" filename does)
# --------------------------------------------------------------------------


class MockDPUploadClient(BaseDPUploadClient):
    def __init__(self) -> None:
        self._next_tran_id = 168530
        self._poll_attempts: dict[str, int] = {}
        self.chunk_calls: list[str] = []  # file_name per chunk, for assertions

    def _decide_outcome(self, file_name: str) -> bool:
        name = file_name.lower()
        if "success" in name:
            return True
        if "fail" in name:
            return False
        import random

        return random.random() < settings.cbos_setl_mock_random_success_rate

    def _list_upload_masters(self) -> dict:
        return {
            "Status": "Success",
            "Result": [
                {"ID": 22, "DISPLAY_NAME": "DP CD07 Upload Settlement Info", "DEPOSITORY": "CDSL"},
                {"ID": 16, "DISPLAY_NAME": "DP_NSDL_Settlement", "DEPOSITORY": "NSDL"},
            ],
            "Total_Row": 2,
        }

    def _get_upload_master_details(self, upload_id: str) -> dict:
        return {
            "Status": "Success",
            "Result": [
                {
                    "id": int(upload_id) if str(upload_id).isdigit() else upload_id,
                    "module": "DP",
                    "display_name": "DP CD07 Upload Settlement Info",
                    "file_extension": "*",
                    "separator": "~",
                    "top_rows_skip": 0,
                    "bottom_rows_skip": 0,
                    "db_name": "CBOS-DP",
                    "table_name": "TUpload_CD07_Upload_Settlement_Info",
                    "schema_name": "dbo",
                    "process_required": "1",
                    "sp_name": "USP_DP_CD07_Upload_Settlement_Info",
                    "insert_type": "1",
                    "group_col": "DP",
                    "is_active": True,
                    "column_count": 0,
                    "column_validator": 2,
                    "Columns_Name": "SETTLEMENT_NUMBER,SETTLEMENT_DATE,REMARKS",
                    "is_first_col_identity": 2,
                    "Upload_File_Size_Greater": "No",
                    "Upload_Save_File_Ext": 2,
                    "Upload_Is_Transid_Required": 2,
                    "priority": 1,
                    "depository": "CDSL",
                    "file_name_compare_operator": "",
                    "file_name_to_compare": "",
                }
            ],
            "Total_Row": 1,
        }

    def _validate_file(self, config: UploadMasterConfig, file_name: str, unique_identifier: str) -> dict:
        if self._decide_outcome(file_name) is False:
            logger.debug("[MOCK] uploadfilevalidate rejecting %s", file_name)
            return {"Status": "Success", "Description": ["Error_msg"], "Result": '[{"Error_msg": "Invalid file"}]'}
        return {"Status": "Success", "Description": "", "Result": "Success", "Total_Row": 0, "TranId": None}

    def _upload_chunk(
        self, file_name: str, chunk_bytes: bytes, current_chunk: int, total_chunks: int
    ) -> dict:
        self.chunk_calls.append(file_name)
        is_last = current_chunk == total_chunks - 1
        logger.debug("[MOCK] chunk %d/%d of %s", current_chunk, total_chunks, file_name)
        return {
            "Status": _FILE_UPLOADED if is_last else _CHUNK_UPLOADED,
            "FileName": file_name,
            "currentChunk": str(current_chunk),
            "totalChunks": str(total_chunks),
            "fCount": str(current_chunk + 1),
        }

    def _finalize_upload(
        self, config: UploadMasterConfig, file_name: str, chunk_full_path: str, unique_identifier: str
    ) -> dict:
        tran_id = self._next_tran_id
        self._next_tran_id += 1
        logger.debug("[MOCK] uploadfilemaster %s -> Tran_Id=%s", file_name, tran_id)
        return {
            "Status": "Success",
            "Result": [
                {
                    "Tran_Id": tran_id,
                    "File_Name": file_name,
                    "Status": 0,
                    "Description": "Started",
                    "Module": config.module or "DP",
                }
            ],
            "TranId": str(tran_id),
        }

    def _get_upload_status(self, tran_id: str) -> dict:
        attempts = self._poll_attempts.get(tran_id, 0) + 1
        self._poll_attempts[tran_id] = attempts
        if attempts <= settings.cbos_setl_mock_pending_polls:
            logger.debug("[MOCK] GetFileUploadStatus Tran_Id=%s -> Inserting (attempt %d)", tran_id, attempts)
            return {"Status": "Success", "Result": [{"Tran_Id": tran_id, "Status": STATUS_INSERTING, "Description": "Inserting"}]}
        logger.debug("[MOCK] GetFileUploadStatus Tran_Id=%s -> Success", tran_id)
        return {
            "Status": "Success",
            "Result": [{"Tran_Id": tran_id, "Status": STATUS_SUCCESS, "Description": "Inserted successfully"}],
        }

    def _run_process(self, config: UploadMasterConfig, unique_identifier: str, tran_id: str) -> dict:
        logger.debug("[MOCK] uploadprocess Tran_Id=%s", tran_id)
        return {"Status": "Success", "Result": [{"RESPONSE": "Processed successfully"}]}


# --------------------------------------------------------------------------
# Factory - CBOS_SETL_MODE picks the implementation once per process.
# --------------------------------------------------------------------------

_client: BaseDPUploadClient | None = None


def get_dp_upload_client() -> BaseDPUploadClient:
    global _client
    if _client is None:
        mode = settings.cbos_setl_mode.strip().upper()
        if mode == "REAL":
            _client = DPUploadClient()
        elif mode == "MOCK":
            _client = MockDPUploadClient()
        else:
            raise DPUploadError(f"Invalid CBOS_SETL_MODE '{settings.cbos_setl_mode}' - must be MOCK or REAL")
        logger.info("dp_upload_client: using %s (CBOS_SETL_MODE=%s)", type(_client).__name__, mode)
    return _client


def set_dp_upload_client(client: BaseDPUploadClient | None) -> None:
    """Inject a specific client (e.g. a MockDPUploadClient in a test), bypassing
    the CBOS_SETL_MODE factory. Pass None via reset_dp_upload_client() to clear it."""
    global _client
    _client = client


def reset_dp_upload_client() -> None:
    """Clear the cached client so the next get_dp_upload_client() rebuilds from
    CBOS_SETL_MODE. Call between tests."""
    global _client
    _client = None
