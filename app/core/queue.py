import logging
import threading
from dataclasses import dataclass, field
from queue import Queue
from typing import Literal

logger = logging.getLogger("upload_queue")


@dataclass
class SegmentBatchTask:
    """One CBOS batch = one segment + one trade date. Every file for that
    segment on that date - across ALL its exchange sub-folders - is one unit,
    because CBOS reserves exactly ONE PROCESSID per segment/date and EDP_Billing
    reads it back per segment/date via getdropdown (see CONTEXT.md). Slicing by
    exchange would reserve two PIDs for e.g. EQ's BSE + NSE folders and half the
    files would never trigger.

    Each file keeps its own exchange (from the manifest's per-file metadata -
    there is no exchange folder level) for matching (upload_matching
    tie-breaks by exchange) and audit - exchange is per-file metadata, not a
    partition key.

    batch_id/correlation_id tie the task back to its manifest and Batch row
    (docs/BATCH_HANDOFF_CONTRACT.md). mode="proceed" is the audited
    force-proceed path for an INCOMPLETE batch: no files to upload - just
    mark the named slots optional and re-confirm (see
    upload_service.proceed_batch)."""
    folder_date: str
    segment: str
    files: list[tuple[str, str]] = field(default_factory=list)  # (file_path, exchange)
    batch_id: str | None = None
    correlation_id: str | None = None
    mode: Literal["upload", "proceed"] = "upload"
    proceed_slots: list[str] = field(default_factory=list)      # UploadIDs ops chose (mode="proceed")
    proceed_reason: str | None = None

    @property
    def key(self) -> str:
        """In-flight guard key. Includes mode and batch_id so it only dedups
        EXACT resubmissions of one batch: a SUPERSEDING manifest for the same
        segment/date has a fresh batch_id and must never be swallowed while
        the old batch is queued/in flight (it would be stranded forever -
        rescan skips known batch_ids). Per-segment/date serialization does
        not depend on this key: the single worker thread processes one batch
        at a time globally."""
        return f"{self.folder_date}|{self.segment}|{self.mode}|{self.batch_id or 'scan'}"


class BatchQueue:
    """Batches waiting to be uploaded, plus the in-flight guard that stops the
    same batch being queued twice while it is already queued or being
    processed.

    One instance is created at startup (app/main.py) and handed to the worker,
    the scheduler and the system endpoints. Deliberately not a module global:
    ADR 3 removed the other process-wide singletons so tests can supply their
    own, and this was the last of them. It is also what lets ADR 10's bounded
    worker pool hand every worker the same queue explicitly.

    Thread-safe: the guard is held under a lock, and the underlying Queue is
    itself thread-safe.
    """

    def __init__(self) -> None:
        self._queue: Queue[SegmentBatchTask] = Queue()
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()

    def enqueue(self, task: SegmentBatchTask) -> bool:
        """Add a batch unless that segment/date is already queued or in flight.
        Returns True if it was added."""
        with self._lock:
            if task.key in self._in_flight:
                return False
            self._in_flight.add(task.key)

        self._queue.put(task)
        logger.info("Added to queue: %s (%d file(s))", task.key, len(task.files))
        logger.info("Queue size: %d", self._queue.qsize())
        return True

    def is_queued(self, batch_key: str) -> bool:
        with self._lock:
            return batch_key in self._in_flight

    def get(self) -> SegmentBatchTask:
        """Block until a batch is available, then hand it over."""
        return self._queue.get()

    def task_done(self) -> None:
        """Mark the batch just handed out by get() as finished."""
        self._queue.task_done()

    def release(self, batch_key: str) -> None:
        """Drop the in-flight guard once a batch has finished processing
        (success or failure), so it can be discovered again."""
        with self._lock:
            self._in_flight.discard(batch_key)

    def empty(self) -> bool:
        return self._queue.empty()

    @property
    def size(self) -> int:
        """Batches still waiting. Drops to 0 as soon as a worker dequeues the
        last one, which may still be mid-flight - see `unfinished`."""
        return self._queue.qsize()

    @property
    def unfinished(self) -> int:
        """Batches dequeued but not yet marked done. The correct "is everything
        truly finished" signal, since it only drops once a worker calls
        task_done()."""
        return self._queue.unfinished_tasks
