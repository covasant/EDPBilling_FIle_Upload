"""Minimal compatibility layer for the shared edpb-core interfaces used by this repo.

This repository previously expected a sibling checkout of the shared package.
In environments where that checkout is absent, these lightweight shims keep the
application importable and allow the local FastAPI app to boot.
"""

from .batch_api import BatchStatus
from .manifest import ManifestValidationError, validate_manifest

__all__ = ["BatchStatus", "ManifestValidationError", "validate_manifest"]
