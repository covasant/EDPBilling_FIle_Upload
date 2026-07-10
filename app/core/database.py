import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import get_settings

logger = logging.getLogger("database")
settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def init_db() -> None:
    from app.models import uploaded_file  # noqa: F401 - registers models before create_all

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
        }
        for column_name, column_type in missing_columns.items():
            if column_name in columns:
                logger.debug("init_db: '%s' column already present", column_name)
                continue
            logger.info("init_db: '%s' column missing on uploaded_files, adding it", column_name)
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE uploaded_files ADD COLUMN {column_name} {column_type};"))
            logger.info("init_db: '%s' column added", column_name)


def get_db_session():
    """FastAPI dependency: yields a request-scoped session, closed afterwards."""
    db = SessionLocal()
    logger.debug("DB session opened")
    try:
        yield db
    finally:
        db.close()
        logger.debug("DB session closed")
