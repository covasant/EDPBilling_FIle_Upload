"""Unit tests for the file -> UploadID matcher (pure logic, no network)."""

import pytest

from app.clients.cbos_client import UploadRule
from app.clients.cbos_client import _parse_upload_rule as parse_upload_rule
from app.services.upload_matching import (
    AmbiguousUploadRule,
    ColumnCountMismatch,
    NoMatchingUploadRule,
    _pattern_matches,
    fetch_upload_rules,
    match_file,
)


def _rule(uid, pattern, op="LIKE", ext="CSV", cols=None, name=None):
    return UploadRule(
        upload_id=str(uid),
        name=name or f"U{uid}",
        file_name_pattern=pattern,
        compare_operator=op,
        extension=ext,
        column_count=cols,
        raw_settings={},
    )


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
    rules = [
        _rule(81, "SCRIP", ext="TXT", name="BSE SCRIP"),
        _rule(82, "SCRIP", ext="TXT", name="NSE SCRIP"),
    ]
    f = _write(tmp_path, "SCRIP_190626.txt")
    assert match_file(f, rules, exchange="NSE").upload_id == "82"
    assert match_file(f, rules, exchange="BSE").upload_id == "81"


def test_genuine_ambiguity_is_rejected_not_guessed(tmp_path):
    """Same pattern, same extension, no exchange signal -> reject loudly rather
    than pick the wrong UploadID."""
    rules = [
        _rule(81, "SCRIP", ext="TXT", name="SCRIP A"),
        _rule(202, "SCRIP", ext="TXT", name="SCRIP B"),
    ]
    with pytest.raises(AmbiguousUploadRule):
        match_file(_write(tmp_path, "SCRIP_190626.txt"), rules)


def test_fetch_upload_rules_pulls_each_uploadid_via_mock_client():
    """fetch_upload_rules goes through the (mock) CBOS client - proves the
    settings lookup works end-to-end without network."""
    from app.clients import cbos_client
    from app.clients.cbos_client import UploadCandidate

    candidates = [
        UploadCandidate(upload_id="81", step_no=1, name="BSE SCRIP"),
        UploadCandidate(upload_id="85", step_no=2, name="BSE TRADE FILE"),
    ]
    rules = fetch_upload_rules(candidates, cbos_client.get_cbos_client())
    assert {r.upload_id for r in rules} == {"81", "85"}


def test_fetch_upload_rules_deduplicates_repeated_uploadids():
    """A segment's Table2 can list the same UploadID at more than one step;
    settings are fetched once per distinct ID."""
    from app.clients import cbos_client
    from app.clients.cbos_client import UploadCandidate

    candidates = [
        UploadCandidate(upload_id="81", step_no=1, name="BSE SCRIP"),
        UploadCandidate(upload_id="81", step_no=7, name="BSE SCRIP"),
    ]
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
    rule = parse_upload_rule(
        "81", {"FILE NAME": "SCRIP", "FILEEXTENSION": "TXT", "NO. OF COLUMNS": "30"}
    )
    assert rule.column_count == 30


def test_parse_treats_an_unusable_column_count_as_no_check():
    """Absent, blank, '-' or non-numeric all mean 'don't check columns' - none
    of them may cost us the whole rule."""
    for raw in (None, "", "-", "N/A", "thirty"):
        rule = parse_upload_rule(
            "81", {"FILE NAME": "SCRIP", "FILEEXTENSION": "TXT", "NO. OF COLUMNS": raw}
        )
        assert rule is not None, f"{raw!r} should not discard the rule"
        assert rule.column_count is None


def test_parse_falls_back_to_the_candidate_name():
    """The Table2 slot's label is used when the settings row carries no NAME."""
    rule = parse_upload_rule(
        "81", {"FILE NAME": "SCRIP", "FILEEXTENSION": "TXT"}, fallback_name="BSE SCRIP"
    )
    assert rule.name == "BSE SCRIP"
    named = parse_upload_rule(
        "81",
        {"NAME": "From settings", "FILE NAME": "SCRIP", "FILEEXTENSION": "TXT"},
        fallback_name="BSE SCRIP",
    )
    assert named.name == "From settings", "the settings row wins when it has a NAME"


def test_parse_keeps_the_raw_row_for_audit():
    setting = {"FILE NAME": "SCRIP", "FILEEXTENSION": "TXT", "ODDBALL": 1}
    assert parse_upload_rule("81", setting).raw_settings == setting


def test_placeholder_exchange_does_not_break_ties(tmp_path):
    """ "NA" is a placeholder for segments with no exchange split, not a real
    exchange. The tie-breaker substring-matches the exchange against the CBOS
    rule name, and "NA" hides inside ordinary words (FINAL, MANUAL, NATIONAL) -
    so feeding it in could silently pick the wrong UploadID. Passing None
    instead must leave the tie genuinely unresolved."""
    f = _write(tmp_path, "POSITION_20260717.csv")
    tied = [
        _rule(1, "POSITION", ext="CSV", name="POSITION INTRADAY"),
        _rule(2, "POSITION", ext="CSV", name="POSITION FINAL"),  # contains "NA"
    ]

    # The bug: "NA" singles out "POSITION FINAL" and returns it with confidence.
    assert match_file(f, tied, exchange="NA").upload_id == "2"

    # Correct behaviour: no exchange info -> the tie stands and is rejected loudly.
    with pytest.raises(AmbiguousUploadRule):
        match_file(f, tied, exchange=None)


def test_processing_steps_are_never_asked_for_upload_settings():
    """Table2 rows with UPLOADID=0 are processing steps (Brokerage Computation,
    Bill Posting), not file slots. Asking CBOS for their upload settings is a
    call real CBOS may reject - and a Step 4 error lands in process_batch's
    setup retry loop, which dumps the whole batch to uploadFailed. The mock
    answers with a phantom "UPLOAD 0" rule instead, so nothing looked wrong.
    """
    from app.clients.cbos_client import UploadCandidate
    from app.services.upload_matching import fetch_upload_rules

    candidates = [
        UploadCandidate(upload_id="535", step_no=3, name="MCX Trade File Upload"),
        UploadCandidate(upload_id="0", step_no=5, name="MCX Brokerage Computation"),
        UploadCandidate(upload_id="0", step_no=6, name="MCX Bill Posting"),
    ]

    asked: list[str] = []

    class _Client:
        def upload_settings(self, upload_id, fallback_name="", segment="", trade_date=""):
            asked.append(upload_id)
            return _rule(535, "MCX_CO_0_CM", ext="csv", name="MCX COM TRADE FILE")

    rules = fetch_upload_rules(candidates, _Client())

    assert asked == ["535"], f"Step 4 must only run for real file slots, asked: {asked}"
    assert len(rules) == 1, "a processing step must not become a matching rule"


# ---------------------------------------------------------------------------
# Control-record files (NCDEX physical AL02 and friends)
# ---------------------------------------------------------------------------

# The real NCDEX physical trade file, trade date 2026-06-17. Line 1 is a
# control record (7 columns); line 2 is the delivery row (20 columns), which
# is what CBOS's rule 321 declares. The first-line-only sniff rejected this
# before CBOS ever saw it.
_AL02_REAL = (
    "AL02,01240,17062026,1,120,120,0\n"
    "17-JUN-2026,D,2026016,FUTCOM,GUARGUM5,19-JUN-2026,0.00,FF,0,JODHPUR,"
    "01240,C,H30081,S,120,120,0,Y,120,0\n"
)


def test_control_record_file_is_not_rejected(tmp_path):
    """A file whose FIRST line is a control record must still match on its data
    rows. Observed live 2026-06-17: NCDEXPHY's AL02 was rejected for "has 7
    column(s), expected 20" while its data row had exactly 20."""
    rules = [_rule(321, "NCDEX_AL02_01240_", ext="CSV", cols=20, name="NCDEX Physical Trade File")]
    f = _write(tmp_path, "NCDEX_AL02_01240_17062026.CSV", content=_AL02_REAL)

    assert match_file(f, rules).upload_id == "321"


def test_genuinely_wrong_width_is_still_rejected(tmp_path):
    """The relaxation must not disarm the check: when NO sniffed line has the
    expected width the file is still refused."""
    rules = [_rule(321, "NCDEX_AL02_01240_", ext="CSV", cols=99)]
    f = _write(tmp_path, "NCDEX_AL02_01240_17062026.CSV", content=_AL02_REAL)

    with pytest.raises(ColumnCountMismatch):
        match_file(f, rules)


def test_first_line_match_still_works(tmp_path):
    """The common case - no control record, line 1 is data - is unchanged.
    This is what every segment uploading successfully today relies on."""
    rules = [_rule(84, "C_STT_IND", ext="CSV", cols=3)]
    f = _write(tmp_path, "C_STT_IND_22062026.csv", content="a,b,c\n1,2,3\n")

    assert match_file(f, rules).upload_id == "84"


# ---------------------------------------------------------------------------
# Non-CSV files (NSE's pipe-delimited contract masters)
# ---------------------------------------------------------------------------

# The real NSE CO contract master, trade date 2026-08-11, first two lines
# verbatim (the data row truncated to the first 12 of its 69 fields, with the
# rest supplied below so the width is exact). Line 1 is a 3-field control
# record; the data rows carry 69 fields, which is what CBOS's rule 139
# declares. Counted on a comma every line is ONE field, so the sniff reported
# [1, 1, 1, 1, 1] and rejected it before CBOS ever saw it.
_CO_CONTRACT_REAL = "NEATCO|15500|\n" + "".join(
    "|".join(
        ["1", "0", "UNDCOM", "GOLD", "XX", "", "-1", "-1", "XX", "2", "0", ""]
        + ["0"] * 57  # 12 + 57 = 69 fields, the real row's width
    )
    + "\n"
    for _ in range(3)
)


def test_pipe_delimited_contract_master_is_not_rejected(tmp_path):
    """NSE's contract masters are pipe-delimited, not CSV. Observed live
    2026-08-11: co_contract was rejected for "has [1, 1, 1, 1, 1] column(s),
    expected 69" while its data rows had exactly the 69 CBOS asked for."""
    rules = [_rule(139, "CO_CONTRACT", ext="*", cols=69, name="CONTRACT MASTER - NSECOM")]
    f = _write(tmp_path, "co_contract", content=_CO_CONTRACT_REAL)

    assert match_file(f, rules).upload_id == "139"


def test_a_wrong_width_is_still_rejected_under_every_delimiter(tmp_path):
    """The fallback must not disarm the check: when NO delimiter produces the
    declared width on any sniffed line, the file is still refused."""
    rules = [_rule(139, "CO_CONTRACT", ext="*", cols=70)]
    f = _write(tmp_path, "co_contract", content=_CO_CONTRACT_REAL)

    with pytest.raises(ColumnCountMismatch):
        match_file(f, rules)


def test_the_rejection_names_the_delimiters_it_tried(tmp_path):
    """A bare "has 1 column(s)" is what sent us looking at NSE's file instead of
    our own sniff. The message must say what it counted with."""
    rules = [_rule(139, "CO_CONTRACT", ext="*", cols=70)]
    f = _write(tmp_path, "co_contract", content=_CO_CONTRACT_REAL)

    with pytest.raises(ColumnCountMismatch, match=r"delimiters tried:.*\|"):
        match_file(f, rules)


def test_a_comma_file_is_unaffected_by_the_fallback(tmp_path):
    """The configured delimiter still wins: a comma file whose width matches
    must not be re-counted under a fallback that happens to agree."""
    rules = [_rule(84, "C_STT_IND", ext="CSV", cols=3)]
    f = _write(tmp_path, "C_STT_IND_22062026.csv", content="a,b,c\n1,2,3\n")

    assert match_file(f, rules).upload_id == "84"


def test_only_the_first_few_lines_are_sniffed(tmp_path):
    """Bounded so a 77MB trade file is not read end to end. A matching width
    that appears only past the sniff window does not rescue the file."""
    from app.services.upload_matching import COLUMN_SNIFF_LINES

    body = "".join("a,b\n" for _ in range(COLUMN_SNIFF_LINES + 3)) + "a,b,c,d,e\n"
    rules = [_rule(90, "DEEP", ext="CSV", cols=5)]
    f = _write(tmp_path, "DEEP_1.csv", content=body)

    with pytest.raises(ColumnCountMismatch):
        match_file(f, rules)


# ---------------------------------------------------------------------------
# Fixed-width files (BSE's SCRIP master)
# ---------------------------------------------------------------------------

# BSE's scrip master, trade date 2026-08-11: fixed-width records, no separator
# anywhere in the line. Counted on a comma, a pipe OR a tab every line is one
# field, so the sniff reported [1, 1, 1, 1, 1] against rule 81's declared 30 and
# the file was rejected before CBOS ever saw it.
_SCRIP_CC_FIXED_WIDTH = "".join(
    f"5000{n:02d}   RELIANCE   EQ  A  1  10.00  N  BSE  20260811  ACTIVE       \n"
    for n in range(1, 6)
)


def test_fixed_width_file_is_not_rejected(tmp_path):
    """No candidate delimiter finds a boundary, so the file's shape is UNKNOWN,
    not wrong. Skip the check and let CBOS arbitrate - the same treatment a
    .xlsx already gets. Observed live 2026-08-11: SCRIP_CC_110826.txt was
    rejected for "has [1, 1, 1, 1, 1] column(s), expected 30"."""
    rules = [_rule(81, "SCRIP", ext="TXT", cols=30, name="BSE SCRIP")]
    f = _write(tmp_path, "SCRIP_CC_110826.txt", content=_SCRIP_CC_FIXED_WIDTH)

    assert match_file(f, rules).upload_id == "81"


def test_the_skip_needs_every_line_under_every_delimiter(tmp_path):
    """The escape hatch is narrow on purpose. One delimiter that DOES find a
    boundary means the format is understood, so a width disagreement is real
    evidence and must still reject - even though the other two count 1."""
    rules = [_rule(139, "CO_CONTRACT", ext="*", cols=70)]
    f = _write(tmp_path, "co_contract", content=_CO_CONTRACT_REAL)  # pipe parses to 69

    with pytest.raises(ColumnCountMismatch):
        match_file(f, rules)


def test_a_single_column_rule_still_matches_normally(tmp_path):
    """A rule that genuinely declares 1 column matches on the width and never
    reaches the skip - the skip must not be what makes these files pass."""
    rules = [_rule(90, "ISIN_LIST", ext="TXT", cols=1)]
    f = _write(tmp_path, "ISIN_LIST_110826.txt", content="INE002A01018\nINE009A01021\n")

    assert match_file(f, rules).upload_id == "90"


def test_an_empty_file_is_still_rejected_not_skipped(tmp_path):
    """A blank placeholder has no widths at all, so it must land on EmptyFile
    rather than slip through the "all widths are 1" hatch."""
    from app.services.upload_matching import EmptyFile

    rules = [_rule(81, "SCRIP", ext="TXT", cols=30, name="BSE SCRIP")]
    f = _write(tmp_path, "SCRIP_CC_110826.txt", content="\n   \n\n")

    with pytest.raises(EmptyFile):
        match_file(f, rules)


# ---------------------------------------------------------------------------
# Step-40 fallback for a slot whose Step-4 pattern is blank
# ---------------------------------------------------------------------------


def test_blank_step4_pattern_falls_back_to_step40(monkeypatch):
    """NCDEXPHY/482 live: Step 4 returns 'FILE NAME (CONTAINS)': '' while Step
    40 returns '%%'. Dropping the slot strands SS06, which CBOS lists as a
    MANDATORY Table2 slot - so the batch can never complete."""
    from app.clients import cbos_client as cc

    row = {
        "ID": 482,
        "NAME": "NCDEX DELIVERY FILE - SS06",
        "FILE NAME (CONTAINS)": "",
        "FILEEXTENSION": "CSV",
        "NO. OF COLUMNS": 9,
    }
    assert cc._parse_upload_rule("482", row, "SS06") is None  # today's behaviour

    rule = cc._parse_upload_rule("482", row, "SS06", override_pattern="")
    assert rule is not None and rule.upload_id == "482"
    assert rule.file_name_pattern == ""  # '%%' means "no filename constraint"
    assert rule.column_count == 9


def test_wildcard_slot_is_a_last_resort_never_a_thief(tmp_path):
    """An empty pattern matches anything, so the only thing protecting the real
    slots is longest-pattern-wins. AL02 must still go to 321, not 482."""
    rules = [
        _rule(321, "NCDEX_AL02_01240_", ext="CSV", cols=20),
        _rule(482, "", ext="CSV", cols=9),  # the '%%' slot
    ]
    al02 = _write(tmp_path, "NCDEX_AL02_01240_17062026.CSV", content=_AL02_REAL)
    ss06 = _write(
        tmp_path,
        "NCDEX_SS06_01240_D2026016.CSV",
        content="SS06,01240,17062026,D,2026016,000002,1,0,0,0,1\n36,GUARGUM5,JODHPUR,,35,0,0.00,4078637.50,M51085\n",
    )

    assert match_file(al02, rules).upload_id == "321", "the specific slot must win"
    assert match_file(ss06, rules).upload_id == "482", "the leftover file takes the wildcard slot"


def test_step40_concrete_filename_is_refused(monkeypatch):
    """Step 40 answers in two shapes. EQ/81 returns 'SCRIP_030826.TXT' - a
    literal name whose date Step 40 computes from the SERVER's clock, since the
    request carries no trade date. Adopting it would reject valid files on every
    backfill, so only wildcard-delimited values are usable."""
    from app.clients.cbos_client import CBOSClient

    captured = {}

    class _Probe(CBOSClient):
        def __init__(self):
            pass

        def get_expected_filename(self, segment, upload_id, trade_date=""):
            captured["asked"] = (segment, upload_id)
            return {"Status": "Success", "Data": [{"ExpectedFileNamePattern1": "SCRIP_030826.TXT"}]}

    assert _Probe()._expected_name_pattern("EQ", "81") is None
    assert captured["asked"] == ("EQ", "81")


def test_step40_wildcard_shapes_are_stripped(monkeypatch):
    """The two usable shapes, both measured against real CBOS."""
    from app.clients.cbos_client import CBOSClient

    class _Probe(CBOSClient):
        def __init__(self, value):
            self._value = value

        def get_expected_filename(self, segment, upload_id, trade_date=""):
            return {"Status": "Success", "Data": [{"ExpectedFileNamePattern1": self._value}]}

    assert (
        _Probe("%NCDEX_AL02_01240_%")._expected_name_pattern("NCDEXPHY", "321")
        == "NCDEX_AL02_01240_"
    )
    assert _Probe("%%")._expected_name_pattern("NCDEXPHY", "482") == ""


def test_step40_failure_never_breaks_the_batch(monkeypatch):
    """An optional cross-check must not turn a working batch into a failed one."""
    from app.clients.cbos_client import CBOSClient

    class _Boom(CBOSClient):
        def __init__(self):
            pass

        def get_expected_filename(self, segment, upload_id, trade_date=""):
            raise RuntimeError("CBOS down")

    assert _Boom()._expected_name_pattern("NCDEXPHY", "482") is None


def test_pattern_matching_is_case_insensitive_because_cbos_is():
    """Proved live on 2026-08-22 against CBOS UAT.

    CBOS holds `ICCLFINAL_VARELMAM_` for UploadID 681 while BSE publishes
    `ICCLFinal_VARELMAM_170826.csv`. A case-SENSITIVE contains rejects that file; CBOS accepts
    it under its real name. CBOS runs SQL Server, whose default collation is case-insensitive,
    so the strict comparison was ours alone and stricter than the system it models.

    It failed in the worst available way: a name mismatch is a 422, which the billing engine
    treats as PERMANENT and fails the whole process on rather than retrying. One file CBOS
    would have taken could kill a Collateral Valuation run every night, reporting a
    configuration error that did not exist.
    """
    assert _pattern_matches("ICCLFINAL_VARELMAM_", "CONTAINS", "ICCLFinal_VARELMAM_170826.csv")
    # ...and the other direction, since either side can carry the odd casing.
    assert _pattern_matches("iccl", "CONTAINS", "ICCLFinal_VARELMAM_170826.csv")
    assert _pattern_matches("BSE_CM", "LIKE", "bhavcopy_bse_cm_0_0_0_20260817_f_0000.csv")


def test_case_insensitivity_applies_to_every_operator():
    """The collation is a property of CBOS's database, not of one comparison, so a rule that
    happens to use EQUALS or STARTS WITH must behave the same way."""
    assert _pattern_matches("NNF_SECURITY", "EQUAL", "nnf_security")
    assert _pattern_matches("br", "STARTSWITH", "BR220626")
    assert _pattern_matches(".CSV", "ENDSWITH", "somefile.csv")


def test_case_insensitivity_does_not_make_everything_match():
    """It loosens the comparison; it must not empty it. A genuinely different name still fails,
    which is what keeps a slot pointed at the wrong UploadID from uploading cleanly."""
    assert not _pattern_matches("ICCLFINAL_VARELMAM_", "CONTAINS", "CB_Haircut_17082026.CSV")
    assert not _pattern_matches("nnf_security", "EQUAL", "nnf_security_1")
    assert not _pattern_matches("BR", "STARTSWITH", "XBR220626")
