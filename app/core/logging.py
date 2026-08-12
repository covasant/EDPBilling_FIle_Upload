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

    root = logging.getLogger()

    # basicConfig() only installs a handler when the root has none — documented, and
    # the trap here: on a SECOND call it is a silent no-op, so the new level was
    # discarded and only the filter re-attachment below still took effect. Anything that
    # configured root logging first (uvicorn, pytest, a library, a re-init) left this
    # stuck on whatever level happened to win the race, with nothing logged to say so.
    # Install the handler once ourselves, then set the level UNCONDITIONALLY, so
    # configure_logging() means the same thing every time it is called.
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s [%(corr)s] %(message)s")
        )
        root.addHandler(handler)

    root.setLevel(level)
    for handler in root.handlers:
        handler.setLevel(logging.NOTSET)  # let the root level decide, not a stale one
        if not any(isinstance(f, _CorrelationFilter) for f in handler.filters):
            # Every handler needs it, including ones we did not install: the format
            # string references %(corr)s, and a record reaching a handler without the
            # filter raises rather than logging.
            handler.addFilter(_CorrelationFilter())
