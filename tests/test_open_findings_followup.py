"""The second pass of EDPB findings: M5, M9, M10, L1, L7.

Each pins a behaviour that failed quietly rather than loudly — a level silently
discarded, an audit line that overstated what happened, an empty file waved through, a
leaked thread, a bug filed as an upstream failure.
"""

from __future__ import annotations

import logging
import threading

import pytest

# ── M5: configure_logging must mean the same thing every time ────────────────


def test_configure_logging_applies_the_level_on_a_second_call():
    """basicConfig() is a documented no-op once the root has handlers, so the second
    call silently discarded the new level — leaving the process stuck on whatever
    uvicorn, pytest or a library happened to set first, with nothing logged to say so."""
    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging(level="INFO")
        assert root.level == logging.INFO

        configure_logging(level="DEBUG")  # would have been a no-op before
        assert root.level == logging.DEBUG, "the second call's level was discarded"

        configure_logging(level="WARNING")
        assert root.level == logging.WARNING
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_every_handler_can_format_a_record_after_repeated_configuration():
    """The format string references %(corr)s, so a handler without the correlation
    filter raises instead of logging. Re-configuring must not leave one behind."""
    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging(level="INFO")
        configure_logging(level="DEBUG")
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        for handler in root.handlers:
            for f in handler.filters:
                f.filter(record)
            handler.format(record)  # raises on a missing %(corr)s
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


# ── M9: the force-proceed audit must describe what actually happened ─────────


def test_force_proceed_does_not_claim_files_were_uploaded():
    """`files_in_cbos` used to carry len(requested) — the count of ops-named slots being
    force-proceeded — under the basis text "all manifest files uploaded + registered".
    So proceeding past 2 unfilled slots reported 2 files in CBOS. Zero files move here,
    and this is the audit trail for an action a human deliberately took."""
    from app.services.upload_service import _batch_proceed_complete

    detail = _batch_proceed_complete(via="force-proceed", proceed_slots=["127", "534"])

    assert detail["files_in_cbos"] == 0
    assert "no files were uploaded" in detail["basis"]
    assert detail["proceed_slots"] == ["127", "534"]


# ── M10: an empty file must fail safe, not sail through ──────────────────────


def test_an_empty_file_is_rejected_rather_than_skipping_validation(tmp_path, monkeypatch):
    """_count_columns returned None both for "unreadable binary" and for "no data
    lines", and the caller skipped the check on None — so a zero-byte placeholder took
    the .xlsx exemption and was forwarded to CBOS."""
    monkeypatch.setenv("FILE_ROOT_PATH", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.services import upload_matching

    empty = tmp_path / "empty.csv"
    empty.write_text("")
    blank = tmp_path / "blank.csv"
    blank.write_text("\n\n   \n")

    # Read fine, nothing in it — as distinct from None, which means not sniffable.
    # Every candidate delimiter maps to an empty width list; the caller treats that
    # (not None) as EmptyFile. Asserted per-delimiter rather than against a literal
    # so adding a delimiter cannot quietly turn "empty" back into "unsniffable".
    for path in (empty, blank):
        sniffed = upload_matching._count_columns(path)
        assert sniffed is not None, f"{path.name} is readable text, not a binary exemption"
        assert not any(sniffed.values()), f"{path.name} has no data line to validate"


def test_an_unsniffable_file_still_skips_the_check(tmp_path, monkeypatch):
    """The other half: None must keep meaning "cannot be read as delimited text", so a
    binary format is still exempt rather than newly rejected."""
    monkeypatch.setenv("FILE_ROOT_PATH", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.services import upload_matching

    binary = tmp_path / "book.xlsx"
    binary.write_bytes(b"PK\x03\x04\xff\xfe\x00\x01binary-nonsense")
    assert upload_matching._count_columns(binary) is None


def test_empty_file_is_a_column_count_mismatch_subclass():
    """Existing `except ColumnCountMismatch` handlers must keep catching it — it is the
    same class of local rejection, just a more specific reason."""
    from app.services.upload_matching import ColumnCountMismatch, EmptyFile

    assert issubclass(EmptyFile, ColumnCountMismatch)


# ── L1: the worker thread must be signalled and joined ───────────────────────


def test_the_worker_stops_on_the_shutdown_sentinel():
    """The worker blocks in queue.get(), so a flag would only be noticed after the next
    batch arrived — on a quiet queue, never. A sentinel wakes it immediately."""
    from app.core.queue import BatchQueue
    from app.workers import upload_worker

    queue = BatchQueue()
    # "not alive" is not enough: a worker that CRASHED on the sentinel is also not
    # alive, and that is a different (worse) outcome. Record a clean return instead.
    returned: list[bool] = []

    def _run() -> None:
        upload_worker.run(queue)
        returned.append(True)  # only reached if run() returns rather than raising

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    queue.request_shutdown()
    thread.join(timeout=5)

    assert not thread.is_alive(), "the worker ignored the shutdown signal"
    assert returned == [True], "the worker exited by crashing on the sentinel, not cleanly"


def test_repeated_lifespans_do_not_leak_a_worker_thread(monkeypatch, tmp_path):
    """The actual reported symptom: any interpreter running the lifespan more than once
    — a test suite using `with TestClient(app):` repeatedly — leaked one blocked thread
    per cycle."""
    monkeypatch.setenv("FILE_ROOT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import app

    def worker_threads() -> int:
        return sum(
            1 for t in threading.enumerate() if t.name == "cbos-upload-worker" and t.is_alive()
        )

    before = worker_threads()
    for _ in range(3):
        with TestClient(app):
            pass
    assert worker_threads() <= before, "a worker thread was left running per lifespan"


# ── L7: a programming error must not be filed as a CBOS failure ──────────────


def test_a_programming_error_propagates_instead_of_being_filed_as_a_cbos_failure(
    monkeypatch, tmp_path
):
    """Steps 5 and 7 caught bare Exception, so an AttributeError in our own code was
    written into the per-file audit trail identically to a genuine CBOS failure — it
    read as an upstream problem and nobody went looking. It must now escape
    _process_batch, where the worker logs the traceback and marks the batch FAILED."""
    monkeypatch.setenv("CBOS_MODE", "MOCK")
    monkeypatch.setenv("CBOS_MOCK_RANDOM_SUCCESS_RATE", "1.0")
    monkeypatch.setenv("CBOS_MOCK_PENDING_POLLS", "0")
    monkeypatch.setenv("CBOS_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("CBOS_RETRY_DELAY_SECONDS", "0")
    from app.core.config import get_settings, settings

    get_settings.cache_clear()

    from pathlib import Path

    from app.clients import cbos_client
    from app.clients.cbos_client import MockCBOSClient
    from app.core import database
    from app.core.queue import SegmentBatchTask
    from app.services import upload_service

    class _BuggyClient(MockCBOSClient):
        def register_file(self, *a, **k):
            raise AttributeError("simulated bug in our own code, not a CBOS failure")

    database.init_db()
    cbos_client.set_cbos_client(_BuggyClient())

    folder = Path(settings.file_root_path) / "24-07-2026" / "MCX" / "NA"
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "Position_MCXCCL_CO_0_CM_55930_20260724_F_0000.csv"
    f.write_text(",".join(str(i) for i in range(46)))

    task = SegmentBatchTask(folder_date="24-07-2026", segment="MCX", files=[(str(f), "NA")])

    with pytest.raises(AttributeError):
        upload_service.process_batch(task)
