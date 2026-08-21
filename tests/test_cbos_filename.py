"""The name CBOS is told, when it differs from the name on disk (pure logic, no network).

CBOS validates the DATE inside an uploaded filename against one it computes itself and
rejects a mismatch outright — observed live on 2026-08-17:

    NSE BSE InterOperable Scrip Mapping Upload — FAILED
    FILE NAME TRADE DATE(T-1) MISMATCH  2026-08-17

That day NSE published no ``BSE_Scrip_Series_Mapping_14082026.csv`` for Friday at all and
listed ``..._15082026.csv`` instead — a Saturday, and Independence Day, so not a session.
CBOS wanted the 14th. The Step-40 payloads below are the real ones, captured from live
CBOS while diagnosing it.
"""

import pytest

from app.services import cbos_filename
from app.services.cbos_filename import resolve_upload_name

TRADE_DATE = "2026-08-17"

# Verbatim from live CBOS, Step 40 (get_expected_filename), 2026-08-17.
MAPPING_83 = {
    "UploadID": 83,
    "Description": "EQ NSE/BSE INTEROPERABLE SCRIP MAPPING UPLOAD",
    "DateBasis": "T-1",
    "MatchType": "CONTAINS_ALL",
    "ExpectedFileNamePattern1": "%BSE_SCRIP_SERIES_MAPPING%",
    "ExpectedFileNamePattern2": "%14082026%",
    "InputTradeDate": "2026-08-17",
    "Trading_Date_DDMMYYYY": "17082026",
    "LastTradingDate_DDMMYYYY": "14082026",
}
STT_84 = {
    "UploadID": 84,
    "DateBasis": "T",
    "MatchType": "CONTAINS_ALL",
    "ExpectedFileNamePattern1": "%C_STT_IND%",
    "ExpectedFileNamePattern2": "%17082026%",
    "InputTradeDate": "2026-08-17",
    "Trading_Date_DDMMYYYY": "17082026",
    "LastTradingDate_DDMMYYYY": "14082026",
}
SCRIP_81 = {
    "UploadID": 81,
    "DateBasis": "T-1",
    "MatchType": "EXACT",
    "ExpectedFileNamePattern1": "SCRIP_140826.TXT",
    "InputTradeDate": "2026-08-17",
    "Trading_Date_DDMMYYYY": "17082026",
    "LastTradingDate_DDMMYYYY": "14082026",
}


class _Client:
    """Answers Step 40 with a canned payload, in CBOS's envelope shape."""

    def __init__(self, data, raises=False):
        self._data, self._raises = data, raises
        self.calls = 0

    def get_expected_filename(self, segment, upload_id, trade_date=""):
        self.calls += 1
        if self._raises:
            raise RuntimeError("CBOS said no")
        return {"Data": [self._data]}


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(cbos_filename.settings, "cbos_rewrite_upload_filename_date", True)


# ── The normal day: nothing happens ──────────────────────────────────────────


def test_a_name_that_already_carries_cbos_date_is_left_alone():
    """Every file on almost every day. The exchange names these for the same session CBOS
    expects, so this whole mechanism is inert and the on-disk name goes up unchanged."""
    name, note = resolve_upload_name(
        _Client(MAPPING_83), "EQ", "83", "BSE_Scrip_Series_Mapping_14082026.csv", TRADE_DATE
    )
    assert name == "BSE_Scrip_Series_Mapping_14082026.csv"
    assert note is None


def test_a_name_with_no_date_in_it_is_left_alone():
    """nnf_security.dat, contract.txt — nothing to rewrite and nothing to assert."""
    for undated in ("nnf_security.dat", "contract.txt", "security_slb.txt"):
        name, note = resolve_upload_name(_Client(MAPPING_83), "EQ", "83", undated, TRADE_DATE)
        assert (name, note) == (undated, None), undated


# ── The day it fires ─────────────────────────────────────────────────────────


def test_the_saturday_stamped_file_is_sent_under_the_date_cbos_wants():
    """The live failure. CBOS wants 14082026; NSE published only 15082026."""
    name, note = resolve_upload_name(
        _Client(MAPPING_83), "EQ", "83", "BSE_Scrip_Series_Mapping_15082026.csv", TRADE_DATE
    )
    assert name == "BSE_Scrip_Series_Mapping_14082026.csv"
    assert note is not None
    assert note["on_disk"] == "BSE_Scrip_Series_Mapping_15082026.csv"
    assert note["sent_as"] == name
    assert note["cbos_last_trading_date"] == "14082026"
    assert note["days_late"] == 1
    assert "not verified" in note["asserts"], (
        "the audit row must say the content claim is unchecked — it is the only record "
        "that we asserted a Saturday-stamped file holds Friday's data"
    )


def test_a_t_basis_slot_is_out_of_scope_entirely():
    """Thirteen of the sixteen dated slots are DateBasis T and are not touched, even when
    stamped a day late. Narrow on purpose: none has failed, CBOS's own check would catch
    it, and a rejected upload someone looks at beats a silent rewrite."""
    late = "C_STT_IND_18082026.csv"
    assert resolve_upload_name(_Client(STT_84), "EQ", "84", late, TRADE_DATE) == (late, None)


def test_the_bse_ddmmyy_files_are_in_scope_and_keep_their_shape():
    """Three of the six T-1 slots are BSE's, stamped ddmmyy — 81 (BSE SCRIP), 97 (BSEFO
    contract master) and 117 (BSECD contract master).

    An earlier cut of this module handled DDMMYYYY only and silently left these three
    exposed to the very failure it exists to fix. The rewrite must also keep the shape it
    found: CBOS's expected name for 97 is EQD_CO140826.CSV, not EQD_CO14082026.CSV.
    """
    name, note = resolve_upload_name(
        _Client(SCRIP_81), "EQ", "81", "SCRIP_CC_150826.txt", TRADE_DATE
    )
    assert name == "SCRIP_CC_140826.txt", "six digits in, six digits out"
    assert note["sent_date_token"] == "140826"
    assert note["cbos_last_trading_date"] == "14082026"


def test_a_ddmmyy_name_already_correct_is_left_alone():
    """Today's real BSE scrip file — it already carries the wanted day, which is why this
    slot has been uploading fine while the mapping file was rejected."""
    assert resolve_upload_name(
        _Client(SCRIP_81), "EQ", "81", "SCRIP_CC_140826.txt", TRADE_DATE
    ) == ("SCRIP_CC_140826.txt", None)


def test_a_ddmmyy_file_stamped_earlier_is_refused_like_any_other():
    """The direction rule is not format-specific: an older BSE file is stale data whether
    it is stamped in six digits or eight."""
    stale = "SCRIP_CC_070826.txt"
    assert resolve_upload_name(_Client(SCRIP_81), "EQ", "81", stale, TRADE_DATE) == (stale, None)


# ── The things it must NOT touch ─────────────────────────────────────────────


def test_a_number_that_is_not_a_date_is_never_rewritten():
    """The trade file's YYYYMMDD would be month 26 read as DDMMYYYY, so it is not a date
    and not a candidate. Without that check this would corrupt the name of the biggest
    file in the batch."""
    original = "Trade_NSE_CM_0_TM_10412_20260813_F_0000.csv"
    name, note = resolve_upload_name(_Client(MAPPING_83), "EQ", "83", original, TRADE_DATE)
    assert (name, note) == (original, None)


def test_a_backfill_is_never_rewritten():
    """Step 40 takes no trade date — it answers for whatever day CBOS thinks it is
    (verified live: passing tradedate=2026-08-12 still returned InputTradeDate
    2026-08-17). Following it on an old batch would stamp TODAY onto that batch's file,
    which is worse than the mismatch this exists to fix."""
    name, note = resolve_upload_name(
        _Client(MAPPING_83),
        "EQ",
        "83",
        "BSE_Scrip_Series_Mapping_11082026.csv",
        "2026-08-12",  # an older batch; CBOS still answers for the 17th
    )
    assert (name, note) == ("BSE_Scrip_Series_Mapping_11082026.csv", None)


def test_an_unknown_date_basis_is_not_guessed():
    """'N/A' and anything new leave the name alone rather than picking a date."""
    payload = dict(MAPPING_83, DateBasis="N/A")
    original = "BSE_Scrip_Series_Mapping_15082026.csv"
    assert resolve_upload_name(_Client(payload), "EQ", "83", original, TRADE_DATE) == (
        original,
        None,
    )


def test_a_step_40_failure_never_fails_the_upload():
    """A cross-check that cannot answer must not cost the batch a file — falling back to
    the on-disk name is exactly the behaviour before this existed."""
    original = "BSE_Scrip_Series_Mapping_15082026.csv"
    assert resolve_upload_name(_Client(None, raises=True), "EQ", "83", original, TRADE_DATE) == (
        original,
        None,
    )


def test_the_kill_switch_stops_it_without_a_deploy(monkeypatch):
    """It asserts something about file CONTENTS that cannot be checked here, so there has
    to be a way to stop it that is not a code change."""
    monkeypatch.setattr(cbos_filename.settings, "cbos_rewrite_upload_filename_date", False)
    client = _Client(MAPPING_83)
    original = "BSE_Scrip_Series_Mapping_15082026.csv"
    assert resolve_upload_name(client, "EQ", "83", original, TRADE_DATE) == (original, None)
    assert client.calls == 0, "disabled means not even asking CBOS"


# ── The direction rule: stale data must NOT be laundered ─────────────────────


def test_an_older_file_is_never_relabelled_as_the_wanted_session():
    """THE SAFETY PROPERTY. The download bot's window reaches back up to seven days to
    find a file, so on a publication outage it legitimately holds genuinely OLD data.

    CBOS's date check is what catches that: it rejects the upload and someone looks.
    Rewriting the name would relabel stale data as current and hand it to billing with
    the objection silenced — a working backstop turned into invisible wrong data. So a
    file stamped BEFORE the wanted session is refused, and CBOS's rejection stands.
    """
    stale = "BSE_Scrip_Series_Mapping_07082026.csv"  # a week old; CBOS wants 14082026
    name, note = resolve_upload_name(_Client(MAPPING_83), "EQ", "83", stale, TRADE_DATE)
    assert (name, note) == (stale, None)


@pytest.mark.parametrize("stamp", ["11082026", "12082026", "13082026"])
def test_every_earlier_stamp_is_refused_not_just_very_old_ones(stamp):
    """One day early is the same kind of wrong as a week early — it is a different
    session's file either way, and only the wanted session's may go up under its name."""
    older = f"BSE_Scrip_Series_Mapping_{stamp}.csv"
    assert resolve_upload_name(_Client(MAPPING_83), "EQ", "83", older, TRADE_DATE) == (
        older,
        None,
    )


def test_a_stamp_too_far_after_the_session_is_refused_too():
    """A late stamp is a day or two, not a fortnight. Past the bound it is not this
    session's file arriving late, it is a different file."""
    far = "BSE_Scrip_Series_Mapping_25082026.csv"  # 11 days after the wanted 14082026
    assert resolve_upload_name(_Client(MAPPING_83), "EQ", "83", far, TRADE_DATE) == (far, None)


@pytest.mark.parametrize("stamp", ["15082026", "16082026", "17082026"])
def test_a_stamp_just_after_the_session_is_accepted_as_a_late_publication(stamp):
    """The observed case and its neighbours: Friday's file stamped over the weekend or on
    the Monday it was generated."""
    late = f"BSE_Scrip_Series_Mapping_{stamp}.csv"
    name, note = resolve_upload_name(_Client(MAPPING_83), "EQ", "83", late, TRADE_DATE)
    assert name == "BSE_Scrip_Series_Mapping_14082026.csv"
    assert note is not None and 1 <= note["days_late"] <= 3


def test_the_refusal_reason_is_recorded_in_the_log_not_swallowed(caplog):
    """CBOS is about to reject this upload and that rejection is correct. Silence would
    leave someone hunting for why a file failed when the answer is that it is the wrong
    day's — so the refusal says so."""
    import logging

    with caplog.at_level(logging.WARNING, logger="cbos_filename"):
        resolve_upload_name(
            _Client(MAPPING_83), "EQ", "83", "BSE_Scrip_Series_Mapping_07082026.csv", TRADE_DATE
        )
    assert any("NOT rewriting" in r.getMessage() for r in caplog.records)


# ── The name has to reach the wire, on both steps ────────────────────────────
# The resolver above is only half of it. What CBOS actually receives is decided by
# upload_file (Step 5, per chunk) and register_file (Step 7), and CBOS binds the two by
# GUID — so registering a different name than was uploaded strands the drop folder. These
# pin the wiring rather than the decision.


def _client_recording(monkeypatch):
    from app.clients.cbos_client import CBOSClient

    client = CBOSClient()
    seen = {"chunks": [], "registered": []}
    monkeypatch.setattr(
        client,
        "_upload_chunk",
        lambda upload_id, guid, file_name, chunk, cur, total: (
            seen["chunks"].append(file_name) or {}
        ),
    )
    monkeypatch.setattr(
        client,
        "_create_file_entry",
        lambda upload_id, guid, file_name, process_id, trade_date: (
            seen["registered"].append(file_name) or {}
        ),
    )
    return client, seen


def test_step_5_sends_the_name_it_was_given_not_the_one_on_disk(tmp_path, monkeypatch):
    """The whole point of passing a name instead of renaming the file: disk keeps the
    exchange's name, so which day's data this was stays recoverable."""
    client, seen = _client_recording(monkeypatch)
    on_disk = tmp_path / "BSE_Scrip_Series_Mapping_15082026.csv"
    on_disk.write_text("a,b\n1,2\n")

    client.upload_file(on_disk, "83", "guid-1", file_name="BSE_Scrip_Series_Mapping_14082026.csv")

    assert seen["chunks"] == ["BSE_Scrip_Series_Mapping_14082026.csv"]
    assert on_disk.exists(), "the file on disk must not be renamed"
    assert on_disk.name == "BSE_Scrip_Series_Mapping_15082026.csv"


def test_step_5_defaults_to_the_disk_name_when_not_given_one(tmp_path, monkeypatch):
    """Every other file, every other day — the parameter is opt-in and absent means
    exactly the previous behaviour."""
    client, seen = _client_recording(monkeypatch)
    on_disk = tmp_path / "C_STT_17082026.csv"
    on_disk.write_text("a\n1\n")

    client.upload_file(on_disk, "94", "guid-2")

    assert seen["chunks"] == ["C_STT_17082026.csv"]


def test_step_7_registers_the_same_name_step_5_uploaded(tmp_path, monkeypatch):
    """CBOS binds the chunks to the entry by GUID. Registering a different name than was
    uploaded leaves the drop folder stranded — the file is in CBOS and unusable."""
    client, seen = _client_recording(monkeypatch)
    on_disk = tmp_path / "BSE_Scrip_Series_Mapping_15082026.csv"
    on_disk.write_text("a,b\n1,2\n")
    sent = "BSE_Scrip_Series_Mapping_14082026.csv"

    client.upload_file(on_disk, "83", "guid-3", file_name=sent)
    client.register_file("83", "guid-3", sent, "PID1", "2026-08-17")

    assert seen["chunks"] == [sent]
    assert seen["registered"] == [sent]
    assert seen["chunks"] == seen["registered"], "Step 5 and Step 7 must agree on the name"


def test_the_upload_service_hands_both_steps_the_resolved_name():
    """Guard on the wiring in upload_service, which is where the two could drift apart:
    the resolved name is computed once and used for Step 5 AND carried through
    pending_registration to Step 7."""
    import inspect

    from app.services import upload_service

    src = inspect.getsource(upload_service._process_batch)
    assert "file_name=send_name" in src, "Step 5 must be given the resolved name"
    assert "register_file(rule.upload_id, guid, send_name" in src, (
        "Step 7 must register the SAME name Step 5 uploaded, not file_path.name"
    )
