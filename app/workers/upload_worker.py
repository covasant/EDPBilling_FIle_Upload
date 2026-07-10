import logging

from app.core.queue import file_queue, release
from app.services import upload_service

logger = logging.getLogger("upload_worker")


def run() -> None:
    """Runs forever in a dedicated background thread, processing one queued
    file at a time (see app/main.py's lifespan for how it's started).

    This function's only job is to consume queue items sequentially - all
    upload/move/database logic lives in upload_service.process_task.
    """
    logger.info("Queue worker started")
    while True:
        task = file_queue.get()
        logger.debug("Worker picked up task: %s (queue size now %d)", task.file_path, file_queue.qsize())
        try:
            upload_service.process_task(task)
        except Exception:
            logger.exception("Unexpected error processing %s", task.file_path)
        finally:
            file_queue.task_done()
            release(task.file_path)
            logger.debug("Worker finished task: %s", task.file_path)
