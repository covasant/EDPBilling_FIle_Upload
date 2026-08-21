"""Resolves which CBOS UploadID a discovered file belongs to, and validates
it against that UploadID's file-name pattern / extension / column-count
rules fetched from CBOS (Step 4).

CBOS's field names are not known here - the client decodes a settings row
into an UploadRule and this module works only with that type.

This replaces the previous (incorrect) behavior of always uploading every
file in a segment/exchange folder under Table2's first UploadID. Every
UploadID CBOS offers for the batch's process is fetched once
(fetch_upload_rules), then every discovered file is matched independently
against that full rule set (match_file).

Pattern matching is mandatory - a file must match a rule's pattern to be
selected at all. Extension matching is
NOT mandatory: a matched file is never rejected for having a different
extension than Upload Settings declared - a mismatch is only logged as a
warning, and the file still proceeds to upload under that UploadID. CBOS's
own Step 5/7/9 responses are the actual arbiter of whether the file is
ultimately accepted; this engine's job is only to pick the right UploadID.
"""

import csv
import logging
from pathlib import Path

from app.clients.cbos_client import UploadRule
from app.core.config import settings

logger = logging.getLogger("upload_matching")

# How many non-empty lines _count_columns looks at. Enough to see past a
# control record into real data rows, small enough to stay a cheap sniff on a
# 77MB trade file.
COLUMN_SNIFF_LINES = 5

# Tried after settings.upload_match_delimiter when that one does not produce the
# width CBOS declared. Pipe is what NSE's contract masters use; tab covers the
# other common non-CSV export. Deliberately a SHORT list - each extra delimiter
# is another chance for a malformed file to hit the declared width by accident.
_FALLBACK_DELIMITERS = ("|", "\t")


def _pattern_matches(pattern: str, operator: str, name: str) -> bool:
    """Apply CBOS's declared match semantics against the filename. The operator
    is whatever Step 4 declared for this UploadID - never assumed - so a new
    operator value in CBOS needs no code change here as long as it maps to one
    of these comparisons.

    Spaces and underscores are ignored when reading the operator, so "STARTS
    WITH", "STARTS_WITH" and "STARTSWITH" are the same thing. Real CBOS spells
    it with a space (see _extract_pattern)."""
    op = operator.strip().upper().replace(" ", "").replace("_", "")
    if op in ("LIKE", "CONTAINS", ""):
        return pattern in name
    if op in ("EQUALS", "EQUAL", "="):
        return pattern == name
    if op in ("STARTSWITH",):
        return name.startswith(pattern)
    if op in ("ENDSWITH",):
        return name.endswith(pattern)
    logger.warning(
        "upload_matching: unknown file-name compare operator=%r, defaulting to LIKE/contains",
        operator,
    )
    return pattern in name


class FileRejected(Exception):  # noqa: N818 - established name, raised/caught across services + tests
    """Base for any reason a file can't be uploaded under any UploadID."""


class NoMatchingUploadRule(FileRejected):
    """No UploadID's pattern matched this file (extension is never a
    rejection reason on its own)."""


class ColumnCountMismatch(FileRejected):
    """File matched a pattern/extension but its column count didn't match."""


class EmptyFile(ColumnCountMismatch):
    """File matched a rule but contains no data line to validate.

    A subclass of ColumnCountMismatch so every existing handler keeps catching it — it
    is the same class of local rejection, just a more specific reason.
    """


class AmbiguousUploadRule(FileRejected):
    """Multiple equally-specific UploadIDs matched and extension + exchange
    couldn't single one out - reject loudly rather than silently pick wrong."""


def matches_rule(rule: UploadRule, file_name: str) -> bool:
    """Does this filename satisfy ONE known rule?

    `match_file` below answers a different question — *which* of many rules a file belongs to —
    and carries the tie-breaking and column-count machinery that goes with it. A caller that
    already knows the UploadID wants only the name check, and reaching into `_pattern_matches`
    from outside this module to get it would leave the operator semantics duplicated in two
    places. Added for the post-trade lane, where the slot names its own UploadID.
    """
    return _pattern_matches(rule.file_name_pattern, rule.compare_operator, file_name)


def fetch_upload_rules(
    candidates, client, segment: str = "", trade_date: str = ""
) -> list[UploadRule]:
    """Step 4: fetch upload settings for every distinct UploadID a batch's
    reservation offers (not just the first one), so every candidate's matching
    rule is known before any file is matched.

    `candidates` are cbos_client.UploadCandidate values; `client` is the CBOS
    client the batch is already using. Decoding each row into an UploadRule
    is the client's job.

    `segment` lets the client fall back to Step 40 for a slot whose Step-4
    pattern is blank - without it such a slot is dropped and its mandatory file
    can never be uploaded (see CBOSClient.upload_settings). `trade_date` is
    that same fallback's request parameter - pass it whenever `segment` is
    passed."""
    rules: list[UploadRule] = []
    seen_ids: set[str] = set()

    for candidate in candidates:
        # UPLOADID=0 marks a processing step (Brokerage Computation, Bill
        # Posting) - CBOS never expects a file there, so there are no upload
        # settings to fetch. Asking anyway is a call real CBOS may well reject,
        # and a Step 4 error propagates into process_batch's setup retry loop:
        # three attempts, then every file in the batch goes to uploadFailed.
        # The mock happens to answer with a phantom "UPLOAD 0" rule, which then
        # joins the matching pool - so this stayed invisible in MOCK mode.
        if not candidate.expects_a_file:
            continue

        upload_id = candidate.upload_id
        if upload_id in seen_ids:
            continue
        seen_ids.add(upload_id)

        rule = client.upload_settings(
            upload_id, fallback_name=candidate.name, segment=segment, trade_date=trade_date
        )
        if rule is not None:
            rules.append(rule)

    logger.info("Loaded %d Upload Rules from CBOS", len(rules))
    return rules


def _candidate_delimiters() -> tuple[str, ...]:
    """The configured delimiter first, then the fallbacks it does not already
    cover. Order matters: first is what an error message reports, so a
    comma-delimited estate reads exactly as it did before this fallback existed.
    """
    configured = settings.upload_match_delimiter
    return (configured, *(d for d in _FALLBACK_DELIMITERS if d != configured))


def _count_columns(file_path: Path) -> dict[str, list[int]] | None:
    """Best-effort column counts for the first COLUMN_SNIFF_LINES non-empty
    lines, counted under EACH of _candidate_delimiters(). Returns
    ``{delimiter: [width per sniffed line]}`` - every delimiter mapping to a
    list of the same length - or None if the file can't be read as delimited
    text (binary formats like .xlsx aren't sniffed here - see the module
    docstring's known limitation).

    SEVERAL lines, not just the first, because some exchange files open with a
    CONTROL RECORD whose width has nothing to do with the data. NCDEX's
    physical trade file is one:

        AL02,01240,17062026,1,120,120,0                     <- 7  (control)
        17-JUN-2026,D,2026016,FUTCOM,GUARGUM5,...           <- 20 (data)

    CBOS's rule 321 for that UploadID declares 20 - it describes the data
    rows. Reading only the first line saw 7, so a perfectly valid file the
    exchange had just published was rejected before CBOS ever saw it
    (observed live, trade date 2026-06-17).

    SEVERAL delimiters, for the same reason one axis over. Not every exchange
    file is a CSV. NSE's contract masters are PIPE-delimited - co_contract on
    trade date 2026-08-11 opens with a 3-field control record and then carries
    8.7MB of 69-field rows, which is exactly the width CBOS's rule 139
    declares. Counted on a comma every line is one field, so the sniff reported
    [1, 1, 1, 1, 1] against an expected 69 and rejected a file both NSE and
    CBOS agreed was correct.

    Since the engine's job is to pick the right UploadID and CBOS's own Step
    5/7/9 responses are the arbiter of acceptance (see the module docstring), a
    local sniff must not be the thing that blocks a real file.
    """
    lines: list[str] = []
    try:
        with open(file_path, encoding="utf-8", errors="strict", newline="") as fh:
            for line in fh:
                if not line.strip():
                    continue
                lines.append(line)
                if len(lines) >= COLUMN_SNIFF_LINES:
                    break
    except (UnicodeDecodeError, OSError) as exc:
        logger.debug("upload_matching: could not sniff columns for %s: %s", file_path.name, exc)
        return None
    # All-empty lists mean "read it fine, there was nothing in it" — NOT the same as
    # None, which means "could not be read as delimited text at all" (a .xlsx, say) and
    # is a legitimate reason to skip the check. Collapsing the two let a zero-byte file
    # take the .xlsx exemption and sail through to CBOS.
    return {
        delimiter: [len(next(csv.reader([line], delimiter=delimiter))) for line in lines]
        for delimiter in _candidate_delimiters()
    }


def _disambiguate(
    tied: list[UploadRule], extension: str, exchange: str | None, file_path: Path
) -> UploadRule:
    """Break a tie between equally-specific pattern matches using extension, then
    exchange (the exchange folder name usually appears in the CBOS label, e.g.
    'BSE SCRIP' vs 'NSE SCRIP'). Raises AmbiguousUploadRule if neither singles out
    one UploadID - a loud rejection beats a silent wrong UploadID."""
    pool = tied
    if extension:
        by_ext = [r for r in pool if r.extension and r.extension == extension]
        if len(by_ext) == 1:
            logger.info(
                "Tie broken by extension .%s -> UploadID=%s", extension, by_ext[0].upload_id
            )
            return by_ext[0]
        if by_ext:
            pool = by_ext
    if exchange:
        by_exch = [r for r in pool if exchange.upper() in r.name.upper()]
        if len(by_exch) == 1:
            logger.info("Tie broken by exchange %s -> UploadID=%s", exchange, by_exch[0].upload_id)
            return by_exch[0]
        if by_exch:
            pool = by_exch
    if len(pool) == 1:
        return pool[0]
    logger.warning(
        "upload_matching: REJECTED file=%s reason='ambiguous UploadID' candidates=%s",
        file_path.name,
        [(r.upload_id, r.name, r.extension) for r in pool],
    )
    raise AmbiguousUploadRule(
        f"'{file_path.name}' matches {len(pool)} equally-specific UploadIDs "
        f"{[(r.upload_id, r.name, r.extension) for r in pool]} - "
        f"extension/exchange couldn't disambiguate"
    )


def match_file(file_path: Path, rules: list[UploadRule], exchange: str | None = None) -> UploadRule:
    """Match one discovered file against every known UploadID rule.

    Pattern matching is MANDATORY - a rule only qualifies if its pattern is
    matched against the filename per the rule's compare operator.
    When several rules match, the longest pattern wins; if several tie on pattern
    length, the file's extension and its exchange folder break the tie (see
    _disambiguate). A single unambiguous match is NEVER rejected for a wrong
    extension - that's only a warning, since CBOS's own Step 5/7/9 responses are
    the real arbiter (SCRIP_123.xlsx still selects UploadID=81 even if 81 says TXT).

    Raises NoMatchingUploadRule if NO pattern matches, AmbiguousUploadRule if a
    tie can't be broken, ColumnCountMismatch if the matched rule's column count
    is checked and doesn't fit.

    The column check only rejects on evidence. When no candidate delimiter finds
    ANY column boundary the file's shape is unknown, not wrong, and the check is
    skipped with a warning - exactly as it already is for formats _count_columns
    cannot read at all."""
    name = file_path.stem.upper()
    extension = file_path.suffix.lstrip(".").upper()
    logger.info("File = %s (exchange=%s)", file_path.name, exchange)

    candidates = [
        r for r in rules if _pattern_matches(r.file_name_pattern.upper(), r.compare_operator, name)
    ]

    if not candidates:
        available_patterns = sorted({r.file_name_pattern for r in rules})
        logger.warning(
            "upload_matching: REJECTED file=%s reason='no UploadID pattern matched' "
            "available_patterns=%s",
            file_path.name,
            available_patterns,
        )
        raise NoMatchingUploadRule(
            f"'{file_path.name}' matches no known UploadID pattern - "
            f"available patterns={available_patterns}, checked {len(rules)} rule(s): "
            f"{[(r.upload_id, r.file_name_pattern, r.extension) for r in rules]}"
        )

    # Longest pattern wins; ties are broken by extension then exchange.
    candidates.sort(key=lambda r: len(r.file_name_pattern), reverse=True)
    top_len = len(candidates[0].file_name_pattern)
    tied = [r for r in candidates if len(r.file_name_pattern) == top_len]
    rule = tied[0] if len(tied) == 1 else _disambiguate(tied, extension, exchange, file_path)
    logger.info("Matched Pattern = %s", rule.file_name_pattern)

    if rule.extension and extension and rule.extension != extension:
        logger.warning(
            "Expected extension %s but found %s (file=%s, UploadID=%s) - uploading anyway",
            rule.extension,
            extension,
            file_path.name,
            rule.upload_id,
        )

    logger.info("Selected UploadID = %s", rule.upload_id)

    if settings.upload_match_validate_columns and rule.column_count is not None:
        sniffed = _count_columns(file_path)
        configured = settings.upload_match_delimiter
        # ANY sniffed line under ANY candidate delimiter matching is enough.
        # Strictly more permissive than both earlier versions of this check, so
        # nothing that uploads today can start failing - those files already
        # match on line 1 under the configured delimiter. What it adds is
        # control-record files and non-CSV files (see _count_columns), which
        # were being rejected over a shape CBOS's rule never described.
        if sniffed is not None:
            if not any(sniffed.values()):
                # Fail safe, not open. A zero-byte or all-blank placeholder that happens
                # to match an UploadID's filename pattern is exactly the kind of file
                # local validation exists to stop — forwarding it wastes a CBOS round
                # trip and lands an empty file in the billing data.
                raise EmptyFile(
                    f"'{file_path.name}' matched UploadID={rule.upload_id} ({rule.name}) "
                    f"but contains no data line to validate"
                )
            matched_delimiter = next(
                (d for d, widths in sniffed.items() if rule.column_count in widths), None
            )
            if matched_delimiter is None:
                # Width 1 under EVERY candidate delimiter on EVERY sniffed line means
                # the sniff never found a delimiter at all: the file is fixed-width, or
                # uses a separator _candidate_delimiters() does not try. That is the
                # SAME state of knowledge as the .xlsx case - where _count_columns
                # returns None and the check is skipped - so the two must agree, or a
                # file is refused for being unreadable by US rather than malformed.
                #
                # Observed live: BSE's SCRIP_CC (trade date 2026-08-11) sniffed
                # [1, 1, 1, 1, 1] against rule 81's declared 30 and never reached CBOS.
                # That is the third valid file this check has blocked (see
                # _count_columns for the NCDEX and NSE ones), so close the class rather
                # than add a fourth delimiter and wait for the fourth incident.
                #
                # Note this branch is unreachable when the rule declares 1 column - a
                # genuine single-column file matches above and never gets here.
                if all(width == 1 for widths in sniffed.values() for width in widths):
                    logger.warning(
                        "upload_matching: cannot see columns in %s under any of %s "
                        "(fixed-width, or a delimiter we don't try) - skipping the "
                        "column check, UploadID=%s declared %d; CBOS Step 5/7/9 arbitrates",
                        file_path.name,
                        ", ".join(repr(d) for d in sniffed),
                        rule.upload_id,
                        rule.column_count,
                    )
                    return rule
                # Reported under the CONFIGURED delimiter, which is the estate's norm and
                # what the reader will assume. The tried list is named so the next person
                # to read this is not misled the way a bare "has 1 column(s)" misled us.
                actual = sniffed[configured]
                raise ColumnCountMismatch(
                    f"'{file_path.name}' matched UploadID={rule.upload_id} ({rule.name}) "
                    f"but has {actual[0] if len(actual) == 1 else actual} "
                    f"column(s), expected {rule.column_count} "
                    f"(delimiters tried: {', '.join(repr(d) for d in sniffed)})"
                )
            widths = sniffed[matched_delimiter]
            if matched_delimiter != configured:
                # Not an error, but never silent: "this segment's file is not a CSV" is a
                # fact about the estate worth being able to grep for later.
                logger.info(
                    "upload_matching: %s is %r-delimited, not %r; matched UploadID=%s "
                    "on its %d-column rows",
                    file_path.name,
                    matched_delimiter,
                    configured,
                    rule.upload_id,
                    rule.column_count,
                )
            if widths[0] != rule.column_count:
                # Matched on a later line: the first line is a control record. Worth
                # a breadcrumb - it is the difference between "file is fine" and
                # "file is malformed" when someone reads this back.
                logger.info(
                    "upload_matching: %s opens with a %d-column control record; "
                    "matched UploadID=%s on a later %d-column data line",
                    file_path.name,
                    widths[0],
                    rule.upload_id,
                    rule.column_count,
                )

    return rule
