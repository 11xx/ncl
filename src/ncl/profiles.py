"""Profile selection and canonical allowlist scope checks."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote_to_bytes, urlsplit

from . import config, exits

_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


@dataclass(frozen=True)
class _CanonicalHref:
    authority: tuple[str, str, int | None] | None
    segments: tuple[str, ...]


def _decode_segment(segment: str) -> str:
    index = 0
    while index < len(segment):
        if segment[index] == "%":
            if not _PERCENT_ESCAPE.fullmatch(segment[index : index + 3]):
                raise ValueError("href contains an invalid percent escape")
            index += 3
        else:
            index += 1
    try:
        decoded = unquote_to_bytes(segment).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("href contains a non-UTF-8 percent escape") from exc
    return unicodedata.normalize("NFC", decoded)


def _authority(parsed) -> tuple[str, str, int | None] | None:
    if not parsed.scheme and not parsed.netloc:
        return None
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("href must use either a path or a complete URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("href must not contain user information")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("href has an invalid port") from exc
    if not host:
        raise ValueError("href URL must contain a host")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("href port must be between 1 and 65535")
    return (parsed.scheme.lower(), host.lower(), port)


def _canonical(value: str) -> _CanonicalHref:
    if not isinstance(value, str) or not value:
        raise ValueError("href must be a non-empty string")
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment:
        raise ValueError("href must not contain a query or fragment")
    authority = _authority(parsed)
    path = parsed.path
    if not path:
        raise ValueError("href must contain a path")
    if authority is None and not path.startswith("/"):
        raise ValueError("path hrefs must be absolute")

    raw_segments = path.split("/")
    if path.startswith("/"):
        raw_segments = raw_segments[1:]
    while raw_segments and raw_segments[-1] == "":
        raw_segments.pop()

    segments: list[str] = []
    for raw_segment in raw_segments:
        segment = _decode_segment(raw_segment)
        if segment == "..":
            raise ValueError("href contains a '..' path segment")
        if segment == ".":
            continue
        segments.append(segment)
    return _CanonicalHref(authority=authority, segments=tuple(segments))


def canonicalize_href(value: str) -> tuple[tuple[str, str, int | None] | None, tuple[str, ...]]:
    """Return the canonical authority and decoded path segments for an href."""
    href = _canonical(value)
    return href.authority, href.segments


def in_scope(candidate_href: str, allowlist: list[str] | tuple[str, ...]) -> bool:
    """Return whether a candidate is the named collection or one of its children."""
    if not allowlist:
        return False
    try:
        candidate = _canonical(candidate_href)
    except ValueError:
        return False
    for allowed_href in allowlist:
        try:
            allowed = _canonical(allowed_href)
        except ValueError:
            continue
        if candidate.authority != allowed.authority:
            continue
        if candidate.segments[: len(allowed.segments)] == allowed.segments:
            return True
    return False


def resolve(
    name: str | None = None,
    *,
    loaded: config.Config | None = None,
    path: str | None = None,
) -> config.Profile:
    """Resolve an explicit profile, or the configured default profile."""
    loaded = loaded or config.load(path)
    selected = name or loaded.default_profile
    try:
        return loaded.profiles[selected]
    except KeyError as exc:
        raise config.ConfigError(
            f"{loaded.path}: profile {selected!r} is not configured", exits.NOT_CONFIGURED
        ) from exc
