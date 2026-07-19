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

from app.clients import cbos_client
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
    """Apply CBOS's own FileNameCompareOperator against the filename. The
    operator is whatever CBOS's Step 4 response declares for this UploadID -
    never assumed - so adding a new operator value in CBOS requires no code
    change here as long as it maps to one of these comparison semantics."""
    op = operator.strip().upper()
    if op in ("LIKE", "CONTAINS", ""):
        return pattern in name
    if op in ("EQUALS", "EQUAL", "="):
        return pattern == name
    if op in ("STARTSWITH", "STARTS_WITH"):
        return name.startswith(pattern)
    if op in ("ENDSWITH", "ENDS_WITH"):
        return name.endswith(pattern)
    logger.warning("upload_matching: unknown FileNameCompareOperator=%r, defaulting to LIKE/contains", operator)
    return pattern in name


class FileRejected(Exception):
    """Base for any reason a file can't be uploaded under any UploadID."""


class NoMatchingUploadRule(FileRejected):
    """No UploadID's pattern matched this file (extension is never a
    rejection reason on its own)."""


class ColumnCountMismatch(FileRejected):
    """File matched a pattern/extension but its column count didn't match."""


def fetch_upload_rules(table2: list[dict]) -> list[UploadRule]:
    """Step 4: fetch upload settings for every distinct UploadID in Table2
    (not just the first one), so every candidate's matching rule is known
    before any file is matched."""
    rules: list[UploadRule] = []
    seen_ids: set[str] = set()

    for candidate in table2:
        raw_upload_id = candidate.get("UPLOADID")
        if raw_upload_id is None:
            continue
        upload_id = str(raw_upload_id)
        if upload_id in seen_ids:
            continue
        seen_ids.add(upload_id)

        response = cbos_client.get_upload_settings(upload_id)
        result = response.get("Result") or []
        if not result:
            logger.warning("upload_matching: no upload settings returned for UPLOADID=%s, skipping", upload_id)
            continue

        setting = result[0]
        pattern = str(setting.get("FILE NAME") or setting.get("FileNameToCompare") or "").strip()
        compare_operator = str(setting.get("FileNameCompareOperator") or "LIKE").strip()
        extension = str(setting.get("FILEEXTENSION") or setting.get("FileExtension") or "").strip().lstrip(".").upper()
        raw_columns = setting.get("NO. OF COLUMNS")

        if not pattern or not extension:
            logger.warning(
                "upload_matching: incomplete upload settings for UPLOADID=%s (%s), skipping", upload_id, setting
            )
            continue

        column_count = None
        if raw_columns not in (None, "", "-"):
            try:
                column_count = int(raw_columns)
            except (TypeError, ValueError):
                logger.warning(
                    "upload_matching: non-numeric column count %r for UPLOADID=%s, ignoring", raw_columns, upload_id
                )

        rules.append(UploadRule(
            upload_id=upload_id,
            name=str(setting.get("NAME") or candidate.get("NAME") or ""),
            file_name_pattern=pattern,
            compare_operator=compare_operator,
            extension=extension,
            column_count=column_count,
            raw_settings=setting,
        ))

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


def match_file(file_path: Path, rules: list[UploadRule]) -> UploadRule:
    """Match one discovered file against every known UploadID rule.

    Pattern matching is MANDATORY - a rule only qualifies if its pattern is
    contained in the filename (FileNameToCompare / LIKE-style containment).
    Extension matching is NOT a rejection criterion: a file is never turned
    away just because its extension differs from what Upload Settings
    declared for the matched UploadID (SCRIP_123.xlsx still selects
    UploadID=81 even though 81's settings say TXT) - a mismatch is only
    logged as a warning, and the file proceeds to upload regardless. CBOS's
    own Step 5/7/9 responses are the real arbiter of whether that file/
    extension combination is actually accepted.

    Raises NoMatchingUploadRule only if NO rule's pattern is found in the
    filename at all. Raises ColumnCountMismatch if the matched rule's
    column count is checked and doesn't fit (independent of extension)."""
    name = file_path.stem.upper()
    extension = file_path.suffix.lstrip(".").upper()
    logger.info("File = %s", file_path.name)

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

    # Longest pattern wins: if a filename satisfies more than one rule's
    # correct one, not just whichever rule happened to load first.
    candidates.sort(key=lambda r: len(r.file_name_pattern), reverse=True)
    if len(candidates) > 1 and len(candidates[0].file_name_pattern) == len(candidates[1].file_name_pattern):
        logger.warning(
            "upload_matching: '%s' matched %d equally-specific UploadID rules (%s) - using UploadID=%s",
            file_path.name, len(candidates), [c.upload_id for c in candidates], candidates[0].upload_id,
        )
    rule = candidates[0]
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
