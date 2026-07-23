"""End-to-end upload runner: take real downloaded files and push them through
the full manifest -> POST-/batches -> CBOS sequence against the in-process
mock, so the whole pipeline can be watched step by step without touching real
CBOS.

What it does (mirroring docs/BATCH_HANDOFF_CONTRACT.md):
  1. stages the downloaded files into the uploader's flat
     {FILE_ROOT}/{date}/{SEGMENT}/ layout and writes a manifest.json for them
     (sha256s and all - playing the bot's finalization protocol),
  2. runs manifest intake -> queue -> process_batch (Steps 1->9) in MOCK
     mode,
  3. prints a summary (batch status, each file's matched UploadID, GUID, and
     outcome).

The completeness gate is live here too: staging only a subset of the
segment's mandatory files parks the batch INCOMPLETE - exactly what
production would do.

Run:
    uv run python -m scripts.e2e_upload
    uv run python -m scripts.e2e_upload --date 17-07-2026 --segment MCX \
        --source .../mofsl_file_download_rpa_bot/downloads/edpb/17-07-2026/MCX
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

DEFAULT_DOWNLOAD_ROOT = ("/home/dawood/projects/covasant/mofsl/"
                         "mofsl_file_download_rpa_bot/downloads/edpb")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="End-to-end manifest upload of downloaded files against the mock CBOS.")
    p.add_argument("--date", default="17-07-2026", help="trade date folder (DD-MM-YYYY)")
    p.add_argument("--segment", default="MCX")
    p.add_argument("--source", default=None,
                   help="dir holding the downloaded files "
                        "(default: the mofsl download repo for this date/segment)")
    p.add_argument("--work-dir",
                   default=str(Path(__file__).resolve().parent.parent / ".e2e_work"),
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


def _write_manifest(stage_dir: Path, files: list[Path], segment: str, folder_date: str) -> Path:
    """Play the bot's finalization protocol: sha256 every staged file, write
    manifest.json last."""
    trade_date = datetime.strptime(folder_date, "%d-%m-%Y").strftime("%Y-%m-%d")
    entries = []
    for f in files:
        body = (stage_dir / f.name).read_bytes()
        entries.append({
            "name": f.name,
            "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body),
            "exchange": segment,
        })
    manifest = {
        "manifest_version": 1,
        "batch_id": f"{segment}-{trade_date}-{uuid.uuid4().hex[:8]}",
        "segment": segment,
        "trade_date": trade_date,
        "correlation_id": f"e2e-{uuid.uuid4().hex[:8]}",
        "producer": {"name": "scripts.e2e_upload", "version": "dev", "action": "all"},
        "created_at": f"{trade_date}T00:00:00+05:30",
        "files": entries,
        "download_outcome": {"status": "success", "no_data": [], "failed": []},
    }
    manifest_path = stage_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path


def main() -> int:
    args = _parse_args()
    source = (Path(args.source) if args.source
              else Path(DEFAULT_DOWNLOAD_ROOT) / args.date / args.segment)
    work_dir = Path(args.work_dir)

    if not source.is_dir():
        print(f"ERROR: source dir does not exist: {source}")
        return 2
    src_files = sorted(p for p in source.iterdir() if p.is_file() and p.name != "manifest.json")
    if not src_files:
        print(f"ERROR: no files in source dir: {source}")
        return 2

    # Fresh staging area each run (also drops the previous sqlite DB). Flat
    # {date}/{SEGMENT}/ layout - no exchange level (BATCH_HANDOFF_CONTRACT.md).
    if work_dir.exists():
        shutil.rmtree(work_dir)
    stage_dir = work_dir / args.date / args.segment
    stage_dir.mkdir(parents=True, exist_ok=True)
    for f in src_files:
        shutil.copy2(f, stage_dir / f.name)
    manifest_path = _write_manifest(stage_dir, src_files, args.segment, args.date)

    print("=" * 78)
    print(f"Staged {len(src_files)} file(s) + manifest into {stage_dir}")
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
    from app.core.queue import BatchQueue
    from app.models.batch import Batch
    from app.models.uploaded_file import UploadedFile
    from app.repositories.batch_repository import BatchRepository
    from app.services import manifest_service, upload_service

    get_settings.cache_clear()
    database.get_engine.cache_clear()
    database.get_sessionmaker.cache_clear()
    cbos_client.reset_cbos_client()
    configure_logging()
    database.init_db()

    # Manifest intake exactly as POST /batches does it, then drain the queue
    # exactly as the worker would.
    manifest = manifest_service.load_manifest(manifest_path)
    manifest_service.verify_checksums(manifest_path)
    session = database.get_sessionmaker()()
    try:
        BatchRepository(session).create(
            batch_id=manifest.batch_id, segment=manifest.segment,
            trade_date=manifest.trade_date, folder_date=manifest.folder_date,
            manifest_path=str(manifest_path), correlation_id=manifest.correlation_id,
        )
    finally:
        session.close()

    queue = BatchQueue()
    queue.enqueue(manifest_service.to_task(manifest))

    processed = 0
    while not queue.empty():
        task = queue.get()
        try:
            upload_service.process_batch(task)
            processed += 1
        finally:
            queue.release(task.key)
            queue.task_done()

    # Summary straight from the batch + audit tables.
    print("\n" + "=" * 78)
    session = database.get_sessionmaker()()
    try:
        batch = session.query(Batch).one()
        print(f"RESULT: batch {batch.batch_id} -> {batch.status.upper()}")
        if batch.status_detail:
            print(f"        detail: {batch.status_detail}")
        rows = session.query(UploadedFile).order_by(UploadedFile.id).all()
        for r in rows:
            print(f"  [{r.status:8}] {Path(r.file_path).name}")
            print(f"             UploadID={r.cbos_upload_id}  pattern={r.matched_pattern!r}  "
                  f"guid={r.guid}  process_id={r.process_id}")
            if r.validation_error:
                print(f"             validation_error: {r.validation_error}")
    finally:
        session.close()

    uploaded = list((stage_dir / "uploaded").glob("*"))
    failed = list((stage_dir / "uploadFailed").glob("*"))
    print(f"\n  uploaded/     : {[p.name for p in uploaded]}")
    print(f"  uploadFailed/ : {[p.name for p in failed]}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
