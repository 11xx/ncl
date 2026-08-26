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


def _held_prefix_length(value: str) -> int:
    """How many trailing characters could still turn into a registered secret.

    A secret split across two writes is invisible to a per-write replacement, so
    the wrapper keeps back the longest suffix that is a proper prefix of some
    registered value and reconsiders it once the next write arrives.
    """
    if not _SECRETS:
        return 0
    longest = max(len(secret) for secret in _SECRETS)
    for length in range(min(longest - 1, len(value)), 0, -1):
        suffix = value[len(value) - length:]
        if any(secret.startswith(suffix) and len(secret) > length for secret in _SECRETS):
            return length
    return 0


class _RedactingTextStream:
    """A text stream whose redaction spans write boundaries.

    Held text is never released on flush: by construction it is a proper prefix
    of a registered secret, so draining it early is exactly the leak this exists
    to close. It is at most one character short of the longest secret, and it
    reaches the wrapped stream when the wrapper exits.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._pending = ""

    def write(self, value: str) -> int:
        redacted = _redact_text(self._pending + value)
        held = _held_prefix_length(redacted)
        keep = len(redacted) - held
        self._pending = redacted[keep:]
        if keep:
            self._stream.write(redacted[:keep])
        # The text-stream contract counts the characters of the argument that
        # were consumed, which redaction changes the length of but not the fate.
        return len(value)

    def writelines(self, values: Any) -> None:
        for value in values:
            self.write(value)

    def flush(self) -> None:
        self._stream.flush()

    def drain(self) -> None:
        """Release held text, which holds no complete secret, and forget it."""
        pending, self._pending = self._pending, ""
        if pending:
            self._stream.write(_redact_text(pending))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


@contextmanager
def redacted_standard_streams():
    """Redact text written through stdout and stderr for one CLI invocation."""
    stdout = sys.stdout
    stderr = sys.stderr
    wrapped = (_RedactingTextStream(stdout), _RedactingTextStream(stderr))
    sys.stdout, sys.stderr = wrapped
    try:
        yield
    finally:
        for stream in wrapped:
            stream.drain()
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
