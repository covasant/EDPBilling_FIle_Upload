"""End-to-end tests for the settlement upload API, against the in-process
MOCK DP upload client. Mirrors tests/test_batches_api.py's structure."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _fast(monkeypatch):
    monkeypatch.setenv("CBOS_SETL_MOCK_RANDOM_SUCCESS_RATE", "1.0")
    monkeypatch.setenv("CBOS_SETL_MOCK_PENDING_POLLS", "0")
    monkeypatch.setenv("CBOS_SETL_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("CBOS_SETL_RETRY_DELAY_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()


@pytest.fixture()
def client(monkeypatch):
    _fast(monkeypatch)
    import app.main as main_module

    def _no_worker(queue):  # billing's worker thread body; not relevant here
        return

    monkeypatch.setattr(main_module, "run_worker", _no_worker)
    with TestClient(main_module.app) as c:
        yield c


def _shared_folder() -> Path:
    from app.core.config import settings

    return Path(settings.cbos_setl_shared_folder_path)


def _drop_file(name: str, content: bytes = b"col1|col2\nval1|val2\n") -> None:
    (_shared_folder() / name).write_bytes(content)


def test_upload_masters_list_is_ops_tooling(client):
    resp = client.get("/settlements/upload-masters")
    assert resp.status_code == 200
    assert resp.json()


def test_upload_master_details_returns_config(client):
    resp = client.get("/settlements/upload-masters/22")
    assert resp.status_code == 200
    assert resp.json()["process_required"] == "1"


def test_submit_upload_success_runs_full_flow_and_process_step(client):
    _drop_file("success_settlement.csv")
    resp = client.post(
        "/settlements/uploads",
        json={"upload_id": 22, "file_name": "success_settlement.csv", "correlation_id": "corr-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "processed"  # mock config has process_required=True
    assert body["tran_id"]
    assert body["correlation_id"] == "corr-1"

    detail = client.get(f"/settlements/uploads/{body['settlement_upload_id']}").json()
    assert detail["status"] == "processed"
    assert detail["chunk_full_path"].endswith("success_settlement.csv")
    assert detail["guid"]


def test_submit_upload_missing_file_is_404(client):
    resp = client.post(
        "/settlements/uploads", json={"upload_id": 22, "file_name": "does_not_exist.csv"}
    )
    assert resp.status_code == 404


def test_submit_upload_validation_failure_marks_failed(client):
    _drop_file("fail_settlement.csv")
    resp = client.post(
        "/settlements/uploads", json={"upload_id": 22, "file_name": "fail_settlement.csv"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"

    detail = client.get(f"/settlements/uploads/{body['settlement_upload_id']}").json()
    assert detail["status"] == "failed"
    assert detail["retry_count"] >= 1


def test_get_unknown_upload_is_404(client):
    resp = client.get("/settlements/uploads/999999")
    assert resp.status_code == 404


def test_status_endpoint_repolls_without_replaying_upload(client):
    _drop_file("success_status.csv")
    submit = client.post(
        "/settlements/uploads", json={"upload_id": 22, "file_name": "success_status.csv"}
    )
    settlement_upload_id = submit.json()["settlement_upload_id"]

    status_resp = client.get(f"/settlements/uploads/{settlement_upload_id}/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] in ("success", "processed")


def test_status_endpoint_unknown_id_is_404(client):
    resp = client.get("/settlements/uploads/999999/status")
    assert resp.status_code == 404
