"""Unit tests for the file -> UploadID matcher (pure logic, no network)."""

import pytest

from app.clients.cbos_client import UploadRule, _parse_upload_rule as parse_upload_rule
from app.services.upload_matching import (
    AmbiguousUploadRule,
    ColumnCountMismatch,
    NoMatchingUploadRule,
    _pattern_matches,
    fetch_upload_rules,
    match_file,
)


def _rule(uid, pattern, op="LIKE", ext="CSV", cols=None, name=None):
    return UploadRule(upload_id=str(uid), name=name or f"U{uid}", file_name_pattern=pattern,
                      compare_operator=op, extension=ext, column_count=cols, raw_settings={})


def _write(tmp_path, name, content="a,b,c\n1,2,3\n"):
    p = tmp_path / name
    p.write_text(content)
    return p


def test_pattern_operators():
    assert _pattern_matches("SCRIP", "LIKE", "BSE_SCRIP_190626")
    assert _pattern_matches("nnf_security", "EQUAL", "nnf_security")
    assert not _pattern_matches("nnf_security", "EQUAL", "nnf_security_1")
    assert _pattern_matches("BR", "STARTSWITH", "BR220626")
    assert not _pattern_matches("BR", "STARTSWITH", "ABR220626")


def test_mcx_position_and_trade_resolve_to_distinct_uploadids(tmp_path):
    """The two real MCX UDIFF patterns are substrings; position must pick 534,
    trade must pick 535, despite the overlap."""
    rules = [_rule(534, "MCXCCL_CO_0_CM_55930"), _rule(535, "MCX_CO_0_CM_55930")]
    pos = _write(tmp_path, "Position_MCXCCL_CO_0_CM_55930_20260714_F_0000.csv")
    trade = _write(tmp_path, "Trade_MCX_CO_0_CM_55930_20260714_F_0000.csv")
    assert match_file(pos, rules).upload_id == "534"
    assert match_file(trade, rules).upload_id == "535"


def test_no_matching_pattern_raises(tmp_path):
    with pytest.raises(NoMatchingUploadRule):
        match_file(_write(tmp_path, "totally_unrelated.csv"), [_rule(81, "SCRIP")])


def test_column_count_mismatch_raises(tmp_path):
    rules = [_rule(84, "C_STT_IND", ext="CSV", cols=5)]
    f = _write(tmp_path, "C_STT_IND_22062026.csv", content="a,b,c\n")  # 3 cols, expected 5
    with pytest.raises(ColumnCountMismatch):
        match_file(f, rules)


def test_wrong_extension_is_not_a_rejection(tmp_path):
    """Extension mismatch only warns; the file still resolves (per the matcher's
    documented contract)."""
    rules = [_rule(81, "SCRIP", ext="TXT")]
    f = _write(tmp_path, "BSE_SCRIP_190626.xlsx")
    assert match_file(f, rules).upload_id == "81"


def test_equal_length_tie_broken_by_extension(tmp_path):
    """Two UploadIDs share pattern 'SCRIP' (len 5); the file's extension picks
    the right one instead of silently taking whichever loaded first."""
    rules = [_rule(81, "SCRIP", ext="TXT"), _rule(202, "SCRIP", ext="XLS")]
    assert match_file(_write(tmp_path, "SCRIP_190626.txt"), rules).upload_id == "81"
    assert match_file(_write(tmp_path, "SCRIP_190626.xls"), rules).upload_id == "202"


def test_equal_length_tie_broken_by_exchange(tmp_path):
    """Same pattern AND extension - the exchange folder disambiguates via the
    CBOS label (BSE SCRIP vs NSE SCRIP)."""
    rules = [_rule(81, "SCRIP", ext="TXT", name="BSE SCRIP"),
             _rule(82, "SCRIP", ext="TXT", name="NSE SCRIP")]
    f = _write(tmp_path, "SCRIP_190626.txt")
    assert match_file(f, rules, exchange="NSE").upload_id == "82"
    assert match_file(f, rules, exchange="BSE").upload_id == "81"


def test_genuine_ambiguity_is_rejected_not_guessed(tmp_path):
    """Same pattern, same extension, no exchange signal -> reject loudly rather
    than pick the wrong UploadID."""
    rules = [_rule(81, "SCRIP", ext="TXT", name="SCRIP A"),
             _rule(202, "SCRIP", ext="TXT", name="SCRIP B")]
    with pytest.raises(AmbiguousUploadRule):
        match_file(_write(tmp_path, "SCRIP_190626.txt"), rules)


def test_fetch_upload_rules_pulls_each_uploadid_via_mock_client():
    """fetch_upload_rules goes through the (mock) CBOS client - proves the
    settings lookup works end-to-end without network."""
    from app.clients import cbos_client
    from app.clients.cbos_client import UploadCandidate

    candidates = [UploadCandidate(upload_id="81", step_no=1, name="BSE SCRIP"),
                  UploadCandidate(upload_id="85", step_no=2, name="BSE TRADE FILE")]
    rules = fetch_upload_rules(candidates, cbos_client.get_cbos_client())
    assert {r.upload_id for r in rules} == {"81", "85"}


def test_fetch_upload_rules_deduplicates_repeated_uploadids():
    """A segment's Table2 can list the same UploadID at more than one step;
    settings are fetched once per distinct ID."""
    from app.clients import cbos_client
    from app.clients.cbos_client import UploadCandidate

    candidates = [UploadCandidate(upload_id="81", step_no=1, name="BSE SCRIP"),
                  UploadCandidate(upload_id="81", step_no=7, name="BSE SCRIP")]
    rules = fetch_upload_rules(candidates, cbos_client.get_cbos_client())
    assert len(rules) == 1


# --- parse_upload_rule: the raw Step-4 row, interpreted (pure, no client) ------

def test_parse_accepts_either_filename_key():
    """CBOS spells the pattern field two ways depending on the endpoint."""
    a = parse_upload_rule("81", {"FILE NAME": "SCRIP", "FILEEXTENSION": "TXT"})
    b = parse_upload_rule("81", {"FileNameToCompare": "SCRIP", "FILEEXTENSION": "TXT"})
    assert a.file_name_pattern == b.file_name_pattern == "SCRIP"


def test_parse_accepts_either_extension_key_and_normalises_it():
    for key in ("FILEEXTENSION", "FileExtension"):
        rule = parse_upload_rule("81", {"FILE NAME": "SCRIP", key: ".csv"})
        assert rule.extension == "CSV", f"{key} should normalise to bare uppercase"


def test_parse_defaults_the_compare_operator_to_like():
    rule = parse_upload_rule("81", {"FILE NAME": "SCRIP", "FILEEXTENSION": "TXT"})
    assert rule.compare_operator == "LIKE"


def test_parse_skips_a_row_with_no_pattern_or_no_extension():
    """An unusable row is a skip, not an error - it simply can't match."""
    assert parse_upload_rule("81", {"FILEEXTENSION": "TXT"}) is None
    assert parse_upload_rule("81", {"FILE NAME": "SCRIP"}) is None
    assert parse_upload_rule("81", {}) is None


def test_parse_reads_a_numeric_column_count():
    rule = parse_upload_rule("81", {"FILE NAME": "SCRIP", "FILEEXTENSION": "TXT", "NO. OF COLUMNS": "30"})
    assert rule.column_count == 30


def test_parse_treats_an_unusable_column_count_as_no_check():
    """Absent, blank, '-' or non-numeric all mean 'don't check columns' - none
    of them may cost us the whole rule."""
    for raw in (None, "", "-", "N/A", "thirty"):
        rule = parse_upload_rule("81", {"FILE NAME": "SCRIP", "FILEEXTENSION": "TXT", "NO. OF COLUMNS": raw})
        assert rule is not None, f"{raw!r} should not discard the rule"
        assert rule.column_count is None


def test_parse_falls_back_to_the_candidate_name():
    """The Table2 slot's label is used when the settings row carries no NAME."""
    rule = parse_upload_rule("81", {"FILE NAME": "SCRIP", "FILEEXTENSION": "TXT"},
                             fallback_name="BSE SCRIP")
    assert rule.name == "BSE SCRIP"
    named = parse_upload_rule("81", {"NAME": "From settings", "FILE NAME": "SCRIP", "FILEEXTENSION": "TXT"},
                              fallback_name="BSE SCRIP")
    assert named.name == "From settings", "the settings row wins when it has a NAME"


def test_parse_keeps_the_raw_row_for_audit():
    setting = {"FILE NAME": "SCRIP", "FILEEXTENSION": "TXT", "ODDBALL": 1}
    assert parse_upload_rule("81", setting).raw_settings == setting
