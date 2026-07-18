"""Filesystem-only concerns: walking the segment/date tree, and moving files
between source/upload/fail locations. No database, queue, or network calls
happen here - see upload_service.py for orchestration."""

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger("file_service")
settings = get_settings()

UPLOAD_SUBFOLDER = "uploaded"
FAILED_SUBFOLDER = "uploadFailed"
# Never treated as a segment/date folder / never scanned for source files.
_PROCESSED_SUBFOLDER_NAMES = {UPLOAD_SUBFOLDER, FAILED_SUBFOLDER}


def get_root() -> Path:
    return Path(settings.file_root_path)


def get_today_folder_name() -> str:
    return datetime.now().strftime(settings.date_folder_format)


def get_processing_dates() -> list[str]:
    """Dates the scheduler should scan, most recent first: [T, T-1, ...]."""
    today = datetime.now()
    dates = []
    for i in range(settings.scan_days_back + 1):
        d = today - timedelta(days=i)
        dates.append(d.strftime(settings.date_folder_format))
    return dates


def list_subdirs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [
        p
        for p in folder.iterdir()
        if p.is_dir() and p.name not in _PROCESSED_SUBFOLDER_NAMES
    ]


def list_files(folder: Path) -> list[Path]:
    """Files directly under `folder`. Never recurses, so uploaded/ and uploadFailed/
    subfolders are naturally excluded."""
    if not folder.is_dir():
        return []
    return [p for p in folder.iterdir() if p.is_file()]


def discover_files_for_date(root: Path, folder_date: str):
    """Yields (file_path, segment, exchange) for every source file found directly
    under root/{folder_date}/{segment}/{exchange}/."""
    date_folder = root / folder_date
    logger.debug("discover_files_for_date: scanning %s", date_folder)
    if not date_folder.is_dir():
        logger.debug("discover_files_for_date: %s does not exist, skipping", date_folder)
        return

    for segment_folder in list_subdirs(date_folder):
        for exchange_folder in list_subdirs(segment_folder):
            files = list_files(exchange_folder)
            logger.debug(
                "discover_files_for_date: %s/%s -> %d file(s)",
                segment_folder.name, exchange_folder.name, len(files),
            )
            for file_path in files:
                yield file_path, segment_folder.name, exchange_folder.name


def build_destination_dir(segment: str, exchange: str, folder_date: str | None = None) -> Path:
    """Build {FILE_ROOT_PATH}/{date}/{segment}/{exchange}/ for a given segment and exchange."""
    folder_date = folder_date or get_today_folder_name()
    return get_root() / folder_date / segment / exchange


def save_uploaded_file(content: bytes, file_name: str, segment: str, exchange: str) -> Path:
    """Write uploaded bytes to the standard segment/exchange/date folder, creating dirs as needed."""
    dest_dir = build_destination_dir(segment, exchange)
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / file_name
    with open(dest_path, "wb") as fh:
        fh.write(content)
    logger.info("save_uploaded_file: wrote %s (%d bytes)", dest_path, len(content))

    return dest_path


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
