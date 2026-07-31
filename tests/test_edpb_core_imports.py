import importlib
import sys


def test_batch_service_imports_without_external_edpb_core(monkeypatch):
    monkeypatch.setenv("FILE_ROOT_PATH", ".")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")

    for name in ["edpb_core", "edpb_core.batch_api", "edpb_core.manifest"]:
        sys.modules.pop(name, None)

    module = importlib.import_module("app.services.batch_service")

    assert module.IntakeResult.__name__ == "IntakeResult"
