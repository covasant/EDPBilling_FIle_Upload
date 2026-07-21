"""Mock CBOS v4 server.

A standalone FastAPI app that mimics the real CBOS trade-process API
(EDP_Trade_Process_API_Documentation_v4.docx) closely enough that this repo's
CBOSClient can run end-to-end against it with zero code changes - point both
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

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

from mock_cbos import data
from mock_cbos.state import STATE

app = FastAPI(title="Mock CBOS v4", version="4.0.0")


def _pending_polls() -> int:
    try:
        return int(os.getenv("MOCK_CBOS_PENDING_POLLS", "1"))
    except ValueError:
        return 1


def _holidays() -> set[str]:
    raw = os.getenv("MOCK_CBOS_HOLIDAYS", "")
    return {d.strip() for d in raw.split(",") if d.strip()}


def _ok(**extra) -> dict:
    return {"Status": "Success", **extra}


# ==============================================================================
# CORE host  (/v1/api/*)  - real host http://10.167.202.164:8003
# ==============================================================================

@app.post("/v1/api/process/getNewTradeProcess")
async def get_new_trade_process(payload: dict):
    """Step 2 (PROCESSID=0) reserves a process; Step 10 (PROCESSID=<real>)
    triggers execution. Same endpoint, behaviour switched by PROCESSID."""
    segment = payload.get("GROUPNAME", "")
    login_id = payload.get("LOGINID", "")
    trade_date = payload.get("TRADEDATE", "")
    process_id = str(payload.get("PROCESSID", "0"))

    if process_id == "0":
        proc = STATE.reserve_process(segment, login_id, trade_date)
        return _ok(Result={
            "Table1": [{"PROCESSID": int(proc.process_id), "ISRUNNABLE": True, "ISAUTOUPLOAD": True}],
            "Table2": [
                {"STEPNO": s.stepno, "NAME": s.name, "STATUS": s.status, "UPLOADID": s.uploadid}
                for s in proc.steps
            ],
        })

    # Trigger path (Step 10).
    proc = STATE.trigger(process_id)
    if proc is None:
        return JSONResponse(status_code=400, content={"Status": "FAILED", "Message": f"PROCESSID {process_id} not found"})
    return _ok(Result={
        "Table1": [{"PROCESSID": int(proc.process_id), "ISRUNNABLE": True}],
        "Table2": [
            {"STEPNO": s.stepno, "NAME": s.name, "STATUS": s.status,
             "UPLOADID": s.uploadid, "STARTDATETIME": "2026-07-19 10:00:00" if proc.triggered else ""}
            for s in proc.steps
        ],
    })


@app.post("/v1/api/process/GetNewTradeProcessPromodalUploadSettings")
async def get_upload_settings(payload: dict):
    """Step 4 - per-UPLOADID rule."""
    upload_id = str(payload.get("UPLOADID", ""))
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
async def register_file(payload: dict):
    """Step 7 - associates the GUID folder with a PROCESSID + UPLOADID. This is
    the step whose absence leaves files orphaned and FILEUPLOAD stuck on FALSE."""
    guid = payload.get("uploadfoldername", "")
    upload_id = str(payload.get("uploadid", ""))
    process_id = str(payload.get("paraM9", ""))
    file_name = payload.get("uploadfilename", "")

    if "fail" in str(file_name).lower():
        return JSONResponse(status_code=200, content={
            "Status": "FAILED",
            "Message": f"CBOS rejected '{file_name}' (business-failure scenario)",
        })

    ok, message = STATE.register_file(guid, upload_id, process_id)
    if not ok:
        return JSONResponse(status_code=200, content={"Status": "FAILED", "Message": message})
    return _ok(Result=message)


@app.post("/v1/api/process/UpdateNewTradeProcessProcessDetailsIsMandatory")
async def update_is_mandatory(payload: dict):
    """Step 8 - mark a subprocess step optional (ISOPTIONAL=0 in the doc means
    'not mandatory'). Without this, no-file mandatory steps keep FILEUPLOAD FALSE."""
    process_id = str(payload.get("PROCESSID", ""))
    stepno = payload.get("STEPNO", 0)
    is_optional = str(payload.get("ISOPTIONAL", "0")) in ("0", "true", "True", "1")
    # Doc: ISOPTIONAL=0 -> "make this optional / not mandatory". We treat the call
    # as "this step is now optional" regardless of the exact flag value, matching
    # how the field is used to skip no-file steps.
    ok, message = STATE.mark_optional(process_id, stepno, is_optional=True)
    status_code = 200 if ok else 400
    return JSONResponse(status_code=status_code, content=_ok(Result={"Table1": [{"MSG": message}]}) if ok
                        else {"Status": "FAILED", "Message": message})


@app.post("/v1/api/brokerage/getdropdown")
async def get_dropdown(payload: dict):
    """Step 6 - EXISTINGPROCESSID lookup (confirmation, off the critical path)."""
    segment = payload.get("FILTER1", "")
    login_id = payload.get("LOGINID", "")
    proc = STATE.latest_process(segment)
    if proc is None:
        return _ok(Result=[])
    return _ok(Result=[{"_KEY": int(proc.process_id), "_DESC": f"{proc.process_id} - {login_id} - {proc.trade_date}"}])


# --- Collateral / MTF / Margin trigger endpoints (Steps 17-36) -----------------
# Canned "process started" responses so the full pipeline can be exercised.
@app.post("/v1/api/process/GetCollateralValuation")
async def collateral_valuation(payload: dict):
    if str(payload.get("BUTTONNAME", "")).upper() == "REFRESH":
        return _ok(Result={"Table1": []})  # empty => not triggered yet
    return _ok(Result={"Table1": [{"MSG": "Process started successfully and will run in the background"}]})


@app.post("/v1/api/process/MTFTradeProcessCollateralAllocation")
async def mtf_collateral_allocation(payload: dict):
    return _ok(Result={"Table1": [{"MSG": "Process started successfully and will run in the background"}]})


@app.post("/v1/api/process/MTFTradeProcessFundTransfer")
async def mtf_fund_transfer(payload: dict):
    return _ok(Result={"Table1": [{"MSG": "Process started successfully and will run in the background"}]})


@app.post("/v1/api/process/MTFTradeProcess")
async def mtf_trade_process(payload: dict):
    return _ok(Result=[{"MSG": "Process completed successfully"}])


@app.post("/v1/api/process/CombinedMarginProcess")
async def combined_margin(payload: dict):
    if str(payload.get("BUTTONNAME", "")).upper() == "REFRESH":
        return _ok(Result={"Table1": []})
    return _ok(Result={"Table1": [{"MSG": "Process started successfully and will run in the background"}]})


# ==============================================================================
# GTG host  (/api/edp/*)  - real host http://10.167.202.234:8087
# ==============================================================================

@app.post("/api/edp/file_process_status")
async def file_process_status(payload: dict):
    """Shared GTG/status endpoint; behaviour switched by ProcessName. Covers
    Step 1 (holiday), Step 3 (CheckProcessIDExist), Step 9 (FILEUPLOAD), and the
    downstream GTG checks (BILLPOSTING, RECON, ...)."""
    process_name = str(payload.get("ProcessName", ""))
    segment = payload.get("Segment", "")

    if process_name == "BeginFileUpload":
        # Step 1 - holiday check.
        proc = STATE.latest_process(segment)
        trade_date = proc.trade_date if proc else ""
        msg = "HOLIDAY" if trade_date in _holidays() else "SKIP"
        return _ok(Data=[{"MSG": msg}])

    if process_name == "CheckProcessIDExist":
        proc = STATE.latest_process(segment)
        if proc is None:
            return _ok(Data=[{"MSG": "NO PROCESS ID GENERATED"}])
        return _ok(Data=[{"MSG": f"PROCESS ID ALREADY GENERATED : {proc.process_id}"}])

    if process_name == "FILEUPLOAD":
        # Step 9 - TRUE only once every mandatory upload step is satisfied AND
        # the pending-poll delay has elapsed.
        proc = STATE.latest_process(segment)
        if proc is None:
            return _ok(Data=[{"MSG": "FALSE"}])
        proc.fileupload_polls += 1
        if proc.fileupload_polls <= _pending_polls():
            return _ok(Data=[{"MSG": "FALSE"}])
        return _ok(Data=[{"MSG": "TRUE" if proc.gtg_ready() else "FALSE"}])

    # Downstream GTG checks - canned TRUE.
    return _ok(Data=[{"MSG": "TRUE"}])


@app.post("/api/edp/get_expected_filename")
async def get_expected_filename(payload: dict):
    """Step 39 - expected filename pattern for a segment/upload id."""
    upload_id = str(payload.get("uploadid", ""))
    return _ok(Data=[{"UploadID": upload_id, "ExpectedFileNamePattern1": data.expected_pattern(upload_id)}])


# ==============================================================================
# Health + test helpers
# ==============================================================================

@app.get("/health")
async def health():
    return {"status": "ok", "service": "mock-cbos-v4"}


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
