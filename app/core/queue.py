import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from queue import Queue

logger = logging.getLogger("upload_queue")


@dataclass
class FileTask:
    file_path: str
    folder_date: str
    segment: str
    exchange: str


file_queue: "Queue[FileTask]" = Queue()

# In-memory guard against enqueuing the same file twice while it's already
# queued or being processed. Cleared once the worker finishes with a file.
queued_files: set[str] = set()
_lock = threading.Lock()


def is_queued(file_path: str) -> bool:
    with _lock:
        return file_path in queued_files


def enqueue(task: FileTask) -> bool:
    """Add a task to the queue unless it's already queued. Returns True if added."""
    with _lock:
        if task.file_path in queued_files:
            return False
        queued_files.add(task.file_path)

    file_queue.put(task)
    logger.info("Added to queue: %s", Path(task.file_path).name)
    logger.info("Queue size: %d", file_queue.qsize())
    return True


def release(file_path: str) -> None:
    """Call once a queued file has finished processing (success or failure)."""
    with _lock:
        queued_files.discard(file_path)
