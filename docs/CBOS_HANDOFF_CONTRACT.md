# CBOS handoff contract — Uploader ↔ cams-edp-billing-automation-agent-repo

Three repos share the CBOS trade-process API. They do **not** call each other —
all coordination is **through CBOS as the shared backend**. This documents the
boundary so the two sides don't collide.

| Repo | Owns |
|------|------|
| `cams-edp-file-download-rpa-bot-repo` | Download files off exchange portals → disk |
| **`cams-edp-file-handling-agent-repo`** (this repo) | Get those files **into** CBOS |
| `cams-edp-billing-automation-agent-repo` | Scheduler: **trigger** + downstream (bill posting, recon, margin, MTF, collateral) |

## Step ownership (per segment + date) — V6 numbering

| Step | Call | Owner |
|------|------|-------|
| 2 | `getNewTradeProcess(PROCESSID=0)` — reserve PID + read Table2 | **cams-edp-billing-automation-agent-repo** (moved from the uploader — see "One reserver" below); uploader consumes the process_id/Table1/Table2 handed to it on `POST /batches` |
| 3 | `CheckProcessIDExist` | Uploader (sanity, non-fatal) |
| 4 | `GetNewTradeProcessPromodalUploadSettings` — per-slot rules | **Uploader** |
| 5 | `SaveTradePromodalUploadChunkFile` — upload bytes → GUID | **Uploader** |
| 7 | `SaveNewTradeProcessPromodalUploadFile` — register GUID→UPLOADID→PID | **Uploader** |
| 8 | `UpdateNewTradeProcessProcessDetailsIsMandatory` — mark empty slots optional | **Uploader** |
| 9 | `file_process_status(FILEUPLOAD)` — good-to-go | **cams-edp-billing-automation-agent-repo** (authoritative); uploader may read once as its own confirmation |
| 10 | `file_process_status(CHECKINSTITRADE)` — Insti Trade GTG (**new in V6**) | **cams-edp-billing-automation-agent-repo** — post-trigger; must be TRUE *before billposting* (see trigger-first note); CBOS does **not** enforce this server-side |
| 11 | `getNewTradeProcess(PROCESSID=real)` — trigger (was Step 10 pre-V6) | **cams-edp-billing-automation-agent-repo** |
| 12–40 | bill posting / recon / contract notes / collateral / fund transfer / MTF / margin | **cams-edp-billing-automation-agent-repo** |

**The uploader's definition of done (trigger-first, 2026-07-24): get the files
into CBOS — upload + register every slot — then report the batch `UNCONFIRMED`.**
It polls `FILEUPLOAD` exactly **once** (in case CBOS already flipped it `TRUE` →
`CONFIRMED`), but it does **not** wait for `TRUE`: under trigger-first that flag
does not flip until `cams-edp-billing-automation-agent-repo` fires the trigger, which happens *after* the
upload. `cams-edp-billing-automation-agent-repo` is the authoritative post-trigger `FILEUPLOAD` poller. (The
old ~60s wait-for-`TRUE` loop was pure dead time — it only delayed the batch
reaching `UNCONFIRMED` and, with it, the engine's trigger — and was removed.)

> **Trigger-first execution order (SME ruling 2026-07-24).** The numbers above
> are CBOS's own step labels; `cams-edp-billing-automation-agent-repo` no longer *executes* them in that
> order. Field observation: `FILEUPLOAD` good-to-go does **not** go TRUE until
> the process is triggered — so it cannot be a pre-trigger gate. The engine now:
> **(a)** fires the trigger (Step 11) first — after a pre-trigger completeness
> guard that refuses to trigger a batch the uploader parked INCOMPLETE — then
> **(b)** polls `FILEUPLOAD` (Step 9) good-to-go, then **(c)** polls
> `CHECKINSTITRADE` (Step 10), which now gates the move into **billposting**, not
> the trigger. This **reverses** the V6 doc's "insti before trigger" ordering, on
> the strength of the SME confirmation; completeness is still enforced before the
> trigger by the guard, so trigger-first does not mean billing on incomplete data.
> Verified live end-to-end 2026-07-24 (MCX straight-through; EQ INCOMPLETE →
> guard fails pre-trigger with zero trigger calls → ops proceed+retry → COMPLETED).

## The two things that cross the boundary

1. **PROCESSID.** `cams-edp-billing-automation-agent-repo` now reserves it (Step 2), at its `INIT` state,
   right after the holiday check passes — the engine is the **sole reserver**
   (see "One reserver" below; this reverses the pre-2026-08-13 rule where the
   uploader reserved). It's passed **directly** now: the engine hands
   `process_id` + Table1 + Table2 to the uploader on `POST /batches`, which
   consumes them instead of calling `getNewTradeProcess` itself. The engine's
   `getdropdown(EXISTINGPROCESSID)` read-back (`_resolve_process_id`) remains
   only as a defensive fallback for a segment that somehow reaches `TRIGGERED`
   without a process_id already resolved.
2. **`FILEUPLOAD` status flag** — flips `TRUE` once every expected slot is filled
   or marked optional. That flag *is* the "files are in" signal `cams-edp-billing-automation-agent-repo`
   waits on. There is no back-channel: if it doesn't flip within the segment's
   window, `cams-edp-billing-automation-agent-repo` times the segment out.

## Rules that must hold (or the handoff breaks)

1. **One reserver.** `getNewTradeProcess(PROCESSID=0)` mints a **new** PID every
   call. If both repos reserve, there are two PIDs for one segment/date — one
   side fills PID-A, the other triggers/expects PID-B (empty) → timeout or a
   mismatch. This is exactly what happened on **2026-07-21** when both sides
   reserved.
   ✅ *2026-07-23 – 2026-08-13:* the uploader was the sole reserver;
   `cams-edp-billing-automation-agent-repo`'s `_resolve_process_id` was read-only
   (`getdropdown(EXISTINGPROCESSID)`).
   🔁 **Changed 2026-08-13:** ownership moved to `cams-edp-billing-automation-agent-repo`.
   Its `INIT` state is now the sole reserver: it reserves a fresh PID (or
   reconciles an existing one already on the row — never both, never a
   second mint for a segment/date that already has one) and hands
   process_id/Table1/Table2 to the uploader on `POST /batches`. The uploader
   no longer calls `getNewTradeProcess` for a batch that carries a supplied
   process_id (`upload_service.py`'s `task.process_id` branch); it only falls
   back to self-reserving (`reserve_process`/`find_existing_process_id`) for a
   caller that hasn't adopted this field. The engine's own
   `_resolve_process_id`/`getdropdown` read-back is now a defensive fallback,
   not the primary path — kept in case a segment somehow reaches `TRIGGERED`
   without a process_id already resolved. Whichever side reserves, the
   invariant that matters is unchanged: **exactly one caller mints a PID for
   a given segment/date, ever.**
2. **One PID per (segment, date).** Whoever reserves must do so exactly once
   per segment/date — **not** per exchange folder — so nothing downstream is
   ambiguous. (Batch unit = `(segment, date)`; exchange is file metadata.)
3. **Timing.** The uploader must finish (FILEUPLOAD=TRUE) before the segment's
   trigger window closes on the `cams-edp-billing-automation-agent-repo` side.

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
- ~~The uploader is on API doc v4; `cams-edp-billing-automation-agent-repo`'s client is pinned to v3.~~
  ✅ *Resolved on `feat/edpb-alignment`:* both repos now target **V6**
  (V5's TradeDate everywhere + V6's Step-10 Insti Trade gate), with wire
  shapes shared via `edpb_core.cbos` payload builders — no duplicated DTOs.
