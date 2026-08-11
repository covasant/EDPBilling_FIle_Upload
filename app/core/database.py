import logging
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import get_settings

logger = logging.getLogger("database")

Base = declarative_base()


@lru_cache
def get_engine() -> Engine:
    """The SQLAlchemy engine, built lazily on first use (not at import time),
    so importing this module never requires DATABASE_URL. Cached, so it's still
    a single engine per process. Tests reset it with reset_engine()."""
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def reset_engine() -> None:
    """Drop the cached engine, disposing its connection pool first.

    Clearing the lru_cache on its own only drops the reference: the Engine's pool keeps
    its connections open until the GC happens to collect it, which is the leak
    SQLAlchemy's docs warn about. The test suite clears this cache around every test, so
    "eventually" meant hundreds of open handles across a run. Call this instead of
    get_engine.cache_clear().
    """
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@lru_cache
def get_sessionmaker() -> sessionmaker:
    return sessionmaker(bind=get_engine(), autocommit=False, autoflush=False)


def _is_duplicate_column(exc: Exception) -> bool:
    """True when a failed ALTER means "the column is already there".

    Matched on message text because each backend words it differently and none of them
    map it to a distinct DBAPI class: SQLite says "duplicate column name", PostgreSQL
    "column ... already exists", MySQL "Duplicate column name". Deliberately narrow — a
    message we do not recognise re-raises rather than being swallowed as benign.

    NOTE: this is a guard, not a migration system. It exists because these ALTERs are
    hand-rolled; the real fix is Alembic, which would make the whole block above
    unnecessary and give the schema an ordered, reviewable history.
    """
    return "duplicate column" in str(exc).lower() or "already exists" in str(exc).lower()


def init_db() -> None:
    # Imported for their side effect: registering the tables on Base.metadata
    # so create_all below sees them.
    from app.models import batch, settlement_upload, uploaded_file  # noqa: F401

    engine = get_engine()
    logger.debug("init_db: creating tables (create_all) against %s", engine.url)
    Base.metadata.create_all(bind=engine)
    logger.debug("init_db: create_all complete")

    # Automatically add columns that are missing (for existing databases created
    # before these fields existed). create_all() never ALTERs existing tables,
    # so newly added model columns need to be patched in by hand here.
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "uploaded_files" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("uploaded_files")}
        missing_columns = {
            "exchange": "VARCHAR",
            "process_id": "VARCHAR",
            "guid": "VARCHAR",
            "request_log": "TEXT",
            "matched_pattern": "VARCHAR",
            "validation_error": "TEXT",
            "cbos_upload_settings": "TEXT",
            "correlation_id": "VARCHAR",
            "batch_id": "VARCHAR",
        }
        for column_name, column_type in missing_columns.items():
            if column_name in columns:
                logger.debug("init_db: '%s' column already present", column_name)
                continue
            logger.info("init_db: '%s' column missing on uploaded_files, adding it", column_name)
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(f"ALTER TABLE uploaded_files ADD COLUMN {column_name} {column_type};")
                    )
            except (OperationalError, ProgrammingError) as exc:
                # `columns` was snapshotted once, before this loop. Two processes starting
                # together — `uvicorn --workers N`, or two pods mid-rolling-deploy — both
                # see the column missing and both issue the ALTER; the loser used to die
                # at startup on a duplicate-column error. Losing that race is the correct
                # outcome, not a failure: the column exists either way, which is all this
                # block wanted. Anything else still raises.
                if not _is_duplicate_column(exc):
                    raise
                logger.info("init_db: '%s' was added concurrently by another process", column_name)
                continue
            logger.info("init_db: '%s' column added", column_name)


def get_db_session():
    """FastAPI dependency: yields a request-scoped session, closed afterwards."""
    db = get_sessionmaker()()
    logger.debug("DB session opened")
    try:
        yield db
    finally:
        db.close()
        logger.debug("DB session closed")
