"""Confining a caller-supplied path to the directory it is supposed to be under.

Two places take a name or a relative path from outside and join it onto a root:
settlement uploads (``file_name`` from the request body) and manifest intake
(``manifest_path``, and every ``files[].name`` inside it). Neither checked the result,
and ``pathlib`` makes the failure quiet rather than loud:

    Path("/shared") / "/etc/passwd"        -> PosixPath('/etc/passwd')
    Path("/shared") / "../../etc/passwd"   -> PosixPath('/shared/../../etc/passwd')

The first is the one that surprises people: ``/`` DISCARDS the left operand entirely
when the right is absolute. No error, no warning — the root silently stops applying.

The security framing (an attacker reading arbitrary files) is real but not the only
one, and on a trusted internal network not the most likely: an upstream caller that
sends a full path where a bare name was expected, or a manifest with a wrong ``name``,
hits exactly the same code. The settlement path then uploads whatever it found to the
external DP endpoint; the manifest path ``shutil.move``s real files out of the intake
tree. Both are silent, and the second loses data.

``resolve()`` before comparing is what makes this hold: it collapses ``..`` and follows
symlinks, so neither a traversal sequence nor a symlink planted inside the root can
point the result outside it.
"""

from __future__ import annotations

from pathlib import Path


class UnsafePathError(ValueError):
    """A caller-supplied path escaped, or tried to escape, its declared root."""


def resolve_within(root: Path | str, candidate: Path | str, *, what: str = "path") -> Path:
    """Join ``candidate`` onto ``root`` and return it, or raise :class:`UnsafePathError`.

    ``candidate`` must stay strictly inside ``root`` once resolved. An absolute
    ``candidate`` is refused outright rather than silently replacing ``root`` — a caller
    passing one has misunderstood the contract, and guessing which it meant would be
    worse than telling it.
    """
    base = Path(root).resolve()
    raw = Path(candidate)

    if raw.is_absolute():
        raise UnsafePathError(
            f"{what} {str(candidate)!r} is an absolute path; expected a name or a path "
            f"relative to {base}"
        )

    resolved = (base / raw).resolve()
    if resolved != base and base not in resolved.parents:
        raise UnsafePathError(f"{what} {str(candidate)!r} resolves outside {base}")
    return resolved


def assert_within(root: Path | str, candidate: Path | str, *, what: str = "path") -> Path:
    """Check an ALREADY-ABSOLUTE ``candidate`` lies inside ``root``, and return it.

    For the case where the caller legitimately supplies a full path (the manifest's own
    location) and the question is only whether it is under the permitted root.
    """
    base = Path(root).resolve()
    resolved = Path(candidate).resolve()
    if resolved != base and base not in resolved.parents:
        raise UnsafePathError(f"{what} {str(candidate)!r} resolves outside {base}")
    return resolved
