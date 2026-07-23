"""Batch intake + status + rescan + force-proceed — the service behind the
batches API (docs/BATCH_HANDOFF_CONTRACT.md). The API layer stays a thin
router (ADR 3): sessions arrive via the documented dependency and ALL
orchestration (validate → verify → record → enqueue) lives here.

Statuses are edpb_core.batch_api.BatchStatus everywhere (ADR 5) — the same
enum the EDP_Billing engine reads, so the two sides can't drift.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from edpb_core.batch_api import BatchStatus
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.queue import BatchQueue, SegmentBatchTask
from app.models.batch import Batch
from app.repositories.batch_repository import BatchRepository
from app.repositories.uploaded_file_repository import UploadedFileRepository
from app.services import file_service, manifest_service
from app.services.manifest_service import ChecksumMismatchError, LoadedManifest, ManifestError

logger = logging.getLogger("batch_service")


class UnknownBatchError(Exception):
    """No batches row for this batch_id (HTTP 404 at the API layer)."""


class ProceedNotAllowedError(Exception):
    """Force-proceed on a batch that isn't INCOMPLETE (HTTP 409)."""


@dataclass(frozen=True)
class IntakeResult:
    batch_id: str
    status: str
    already_known: bool  # True -> API answers 200, else 202


def submit_manifest(session: Session, queue: BatchQueue, manifest_path: Path) -> IntakeResult:
    """The intake lane: validate -> verify checksums -> record -> enqueue.

    Idempotent on batch_id, airtight under concurrency: a lost create race
    (uq_batches_batch_id) is treated as "already known", never a 500. A
    re-POST of a still-QUEUED batch re-enqueues it — that makes POST /batches
    itself the recovery path for a crash between record and enqueue, and for
    a superseding manifest whose task was in flight.

    Raises ManifestError (400) / ChecksumMismatchError (422, recorded as
    REJECTED with files left in place).
    """
    repo = BatchRepository(session)
    manifest = manifest_service.load_manifest(manifest_path)

    existing = repo.find_by_batch_id(manifest.batch_id)
    if existing is not None:
        return _acknowledge_known(queue, manifest, existing)

    try:
        manifest_service.verify_checksums(manifest_path, manifest.raw)
    except ChecksumMismatchError as exc:
        # Recorded for audit (GET /batches/{id} answers "why was it
        # rejected"); files stay in place — a superseding manifest is the fix.
        try:
            repo.create(
                batch_id=manifest.batch_id,
                segment=manifest.segment,
                trade_date=manifest.trade_date,
                folder_date=manifest.folder_date,
                manifest_path=str(manifest_path),
                correlation_id=manifest.correlation_id,
                status=BatchStatus.REJECTED,
                status_detail=json.dumps({"checksum_error": str(exc)}),
            )
        except IntegrityError:
            session.rollback()  # concurrent POST recorded it first — same outcome
        raise

    try:
        repo.create(
            batch_id=manifest.batch_id,
            segment=manifest.segment,
            trade_date=manifest.trade_date,
            folder_date=manifest.folder_date,
            manifest_path=str(manifest_path),
            correlation_id=manifest.correlation_id,
        )
    except IntegrityError:
        # Lost a create race with a concurrent POST of the same batch_id —
        # the contract's answer is 200 {"status": "<current>"}, not a 500.
        session.rollback()
        existing = repo.find_by_batch_id(manifest.batch_id)
        if existing is None:  # pragma: no cover - only a rolled-back create
            raise
        logger.info(
            "submit_manifest: %s lost create race - acknowledging existing (status=%s)",
            manifest.batch_id,
            existing.status,
        )
        return _acknowledge_known(queue, manifest, existing)

    _enqueue_checked(queue, manifest)
    return IntakeResult(batch_id=manifest.batch_id, status=BatchStatus.QUEUED, already_known=False)


def _acknowledge_known(
    queue: BatchQueue, manifest: LoadedManifest, existing: Batch
) -> IntakeResult:
    """A batch_id we already recorded. Non-terminal (QUEUED/UPLOADING) rows
    are re-enqueued — idempotently: the in-memory guard drops the duplicate
    if the task is genuinely still queued or in flight."""
    if existing.status in (BatchStatus.QUEUED, BatchStatus.UPLOADING):
        # QUEUED: crash between record and enqueue, or a dropped duplicate.
        # UPLOADING: the process died mid-batch and the in-memory queue was
        # lost - without this, that row was stranded forever. A LIVE mid-
        # flight batch is safe: its guard key is still held, so this enqueue
        # is dropped.
        _enqueue_checked(queue, manifest)
    logger.info(
        "submit_manifest: batch %s already known (status=%s)", existing.batch_id, existing.status
    )
    return IntakeResult(batch_id=existing.batch_id, status=existing.status, already_known=True)


def _enqueue_checked(queue: BatchQueue, manifest: LoadedManifest) -> None:
    """Enqueue and LOG a dropped duplicate. The guard key includes batch_id
    (see SegmentBatchTask.key), so a superseding manifest for the same
    segment/date is a different key and can never be silently swallowed."""
    task = manifest_service.to_task(manifest)
    if not queue.enqueue(task):
        logger.info("Batch %s already queued/in flight - not enqueued twice", task.key)


def get_batch_details(session: Session, batch_id: str) -> dict:
    batch = BatchRepository(session).find_by_batch_id(batch_id)
    if batch is None:
        raise UnknownBatchError(batch_id)
    files = UploadedFileRepository(session).find_for_batch(
        batch.batch_id,
        batch.folder_date,
        batch.segment,
    )
    return {
        "batch_id": batch.batch_id,
        "segment": batch.segment,
        "trade_date": batch.trade_date,
        "status": batch.status,
        "status_detail": json.loads(batch.status_detail) if batch.status_detail else None,
        "correlation_id": batch.correlation_id,
        "proceed": (
            {
                "slots": json.loads(batch.proceed_slots),
                "reason": batch.proceed_reason,
                "at": batch.proceeded_at.isoformat() if batch.proceeded_at else None,
            }
            if batch.proceed_slots
            else None
        ),
        "files": [
            {
                "name": f.file_name,
                "status": f.status,
                "outcome": f.cbos_response,
                "cbos_upload_id": f.cbos_upload_id,
                "process_id": f.process_id,
            }
            for f in files
        ],
    }


def rescan(session: Session, queue: BatchQueue) -> dict:
    """Queue every on-disk manifest whose batch_id isn't known yet, and
    re-enqueue known-but-still-QUEUED batches (callback/crash catch-up)."""
    queued: list[str] = []
    skipped = 0
    for manifest_path in manifest_service.find_manifests(file_service.get_root()):
        try:
            result = submit_manifest(session, queue, manifest_path)
        except (ManifestError, ChecksumMismatchError) as exc:
            logger.warning("rescan: manifest %s rejected: %s", manifest_path, exc)
            skipped += 1
            continue
        if not result.already_known:
            queued.append(result.batch_id)
    logger.info("rescan: queued=%s skipped=%d", queued, skipped)
    return {"queued": queued, "skipped": skipped}


def request_proceed(
    session: Session, queue: BatchQueue, batch_id: str, slots: list[str], reason: str
) -> dict:
    """Audited force-proceed for an INCOMPLETE batch (see the contract's
    completeness-gate section). Records the ops decision, then queues the
    proceed lane (upload_service._proceed_batch)."""
    repo = BatchRepository(session)
    batch = repo.find_by_batch_id(batch_id)
    if batch is None:
        raise UnknownBatchError(batch_id)
    if batch.status != BatchStatus.INCOMPLETE:
        raise ProceedNotAllowedError(
            f"batch {batch_id} is {batch.status!r} - proceed applies only to 'incomplete'"
        )

    repo.record_proceed(batch, slots, reason)
    queue.enqueue(
        SegmentBatchTask(
            folder_date=batch.folder_date,
            segment=batch.segment,
            batch_id=batch.batch_id,
            correlation_id=batch.correlation_id,
            mode="proceed",
            proceed_slots=list(slots),
            proceed_reason=reason,
        )
    )
    return {"batch_id": batch_id, "status": "proceed_queued", "slots": slots}
