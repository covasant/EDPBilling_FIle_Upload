# Batch handoff contract — Downloader → Uploader (manifest + POST /batches)

Decided 2026-07-23 (wayfinder ticket 05). Replaces the implicit folder-scan
coupling ("set `EDPB_BASE_DIR` to the uploader's `FILE_ROOT_PATH` and hope")
with an explicit, atomic, checksummed handoff. Companion to
`CBOS_HANDOFF_CONTRACT.md` (which governs the Uploader ↔ EDP_Billing side).

| Decision | Choice |
|---|---|
| Batch unit | **One aggregated batch per (segment, trade date)** — one `manifest.json`, written when the bot's full-segment download run completes |
| Transport | Shared filesystem; `POST /batches` carries the **manifest path** |
| Folder layout | **Flat `{DD-MM-YYYY}/{SEGMENT}/`** — exchange is per-file metadata; the `{exchange}/` folder level and the `NA` shim are dead |
| Completeness authority | **Uploader only** (required-files gate, ticket 04). The bot reports `download_outcome` facts, never decides |
| Uploader trigger | **API-only**: `POST /batches`. Interim caller = bot callback after finalization; final caller = EDP_Billing engine (ticket 10). No folder-scan scheduler |

## The manifest

One per (segment, trade date): `{FILE_ROOT}/{DD-MM-YYYY}/{SEGMENT}/manifest.json`.
Schema: `edpb_core/manifest.schema.json` (packaged in the shared `edpb-core`
package under `EDP_Billing/packages/edpb-core` — THE canonical copy). Example:

```json
{
  "manifest_version": 1,
  "batch_id": "MCX-2026-07-20-a3f8c2d1",
  "segment": "MCX",
  "trade_date": "2026-07-20",
  "correlation_id": "req-7f3a1b9c",
  "producer": { "name": "mofsl_file_download_rpa_bot", "version": "1.4.0", "action": "all" },
  "created_at": "2026-07-20T03:12:45+05:30",
  "files": [
    { "name": "Trade_MCX_CO_0_CM_55930_20260720_F_0000.csv",
      "sha256": "9b4e…", "size_bytes": 184320, "exchange": "MCX", "kind": "trade" },
    { "name": "Position_MCXCCL_CO_0_CM_55930_20260720_F_0000.csv",
      "sha256": "77aa…", "size_bytes": 92160, "exchange": "MCX", "kind": "position" },
    { "name": "MCX_ProductMaster.csv",
      "sha256": "c1d2…", "size_bytes": 51200, "exchange": "MCX", "kind": "product_master" }
  ],
  "download_outcome": { "status": "success", "no_data": [], "failed": [] }
}
```

Rules:
- Dates are **ISO (`YYYY-MM-DD`) inside the manifest**; only folder names keep
  `DD-MM-YYYY`. `created_at` is ISO-8601 with offset (IST).
- `batch_id` = `{SEGMENT}-{trade_date}-{8 hex chars}`, fresh per finalization
  attempt. A re-run supersedes: new `manifest.json`, new `batch_id`; the audit
  trail keeps history per batch_id.
- `files[].name` is relative to the manifest's own directory (flat — no
  subpaths). `exchange` and `kind` are metadata for matching/diagnostics.
- `download_outcome` is the bot's factual report: `status` `success|partial`,
  `no_data`/`failed` list the actions or file kinds that produced nothing.
  A `partial` manifest is still finalized and POSTed — the uploader's
  required-files gate is the single authority on whether to proceed or park
  the batch INCOMPLETE.
- Single-action debug runs (e.g. BSE `vn` only) do **not** finalize a
  manifest; only the full-segment run does.

## Atomic finalization (bot side)

1. Download the files into `{date}/{SEGMENT}/` (the portals write final
   names directly — no `.part` staging; see the note below on why that is
   sufficient).
2. Compute sha256/size per file; write `manifest.json.tmp`; **fsync the
   bytes**; rename to `manifest.json`; **fsync the directory** — the
   manifest is written **last** and its durable rename is the commit point.
3. The engine submits the manifest (`POST /batches` from its UPLOADING
   state). The bot's own callback exists for standalone runs
   (`EDPB_UPLOADER_URL`, default off); `POST /batches/rescan` recovers
   anything missed.

Why no `.part` staging: the uploader only ever acts on files LISTED in a
manifest received after its commit point and verifies each sha256 **at
intake** — a torn or in-progress file fails verification (422, batch
rejected, files left in place) rather than being queued. A re-run
supersedes with a fresh manifest; the superseded batch terminates as
FAILED ("superseded") when its files are gone. Known window: intake-time
checksums do not protect a batch already mid-upload when the bot re-runs
over the same files — the engine-driven flow avoids this by issuing one
download per segment-day cycle; ops re-runs should wait for the in-flight
batch to reach a terminal status first.

## POST /batches (uploader side)

```
POST /batches            {"manifest_path": "/abs/path/.../manifest.json"}
  202 {"batch_id": "...", "status": "queued"}
  200 {"batch_id": "...", "status": "<current>"}     ← already-known batch_id (idempotent)
  400 manifest unreadable / schema-invalid           422 checksum mismatch (batch rejected, files left in place)

GET  /batches/{batch_id}
  200 {"batch_id", "status": "queued|uploading|confirmed|unconfirmed|incomplete|failed|rejected",
       "files": [{"name", "outcome", "cbos_upload_id", ...}]}   ← from the audit table

POST /batches/rescan     {}
  202 {"queued": ["<batch_id>", ...]}
  Walks FILE_ROOT for manifest.json files not yet known to the audit trail and
  queues them. THE manual ops path (replaces drop-a-file-in-folder + /run-now)
  and the catch-up path when the bot's callback couldn't reach the uploader.
```

- The APScheduler folder-scan trigger is **removed** (ticket 09). Nothing is
  uploaded without a manifest.
- Upload processing itself is unchanged: Steps 1–9, existing-PID reuse,
  slot-status idempotent skip, `uploaded/`/`uploadFailed/` moves — all keyed
  off the manifest's file list instead of a directory listing.
- `correlation_id` flows manifest → batch processing → audit rows (ticket 11).

## Completeness gate (decided 2026-07-23, wayfinder ticket 04)

The gate closes the "Step 8 marks every unfilled slot optional → FILEUPLOAD
flips TRUE on incomplete data" hole. Implementation: ticket 07.

- **Source of truth: CBOS Table2.** The required set for a batch is the
  reserved PID's file-expecting slots (non-zero UploadID) — never a second
  spec. `EDP_RequiredFiles.xlsx` / `EDPFILEUPLOADSETTING.xlsx` are
  documentation, not runtime inputs.
- **Optional-slot allowlist.** A small, code-reviewed YAML in this repo
  (`app/config/optional_slots.yaml`, changed via PR) lists the UploadIDs per
  segment that are *legitimately* absent some days (e.g. MCX Physical 320,
  BSE Auction 451). Step 8 may only auto-mark **allowlisted** unfilled slots
  optional.
- **Any other unfilled slot ⇒ the batch parks `INCOMPLETE`**: no Step 8, no
  FILEUPLOAD confirmation. Files that DID reach CBOS move to `uploaded/`
  with a `gate_parked` outcome (they are registered in CBOS; re-dropping
  would duplicate them — CBOS's per-slot STATUS readback idempotent-skips
  them on any re-run); the batch-level INCOMPLETE verdict lives on the
  batches row. A superseding manifest (re-download) is the normal fix.
- **Audited force-proceed**: `POST /batches/{batch_id}/proceed
  {"slots": [<uploadid>...], "reason": "..."}` — ops explicitly names the
  slots to mark optional; recorded in the audit trail; then the batch
  resumes Steps 8–9.
- **Alerting**: the uploader only *exposes* `INCOMPLETE` (via
  `GET /batches/{batch_id}`); EDP_Billing — which owns Graph email alerting
  and sees FILEUPLOAD staying FALSE — notifies ops. One alerting system.
- The initial allowlist contents must be **confirmed with MOFSL ops**
  (seeded from the xlsx sheets + real run logs, but the business call on
  "legitimately absent" is theirs).

## Who calls what, when

| Phase | Trigger chain |
|---|---|
| ✅ Current (ticket 10 landed 2026-07-23) | EDP_Billing engine (`DOWNLOADING` state) → bot `/edpb/{code}/download` → bot finalizes → engine (`UPLOADING` state) `POST /batches` → engine polls FILEUPLOAD + batch status (INCOMPLETE ⇒ segment FAILED + email) |
| Standalone/interim fallback | bot callback (`EDPB_UPLOADER_URL`, default off) or `POST /batches/rescan` |
