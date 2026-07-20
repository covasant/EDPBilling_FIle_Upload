import logging
import threading
from dataclasses import dataclass, field
from queue import Queue

logger = logging.getLogger("upload_queue")


@dataclass
class SegmentBatchTask:
    """One CBOS batch = one segment + one trade date. Every file for that
    segment on that date - across ALL its exchange sub-folders - is one unit,
    because CBOS reserves exactly ONE PROCESSID per segment/date and EDP_Billing
    reads it back per segment/date via getdropdown (see
    docs/CBOS_HANDOFF_CONTRACT.md). Slicing by exchange would reserve two PIDs
    for e.g. EQ's BSE + NSE folders and half the files would never trigger.

    Each file keeps its own exchange (the sub-folder it came from) for matching
    (upload_matching tie-breaks by exchange) and audit - exchange is per-file
    metadata, not a partition key."""
    folder_date: str
    segment: str
    files: list[tuple[str, str]] = field(default_factory=list)  # (file_path, exchange)

    @property
    def key(self) -> str:
        return f"{self.folder_date}|{self.segment}"


file_queue: "Queue[SegmentBatchTask]" = Queue()

# In-memory guard against enqueuing the same segment/date/exchange batch
# twice while it's already queued or being processed. Cleared once the
# worker finishes with a batch.
queued_batches: set[str] = set()
_lock = threading.Lock()


def is_queued(batch_key: str) -> bool:
    with _lock:
        return batch_key in queued_batches


def enqueue(task: SegmentBatchTask) -> bool:
    """Add a batch to the queue unless that segment/date/exchange is
    already queued or in flight. Returns True if added."""
    with _lock:
        if task.key in queued_batches:
            return False
        queued_batches.add(task.key)

    file_queue.put(task)
    logger.info("Added to queue: %s (%d file(s))", task.key, len(task.files))
    logger.info("Queue size: %d", file_queue.qsize())
    return True


def release(batch_key: str) -> None:
    """Call once a queued batch has finished processing (success or failure)."""
    with _lock:
        queued_batches.discard(batch_key)
