"""The one source of "now" for persisted timestamps.

Every model used to answer this question for itself, and they disagreed: `batch.py`
stamped `datetime.now(UTC)` (timezone-aware) while `settlement_upload.py` and
`uploaded_file.py` stamped `datetime.utcnow()` (naive). Same conceptual field, two
shapes — `GET /batches/{id}` returned `...+00:00` where `GET /settlements/uploads/{id}`
returned a bare local-looking string for the same instant, and any code that compared
one to the other raised `TypeError: can't subtract offset-naive and offset-aware
datetimes`.

A shared function is the fix rather than three corrected copies: copies drift, and this
one already had.

NOTE on storage: the columns are plain `DateTime`, and SQLAlchemy's SQLite dialect drops
tzinfo when it writes. So the value persisted is the correct UTC wall-clock time, but it
reads back naive. What this module guarantees is that every timestamp is *produced* in
UTC by the same call — not that the database round-trips the offset. Making the columns
`DateTime(timezone=True)` is a schema change and a separate decision.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Timezone-aware current time in UTC. Use for every persisted timestamp."""
    return datetime.now(UTC)
