"""One post-trade file, from a name on disk to a registration in CBOS.

`test_post_trade_registration.py` pins the Step 41 payload. This drives the whole service: find
the file, check the UploadID is real, check the name against CBOS's own rule, chunk it up, register
it — and the four ways it can refuse.

**The refusals are the point.** All four look identical from the caller's side if they are
collapsed into one error, and the caller must treat them differently: a missing file at 3 AM means
wait, a dead UploadID means stop and fix configuration, and CBOS being unreachable means retry.
Getting that wrong is how a deactivated UploadID comes to look like a slow exchange.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.clients.cbos_client import MockCBOSClient
from app.core.config import settings
from app.main import app
from app.services import post_trade_upload_service as service

TRADE_DATE = "18-08-2026"  # the FOLDER date, %d-%m-%Y — not ISO
CASH_MG02_UDIFF = "547"
FILE_NAME = "Margin_NCL_CM_0_CM_10412_20260818_F_0000.csv"


class _Client(MockCBOSClient):
    """The mock, with Step 4 answering for the ids this test cares about."""

    def __init__(self, *, settings_row: dict | None = None):
        super().__init__()
        self._row = settings_row

    def _get_upload_settings(self, upload_id: str) -> dict:
        if self._row is None:
            # Exactly how CBOS answers an unavailable id: Success, and nothing in it.
            return {"Status": "Success", "Result": []}
        return {"Status": "Success", "Result": [{"ID": int(upload_id), **self._row}]}


def _cash_mg02_row() -> dict:
    """CASH MG02 - UDIFF as CBOS really describes it (id 547, read live 2026-08-19)."""
    return {
        "NAME": "CASH MG02 - UDIFF",
        "FILE NAME (CONTAINS)": "Margin_NCL_CM_0_CM_10412",
        "FILEEXTENSION": "csv",
        "NO. OF COLUMNS": 52,
    }


@pytest.fixture
def posttrade_folder():
    """A POSTTRADE folder for the trade date, as the download bot leaves it.

    Reads `settings.file_root_path` rather than patching it. conftest already points it at a
    per-test tmp dir via FILE_ROOT_PATH, and the first version of this fixture monkeypatched the
    settings SINGLETON on top of that — mutating a shared object that other tests rebuild with
    `get_settings.cache_clear()`. It passed alone and broke `test_upload_robustness` when the two
    ran together, which is the whole reason this comment exists.
    """
    folder = Path(settings.file_root_path) / TRADE_DATE / "POSTTRADE"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / FILE_NAME).write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    return folder


def test_a_file_reaches_cbos_with_its_upload_id_and_a_matching_guid(posttrade_folder) -> None:
    """The happy path, and the one invariant that binds the two calls together.

    CBOS matches a registration to the bytes it received by the GUID and nothing else, so Step 5
    and Step 41 must carry the SAME one. They are separate calls made from separate lines; a
    regenerated GUID between them would upload the file and register an empty folder, and CBOS
    would report success for both.
    """
    client = _Client(settings_row=_cash_mg02_row())

    result = service.upload_one(
        upload_id=CASH_MG02_UDIFF, file_name=FILE_NAME, trade_date=TRADE_DATE, client=client
    )

    assert client.upload_calls == [(CASH_MG02_UDIFF, FILE_NAME)], "Step 5 ran once, for this id"
    assert client.post_trade_file_entries == [(CASH_MG02_UDIFF, FILE_NAME, TRADE_DATE)]
    assert result.guid, "a GUID was minted"
    assert result.rule_name == "CASH MG02 - UDIFF", "CBOS's own name is echoed back for the log"


def test_a_dead_upload_id_fails_before_anything_is_sent(posttrade_folder) -> None:
    """The failure this project has been bitten by, caught at the start of the run.

    CBOS answers an unavailable UploadID with `{"Status":"Success","Result":[]}` — not an error.
    Five post-trade ids are in that state today. Without this check the file would chunk all the
    way up and be refused at registration, looking exactly like a filename problem.
    """
    client = _Client(settings_row=None)

    with pytest.raises(service.UnknownUploadId, match="669"):
        service.upload_one(
            upload_id="669", file_name=FILE_NAME, trade_date=TRADE_DATE, client=client
        )

    assert client.upload_calls == [], "nothing may be transferred before the id is known good"


def test_a_name_cbos_would_refuse_fails_before_the_transfer(posttrade_folder) -> None:
    """Checked up front, and it catches more than a typo.

    A slot configured with the WRONG UploadID is the dangerous case: the file would otherwise
    upload cleanly into some other file's table. Here the id is CASH PEAK's rather than CASH
    MG02's, so the pattern will not match and the run stops.
    """
    peak_row = dict(_cash_mg02_row(), NAME="CASH Peak File - UDIFF")
    peak_row["FILE NAME (CONTAINS)"] = "Margin_NCL_CD_0_TM_10412"  # a different file entirely
    client = _Client(settings_row=peak_row)

    with pytest.raises(service.FileNameRejected):
        service.upload_one(
            upload_id="554", file_name=FILE_NAME, trade_date=TRADE_DATE, client=client
        )

    assert client.upload_calls == [], "a mismatched name must cost no transfer"


def test_a_missing_file_is_not_an_error_about_cbos(posttrade_folder) -> None:
    """The common case in the small hours: MCX EOD publishes at 4:41 AM.

    Distinct from every other failure here, because it is the only one the caller fixes by
    waiting.
    """
    with pytest.raises(service.PostTradeFileNotFound):
        service.upload_one(
            upload_id=CASH_MG02_UDIFF,
            file_name="not_here_yet.csv",
            trade_date=TRADE_DATE,
            client=_Client(settings_row=_cash_mg02_row()),
        )


def test_a_name_cannot_reach_outside_the_trade_date_folder(posttrade_folder) -> None:
    """`file_name` arrives over HTTP, so it is not trusted to stay in its folder."""
    with pytest.raises(service.PostTradeFileNotFound):
        service.upload_one(
            upload_id=CASH_MG02_UDIFF,
            file_name="../../etc/passwd",
            trade_date=TRADE_DATE,
            client=_Client(settings_row=_cash_mg02_row()),
        )


def test_no_batch_is_created(posttrade_folder) -> None:
    """A post-trade file must not acquire segment batch semantics.

    The bot keeps these files out of `slots.py` and the manifest deliberately: declaring one as a
    segment slot parks that segment INCOMPLETE every day the file is absent. Reusing the
    uploader's transport must not quietly reuse its batch machinery, so the mock's segment view
    stays empty.
    """
    client = _Client(settings_row=_cash_mg02_row())
    service.upload_one(
        upload_id=CASH_MG02_UDIFF, file_name=FILE_NAME, trade_date=TRADE_DATE, client=client
    )
    assert client._segment_file_names == {}, "no segment may see a post-trade file"


# ── the wire ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raises,expected_status",
    [
        (service.PostTradeFileNotFound("nope"), 404),
        (service.UnknownUploadId("nope"), 422),
        (service.FileNameRejected("nope"), 422),
    ],
)
def test_each_failure_gets_its_own_status_code(monkeypatch, raises, expected_status) -> None:
    """404 means wait, 422 means fix the configuration. The caller acts on the difference.

    Collapsed into one code, a deactivated UploadID is indistinguishable from an exchange running
    late, and the engine would retry it every wakeup until its window closed.
    """
    from app.api.v1.endpoints import post_trade as endpoint

    def _boom(**_kw):
        raise raises

    monkeypatch.setattr(endpoint.service, "upload_one", _boom)
    with TestClient(app) as client:
        resp = client.post(
            "/post-trade/uploads",
            json={"upload_id": "547", "file_name": FILE_NAME, "trade_date": TRADE_DATE},
        )
    assert resp.status_code == expected_status, resp.text


def test_segment_reaches_upload_settings_for_the_step40_fallback():
    """A slot whose Step-4 pattern is BLANK produces no usable rule and is dropped, so without
    the Step-40 fallback its file can never be uploaded at all.

    Found live on 2026-08-22 with UploadID 490 (NSE NAV): the Step-4 row exists but its
    `FILE NAME (CONTAINS)` is an empty string, so `upload_one` raised UnknownUploadId - a 422,
    which the billing engine treats as PERMANENT and fails the process on - for a slot that is
    configured, just loosely. The segment lane met this on NCDEXPHY/482 and built the fallback;
    the post-trade lane accepted a `segment` argument and dropped it on the floor.
    """
    from app.services import post_trade_upload_service as svc

    seen = {}

    class _Client:
        def upload_settings(self, upload_id, segment="", trade_date="", **_):
            seen["upload_id"] = upload_id
            seen["segment"] = segment
            seen["trade_date"] = trade_date
            return None  # what a blank Step-4 pattern produces

    try:
        svc._rule_for(_Client(), "490", segment="MF", trade_date="21-08-2026")
    except svc.UnknownUploadId as exc:
        assert "490" in str(exc)
        assert "MF" in str(exc), "the error should name the segment it tried"
    else:
        raise AssertionError("a None rule must still raise")

    assert seen["segment"] == "MF", "segment must reach upload_settings or the fallback is dead"
    assert seen["trade_date"] == "21-08-2026", "Step 40 takes the trade date as a parameter"


def test_segment_reaches_upload_settings_for_the_step40_fallback():
    """A slot whose Step-4 pattern is BLANK produces no usable rule and is dropped, so without
    the Step-40 fallback its file can never be uploaded at all.

    Found live on 2026-08-22 with UploadID 490 (NSE NAV): the Step-4 row exists but its
    `FILE NAME (CONTAINS)` is an empty string, so `upload_one` raised UnknownUploadId - a 422,
    which the billing engine treats as PERMANENT and fails the process on - for a slot that is
    configured, just loosely. The segment lane met the same thing on NCDEXPHY/482 and built the
    fallback for it; the post-trade lane accepted a `segment` argument and dropped it.
    """
    from app.services import post_trade_upload_service as svc

    seen = {}

    class _Client:
        def upload_settings(self, upload_id, segment="", trade_date="", **_):
            seen["upload_id"] = upload_id
            seen["segment"] = segment
            seen["trade_date"] = trade_date
            return None  # what a blank Step-4 pattern produces

    try:
        svc._rule_for(_Client(), "490", segment="MF", trade_date="21-08-2026")
    except svc.UnknownUploadId as exc:
        assert "490" in str(exc)
        assert "MF" in str(exc), "the error should name the segment it tried"
    else:
        raise AssertionError("a None rule must still raise")

    assert seen["segment"] == "MF", "segment must reach upload_settings or the fallback is dead"
    assert seen["trade_date"] == "21-08-2026", "Step 40 takes the trade date as a parameter"


def test_locate_finds_a_file_in_its_process_folder(tmp_path, monkeypatch):
    """The bot MOVES a required file into POSTTRADE/<PROCESS>/, so that is where the uploader
    has to look. Before this it only ever read the flat root, and every required file would
    have 404'd as "not fetched yet" the moment filing by process turned on."""
    from app.core.config import settings
    from app.services import post_trade_upload_service as svc

    monkeypatch.setattr(settings, "file_root_path", str(tmp_path))
    d = tmp_path / "17-08-2026" / "POSTTRADE" / "COLVAL"
    d.mkdir(parents=True)
    (d / "CB_Bhavcopy17082026.CSV").write_bytes(b"x")

    got = svc._locate("CB_Bhavcopy17082026.CSV", "17-08-2026", "COLVAL")
    assert got == d / "CB_Bhavcopy17082026.CSV"


def test_locate_falls_back_to_the_flat_root(tmp_path, monkeypatch):
    """Older dates, and anything placed by hand, still live in the root. A caller naming a
    folder must not be worse off than one that does not."""
    from app.core.config import settings
    from app.services import post_trade_upload_service as svc

    monkeypatch.setattr(settings, "file_root_path", str(tmp_path))
    root = tmp_path / "17-08-2026" / "POSTTRADE"
    root.mkdir(parents=True)
    (root / "loose.csv").write_bytes(b"x")

    assert svc._locate("loose.csv", "17-08-2026", "COLVAL") == root / "loose.csv"
    assert svc._locate("loose.csv", "17-08-2026") == root / "loose.csv"


def test_locate_names_both_places_it_tried(tmp_path, monkeypatch):
    """A 404 that names only one of two folders sends someone looking in the wrong place."""
    from app.core.config import settings
    from app.services import post_trade_upload_service as svc

    monkeypatch.setattr(settings, "file_root_path", str(tmp_path))
    (tmp_path / "17-08-2026" / "POSTTRADE" / "COLVAL").mkdir(parents=True)

    try:
        svc._locate("nope.csv", "17-08-2026", "COLVAL")
    except svc.PostTradeFileNotFound as exc:
        assert "COLVAL" in str(exc) and "POSTTRADE" in str(exc)
    else:
        raise AssertionError("should not have found it")


def test_a_traversing_folder_is_refused(tmp_path, monkeypatch):
    """`folder` arrives over HTTP just like `file_name`, so it gets the same guard. Without it
    a folder of '../../..' reaches anywhere the process can read."""
    from app.core.config import settings
    from app.services import post_trade_upload_service as svc

    monkeypatch.setattr(settings, "file_root_path", str(tmp_path))
    (tmp_path / "17-08-2026" / "POSTTRADE").mkdir(parents=True)

    for bad in ("../..", "../../etc", "/etc"):
        try:
            svc._locate("passwd", "17-08-2026", bad)
        except svc.PostTradeFileNotFound:
            pass  # refused, by either the folder guard or the name guard
        else:
            raise AssertionError(f"{bad!r} should not resolve")
