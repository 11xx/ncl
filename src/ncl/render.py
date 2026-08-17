"""The single output boundary for redacted command results and messages."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any

_MARKER = "[redacted]"
_SECRETS: set[str] = set()


def register_secret(value: str) -> None:
    """Register a non-empty value that must not appear in command output."""
    if isinstance(value, str) and value:
        _SECRETS.add(value)


def _redact_text(value: str) -> str:
    for secret in sorted(_SECRETS, key=len, reverse=True):
        value = value.replace(secret, _MARKER)
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        return {_redact(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def emit(value: Any, *, end: str = "\n", error: bool = False) -> None:
    """Render one value after recursively redacting registered secrets."""
    redacted = _redact(value)
    text = redacted if isinstance(redacted, str) else json.dumps(
        redacted, ensure_ascii=False, sort_keys=True
    )
    stream = sys.stderr if error else sys.stdout
    stream.write(_redact_text(text) + end)
    stream.flush()


def emit_error(value: Any, *, end: str = "\n") -> None:
    """Render one value to stderr after recursively redacting secrets."""
    emit(value, end=end, error=True)
