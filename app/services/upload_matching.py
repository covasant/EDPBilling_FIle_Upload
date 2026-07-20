"""Resolves which CBOS UploadID a discovered file belongs to, and validates
it against that UploadID's file-name pattern / extension / column-count
rules fetched from CBOS (Step 4: GetNewTradeProcessPromodalUploadSettings).

This replaces the previous (incorrect) behavior of always uploading every
file in a segment/exchange folder under Table2's first UploadID. Every
UploadID CBOS offers for the batch's process is fetched once
(fetch_upload_rules), then every discovered file is matched independently
against that full rule set (match_file).

Pattern matching is mandatory - a file must contain a rule's FILE NAME
pattern somewhere in its name to be selected at all. Extension matching is
NOT mandatory: a matched file is never rejected for having a different
extension than Upload Settings declared - a mismatch is only logged as a
warning, and the file still proceeds to upload under that UploadID. CBOS's
own Step 5/7/9 responses are the actual arbiter of whether the file is
ultimately accepted; this engine's job is only to pick the right UploadID.
"""

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger("upload_matching")


@dataclass(frozen=True)
class UploadRule:
    upload_id: str
    name: str
    file_name_pattern: str
    compare_operator: str
    extension: str
    column_count: int | None
    raw_settings: dict


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
    logger.warning("upload_matching: unknown file-name compare operator=%r, defaulting to LIKE/contains", operator)
    return pattern in name


class FileRejected(Exception):
    """Base for any reason a file can't be uploaded under any UploadID."""


class NoMatchingUploadRule(FileRejected):
    """No UploadID's pattern matched this file (extension is never a
    rejection reason on its own)."""


class ColumnCountMismatch(FileRejected):
    """File matched a pattern/extension but its column count didn't match."""


class AmbiguousUploadRule(FileRejected):
    """Multiple equally-specific UploadIDs matched and extension + exchange
    couldn't single one out - reject loudly rather than silently pick wrong."""


def _extract_pattern(setting: dict) -> tuple[str, str]:
    """Find the file-name pattern in a Step-4 row, and any match operator that
    came with it. Returns (pattern, operator_from_key).

    Real CBOS bakes the semantics into the key name and sends no separate
    operator field at all:

        "FILE NAME (CONTAINS)": "MCX_PRODUCTMASTER"

    So the parenthetical IS the operator. Any key beginning "FILE NAME" is
    treated as the pattern field and its parenthetical (if any) as the
    operator, which covers (CONTAINS) and whatever other variants CBOS uses
    without needing a code change per variant. The older documented spellings
    ("FILE NAME", "FileNameToCompare") still work and simply yield no operator
    hint, falling back to CBOS's default containment behaviour.
    """
    for key, value in setting.items():
        if not str(key).strip().upper().startswith("FILE NAME"):
            continue
        pattern = str(value or "").strip()
        if not pattern:
            continue
        operator = ""
        if "(" in key and ")" in key:
            operator = key[key.index("(") + 1:key.rindex(")")].strip()
        return pattern, operator

    # Documented alternate spelling, no operator baked in.
    return str(setting.get("FileNameToCompare") or "").strip(), ""


def parse_upload_rule(upload_id: str, setting: dict, fallback_name: str = "") -> UploadRule | None:
    """Turn one raw Step-4 settings row into an UploadRule. Pure - takes the
    row CBOS returned, makes no calls of its own.

    Returns None if the row can't produce a usable rule, which is a skip and
    never an error: a slot with no pattern or no extension simply can't match
    anything.

    Tolerances that exist because CBOS's rows aren't uniform:
      - the pattern key carries its own operator - "FILE NAME (CONTAINS)" -
        and real CBOS sends no separate operator field at all (see
        _extract_pattern); the documented "FILE NAME" / "FileNameToCompare"
        spellings still work
      - the extension as "FILEEXTENSION" or "FileExtension", and may carry a
        leading dot or any case
      - an explicit FileNameCompareOperator, if CBOS ever sends one, wins over
        the key's parenthetical; with neither, CBOS's default containment
        behaviour (LIKE) applies
      - the column count may be absent, blank, "-", or non-numeric; any of
        those means "don't check columns" rather than "reject this slot"
    """
    pattern, operator_from_key = _extract_pattern(setting)
    compare_operator = str(
        setting.get("FileNameCompareOperator") or operator_from_key or "LIKE"
    ).strip()
    extension = str(setting.get("FILEEXTENSION") or setting.get("FileExtension") or "").strip().lstrip(".").upper()

    if not pattern or not extension:
        logger.warning(
            "upload_matching: incomplete upload settings for UPLOADID=%s (%s), skipping", upload_id, setting
        )
        return None

    raw_columns = setting.get("NO. OF COLUMNS")
    column_count = None
    if raw_columns not in (None, "", "-"):
        try:
            column_count = int(raw_columns)
        except (TypeError, ValueError):
            logger.warning(
                "upload_matching: non-numeric column count %r for UPLOADID=%s, ignoring", raw_columns, upload_id
            )

    return UploadRule(
        upload_id=upload_id,
        name=str(setting.get("NAME") or fallback_name or ""),
        file_name_pattern=pattern,
        compare_operator=compare_operator,
        extension=extension,
        column_count=column_count,
        raw_settings=setting,
    )


def fetch_upload_rules(candidates, client) -> list[UploadRule]:
    """Step 4: fetch upload settings for every distinct UploadID a batch's
    reservation offers (not just the first one), so every candidate's matching
    rule is known before any file is matched.

    `candidates` are cbos_client.UploadCandidate values; `client` is the CBOS
    client the batch is already using. Interpreting each row is
    parse_upload_rule's job."""
    rules: list[UploadRule] = []
    seen_ids: set[str] = set()

    for candidate in candidates:
        upload_id = candidate.upload_id
        if upload_id in seen_ids:
            continue
        seen_ids.add(upload_id)

        setting = client.upload_settings(upload_id)
        if setting is None:
            continue

        rule = parse_upload_rule(upload_id, setting, fallback_name=candidate.name)
        if rule is not None:
            rules.append(rule)

    logger.info("Loaded %d Upload Rules from CBOS", len(rules))
    return rules


def _count_columns(file_path: Path) -> int | None:
    """Best-effort column count from the first non-empty line, split on
    settings.upload_match_delimiter. Returns None if the file can't be read
    as delimited text (binary formats like .xlsx aren't sniffed here - see
    the module docstring's known limitation)."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="strict", newline="") as fh:
            for line in fh:
                if line.strip():
                    return len(next(csv.reader([line], delimiter=settings.upload_match_delimiter)))
    except (UnicodeDecodeError, OSError) as exc:
        logger.debug("upload_matching: could not sniff columns for %s: %s", file_path.name, exc)
        return None
    return None


def _disambiguate(tied: list[UploadRule], extension: str, exchange: str | None, file_path: Path) -> UploadRule:
    """Break a tie between equally-specific pattern matches using extension, then
    exchange (the exchange folder name usually appears in the CBOS label, e.g.
    'BSE SCRIP' vs 'NSE SCRIP'). Raises AmbiguousUploadRule if neither singles out
    one UploadID - a loud rejection beats a silent wrong UploadID."""
    pool = tied
    if extension:
        by_ext = [r for r in pool if r.extension and r.extension == extension]
        if len(by_ext) == 1:
            logger.info("Tie broken by extension .%s -> UploadID=%s", extension, by_ext[0].upload_id)
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
    logger.warning("upload_matching: REJECTED file=%s reason='ambiguous UploadID' candidates=%s",
                   file_path.name, [(r.upload_id, r.name, r.extension) for r in pool])
    raise AmbiguousUploadRule(
        f"'{file_path.name}' matches {len(pool)} equally-specific UploadIDs "
        f"{[(r.upload_id, r.name, r.extension) for r in pool]} - extension/exchange couldn't disambiguate"
    )


def match_file(file_path: Path, rules: list[UploadRule], exchange: str | None = None) -> UploadRule:
    """Match one discovered file against every known UploadID rule.

    Pattern matching is MANDATORY - a rule only qualifies if its pattern is
    contained in the filename (FileNameToCompare / LIKE-style containment).
    When several rules match, the longest pattern wins; if several tie on pattern
    length, the file's extension and its exchange folder break the tie (see
    _disambiguate). A single unambiguous match is NEVER rejected for a wrong
    extension - that's only a warning, since CBOS's own Step 5/7/9 responses are
    the real arbiter (SCRIP_123.xlsx still selects UploadID=81 even if 81 says TXT).

    Raises NoMatchingUploadRule if NO pattern matches, AmbiguousUploadRule if a
    tie can't be broken, ColumnCountMismatch if the matched rule's column count
    is checked and doesn't fit."""
    name = file_path.stem.upper()
    extension = file_path.suffix.lstrip(".").upper()
    logger.info("File = %s (exchange=%s)", file_path.name, exchange)

    candidates = [r for r in rules if _pattern_matches(r.file_name_pattern.upper(), r.compare_operator, name)]

    if not candidates:
        available_patterns = sorted({r.file_name_pattern for r in rules})
        logger.warning(
            "upload_matching: REJECTED file=%s reason='no UploadID pattern matched' available_patterns=%s",
            file_path.name, available_patterns,
        )
        raise NoMatchingUploadRule(
            f"'{file_path.name}' matches no known UploadID pattern - available patterns={available_patterns}, "
            f"checked {len(rules)} rule(s): {[(r.upload_id, r.file_name_pattern, r.extension) for r in rules]}"
        )

    # Longest pattern wins; ties are broken by extension then exchange.
    candidates.sort(key=lambda r: len(r.file_name_pattern), reverse=True)
    top_len = len(candidates[0].file_name_pattern)
    tied = [r for r in candidates if len(r.file_name_pattern) == top_len]
    rule = tied[0] if len(tied) == 1 else _disambiguate(tied, extension, exchange, file_path)
    logger.info("Matched Pattern = %s", rule.file_name_pattern)

    if rule.extension and extension and rule.extension != extension:
        logger.warning("Expected extension %s but found %s (file=%s, UploadID=%s) - uploading anyway",
                        rule.extension, extension, file_path.name, rule.upload_id)

    logger.info("Selected UploadID = %s", rule.upload_id)

    if settings.upload_match_validate_columns and rule.column_count is not None:
        actual = _count_columns(file_path)
        if actual is not None and actual != rule.column_count:
            raise ColumnCountMismatch(
                f"'{file_path.name}' matched UploadID={rule.upload_id} ({rule.name}) but has {actual} "
                f"column(s), expected {rule.column_count}"
            )

    return rule
