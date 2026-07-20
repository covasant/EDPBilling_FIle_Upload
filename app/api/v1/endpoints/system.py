import logging

from fastapi import APIRouter, Request

from app.services.upload_service import discover_and_enqueue

logger = logging.getLogger("system_endpoint")
router = APIRouter(tags=["system"])


@router.get("/health")
def health():
    logger.debug("GET /health")
    return {"status": "ok"}


@router.post("/run-now")
def run_now(request: Request):
    logger.info("POST /run-now: manual discovery scan triggered")
    discover_and_enqueue(request.app.state.batch_queue)
    logger.info("POST /run-now: discovery scan complete")
    return {"status": "triggered"}


@router.get("/queue-status")
def queue_status(request: Request):
    """Lets external tooling (tests, monitoring) observe queue depth without
    reaching into process internals.

    queue_size (qsize) drops to 0 as soon as a worker dequeues the last item -
    while it may still be mid-flight (network calls, file move, DB commit).
    unfinished_tasks only drops once the worker calls task_done(), so it's the
    correct "is everything truly done" signal for callers like the test
    harness that need to know processing has actually finished.
    """
    queue = request.app.state.batch_queue
    status = {
        "queue_size": queue.size,
        "unfinished_tasks": queue.unfinished,
    }
    logger.debug("GET /queue-status: %s", status)
    return status
