from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ManifestValidationError(ValueError):
    """Raised when a manifest payload is structurally invalid."""


def validate_manifest(data: dict[str, Any]) -> None:
    """Validate the minimal manifest shape this app expects.

    The real edpb-core package provides the authoritative schema, but this local
    shim is sufficient to keep startup and basic batch intake working in the
    current workspace.
    """
    required_top_level = {"manifest_version", "batch_id", "segment", "trade_date", "files"}
    if not isinstance(data, dict):
        raise ManifestValidationError("manifest must be a JSON object")

    missing = sorted(required_top_level - set(data))
    if missing:
        raise ManifestValidationError(f"missing required fields: {', '.join(missing)}")

    if not isinstance(data["files"], list):
        raise ManifestValidationError("files must be a list")

    for entry in data["files"]:
        if not isinstance(entry, dict):
            raise ManifestValidationError("each file entry must be an object")
        for key in ("name",):
            if key not in entry:
                raise ManifestValidationError(f"file entry missing required field: {key}")
