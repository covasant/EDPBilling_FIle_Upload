"""The log has to be usable as the primary diagnostic for a CBOS batch.

Two things are being pinned here, both of which were broken or absent before:

1. Every CBOS step narrates its request and its response, in BOTH modes. The
   wire logging used to live inside CBOSClient, so a MOCK run - the mode used
   for local development - printed step names and never a payload.

2. Every line a batch run emits carries the same id, including lines from
   modules that know nothing about batches. The batch key can't do this job
   alone: the scheduler rescans every 30s, so "17-07-2026|MCX" appears in the
   log dozens of times a day and grepping it returns every run at once.

These are asserted rather than eyeballed because logging is the kind of thing
that decays silently: nothing fails when a log line stops being emitted.
"""

import logging

import pytest

from app.clients import cbos_client
from app.core import correlation


@pytest.fixture
def mock_client(monkeypatch):
    monkeypatch.setenv("CBOS_MODE", "MOCK")
    from app.core.config import get_settings

    get_settings.cache_clear()
    return cbos_client.MockCBOSClient()


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records]


# --- step narration -------------------------------------------------------

def test_mock_mode_logs_request_and_response_for_each_step(caplog, mock_client):
    """The gap this closes: MOCK mode used to show 'Step 2 - ...' and nothing
    about what was sent or what came back."""
    caplog.set_level(logging.INFO, logger="cbos_client")

    mock_client.reserve_process("MCX", "17-07-2026")

    msgs = _messages(caplog)
    request = [m for m in msgs if "Step 2 getNewTradeProcess REQUEST" in m]
    response = [m for m in msgs if "Step 2 getNewTradeProcess RESPONSE" in m]

    assert len(request) == 1, f"expected one Step 2 request line, got: {msgs}"
    assert len(response) == 1, f"expected one Step 2 response line, got: {msgs}"
    # The request line must show what we actually asked for, not just the name.
    assert "MCX" in request[0] and "17-07-2026" in request[0]
    # The response line must show what came back, not just that it arrived.
    assert "PROCESSID" in response[0]


def test_reservation_logs_the_slots_cbos_expects(caplog, mock_client):
    """The Table2 slot list is the batch's plan. Logging it at reserve time is
    what makes a later FILEUPLOAD=FALSE diagnosable from the log alone."""
    caplog.set_level(logging.INFO, logger="cbos_client")

    reservation = mock_client.reserve_process("MCX", "17-07-2026")

    line = next(m for m in _messages(caplog) if "Step 2 reserved ProcessID" in m)
    expected = [c.upload_id for c in reservation.candidates if c.expects_a_file]
    assert f"{len(expected)} expecting a file" in line
    for upload_id in expected:
        assert upload_id in line


def test_a_failed_step_logs_at_error_even_when_the_caller_ignores_it(caplog, mock_client):
    """Steps 3, 6 and 8 are non-fatal - the caller swallows the exception. If
    the failure were logged at the call's own level it could be invisible at
    INFO, and a silently-skipped Step 8 is exactly the failure that lets CBOS
    bill without a file."""
    caplog.set_level(logging.INFO, logger="cbos_client")

    def boom(*_args, **_kwargs):
        raise cbos_client.CBOSUploadError("CBOS said no")

    mock_client._update_step_optional = boom

    with pytest.raises(cbos_client.CBOSUploadError):
        mock_client.mark_step_optional("17658", 4)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a failed CBOS call must reach the log at ERROR"
    assert "Step 8" in errors[0].getMessage()
    assert "CBOS said no" in errors[0].getMessage()


def test_chunk_calls_stay_off_the_info_log(caplog, mock_client, tmp_path):
    """A large file is hundreds of chunks. Per-chunk lines at INFO would bury
    the batch narrative, so they belong at DEBUG - but the Step 5 summary must
    still be visible at INFO, otherwise the upload leaves no INFO trace."""
    src = tmp_path / "Trade_MCX.csv"
    src.write_bytes(b"x" * 3000)

    caplog.set_level(logging.INFO, logger="cbos_client")
    mock_client.upload_file(src, upload_id="535", guid="guid-1")

    msgs = _messages(caplog)
    assert not [m for m in msgs if "Step 5 SaveTradePromodalUploadChunkFile REQUEST" in m], \
        "per-chunk lines must not appear at INFO"
    assert [m for m in msgs if "Step 5 complete" in m], "the Step 5 summary must survive at INFO"


def test_long_response_is_truncated_but_says_so(caplog, mock_client):
    """A truncated body that doesn't announce the truncation is worse than no
    body: it reads as a complete response that genuinely ended there."""
    caplog.set_level(logging.INFO, logger="cbos_client")

    mock_client._check_process_id_exist = lambda *_a, **_k: {
        "Data": [{"MSG": "x" * 5000}]
    }
    mock_client.check_process_exists("MCX", "14-07-2026")

    line = next(m for m in _messages(caplog) if "Step 3 CheckProcessIDExist RESPONSE" in m)
    assert "LOG_LEVEL=DEBUG" in line, "truncation must be marked and point at the fix"
    assert len(line) < 1200, "a response line must stay greppable"


def test_secrets_never_reach_a_step_log_line(caplog, mock_client):
    """_call logs its arguments; if a credential-bearing argument is ever added
    to a step it must be masked, the same as the HTTP payload path."""
    caplog.set_level(logging.INFO, logger="cbos_client")

    mock_client._check_process_id_exist = lambda *_a, **_k: {"Data": [{"MSG": "ok"}]}
    mock_client._call(3, "CheckProcessIDExist",
                      lambda: {"Data": [{"MSG": "ok"}]},
                      segment="MCX", password="hunter2")

    line = next(m for m in _messages(caplog) if "REQUEST" in m)
    assert "hunter2" not in line
    assert "***" in line


# --- correlation id -------------------------------------------------------

def test_two_runs_of_the_same_batch_get_different_ids():
    """The whole point. "17-07-2026|MCX" recurs every rescan, so the key alone
    can't separate one run's lines from the previous run's."""
    with correlation.batch_context("17-07-2026|MCX"):
        first = correlation.label()
    with correlation.batch_context("17-07-2026|MCX"):
        second = correlation.label()

    assert first != second
    assert first.endswith("17-07-2026|MCX") and second.endswith("17-07-2026|MCX")


def test_context_is_restored_after_a_batch_even_if_it_raises():
    """A batch that dies must not leak its id onto the scheduler's next line -
    which would attribute unrelated work to a failed run."""
    assert correlation.label() == correlation.NO_CONTEXT

    with pytest.raises(ValueError):
        with correlation.batch_context("17-07-2026|MCX"):
            raise ValueError("batch blew up")

    assert correlation.label() == correlation.NO_CONTEXT


def test_the_formatter_stamps_the_id_onto_every_record(caplog):
    """The filter has to apply to records from modules that know nothing about
    correlation - otherwise a third-party log line crashes the formatter on a
    missing %(corr)s field."""
    from app.core.logging import _CorrelationFilter

    record = logging.LogRecord("sqlalchemy.engine", logging.INFO, __file__, 1,
                               "SELECT 1", None, None)
    filt = _CorrelationFilter()

    with correlation.batch_context("17-07-2026|MCX"):
        assert filt.filter(record) is True
        assert record.corr == correlation.label()
        assert "17-07-2026|MCX" in record.corr

    filt.filter(record)
    assert record.corr == correlation.NO_CONTEXT
