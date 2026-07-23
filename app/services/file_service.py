"""Filesystem-only concerns: moving processed files between source/uploaded/
failed locations. No database, queue, or network calls happen here - see
upload_service.py for orchestration and manifest_service.py for how work is
declared (there is no directory scanning anymore; the manifest lists the
files)."""

import logging
import shutil
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger("file_service")

UPLOAD_SUBFOLDER = "uploaded"
FAILED_SUBFOLDER = "uploadFailed"
# Recorded as the exchange for files whose manifest entry carries none.
# Audit/tie-break only - the exchange is never sent to CBOS.
NO_EXCHANGE = "NA"


def get_root() -> Path:
    return Path(settings.file_root_path)


def move_to_uploaded(file_path: Path) -> Path:
    return _move_file(file_path, UPLOAD_SUBFOLDER)


def move_to_failed(file_path: Path) -> Path:
    return _move_file(file_path, FAILED_SUBFOLDER)


def _move_file(file_path: Path, subfolder_name: str) -> Path:
    """Move file_path into a sibling subfolder (uploaded/ or uploadFailed/)
    of its parent (the date folder), creating the subfolder if needed.
    Removes the file from its source location.

    If a same-named file already sits at the destination (e.g. this file
    name was processed before, or was reprocessed after being manually
    re-dropped into the source folder), a numeric suffix is appended so the
    move never silently overwrites the earlier file and never collides with
    that earlier attempt's file_path in the uploaded_files table (file_path
    has a UNIQUE constraint - see UploadedFile.uq_uploaded_files_file_path)."""
    dest_dir = file_path.parent / subfolder_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / file_path.name
    if dest_path.exists():
        stem, suffix = file_path.stem, file_path.suffix
        counter = 2
        while dest_path.exists():
            dest_path = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        logger.warning(
            "_move_file: %s already exists at destination, renaming this attempt to %s to avoid overwriting it "
            "and to keep file_path unique",
            file_path.name, dest_path.name,
        )

    logger.debug("_move_file: %s -> %s", file_path, dest_path)
    shutil.move(str(file_path), str(dest_path))
    logger.info("_move_file: moved %s into %s/", file_path.name, subfolder_name)
    return dest_path
