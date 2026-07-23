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
    CONFIRMED = "confirmed"              # uploaded, registered, FILEUPLOAD TRUE
    UNCONFIRMED = "unconfirmed"          # uploaded, registered, FILEUPLOAD not yet TRUE
    IDEMPOTENT_SKIP = "idempotent_skip"  # already in CBOS for this batch + UploadID
    REJECTED = "rejected"                # matched no upload rule, or failed a local check
    FAILED = "failed"                    # a CBOS call errored
    GATE_PARKED = "gate_parked"          # in CBOS, but the batch parked INCOMPLETE (gate)


@dataclass(frozen=True)
class FileOutcome:
    outcome: Outcome
    destination: Destination
    status: str                          # the audit row's status column
    cbos_response: str
    validation_error: str | None = None
    counts_as_retry: bool = False
    stamp_uploaded_at: bool = False


def confirmed() -> FileOutcome:
    """Step 9 reported FILEUPLOAD TRUE.

    The audit row records the verdict, not a payload: the client returns a
    bool, so anything payload-shaped stored here would be fabricated rather
    than what CBOS actually sent.
    """
    return FileOutcome(
        outcome=Outcome.CONFIRMED,
        destination=Destination.UPLOADED,
        status="uploaded",
        cbos_response="FILEUPLOAD confirmed TRUE",
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
    """Step 9's verdict for a file that was uploaded and registered.

    poll_message is CBOS's own last FILEUPLOAD message (or POLL_TIMED_OUT).
    Only TRUE is documented to mean good-to-go; everything else lands in
    uploaded/ as unconfirmed, carrying the message verbatim so the audit row
    says what CBOS said rather than what we assumed it meant.
    """
    return confirmed() if poll_message == "TRUE" else unconfirmed(poll_message)
