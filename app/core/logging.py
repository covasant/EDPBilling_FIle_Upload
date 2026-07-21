import logging

from app.core import correlation


class _CorrelationFilter(logging.Filter):
    """Attach the in-force correlation id to every record.

    A filter rather than a custom Formatter because it applies to records from
    modules that know nothing about batches - uvicorn, requests, SQLAlchemy - so
    a stray third-party log line can't crash the formatter on a missing field.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.corr = correlation.label()
        return True


def configure_logging(level: int | str | None = None) -> None:
    """Structured, consistent log format across every module - scheduler,
    worker, clients, API routes all share this formatter.

    Level defaults to Settings.log_level (env var LOG_LEVEL), so verbose
    per-step debug logging can be turned on without a code change:
    LOG_LEVEL=DEBUG uvicorn app.main:app --reload

    At INFO you get the CBOS step narrative (one REQUEST + one RESPONSE line per
    step). At DEBUG you additionally get the literal wire traffic - full URL,
    HTTP status, raw body - plus a line per upload chunk.
    """
    if level is None:
        from app.core.config import get_settings

        level = get_settings().log_level

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s [%(corr)s] %(message)s",
    )

    # basicConfig is a no-op if handlers already exist (repeated calls, pytest,
    # uvicorn's own setup), so attach the filter to whatever handlers are on the
    # root - otherwise a second call would leave %(corr)s unpopulated.
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _CorrelationFilter) for f in handler.filters):
            handler.addFilter(_CorrelationFilter())
