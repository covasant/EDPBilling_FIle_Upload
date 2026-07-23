# Mock CBOS v5 server

A standalone FastAPI app that mimics the real CBOS trade-process API
(`docs/EDP_Trade_Process_API_Documentation_V5.docx`) faithfully enough that the
uploader can run end-to-end against it — and, crucially, **fail the same way the
real server fails**, so the switch to real CBOS holds no surprises.

## Run

```bash
uvicorn mock_cbos.app:app --port 8009 --reload
```

Point the uploader at it (both CBOS hosts collapse onto one port — the path
namespaces `/v1/api/*` and `/api/edp/*` don't collide):

```dotenv
CBOS_MODE=REAL
CBOS_CORE_BASE_URL=http://localhost:8009
CBOS_GTG_BASE_URL=http://localhost:8009
CBOS_LOGIN_ID=CV0001
CBOS_PASSWORD=anything
```

Postman collections in [`docs/postman/`](../docs/postman/):
- **`EDP_CBOS_MCX_FullBatch`** — hit **Run** and the whole MCX handshake plays in
  one pass (reserve → 3 files × Steps 4/5/7 → Step 8 → poll to TRUE → trigger).
  No files to attach; `process_id`/`guid` are captured automatically.
- **`EDP_Trade_Process_API_v5`** — per-endpoint reference for every step.

## What it models (and why)

It reproduces the two invariants that actually bite:

1. **Orphaned GUID folder.** A chunk upload (Step 5) lands under a GUID, but that
   folder is *orphaned* until Step 7 (`SaveNewTradeProcessPromodalUploadFile`)
   associates it with a PROCESSID + UPLOADID. Register with an unknown GUID →
   `Status: FAILED`.
2. **FILEUPLOAD good-to-go gate.** The Step 9 `FILEUPLOAD` check returns `TRUE`
   only once **every mandatory upload step** (non-zero `UPLOADID`) either has a
   registered file **or** has been marked optional via Step 8
   (`UpdateNewTradeProcessProcessDetailsIsMandatory`). A no-file mandatory step
   keeps it `FALSE` — exactly the `MSG=FALSE` seen in testing.

`GET /__mock/state` shows *why* a process isn't good-to-go (`unsatisfied_upload_steps`).

3. **Chunk reassembly.** Step 5 keeps each chunk's *bytes*, indexed by
   `CurrentChunk`, and reassembles them in order. `GET /__mock/state` reports
   per file: `total_chunks`, `received_chunks`, `missing_chunks`, `complete`,
   `total_bytes` and `sha256` (null until every chunk has arrived). Comparing
   that digest against the source file is the only check that proves the
   uploader's chunking transferred the bytes intact — counting bytes passes
   even when chunks arrive duplicated, out of order, or truncated.

   `tests/test_chunk_wire.py` drives this over a real socket with the real
   `CBOSClient`; it starts this server on an ephemeral port itself, so there is
   nothing to run by hand. Note what it does *not* prove: this server and the
   client were written from the same doc by the same author, so they can agree
   with each other and both be wrong about real CBOS.

## Endpoint map (V5 step → route)

| Step | Route | Host |
|---|---|---|
| 1 Holiday | `POST /api/edp/file_process_status` `ProcessName=BeginFileUpload` | GTG |
| 2 Reserve (PROCESSID=0) | `POST /v1/api/process/getNewTradeProcess` | CORE |
| 3 CheckProcessIDExist | `POST /api/edp/file_process_status` `ProcessName=CheckProcessIDExist` | GTG |
| 4 Upload settings | `POST /v1/api/process/GetNewTradeProcessPromodalUploadSettings` | CORE |
| 5 Chunk upload | `POST /v1/api/process/SaveTradePromodalUploadChunkFile` (multipart) | CORE |
| 6 getdropdown | `POST /v1/api/brokerage/getdropdown` | CORE |
| 7 Register file | `POST /v1/api/process/SaveNewTradeProcessPromodalUploadFile` | CORE |
| 8 Mark optional | `POST /v1/api/process/UpdateNewTradeProcessProcessDetailsIsMandatory` | CORE |
| 9 FILEUPLOAD status | `POST /api/edp/file_process_status` `ProcessName=FILEUPLOAD` | GTG |
| 10 Trigger (PROCESSID=real) | `POST /v1/api/process/getNewTradeProcess` | CORE |
| 17-36 Collateral/MTF/Margin | `POST /v1/api/process/{GetCollateralValuation,MTFTradeProcess,...}` | CORE |
| 39 Expected filename | `POST /api/edp/get_expected_filename` | GTG |

## Scenario matrix

| Scenario | How to trigger | Result |
|---|---|---|
| Happy path | upload+register every mandatory file, mark no-file steps optional, poll | FILEUPLOAD → `TRUE` |
| Orphaned folder | register (Step 7) with a GUID never uploaded | `Status: FAILED` |
| Missing Step 8 | leave a no-file mandatory step un-marked | FILEUPLOAD stays `FALSE` |
| Business failure | any uploaded filename containing `fail` | Step 7 → `Status: FAILED` |
| Pending polls | env `MOCK_CBOS_PENDING_POLLS=N` | first N `FILEUPLOAD` polls → `FALSE`, then real state |
| Holiday | env `MOCK_CBOS_HOLIDAYS=2026-07-14` | Step 1 → `MSG: HOLIDAY` (else `SKIP`) |

## Segments

`MCX` and `EQ` carry a realistic `Table2` (MCX includes the Physical step 320
with no daily file, to exercise the Step-8 path); any other `GROUPNAME` falls
back to a generic single-upload pipeline. See `data.py`.

## Test helpers (not part of the CBOS contract)

- `GET /health` — liveness.
- `GET /__mock/state` — full in-memory state (processes, steps, GUID folders).
- `POST /__mock/reset` — clear all state between test runs.
