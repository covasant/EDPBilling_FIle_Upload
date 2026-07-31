"""The routing decision: given what happened to a file, where does it land and
what does its audit row say?

This module only *decides*. It touches no filesystem, no database and no
network, so the whole decision table can be asserted directly. Carrying a
decision out is upload_service.apply_outcome's job.

The six outcomes are the complete set - every manifest-listed file ends in
exactly one of them. See CONTEXT.md's Outcomes table.
"""

from dataclasses import dataclass
from enum import StrEnum


class Destination(StrEnum):
    """Which sibling folder the file is moved into."""

    UPLOADED = "uploaded"
    FAILED = "failed"


class Outcome(StrEnum):
    CONFIRMED = "confirmed"  # uploaded + registered in CBOS - our job, done
    # No longer produced by from_poll_result: it meant "FILEUPLOAD not yet TRUE",
    # which under trigger-first ordering is every file, always. Kept so historical
    # audit rows written before 2026-07-31 still resolve.
    UNCONFIRMED = "unconfirmed"
    IDEMPOTENT_SKIP = "idempotent_skip"  # already in CBOS for this batch + UploadID
    REJECTED = "rejected"  # matched no upload rule, or failed a local check
    FAILED = "failed"  # a CBOS call errored
    GATE_PARKED = "gate_parked"  # in CBOS, but the batch parked INCOMPLETE (gate)


@dataclass(frozen=True)
class FileOutcome:
    outcome: Outcome
    destination: Destination
    status: str  # the audit row's status column
    cbos_response: str
    validation_error: str | None = None
    counts_as_retry: bool = False
    stamp_uploaded_at: bool = False


def confirmed(poll_message: str = "") -> FileOutcome:
    """The file is in CBOS: chunked (Step 5) and registered (Step 7).

    poll_message is CBOS's last FILEUPLOAD word, recorded verbatim as context
    only - it is not what makes this confirmed, and it is normally FALSE here
    because the engine's trigger has not fired yet. See from_poll_result.
    """
    said = f" (FILEUPLOAD read {poll_message} - trigger not yet fired)" if poll_message else ""
    return FileOutcome(
        outcome=Outcome.CONFIRMED,
        destination=Destination.UPLOADED,
        status="uploaded",
        cbos_response=f"Uploaded and registered in CBOS{said}",
        stamp_uploaded_at=True,
    )


def unconfirmed(poll_message: str = "") -> FileOutcome:
    """Steps 5 and 7 succeeded - the file IS in CBOS - but our Step 9 read
    didn't confirm good-to-go.

    Lands in uploaded/, not uploadFailed/: re-dropping a file CBOS already
    holds would duplicate it. EDP_Billing is the authoritative FILEUPLOAD
    poller and triggers once CBOS reports TRUE.

    poll_message is CBOS's last word (FALSE, SKIP, POLL_TIMED_OUT, ...) and is
    recorded verbatim. Every unconfirmed file used to store one fixed sentence,
    so the audit row couldn't tell "still pending" apart from "SKIP" - a
    distinction that turned out to matter.
    """
    said = f" (FILEUPLOAD said {poll_message})" if poll_message else ""
    return FileOutcome(
        outcome=Outcome.UNCONFIRMED,
        destination=Destination.UPLOADED,
        status="uploaded",
        cbos_response=f"Registered in CBOS; FILEUPLOAD good-to-go not confirmed by uploader{said}",
        stamp_uploaded_at=True,
    )


def idempotent_skip() -> FileOutcome:
    """This exact file already reached CBOS for this segment, trade date and
    UploadID. Move it out of the source folder without re-uploading.

    No uploaded_at stamp - this attempt didn't upload anything; the original
    attempt's row carries the timestamp.
    """
    return FileOutcome(
        outcome=Outcome.IDEMPOTENT_SKIP,
        destination=Destination.UPLOADED,
        status="uploaded",
        cbos_response="Skipped - already uploaded (idempotent)",
    )


def gate_parked(missing_slots: list[str]) -> FileOutcome:
    """This file IS in CBOS (Steps 5+7 succeeded), but the batch parked
    INCOMPLETE at the completeness gate - other mandatory slots are unfilled,
    so Step 8/9 never ran and FILEUPLOAD stays FALSE.

    Lands in uploaded/ (the file itself is safely registered; re-dropping it
    would duplicate it - CBOS's per-slot STATUS readback idempotent-skips it
    on any re-run). The BATCH-level story lives on the batches row
    (status=incomplete, missing slots in status_detail)."""
    return FileOutcome(
        outcome=Outcome.GATE_PARKED,
        destination=Destination.UPLOADED,
        status="uploaded",
        cbos_response=(
            "Registered in CBOS; batch parked INCOMPLETE at completeness gate "
            f"(unfilled mandatory slots: {', '.join(missing_slots)})"
        ),
        stamp_uploaded_at=True,
    )


def rejected(error: Exception) -> FileOutcome:
    """Rejected locally before any upload call was made - no UploadID pattern
    matched, an ambiguous match, or a column-count mismatch.

    A per-file outcome, never an application failure.
    """
    return FileOutcome(
        outcome=Outcome.REJECTED,
        destination=Destination.FAILED,
        status="failed",
        cbos_response=f"Rejected before upload: {error}",
        validation_error=str(error),
        counts_as_retry=True,
    )


def failed(error: Exception) -> FileOutcome:
    """A CBOS call errored - during batch setup, or during this file's upload
    or registration."""
    return FileOutcome(
        outcome=Outcome.FAILED,
        destination=Destination.FAILED,
        status="failed",
        cbos_response=str(error),
        counts_as_retry=True,
    )


def from_poll_result(poll_message: str) -> FileOutcome:
    """Outcome for a file that was uploaded (Step 5) and registered (Step 7).

    That is CONFIRMED regardless of what Step 9 reads. The file is in CBOS - we
    put it there and CBOS acknowledged the registration - and that is the whole
    question this audit row answers.

    It used to key off `poll_message == "TRUE"`, which was unreachable in
    practice: under trigger-first ordering (SME ruling 2026-07-24) FILEUPLOAD
    cannot go TRUE until the engine TRIGGERS, which happens after we finish. So
    every file was recorded as "good-to-go not confirmed" no matter how cleanly
    it uploaded. poll_message is still carried into the batch's status_detail as
    diagnostics (upload_service._fileupload_observation).

    Note both branches always produced status="uploaded" and destination=
    UPLOADED, so this only ever changed the wording of the audit trail - never
    where a file landed or whether it counted as uploaded.
    """
    return confirmed(poll_message)
