import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import init_db
from app.core.logging import configure_logging
from app.core.queue import BatchQueue
from app.workers.upload_worker import run as run_worker

logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Configure logging at startup (not at import) so importing app.main never
    # instantiates Settings - keeps the module import-safe and test-friendly.
    configure_logging()

    # Server Start -> Initialize DB -> Start Queue Worker. Work arrives ONLY
    # via POST /batches (a manifest per docs/BATCH_HANDOFF_CONTRACT.md) - the
    # old filesystem scanner is gone; POST /batches/rescan is the manual
    # catch-up path.
    logger.info("Startup: step 1/2 - initializing database")
    init_db()
    logger.info("Startup: database ready")

    # One queue for the process, handed to everything that touches it - the
    # worker and the API endpoints via app.state.
    batch_queue = BatchQueue(maxsize=settings.batch_queue_maxsize)
    app.state.batch_queue = batch_queue

    logger.info("Startup: step 2/2 - starting queue worker thread")
    worker_thread = threading.Thread(
        target=run_worker, args=(batch_queue,), name="cbos-upload-worker", daemon=True
    )
    worker_thread.start()
    logger.info("Startup: queue worker thread started (name=%s)", worker_thread.name)

    logger.info("Startup complete - awaiting batches (POST /batches)")
    yield

    # Signal the worker and wait for it, rather than just logging and walking away.
    # Harmless at real process exit — the thread is a daemon — but any interpreter that
    # runs the lifespan more than once leaked a blocked thread per cycle, and a test
    # suite using `with TestClient(app):` repeatedly does exactly that.
    #
    # A sentinel, not a flag: the worker blocks in queue.get(), so a flag would only be
    # noticed after the next batch arrived, which on a quiet queue is never.
    logger.info("Shutdown: signalling the queue worker")
    batch_queue.request_shutdown()
    worker_thread.join(timeout=settings.worker_shutdown_grace_seconds)
    if worker_thread.is_alive():
        # Mid-batch, and a batch can legitimately take minutes. Daemon=True means the
        # process still exits; say so rather than pretending it drained.
        logger.warning(
            "Queue worker still running after %.0fs — exiting anyway; a batch was in flight.",
            settings.worker_shutdown_grace_seconds,
        )
    else:
        logger.info("Queue worker stopped cleanly")

    logger.info("Shutdown complete")


app = FastAPI(title="File Uploader", lifespan=lifespan)
app.include_router(api_router)
