# File Upload Handler

A FastAPI service that watches a date/segment/exchange folder tree for trade
files and uploads each one to CBOS's real trade-upload API, recording the
outcome in PostgreSQL for audit purposes. Runs fully automatically: a
scheduler scans on an interval, discovered files are queued, and a single
background worker uploads them one at a time.

The only decision point in the whole pipeline is **the CBOS upload result**:
every discovered file is attempted through CBOS's full Steps 2->7 sequence
with no pre-upload validation of any kind. CBOS succeeding or failing is the
sole thing that decides whether a file ends up in `uploaded/` or
`uploadFailed/`.

```
Server Start
  -> Initialize DB
  -> Start Queue Worker
  -> Start Scheduler
  -> Begin File Processing (automatic, no manual steps)
```

---

## Architecture

```
FastAPI (app.main)
  |
  |-- Scheduler (APScheduler, runs every POLL_INTERVAL_MINUTES)
  |     -> discovers files, enqueues them
  |     -> never uploads, never touches the database
  |
  |-- Queue Worker (background thread, started at app startup)
  |     -> consumes one queued file at a time
  |     -> calls upload_service.process_task() for the actual work
  |
  |-- upload_service (orchestration - the only decision point is CBOS's result)
  |     -> file_service   (filesystem: discover / move files)
  |     -> cbos_client    (network: the real CBOS Steps 2/3/4/6/7)
  |     -> repository     (database: audit log only, never read for decisions)
  |
  |-- PostgreSQL (uploaded_files table - audit trail)
```

### Project layout

```
app/
├── main.py                    FastAPI app + lifespan (DB, worker, scheduler startup)
├── api/v1/
│   ├── router.py               aggregates all v1 routes
│   └── endpoints/
│       ├── upload.py           POST /upload (manual upload edge case)
│       └── system.py           GET /health, POST /run-now, GET /queue-status
├── core/
│   ├── config.py               Settings (pydantic-settings, reads .env)
│   ├── database.py             SQLAlchemy engine/session, init_db()
│   ├── logging.py               structured logging setup
│   └── queue.py                 in-memory Queue + FileTask + in-flight guard
├── models/
│   └── uploaded_file.py         UploadedFile ORM model (audit log)
├── schemas/
│   └── upload.py                UploadResponse (Pydantic)
├── repositories/
│   └── uploaded_file_repository.py   audit-log writer for uploaded_files - write-only, never queried for decisions
├── services/
│   ├── file_service.py          filesystem-only: discover, move files
│   └── upload_service.py        orchestrates discovery -> queue -> CBOS Steps 2-7 -> audit write
├── clients/
│   └── cbos_client.py           the real CBOS trade-upload API (Steps 2/3/4/6/7)
├── workers/
│   └── upload_worker.py         background loop consuming the queue
└── scheduler/
    └── scheduler.py             APScheduler wiring, triggers scans only

scripts/                        local dev tooling - never imported by app/
```

---

## Folder structure the service watches

```
{FILE_ROOT_PATH}/{date}/{segment}/{exchange}/{file}
```

Example:

```
edpb/
├── 06-07-2026/
│   ├── EQ/
│   │   └── BSE/
│   │       ├── trade_001.csv
│   │       ├── trade_002.csv
│   │       ├── uploaded/          <- successfully uploaded files land here
│   │       │   └── trade_003.csv
│   │       └── uploadFailed/      <- failed uploads land here
│   │           └── trade_004.csv
│   ├── FO/
│   ├── CUR/
│   ├── MCX/
│   └── SLBM/
```

- `date` uses `DATE_FOLDER_FORMAT` (default `%d-%m-%Y`).
- Every scan checks **T (today) through T-`SCAN_DAYS_BACK`** (default: today and yesterday).
- `uploaded/` and `uploadFailed/` are created automatically and are always
  excluded from discovery - the scheduler never re-scans its own output
  folders, and a file that has already been moved into either one can never
  be rediscovered. This filesystem exclusion is the *entire* dedup
  mechanism; nothing in the database is consulted to decide whether to
  process a file.
- On success (CBOS Step 7 confirms completion), the file is moved to
  `uploaded/`. On any failure anywhere in the CBOS sequence, it's moved to
  `uploadFailed/` and the audit row's `retry_count` is incremented.

---

## Setup

```powershell
cd file_uploader
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create the Postgres database referenced by `DATABASE_URL` (tables are
created automatically by `init_db()` on startup - no migrations needed):

```sql
CREATE DATABASE edp_cbos;
```

### Configuration (`.env`)

| Variable | Meaning | Example |
|---|---|---|
| `FILE_ROOT_PATH` | Root folder the scheduler scans | `C:/Users/you/mofsl/edpb` |
| `DATE_FOLDER_FORMAT` | strftime format for date folders | `%d-%m-%Y` |
| `POLL_INTERVAL_MINUTES` | How often the scheduler scans | `5` |
| `SCAN_DAYS_BACK` | How many days back to also scan besides today | `1` |
| `LOG_LEVEL` | Log verbosity (`INFO` for milestones, `DEBUG` for full per-step trace) | `INFO` |
| `CBOS_BASE_URL` | Shared host for all 5 CBOS trade-upload endpoints | `https://cbos-host/api` |
| `CBOS_LOGIN_ID` | LOGINID sent on every CBOS call | `CV0001` |
| `CBOS_TIMEOUT_SECONDS` | HTTP timeout per CBOS call | `30` |
| `CBOS_CHUNK_SIZE_BYTES` | Chunk size for Step 4 file upload | `1048576` |
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
venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload
```

On startup you should see, in this order:

```
main INFO Startup: step 1/3 - initializing database
main INFO Startup: step 2/3 - starting queue worker thread
upload_worker INFO Queue worker started
main INFO Startup: step 3/3 - starting scheduler
scheduler INFO Scheduler started, running every N minute(s)
main INFO Startup complete - ready to process files
Application startup complete.
```

Nothing else is required - the scheduler fires automatically on its interval
and the worker processes whatever gets queued.

### API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/run-now` | Trigger an immediate discovery scan (don't wait for the interval) |
| GET | `/queue-status` | `{"queue_size": N, "unfinished_tasks": N}` - queue depth / true in-flight count |
| POST | `/upload` | Manual upload edge case (multipart `file` + `segment` + `exchange` form fields) |
| GET | `/docs` | Swagger UI |

`POST /upload` saves the file into the standard `{date}/{segment}/{exchange}/`
folder and marks it `pending` - it never talks to CBOS directly. The next
scheduler scan discovers and processes it exactly like any other file.

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
