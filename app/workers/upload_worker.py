import logging

from app.core.queue import BatchQueue
from app.services import upload_service

logger = logging.getLogger("upload_worker")


def run(queue: BatchQueue) -> None:
    """Runs forever in a dedicated background thread, processing one queued
    batch at a time (see app/main.py's lifespan for how it's started).

    This function's only job is to consume queue items sequentially - all
    upload/move/database logic lives in upload_service.process_batch.
    """
    logger.info("Queue worker started")
    while True:
        task = queue.get()
        logger.debug("Worker picked up batch: %s (queue size now %d)", task.key, queue.size)
        try:
            upload_service.process_batch(task)
        except Exception:
            logger.exception("Unexpected error processing batch %s", task.key)
        finally:
            queue.task_done()
            queue.release(task.key)
            logger.debug("Worker finished batch: %s", task.key)
