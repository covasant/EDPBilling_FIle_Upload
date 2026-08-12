"""C1/H2: a caller-supplied name or path must not escape the directory it belongs to.

Not only a security property. `Path("/shared") / "/etc/passwd"` is `/etc/passwd` —
pathlib DISCARDS the left side when the right is absolute, silently — so an upstream
caller that sends a full path where a bare name was expected hits exactly the same code
as an attacker would. On the settlement side that uploads the wrong file to the external
DP endpoint; on the manifest side it `shutil.move`s real files out of the intake tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.safe_path import UnsafePathError, assert_within, resolve_within

# ── the primitive ─────────────────────────────────────────────────────────────


def test_pathlib_really_does_discard_the_root(tmp_path):
    """The behaviour the guard exists for, pinned so nobody 'simplifies' it away."""
    assert Path("/shared") / "/etc/passwd" == Path("/etc/passwd")


@pytest.mark.parametrize(
    "escape",
    [
        "../outside.txt",
        "../../outside.txt",
        "sub/../../outside.txt",
        "/etc/passwd",
        "/tmp/absolute.txt",
    ],
)
def test_resolve_within_refuses_anything_that_leaves_the_root(tmp_path, escape):
    with pytest.raises(UnsafePathError):
        resolve_within(tmp_path, escape)


def test_resolve_within_allows_ordinary_names_and_subdirectories(tmp_path):
    assert resolve_within(tmp_path, "file.csv") == (tmp_path / "file.csv").resolve()
    assert resolve_within(tmp_path, "sub/file.csv") == (tmp_path / "sub" / "file.csv").resolve()
    # A traversal that stays inside is fine — it is the destination that matters.
    assert resolve_within(tmp_path, "sub/../file.csv") == (tmp_path / "file.csv").resolve()


def test_a_symlink_out_of_the_root_is_refused(tmp_path):
    """resolve() follows symlinks, so a link planted inside the root cannot be used to
    reach outside it — the check is on the real destination, not the spelling."""
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted in this environment")
    with pytest.raises(UnsafePathError):
        resolve_within(root, "escape/secret.txt")


def test_assert_within_checks_an_absolute_path_against_its_root(tmp_path):
    inside = tmp_path / "a" / "manifest.json"
    inside.parent.mkdir(parents=True)
    inside.write_text("{}")
    assert assert_within(tmp_path, inside) == inside.resolve()
    with pytest.raises(UnsafePathError):
        assert_within(tmp_path / "a", tmp_path / "b" / "manifest.json")


# ── C1: settlement upload ─────────────────────────────────────────────────────


def test_settlement_upload_refuses_a_file_name_outside_the_shared_folder(monkeypatch, tmp_path):
    """The escape target must genuinely EXIST, or this proves nothing: the unguarded
    code also raised for a path that simply was not there, and the test would pass
    against the bug. A real file outside the root is the only honest probe."""
    shared = tmp_path / "shared"
    shared.mkdir()
    secret = tmp_path / "secret.csv"
    secret.write_text("not-yours")

    monkeypatch.setenv("CBOS_SETL_MODE", "MOCK")
    monkeypatch.setenv("CBOS_SETL_SHARED_FOLDER_PATH", str(shared))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.services.settlement_service import SettlementFileNotFoundError, _locate_file

    # Relative escape, and the absolute form pathlib silently honours.
    for bad in ("../secret.csv", str(secret)):
        with pytest.raises(SettlementFileNotFoundError):
            _locate_file(bad)


def test_settlement_upload_still_finds_an_ordinary_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CBOS_SETL_MODE", "MOCK")
    monkeypatch.setenv("CBOS_SETL_SHARED_FOLDER_PATH", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.services.settlement_service import _locate_file

    (tmp_path / "settlement.csv").write_text("a,b\n")
    assert _locate_file("settlement.csv") == (tmp_path / "settlement.csv").resolve()


# ── H2: manifest intake ───────────────────────────────────────────────────────


def _manifest(files):
    return {
        "manifest_version": 1,
        "batch_id": "MCX-2026-07-20-abcd1234",
        "segment": "MCX",
        "trade_date": "2026-07-20",
        "correlation_id": "c-1",
        "producer": {"name": "test", "version": "1", "action": "all"},
        "created_at": "2026-07-20T00:00:00+05:30",
        "files": files,
        "download_outcome": {"status": "success", "no_data": [], "failed": []},
    }


def test_a_manifest_naming_a_file_outside_its_own_directory_is_rejected(monkeypatch, tmp_path):
    """The files[].name paths are handed to shutil.move, so an unconfined one does not
    just read the wrong file — it relocates it.

    TWO controls stop this, and the order matters for anyone reading the fix. The
    canonical edpb-core schema constrains name to `^[^/\\\\]+$`, so a separator is
    rejected at validation before resolve_within ever runs — this test passes with the
    path guard removed, and that is not a defect in the test but the schema doing its
    job. resolve_within is the second line: it holds if the schema is ever loosened, and
    it catches the escape a bare name cannot express (a symlink inside the directory).

    Worth knowing that the first line only became real when the repo-root edpb_core
    shadow was deleted (finding 3.16) — the stub it shadowed validated nothing but the
    presence of 8 top-level keys, and would have let this through."""
    monkeypatch.setenv("FILE_ROOT_PATH", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.services.manifest_service import ManifestError, load_manifest

    stage = tmp_path / "20-07-2026" / "MCX"
    stage.mkdir(parents=True)
    # A real file outside the stage dir, so the unguarded code would happily resolve and
    # return it rather than failing on absence.
    outside = tmp_path / "elsewhere.csv"
    outside.write_text("a,b")

    manifest_path = stage / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            _manifest(
                [
                    {
                        "name": "../../elsewhere.csv",
                        "sha256": "0" * 64,
                        "size_bytes": 4,
                        "exchange": "MCX",
                    }
                ]
            )
        )
    )

    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


def test_a_manifest_outside_the_intake_root_is_rejected(monkeypatch, tmp_path):
    """Every files[].name is resolved relative to the manifest's PARENT, so an
    out-of-tree manifest moves the whole batch with it."""
    monkeypatch.setenv("FILE_ROOT_PATH", str(tmp_path / "intake"))
    (tmp_path / "intake").mkdir()
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.services.manifest_service import ManifestError, load_manifest

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    manifest_path = elsewhere / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest([])))

    with pytest.raises(ManifestError):
        load_manifest(manifest_path)
