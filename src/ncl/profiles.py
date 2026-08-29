"""Profile selection and canonical allowlist scope checks."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

from . import config, exits

_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


@dataclass(frozen=True)
class _CanonicalHref:
    authority: tuple[str, str, int | None] | None
    segments: tuple[str, ...]


def origin_parts(value: str) -> tuple[str, str, int]:
    """Return an origin's scheme, host, and effective port."""
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("href URL must contain an HTTP(S) origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("href must not contain user information")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("href has an invalid port") from exc
    if not host:
        raise ValueError("href URL must contain a host")
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    if not 1 <= port <= 65535:
        raise ValueError("href port must be between 1 and 65535")
    return parsed.scheme.lower(), host.lower(), port


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
    decoded = unicodedata.normalize("NFC", decoded)
    if "/" in decoded or "\\" in decoded:
        raise ValueError("href contains a path separator inside a path segment")
    return decoded


def _authority(parsed) -> tuple[str, str, int | None] | None:
    if not parsed.scheme and not parsed.netloc:
        return None
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("href must use either a path or a complete URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("href must not contain user information")
    return origin_parts(parsed.geturl())


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
    if not segments:
        raise ValueError(
            "href must name at least one path segment; a root entry would allow the entire account"
        )
    return _CanonicalHref(authority=authority, segments=tuple(segments))


def canonicalize_href(value: str) -> tuple[tuple[str, str, int | None] | None, tuple[str, ...]]:
    """Return the canonical authority and decoded path segments for an href."""
    href = _canonical(value)
    return href.authority, href.segments


def fingerprint(profile: Any) -> str:
    """Return a stable identity for the profile state a plan relies on."""
    name = profile if isinstance(profile, str) else getattr(profile, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError("profile must have a non-empty name")
    if all(
        hasattr(profile, field)
        for field in ("origin", "secret_backend", "calendars", "files_roots")
    ):
        state: dict[str, Any] = {
            "name": name,
            "origin": profile.origin,
            "secret_backend": profile.secret_backend,
            "calendars": list(profile.calendars),
            "files_roots": list(profile.files_roots),
        }
    else:
        # Small protocol fakes can identify a profile by name only. Real
        # configured profiles always take the complete branch above.
        state = {"name": name}
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
        # An allowlist entry written as a path carries no authority, and is
        # relative to the profile's own origin. Discovery returns absolute
        # URLs, so requiring both to name an authority would deny every
        # configured collection — silently, since a denial looks the same as a
        # collection that was never allowed. An entry that *does* name an
        # authority still has to match one, so this widens nothing: the
        # candidate reached here through a same-origin check already.
        if allowed.authority is not None and candidate.authority != allowed.authority:
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
