"""Step 41 — registering a file that belongs to no trade process.

The post-trade inputs (collateral valuations, margin files, bond rates) are uploaded through
EDP > EDP REQUEST > UPLOAD rather than a segment's trade-process screen. They never appear in a
Table2 slot list, so there is no PROCESSID to bind them to, and Step 7 cannot register them.
V6.1 added `SaveTradePromodalUploadFile` for exactly that.

**What earns these tests is the near-identity of the two calls.** Step 41 differs from Step 7 in
two characters of URL and one blank field, so the obvious implementation is an optional argument
on the existing method — and then one missing argument silently registers a segment file with no
PROCESSID, or a post-trade file against someone else's. They are separate methods on purpose, and
these tests pin the difference rather than the sameness.
"""

from __future__ import annotations

import json

import pytest

from app.clients.cbos_client import (
    SAVE_POST_TRADE_UPLOAD_FILE_PATH,
    SAVE_UPLOAD_FILE_PATH,
    CBOSClient,
    MockCBOSClient,
)

# The FOLDER date format (`%d-%m-%Y`), not ISO — `_to_cbos_date` parses it with
# settings.date_folder_format and converts to CBOS's own. Written ISO first, and the client
# rejected it; the parameter is named `trade_date` on both Step 7 and Step 41 and means the
# string on the folder, not a date.
TRADE_DATE = "18-08-2026"
TRADE_DATE_AS_CBOS_SEES_IT = "2026-08-18"
GUID = "57858E9A-1FD7-4359-B17D-C5745DE0FD61"
# CASH MG02 in its UDIFF form. Chosen over the doc's ICRA example because 547 is the id this lane
# is first proved against: it is live in CBOS today, where the five EOD ids beside it are not.
CASH_MG02_UDIFF = "547"
FILE_NAME = "Margin_NCL_CM_0_CM_10412_20260818_F_0000.csv"


class _CapturingClient(CBOSClient):
    """The real client with only the socket removed, so the PAYLOAD under test is the real one."""

    def __init__(self):
        # Deliberately NOT super().__init__(): the real constructor refuses to build without CBOS
        # credentials in settings, which is correct for production and beside the point here —
        # these tests never open a socket. What must stay real is `_create_post_trade_file_entry`
        # and the URL builders, and they are.
        self.posts: list[tuple[str, dict]] = []

    def _post(self, url: str, payload: dict) -> dict:
        self.posts.append((url, payload))
        return {"Status": "Success", "Result": "File entry saved successfully"}


def test_step_41_sends_a_blank_process_id() -> None:
    """The one field that distinguishes it, and the reason the endpoint exists.

    V6.1 annotates `paraM9` in place — *"It is common upload no process id is rerquired"* [sic].
    A non-blank value here would bind a post-trade file to some segment's trade process.
    """
    c = _CapturingClient()
    c.register_post_trade_file(CASH_MG02_UDIFF, GUID, FILE_NAME, TRADE_DATE)

    (url, payload) = c.posts[0]
    assert payload["paraM9"] == "", "a post-trade file belongs to no PROCESSID"
    assert url.endswith(SAVE_POST_TRADE_UPLOAD_FILE_PATH)
    assert payload["paraM1"] == TRADE_DATE_AS_CBOS_SEES_IT, "the folder date is converted, not passed through"


def test_step_41_goes_to_a_different_endpoint_from_step_7() -> None:
    """`SaveTradePromodalUploadFile` against `SaveNewTradeProcessPromodalUploadFile`.

    One word shorter, and easy to mistype into the other. Pinned because the two would otherwise
    differ only by a string constant nobody reads twice.
    """
    assert SAVE_POST_TRADE_UPLOAD_FILE_PATH != SAVE_UPLOAD_FILE_PATH
    assert "NewTradeProcess" not in SAVE_POST_TRADE_UPLOAD_FILE_PATH


def test_step_41_is_otherwise_identical_to_step_7() -> None:
    """Same fields, same order, same values — everything except `paraM9` and the URL.

    Stated as an assertion rather than a comment because the two payloads are maintained
    separately: a field added to Step 7 and forgotten here would be found by CBOS, at upload time,
    on a file that publishes once a day.
    """
    c = _CapturingClient()
    c.register_file(CASH_MG02_UDIFF, GUID, FILE_NAME, "17658", TRADE_DATE)
    c.register_post_trade_file(CASH_MG02_UDIFF, GUID, FILE_NAME, TRADE_DATE)

    (_u7, step7), (_u41, step41) = c.posts
    assert list(step7) == list(step41), "the two payloads must carry the same fields in order"
    differing = {k for k in step7 if step7[k] != step41[k]}
    assert differing == {"paraM9"}, f"only the PROCESSID may differ, got {differing}"


def test_the_upload_id_is_carried_verbatim() -> None:
    """It selects the parser and the destination table, not just the name check.

    547 is CASH MG02 UDIFF (52 columns, `MarginFile_NCL_CM_Final_UDIFF_Temp`); 554 is the CASH
    PEAK file with an identical filename pattern and a different destination. Passing the wrong
    one is not a naming error — the file lands, parsed, in the wrong table.
    """
    c = _CapturingClient()
    c.register_post_trade_file(CASH_MG02_UDIFF, GUID, FILE_NAME, TRADE_DATE)
    assert c.posts[0][1]["uploadid"] == CASH_MG02_UDIFF


def test_the_mock_keeps_post_trade_files_out_of_the_segment_view() -> None:
    """A post-trade file must not appear in any segment's file list.

    The bot deliberately keeps these out of `slots.py` and the manifest, because declaring one as
    a segment slot parks that segment INCOMPLETE every day the file is absent. A mock that filed
    them together would let a test assert a segment's completeness and be counting a collateral
    valuation.
    """
    m = MockCBOSClient()
    m.register_post_trade_file(CASH_MG02_UDIFF, GUID, FILE_NAME, TRADE_DATE)

    assert m.post_trade_file_entries == [(CASH_MG02_UDIFF, FILE_NAME, TRADE_DATE)]
    assert m._segment_file_names == {}, "no segment may see a post-trade file"


def test_both_clients_implement_it() -> None:
    """Neither adapter may be left abstract.

    A missing implementation on the mock is the dangerous one: the real client would work, every
    test would pass against the mock, and the gap would surface only in a live run.
    """
    for cls in (CBOSClient, MockCBOSClient):
        assert not getattr(cls, "__abstractmethods__", ()), f"{cls.__name__} is still abstract"
        assert "_create_post_trade_file_entry" in cls.__dict__


@pytest.mark.parametrize("field", ["uploadid", "uploadfilename", "uploadfoldername", "paraM1"])
def test_the_fields_cbos_binds_on_are_all_present(field: str) -> None:
    """GUID binds the registration to the chunks, and paraM1 is the trade date.

    `uploadfoldername` must be the GUID from Step 5 exactly — CBOS matches the registration to the
    uploaded bytes by that folder and nothing else.
    """
    c = _CapturingClient()
    c.register_post_trade_file(CASH_MG02_UDIFF, GUID, FILE_NAME, TRADE_DATE)
    payload = c.posts[0][1]
    assert payload[field], f"{field} must be sent"
    assert json.dumps(payload)  # serialisable as-is, no surprises in the values
