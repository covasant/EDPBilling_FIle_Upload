"""V5 contract tests for the standalone mock CBOS server (mock_cbos/app.py).

The in-process MockCBOSClient got its V5 behaviour together with the client
(existing-PID reuse, STATUS readback, ISRUNNABLE); these tests pin the same
contract onto the HTTP server so runs against it (Postman, CBOS_MODE=REAL
pointed at localhost, the RPA side's testing) see identical semantics:

  - getdropdown(EXISTINGPROCESSID) resolves per (FILTER1=segment,
    FILTER2=trade date) - yesterday's PID must not leak into today's batch;
  - getNewTradeProcess with a real PROCESSID *re-fetches* (real per-slot
    STATUS/STATUSDESC, ISAUTOUPLOAD=False) and must NOT trigger while
    mandatory upload slots are unsatisfied;
  - file_process_status carries TradeDate (Shape A) and the Step-1 holiday
    check answers from the payload alone - before any process exists.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mock_cbos.app import app
from mock_cbos.state import STATE


@pytest.fixture()
def client():
    STATE.reset()
    with TestClient(app) as c:
        yield c
    STATE.reset()


def _reserve(client: TestClient, segment: str = "MCX", trade_date: str = "2026-07-20") -> dict:
    resp = client.post("/v1/api/process/getNewTradeProcess", json={
        "GROUPNAME": segment, "LOGINID": "CV0001", "PASSWORD": "x",
        "TRADEDATE": trade_date, "PROCESSID": "0",
    })
    assert resp.status_code == 200
    return resp.json()["Result"]


def _dropdown(client: TestClient, segment: str, trade_date: str) -> list:
    resp = client.post("/v1/api/brokerage/getdropdown", json={
        "TAG": "EXISTINGPROCESSID", "LOGINID": "CV0001",
        "FILTER1": segment, "FILTER2": trade_date,
        "extraoption2": "", "extraoption3": "",
    })
    assert resp.status_code == 200
    return resp.json()["Result"]


def test_dropdown_filters_by_segment_and_date(client):
    pid_mon = int(_reserve(client, trade_date="2026-07-20")["Table1"][0]["PROCESSID"])
    pid_tue = int(_reserve(client, trade_date="2026-07-21")["Table1"][0]["PROCESSID"])
    assert pid_mon != pid_tue

    assert _dropdown(client, "MCX", "2026-07-20")[0]["_KEY"] == pid_mon
    assert _dropdown(client, "MCX", "2026-07-21")[0]["_KEY"] == pid_tue
    # A date with no reservation is a miss ("mint new"), never a stale PID.
    assert _dropdown(client, "MCX", "2026-07-22") == []
    # Same date, other segment: also a miss.
    assert _dropdown(client, "EQ", "2026-07-20") == []


def test_fresh_reserve_carries_v5_row_shape(client):
    result = _reserve(client)
    table1 = result["Table1"][0]
    assert table1["ISRUNNABLE"] is True
    assert table1["ISAUTOUPLOAD"] is True
    for row in result["Table2"]:
        # Creation rows carry STATUSDESC null, per the doc's example.
        assert row["STATUS"] == "PENDING"
        assert row["STATUSDESC"] is None


def test_refetch_reports_real_status_and_does_not_trigger(client):
    result = _reserve(client)
    pid = str(result["Table1"][0]["PROCESSID"])
    # Upload + register a file into UPLOADID 127 (MCX contract master).
    client.post("/v1/api/process/SaveTradePromodalUploadChunkFile", data={
        "CurrentChunk": "0", "TotalChunks": "1", "Guid": "guid-1",
        "FileName": "MCX_ProductMaster.csv", "UPLOADID": "127",
    }, files={"file": ("MCX_ProductMaster.csv", b"data")})
    reg = client.post("/v1/api/process/SaveNewTradeProcessPromodalUploadFile", json={
        "uploadfoldername": "guid-1", "uploadid": "127", "paraM9": pid,
        "uploadfilename": "MCX_ProductMaster.csv",
    })
    assert reg.json()["Status"] == "Success"

    refetch = client.post("/v1/api/process/getNewTradeProcess", json={
        "GROUPNAME": "MCX", "LOGINID": "CV0001", "PASSWORD": "x",
        "TRADEDATE": "2026-07-20", "PROCESSID": pid,
    }).json()["Result"]

    # Re-fetch: same PID, ISAUTOUPLOAD flipped False (the real-CBOS quirk),
    # still runnable.
    assert str(refetch["Table1"][0]["PROCESSID"]) == pid
    assert refetch["Table1"][0]["ISAUTOUPLOAD"] is False
    assert refetch["Table1"][0]["ISRUNNABLE"] is True

    by_uploadid = {row["UPLOADID"]: row for row in refetch["Table2"]}
    # The filled slot reads back its real progress - not reset to PENDING.
    assert by_uploadid[127]["STATUS"] == "SUCCESS"
    assert by_uploadid[127]["STATUSDESC"] == "FILE UPLOADED"
    # Untouched file slots still advertise the upload as pending.
    assert by_uploadid[534]["STATUS"] == "PENDING"
    assert by_uploadid[534]["STATUSDESC"] == "UPLOAD FILE PENDING"

    # Mandatory slots (534/535/320) are unsatisfied -> the re-fetch must NOT
    # have started billing.
    assert STATE.get_process(pid).triggered is False


def test_refetch_triggers_once_gtg_ready(client):
    result = _reserve(client)
    pid = str(result["Table1"][0]["PROCESSID"])
    # Satisfy every mandatory upload slot by marking each optional (Step 8).
    for row in result["Table2"]:
        if row["UPLOADID"] != 0:
            resp = client.post("/v1/api/process/UpdateNewTradeProcessProcessDetailsIsMandatory",
                               json={"PROCESSID": pid, "STEPNO": row["STEPNO"], "ISOPTIONAL": "0"})
            assert resp.status_code == 200
    client.post("/v1/api/process/getNewTradeProcess", json={
        "GROUPNAME": "MCX", "LOGINID": "CV0001", "PASSWORD": "x",
        "TRADEDATE": "2026-07-20", "PROCESSID": pid,
    })
    assert STATE.get_process(pid).triggered is True


def test_holiday_check_uses_payload_trade_date(client, monkeypatch):
    monkeypatch.setenv("MOCK_CBOS_HOLIDAYS", "2026-07-19")
    # No process reserved yet - the real Step 1 ordering.
    holiday = client.post("/api/edp/file_process_status", json={
        "Segment": "MCX", "TradeDate": "2026-07-19",
        "ProcessName": "BeginFileUpload", "UserID": "CV0001",
    }).json()
    assert holiday["Data"][0]["MSG"] == "HOLIDAY"
    working = client.post("/api/edp/file_process_status", json={
        "Segment": "MCX", "TradeDate": "2026-07-20",
        "ProcessName": "BeginFileUpload", "UserID": "CV0001",
    }).json()
    assert working["Data"][0]["MSG"] == "SKIP"


def test_request_models_stay_lenient(client):
    """The typed request models must accept what the raw-dict handlers did:
    numeric PROCESSID/STEPNO (real traffic sends 17658 and "17658"
    interchangeably) and extra keys like PASSWORD - never a 422."""
    result = _reserve(client)
    pid = result["Table1"][0]["PROCESSID"]  # keep as int on purpose
    refetch = client.post("/v1/api/process/getNewTradeProcess", json={
        "GROUPNAME": "MCX", "LOGINID": "CV0001", "PASSWORD": "extra-key",
        "TRADEDATE": "2026-07-20", "PROCESSID": pid,
    })
    assert refetch.status_code == 200
    assert refetch.json()["Result"]["Table1"][0]["PROCESSID"] == pid

    optional = client.post("/v1/api/process/UpdateNewTradeProcessProcessDetailsIsMandatory",
                           json={"PROCESSID": pid, "STEPNO": "4", "ISOPTIONAL": 0})
    assert optional.status_code == 200


def test_fileupload_status_resolves_per_trade_date(client):
    # Two days reserved; only day 1's slots get satisfied.
    r1 = _reserve(client, trade_date="2026-07-20")
    pid1 = str(r1["Table1"][0]["PROCESSID"])
    _reserve(client, trade_date="2026-07-21")

    for row in r1["Table2"]:
        if row["UPLOADID"] != 0:
            client.post("/v1/api/process/UpdateNewTradeProcessProcessDetailsIsMandatory",
                        json={"PROCESSID": pid1, "STEPNO": row["STEPNO"], "ISOPTIONAL": "0"})

    def poll(trade_date: str) -> str:
        # Two polls: the first eats the default MOCK_CBOS_PENDING_POLLS=1 delay.
        for _ in range(2):
            msg = client.post("/api/edp/file_process_status", json={
                "Segment": "MCX", "TradeDate": trade_date,
                "ProcessName": "FILEUPLOAD", "UserID": "CV0001",
            }).json()["Data"][0]["MSG"]
        return msg

    assert poll("2026-07-20") == "TRUE"
    # Day 2 has its own (unsatisfied) process - it must not inherit day 1's TRUE.
    assert poll("2026-07-21") == "FALSE"
