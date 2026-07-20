"""Unit tests for the file -> UploadID matcher (pure logic, no network)."""

import pytest

from app.services.upload_matching import (
    AmbiguousUploadRule,
    ColumnCountMismatch,
    NoMatchingUploadRule,
    UploadRule,
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
    settings/client seams work end-to-end without network."""
    rules = fetch_upload_rules([{"UPLOADID": 81}, {"UPLOADID": 85}])
    assert {r.upload_id for r in rules} == {"81", "85"}
