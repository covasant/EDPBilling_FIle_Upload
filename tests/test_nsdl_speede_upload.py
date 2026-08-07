"""End-to-end tests for the NSDL SPEED-e upload API, against the in-process
MOCK client. Mirrors tests/test_settlements_api.py's structure.

The CSV fixtures reproduce the real 03-08-2026 SPEED-e exports' shape exactly:
one header row, no BOM, LF endings, unquoted fields, and - critically - NO
trailing newline, including the header-only case a quiet day produces.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Column counts the live API expects, read off UAT 2026-08-06 and mirrored in
# MockNsdlSpeedeClient's _MOCK_CATEGORIES.
#
# NOTE 18 and 45 are what the SPEED-e exports actually have; 57 and 55 are NOT
# - the bot's Invoke/Release screens produce 30 and 29 columns. These tests
# cover the happy path, so they use the counts the API wants. The real
# confiscate/unpledge files are rejected by validate_file until the download
# side is pointed at the "_Initiated" exports, which is the correct outcome and
# is covered by test_wrong_column_count_is_rejected_before_a_tran_id_is_burned.
_HEADERS = {
    "OPEN HOLDING": ",".join(f"c{i}" for i in range(18)),
    "pledge": ",".join(f"c{i}" for i in range(45)),
    "confiscate": ",".join(f"c{i}" for i in range(57)),
    "unpledge": ",".join(f"c{i}" for i in range(55)),
}


def _fast(monkeypatch):
    monkeypatch.setenv("NSDL_SPEEDE_MOCK_PENDING_POLLS", "0")
    monkeypatch.setenv("NSDL_SPEEDE_POLL_INTERVAL_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()


@pytest.fixture()
def client(monkeypatch):
    _fast(monkeypatch)
    import app.main as main_module

    def _no_worker(queue):  # billing's worker thread body; not relevant here
        return

    monkeypatch.setattr(main_module, "run_worker", _no_worker)
    with TestClient(main_module.app) as c:
        yield c


TRADE_DATE = "2026-08-05"


def _folder() -> Path:
    """The dated sub-folder for TRADE_DATE - the configured path is the ROOT,
    matching how the download bot lays runs out."""
    from app.services.nsdl_speede_service import day_folder

    folder = day_folder(TRADE_DATE)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _drop(account: str, report: str, rows: int = 2) -> Path:
    """Write one report the way the download bot does, with no trailing
    newline - exactly how every real SPEED-e export ends."""
    header = _HEADERS[report]
    columns = len(header.split(","))
    body = [",".join(f"v{r}_{c}" for c in range(columns)) for r in range(rows)]
    path = _folder() / f"NSDL {account} {report}.csv"
    path.write_bytes("\n".join([header, *body]).encode())
    return path


def _drop_all(rows: int = 2) -> None:
    from app.services.nsdl_speede_service import CATALOGUE

    for entry in CATALOGUE:
        _drop(entry.account, entry.report, rows)


def _post(client, **body):
    body.setdefault("trade_date", TRADE_DATE)
    return client.post("/settlement/nsdl_speede_upload", json=body)


# ---- the catalogue ---------------------------------------------------------


def test_catalogue_is_the_12_files_in_account_major_order():
    from app.services.nsdl_speede_service import CATALOGUE

    assert len(CATALOGUE) == 12
    assert [(e.account, e.report) for e in CATALOGUE][:5] == [
        ("CMPA", "OPEN HOLDING"),
        ("CMPA", "pledge"),
        ("CMPA", "unpledge"),
        ("CMPA", "confiscate"),
        ("CMFA", "OPEN HOLDING"),
    ]


def test_open_holding_has_a_category_per_account_but_the_rest_share_one():
    from app.services.nsdl_speede_service import CATALOGUE

    names = {(e.account, e.report): e.upload_name for e in CATALOGUE}
    assert names[("CMPA", "OPEN HOLDING")] == "NSDL CMPA OPEN HOLDING"
    assert names[("CMFA", "OPEN HOLDING")] == "NSDL CMFA OPEN HOLDING"
    assert names[("NARNO", "OPEN HOLDING")] == "NSDL NARNO OPEN HOLDING"
    # All three accounts' pledge files feed one category - three appends.
    assert {names[(a, "pledge")] for a in ("CMPA", "CMFA", "NARNO")} == {"NSDL MARGIN PLEDGE"}


def test_file_names_match_the_download_bots_naming():
    from app.services.nsdl_speede_service import CATALOGUE

    names = {e.file_name for e in CATALOGUE}
    assert "NSDL CMPA OPEN HOLDING.csv" in names
    assert "NSDL NARNO confiscate.csv" in names


# ---- the happy path --------------------------------------------------------


def test_single_file_upload_reaches_success(client):
    _drop("CMPA", "pledge")
    resp = _post(client, files=[{"account": "CMPA", "report": "pledge"}])

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["summary"] == {"total": 1, "success": 1, "in_progress": 0, "failed": 0}

    file_result = body["files"][0]
    assert file_result["upload_name"] == "NSDL MARGIN PLEDGE"
    assert file_result["upload_id"] == 7  # resolved from the name, not hardcoded
    assert file_result["tran_id"]
    assert file_result["process_status"] == "SUCCESS"
    # HTML stripped out of both status fields.
    assert "<" not in file_result["upload_status"]


def test_all_twelve_upload_in_one_call(client):
    _drop_all()
    resp = _post(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["summary"]["total"] == 12
    assert body["summary"]["success"] == 12
    assert len({f["tran_id"] for f in body["files"]}) == 12


def test_transmitted_name_carries_the_apis_required_token(client):
    _drop("CMFA", "OPEN HOLDING")
    resp = _post(client, files=[{"account": "CMFA", "report": "OPEN HOLDING"}])

    file_result = resp.json()["files"][0]
    # The on-disk name cannot satisfy FILE NAME (CONTAINS); the transmitted one
    # is built from the live token instead, account-qualified so three accounts
    # feeding one category stay distinguishable in the history log.
    assert file_result["file_name"] == "NSDL CMFA OPEN HOLDING.csv"
    assert file_result["transmit_file_name"] == "View_Open_Holdings_CMFA.csv"


# ---- the header strip ------------------------------------------------------


def test_header_row_is_stripped_before_the_bytes_go_out(client):
    path = _drop("CMPA", "OPEN HOLDING", rows=3)
    header_len = len(_HEADERS["OPEN HOLDING"]) + 1  # + the LF

    resp = _post(client, files=[{"account": "CMPA", "report": "OPEN HOLDING"}])

    file_result = resp.json()["files"][0]
    # + 1 for the closing newline the fixture (like every real export) lacks.
    assert file_result["data_bytes"] == path.stat().st_size - header_len + 1


def test_a_closing_newline_is_added_so_the_last_row_is_not_dropped(client):
    # CBOS discards an unterminated final line: UPLOADID 24 loaded 72,921 rows
    # from a file carrying 72,922 (UAT 2026-08-06, TRANID 339086). No SPEED-e
    # export ends with a terminator, so one is appended on the wire.
    path = _drop("CMFA", "OPEN HOLDING", rows=5)
    assert not path.read_bytes().endswith(b"\n"), "fixture must mimic a real export"

    _post(client, files=[{"account": "CMFA", "report": "OPEN HOLDING"}])

    from app.clients.nsdl_speede_client import get_nsdl_speede_client

    sent = sum(size for _guid, _chunk, size in get_nsdl_speede_client().chunk_calls)
    body = path.read_bytes().split(b"\n", 1)[1]
    assert sent == len(body) + 1
    assert sent == len(body + b"\n")


def test_no_newline_is_added_when_the_file_already_ends_with_one(client):
    path = _folder() / "NSDL CMPA OPEN HOLDING.csv"
    header = _HEADERS["OPEN HOLDING"]
    row = ",".join(f"v{c}" for c in range(18))
    path.write_bytes(f"{header}\n{row}\n".encode())  # already terminated

    resp = _post(client, files=[{"account": "CMPA", "report": "OPEN HOLDING"}])

    assert resp.json()["files"][0]["data_bytes"] == len(row) + 1


def test_header_only_export_uploads_one_empty_chunk(client):
    # A quiet day: login_3 produced header-only confiscate/unpledge exports in
    # the real 03-08-2026 sample set. These still upload, so the stored
    # procedure runs and records a zero-row day.
    _drop("NARNO", "confiscate", rows=0)
    resp = _post(client, files=[{"account": "NARNO", "report": "confiscate"}])

    body = resp.json()
    assert body["status"] == "success"
    file_result = body["files"][0]
    assert file_result["data_bytes"] == 0
    assert file_result["total_chunks"] == 1


def test_strip_can_be_turned_off(client, monkeypatch):
    path = _drop("CMPA", "unpledge")
    monkeypatch.setenv("NSDL_SPEEDE_STRIP_HEADER", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()

    resp = _post(client, files=[{"account": "CMPA", "report": "unpledge"}])
    # Whole file including its header, + the closing newline it lacks.
    assert resp.json()["files"][0]["data_bytes"] == path.stat().st_size + 1


# ---- validation (this API has no server-side validate call) ----------------


def test_wrong_column_count_is_rejected_before_a_tran_id_is_burned(client):
    path = _folder() / "NSDL CMPA OPEN HOLDING.csv"
    path.write_bytes(b"only,three,columns\n1,2,3")

    resp = _post(client, files=[{"account": "CMPA", "report": "OPEN HOLDING"}])

    body = resp.json()
    assert body["status"] == "failed"
    file_result = body["files"][0]
    assert file_result["tran_id"] is None  # nothing was appended
    assert "18" in file_result["detail"]


def test_missing_file_fails_only_that_file(client):
    _drop_all()
    (_folder() / "NSDL CMFA pledge.csv").unlink()

    body = _post(client).json()

    assert body["status"] == "partial"
    assert body["summary"] == {"total": 12, "success": 11, "in_progress": 0, "failed": 1}
    failed = [f for f in body["files"] if f["status"] == "failed"]
    assert [(f["account"], f["report"]) for f in failed] == [("CMFA", "pledge")]


def test_unknown_selector_is_a_400(client):
    resp = _post(client, files=[{"account": "CMPA", "report": "not-a-report"}])
    assert resp.status_code == 400


# ---- the dated folder -------------------------------------------------------


def test_files_are_read_from_the_bots_dated_folder(client):
    from app.core.config import settings

    _drop("CMPA", "pledge")
    root = Path(settings.nsdl_speede_shared_folder_path)
    # The configured path is the ROOT; the day's folder sits under it, named
    # exactly as the download bot names its run directory.
    assert (root / "nsdl_speede_05082026" / "NSDL CMPA pledge.csv").is_file()

    assert _post(client).json()["summary"]["success"] >= 1


def test_a_day_with_no_download_folder_fails_with_a_pointed_message(client):
    body = _post(client, trade_date="2026-08-04")  # bot never ran for this day

    file_result = body.json()["files"][0]
    assert file_result["status"] == "failed"
    assert "nsdl_speede_04082026" in file_result["detail"]


def test_a_non_iso_trade_date_is_rejected(client):
    # DD-MM-YYYY would resolve to the wrong folder AND the wrong PARAM1.
    assert _post(client, trade_date="05-08-2026").status_code == 422


# ---- idempotency: the rule that protects against duplicated rows -----------


def test_a_completed_file_is_never_re_uploaded(client):
    _drop("CMPA", "confiscate")
    first = _post(client, files=[{"account": "CMPA", "report": "confiscate"}]).json()

    from app.clients.nsdl_speede_client import get_nsdl_speede_client

    chunks_after_first = len(get_nsdl_speede_client().chunk_calls)

    second = _post(client, files=[{"account": "CMPA", "report": "confiscate"}]).json()

    assert second["status"] == "success"
    assert second["files"][0]["detail"] == "already uploaded"
    # Same TranId, and not one further chunk on the wire. This API appends: a
    # second upload would duplicate every row in the file.
    assert second["files"][0]["tran_id"] == first["files"][0]["tran_id"]
    assert len(get_nsdl_speede_client().chunk_calls) == chunks_after_first


def test_a_retry_of_an_in_progress_file_re_polls_instead_of_re_uploading(client, monkeypatch):
    monkeypatch.setenv("NSDL_SPEEDE_MOCK_PENDING_POLLS", "5")
    monkeypatch.setenv("NSDL_SPEEDE_POLL_MAX_ATTEMPTS", "2")
    from app.core.config import get_settings

    get_settings.cache_clear()

    _drop("CMPA", "pledge")
    first = _post(client, files=[{"account": "CMPA", "report": "pledge"}]).json()
    assert first["status"] == "in_progress"
    assert first["files"][0]["tran_id"]

    from app.clients.nsdl_speede_client import get_nsdl_speede_client

    chunks_after_first = len(get_nsdl_speede_client().chunk_calls)

    # What the workflow's retry does after a non-success response.
    second = _post(client, files=[{"account": "CMPA", "report": "pledge"}]).json()

    assert second["files"][0]["tran_id"] == first["files"][0]["tran_id"]
    assert len(get_nsdl_speede_client().chunk_calls) == chunks_after_first


def test_status_endpoint_resolves_an_in_progress_file(client, monkeypatch):
    monkeypatch.setenv("NSDL_SPEEDE_MOCK_PENDING_POLLS", "1")
    monkeypatch.setenv("NSDL_SPEEDE_POLL_MAX_ATTEMPTS", "1")
    from app.core.config import get_settings

    get_settings.cache_clear()

    _drop("NARNO", "unpledge")
    first = _post(client, files=[{"account": "NARNO", "report": "unpledge"}]).json()
    assert first["status"] == "in_progress"

    upload_id = first["files"][0]["settlement_upload_id"]
    resp = client.get(f"/settlement/nsdl_speede_upload/{upload_id}")

    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_status_endpoint_404s_on_an_unknown_id(client):
    assert client.get("/settlement/nsdl_speede_upload/999").status_code == 404


# ---- the settlements API is untouched --------------------------------------


def test_settlements_endpoints_still_work(client):
    assert client.get("/settlements/upload-masters").status_code == 200
