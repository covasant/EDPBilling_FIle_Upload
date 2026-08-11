"""Masking credentials that an UPSTREAM SERVER echoed back at us.

Outgoing request payloads were already redacted (`cbos_client._redact`). Responses were
not — and a validation error that quotes the offending request back is a completely
ordinary API design, not an exotic one. So a bad login could put a cleartext credential
into an ERROR log line, and then, because the same text becomes the CBOSUploadError
message, into `uploaded_files.cbos_response` via `upload_outcome.failed()`.

That second hop is what makes this worth its own module: a transient log leak ages out
with log retention, but a credential written into a database row stays there until
someone goes looking for it. Neither can be un-written after the fact.

Length capping lives here too, for the same call sites: an upstream returning a megabyte
of HTML should not be able to flood a log file or a DB column.
"""

from __future__ import annotations

import re

# Field names whose VALUES must never be persisted or logged. Mirrors
# cbos_client._SECRET_KEYS, which covers the same names on the request side.
SECRET_KEYS = ("password", "passwd", "pwd", "api_key", "apikey", "token", "secret", "seskey")

# The shapes a server echoes a field back in:
#   "PASSWORD":"s3cret"   PASSWORD=s3cret   <PASSWORD>s3cret</PASSWORD>   pwd: s3cret
# Longest key first, because regex alternation is first-match, not longest-match — with
# "pwd" ahead of "password" the shorter one would win and leave "ord":"s3cret" behind.
_KEYS_ALT = "|".join(sorted(SECRET_KEYS, key=len, reverse=True))
_ECHOED_SECRET_RX = re.compile(
    r"(?i)((?:" + _KEYS_ALT + r")[\"'>\]]?\s*[:=>]?\s*[\"'<]?)([^\"'<>&,;}\s|]+)"
)

DEFAULT_LIMIT = 2000


def redact_response_text(text: str, *, limit: int = DEFAULT_LIMIT) -> str:
    """Mask echoed credentials in ``text`` and cap it at ``limit`` characters.

    Truncation is marked rather than silent, so a body that got cut is never mistaken
    for one that genuinely ended there.
    """
    if not text:
        return text
    masked = _ECHOED_SECRET_RX.sub(r"\1***", text)
    if len(masked) > limit:
        return f"{masked[:limit]}... [truncated {len(masked) - limit} chars]"
    return masked
