# CBOS handoff contract — Uploader ↔ EDP_Billing

Three repos share the CBOS trade-process API. They do **not** call each other —
all coordination is **through CBOS as the shared backend**. This documents the
boundary so the two sides don't collide.

| Repo | Owns |
|------|------|
| `mofsl_file_download_rpa_bot` | Download files off exchange portals → disk |
| **`EDPBilling_FIle_Upload`** (this repo) | Get those files **into** CBOS |
| `EDP_Billing` | Scheduler: **trigger** + downstream (bill posting, recon, margin, MTF, collateral) |

## Step ownership (per segment + date) — V6 numbering

| Step | Call | Owner |
|------|------|-------|
| 2 | `getNewTradeProcess(PROCESSID=0)` — reserve PID + read Table2 | **Uploader** |
| 3 | `CheckProcessIDExist` | Uploader (sanity, non-fatal) |
| 4 | `GetNewTradeProcessPromodalUploadSettings` — per-slot rules | **Uploader** |
| 5 | `SaveTradePromodalUploadChunkFile` — upload bytes → GUID | **Uploader** |
| 7 | `SaveNewTradeProcessPromodalUploadFile` — register GUID→UPLOADID→PID | **Uploader** |
| 8 | `UpdateNewTradeProcessProcessDetailsIsMandatory` — mark empty slots optional | **Uploader** |
| 9 | `file_process_status(FILEUPLOAD)` — good-to-go | **EDP_Billing** (authoritative); uploader may read once as its own confirmation |
| 10 | `file_process_status(CHECKINSTITRADE)` — Insti Trade GTG (**new in V6**) | **EDP_Billing** — must be TRUE *after* FILEUPLOAD and *before* the trigger; CBOS does **not** enforce this server-side |
| 11 | `getNewTradeProcess(PROCESSID=real)` — trigger (was Step 10 pre-V6) | **EDP_Billing** |
| 12–40 | bill posting / recon / contract notes / collateral / fund transfer / MTF / margin | **EDP_Billing** |

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
   **uploader is the sole reserver**, and **`EDP_Billing` reads-or-waits only**.
   ✅ *Resolved 2026-07-23:* `EDP_Billing`'s "reserve if none exists" branch was
   removed (`RealSegmentStateMachine._resolve_process_id` on `feat/edpb-alignment`
   is read-only — `getdropdown(EXISTINGPROCESSID)` misses are a normal wait, and
   the agent never calls reserve-mode `getNewTradeProcess`); the uploader side
   reuses an existing PID before minting (`find_existing_process_id`,
   `feat/existing-pid` line).
2. **One PID per (segment, date).** The uploader must reserve exactly once per
   segment/date — **not** per exchange folder — so `getdropdown` is unambiguous.
   (Batch unit = `(segment, date)`; exchange is file metadata.)
3. **Timing.** The uploader must finish (FILEUPLOAD=TRUE) before the segment's
   trigger window closes on the `EDP_Billing` side.

## Known unknowns (verify against real CBOS)

- **Does `getNewTradeProcess(PROCESSID=<real>)` TRIGGER once all slots are
  satisfied, regardless of caller?** The engine's Step-11 trigger IS that
  call, so presumably yes — but the uploader also re-fetches with the real
  PID at every batch start (`find_existing_process_id` → `reserve_process`).
  If real CBOS triggers on any ready-state real-PID call, an uploader
  re-run after FILEUPLOAD=TRUE could fire billing before the engine does
  (surfaced by live E2E against the v5 mock, whose trigger-when-ready
  behaviour makes exactly this happen). **V6 raises the stakes**: such an
  accidental trigger would also bypass the new Step-10 Insti Trade gate
  entirely — CBOS doesn't enforce it, and the uploader never polls
  CHECKINSTITRADE. Verify in UAT; if it triggers, the uploader must skip
  its refetch once FILEUPLOAD is TRUE.
- **Does `CHECKINSTITRADE` (V6 Step 10) apply to all 10 segments, or only
  insti-relevant ones?** The V6 doc claims the same 40-step workflow for
  every segment (its example is MCX) and documents only FALSE/TRUE
  answers. The engine treats any non-TRUE as "wait" with the segment
  window as timeout backstop — if some segment's insti check never goes
  TRUE in UAT, that segment needs an exemption ruling from MOFSL ops.

- The real MCX `Table2` (which UPLOADIDs, legacy vs UDIFF) — reconstructed in the
  mock, not captured. Ground it from a real reservation response.
- `UpdateNewTradeProcessProcessDetailsIsMandatory` flag: doc uses `ISOPTIONAL="0"`
  to mean *optional* — unverified. The **Table2 readback** side is equally
  unverified: the mock answers Python booleans, but real CBOS sends numbers
  and strings interchangeably elsewhere, so the uploader parses ISOPTIONAL
  via a strict truthy allowlist (`_parse_isoptional` in `cbos_client.py`) —
  unknown values read as "not optional" so the completeness gate fails
  closed. Verify the real readback vocabulary (and whether it inherits the
  Step-8 `"0"`-means-optional inversion) in UAT.
- ~~The uploader is on API doc v4; `EDP_Billing`'s client is pinned to v3.~~
  ✅ *Resolved on `feat/edpb-alignment`:* both repos now target **V6**
  (V5's TradeDate everywhere + V6's Step-10 Insti Trade gate), with wire
  shapes shared via `edpb_core.cbos` payload builders — no duplicated DTOs.
