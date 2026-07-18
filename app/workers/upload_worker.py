import logging

from app.core.queue import file_queue, release
from app.services import upload_service

logger = logging.getLogger("upload_worker")


def run() -> None:
    """Runs forever in a dedicated background thread, processing one queued
    segment/date/exchange batch at a time (see app/main.py's lifespan for
    how it's started).

    This function's only job is to consume queue items sequentially - all
    upload/move/database logic lives in upload_service.process_batch.
    """
    logger.info("Queue worker started")
    while True:
        task = file_queue.get()
        logger.debug("Worker picked up batch: %s (queue size now %d)", task.key, file_queue.qsize())
        try:
            upload_service.process_batch(task)
        except Exception:
            logger.exception("Unexpected error processing batch %s", task.key)
        finally:
            file_queue.task_done()
            release(task.key)
            logger.debug("Worker finished batch: %s", task.key)
