"""Manifest intake: load, schema-validate, checksum-verify, and turn a
manifest.json into a SegmentBatchTask (see docs/BATCH_HANDOFF_CONTRACT.md and
docs/manifest.schema.json).

This replaces filesystem discovery as the ONLY way work enters the queue:
nothing is uploaded without a manifest. Filesystem-only concerns stay in
file_service; CBOS concerns stay in upload_service.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from edpb_core.manifest import ManifestValidationError, validate_manifest

from app.core.config import settings
from app.core.queue import SegmentBatchTask
from app.services import file_service

logger = logging.getLogger("manifest_service")

MANIFEST_NAME = "manifest.json"


class ManifestError(Exception):
    """Manifest unreadable or schema-invalid (HTTP 400 at the API layer)."""


class ChecksumMismatchError(Exception):
    """A listed file is missing or its sha256/size doesn't match (HTTP 422).
    The batch is rejected; files are left in place."""


@dataclass(frozen=True)
class LoadedManifest:
    batch_id: str
    segment: str
    trade_date: str          # ISO YYYY-MM-DD, as in the manifest
    folder_date: str         # DD-MM-YYYY, derived - the folder/batch key form
    correlation_id: str | None
    manifest_path: Path
    files: list[tuple[str, str]]  # (absolute file path, exchange) - SegmentBatchTask shape


def load_manifest(manifest_path: Path) -> LoadedManifest:
    """Read + schema-validate a manifest. Raises ManifestError on anything
    that makes the manifest itself untrustworthy; does NOT touch the listed
    files (that's verify_checksums, a separate, slower step)."""
    if not manifest_path.is_file():
        raise ManifestError(f"manifest not found: {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest unreadable: {exc}") from exc

    try:
        validate_manifest(data)  # THE schema, packaged in edpb-core
    except ManifestValidationError as exc:
        raise ManifestError(f"manifest schema-invalid: {exc}") from exc

    trade_date = data["trade_date"]
    folder_date = datetime.strptime(trade_date, "%Y-%m-%d").strftime(settings.date_folder_format)

    base = manifest_path.parent
    files = [
        (str(base / f["name"]), f.get("exchange") or file_service.NO_EXCHANGE)
        for f in data["files"]
    ]
    return LoadedManifest(
        batch_id=data["batch_id"],
        segment=data["segment"],
        trade_date=trade_date,
        folder_date=folder_date,
        correlation_id=data.get("correlation_id"),
        manifest_path=manifest_path,
        files=files,
    )


def verify_checksums(manifest_path: Path) -> None:
    """Confirm every listed file exists with the declared sha256 and size.
    Raises ChecksumMismatchError naming the first offending file."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    for entry in data["files"]:
        path = base / entry["name"]
        if not path.is_file():
            raise ChecksumMismatchError(f"listed file missing: {entry['name']}")
        size = path.stat().st_size
        if size != entry["size_bytes"]:
            raise ChecksumMismatchError(
                f"{entry['name']}: size {size} != declared {entry['size_bytes']}")
        digest = _sha256(path)
        if digest != entry["sha256"]:
            raise ChecksumMismatchError(
                f"{entry['name']}: sha256 {digest[:12]}... != declared {entry['sha256'][:12]}...")
    logger.info("Manifest %s: %d file(s) checksum-verified", manifest_path, len(data["files"]))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def to_task(manifest: LoadedManifest) -> SegmentBatchTask:
    return SegmentBatchTask(
        folder_date=manifest.folder_date,
        segment=manifest.segment,
        files=manifest.files,
        batch_id=manifest.batch_id,
        correlation_id=manifest.correlation_id,
    )


def find_manifests(root: Path) -> list[Path]:
    """Every manifest.json under {root}/{date}/{SEGMENT}/ - what
    POST /batches/rescan walks. Flat two-level layout per the contract."""
    if not root.is_dir():
        return []
    found = sorted(root.glob(f"*/*/{MANIFEST_NAME}"))
    logger.debug("find_manifests: %d manifest(s) under %s", len(found), root)
    return found
