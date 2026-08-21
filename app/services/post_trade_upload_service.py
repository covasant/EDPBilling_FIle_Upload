"""Uploads one post-trade input file into CBOS, synchronously.

The post-trade processes — Collateral Valuation, Collateral Allocation, MTF Fund Transfer, Daily
Margin Reporting, Daily Margin Statement — consume files that no trade process owns: bond
valuations, margin files, bhavcopies, rate files. They are uploaded through EDP > EDP REQUEST >
UPLOAD, so they never appear in a Table2 slot list and there is no PROCESSID to bind them to.

**This is a path BESIDE the segment lane, not an extension of it.** It reuses the transport —
`upload_file` (Step 5) is untouched and shared, because the chunk endpoint keys off the GUID alone
and carries no PROCESSID — and deliberately reuses nothing else. In particular there is **no
batch and no completeness gate**: the download bot keeps post-trade files out of `slots.py` and
the manifest on purpose, because declaring one as a segment slot parks that segment INCOMPLETE
every day the file is absent. Bringing batch semantics across would undo that decision by
accident.

Synchronous, like `settlement_service` and unlike `upload_service.process_batch`, and for the same
reason: the caller (the billing engine's post-trade state machine) already owns scheduling, retry
and window tracking for the trade date. It wants call-and-get-result, not a queue it has to poll.
Post-trade files are also small — single-digit MB against the segment lane's 486 MB trade file —
so there is no long transfer to hide behind a 202.

The flow is three CBOS calls:

    Step 4   upload_settings(upload_id)        -> the rule: name pattern, extension, columns
    Step 5   upload_file(path, id, guid)       -> chunked transfer, shared with the segment lane
    Step 41  register_post_trade_file(...)     -> registration with a blank PROCESSID
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.clients.cbos_client import BaseCBOSClient, CBOSUploadError, UploadRule, get_cbos_client
from app.core.config import settings
from app.core.safe_path import UnsafePathError, resolve_within
from app.services import upload_matching

logger = logging.getLogger("post_trade_upload")


class PostTradeFileNotFound(Exception):
    """The named file is not under the post-trade folder for that date — most likely the download
    bot has not fetched it yet, which is a wait rather than a failure."""


class UnknownUploadId(Exception):
    """CBOS returned no settings for this UploadID.

    **Answered as Success with an empty Result, not as an error** — so an id that has been
    deactivated on CBOS's side is indistinguishable from a typo unless it is checked up front.
    Five post-trade ids are in exactly that state today (the UDIFF EOD rows), and a slot pointing
    at one of them would otherwise fail at upload time looking like a filename mismatch. Raised
    here so it fails at the start of the run, naming the id.
    """


class FileNameRejected(Exception):
    """The file on disk does not match the pattern CBOS holds for this UploadID.

    Checked before the transfer rather than after, because CBOS's own answer to a name it does not
    like arrives after every chunk has been sent — and on a shared, rate-limited link that is a
    minute wasted per file. Also catches the more dangerous case: a slot configured with the wrong
    id, where the file would otherwise upload cleanly into another file's table.
    """


@dataclass(frozen=True)
class PostTradeUploadResult:
    upload_id: str
    file_name: str
    guid: str
    trade_date: str
    rule_name: str
    declared_name: str = ""


def _locate(file_name: str, trade_date: str) -> Path:
    """The file, under this trade date's POSTTRADE folder and nowhere else.

    `resolve_within` is the same guard the settlement path uses: a caller-supplied name must not
    be able to reach outside the folder it names, and these names come over HTTP.
    """
    folder = Path(settings.file_root_path) / trade_date / "POSTTRADE"
    try:
        path = resolve_within(folder, file_name)
    except UnsafePathError as exc:
        raise PostTradeFileNotFound(f"{file_name!r} is not a name inside {folder}") from exc
    if not path.is_file():
        raise PostTradeFileNotFound(
            f"{file_name!r} is not in {folder} — the bot may not have fetched it yet"
        )
    return path


def _rule_for(client: BaseCBOSClient, upload_id: str) -> UploadRule:
    """Step 4, with the empty answer turned into a named failure rather than a None."""
    rule = client.upload_settings(upload_id)
    if rule is None:
        raise UnknownUploadId(
            f"CBOS returned no settings for UPLOADID={upload_id}. It is either wrong or "
            f"deactivated — CBOS answers both the same way, with Success and an empty Result."
        )
    return rule


def _name_cbos_will_accept(rule: UploadRule, actual: str) -> str | None:
    """A name satisfying CBOS's Step 4 rule, built by PREFIXING the real one — or None if
    the file already passes.

    **Step 4 is the gate, not Step 40.** Step 40 returns a canonical-looking name with a
    date token and reads like the authority, but it is a diagnostic: proved on 2026-08-21
    by uploading ``BhavCopy_NSE_CM_0_0_0_20260820_F_0000.csv`` under UploadID 518 and
    having CBOS accept it, TRANID and all, while Step 40 for that id demanded a DDMMYYYY
    date the file does not carry. Step 4's rule is a plain substring plus an extension and
    carries **no date at all**, which is why the bhavcopies were never actually broken.

    Only two of the twenty genuinely fail it, and both are a token the published name has
    never contained::

        345  MOSL VAR   wants 'MOSLVar file'   file is  MOSLVarfile -20082026.xlsx
        276  MCX EOD    wants 'MCX_MARGIN'     MCX serves Margin_MCXCCL_... and MCX_MRG_...

    **PREFIXED, not replaced.** ``<token>_<original name>`` keeps the whole published name
    visible inside the declared one, so anyone reading CBOS can still see which file this
    really was. Synthesising a bare name from the rule would satisfy the gate and destroy
    that, and this is the one point where a file stops being self-describing.

    Nothing here decides WHICH file this is — the caller named the UploadID, and the check
    below is on a name that has already been rejected. Identification never depends on the
    name being replaced, which is what stops this being a check that can only pass.
    """
    token = (rule.file_name_pattern or "").strip()
    if not token or token.lower() in actual.lower():
        return None  # no rule to satisfy, or already satisfied
    return f"{token}_{actual}"


def upload_one(
    *,
    upload_id: str,
    file_name: str,
    trade_date: str,
    segment: str = "",  # accepted for the wire; the Step 4 rule needs no segment
    translate_name: bool = False,
    client: BaseCBOSClient | None = None,
) -> PostTradeUploadResult:
    """Put one post-trade file into CBOS. Raises rather than returning a status.

    `trade_date` is the FOLDER date string (`%d-%m-%Y`), matching Step 7's parameter of the same
    name — the client converts it to CBOS's format itself.

    `upload_id` comes from the caller's configuration, not from a lookup here. It is per-file and
    it selects the parser and the destination table, so it is the caller's to get right: 547 is
    CASH MG02 in UDIFF form, 554 is the CASH PEAK file with an identical filename pattern and a
    different destination.
    """
    client = client or get_cbos_client()
    path = _locate(file_name, trade_date)
    rule = _rule_for(client, upload_id)

    # The pattern check that upload_matching does for a batch, applied to one known id. Not a
    # search — the caller has already said which id this file is for, and this only confirms it.
    if not upload_matching.matches_rule(rule, path.name) and not translate_name:
        raise FileNameRejected(
            f"{path.name!r} does not match the pattern CBOS holds for UPLOADID={upload_id} "
            f"({rule.name!r}: {rule.compare_operator} {rule.file_name_pattern!r}). Either the "
            f"file is for a different UploadID, or the exchange has changed the name."
        )

    # The name DECLARED to CBOS, which is not necessarily the name on disk. Opt-in per
    # request: a blanket switch would quietly license this for the files where CBOS's
    # pattern matches SEVERAL real files (NSE VAR's six snapshots, ICCL's three variants),
    # and there a rename would let the wrong artefact through under a name CBOS accepts.
    declared = path.name
    if translate_name:
        derived = _name_cbos_will_accept(rule, path.name)
        if derived:
            # Both names, always. A log line saying only that something was renamed cannot
            # be audited, and this is the one point where the file stops being
            # self-describing.
            logger.warning(
                "post-trade upload: declaring %r to CBOS for UPLOADID=%s; the file on disk "
                "is %r. CBOS's Step 40 pattern cannot match the published name — see "
                "_cbos_expected_name. Bytes and the on-disk name are unchanged.",
                derived,
                upload_id,
                path.name,
            )
            declared = derived

    guid = str(uuid.uuid4())
    logger.info(
        "post-trade upload starting: file=%s upload_id=%s (%s) trade_date=%s guid=%s declared=%s",
        path.name,
        upload_id,
        rule.name,
        trade_date,
        guid,
        declared,
    )

    client.upload_file(path, upload_id, guid, file_name=declared)
    client.register_post_trade_file(upload_id, guid, declared, trade_date)

    logger.info(
        "post-trade upload done: file=%s upload_id=%s trade_date=%s", path.name, upload_id, trade_date
    )
    return PostTradeUploadResult(
        upload_id=upload_id,
        file_name=path.name,
        guid=guid,
        trade_date=trade_date,
        rule_name=rule.name,
        declared_name="" if declared == path.name else declared,
    )


__all__ = [
    "FileNameRejected",
    "PostTradeFileNotFound",
    "PostTradeUploadResult",
    "UnknownUploadId",
    "upload_one",
]
