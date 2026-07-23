# Domain glossary

The vocabulary this codebase uses, defined once. Module docstrings explain
mechanics; they should not re-define the terms below.

---

## The folder tree

**Trade date** — the business date a set of files belongs to. Appears as a
folder name in `settings.date_folder_format` (e.g. `17-07-2026`), and is
reformatted to `yyyy-mm-dd` on the way out to CBOS. Called `folder_date` in
code.

**Segment** — a market segment (`EQ`, `MCX`, `FO`, …). The second folder level.
Segment plus trade date is the unit CBOS's own workflow operates on, which
makes it the unit this service batches by.

**Exchange** — `BSE`, `NSE`, `NA`, … The third folder level. Per-file metadata,
**not** a partition key: a segment's exchange sub-folders all belong to the same
batch. Used for audit and to break matching ties.

```
{FILE_ROOT_PATH}/{trade date}/{segment}/{exchange}/{file}
```

**uploaded/ and uploadFailed/** — sibling folders a file is moved into once its
outcome is known. A file inside either is structurally invisible to discovery,
which is how the service avoids reprocessing without consulting the database.

---

## The unit of work

**Batch** — one segment on one trade date, across every exchange sub-folder.
Represented by `SegmentBatchTask`; its key is `{trade date}|{segment}`. One
batch reserves exactly one PROCESSID. Slicing a batch by exchange would reserve
two PROCESSIDs for the same segment and strand half the files.

**Discovery** — the scheduled filesystem scan that finds source files and turns
them into batches. Never calls CBOS, never reads the database.

---

## CBOS concepts

**PROCESSID** — the identifier CBOS issues when a batch is reserved (Step 2).
One per batch, shared by every file in it. EDP_Billing reads it back per
segment and trade date.

**UploadID** — a slot in CBOS's pipeline that expects one particular kind of
file (`BSE SCRIP`, `MCX ProductMaster`, …). Every file must be matched to the
right UploadID before it can be uploaded.

**Table2 slot** — one UploadID candidate in CBOS's Step 2 response, carrying a
`UPLOADID` and a `STEPNO`. A batch's Table2 lists every slot the segment's
pipeline expects that day.

**Upload rule** — the file-name pattern, extension and column count CBOS
declares for one UploadID (Step 4). What a file is matched against.

**Empty slot** — a non-zero Table2 slot that received no file today. Marked
optional (Step 8) so it doesn't hold FILEUPLOAD at FALSE.

**FILEUPLOAD** — CBOS's flag meaning "every expected file for this segment has
arrived". Making it go TRUE is where this service's job ends.

**GTG host / CORE host** — CBOS is split across two hosts. GTG serves the
status-check calls (`file_process_status`, `get_expected_filename`); CORE serves
the process and brokerage calls. See `settings.cbos_gtg_base_url` /
`cbos_core_base_url`.

---

## Outcomes

Every discovered file ends in exactly one of these:

| Outcome | Meaning | Lands in |
| --- | --- | --- |
| **Confirmed** | Uploaded, registered, FILEUPLOAD went TRUE | `uploaded/` |
| **Unconfirmed** | Uploaded and registered, but FILEUPLOAD not yet TRUE | `uploaded/` |
| **Idempotent skip** | Already uploaded for this batch and UploadID | `uploaded/` |
| **Rejected** | Matched no upload rule, or failed a local check | `uploadFailed/` |
| **Failed** | A CBOS call errored during upload or registration | `uploadFailed/` |

Unconfirmed deliberately lands in `uploaded/`: the file *is* in CBOS, so
re-dropping it would duplicate.

---

## Scope

**Upload lane** — the part of the CBOS pipeline this repo owns: reserve, match,
upload, register, mark empty slots optional. Steps 2 through 9.

**Handoff** — the point where this repo stops. The V6 **Insti Trade GTG**
(Step 10, CHECKINSTITRADE), the CBOS **trigger** (Step 11) and everything
downstream (bill posting, recon, margin, MTF, collateral) belong to the
**EDP_Billing scheduler**, which polls FILEUPLOAD, then CHECKINSTITRADE, and
triggers once both read TRUE. This service must never trigger. See
`docs/CBOS_HANDOFF_CONTRACT.md`.

**Audit log** — the `uploaded_files` table. Written to record what was
attempted and what CBOS said. Read for exactly one decision: the idempotency
check that stops an already-uploaded file being sent twice.
