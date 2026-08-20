"""Pure validation for entity tags used by conditional requests."""

from __future__ import annotations

import re


_STRONG_QUOTED = re.compile(r'^"[\x21\x23-\x7e\x80-\xff]*"$')


def normalize_strong(value: object) -> str | None:
    """Return a stripped strong quoted ETag, or ``None`` when it is unusable."""
    candidate = value.strip() if isinstance(value, str) else ""
    if not _STRONG_QUOTED.fullmatch(candidate):
        return None
    return candidate
