# CBOS handoff contract — Uploader ↔ EDP_Billing

Three repos share the CBOS trade-process API. They do **not** call each other —
all coordination is **through CBOS as the shared backend**. This documents the
boundary so the two sides don't collide.

| Repo | Owns |
|------|------|
| `mofsl_file_download_rpa_bot` | Download files off exchange portals → disk |
| **`EDPBilling_FIle_Upload`** (this repo) | Get those files **into** CBOS |
| `EDP_Billing` | Scheduler: **trigger** + downstream (bill posting, recon, margin, MTF, collateral) |

## Step ownership (per segment + date)

| Step | Call | Owner |
|------|------|-------|
| 2 | `getNewTradeProcess(PROCESSID=0)` — reserve PID + read Table2 | **Uploader** |
| 3 | `CheckProcessIDExist` | Uploader (sanity, non-fatal) |
| 4 | `GetNewTradeProcessPromodalUploadSettings` — per-slot rules | **Uploader** |
| 5 | `SaveTradePromodalUploadChunkFile` — upload bytes → GUID | **Uploader** |
| 7 | `SaveNewTradeProcessPromodalUploadFile` — register GUID→UPLOADID→PID | **Uploader** |
| 8 | `UpdateNewTradeProcessProcessDetailsIsMandatory` — mark empty slots optional | **Uploader** |
| 9 | `file_process_status(FILEUPLOAD)` — good-to-go | **EDP_Billing** (authoritative); uploader may read once as its own confirmation |
| 10 | `getNewTradeProcess(PROCESSID=real)` — trigger | **EDP_Billing** |
| 11–39 | bill posting / recon / contract notes / collateral / fund transfer / MTF / margin | **EDP_Billing** |

**The uploader's definition of done: make `FILEUPLOAD` go `TRUE`.** Nothing more.

## The two things that cross the boundary — both via CBOS

1. **PROCESSID** — the uploader reserves it (Step 2). `EDP_Billing` reads it back
   with `getdropdown(EXISTINGPROCESSID)` for that segment/date and triggers *that*
   PID. Never passed directly.
2. **`FILEUPLOAD` status flag** — flips `TRUE` once every expected slot is filled
   or marked optional. That flag *is* the "files are in" signal `EDP_Billing`
   waits on. There is no back-channel: if it doesn't flip within the segment's
   window, `EDP_Billing` times the segment out.

## Rules that must hold (or the handoff breaks)

1. **One reserver.** `getNewTradeProcess(PROCESSID=0)` mints a **new** PID every
   call. If both repos reserve, there are two PIDs for one segment/date — the
   uploader fills PID-A, `EDP_Billing` triggers PID-B (empty) → timeout. So the
   **uploader is the sole reserver**, and **`EDP_Billing` must reuse-or-wait
   only** (drop its "reserve if none exists" branch). ← *coordination item for the
   EDP_Billing team.*
2. **One PID per (segment, date).** The uploader must reserve exactly once per
   segment/date — **not** per exchange folder — so `getdropdown` is unambiguous.
   (Batch unit = `(segment, date)`; exchange is file metadata.)
3. **Timing.** The uploader must finish (FILEUPLOAD=TRUE) before the segment's
   trigger window closes on the `EDP_Billing` side.

## Known unknowns (verify against real CBOS)

- The real MCX `Table2` (which UPLOADIDs, legacy vs UDIFF) — reconstructed in the
  mock, not captured. Ground it from a real reservation response.
- `UpdateNewTradeProcessProcessDetailsIsMandatory` flag: doc uses `ISOPTIONAL="0"`
  to mean *optional* — unverified.
- The uploader is on API doc **v4**; `EDP_Billing`'s client is pinned to **v3**.
  Shared endpoints (`getNewTradeProcess`, `getdropdown`) have duplicated DTOs
  across the repos — candidate for a shared client lib to prevent drift.
