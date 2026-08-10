import logging
from datetime import date

import requests
from fastapi import APIRouter, Request

from app.clients.cbos_client import FILE_PROCESS_STATUS_PATH, GET_EXISTING_PROCESS_ID_PATH
from app.core.config import settings

logger = logging.getLogger("system_endpoint")
router = APIRouter(tags=["system"])

_HEALTH_CHECK_SEGMENT = "MCX"


def _cbos_ping(url: str, payload: dict) -> dict:
    try:
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
        return {"url":url, "status": "up","request":payload, "response": response.text}
    except requests.RequestException as exc:
        logger.warning("CBOS health check failed for %s: %s", url, exc)
        return {"status": "down", "error": str(exc)}


@router.get("/health")
def health():
    logger.debug("GET /health")
    today = date.today().isoformat()
    cbos = {
        "cbos_gtg": _cbos_ping(
            f"{settings.cbos_gtg_base_url.rstrip('/')}{FILE_PROCESS_STATUS_PATH}",
            {
                "Segment": _HEALTH_CHECK_SEGMENT,
                "ProcessName": "BeginFileUpload",
                "TRADEDATE": today,
                "UserID": settings.cbos_login_id,
            },
        ),
        "cbos_core": _cbos_ping(
            f"{settings.cbos_core_base_url.rstrip('/')}{GET_EXISTING_PROCESS_ID_PATH}",
            {
                "TAG": "EXISTINGPROCESSID",
                "LOGINID": settings.cbos_login_id,
                "FILTER1": _HEALTH_CHECK_SEGMENT,
                "FILTER2": today,
                "extraoption2": "",
                "extraoption3": "",
            },
        ),
    }
    status = "ok" if all(v["status"] == "up" for v in cbos.values()) else "degraded"
    return {"status": status, "cbos": cbos}


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
