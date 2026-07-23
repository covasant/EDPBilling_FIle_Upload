"""The routing decision table, asserted directly - no filesystem, no database,
no CBOS. See CONTEXT.md's Outcomes table."""

import dataclasses

import pytest

from app.services import upload_outcome
from app.services.upload_outcome import Destination, Outcome


def test_confirmed_lands_in_uploaded_and_stamps_time():
    o = upload_outcome.confirmed()
    assert o.outcome is Outcome.CONFIRMED
    assert o.destination is Destination.UPLOADED
    assert o.status == "uploaded"
    assert o.stamp_uploaded_at
    assert not o.counts_as_retry


def test_unconfirmed_lands_in_uploaded_not_failed():
    """The file IS in CBOS - routing it to uploadFailed/ would invite a
    duplicate re-upload. This is the H5 regression, now assertable directly."""
    o = upload_outcome.unconfirmed()
    assert o.destination is Destination.UPLOADED
    assert o.status == "uploaded"
    assert "not confirmed" in o.cbos_response.lower()


def test_idempotent_skip_does_not_stamp_uploaded_at():
    """This attempt uploaded nothing; the original attempt's row owns the
    timestamp."""
    o = upload_outcome.idempotent_skip()
    assert o.destination is Destination.UPLOADED
    assert not o.stamp_uploaded_at
    assert not o.counts_as_retry


def test_rejected_carries_the_reason_and_counts_a_retry():
    o = upload_outcome.rejected(ValueError("no UploadID pattern matched"))
    assert o.destination is Destination.FAILED
    assert o.status == "failed"
    assert o.validation_error == "no UploadID pattern matched"
    assert o.cbos_response.startswith("Rejected before upload:")
    assert o.counts_as_retry


def test_failed_counts_a_retry_but_sets_no_validation_error():
    """A CBOS error is not a validation failure - validation_error stays empty
    so the two causes remain distinguishable in the audit log."""
    o = upload_outcome.failed(RuntimeError("CBOS 500"))
    assert o.destination is Destination.FAILED
    assert o.status == "failed"
    assert o.validation_error is None
    assert o.counts_as_retry


def test_poll_result_maps_to_confirmed_or_unconfirmed():
    assert upload_outcome.from_poll_result("TRUE").outcome is Outcome.CONFIRMED
    assert upload_outcome.from_poll_result("FALSE").outcome is Outcome.UNCONFIRMED
    # SKIP is not "still pending" - but it is still not TRUE, so the file stays
    # unconfirmed rather than being treated as good-to-go.
    assert upload_outcome.from_poll_result("SKIP").outcome is Outcome.UNCONFIRMED
    assert "SKIP" in upload_outcome.from_poll_result("SKIP").cbos_response


def test_only_failures_route_to_uploadfailed():
    """The whole table in one assertion: exactly two of the five outcomes send
    a file to uploadFailed/."""
    to_failed = {
        o.outcome
        for o in (
            upload_outcome.confirmed(),
            upload_outcome.unconfirmed(),
            upload_outcome.idempotent_skip(),
            upload_outcome.rejected(ValueError("x")),
            upload_outcome.failed(RuntimeError("y")),
        )
        if o.destination is Destination.FAILED
    }
    assert to_failed == {Outcome.REJECTED, Outcome.FAILED}


def test_outcomes_are_immutable():
    """Outcomes are values - apply_outcome must not be able to mutate one it
    was handed."""
    o = upload_outcome.confirmed()
    with pytest.raises(dataclasses.FrozenInstanceError):
        o.status = "tampered"
