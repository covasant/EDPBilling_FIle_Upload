"""End-to-end upload runner: take real downloaded files and push them through
the full CBOS sequence against the in-process mock, so the whole pipeline can be
watched step by step without touching real CBOS.

What it does:
  1. stages the downloaded files into the uploader's expected
     {FILE_ROOT}/{date}/{segment}/{exchange}/ layout,
  2. runs discovery -> queue -> process_batch (Steps 2->9) in MOCK mode,
  3. prints a summary (each file's matched UploadID, GUID, and outcome).

The downloader (mofsl_file_download_rpa_bot) writes {date}/MCX/<files> with no
exchange level; the uploader expects a {segment}/{exchange} tree, so this script
bridges that gap with a placeholder exchange folder (default "NA").

Run:
    uv run python -m scripts.e2e_upload
    uv run python -m scripts.e2e_upload --date 17-07-2026 --segment MCX \
        --source /home/dawood/projects/covasant/mofsl_file_download_rpa_bot/downloads/edpb/17-07-2026/MCX
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

DEFAULT_DOWNLOAD_ROOT = "/home/dawood/projects/covasant/mofsl_file_download_rpa_bot/downloads/edpb"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="End-to-end upload of downloaded files against the mock CBOS.")
    p.add_argument("--date", default="17-07-2026", help="trade date folder (DD-MM-YYYY)")
    p.add_argument("--segment", default="MCX")
    p.add_argument("--exchange", default="NA", help="placeholder exchange sub-folder (downloader has none)")
    p.add_argument("--source", default=None,
                   help="dir holding the downloaded files (default: the mofsl download repo for this date/segment)")
    p.add_argument("--work-dir", default=str(Path(__file__).resolve().parent.parent / ".e2e_work"),
                   help="scratch FILE_ROOT the run stages files into (wiped each run)")
    return p.parse_args()


def _configure_env(work_dir: Path) -> None:
    # Set BEFORE importing app modules. Config is lazy, so import is safe either
    # way, but this guarantees the run's settings win.
    os.environ["FILE_ROOT_PATH"] = str(work_dir)
    os.environ["DATABASE_URL"] = f"sqlite:///{work_dir / 'e2e.db'}"
    os.environ["CBOS_MODE"] = "MOCK"
    os.environ["LOG_LEVEL"] = "INFO"
    # Deterministic, fast mock: always succeed, one pending poll, no sleeping.
    os.environ["CBOS_MOCK_RANDOM_SUCCESS_RATE"] = "1.0"
    os.environ["CBOS_MOCK_PENDING_POLLS"] = "1"
    os.environ["CBOS_POLL_INTERVAL_SECONDS"] = "0"


def main() -> int:
    args = _parse_args()
    source = Path(args.source) if args.source else Path(DEFAULT_DOWNLOAD_ROOT) / args.date / args.segment
    work_dir = Path(args.work_dir)

    if not source.is_dir():
        print(f"ERROR: source dir does not exist: {source}")
        return 2
    src_files = sorted(p for p in source.iterdir() if p.is_file())
    if not src_files:
        print(f"ERROR: no files in source dir: {source}")
        return 2

    # Fresh staging area each run (also drops the previous sqlite DB).
    if work_dir.exists():
        shutil.rmtree(work_dir)
    stage_dir = work_dir / args.date / args.segment / args.exchange
    stage_dir.mkdir(parents=True, exist_ok=True)
    for f in src_files:
        shutil.copy2(f, stage_dir / f.name)

    print("=" * 78)
    print(f"Staged {len(src_files)} file(s) into {stage_dir}")
    for f in src_files:
        print(f"  - {f.name} ({f.stat().st_size:,} bytes)")
    print(f"CBOS_MODE=MOCK  segment={args.segment}  date={args.date}")
    print("=" * 78)

    _configure_env(work_dir)

    # Import only after env is set.
    from app.clients import cbos_client
    from app.core import database
    from app.core.config import get_settings
    from app.core.logging import configure_logging
    from app.core.queue import file_queue, release
    from app.models.uploaded_file import UploadedFile
    from app.services import upload_service
    from app.services.file_service import get_root

    get_settings.cache_clear()
    database.get_engine.cache_clear()
    database.get_sessionmaker.cache_clear()
    cbos_client.reset_cbos_client()
    configure_logging()
    database.init_db()

    # Discovery for this specific date (bypassing the scheduler's today/T-1
    # window), then drain the queue exactly as the worker would.
    upload_service._discover_date(get_root(), args.date)

    processed = 0
    while not file_queue.empty():
        task = file_queue.get()
        try:
            upload_service.process_batch(task)
            processed += 1
        finally:
            release(task.key)
            file_queue.task_done()

    # Summary straight from the audit table.
    print("\n" + "=" * 78)
    print(f"RESULT: processed {processed} batch(es)")
    session = database.get_sessionmaker()()
    try:
        rows = session.query(UploadedFile).order_by(UploadedFile.id).all()
        for r in rows:
            print(f"  [{r.status:8}] {Path(r.file_path).name}")
            print(f"             UploadID={r.cbos_upload_id}  pattern={r.matched_pattern!r}  "
                  f"guid={r.guid}  process_id={r.process_id}")
            if r.validation_error:
                print(f"             validation_error: {r.validation_error}")
    finally:
        session.close()

    uploaded = list((work_dir / args.date / args.segment / args.exchange / "uploaded").glob("*"))
    failed = list((work_dir / args.date / args.segment / args.exchange / "uploadFailed").glob("*"))
    print(f"\n  uploaded/     : {[p.name for p in uploaded]}")
    print(f"  uploadFailed/ : {[p.name for p in failed]}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
