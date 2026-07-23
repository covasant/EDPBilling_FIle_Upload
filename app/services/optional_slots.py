"""Loader for the optional-slot allowlist (app/config/optional_slots.yaml).

The allowlist bounds Step 8: only these UploadIDs may be auto-marked optional
when unfilled. See docs/BATCH_HANDOFF_CONTRACT.md ("Completeness gate") for
why this is a code-reviewed file and not a runtime setting.
"""

import logging
from functools import lru_cache
from pathlib import Path

import yaml

from app.core.config import settings

logger = logging.getLogger("optional_slots")


@lru_cache
def _load() -> dict[str, set[str]]:
    path = Path(settings.optional_slots_path)
    if not path.is_file():
        # Missing allowlist = empty allowlist: the gate fails CLOSED (every
        # unfilled file slot parks the batch), never silently open.
        logger.warning("optional_slots: %s not found - treating every unfilled slot "
                       "as required", path)
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    parsed = {str(seg).upper(): {str(slot) for slot in (slots or [])} for seg, slots in raw.items()}
    logger.info("optional_slots: loaded %s", {s: sorted(v) for s, v in parsed.items()})
    return parsed


def allowlisted(segment: str) -> set[str]:
    """UploadIDs that may be auto-marked optional for this segment."""
    return _load().get(segment.upper(), set())


def reload() -> None:
    """Test hook / config-change hook: drop the cache."""
    _load.cache_clear()
