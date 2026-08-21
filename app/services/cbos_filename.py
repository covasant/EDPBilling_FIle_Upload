"""The name CBOS is told for the three T-1 files, when the exchange stamped a later day.

CBOS validates the date inside an uploaded filename and rejects a mismatch — observed
live 2026-08-17:

    NSE BSE InterOperable Scrip Mapping Upload — FAILED
    FILE NAME TRADE DATE(T-1) MISMATCH  2026-08-17

NSE published no ``BSE_Scrip_Series_Mapping_14082026.csv`` for the Friday at all and
listed ``..._15082026.csv`` instead — a Saturday, and Independence Day, so not a session.
CBOS wanted the 14th. Same shape on 24/25 July; 9 of 679 files in that folder are weekend
stamps. So: the right file, published late, stamped with the day it was generated.

SCOPE IS THE SIX ``T-1`` SLOTS. Step 40 (``get_expected_filename``) reports a
``DateBasis`` per UploadID, queried live 2026-08-17:

    81   BSE SCRIP                       SCRIP_140826.TXT          ddmmyy
    83   NSE BSE INTEROP SCRIP MAPPING   %...% + %14082026%        DDMMYYYY
    97   CONTRACT MASTER - BSEFO         EQD_CO140826.CSV          ddmmyy
    117  CONTRACT MASTER - BSECD         %BFX_CO% + %140826%       ddmmyy
    119  CONTRACT MAPPING FILE CD        %...% + %14082026%        DDMMYYYY
    138  CONTRACT MAPPING FILE FO        %...% + %14082026%        DDMMYYYY

BOTH date shapes are in play and both are handled — an earlier cut of this module took
only DDMMYYYY, which silently left half the T-1 slots exposed to the very failure it
exists to fix. The list came from ``EDPFILEUPLOADSETTING.xlsx``, whose filename templates
cover only three of the six; Step 40 is the authority and says otherwise.

The other thirteen dated slots are ``T`` and are NOT touched, even though the same late
stamp could in principle happen to them. None has been reported, CBOS's own check would
catch it, and a rejected upload someone looks at beats a silent rewrite. Widen this only
when a real failure asks for it.

WHAT A REWRITE ASSERTS, AND CANNOT CHECK. Sending Saturday's file under Friday's name
claims it holds Friday's data. Not verifiable here: the file has no date field, and ~275
rows churn between any two consecutive days, so its contents fit several dates. EDP make
this call by hand today; this automates their decision, which is why every rewrite is
recorded in the batch's request log.

TWO GUARDS, both load-bearing:

* **Direction.** Only a file stamped LATER than the wanted session, by at most
  :data:`_MAX_LATE_STAMP_DAYS`, is rewritten. One stamped EARLIER is refused — the
  download bot's own window reaches back seven days, so on a publication outage it can
  hold genuinely old data, and CBOS's date check is what catches that. Rewriting would
  relabel stale data as current and silence the objection.
* **Today only.** Step 40 takes no trade date; it answers for whatever day CBOS thinks it
  is (verified: passing ``tradedate=2026-08-12`` still returned ``InputTradeDate:
  2026-08-17``). On a backfill its dates are another day's, so a rewrite is skipped.

NEVER FAILS A BATCH. Any problem falls back to the on-disk name, which is exactly the
behaviour before this existed.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from app.core.config import settings

logger = logging.getLogger("cbos_filename")

# DDMMYYYY and ddmmyy, neither preceded nor followed by another digit so a longer number
# is never partially matched (and so the 6-digit pattern cannot match inside an 8-digit
# one). Three of the six slots use each form, and a rewrite must keep the shape it found:
# CBOS's expected name for UPLOADID 97 is EQD_CO140826.CSV, not EQD_CO14082026.CSV.
_D8 = re.compile(r"(?<!\d)(\d{8})(?!\d)")
_D6 = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# How far after the wanted session a file may be stamped and still be that session's,
# published late. Three days covers a Friday session stamped the following Monday. A
# judgement, not a measurement — both observed gaps were one day.
_MAX_LATE_STAMP_DAYS = 3


def _as_date(token: str) -> date | None:
    """``"14082026"`` or ``"140826"`` -> date, or None if it is not one.

    This is what keeps the rewrite off numbers that merely look like dates. The trade
    file's ``Trade_NSE_CM_0_TM_10412_20260813_F_0000.csv`` carries a YYYYMMDD stamp, which
    read as DDMMYYYY is month 26 — not a date, so not a candidate.
    """
    try:
        year = int(token[4:8]) if len(token) == 8 else 2000 + int(token[4:6])
        return date(year, int(token[2:4]), int(token[0:2]))
    except ValueError:
        return None


def resolve_upload_name(
    client, segment: str, upload_id: str, local_name: str, trade_date: str
) -> tuple[str, dict | None]:
    """The name to send CBOS for this file, plus an audit entry when it differs.

    ``(local_name, None)`` means "nothing to do", which is the answer for almost every
    file on almost every day. A second return value means the name was rewritten and the
    caller MUST record it — see the module docstring on what that asserts.
    """
    if not settings.cbos_rewrite_upload_filename_date:
        return local_name, None

    try:
        from app.clients.cbos_client import _decode_body

        raw = client.get_expected_filename(segment, upload_id, trade_date)
        data = (_decode_body(raw, "get_expected_filename").get("Data") or [{}])[0]
    except Exception as exc:  # a cross-check must never cost the batch a file
        logger.warning(
            "Step 40 failed for UPLOADID=%s (%s) - sending %s", upload_id, exc, local_name
        )
        return local_name, None

    if str(data.get("DateBasis") or "").strip().upper() != "T-1":
        return local_name, None  # out of scope — see the module docstring

    if str(data.get("InputTradeDate") or "")[:10] != str(trade_date)[:10]:
        logger.info(
            "UPLOADID=%s: Step 40 answered for %s, this batch is %s - not rewriting %s",
            upload_id,
            data.get("InputTradeDate"),
            trade_date,
            local_name,
        )
        return local_name, None

    wanted = str(data.get("LastTradingDate_DDMMYYYY") or "").strip()
    wanted_date = _as_date(wanted) if len(wanted) == 8 else None
    if wanted_date is None:
        return local_name, None

    # 8-digit first: a name carrying DDMMYYYY has no 6-digit run to find anyway, and
    # trying the wider form first would never mis-fire on the narrower one.
    wanted_token, found = wanted, None
    for pattern, token_form in ((_D8, wanted), (_D6, wanted[:4] + wanted[6:8])):
        found = next(((t, d) for t in pattern.findall(local_name) if (d := _as_date(t))), None)
        if found is not None:
            wanted_token = token_form
            break
    if found is None or found[0] == wanted_token:
        return local_name, None

    found_token, found_date = found
    gap = (found_date - wanted_date).days
    if not 0 < gap <= _MAX_LATE_STAMP_DAYS:
        # Refused, and loudly: CBOS is about to reject this and that rejection is CORRECT.
        # Silence would leave someone hunting for the failure when the answer is that we
        # are holding the wrong day's file.
        logger.warning(
            "UPLOADID=%s: NOT rewriting %s - stamped %s against a wanted session of %s. "
            "CBOS will reject it, and should.",
            upload_id,
            local_name,
            found_date,
            wanted_date,
        )
        return local_name, None

    new_name = local_name.replace(found_token, wanted_token, 1)
    logger.warning(
        "UPLOADID=%s: uploading %s AS %s - stamped %d day(s) after the session CBOS wants. "
        "The file on disk is untouched.",
        upload_id,
        local_name,
        new_name,
        gap,
    )
    return new_name, {
        "step": "filename_date_rewritten",
        "upload_id": upload_id,
        "on_disk": local_name,
        "sent_as": new_name,
        "cbos_last_trading_date": wanted,
        "sent_date_token": wanted_token,
        "days_late": gap,
        "asserts": (
            "the file the exchange stamped a later day holds the wanted session's data - "
            "not verified from its contents"
        ),
    }
