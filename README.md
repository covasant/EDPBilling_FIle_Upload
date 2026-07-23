# File Upload Handler

A FastAPI service that uploads manifest-declared batches of trade files to
CBOS's real trade-upload API, recording per-file outcomes in PostgreSQL and
per-batch status in a `batches` table. Work enters ONLY through the batches
API (`POST /batches` with a `manifest.json` written by the download bot -
see `docs/BATCH_HANDOFF_CONTRACT.md`); there is no filesystem scanner. A
single background worker uploads one batch at a time.

Two decision points govern a batch:
1. **CBOS's own upload results** (Steps 1->9) decide each file's fate
   (`uploaded/` vs `uploadFailed/`), and
2. **the completeness gate** decides the batch's: Step 8 may only auto-mark
   `app/config/optional_slots.yaml`-allowlisted slots optional - any other
   unfilled mandatory Table2 slot parks the batch `incomplete` (FILEUPLOAD
   stays FALSE) until a superseding manifest or an audited
   `POST /batches/{id}/proceed`.

```
Server Start
  -> Initialize DB
  -> Start Queue Worker
  -> Await POST /batches (bot callback, engine, or ops)
```

---

## Architecture

```
FastAPI (app.main)
  |
  |-- Batches API (POST /batches | GET /batches/{id} | POST /batches/rescan
  |                 | POST /batches/{id}/proceed)
  |     -> manifest_service: schema-validate (docs/manifest.schema.json),
  |        sha256-verify, record Batch row, enqueue
  |
  |-- Queue Worker (background thread, started at app startup)
  |     -> consumes one queued batch at a time
  |     -> calls upload_service.process_batch() for the actual work
  |
  |-- upload_service (orchestration: Steps 1->9 + completeness gate)
  |     -> optional_slots (the gate's allowlist, app/config/optional_slots.yaml)
  |     -> file_service   (filesystem: move files)
  |     -> cbos_client    (network: the real CBOS Steps 1-9)
  |     -> repositories   (uploaded_files audit + batches status)
  |
  |-- PostgreSQL (uploaded_files audit trail + batches status)
```

### Project layout

```
app/
├── main.py                    FastAPI app + lifespan (DB, worker startup)
├── api/v1/
│   ├── router.py               aggregates all v1 routes
│   └── endpoints/
│       ├── batches.py          POST /batches, GET /batches/{id}, rescan, proceed
│       └── system.py           GET /health, GET /queue-status
├── core/
│   ├── config.py               Settings (pydantic-settings, reads .env)
│   ├── database.py             SQLAlchemy engine/session, init_db()
│   ├── logging.py               structured logging setup + correlation-id filter
│   ├── correlation.py           per-run id carried on every log line
│   └── queue.py                 in-memory Queue + FileTask + in-flight guard
├── models/
│   └── uploaded_file.py         UploadedFile ORM model (audit log)
├── schemas/
│   └── upload.py                UploadResponse (Pydantic)
├── repositories/
│   └── uploaded_file_repository.py   audit-log writer for uploaded_files - write-only, never queried for decisions
├── services/
│   ├── manifest_service.py      manifest intake: schema-validate, sha256-verify, task-build
│   ├── optional_slots.py        completeness-gate allowlist loader
│   ├── file_service.py          filesystem-only: move files
│   └── upload_service.py        orchestrates queue -> CBOS Steps 1-9 + gate -> audit write
├── config/
│   └── optional_slots.yaml      the gate's allowlist (code-reviewed; see BATCH_HANDOFF_CONTRACT.md)
├── clients/
│   └── cbos_client.py           the real CBOS trade-upload API (Steps 1-9)
└── workers/
    └── upload_worker.py         background loop consuming the queue

scripts/                        local dev tooling - never imported by app/
```

---

## Folder structure (flat - see docs/BATCH_HANDOFF_CONTRACT.md)

```
{FILE_ROOT_PATH}/{date}/{SEGMENT}/{files + manifest.json}
```

Example:

```
edpb/
├── 20-07-2026/
│   ├── MCX/
│   │   ├── manifest.json          <- the batch declaration (bot-written, atomic)
│   │   ├── Trade_MCX_....csv
│   │   ├── Position_MCXCCL_....csv
│   │   ├── uploaded/              <- successfully uploaded files land here
│   │   └── uploadFailed/          <- failed uploads land here
│   ├── EQ/
│   └── CUR/
```

- `date` uses `DATE_FOLDER_FORMAT` (default `%d-%m-%Y`); dates INSIDE the
  manifest are ISO (`YYYY-MM-DD`).
- There is NO exchange folder level - exchange is per-file metadata in the
  manifest.
- Only files LISTED in a manifest are ever touched, and only when that
  manifest is submitted (`POST /batches`) or rescanned. Files already moved
  into `uploaded/`/`uploadFailed/` are gone from the manifest's directory -
  that structural fact plus CBOS's own per-slot STATUS readback is the dedup
  mechanism.
- On success the file moves to `uploaded/`; on failure to `uploadFailed/`
  with `retry_count` incremented. When the completeness gate parks a batch,
  files that DID reach CBOS still move to `uploaded/` (they are registered
  there) and the batches row records `incomplete` + the missing slots.

---

## Setup

The repo ships `pyproject.toml` + `uv.lock`, so uv is the primary path — it
reads `.python-version` and builds the venv on 3.12 for you:

```powershell
uv sync
```

`requirements.txt` is kept in sync with `pyproject.toml`'s runtime pins, so a
plain venv works too if uv isn't available:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Windows / VDI

The VDI images are not uniform — expect one of these:

- **`python` or `pip` "is not recognized".** Python isn't on `PATH`. Use the
  Windows launcher instead: `py --version`, `py -m pip install uv`. If `python`
  opens the Microsoft Store, that's the App Execution Alias stub — disable it
  under Settings -> Apps -> Advanced app settings -> App execution aliases.
- **`uv` not recognized right after `pip install uv`.** The wheel puts `uv.exe`
  in a `Scripts\` dir that isn't on `PATH`. Call it as a module: `py -m uv sync`.
- **`invalid peer certificate: UnknownIssuer` during `uv sync`.** The corporate
  proxy re-signs HTTPS with an internal root CA that uv's bundled cert store
  doesn't know. Set `$env:UV_SYSTEM_CERTS = 1` to use the Windows store. (pip on
  these images is usually already configured for the proxy.)
- **Build-from-source errors.** Some images ship CPython 3.14+, which has no
  wheels for parts of this stack, and the VDI has no C toolchain. Both commands
  above pin 3.12 for exactly this reason — don't drop the pin.
- **"Failed to hardlink files; falling back to full copy."** Harmless. uv's cache
  and the checkout sit on different drives (`C:` vs `D:`), so hardlinks aren't
  possible. Set `$env:UV_LINK_MODE = "copy"` to silence it.

Create the Postgres database referenced by `DATABASE_URL` (tables are
created automatically by `init_db()` on startup - no migrations needed):

```sql
CREATE DATABASE edp_cbos;
```

### Configuration (`.env`)

| Variable | Meaning | Example |
|---|---|---|
| `FILE_ROOT_PATH` | Root folder batches live under | `C:/Users/you/mofsl/edpb` |
| `DATE_FOLDER_FORMAT` | strftime format for date folders | `%d-%m-%Y` |
| `MANIFEST_SCHEMA_PATH` | JSON Schema manifests are validated against | `docs/manifest.schema.json` |
| `OPTIONAL_SLOTS_PATH` | The completeness gate's allowlist | `app/config/optional_slots.yaml` |
| `LOG_LEVEL` | Log verbosity (`INFO` for milestones, `DEBUG` for full per-step trace) | `INFO` |
| `CBOS_BASE_URL` | Shared host for all 5 CBOS trade-upload endpoints | `https://cbos-host/api` |
| `CBOS_LOGIN_ID` | LOGINID sent on every CBOS call | `CV0001` |
| `CBOS_TIMEOUT_SECONDS` | HTTP timeout per CBOS call | `30` |
| `CHUNK_SIZE_KB` | Chunk size for Step 4 file upload, in KB | `51200` (50 MB) |
| `CBOS_CHUNK_RETRY_ATTEMPTS` | Max retries for a single failed chunk before aborting the upload | `3` |
| `CBOS_POLL_INTERVAL_SECONDS` | Delay between Step 7 polls | `2` |
| `CBOS_POLL_MAX_ATTEMPTS` | Max Step 7 polls before treating it as a failed/timed-out upload | `30` |
| `DATABASE_URL` | Postgres connection string | `postgresql://user:pass@host:5432/db` |

All values are read once via `app/core/config.py`'s `Settings` (pydantic-settings),
which loads `.env` automatically and matches env var names case-insensitively.

**Note:** if your Postgres password contains special characters (`@`, `#`, etc.),
URL-encode it in `DATABASE_URL` (`@` -> `%40`).

---

## Running the app

```powershell
uv run uvicorn app.main:app --reload
```

Or, on the plain-venv path:

```powershell
.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload
```

On startup you should see, in this order:

```
main INFO Startup: step 1/2 - initializing database
main INFO Startup: step 2/2 - starting queue worker thread
upload_worker INFO Queue worker started
main INFO Startup complete - awaiting batches (POST /batches)
Application startup complete.
```

Work arrives via POST /batches - the download bot calls it after finalizing
a manifest; POST /batches/rescan catches up on any manifest the callback
missed.

### API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/batches` | Submit a manifest (`{"manifest_path": ...}`) - 202 queued, 200 already-known, 400 schema-invalid, 422 checksum mismatch |
| GET | `/batches/{batch_id}` | Batch status (`queued/uploading/confirmed/unconfirmed/incomplete/failed/rejected`) + per-file outcomes |
| POST | `/batches/rescan` | Queue every on-disk manifest not yet known (manual ops path / callback catch-up) |
| POST | `/batches/{batch_id}/proceed` | Audited force-proceed for an `incomplete` batch (`{"slots": [...], "reason": "..."}`) |
| GET | `/queue-status` | `{"queue_size": N, "unfinished_tasks": N}` - queue depth / true in-flight count |
| GET | `/docs` | Swagger UI |

---

## The CBOS upload sequence

For every discovered file, `upload_service.process_task()` drives
`cbos_client.py` through CBOS's real API, in order:

| Step | API | Purpose |
|---|---|---|
| 2 | `getNewTradeProcess` | Obtain a `PROCESSID` and the `Table2` list of candidate `UPLOADID`s |
| 3 | `GetNewTradeProcessPromodalUploadSettings` | Validate the file's name/extension against each candidate `UPLOADID` until one accepts it |
| 4 | `SaveTradePromodalUploadChunkFile` | Upload the file in chunks under a freshly generated GUID |
| 6 | `SaveNewTradeProcessPromodalUploadFile` | Register the uploaded chunks as one file |
| 7 | `file_process_status` | Poll (`CBOS_POLL_INTERVAL_SECONDS` apart, up to `CBOS_POLL_MAX_ATTEMPTS` times) until CBOS confirms `MSG=TRUE` |

**There is no Step 1 or Step 5** - those belong to other CBOS flows this
service doesn't use.

A failure at *any* step - process-id creation, upload-settings lookup, chunk
upload, file registration, or status polling (including a timeout) - is
caught by the single `except Exception` in `process_task()` and routed to
`handle_upload_failure()`. Only Step 7 confirming completion routes to
`handle_upload_success()`. These two functions are the only places in the
codebase that move a file on disk or write its final audit status.

### Reading the log

Every CBOS step logs its request and its response, in **both** `CBOS_MODE=MOCK`
and `CBOS_MODE=REAL` - the narration lives on `BaseCBOSClient._call`, so the two
modes read identically and a mock run is a faithful rehearsal of a real one.

Every line emitted while a batch is in flight carries a correlation id and the
batch key, so one batch's whole conversation can be pulled out of a day's log:

```
2026-07-21 08:47:10 cbos_client INFO [3fb59f94 17-07-2026|MCX] Step 2 getNewTradeProcess REQUEST  {'segment': 'MCX', 'trade_date': '17-07-2026'}
2026-07-21 08:47:10 cbos_client INFO [3fb59f94 17-07-2026|MCX] Step 2 getNewTradeProcess RESPONSE {'Status': 'Success', 'Result': {...}}
2026-07-21 08:47:10 cbos_client INFO [3fb59f94 17-07-2026|MCX] Step 2 reserved ProcessID=17658 segment=MCX: 6 Table2 slot(s), 4 expecting a file ['127', '534', '535', '320']
```

```bash
grep 3fb59f94 app.log          # one batch, start to finish
```

Lines outside a batch (scheduler ticks, startup) show `[-]`.

| Level | What you get |
|---|---|
| `INFO` | One REQUEST + one RESPONSE line per step; per-file and per-batch summaries; the Step 9 gate line listing which UploadIDs were filled and which were marked optional. Response bodies are truncated at 600 chars, and say so. |
| `DEBUG` | Adds the literal wire traffic (full URL, HTTP status, untruncated body), one line per upload chunk, and one per FILEUPLOAD poll. |

A failed CBOS call always logs at `ERROR`, even for the steps whose failure the
caller deliberately swallows (3, 6, 8) - a silently-skipped Step 8 is what would
let CBOS proceed to billing without a file, so it must never be invisible at
`INFO`.

---

## Database

Table `uploaded_files` - **pure audit log**. Nothing in the application
queries this table to decide whether to skip, retry, or reprocess a file;
every processing attempt gets its own fresh row.

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `file_name` | String | |
| `file_path` | String, unique | Current location - updated when the file is moved |
| `folder_date` | String | The date folder it was discovered under |
| `segment` | String | e.g. `EQ`, `FO`, `CUR`, `MCX`, `SLBM` |
| `exchange` | String | e.g. `BSE`, `NSE`, `MCX` |
| `status` | String | `pending` \| `uploaded` \| `failed` |
| `cbos_response` | String | Final outcome - Step 7's response on success, or the error that failed the sequence |
| `process_id` | String | `PROCESSID` from Step 2 |
| `cbos_upload_id` | String | `UPLOADID` selected in Step 3 |
| `guid` | String | GUID used for Step 4 chunking + Step 6 registration |
| `request_log` | Text (JSON) | Every step attempted, with its request/response, for full audit traceability |
| `retry_count` | Integer | Incremented on each failed attempt |
| `uploaded_at` | DateTime | Set on success |
| `created_at` | DateTime | Row creation time |

---

## Troubleshooting

- **Files not being discovered** - confirm they sit directly under
  `{FILE_ROOT_PATH}/{date}/{segment}/{exchange}/`, not inside `uploaded/` or
  `uploadFailed/`, and that `{date}` matches `DATE_FOLDER_FORMAT` for one of
  the days in the scan window (today through `SCAN_DAYS_BACK`).
- **Everything is going to `uploadFailed/`** - check the `cbos_response`
  column (or `request_log` for the full step-by-step trace) on the relevant
  row; it holds the exact CBOS error. A connection timeout to `CBOS_BASE_URL`
  is the most common cause in a new environment - confirm that host/port is
  reachable first.
- **A file is stuck reprocessing over and over without ever moving** - this
  should no longer happen, since any exception during the CBOS sequence
  (including a corrupt/unreadable file) is caught and routed to
  `uploadFailed/`. If you see it, that's a bug in `process_task()`'s
  exception handling - file an issue rather than working around it.
- **Schema changes not taking effect** - `init_db()` only calls
  `create_all()`, which does not `ALTER TABLE` existing tables; new columns
  are patched in individually by `init_db()`'s migration loop in
  `app/core/database.py`. Add new columns there when the model changes.
