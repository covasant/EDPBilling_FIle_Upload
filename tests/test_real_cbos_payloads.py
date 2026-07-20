"""Parsing pinned to responses captured from the real CBOS UAT server
(10.167.202.164, 2026-07-20). Verbatim - do not tidy these payloads.

These exist because the mock was written from the API documentation, and the
documentation was wrong about the Step-4 pattern key. Every rule was silently
skipped and every file would have routed to uploadFailed/, with a fully green
suite, because the mock encoded the same wrong key. Anything captured from the
real server belongs here.
"""

from app.clients.cbos_client import MockCBOSClient, _parse_upload_rule as parse_upload_rule
from app.services.upload_matching import match_file

# Verbatim Result[0] from POST /v1/api/process/GetNewTradeProcessPromodalUploadSettings {"UPLOADID":"127"}
REAL_UPLOAD_SETTINGS_127 = {
    "ID": 127,
    "NAME": "CONTRACT MASTER - MCXCOM",
    "SAMPLE FILE": '<a href="https://bizops.motilaloswal.com/pdf/Commodity.jpg"   target="_blank">Download</a>',
    "FILE NAME (CONTAINS)": "MCX_PRODUCTMASTER",
    "FILEEXTENSION": "CSV",
    "NO. OF COLUMNS": 68,
}

# Verbatim Result from POST /v1/api/process/getNewTradeProcess {"PROCESSID":"17739", ...}, trimmed to
# the first four Table2 rows - the three that expect a file, plus the first that does not.
REAL_RESERVE_RESULT = {
    "Table1": [{"PROCESSID": 17739, "ISRUNNABLE": True, "ISAUTOUPLOAD": False}],
    "Table2": [
        {"ID": 144324, "STEPNO": 1, "NAME": "Contract Master Upload - MCXCOM", "STATUS": "PENDING",
         "CREATEDBY": "CV0001", "ISOPTIONAL": False, "UPLOADID": 127, "ISOPTIONALVISIBLE": False},
        {"ID": 144326, "STEPNO": 2, "NAME": "MCX COM Trade File Upload - UDIFF", "STATUS": "PENDING",
         "CREATEDBY": "CV0001", "ISOPTIONAL": False, "UPLOADID": 535, "ISOPTIONALVISIBLE": False},
        {"ID": 144325, "STEPNO": 3, "NAME": "Position File Upload - UDIFF", "STATUS": "PENDING",
         "CREATEDBY": "CV0001", "ISOPTIONAL": False, "UPLOADID": 534, "ISOPTIONALVISIBLE": False},
        {"ID": 144340, "STEPNO": 4, "NAME": "Trade data Merger", "STATUS": "PENDING",
         "CREATEDBY": "CV0001", "ISOPTIONAL": False, "UPLOADID": 0, "ISOPTIONALVISIBLE": False},
    ],
}


class _RealPayloadClient(MockCBOSClient):
    def _get_new_trade_process(self, segment, trade_date):
        return {"Status": "Success", "Result": REAL_RESERVE_RESULT, "filename": None, "PDFData": None}

    def _get_upload_settings(self, upload_id):
        return {"Status": "Success", "Result": [REAL_UPLOAD_SETTINGS_127], "filename": None, "PDFData": None}


# --- Step 4 --------------------------------------------------------------------

def test_real_step4_row_produces_a_usable_rule():
    """The regression. Real CBOS sends "FILE NAME (CONTAINS)", not "FILE NAME";
    parsing it as None skipped every rule and failed every file."""
    rule = parse_upload_rule("127", REAL_UPLOAD_SETTINGS_127)
    assert rule is not None, "real CBOS settings row must produce a rule"
    assert rule.file_name_pattern == "MCX_PRODUCTMASTER"
    assert rule.extension == "CSV"
    assert rule.column_count == 68
    assert rule.name == "CONTRACT MASTER - MCXCOM"


def test_operator_comes_from_the_key_name():
    """Real CBOS sends no FileNameCompareOperator field at all - the semantics
    are the parenthetical in the key."""
    assert "FileNameCompareOperator" not in REAL_UPLOAD_SETTINGS_127
    assert parse_upload_rule("127", REAL_UPLOAD_SETTINGS_127).compare_operator == "CONTAINS"


def test_a_real_mcx_file_matches_the_real_rule(tmp_path):
    rule = parse_upload_rule("127", REAL_UPLOAD_SETTINGS_127)
    f = tmp_path / "MCX_PRODUCTMASTER_17072026.csv"
    f.write_text(",".join(str(i) for i in range(68)) + "\n")
    assert match_file(f, [rule]).upload_id == "127"


def test_html_in_sample_file_does_not_confuse_the_parser():
    """The SAMPLE FILE field carries an HTML anchor; it must not be mistaken
    for a pattern field."""
    assert parse_upload_rule("127", REAL_UPLOAD_SETTINGS_127).file_name_pattern == "MCX_PRODUCTMASTER"


# --- Step 2 --------------------------------------------------------------------

def test_real_reserve_response_parses():
    reservation = _RealPayloadClient().reserve_process("MCX", "14-07-2026")
    assert reservation.process_id == "17739"
    assert [c.upload_id for c in reservation.candidates] == ["127", "535", "534", "0"]
    assert [c.step_no for c in reservation.candidates] == [1, 2, 3, 4]


def test_real_zero_uploadid_rows_expect_no_file():
    """Real MCX Table2 is mostly UPLOADID=0 pipeline steps. Marking those
    optional at Step 8 would be wrong - they never take a file."""
    reservation = _RealPayloadClient().reserve_process("MCX", "14-07-2026")
    expecting = [c.upload_id for c in reservation.candidates if c.expects_a_file]
    assert expecting == ["127", "535", "534"]


# --- double-encoded bodies ------------------------------------------------------
# CBOS returns its payload as a JSON *string* holding a JSON document, not the
# document itself. requests' .json() yields a str for that, and every .get()
# downstream dies with AttributeError - which is NOT CBOSUploadError, so it
# escapes process_batch's setup retry loop, the files are never routed to
# uploadFailed/, and the next scan rediscovers them forever.

import json

import pytest

from app.clients.cbos_client import CBOSUploadError, _decode_body


class _DoubleEncodedClient(MockCBOSClient):
    """Returns bodies exactly as the UAT server does - one extra JSON layer."""

    def _get_new_trade_process(self, segment, trade_date):
        return json.dumps(json.dumps({"Status": "Success", "Result": REAL_RESERVE_RESULT}))

    def _get_upload_settings(self, upload_id):
        return json.dumps(json.dumps({"Status": "Success", "Result": [REAL_UPLOAD_SETTINGS_127]}))

    def _file_upload_status(self, segment):
        return json.dumps(json.dumps({"Status": "Success", "Data": [{"MSG": "TRUE"}]}))


def test_double_encoded_reserve_still_parses():
    reservation = _DoubleEncodedClient().reserve_process("MCX", "14-07-2026")
    assert reservation.process_id == "17739"
    assert [c.upload_id for c in reservation.candidates] == ["127", "535", "534", "0"]


def test_double_encoded_upload_settings_still_parses():
    rule = _DoubleEncodedClient().upload_settings("127")
    assert rule.file_name_pattern == "MCX_PRODUCTMASTER"
    assert rule.compare_operator == "CONTAINS"
    assert rule.column_count == 68


def test_double_encoded_gtg_still_parses(monkeypatch):
    monkeypatch.setenv("CBOS_POLL_INTERVAL_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()
    assert _DoubleEncodedClient().confirm_upload("MCX") is True


def test_singly_encoded_bodies_are_untouched():
    """The unwrap must be a no-op when CBOS behaves."""
    body = {"Status": "Success", "Result": {"a": 1}}
    assert _decode_body(body, "x") is body


def test_a_non_object_body_raises_cbos_error_not_attribute_error():
    """The whole point: a bad body must fail as CBOSUploadError so the setup
    retry loop catches it and the batch fails cleanly, instead of looping."""
    for bad in ('"just a string"', json.dumps([1, 2, 3]), json.dumps(None)):
        with pytest.raises(CBOSUploadError):
            _decode_body(json.loads(bad), "x")


def test_a_string_that_is_not_json_raises_cbos_error():
    with pytest.raises(CBOSUploadError, match="not JSON"):
        _decode_body("<html>502 Bad Gateway</html>", "x")
