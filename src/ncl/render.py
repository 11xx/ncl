"""Credential redaction for structured values and command text streams."""

from __future__ import annotations

import base64
import errno
import json
import sys
from bisect import bisect_right
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


#: Replacing a secret can complete another one at the seam, because the marker
#: contributes characters of its own: with `ab` and `]xy` both registered,
#: `abxy` becomes `[redacted]xy`, which contains `]xy` in full. Replacement
#: therefore runs to a fixed point rather than once. A marker that already
#: contains a registered value is not safe to emit, so those values are
#: removed while the surrounding text is retained.
_REDACTION_PASSES = 8


_TextChunk = tuple[Any, str]


def _join_chunks(chunks: list[_TextChunk]) -> str:
    return "".join(text for _, text in chunks)


def _append_chunk(chunks: list[_TextChunk], target: Any, text: str) -> None:
    if not text:
        return
    if chunks and chunks[-1][0] is target:
        chunks[-1] = (target, chunks[-1][1] + text)
    else:
        chunks.append((target, text))


def _chunk_starts(chunks: list[_TextChunk]) -> list[int]:
    starts: list[int] = []
    position = 0
    for _, text in chunks:
        starts.append(position)
        position += len(text)
    return starts


def _append_chunk_range(
    result: list[_TextChunk],
    chunks: list[_TextChunk],
    starts: list[int],
    start: int,
    end: int,
) -> None:
    if start >= end:
        return
    index = max(0, min(len(chunks) - 1, bisect_right(starts, start) - 1))
    while index < len(chunks):
        chunk_start = starts[index]
        if chunk_start >= end:
            break
        left = max(start - chunk_start, 0)
        right = min(end - chunk_start, len(chunks[index][1]))
        if left < right:
            _append_chunk(result, chunks[index][0], chunks[index][1][left:right])
        index += 1


def _slice_chunks(chunks: list[_TextChunk], start: int, end: int) -> list[_TextChunk]:
    if start >= end or not chunks:
        return []
    return_result: list[_TextChunk] = []
    _append_chunk_range(return_result, chunks, _chunk_starts(chunks), start, end)
    return return_result


def _replace_secret(
    chunks: list[_TextChunk], secret: str, replacement: str
) -> list[_TextChunk]:
    value = _join_chunks(chunks)
    if not secret or secret not in value:
        return chunks
    starts = _chunk_starts(chunks)
    result: list[_TextChunk] = []
    cursor = 0
    while True:
        match = value.find(secret, cursor)
        if match < 0:
            break
        _append_chunk_range(result, chunks, starts, cursor, match)
        target_index = max(0, min(len(chunks) - 1, bisect_right(starts, match) - 1))
        _append_chunk(result, chunks[target_index][0], replacement)
        cursor = match + len(secret)
    _append_chunk_range(result, chunks, starts, cursor, len(value))
    return result


def _redaction_replacement() -> str:
    if any(secret and secret in _MARKER for secret in _SECRETS):
        return ""
    return _MARKER


def _redact_chunks(chunks: list[_TextChunk], replacement: str | None = None) -> list[_TextChunk]:
    """Redact an ordered sequence while retaining each output target."""
    if replacement is None:
        replacement = _redaction_replacement()
    current = chunks
    for _ in range(_REDACTION_PASSES):
        replaced = current
        for secret in sorted(_SECRETS, key=len, reverse=True):
            replaced = _replace_secret(replaced, secret, replacement)
        current_value = _join_chunks(current)
        replaced_value = _join_chunks(replaced)
        if replaced_value == current_value:
            if any(secret and secret in replaced_value for secret in _SECRETS):
                if replacement:
                    return _redact_chunks(chunks, replacement="")
                return []
            return replaced
        current = replaced
    if replacement:
        return _redact_chunks(chunks, replacement="")
    return []


def _redact_text(value: str) -> str:
    return _join_chunks(_redact_chunks([(None, value)]))


def _held_prefix_length(value: str) -> int:
    """How many trailing characters could still turn into a registered secret.

    A secret split across two writes is invisible to a per-write replacement, so
    the wrapper keeps back the longest suffix that is a proper prefix of some
    registered value and reconsiders it once the next write arrives.
    """
    secrets = tuple(secret for secret in _SECRETS if secret)
    if not secrets:
        return 0
    longest = max(len(secret) for secret in secrets)
    for length in range(min(longest - 1, len(value)), 0, -1):
        suffix = value[len(value) - length:]
        if any(secret.startswith(suffix) and len(secret) > length for secret in secrets):
            return length
    return 0


class _StreamRedactionState:
    """Redaction state shared by stdout and stderr for one invocation."""

    def __init__(self) -> None:
        self._pending: list[_TextChunk] = []
        self._ready: list[_TextChunk] = []

    def _emit_ready(self) -> None:
        while self._ready:
            target, value = self._ready[0]
            while value:
                written = target.write(value)
                if not isinstance(written, int) or isinstance(written, bool):
                    raise TypeError("text stream write() must return an integer")
                if written < 0 or written > len(value):
                    raise ValueError("text stream write() returned an invalid length")
                if written == 0:
                    self._ready[0] = (target, value)
                    raise BlockingIOError(errno.EAGAIN, "text stream write() made no progress")
                value = value[written:]
                self._ready[0] = (target, value)
            self._ready.pop(0)

    def write(self, target: Any, value: str) -> int:
        if not isinstance(value, str):
            raise TypeError(f"write() argument must be str, not {type(value).__name__}")
        self._emit_ready()
        combined = [*self._pending, (target, value)]
        redacted = _redact_chunks(combined)
        text = _join_chunks(redacted)
        held = _held_prefix_length(text)
        keep = len(text) - held
        self._ready.extend(_slice_chunks(redacted, 0, keep))
        self._pending = _slice_chunks(redacted, keep, len(text))
        self._emit_ready()
        return len(value)

    def flush(self, target: Any) -> None:
        self._emit_ready()
        target.flush()

    def drain(self) -> None:
        self._emit_ready()
        self._ready.extend(self._pending)
        self._pending = []
        self._emit_ready()


class _RedactingTextStream:
    """A text stream whose redaction spans all standard-stream boundaries.

    The stdout and stderr wrappers share one state, so a secret split between
    them cannot be reconstructed in a merged destination. Held text is never
    released on flush: by construction it is a proper prefix of a registered
    secret, so draining it early is exactly the leak this exists to close. It is
    at most one character short of the longest secret and reaches its original
    stream when the wrapper exits.
    """

    def __init__(self, stream: Any, state: _StreamRedactionState) -> None:
        self._stream = stream
        self._state = state

    def write(self, value: str) -> int:
        return self._state.write(self._stream, value)

    def writelines(self, values: Any) -> None:
        for value in values:
            self.write(value)

    def flush(self) -> None:
        self._state.flush(self._stream)

    def drain(self) -> None:
        """Release held text, which holds no complete secret, in order."""
        self._state.drain()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


@contextmanager
def redacted_standard_streams():
    """Redact one ordered text stream spanning stdout and stderr."""
    stdout = sys.stdout
    stderr = sys.stderr
    state = _StreamRedactionState()
    wrapped = (_RedactingTextStream(stdout, state), _RedactingTextStream(stderr, state))
    sys.stdout, sys.stderr = wrapped
    try:
        yield
    finally:
        # Restoration is not conditional on draining or flushing: a stream that
        # fails to accept its last write must not also leave a wrapper installed.
        try:
            state.drain()
            flushed: list[Any] = []
            for stream in (stdout, stderr):
                if not any(stream is previous for previous in flushed):
                    stream.flush()
                    flushed.append(stream)
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
