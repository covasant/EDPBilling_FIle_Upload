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

## Corporate Actions — the `FOPositionChange` lane (V6 Steps 34–35)

An **event-driven** lane, not a fourth branch of the daily pipeline. It runs only on
dates a corporate action occurs (three dates in seven weeks, for member 10412), and it
reuses this repo's upload lane unchanged.

`FOPositionChange` is a **pseudo-segment**: it goes in `getNewTradeProcess`'s
`GROUPNAME` field where a real segment code goes, reserves its own PROCESSID with its
own Table2, and is uploaded to through the identical Steps 4/5/7/8. It is a legal
manifest `segment` value (`edpb_core.segments.CORP_ACTION_SEGMENT`) so no special-casing
is needed at intake.

| Step | Call | Owner |
|------|------|-------|
| — | Fetch `<SYMBOL>_<member>_(EXISTING\|ADJUSTED)_POSITIONS.CSV` from NSE Extranet `FO/Reports` (member tree) | **cams-edp-file-download-rpa-bot-repo** — `POST /edpb/corpaction/nse/positions/download` |
| 34 | `file_process_status(Segment=DR, ProcessName=BILLPOSTING)` — the gate | **cams-edp-billing-automation-agent-repo** — `corpaction.check_fo_billposting` |
| 35 ph.1 | `getNewTradeProcess(GROUPNAME=FOPositionChange, PROCESSID=0)` — reserve | **cams-edp-billing-automation-agent-repo** — `corpaction.reserve_position_change` |
| 4/5/7/8 | match → chunk-upload → register → mark empty slots optional | **Uploader** (this repo), unchanged |
| 35 ph.2 | `getNewTradeProcess(GROUPNAME=FOPositionChange, PROCESSID=<real>)` — trigger | **cams-edp-billing-automation-agent-repo** — `corpaction.trigger_position_change` |

**Two files, always.** `EXISTING` is the position book before NSE applies the corporate
action ratio, `ADJUSTED` the same book after; CBOS needs both to compute the delta.
They are published as a pair, sub-second apart, on every observed date. A run that
fetches only one silently halves the input, which is why the bot reports `unpaired`.

**The sequencer** is `corpaction.run_position_change` in the engine. It drives the
lane as far as it can go in one cycle and returns, rather than blocking on the upload:
a run that stops at `AWAITING_UPLOAD` carries `process_id` + `batch_id`, and the caller
**must** re-enter with both. Re-entering without them reserves a SECOND process for the
same date, because Phase 1 mints a fresh PID on every call — so the reserved process
would hold the files while a different, empty one gets triggered. When the handle is
absent the sequencer asks CBOS for an existing PID first and refuses to reserve if that
lookup could not answer.

**Rules specific to this lane:**

1. **Phase 2 must not fire until the files are registered.** Both phases are the same
   endpoint with the same GROUPNAME, differing only in whether `PROCESSID` is `"0"`, so
   an early trigger is one argument away — and CBOS would restate the F&O position book
   against no input rather than refuse. `trigger_position_change` requires an explicit
   `files_registered` assertion and refuses `PROCESSID="0"` outright.
2. **The uploader skips its Step 1 holiday check for this segment.** `BeginFileUpload`
   takes a *Segment*, and `FOPositionChange` is a *GROUPNAME*. This lane's
   "should today run" gate is Step 34, checked upstream. See
   `upload_service.process_batch`.
3. **Timing.** NSE publishes at ~19:45 IST on every observed date. The V6 doc's
   "10 PM–11:59 PM" ops window is conservative, not wrong.

## Known unknowns (verify against real CBOS)

- **What does `getNewTradeProcess(GROUPNAME="FOPositionChange", PROCESSID="0")`
  return in Table2?** Nothing in this lane has been run against real CBOS. That
  response is the authoritative answer to how many upload slots the process
  expects — i.e. whether it wants both position CSVs or only one — and to what
  `GetNewTradeProcessPromodalUploadSettings` will declare for each slot's
  filename pattern and column count. The V6 doc names two files; the ops
  instruction named one. **One call in UAT settles it**, and it is the first
  thing to run when a corporate action date is available.
- **Do the segment-scoped status calls accept `FOPositionChange`?** Almost
  certainly not — CBOS answered `INVALID SEGMENT` for the post-trade
  pseudo-segments put through segment-scoped calls (the engine's
  `docs/CBOS-CLIENT-ASKS.md`, item (d)). `BeginFileUpload` is already skipped
  for this segment (rule 2 above). `CheckProcessIDExist` and the Step 9
  `FILEUPLOAD` poll are **not** skipped: both are non-fatal by construction, so
  an `INVALID SEGMENT` reply costs a log line, and leaving them in place means
  the answer gets *observed* rather than assumed. If UAT shows they answer
  usefully, keep them; if they answer `INVALID SEGMENT`, skip them the same way.
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
