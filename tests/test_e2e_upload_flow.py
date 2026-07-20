"""Self-contained end-to-end test of the full CBOS sequence (Steps 2->9) in
MOCK mode, using synthetic MCX files that mirror the real download output
(correct patterns + column counts). No network, no external files.

This is the repeatable version of scripts/e2e_upload.py.
"""

from pathlib import Path


def test_full_mcx_batch_uploads_all_three(monkeypatch):
    # deterministic, instant mock
    monkeypatch.setenv("CBOS_MOCK_RANDOM_SUCCESS_RATE", "1.0")
    monkeypatch.setenv("CBOS_MOCK_PENDING_POLLS", "0")
    monkeypatch.setenv("CBOS_POLL_INTERVAL_SECONDS", "0")

    from app.core import database
    from app.core.config import get_settings, settings
    from app.core.queue import SegmentBatchTask
    from app.models.uploaded_file import UploadedFile
    from app.services import upload_service

    get_settings.cache_clear()
    database.get_engine.cache_clear()
    database.get_sessionmaker.cache_clear()
    database.init_db()

    date, segment, exchange = "17-07-2026", "MCX", "NA"
    folder = Path(settings.file_root_path) / date / segment / exchange
    folder.mkdir(parents=True)

    # name -> (pattern-bearing filename, expected column count from the mock settings)
    specs = {
        "MCX_ProductMaster.csv": 68,                                       # -> UploadID 127
        "Position_MCXCCL_CO_0_CM_55930_20260717_F_0000.csv": 46,          # -> UploadID 534
        "Trade_MCX_CO_0_CM_55930_20260717_F_0000.csv": 46,               # -> UploadID 535
    }
    paths = []
    for name, cols in specs.items():
        p = folder / name
        p.write_text(",".join(str(i) for i in range(cols)) + "\n")
        paths.append(str(p))

    from app.clients import cbos_client

    client = cbos_client.get_cbos_client()
    task = SegmentBatchTask(folder_date=date, segment=segment, exchange=exchange, file_paths=paths)
    upload_service.process_batch(task)

    session = database.get_sessionmaker()()
    try:
        rows = session.query(UploadedFile).all()
        assert len(rows) == 3
        assert {r.status for r in rows} == {"uploaded"}
        assert {r.cbos_upload_id for r in rows} == {"127", "534", "535"}
        assert all(r.guid and r.process_id for r in rows)
    finally:
        session.close()

    # Boundary: the empty non-zero slot (320 = STEPNO 4, no file today) is marked
    # optional via Step 8, so FILEUPLOAD can reach TRUE.
    assert 4 in [stepno for _, stepno in client.marked_optional]


def test_uploader_does_not_trigger():
    """This repo owns the upload lane only - the CBOS trigger (Step 10) belongs
    to EDP_Billing. The client must not expose a trigger call."""
    from app.clients import cbos_client

    assert not hasattr(cbos_client, "trigger_process")
    assert not hasattr(cbos_client.get_cbos_client(), "trigger_process")
