"""Credential redaction for structured values and command text streams."""

from __future__ import annotations

import base64
import json
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

_MARKER = "[redacted]"
_SECRETS: set[str] = set()


def register_secret(value: str) -> None:
    """Register a non-empty value that must not appear in command output."""
    if isinstance(value, str) and value:
        _SECRETS.add(value)


def credential_representations(login_name: str, app_password: str) -> dict[str, str]:
    """Derive values that grant or directly disclose the stored credential."""
    user_password = f"{login_name}:{app_password}"
    encoded = base64.b64encode(user_password.encode()).decode("ascii")
    return {
        "password": app_password,
        "user_password": user_password,
        "base64": encoded,
        "basic": f"Basic {encoded}",
    }


def _redact_text(value: str) -> str:
    for secret in sorted(_SECRETS, key=len, reverse=True):
        value = value.replace(secret, _MARKER)
    return value


class _RedactingTextStream:
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, value: str) -> int:
        return self._stream.write(_redact_text(value))

    def writelines(self, values: Any) -> None:
        self._stream.writelines(_redact_text(value) for value in values)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


@contextmanager
def redacted_standard_streams():
    """Redact text written through stdout and stderr for one CLI invocation."""
    stdout = sys.stdout
    stderr = sys.stderr
    sys.stdout = _RedactingTextStream(stdout)
    sys.stderr = _RedactingTextStream(stderr)
    try:
        yield
    finally:
        sys.stdout = stdout
        sys.stderr = stderr


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
