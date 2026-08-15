"""The FOPositionChange pseudo-segment — CBOS's Corporate Action Position Change
process (V6 Steps 34-35) coming through the ordinary upload lane.

A corporate action makes NSE revise F&O lot sizes and strikes, and CBOS restates the
position book from two CSVs NSE publishes into FO/Reports. It reserves its own
PROCESSID with its own Table2 and is uploaded to through the identical Steps 4/5/7/8,
so the uploader needs no new lane — only for `FOPositionChange` to be a legal manifest
`segment`, and for the ONE segment-scoped call that would misbehave to be skipped.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from edpb_core.manifest import ManifestValidationError, validate_manifest
from edpb_core.segments import CORP_ACTION_SEGMENT, SEGMENT_ORDER

TRADE_DATE = "2026-08-13"


def _manifest(segment: str, batch_id: str | None = None) -> dict:
    return {
        "manifest_version": 1,
        "batch_id": batch_id or f"{segment}-{TRADE_DATE}-abcdef12",
        "segment": segment,
        "trade_date": TRADE_DATE,
        "producer": {"name": "edpb", "version": "1.0.0", "action": "corpaction"},
        "created_at": "2026-08-13T19:50:00+05:30",
        "download_outcome": {"status": "success"},
        "files": [
            {
                "name": f"HINDPETRO_10412_{leg}_POSITIONS.CSV",
                "kind": f"corp_action_{leg.lower()}_positions",
                "exchange": "NSE",
                "sha256": "0" * 64,
                "size_bytes": 42,
            }
            # Both legs: EXISTING is the book before the ratio is applied, ADJUSTED the
            # same book after, and CBOS needs both to compute the delta.
            for leg in ("EXISTING", "ADJUSTED")
        ],
    }


# ── The manifest contract ────────────────────────────────────────────────────


def test_the_pseudo_segment_is_a_legal_manifest_segment() -> None:
    """Without this the bot's corporate action manifest is rejected at intake and the
    Step 34-35 files never reach CBOS at all."""
    validate_manifest(_manifest(CORP_ACTION_SEGMENT))


def test_its_mixed_case_batch_id_prefix_is_accepted() -> None:
    """batch_id is `{segment}-{date}-{hex}` built verbatim from the segment, and
    FOPositionChange is spelled the way CBOS spells it. The old `^[A-Z]+` pattern would
    have rejected the manifest for its ID alone, after the enum already allowed it."""
    validate_manifest(
        _manifest(CORP_ACTION_SEGMENT, batch_id=f"{CORP_ACTION_SEGMENT}-{TRADE_DATE}-0a1b2c3d")
    )


def test_the_enum_still_refuses_an_unknown_segment() -> None:
    """Widening the batch_id pattern must not have made the segment field a free string —
    the enum is what stops a typo'd folder name reaching CBOS as a GROUPNAME."""
    with pytest.raises(ManifestValidationError):
        validate_manifest(_manifest("FOPositionChanges"))


def test_the_pseudo_segment_is_not_in_the_daily_segment_order() -> None:
    """A corporate action is event-driven and rare. In SEGMENT_ORDER the engine would
    drive it every trading day and report a missing batch on almost all of them."""
    assert CORP_ACTION_SEGMENT not in SEGMENT_ORDER


# ── The one call that would misbehave ────────────────────────────────────────


def _task(tmp_path: Path, segment: str):
    from app.core.config import settings
    from app.core.queue import SegmentBatchTask

    folder = Path(settings.file_root_path) / "13-08-2026" / segment / "NSE"
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "HINDPETRO_10412_EXISTING_POSITIONS.CSV"
    f.write_text("symbol,qty\nHINDPETRO,100\n")
    return SegmentBatchTask(folder_date="13-08-2026", segment=segment, files=[(str(f), "NSE")])


def _record_begin_upload(monkeypatch) -> list[str]:
    """Capture which segments Step 1 (BeginFileUpload) is asked about."""
    from app.clients import cbos_client

    asked: list[str] = []
    client = cbos_client.get_cbos_client()
    original = client.may_begin_upload

    def spy(segment: str, trade_date: str) -> bool:
        asked.append(segment)
        return original(segment, trade_date)

    monkeypatch.setattr(client, "may_begin_upload", spy)
    return asked


def test_the_holiday_check_is_not_asked_for_the_pseudo_segment(monkeypatch, tmp_path) -> None:
    """BeginFileUpload takes a Segment, and FOPositionChange is a GROUPNAME. CBOS answered
    INVALID SEGMENT for the post-trade pseudo-segments put through segment-scoped calls,
    and the day CBOS_HOLIDAY_CHECK_ENFORCED is switched on, that answer would defer every
    corporate action batch forever on a reply that was never about a holiday.

    Its real gate is Step 34 (DR BILLPOSTING), checked by the engine before it reserves
    the PROCESSID this batch arrives with.
    """
    monkeypatch.setenv("CBOS_MOCK_RANDOM_SUCCESS_RATE", "1.0")
    monkeypatch.setenv("CBOS_MOCK_PENDING_POLLS", "0")
    monkeypatch.setenv("CBOS_POLL_INTERVAL_SECONDS", "0")

    from app.core import database
    from app.services import upload_service

    database.reset_engine()
    database.init_db()

    asked = _record_begin_upload(monkeypatch)
    upload_service.process_batch(_task(tmp_path, CORP_ACTION_SEGMENT))

    assert CORP_ACTION_SEGMENT not in asked


def test_a_real_segment_is_still_asked(monkeypatch, tmp_path) -> None:
    """The skip must be exactly one segment wide — the holiday check is a real guard for
    the nine market segments and silently dropping it for them would start batches on
    days CBOS had already ruled out."""
    monkeypatch.setenv("CBOS_MOCK_RANDOM_SUCCESS_RATE", "1.0")
    monkeypatch.setenv("CBOS_MOCK_PENDING_POLLS", "0")
    monkeypatch.setenv("CBOS_POLL_INTERVAL_SECONDS", "0")

    from app.core import database
    from app.services import upload_service

    database.reset_engine()
    database.init_db()

    asked = _record_begin_upload(monkeypatch)
    upload_service.process_batch(_task(tmp_path, "MCX"))

    assert "MCX" in asked


def test_the_uploader_still_does_not_trigger_the_corporate_action() -> None:
    """Step 35 Phase 2 belongs to the engine, exactly as Step 11 does for a real segment.
    The two phases are the same endpoint differing only in PROCESSID, so a trigger call
    living here would be one argument away from restating the book against no input."""
    from app.clients import cbos_client

    assert not hasattr(cbos_client, "trigger_process")
    assert not hasattr(cbos_client.get_cbos_client(), "trigger_process")


def test_the_bots_manifest_writer_and_the_schema_agree_on_the_batch_id_shape() -> None:
    """The bot builds `f"{segment}-{iso_date}-{uuid4().hex[:8]}"`. Pinned here because
    the two live in different repos and nothing else would catch them drifting."""
    batch_id = f"{CORP_ACTION_SEGMENT}-{date(2026, 8, 13).isoformat()}-0123abcd"
    validate_manifest(_manifest(CORP_ACTION_SEGMENT, batch_id=batch_id))
    assert json.dumps(batch_id)  # trivially serialisable — no exotic characters
