"""One-time migration: add `file_version` to nsdl_speede_uploads and widen its
unique constraint from (trade_date, account, report) to
(trade_date, account, report, file_version).

Why this exists: the download bot now writes a numbered file per trigger
("NSDL <code> <label> <n>.csv", never overwriting - see the download repo's
src/portals/nsdl_speede/reports.py) instead of one stable name per day. The
upload side now tracks "already uploaded" per exact file version instead of
once per (trade_date, account, report), so a later trigger that produced a
NEW numbered file still gets uploaded even though an earlier version of the
same report already succeeded today. See NsdlSpeedeUpload's docstring
(app/models/nsdl_speede_upload.py) for the full rationale.

SQLite can't ALTER a table's constraints in place (both UAT and prod run on
SQLite here, not Postgres), so this rebuilds the table: rename the existing
one aside, let the (already-updated) ORM model create the new shape, copy
every row across, drop the old one. Every existing tran_id/status/history is
carried over untouched - only `file_version` is new, and it's NULL for rows
that predate it (nothing needs to match against them - see the model's
docstring on why that's safe).

Idempotent - safe to run more than once, and safe to run against a database
that has never been started yet:
  - If nsdl_speede_uploads doesn't exist at all, there's nothing to migrate -
    the next app startup's create_all() will make it in the new shape.
  - If it exists and already has file_version, this exits immediately.
  - If a previous run crashed after the rename but before the drop, the old
    data is sitting safely under the "_pre_file_version" name - this picks
    that up and finishes the copy rather than duplicating work.

Run ONCE per environment (UAT, prod - they're separate SQLite files, each
needs its own run), from that environment's own checkout/`.env`, BEFORE
starting the app there - the updated service code expects the column to
already exist:

    uv run python -m scripts.migrate_nsdl_speede_file_version

or, on the plain-venv path:

    .venv\\Scripts\\Activate.ps1
    python -m scripts.migrate_nsdl_speede_file_version
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from app.core.database import get_engine
from app.models.nsdl_speede_upload import NsdlSpeedeUpload

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_nsdl_speede_file_version")

TABLE = "nsdl_speede_uploads"
OLD_TABLE = f"{TABLE}_pre_file_version"

# The columns the table had BEFORE this migration - i.e. every column on the
# now-updated model except the one this migration adds. Deriving it from the
# model instead of hand-listing it means it can never drift from what
# NsdlSpeedeUpload actually declares elsewhere.
_OLD_COLUMNS = [c.name for c in NsdlSpeedeUpload.__table__.columns if c.name != "file_version"]


def main() -> None:
    engine = get_engine()
    logger.info("Target DB: %s", engine.url)

    inspector = inspect(engine)
    if TABLE not in inspector.get_table_names():
        logger.info(
            "'%s' does not exist yet - nothing to migrate; the next app startup's "
            "create_all() will create it already in the new shape.",
            TABLE,
        )
        return

    columns = {c["name"] for c in inspector.get_columns(TABLE)}
    if "file_version" in columns:
        logger.info("'%s' already has file_version - already migrated, nothing to do.", TABLE)
        return

    with engine.begin() as conn:
        if inspect(conn).has_table(OLD_TABLE):
            # A previous run got as far as the rename but crashed before the
            # drop at the end - the original data is safe under OLD_TABLE.
            # Drop whatever half-finished new table exists (if any) and redo
            # the copy from the preserved original.
            logger.warning(
                "Found leftover '%s' from an interrupted run - resuming from it.", OLD_TABLE
            )
            conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
        else:
            conn.execute(text(f"ALTER TABLE {TABLE} RENAME TO {OLD_TABLE}"))

        # Single source of truth for the target shape - no hand-duplicated DDL.
        NsdlSpeedeUpload.__table__.create(conn)

        select_cols = ", ".join(_OLD_COLUMNS)
        conn.execute(
            text(
                f"INSERT INTO {TABLE} ({select_cols}, file_version) "
                f"SELECT {select_cols}, NULL FROM {OLD_TABLE}"
            )
        )
        row_count = conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()
        conn.execute(text(f"DROP TABLE {OLD_TABLE}"))

    logger.info(
        "Migration complete: %d row(s) carried over into the new '%s' "
        "(file_version=NULL for all of them - they predate versioning).",
        row_count,
        TABLE,
    )


if __name__ == "__main__":
    main()
