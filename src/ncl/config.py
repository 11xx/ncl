"""Configuration loading and strict validation for ``ncl`` profiles."""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import exits


class ConfigError(RuntimeError):
    """A configuration problem with the exit code the caller should use."""

    def __init__(self, message: str, code: int = exits.PRECONDITION_FAILED) -> None:
        self.code = code
        self.exit_code = code
        super().__init__(message)


@dataclass(frozen=True)
class Profile:
    """One configured Nextcloud origin and its client-side scope."""

    name: str
    origin: str
    secret_backend: str
    calendars: tuple[str, ...]
    files_roots: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values.pop("name")
        values["calendars"] = list(self.calendars)
        values["files_roots"] = list(self.files_roots)
        return values


@dataclass(frozen=True)
class Config:
    """The validated configuration and the path it came from."""

    path: Path
    default_profile: str
    profiles: dict[str, Profile]


def config_path(path: Path | str | None = None) -> Path:
    """Resolve the runtime configuration path, including the test override."""
    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get("NCL_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "ncl" / "config.toml"


def _error(context: str, message: str) -> ConfigError:
    return ConfigError(f"{context}: {message}")


def _table(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(context, "expected a table")
    return value


def _keys(table: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise _error(context, f"unknown key {unknown[0]!r}")


def _required(table: dict[str, Any], key: str, context: str) -> Any:
    if key not in table:
        raise _error(context, f"missing required key {key!r}")
    return table[key]


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error(context, "expected a non-empty string")
    return value


def _string_list(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _error(context, "expected an array of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise _error(context, f"entry {index} must be a non-empty string")
        result.append(item)
    return tuple(result)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_origin(value: Any, context: str = "origin") -> str:
    """Validate an origin without contacting DNS or a Nextcloud server."""
    origin = _string(value, context)
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise _error(context, f"cannot parse origin: {exc}") from exc

    if parsed.scheme not in {"https", "http"}:
        raise _error(context, "must use https://; only http:// loopback is allowed")
    if not host:
        raise _error(context, "must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise _error(context, "must not include user information")
    if parsed.path or parsed.query or parsed.fragment:
        raise _error(
            context,
            "must contain only scheme, host, and optional port; no path, query, or fragment",
        )
    if port is not None and not 1 <= port <= 65535:
        raise _error(context, "port must be between 1 and 65535")
    if parsed.scheme == "http" and not _is_loopback(host):
        raise _error(context, "http:// is allowed only when the host is loopback; use https://")
    return origin


def _validate_profile(name: str, raw: Any) -> Profile:
    context = f"profiles.{name}"
    table = _table(raw, context)
    allowed = {
        "origin",
        "secret_backend",
        "calendars",
        "files_roots",
    }
    _keys(table, allowed, context)

    origin = validate_origin(_required(table, "origin", context), f"{context}.origin")
    backend = _string(table.get("secret_backend", "pass"), f"{context}.secret_backend")
    if backend not in {"pass", "libsecret"}:
        raise _error(f"{context}.secret_backend", "must be 'pass' or 'libsecret'")

    calendars = _string_list(_required(table, "calendars", context), f"{context}.calendars")
    files_roots = _string_list(
        _required(table, "files_roots", context), f"{context}.files_roots"
    )

    return Profile(
        name=name,
        origin=origin,
        secret_backend=backend,
        calendars=calendars,
        files_roots=files_roots,
    )


def validate(raw: Any, path: Path | str = "<config>") -> Config:
    """Validate a TOML value completely and return typed profile data."""
    path = Path(path)
    table = _table(raw, str(path))
    _keys(table, {"default_profile", "profiles"}, str(path))
    default_profile = _string(
        _required(table, "default_profile", str(path)), f"{path}.default_profile"
    )
    raw_profiles = _table(_required(table, "profiles", str(path)), f"{path}.profiles")
    profiles: dict[str, Profile] = {}
    for name, raw_profile in raw_profiles.items():
        if not isinstance(name, str) or not name:
            raise _error(f"{path}.profiles", "profile names must be non-empty strings")
        profiles[name] = _validate_profile(name, raw_profile)

    # Import lazily so profiles can use ConfigError without creating an import
    # cycle while the configuration module is being initialized.
    from .profiles import canonicalize_href

    for profile in profiles.values():
        for field, entries in (
            ("calendars", profile.calendars),
            ("files_roots", profile.files_roots),
        ):
            for index, entry in enumerate(entries):
                try:
                    canonicalize_href(entry)
                except ValueError as exc:
                    raise _error(
                        f"{path}.profiles.{profile.name}.{field}[{index}]", str(exc)
                    ) from exc

    return Config(path=path, default_profile=default_profile, profiles=profiles)


def load(path: Path | str | None = None) -> Config:
    """Read and validate the configured file.

    A missing file is reported as ``NOT_CONFIGURED`` by the command that needs
    a profile, while callers such as doctor can use ``load_optional`` to report
    the missing path as a check rather than an exception.
    """
    resolved = config_path(path)
    if not resolved.exists():
        raise ConfigError(
            f"{resolved}: configuration file not found", exits.NOT_CONFIGURED
        )
    try:
        with resolved.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{resolved}: cannot read configuration: {exc}") from exc
    return validate(raw, resolved)


def load_optional(path: Path | str | None = None) -> Config | None:
    """Load configuration when present, leaving absence to the caller."""
    resolved = config_path(path)
    if not resolved.exists():
        return None
    return load(resolved)
