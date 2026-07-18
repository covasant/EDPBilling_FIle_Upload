import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.database import init_db
from app.core.logging import configure_logging
from app.scheduler.scheduler import start_scheduler, stop_scheduler
from app.workers.upload_worker import run as run_worker

configure_logging()

logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    
    # Server Start -> Initialize DB -> Start Queue Worker -> Start Scheduler
    # -> Begin File Processing. No manual startup step is required for
    # either the scheduler or the worker.
    logger.info("Startup: step 1/3 - initializing database")
    init_db()
    logger.info("Startup: database ready")

    logger.info("Startup: step 2/3 - starting queue worker thread")
    worker_thread = threading.Thread(target=run_worker, name="cbos-upload-worker", daemon=True)
    worker_thread.start()
    logger.info("Startup: queue worker thread started (name=%s)", worker_thread.name)

    logger.info("Startup: step 3/3 - starting scheduler")
    start_scheduler()

    logger.info("Startup complete - ready to process files")
    yield

    logger.info("Shutdown: stopping scheduler")
    stop_scheduler()
    logger.info("Shutdown complete")


app = FastAPI(title="File Uploader", lifespan=lifespan)
app.include_router(api_router)
