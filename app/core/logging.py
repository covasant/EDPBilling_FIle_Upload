import logging


def configure_logging(level: int | str | None = None) -> None:
    """Structured, consistent log format across every module - scheduler,
    worker, clients, API routes all share this formatter.

    Level defaults to Settings.log_level (env var LOG_LEVEL), so verbose
    per-step debug logging can be turned on without a code change:
    LOG_LEVEL=DEBUG uvicorn app.main:app --reload
    """
    if level is None:
        from app.core.config import get_settings

        level = get_settings().log_level

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
