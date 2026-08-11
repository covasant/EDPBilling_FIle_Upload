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
        except Exception as exc:
            logger.exception("Unexpected error processing batch %s", task.key)
            # Record the crash on the batch row before moving on. The blanket except
            # keeps the WORKER alive, which is right, but it used to leave the BATCH at
            # whatever status it happened to hold when the exception fired — usually
            # UPLOADING. A failed batch was then indistinguishable through the API from
            # one still in progress, and discoverable only by reading worker logs.
            #
            # Best-effort by design: if the status write itself fails (the DB is what
            # crashed the batch, say) that must not take the worker down with it. The
            # log line above is still the record of what happened.
            try:
                upload_service.mark_batch_failed(task, exc)
            except Exception:
                logger.exception("Could not record FAILED status for batch %s", task.key)
        finally:
            queue.task_done()
            queue.release(task.key)
            logger.debug("Worker finished batch: %s", task.key)
