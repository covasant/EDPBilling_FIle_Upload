"""A per-run id stamped on every log line, so one batch run can be pulled out
of a day's log with a single grep.

The batch key alone can't do this: it is "17-07-2026|MCX", and the scheduler
rescans every POLL_INTERVAL_SECONDS, so the same key appears in the log dozens
of times a day. Grepping it returns every run of that segment smeared together.
The id separates run 1 from run 7.

PROCESSID would be the natural key - CBOS mints a fresh one per run - but it
doesn't exist until Step 2 returns, and if Step 2 is what failed there is never
one at all. That is exactly the run you most need to trace, hence an id minted
locally at batch start.

A ContextVar rather than a parameter because the id has to reach log calls in
modules that have no business knowing about batches - cbos_client's HTTP layer,
the repository - and threading it through eight signatures to satisfy a log
format would put logging concerns into the interface.

Since the manifest handoff (BATCH_HANDOFF_CONTRACT.md), a batch usually
arrives CARRYING an id: the manifest's correlation_id, minted by the
EDP_Billing engine (or the bot) and threaded through every service so one
grep traces a segment-day across engine -> bot -> uploader logs. batch_context
adopts that id when given one and only mints its own for id-less batches
(hand-written manifests, old producers).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar

# (correlation_id, batch_key). Empty outside a batch.
_context: ContextVar[tuple[str, str]] = ContextVar("cbos_correlation", default=("", ""))

NO_CONTEXT = "-"


def label() -> str:
    """What the log formatter prints: "3fb59f94 17-07-2026|MCX", or "-"."""
    corr, key = _context.get()
    return f"{corr} {key}" if corr else NO_CONTEXT


@contextmanager
def batch_context(batch_key: str, correlation_id: str | None = None):
    """Stamp every log line emitted inside this block with the batch's
    correlation id (adopted from its manifest when present, else a fresh
    8-hex run id — short enough to read in a line prefix, collision-safe
    within a day's logs)."""
    token = _context.set((correlation_id or uuid.uuid4().hex[:8], batch_key))
    try:
        yield
    finally:
        _context.reset(token)
