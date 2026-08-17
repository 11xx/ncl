"""Local and authenticated precondition checks for a configured profile."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, exits, profiles


def _resource_exists(session: Any, href: str) -> bool:
    response = session.request(
        "PROPFIND",
        href,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        data=(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
        ),
    )
    return response.status in {200, 207}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class Report:
    checks: tuple[Check, ...]

    @property
    def exit_code(self) -> int:
        return (
            exits.OK
            if all(check.status != "fail" for check in self.checks)
            else exits.PRECONDITION_FAILED
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "checks": [check.as_dict() for check in self.checks],
            "response": {str(code): message for code, message in exits.RESPONSE.items()},
        }


def probe_pass() -> tuple[bool, str]:
    """Check that pass can store, retrieve, and clear a value."""
    from . import secrets as secret_store

    return secret_store.probe_backend("pass")


def probe_libsecret() -> tuple[bool, str]:
    """Verify that Secret Service can store, retrieve, and clear a value."""
    from . import secrets as secret_store

    return secret_store.probe_backend("libsecret")


def _check(name: str, status: str, detail: str) -> Check:
    return Check(name=name, status=status, detail=detail)


def _run_probe(name: str, probe: Callable[[], tuple[bool, str]]) -> Check:
    passed, detail = probe()
    return _check(name, "pass" if passed else "fail", detail)


def _skip(name: str, detail: str) -> Check:
    return _check(name, "skip", detail)


def _run_authenticated(profile: Any, checks: list[Check], *, transport: Any = None) -> None:
    from . import identity
    from . import secrets as secret_store
    from .session import Session, SessionError

    try:
        login_name = secret_store.get(profile, "login_name")
        app_password = secret_store.get(profile, "app_password")
    except secret_store.SecretError:
        login_name = app_password = None
    if not login_name or not app_password:
        detail = "no credential is stored; run `ncl login`"
        checks.extend(
            (
                _skip(f"credential:{profile.name}", detail),
                _skip(f"principal:{profile.name}", detail),
                _skip(f"calendar-home:{profile.name}", detail),
            )
        )
        for field, entries in (
            ("calendars", profile.calendars),
            ("files_roots", profile.files_roots),
        ):
            for index, _entry in enumerate(entries):
                checks.append(_skip(f"remote:{profile.name}:{field}:{index}", detail))
        return

    session = Session(profile, transport=transport)
    try:
        result = identity.discover(profile, session=session)
    except (SessionError, identity.IdentityError) as exc:
        checks.append(_check(f"credential:{profile.name}", "fail", str(exc)))
        checks.append(
            _check(
                f"principal:{profile.name}",
                "fail",
                str(exc),
            )
        )
        checks.append(_skip(f"calendar-home:{profile.name}", "principal discovery failed"))
        for field, entries in (
            ("calendars", profile.calendars),
            ("files_roots", profile.files_roots),
        ):
            for index, _entry in enumerate(entries):
                checks.append(
                    _skip(
                        f"remote:{profile.name}:{field}:{index}",
                        "principal discovery failed",
                    )
        )
        return

    checks.append(_check(f"credential:{profile.name}", "pass", "stored credential was accepted"))
    checks.append(_check(f"principal:{profile.name}", "pass", f"resolved {result.account_name!r}"))
    checks.append(_check(f"calendar-home:{profile.name}", "pass", result.calendar_home))
    for field, entries in (("calendars", profile.calendars), ("files_roots", profile.files_roots)):
        for index, entry in enumerate(entries):
            name = f"remote:{profile.name}:{field}:{index}"
            if field == "calendars" and not identity.in_calendar_home(entry, result.calendar_home):
                checks.append(_check(name, "fail", "allowlist entry is outside the calendar home"))
                continue
            try:
                exists = _resource_exists(session, entry)
            except (SessionError, identity.IdentityError) as exc:
                checks.append(_check(name, "fail", str(exc)))
            else:
                checks.append(
                    _check(name, "pass", "resource exists")
                    if exists
                    else _check(name, "fail", "resource was not found")
                )


def run(
    path: str | Path | None = None,
    profile_name: str | None = None,
    *,
    transport: Any = None,
) -> Report:
    """Run local checks and authenticated checks when a credential is present."""
    resolved = config.config_path(path)
    checks: list[Check] = []
    try:
        loaded = config.load(resolved)
    except config.ConfigError as exc:
        checks.append(_check("config", "fail", str(exc)))
        checks.extend(
            (
                _skip("default-profile", "configuration did not load"),
                _skip("origins", "configuration did not load"),
                _skip("secret-backends", "configuration did not load"),
                _skip("allowlists", "configuration did not load"),
            )
        )
        return Report(tuple(checks))

    checks.append(_check("config", "pass", f"{loaded.path}: parsed and all profiles validate"))

    if loaded.default_profile in loaded.profiles:
        checks.append(
            _check("default-profile", "pass", f"default profile is {loaded.default_profile!r}")
        )
    else:
        checks.append(
            _check(
                "default-profile",
                "fail",
                f"default profile {loaded.default_profile!r} is not configured",
            )
        )

    selected_profiles = list(loaded.profiles.values())
    if profile_name is not None:
        selected = loaded.profiles.get(profile_name)
        if selected is None:
            checks.append(
                _check("selected-profile", "fail", f"profile {profile_name!r} is not configured")
            )
            selected_profiles = []
        else:
            checks.append(_check("selected-profile", "pass", f"profile is {profile_name!r}"))
            selected_profiles = [selected]

    for profile in selected_profiles:
        checks.append(
            _check(f"origin:{profile.name}", "pass", f"{profile.origin}: well-formed origin")
        )

        if profile.secret_backend == "pass":
            checks.append(_run_probe(f"secret-backend:{profile.name}:pass", probe_pass))
            checks.append(
                _skip(
                    f"secret-backend:{profile.name}:libsecret",
                    "not selected by this profile",
                )
            )
        else:
            checks.append(
                _skip(
                    f"secret-backend:{profile.name}:pass",
                    "not selected by this profile",
                )
            )
            checks.append(_run_probe(f"secret-backend:{profile.name}:libsecret", probe_libsecret))

        for field, entries in (
            ("calendars", profile.calendars),
            ("files_roots", profile.files_roots),
        ):
            name = f"allowlist:{profile.name}:{field}"
            if not entries:
                checks.append(_check(name, "fail", "allowlist is empty"))
                continue
            try:
                for entry in entries:
                    profiles.canonicalize_href(entry)
            except ValueError as exc:
                checks.append(_check(name, "fail", f"entry cannot be canonicalized: {exc}"))
            else:
                noun = "entry" if len(entries) == 1 else "entries"
                checks.append(_check(name, "pass", f"{len(entries)} {noun} canonicalize"))

        _run_authenticated(profile, checks, transport=transport)

    return Report(tuple(checks))
