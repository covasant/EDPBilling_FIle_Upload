"""Mock CBOS v5 server.

A standalone FastAPI app that mimics the real CBOS trade-process API
(EDP_Trade_Process_API_Documentation_V5.docx + docs/postman/
edp_trade_process_openapi.json) closely enough that this repo's CBOSClient can
run end-to-end against it with zero code changes - point both
CBOS base URLs at this server:

    CBOS_MODE=REAL
    CBOS_CORE_BASE_URL=http://localhost:8009
    CBOS_GTG_BASE_URL=http://localhost:8009

Run it:
    uvicorn mock_cbos.app:app --port 8009 --reload

The real host split (CORE :8003 / GTG :8087) collapses onto one port here
because the path namespaces (/v1/api/* vs /api/edp/*) never collide.

Scenario knobs (env):
    MOCK_CBOS_PENDING_POLLS   FILEUPLOAD returns FALSE for the first N polls even
                              once the process is otherwise ready (default 1).
    MOCK_CBOS_HOLIDAYS        comma-separated YYYY-MM-DD dates treated as holidays
                              (Step 1 returns HOLIDAY instead of SKIP).

Business-failure scenario: any uploaded filename containing "fail" (case-
insensitive) makes that file's Step-7 register return Status=FAILED, mirroring a
real CBOS rejection.

Inspect/reset state (test helpers, not part of the CBOS contract):
    GET  /__mock/state    - full in-memory state
    POST /__mock/reset    - clear everything
"""

from __future__ import annotations

import os
from typing import Any, NotRequired, TypedDict

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from mock_cbos import data
from mock_cbos.state import STATE, Process

app = FastAPI(title="Mock CBOS v5", version="5.0.0")


def _pending_polls() -> int:
    try:
        return int(os.getenv("MOCK_CBOS_PENDING_POLLS", "1"))
    except ValueError:
        return 1


def _holidays() -> set[str]:
    raw = os.getenv("MOCK_CBOS_HOLIDAYS", "")
    return {d.strip() for d in raw.split(",") if d.strip()}


def _ok(**extra: Any) -> dict[str, Any]:
    return {"Status": "Success", **extra}


# ------------------------------------------------------------------------------
# Wire shapes. Request models are lenient on purpose: every field is optional
# with the same fallback the raw-dict handlers used (extra keys such as
# PASSWORD/UserID are accepted and ignored, pre-V5 payloads without TradeDate
# still work), so this only adds structure, never new rejections.
# ------------------------------------------------------------------------------

class _CBOSRequest(BaseModel):
    """Base for every CBOS request model: numbers arrive as JSON numbers or
    strings interchangeably in real traffic (PROCESSID: 17658 vs "17658"),
    so coerce rather than 422."""
    model_config = ConfigDict(coerce_numbers_to_str=True)


class TradeProcessRequest(_CBOSRequest):
    """POST getNewTradeProcess - Step 2 (and Step 10, same endpoint)."""
    GROUPNAME: str = ""
    LOGINID: str = ""
    TRADEDATE: str = ""
    PROCESSID: str = "0"


class DropdownRequest(_CBOSRequest):
    """POST getdropdown - Step 6, TAG=EXISTINGPROCESSID."""
    TAG: str = ""
    LOGINID: str = ""
    FILTER1: str = ""  # segment
    FILTER2: str = ""  # trade date, yyyy-mm-dd (V5)


class FileProcessStatusRequest(_CBOSRequest):
    """POST file_process_status - Steps 1/3/9 + downstream GTG checks."""
    Segment: str = ""
    TradeDate: str = ""  # V5: right after Segment (Shape A)
    ProcessName: str = ""


class RegisterFileRequest(_CBOSRequest):
    """POST SaveNewTradeProcessPromodalUploadFile - Step 7."""
    uploadfoldername: str = ""
    uploadid: str = ""
    paraM9: str = ""  # PROCESSID
    uploadfilename: str = ""


class MarkOptionalRequest(_CBOSRequest):
    """POST UpdateNewTradeProcessProcessDetailsIsMandatory - Step 8."""
    PROCESSID: str = ""
    STEPNO: int = 0
    ISOPTIONAL: str = "0"


class UploadSettingsRequest(_CBOSRequest):
    """POST GetNewTradeProcessPromodalUploadSettings - Step 4."""
    UPLOADID: str = ""


class ExpectedFilenameRequest(_CBOSRequest):
    """POST get_expected_filename - Step 39."""
    uploadid: str = ""


class TriggerRequest(_CBOSRequest):
    """Collateral/MTF/margin trigger endpoints - BUTTONNAME switches
    REFRESH (status peek) vs trigger."""
    BUTTONNAME: str = ""


class Table2Row(TypedDict):
    """One V5 Table2 row as the client reads it (STATUS + STATUSDESC are what
    UploadCandidate.needs_upload keys on)."""
    STEPNO: int
    NAME: str
    STATUS: str
    STATUSDESC: str | None
    UPLOADID: int
    ISOPTIONAL: bool
    CREATEDBY: str
    STARTDATETIME: NotRequired[str]


class Table1Row(TypedDict):
    PROCESSID: int
    ISRUNNABLE: bool
    ISAUTOUPLOAD: bool


# ==============================================================================
# CORE host  (/v1/api/*)  - real host http://10.167.202.164:8003
# ==============================================================================

def _table2_rows(proc: Process, with_desc: bool) -> list[Table2Row]:
    """Render a process's Table2 in the V5 row shape (STATUS + STATUSDESC +
    ISOPTIONAL + CREATEDBY). with_desc=False renders STATUSDESC as null,
    matching the doc's *creation* example where every fresh row carries
    STATUSDESC: null; re-fetches report the derived real value."""
    return [
        Table2Row(STEPNO=s.stepno, NAME=s.name, STATUS=s.status,
                  STATUSDESC=(s.status_desc if with_desc else None),
                  UPLOADID=s.uploadid, ISOPTIONAL=s.is_optional, CREATEDBY=proc.login_id)
        for s in proc.steps
    ]


@app.post("/v1/api/process/getNewTradeProcess")
async def get_new_trade_process(payload: TradeProcessRequest):
    """Step 2. PROCESSID=0 mints a new process. PROCESSID=<real> (V5)
    *re-fetches* that process - Table2 then reports each slot's real
    STATUS/STATUSDESC instead of resetting to PENDING, and Table1's
    ISAUTOUPLOAD flips to False (a real-CBOS quirk the client deliberately
    stopped gating on; ISRUNNABLE is the signal now). The Step-10 trigger
    (EDP_Billing's call, same endpoint) only takes effect once every mandatory
    upload slot is satisfied - an early re-fetch by the uploader must not
    start billing."""
    process_id = str(payload.PROCESSID or "0")

    if process_id == "0":
        proc = STATE.reserve_process(payload.GROUPNAME, payload.LOGINID, payload.TRADEDATE)
        return _ok(Result={
            "Table1": [Table1Row(PROCESSID=int(proc.process_id), ISRUNNABLE=True,
                                 ISAUTOUPLOAD=True)],
            "Table2": _table2_rows(proc, with_desc=False),
        })

    # Existing-PROCESSID path: re-fetch; trigger only when GTG-ready.
    proc = STATE.get_process(process_id)
    if proc is None:
        return JSONResponse(status_code=400,
                            content={"Status": "FAILED",
                                     "Message": f"PROCESSID {process_id} not found"})
    if proc.gtg_ready():
        STATE.trigger(process_id)
    started = "2026-07-19 10:00:00" if proc.triggered else ""
    return _ok(Result={
        "Table1": [Table1Row(PROCESSID=int(proc.process_id), ISRUNNABLE=True,
                             ISAUTOUPLOAD=False)],
        "Table2": [
            {**row, "STARTDATETIME": started}
            for row in _table2_rows(proc, with_desc=True)
        ],
    })


@app.post("/v1/api/process/GetNewTradeProcessPromodalUploadSettings")
async def get_upload_settings(payload: UploadSettingsRequest):
    """Step 4 - per-UPLOADID rule."""
    upload_id = payload.UPLOADID
    row = data.upload_setting(upload_id)
    return _ok(Result=[{"ID": int(upload_id) if upload_id.isdigit() else upload_id, **row}])


@app.post("/v1/api/process/SaveTradePromodalUploadChunkFile")
async def upload_chunk(
    CurrentChunk: str = Form(...),
    TotalChunks: str = Form(...),
    Guid: str = Form(...),
    FileName: str = Form(...),
    UPLOADID: str = Form(default=""),
    file: UploadFile | None = File(default=None),
):
    """Step 5 - accepts a chunk (or a whole single-chunk file) and files it
    under the GUID folder. The folder stays orphaned until Step 7 registers it.
    The file part is optional: the mock only tracks the handshake, so a Postman
    Runner can drive the flow with form fields alone (no file to attach)."""
    body = await file.read() if file is not None else b""
    folder = STATE.add_chunk(Guid, FileName, body, int(CurrentChunk), int(TotalChunks))
    return _ok(
        Status="ChunkUploaded",
        Guid=Guid,
        FileName=FileName,
        currentChunk=str(CurrentChunk),
        totalChunks=str(TotalChunks),
        fCount=str(len(folder.files)),
    )


@app.post("/v1/api/process/SaveNewTradeProcessPromodalUploadFile")
async def register_file(payload: RegisterFileRequest):
    """Step 7 - associates the GUID folder with a PROCESSID + UPLOADID. This is
    the step whose absence leaves files orphaned and FILEUPLOAD stuck on FALSE."""
    guid = payload.uploadfoldername
    upload_id = payload.uploadid
    process_id = payload.paraM9
    file_name = payload.uploadfilename

    if "fail" in file_name.lower():
        return JSONResponse(status_code=200, content={
            "Status": "FAILED",
            "Message": f"CBOS rejected '{file_name}' (business-failure scenario)",
        })

    ok, message = STATE.register_file(guid, upload_id, process_id)
    if not ok:
        return JSONResponse(status_code=200, content={"Status": "FAILED", "Message": message})
    return _ok(Result=message)


@app.post("/v1/api/process/UpdateNewTradeProcessProcessDetailsIsMandatory")
async def update_is_mandatory(payload: MarkOptionalRequest):
    """Step 8 - mark a subprocess step optional (ISOPTIONAL=0 in the doc means
    'not mandatory'). Without this, no-file mandatory steps keep FILEUPLOAD FALSE."""
    # Doc: ISOPTIONAL=0 -> "make this optional / not mandatory". We treat the call
    # as "this step is now optional" regardless of the exact flag value, matching
    # how the field is used to skip no-file steps.
    ok, message = STATE.mark_optional(payload.PROCESSID, payload.STEPNO, is_optional=True)
    status_code = 200 if ok else 400
    return JSONResponse(status_code=status_code, content=_ok(Result={"Table1": [{"MSG": message}]}) if ok
                        else {"Status": "FAILED", "Message": message})


@app.post("/v1/api/brokerage/getdropdown")
async def get_dropdown(payload: DropdownRequest):
    """Step 6 - EXISTINGPROCESSID lookup. V5 uses it *up front* too
    (find_existing_process_id): FILTER1 is the segment, FILTER2 the trade
    date (yyyy-mm-dd) - a hit means "reuse this PROCESSID", a miss means
    "mint a new one", so the date filter matters: yesterday's PID must not
    be returned for today's batch."""
    proc = STATE.process_for(payload.FILTER1, payload.FILTER2)
    if proc is None:
        return _ok(Result=[])
    return _ok(Result=[{"_KEY": int(proc.process_id),
                        "_DESC": f"{proc.process_id} - {payload.LOGINID} - {proc.trade_date}"}])


# --- Collateral / MTF / Margin trigger endpoints (Steps 17-36) -----------------
# Canned "process started" responses so the full pipeline can be exercised.
@app.post("/v1/api/process/GetCollateralValuation")
async def collateral_valuation(payload: TriggerRequest):
    if payload.BUTTONNAME.upper() == "REFRESH":
        return _ok(Result={"Table1": []})  # empty => not triggered yet
    return _ok(Result={"Table1": [{"MSG": "Process started successfully and will run in the background"}]})


@app.post("/v1/api/process/MTFTradeProcessCollateralAllocation")
async def mtf_collateral_allocation(payload: TriggerRequest):
    return _ok(Result={"Table1": [{"MSG": "Process started successfully and will run in the background"}]})


@app.post("/v1/api/process/MTFTradeProcessFundTransfer")
async def mtf_fund_transfer(payload: TriggerRequest):
    return _ok(Result={"Table1": [{"MSG": "Process started successfully and will run in the background"}]})


@app.post("/v1/api/process/MTFTradeProcess")
async def mtf_trade_process(payload: TriggerRequest):
    return _ok(Result=[{"MSG": "Process completed successfully"}])


@app.post("/v1/api/process/CombinedMarginProcess")
async def combined_margin(payload: TriggerRequest):
    if payload.BUTTONNAME.upper() == "REFRESH":
        return _ok(Result={"Table1": []})
    return _ok(Result={"Table1": [{"MSG": "Process started successfully and will run in the background"}]})


# ==============================================================================
# GTG host  (/api/edp/*)  - real host http://10.167.202.234:8087
# ==============================================================================

@app.post("/api/edp/file_process_status")
async def file_process_status(payload: FileProcessStatusRequest):
    """Shared GTG/status endpoint; behaviour switched by ProcessName. Covers
    Step 1 (holiday), Step 3 (CheckProcessIDExist), Step 9 (FILEUPLOAD), and the
    downstream GTG checks (BILLPOSTING, RECON, ...).

    V5: the payload now carries TradeDate (yyyy-mm-dd) right after Segment, so
    each check resolves the process for *that* segment/date rather than
    whichever was reserved last - and the Step-1 holiday check can answer from
    the payload alone, before any process exists (the real Step ordering)."""
    process_name = payload.ProcessName
    segment = payload.Segment
    trade_date = payload.TradeDate

    if process_name == "BeginFileUpload":
        # Step 1 - holiday check, against the payload's own TradeDate. Falls
        # back to the latest process's date only for pre-V5 payloads.
        if not trade_date:
            proc = STATE.latest_process(segment)
            trade_date = proc.trade_date if proc else ""
        msg = "HOLIDAY" if trade_date in _holidays() else "SKIP"
        return _ok(Data=[{"MSG": msg}])

    if process_name == "CheckProcessIDExist":
        proc = STATE.process_for(segment, trade_date)
        if proc is None:
            return _ok(Data=[{"MSG": "NO PROCESS ID GENERATED"}])
        return _ok(Data=[{"MSG": f"PROCESS ID ALREADY GENERATED : {proc.process_id}"}])

    if process_name == "FILEUPLOAD":
        # Step 9 - TRUE only once every mandatory upload step is satisfied AND
        # the pending-poll delay has elapsed.
        proc = STATE.process_for(segment, trade_date)
        if proc is None:
            return _ok(Data=[{"MSG": "FALSE"}])
        proc.fileupload_polls += 1
        if proc.fileupload_polls <= _pending_polls():
            return _ok(Data=[{"MSG": "FALSE"}])
        return _ok(Data=[{"MSG": "TRUE" if proc.gtg_ready() else "FALSE"}])

    # Downstream GTG checks - canned TRUE.
    return _ok(Data=[{"MSG": "TRUE"}])


@app.post("/api/edp/get_expected_filename")
async def get_expected_filename(payload: ExpectedFilenameRequest):
    """Step 39 - expected filename pattern for a segment/upload id."""
    upload_id = payload.uploadid
    return _ok(Data=[{"UploadID": upload_id, "ExpectedFileNamePattern1": data.expected_pattern(upload_id)}])


# ==============================================================================
# Health + test helpers
# ==============================================================================

@app.get("/health")
async def health():
    return {"status": "ok", "service": "mock-cbos-v5"}


@app.get("/__mock/state")
async def mock_state():
    return {
        "processes": {
            pid: {
                "segment": p.segment, "trade_date": p.trade_date, "triggered": p.triggered,
                "fileupload_polls": p.fileupload_polls, "gtg_ready": p.gtg_ready(),
                "unsatisfied_upload_steps": [
                    {"stepno": s.stepno, "uploadid": s.uploadid, "name": s.name}
                    for s in p.unsatisfied_upload_steps()
                ],
                "steps": [
                    {"stepno": s.stepno, "uploadid": s.uploadid, "status": s.status,
                     "has_file": s.has_file, "is_optional": s.is_optional}
                    for s in p.steps
                ],
            }
            for pid, p in STATE.processes.items()
        },
        "guids": {
            g: {
                # Per file: what the server would actually have on disk after
                # reassembling the chunks. sha256 is null until every chunk has
                # arrived - compare it against the source file to prove Step 5
                # transferred the bytes intact, not merely the right count.
                "files": {
                    name: {
                        "total_chunks": c.total_chunks,
                        "received_chunks": c.received,
                        "missing_chunks": c.missing,
                        "complete": c.complete,
                        "total_bytes": c.total_bytes,
                        "sha256": c.sha256(),
                    }
                    for name, c in f.files.items()
                },
                "registered": f.registered, "upload_id": f.upload_id,
                "process_id": f.process_id,
            }
            for g, f in STATE.guids.items()
        },
    }


@app.post("/__mock/reset")
async def mock_reset():
    STATE.reset()
    return {"status": "reset"}
