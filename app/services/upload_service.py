"""Coordinates CBOS upload, database updates, and file movement for
manifest-declared batches.

One CBOS "batch" is one segment + one trade date (see app/core/queue.py's
SegmentBatchTask and docs/BATCH_HANDOFF_CONTRACT.md). One PROCESSID is
reserved per segment/date; exchange is per-file metadata, not a partition
key. That's required by the documented workflow itself: Holiday Check
(Step 1), Process ID creation (Step 2), and the trigger (Step 8) all happen
exactly ONCE per segment/date - never once per file. Each manifest-listed
file is independently matched to the correct UploadID
(app/services/upload_matching.py) using the settings CBOS returns for every
Table2 candidate (Step 4).

Work enters ONLY through the batches API (app/api/v1/endpoints/batches.py) -
there is no filesystem scanner. The worker calls process_batch() per queue
item. Dedup stays filesystem-structural (a file already moved into uploaded/
or uploadFailed/ is gone from the manifest's directory) plus the in-memory
in-flight guard in app/core/queue.py. The uploaded_files table is written
for audit; the batches table carries per-batch status (the one table the
API reads back).

The completeness gate (docs/BATCH_HANDOFF_CONTRACT.md, "Completeness gate"):
Step 8 may only auto-mark optional_slots.yaml-allowlisted slots optional.
Any other unfilled file-expecting Table2 slot parks the batch INCOMPLETE -
no Step 8/9, FILEUPLOAD stays FALSE - until a superseding manifest fills it
or ops force-proceed via POST /batches/{id}/proceed (proceed_batch below)."""

import json
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from edpb_core.batch_api import BatchStatus

from app.clients import cbos_client
from app.clients.cbos_client import CBOSUploadError
from app.core import correlation
from app.core.config import settings
from app.core.database import get_sessionmaker
from app.core.queue import SegmentBatchTask
from app.repositories.batch_repository import BatchRepository
from app.repositories.uploaded_file_repository import UploadedFileRepository
from app.services import file_service, optional_slots, upload_matching, upload_outcome
from app.services.upload_matching import FileRejected
from app.services.upload_outcome import Destination, FileOutcome, Outcome

logger = logging.getLogger("upload_service")


# --------------------------------------------------------------------------
# Carrying a decision out. upload_outcome decides which folder a file belongs
# in and what its audit row should say; apply_outcome is the only thing that
# acts on that. Every code path in process_batch ends here for a given file.
# --------------------------------------------------------------------------

_OUTCOME_LOG_LEVEL = {
    Outcome.CONFIRMED: logging.INFO,
    Outcome.IDEMPOTENT_SKIP: logging.INFO,
    Outcome.UNCONFIRMED: logging.WARNING,
    Outcome.REJECTED: logging.WARNING,
    Outcome.FAILED: logging.ERROR,
    Outcome.GATE_PARKED: logging.WARNING,
}


def _set_batch_status(task: SegmentBatchTask, status: BatchStatus, detail: dict | None = None) -> None:
    """Record the batch-level verdict on the batches row (its own short
    session/transaction - per-file audit commits stay independent). No-op for
    tasks without a batch_id (unit tests driving process_batch directly)."""
    if not task.batch_id:
        return
    session = get_sessionmaker()()
    try:
        repo = BatchRepository(session)
        batch = repo.find_by_batch_id(task.batch_id)
        if batch is None:
            logger.warning("Batch %s: no batches row for %s - status %s not recorded",
                           task.key, task.batch_id, status)
            return
        repo.set_status(batch, status, detail)
    finally:
        session.close()


def _warn_if_process_id_differs(gtg_message: str, process_id: str, task: SegmentBatchTask) -> None:
    """Compare the PROCESSID in Step 3's message against the one we reserved.

    CBOS phrases it as "PROCESS ID ALREADY GENERATED : 17741", so the number is
    pulled out rather than matched whole. If no number is found the check is
    skipped silently - an unrecognised phrasing is not evidence of a mismatch.
    """
    found = re.search(r"(\d+)", gtg_message or "")
    if not found:
        return

    gtg_process_id = found.group(1)
    if gtg_process_id == str(process_id):
        return

    logger.error(
        "Batch %s: PROCESSID MISMATCH - we reserved %s but CheckProcessIDExist reports %s for segment %s. "
        "Steps 3 and 9 carry no trade date or PROCESSID, so CBOS resolves the segment itself: FILEUPLOAD "
        "will describe %s, not the process these files are being uploaded into. Uploading anyway; expect "
        "Step 9 not to confirm.",
        task.key, process_id, gtg_process_id, task.segment, gtg_process_id,
    )


def apply_outcome(repo: UploadedFileRepository, record, file_path: Path, outcome: FileOutcome,
                  request_log: list | None = None) -> Path:
    """Move the file into the folder its outcome calls for, and record what
    happened. Pass request_log=None when no CBOS call was made for this file
    (a local rejection), so the column stays empty rather than storing '[]'."""
    move = (file_service.move_to_uploaded if outcome.destination is Destination.UPLOADED
            else file_service.move_to_failed)
    dest_path = move(file_path)

    # Take the destination path off any earlier row that still claims it before
    # writing it here - see UploadedFileRepository.claim_file_path.
    repo.claim_file_path(record, dest_path)

    fields = {
        "status": outcome.status,
        "cbos_response": outcome.cbos_response,
    }
    if outcome.validation_error is not None:
        fields["validation_error"] = outcome.validation_error
    if outcome.counts_as_retry:
        fields["retry_count"] = (record.retry_count or 0) + 1
    if outcome.stamp_uploaded_at:
        fields["uploaded_at"] = datetime.utcnow()
    if request_log is not None:
        fields["request_log"] = json.dumps(request_log, default=str)

    repo.update(record, **fields)
    repo.commit()

    logger.log(
        _OUTCOME_LOG_LEVEL[outcome.outcome],
        "%s: file=%s upload_id=%s destination=%s response=%s",
        outcome.outcome, file_path.name, record.cbos_upload_id, dest_path, outcome.cbos_response,
    )
    return dest_path


def _fail_all_files(repo: UploadedFileRepository, files: list, task: SegmentBatchTask, error: Exception) -> None:
    """Batch setup (reserve / fetch-rules) failed before any upload - route every
    file to uploadFailed/ so the batch fails cleanly instead of being rediscovered
    and retried forever."""
    for file_path, exchange in files:
        record = repo.create_audit_record(
            file_path, task.folder_date, task.segment, exchange,
            correlation_id=task.correlation_id, batch_id=task.batch_id,
        )
        apply_outcome(repo, record, file_path, upload_outcome.failed(error),
                      [{"step": "batch_setup_error", "error": str(error)}])


# --------------------------------------------------------------------------
# Worker: process one queued segment/date batch. Steps 2/3/4/8/9 run once for
# the whole batch; Steps 5/6/7 run once per matched file.
# --------------------------------------------------------------------------


def process_batch(task: SegmentBatchTask) -> None:
    """Attempt the CBOS lane for one segment/date batch - the full upload
    lane (Steps 1->9) for mode="upload", or the audited force-proceed lane
    (Step 8 on ops-named slots + Step 9) for mode="proceed". Called by the
    worker loop for one queue item at a time."""
    # Every log line emitted below - including the ones from cbos_client, which
    # knows nothing about batches - carries this run's id, so one run's whole
    # CBOS conversation can be grepped out of a day's log. The batch key alone
    # won't do: it recurs on every retry.
    with correlation.batch_context(task.key, task.correlation_id):
        if task.mode == "proceed":
            _proceed_batch(task)
        else:
            _process_batch(task)


def _process_batch(task: SegmentBatchTask) -> None:
    files = [(Path(p), exch) for p, exch in task.files if Path(p).exists()]
    if task.files and not files:
        # The manifest LISTED files but every one is gone from the source
        # dir - in practice a SUPERSEDED batch: a newer manifest's run
        # already uploaded and moved them. Terminal, not silent: leaving
        # this row 'queued' made re-POST/rescan recovery re-enqueue it into
        # the same no-op forever.
        logger.warning("Batch %s: no listed file still exists on disk - marking FAILED "
                       "(superseded or already processed)", task.key)
        _set_batch_status(task, BatchStatus.FAILED, {
            "reason": "no listed file exists on disk - superseded by a newer batch "
                      "or already processed; submit the current manifest instead",
        })
        return
    # A DECLARED-EMPTY manifest (the bot's all-no_data partial run) falls
    # through deliberately: the completeness gate is the single authority,
    # and it can only park the batch INCOMPLETE if the batch runs the lane.

    trade_date = task.folder_date
    client = cbos_client.get_cbos_client()
    logger.info("Processing batch %s: %d file(s)", task.key, len(files))
    _set_batch_status(task, BatchStatus.UPLOADING)

    # Step 1: is CBOS accepting uploads for this segment today? Runs before
    # anything is reserved, because Step 2 mints a new PROCESSID on every
    # attempt - starting a batch CBOS has already ruled out would leave one
    # behind for a day that should have produced none.
    #
    # The files are left exactly where they are: a holiday says nothing about
    # them, and the next scan re-checks. No audit rows either - nothing has been
    # attempted, so there is nothing to record about these files yet.
    # Gating is opt-in (CBOS_HOLIDAY_CHECK_ENFORCED). Until a real
    # BeginFileUpload reply has been seen, the check only reports: "any message
    # except SKIP means holiday" comes from a single line of the API doc, and
    # acting on it would be a new way for the uploader to refuse to upload -
    # silently, and looking just like a day with no files.
    try:
        if not client.may_begin_upload(task.segment, trade_date):
            if settings.cbos_holiday_check_enforced:
                logger.warning("Batch %s: CBOS reports today is not a processing day for segment %s - "
                               "leaving files in place for the next submission/rescan", task.key, task.segment)
                _set_batch_status(task, BatchStatus.QUEUED,
                                  {"deferred": "holiday check - not a processing day"})
                return
            logger.warning("Batch %s: Step 1 did not answer %s for segment %s. Not a processing day, IF the "
                           "doc's rule holds - but the holiday check is observe-only "
                           "(CBOS_HOLIDAY_CHECK_ENFORCED=false), so the batch continues. Confirm this "
                           "message with the CBOS team before enforcing it.",
                           task.key, cbos_client.BEGIN_UPLOAD_PROCEED, task.segment)
    except CBOSUploadError as exc:
        # Unreachable/erroring GTG host is not proof of a holiday. Uploading on a
        # holiday is recoverable; refusing to upload on a working day because a
        # status endpoint blipped silently stalls the whole day's billing.
        logger.warning("Batch %s: BeginFileUpload check failed (%s) - proceeding anyway; "
                       "an unanswered holiday check is not a holiday", task.key, exc)

    session = get_sessionmaker()()
    try:
        repo = UploadedFileRepository(session)

        # Steps 2 + 4: reserve the PROCESSID (shared by every file in the batch)
        # and fetch each UploadID's matching rule, up front. Bounded retry so a
        # transient CBOS blip doesn't hot-loop the batch forever - after
        # cbos_max_retries attempts the files are routed to uploadFailed.
        # Reuse this segment/date's PROCESSID if CBOS already has one (Step 6,
        # getdropdown(EXISTINGPROCESSID)) instead of minting a new one on every
        # rescan - see BaseCBOSClient.find_existing_process_id.
        existing_process_id = client.find_existing_process_id(task.segment, trade_date) or "0"
        if existing_process_id != "0":
            logger.info("Batch %s: reusing existing PROCESSID=%s", task.key, existing_process_id)

        reservation = rules = None
        for attempt in range(1, settings.cbos_max_retries + 1):
            try:
                reservation = client.reserve_process(task.segment, trade_date, existing_process_id)
                rules = upload_matching.fetch_upload_rules(reservation.candidates, client)
                break
            except CBOSUploadError as exc:
                logger.warning("Batch %s: setup attempt %d/%d failed: %s",
                               task.key, attempt, settings.cbos_max_retries, exc)
                if attempt < settings.cbos_max_retries:
                    time.sleep(settings.cbos_retry_delay_seconds)
                else:
                    logger.error("Batch %s: setup exhausted %d attempt(s) - routing files to uploadFailed",
                                 task.key, settings.cbos_max_retries)
                    _fail_all_files(repo, files, task, exc)
                    _set_batch_status(task, BatchStatus.FAILED,
                                      {"reason": f"CBOS setup exhausted retries: {exc}"})
                    return
        process_id = reservation.process_id

        # ISRUNNABLE (Table1, Step 2) - CBOS's own signal for whether this
        # PROCESSID can currently take files, replacing the ISAUTOUPLOAD flag
        # this used to watch. ISAUTOUPLOAD flips to False the moment an
        # EXISTING PROCESSID is re-fetched (see find_existing_process_id
        # above) even though the process is perfectly usable, so gating on it
        # would have refused every reused process. Files are left in place
        # for the next scan, same as the holiday check above - nothing has
        # been attempted, so nothing needs recording.
        if not reservation.is_runnable:
            logger.warning("Batch %s: PROCESSID=%s is not runnable (Table1.ISRUNNABLE=False) - "
                           "leaving files in place for the next submission/rescan", task.key, process_id)
            _set_batch_status(task, BatchStatus.QUEUED,
                              {"deferred": f"PROCESSID={process_id} not runnable (ISRUNNABLE=False)"})
            return

        # Step 3: confirm the PROCESSID we just reserved is the one CBOS's
        # good-to-go side is tracking for this segment.
        #
        # Not a gate - a mismatch does not stop the uploads. The files still
        # belong in CBOS, and we do not know CBOS's rule for resolving a segment
        # to a PROCESSID (Steps 3 and 9 carry only Segment/ProcessName/UserID -
        # no trade date, no PROCESSID), so refusing to upload on a mismatch would
        # act on a guess and could block every batch.
        #
        # But it is logged loudly, because a mismatch means Step 9 is reporting
        # on a DIFFERENT process than the one we are filling, and FILEUPLOAD will
        # never go TRUE for our files however perfectly they upload. On
        # 2026-07-21 this call answered 17741 while we filled 17747, and because
        # its reply was discarded, that took a day to find.
        try:
            gtg_message = client.check_process_exists(task.segment, trade_date)
            _warn_if_process_id_differs(gtg_message, process_id, task)
        except CBOSUploadError as exc:
            logger.warning("Batch %s: CheckProcessIDExist failed (non-fatal): %s", task.key, exc)

        if not rules:
            logger.error("Batch %s: no usable UploadID rules resolved from Table2 - every file will be rejected", task.key)

        uploaded_candidates: list[tuple] = []  # (record, dest_file_path, request_log)
        filled_upload_ids: set[str] = set()    # UploadIDs that actually received a file (Steps 5+7)
        candidates_by_upload_id = {c.upload_id: c for c in reservation.candidates}
        # Step 5 done, Step 7 not yet - (record, file_path, rule, guid, request_log).
        # Every file's chunks must land before ANY file is registered: CBOS's
        # backend EXE (Step 7 = "the main process that makes the file entry")
        # only picked files up correctly when every Step 5 for the batch ran
        # first and every Step 7 ran after - interleaving them per file left
        # Table2 STATUS stuck at PENDING and FILEUPLOAD never went TRUE, even
        # though each call answered Success individually.
        pending_registration: list[tuple] = []

        for file_path, exchange in files:
            record = repo.create_audit_record(
                file_path, task.folder_date, task.segment, exchange,
                correlation_id=task.correlation_id, batch_id=task.batch_id,
            )
            request_log: list = []

            # NO_EXCHANGE is a placeholder for segments that don't split by
            # exchange, not a real one. It must not reach the tie-breaker:
            # that does a substring match of the exchange against the CBOS
            # rule name, and "NA" appears inside ordinary words (FINAL,
            # MANUAL, NATIONAL), so it could break a tie towards the wrong
            # UploadID. None means "no exchange information" - the honest input.
            match_exchange = None if exchange == file_service.NO_EXCHANGE else exchange

            try:
                rule = upload_matching.match_file(file_path, rules, exchange=match_exchange)
            except FileRejected as exc:
                apply_outcome(repo, record, file_path, upload_outcome.rejected(exc))
                continue

            repo.update(
                record,
                cbos_upload_id=rule.upload_id,
                matched_pattern=rule.name,
                process_id=process_id,
                cbos_upload_settings=json.dumps(rule.raw_settings, default=str),
            )
            repo.commit()

            # CBOS-side idempotency: reusing an EXISTING PROCESSID (see
            # find_existing_process_id above) reads back each Table2 slot's
            # real STATUS instead of resetting it to PENDING. A slot CBOS
            # already reports as done must not receive another file - move
            # it out of the source without re-uploading.
            candidate = candidates_by_upload_id.get(str(rule.upload_id))
            if candidate is not None and not candidate.needs_upload:
                logger.info("Idempotent skip: %s already accepted by CBOS (UploadID=%s, ProcessID=%s, STATUS=%s)",
                            file_path.name, rule.upload_id, process_id, candidate.status)
                request_log.append({"step": "idempotent_skip", "reason": "cbos_status", "status": candidate.status})
                apply_outcome(repo, record, file_path, upload_outcome.idempotent_skip(), request_log)
                filled_upload_ids.add(str(rule.upload_id))
                continue

            # Idempotency: if this exact file already reached CBOS for this
            # segment/date/UploadID (e.g. a re-drop after a Step-9 timeout), it's
            # already there - skip the re-upload, just move it out of the source.
            if repo.find_completed(task.segment, task.folder_date, rule.upload_id, file_path.name):
                logger.info("Idempotent skip: %s already uploaded (segment=%s date=%s UploadID=%s)",
                            file_path.name, task.segment, task.folder_date, rule.upload_id)
                request_log.append({"step": "idempotent_skip"})
                apply_outcome(repo, record, file_path, upload_outcome.idempotent_skip(), request_log)
                filled_upload_ids.add(str(rule.upload_id))
                continue

            try:
                # Step 5: upload the file under its correctly-matched UploadID.
                # The GUID is OURS (client-generated, not returned by CBOS) and is
                # persisted BEFORE the first chunk goes out. If the upload dies
                # part-way, the abandoned CBOS drop folder is still named in this
                # audit row: those chunks are inert (Step 7 never registered the
                # GUID) but unrecoverable if we never wrote the name down.
                guid = str(uuid.uuid4())
                repo.update(record, guid=guid)
                repo.commit()
                logger.info("Upload started = %s (UploadID=%s, GUID=%s)",
                            file_path.name, rule.upload_id, guid)
                client.upload_file(file_path, rule.upload_id, guid)
                request_log.append({"step": "SaveTradePromodalUploadChunkFile", "upload_id": rule.upload_id, "guid": guid})
                pending_registration.append((record, file_path, rule, guid, request_log))
            except Exception as exc:
                logger.error("Batch %s: upload sequence failed for %s: %s", task.key, file_path.name, exc)
                request_log.append({"step": "error", "error": str(exc)})
                apply_outcome(repo, record, file_path, upload_outcome.failed(exc), request_log)

        # Step 7: register every uploaded file - once every Step 5 in the batch
        # has completed, not interleaved with them (see pending_registration's
        # comment above).
        for record, file_path, rule, guid, request_log in pending_registration:
            try:
                client.register_file(rule.upload_id, guid, file_path.name, process_id, trade_date)
                request_log.append({"step": "SaveNewTradeProcessPromodalUploadFile", "upload_id": rule.upload_id})
                logger.info("CBOS entry created = %s (UploadID=%s, GUID=%s)", file_path.name, rule.upload_id, guid)

                uploaded_candidates.append((record, file_path, request_log))
                filled_upload_ids.add(str(rule.upload_id))
            except Exception as exc:
                logger.error("Batch %s: registration failed for %s: %s", task.key, file_path.name, exc)
                request_log.append({"step": "error", "error": str(exc)})
                apply_outcome(repo, record, file_path, upload_outcome.failed(exc), request_log)

        if task.files and not uploaded_candidates and not filled_upload_ids:
            # Files were declared and attempted, but none reached (or was
            # already in) CBOS - everything rejected/failed.
            logger.info("Batch %s: no file reached or was found in CBOS - nothing to confirm", task.key)
            _set_batch_status(task, BatchStatus.FAILED,
                              {"reason": "no files uploaded (all rejected/failed)"})
            return

        # Step 6: existing-process confirmation lookup, once per batch. Non-fatal -
        # purely diagnostic (it also confirms EDP_Billing will be able to find our
        # PROCESSID via getdropdown).
        try:
            client.existing_process(task.segment, trade_date)
        except CBOSUploadError as exc:
            logger.warning("Batch %s: getdropdown(EXISTINGPROCESSID) failed (non-fatal): %s", task.key, exc)

        # ------------------------------------------------------------------
        # COMPLETENESS GATE (docs/BATCH_HANDOFF_CONTRACT.md). The required
        # set is CBOS's own Table2: every file-expecting slot of this PID.
        # Step 8 may only auto-mark ALLOWLISTED unfilled slots optional -
        # blanket-marking was how billing could run on incomplete data.
        # ------------------------------------------------------------------
        empty_slots = [
            c for c in reservation.candidates
            if c.needs_upload and c.upload_id not in filled_upload_ids
        ]
        allowed = optional_slots.allowlisted(task.segment)
        missing_required = [
            c for c in empty_slots if c.expects_a_file and c.upload_id not in allowed
        ]
        if missing_required:
            missing_desc = [f"{c.upload_id} ({c.name})" for c in missing_required]
            logger.error(
                "Batch %s: COMPLETENESS GATE - %d mandatory Table2 slot(s) unfilled and not "
                "allowlisted: %s. Parking batch INCOMPLETE - no Step 8/9, FILEUPLOAD stays FALSE. "
                "Fix: superseding manifest with the missing file(s), or audited "
                "POST /batches/{batch_id}/proceed.",
                task.key, len(missing_required), missing_desc,
            )
            parked = upload_outcome.gate_parked([str(c.upload_id) for c in missing_required])
            for record, file_path, request_log in uploaded_candidates:
                request_log.append({"step": "completeness_gate", "result": "incomplete",
                                    "missing_slots": [c.upload_id for c in missing_required]})
                apply_outcome(repo, record, file_path, parked, request_log)
            _set_batch_status(task, BatchStatus.INCOMPLETE, {
                "missing_slots": [
                    {"upload_id": c.upload_id, "step_no": c.step_no, "name": c.name}
                    for c in missing_required
                ],
            })
            return

        # Step 8 (Skip Optional Subprocess in Template): the gate passed, so
        # every remaining empty slot is either allowlisted (legitimately
        # absent today) or a zero-UploadID computation/posting step that never
        # takes a file - both get marked skippable so FILEUPLOAD can go TRUE.
        # Already-resolved slots (needs_upload False - an earlier batch on
        # this PROCESSID filled or skipped them) are left alone. Non-fatal per
        # slot. NOTE: the trigger (Step 10) and every downstream step are
        # owned by the EDP_Billing scheduler, not this repo - our job ends at
        # "make FILEUPLOAD go TRUE". See docs/CBOS_HANDOFF_CONTRACT.md.
        for candidate in empty_slots:
            try:
                client.mark_step_optional(process_id, candidate.step_no)
            except CBOSUploadError as exc:
                logger.warning("Batch %s: mark-optional STEPNO=%s failed (non-fatal): %s",
                               task.key, candidate.step_no, exc)

        # Step 9: our own confirmation that the files landed (EDP_Billing is the
        # authoritative FILEUPLOAD poller + the one that triggers). Per segment.
        #
        # Log the gate's inputs first. CBOS only ever answers TRUE/FALSE, so
        # when it says FALSE this line is the only place the log records which
        # slots we believe we satisfied and how - without it, a stuck poll is
        # twenty identical MSG=FALSE lines and no way to tell whether a file was
        # missed, mis-matched, or genuinely still processing on CBOS's side.
        marked_optional = [str(c.upload_id) for c in empty_slots]
        logger.info(
            "Batch %s: Step 9 gate - %d Table2 slot(s) considered: filled=%s marked-optional/skipped=%s",
            task.key,
            len(marked_optional) + len(filled_upload_ids),
            sorted(filled_upload_ids) or "none",
            marked_optional or "none",
        )
        poll_message = client.confirm_upload(task.segment, trade_date)
        outcome = upload_outcome.from_poll_result(poll_message)
        for record, file_path, request_log in uploaded_candidates:
            request_log.append({"step": "file_process_status", "result": poll_message})
            apply_outcome(repo, record, file_path, outcome, request_log)

        _set_batch_status(
            task,
            BatchStatus.CONFIRMED if poll_message == "TRUE" else BatchStatus.UNCONFIRMED,
            {"fileupload": poll_message},
        )
        logger.info(
            "Batch %s complete: %d file(s) in CBOS (%s)",
            task.key, len(uploaded_candidates),
            f"FILEUPLOAD MSG={poll_message}",
        )

    finally:
        session.close()
        logger.debug("process_batch: session closed for %s", task.key)


# --------------------------------------------------------------------------
# Force-proceed lane: an INCOMPLETE batch, resumed by ops naming the slots
# billing may run without (POST /batches/{batch_id}/proceed - already
# recorded on the batches row by the endpoint). No files move here: whatever
# uploaded is already in CBOS; this lane only marks slots optional and
# re-confirms.
# --------------------------------------------------------------------------

def _proceed_batch(task: SegmentBatchTask) -> None:
    trade_date = task.folder_date
    client = cbos_client.get_cbos_client()
    requested = {str(s) for s in task.proceed_slots}
    logger.info("Proceed %s (batch %s): ops-approved optional slots=%s reason=%r",
                task.key, task.batch_id, sorted(requested), task.proceed_reason)

    existing = client.find_existing_process_id(task.segment, trade_date)
    if not existing:
        logger.error("Proceed %s: no PROCESSID exists for this segment/date - nothing to proceed", task.key)
        _set_batch_status(task, BatchStatus.INCOMPLETE,
                          {"proceed_error": "no PROCESSID found for segment/date"})
        return

    try:
        reservation = client.reserve_process(task.segment, trade_date, existing)
    except CBOSUploadError as exc:
        logger.error("Proceed %s: could not re-fetch PROCESSID=%s: %s", task.key, existing, exc)
        _set_batch_status(task, BatchStatus.INCOMPLETE, {"proceed_error": f"re-fetch failed: {exc}"})
        return

    empty_slots = [c for c in reservation.candidates if c.needs_upload]
    empty_file_slots = {c.upload_id for c in empty_slots if c.expects_a_file}

    unknown = requested - empty_file_slots
    if unknown:
        # Naming a slot that is filled (or not part of this process) means ops
        # acted on stale information - stop and let them re-check GET /batches.
        logger.error("Proceed %s: requested slot(s) %s are not unfilled file slots of PROCESSID=%s "
                     "(unfilled: %s)", task.key, sorted(unknown), existing, sorted(empty_file_slots))
        _set_batch_status(task, BatchStatus.INCOMPLETE, {
            "proceed_error": f"slots not unfilled on this process: {sorted(unknown)}",
            "unfilled_slots": sorted(empty_file_slots),
        })
        return

    allowed = optional_slots.allowlisted(task.segment) | requested
    still_missing = [c for c in empty_slots if c.expects_a_file and c.upload_id not in allowed]
    if still_missing:
        logger.error("Proceed %s: slots %s remain mandatory and unfilled - staying INCOMPLETE",
                     task.key, [c.upload_id for c in still_missing])
        _set_batch_status(task, BatchStatus.INCOMPLETE, {
            "missing_slots": [
                {"upload_id": c.upload_id, "step_no": c.step_no, "name": c.name}
                for c in still_missing
            ],
            "proceed_note": f"proceed covered {sorted(requested)} but these remain",
        })
        return

    for candidate in empty_slots:
        try:
            client.mark_step_optional(reservation.process_id, candidate.step_no)
        except CBOSUploadError as exc:
            logger.warning("Proceed %s: mark-optional STEPNO=%s failed (non-fatal): %s",
                           task.key, candidate.step_no, exc)

    poll_message = client.confirm_upload(task.segment, trade_date)
    _set_batch_status(
        task,
        BatchStatus.CONFIRMED if poll_message == "TRUE" else BatchStatus.UNCONFIRMED,
        {"fileupload": poll_message, "via": "force-proceed", "proceed_slots": sorted(requested)},
    )
    logger.info("Proceed %s complete: FILEUPLOAD MSG=%s", task.key, poll_message)
