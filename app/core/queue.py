import logging
import threading
from dataclasses import dataclass, field
from queue import Queue

logger = logging.getLogger("upload_queue")


@dataclass
class SegmentBatchTask:
    """One CBOS batch = one segment + one trade date + one exchange folder.
    Holiday check (Step 1), process-ID creation (Step 2), upload-rule
    resolution (Step 4), and the trigger (Step 8) all happen ONCE per batch
    - never once per file - so every file discovered together in the same
    {date}/{segment}/{exchange}/ folder is processed as a single unit."""
    folder_date: str
    segment: str
    exchange: str
    file_paths: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.folder_date}|{self.segment}|{self.exchange}"


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
    logger.info("Added to queue: %s (%d file(s))", task.key, len(task.file_paths))
    logger.info("Queue size: %d", file_queue.qsize())
    return True


def release(batch_key: str) -> None:
    """Call once a queued batch has finished processing (success or failure)."""
    with _lock:
        queued_batches.discard(batch_key)
