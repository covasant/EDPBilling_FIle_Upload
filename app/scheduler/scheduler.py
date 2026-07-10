import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import get_settings
from app.services.upload_service import discover_and_enqueue

logger = logging.getLogger("scheduler")
settings = get_settings()

scheduler = BackgroundScheduler()

def _scan_job() -> None:
    """The scheduler's only responsibility: trigger a discovery scan. All
    discovery/enqueue logic lives in upload_service.discover_and_enqueue -
    the scheduler never uploads or touches the database directly."""
    logger.info("Running scheduled file discovery job")
    try:
        discover_and_enqueue()
    except Exception:
        logger.exception("Scheduled file discovery job failed")
        raise
    logger.debug("Scheduled file discovery job finished")


def start_scheduler() -> BackgroundScheduler:
    scheduler.add_job(
        _scan_job,
        "interval",
        minutes=settings.poll_interval_minutes,
        id="file_upload_scan",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Scheduler started, running every %s minute(s)", settings.poll_interval_minutes)
    return scheduler


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
