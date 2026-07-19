"""Reference data for the mock CBOS v4 server, drawn from
docs/EDP_Trade_Process_API_Documentation_v4.docx and
docs/EDPFILEUPLOADSETTING.xlsx.

Three lookups:
  SEGMENT_TABLE2   - the Step-2 `Table2` pipeline per segment (STEPNO, NAME,
                     STATUS, UPLOADID). A non-zero UPLOADID means "a file is
                     expected at this step".
  UPLOAD_SETTINGS  - the Step-4 per-UPLOADID rules (NAME, FILE NAME pattern,
                     FILEEXTENSION, NO. OF COLUMNS).
  EXPECTED_PATTERN - the Step-39 expected-filename pattern per UPLOADID.

Only the segments we actively test (MCX, EQ) carry a full Table2; every other
segment falls back to GENERIC_TABLE2 so the server still answers.
"""

from __future__ import annotations

# --- Step 4 upload settings (UPLOADID -> rule) ---------------------------------
# Mirrors the real EDPFILEUPLOADSETTING rows. FILEEXTENSION is sometimes not a
# real extension (e.g. "446", "M01") - kept verbatim, as the real sheet has it.
UPLOAD_SETTINGS: dict[str, dict] = {
    # EQ TRADE
    "81": {"NAME": "BSE SCRIP", "FILE NAME": "SCRIP", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "TXT", "NO. OF COLUMNS": 30},
    "82": {"NAME": "NSE SCRIP", "FILE NAME": "nnf_security", "FileNameCompareOperator": "EQUAL", "FILEEXTENSION": "DAT", "NO. OF COLUMNS": 54},
    "83": {"NAME": "NSE BSE INTEROPERABLE SCRIP MAPPING", "FILE NAME": "bse_scrip_series_mapping", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "CSV", "NO. OF COLUMNS": 6},
    "84": {"NAME": "STT INDICATOR", "FILE NAME": "C_STT_IND", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "CSV", "NO. OF COLUMNS": 5},
    "85": {"NAME": "BSE TRADE FILE", "FILE NAME": "BR", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "446", "NO. OF COLUMNS": 32},
    "86": {"NAME": "NSE TRADE FILE", "FILE NAME": "_10412", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "TXT", "NO. OF COLUMNS": 25},
    "94": {"NAME": "STT NOT TO CHARGE", "FILE NAME": "C_STT", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "CSV", "NO. OF COLUMNS": 4},
    "451": {"NAME": "BSE AUCTION TRADE FILE", "FILE NAME": "AOFR", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "446", "NO. OF COLUMNS": 23},
    "545": {"NAME": "NSE EQ TRADE FILE - UDIFF", "FILE NAME": "Trade_NSE_CM_0_TM_10412", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "csv", "NO. OF COLUMNS": 46},
    "546": {"NAME": "BSE EQ TRADE FILE - UDIFF", "FILE NAME": "Trade_BSE_CM_0_TM_446", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "csv", "NO. OF COLUMNS": 46},
    "551": {"NAME": "SETTLEMENT MASTER NCL - UDIFF", "FILE NAME": "SettlementMaster_NCL_CM", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "csv", "NO. OF COLUMNS": 23},
    "678": {"NAME": "SETTLEMENT MASTER ICCL - UDIFF", "FILE NAME": "SettlementMaster_ICCL_CM", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "csv", "NO. OF COLUMNS": 23},
    # MCX TRADE
    "127": {"NAME": "CONTRACT MASTER - MCXCOM", "FILE NAME": "MCX_PRODUCTMASTER", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "CSV", "NO. OF COLUMNS": 68},
    "128": {"NAME": "POSITION FILE MCX COM", "FILE NAME": "MCX_POSITION", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "CSV", "NO. OF COLUMNS": 33},
    "129": {"NAME": "MCX COM TRADE FILE", "FILE NAME": "MCX_TRD", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "CSV", "NO. OF COLUMNS": 37},
    "534": {"NAME": "POSITION FILE MCX COM - UDIFF", "FILE NAME": "MCXCCL_CO_0_CM_55930", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "csv", "NO. OF COLUMNS": 46},
    "535": {"NAME": "MCX COM TRADE FILE - UDIFF", "FILE NAME": "MCX_CO_0_CM_55930", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "csv", "NO. OF COLUMNS": 46},
    "320": {"NAME": "MCX Physical Trade File", "FILE NAME": "MCX_EXDI_55930_", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "csv", "NO. OF COLUMNS": 19},
    "221": {"NAME": "MCDX Peak File", "FILE NAME": "MCX_PeakMargin", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "*", "NO. OF COLUMNS": 12},
    "222": {"NAME": "MCDX EOD File", "FILE NAME": "MCX_MARGIN_", "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "*", "NO. OF COLUMNS": 19},
}


def upload_setting(upload_id: str) -> dict:
    """Step-4 rule for an UPLOADID, with a generic fallback."""
    row = UPLOAD_SETTINGS.get(str(upload_id))
    if row is None:
        return {"NAME": f"UPLOAD {upload_id}", "FILE NAME": f"UPLOAD{upload_id}",
                "FileNameCompareOperator": "LIKE", "FILEEXTENSION": "TXT", "NO. OF COLUMNS": 0}
    return row


# --- Step 2 Table2 per segment -------------------------------------------------
# STATUS starts PENDING; a non-zero UPLOADID means a file is expected there.
# MCX carries a 4th non-zero step (320, the Physical file) that has NO file on a
# normal day - so the happy path REQUIRES marking it optional via Step 8, exactly
# reproducing the real "MSG=FALSE until you skip the no-file mandatory steps".
MCX_TABLE2 = [
    {"STEPNO": 1, "NAME": "MCX Product Master Upload", "STATUS": "PENDING", "UPLOADID": 127},
    {"STEPNO": 2, "NAME": "MCX Position File Upload (UDIFF)", "STATUS": "PENDING", "UPLOADID": 534},
    {"STEPNO": 3, "NAME": "MCX Trade File Upload (UDIFF)", "STATUS": "PENDING", "UPLOADID": 535},
    {"STEPNO": 4, "NAME": "MCX Physical Trade File Upload", "STATUS": "PENDING", "UPLOADID": 320},
    {"STEPNO": 5, "NAME": "MCX Brokerage Computation", "STATUS": "PENDING", "UPLOADID": 0},
    {"STEPNO": 6, "NAME": "MCX Bill Posting", "STATUS": "PENDING", "UPLOADID": 0},
]

EQ_TABLE2 = [
    {"STEPNO": 1, "NAME": "Settlement Master NSE Upload", "STATUS": "PENDING", "UPLOADID": 551},
    {"STEPNO": 2, "NAME": "Settlement Master BSE Upload", "STATUS": "PENDING", "UPLOADID": 678},
    {"STEPNO": 3, "NAME": "BSE Scrip Upload", "STATUS": "PENDING", "UPLOADID": 81},
    {"STEPNO": 4, "NAME": "NSE Scrip Upload", "STATUS": "PENDING", "UPLOADID": 82},
    {"STEPNO": 5, "NAME": "STT Indicator Upload", "STATUS": "PENDING", "UPLOADID": 84},
    {"STEPNO": 6, "NAME": "STT not to Charge Upload", "STATUS": "PENDING", "UPLOADID": 94},
    {"STEPNO": 7, "NAME": "BSE Trade File Upload (UDIFF)", "STATUS": "PENDING", "UPLOADID": 546},
    {"STEPNO": 8, "NAME": "NSE Trade File Upload (UDIFF)", "STATUS": "PENDING", "UPLOADID": 545},
    {"STEPNO": 9, "NAME": "BSE Auction Trade File Upload", "STATUS": "PENDING", "UPLOADID": 451},
    {"STEPNO": 10, "NAME": "Brokerage / SEBI / STT Computation", "STATUS": "PENDING", "UPLOADID": 0},
    {"STEPNO": 11, "NAME": "Bill Posting", "STATUS": "PENDING", "UPLOADID": 0},
]

GENERIC_TABLE2 = [
    {"STEPNO": 1, "NAME": "Trade File Upload", "STATUS": "PENDING", "UPLOADID": 999},
    {"STEPNO": 2, "NAME": "Bill Posting", "STATUS": "PENDING", "UPLOADID": 0},
]

SEGMENT_TABLE2: dict[str, list[dict]] = {
    "MCX": MCX_TABLE2,
    "EQ": EQ_TABLE2,
}


def table2_for(segment: str) -> list[dict]:
    """A fresh copy of the segment's Table2 (so per-process STATUS edits don't
    leak across processes)."""
    template = SEGMENT_TABLE2.get(segment.upper(), GENERIC_TABLE2)
    return [dict(row) for row in template]


def expected_pattern(upload_id: str) -> str:
    """Step-39 expected filename pattern (DDMMYY embedded), derived from the
    settings row."""
    row = upload_setting(upload_id)
    pattern = row.get("FILE NAME", "")
    ext = str(row.get("FILEEXTENSION", "TXT")).lower()
    return f"{pattern}_DDMMYY.{ext}"
