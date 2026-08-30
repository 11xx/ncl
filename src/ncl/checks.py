"""Local and authenticated precondition checks for a configured profile."""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
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
    if response.status != 207:
        return False
    try:
        root = ET.fromstring(response.body)
    except (ET.ParseError, TypeError) as exc:
        from .identity import IdentityError

        raise IdentityError("the DAV resource response was not valid XML") from exc
    from .identity import DAV, IdentityError, _element_name, _status_code
    from .session import SessionError, absolute_url

    if _element_name(root) != (DAV, "multistatus"):
        raise IdentityError(
            "the DAV resource response was not a Multi-Status response",
            exits.MALFORMED_RESPONSE,
        )

    profile = getattr(session, "profile", None)
    if profile is not None:
        try:
            requested = absolute_url(profile, href)
        except SessionError as exc:
            raise IdentityError(exc.message, exc.code) from exc
    else:
        requested = href

    def response_href(raw: str) -> str:
        if profile is not None:
            try:
                return absolute_url(profile, raw)
            except SessionError as exc:
                raise IdentityError(exc.message, exc.code) from exc
        if raw == href:
            return href
        if raw.startswith("/") and "://" in href:
            parsed = urllib.parse.urlsplit(href)
            return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, raw, "", ""))
        base = href if href.endswith("/") else href.rsplit("/", 1)[0] + "/"
        return urllib.parse.urljoin(base, raw)

    for entry in root:
        if _element_name(entry) != (DAV, "response"):
            continue
        href_element = next(
            (item for item in entry if _element_name(item) == (DAV, "href")), None
        )
        raw = (href_element.text or "").strip() if href_element is not None else ""
        if not raw:
            raise IdentityError(
                "the DAV resource response omitted a resource href",
                exits.MALFORMED_RESPONSE,
            )
        if response_href(raw) != requested:
            continue

        direct_status = next(
            (item for item in entry if _element_name(item) == (DAV, "status")), None
        )
        direct_code = _status_code(direct_status.text if direct_status is not None else None)
        if direct_code is not None:
            return 200 <= direct_code < 300

        successful = False
        failed = False
        for propstat in entry:
            if _element_name(propstat) != (DAV, "propstat"):
                continue
            status = next(
                (item for item in propstat if _element_name(item) == (DAV, "status")), None
            )
            code = _status_code(status.text if status is not None else None)
            prop = next((item for item in propstat if _element_name(item) == (DAV, "prop")), None)
            if code is None or prop is None:
                continue
            if 200 <= code < 300:
                successful = True
            else:
                failed = True
        if successful:
            return True
        if failed:
            return False
        raise IdentityError(
            "the DAV resource response contained no usable status",
            exits.MALFORMED_RESPONSE,
        )

    raise IdentityError(
        "the DAV resource response omitted the requested resource",
        exits.MALFORMED_RESPONSE,
    )


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    code: int = exits.OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "code": self.code,
        }


@dataclass(frozen=True)
class Report:
    checks: tuple[Check, ...]

    @property
    def exit_code(self) -> int:
        for check in self.checks:
            if check.status == "fail":
                return check.code or exits.PRECONDITION_FAILED
        return exits.OK

    def as_dict(self) -> dict[str, Any]:
        code = self.exit_code
        return {
            "checks": [check.as_dict() for check in self.checks],
            "code": code,
            "exit_code": code,
            "remediation": exits.RESPONSE.get(code),
            "response": {str(code): message for code, message in exits.RESPONSE.items()},
        }


def inspect_backend(name: str):
    """Report a backend's state without storing or removing anything."""
    from . import secrets as secret_store

    return secret_store.inspect_backend(name)


def round_trip_backend(name: str):
    """Prove a backend can keep a value, by storing and removing one."""
    from . import secrets as secret_store

    return secret_store.round_trip_backend(name)


def _check(name: str, status: str, detail: str, code: int = exits.OK) -> Check:
    if status == "fail" and code == exits.OK:
        code = exits.PRECONDITION_FAILED
    return Check(name=name, status=status, detail=detail, code=code)


def _run_probe(name: str, backend: str, *, round_trip: bool) -> Check:
    """Report a backend check, saying which question was actually answered."""
    state = round_trip_backend(backend) if round_trip else inspect_backend(backend)
    proof = "round trip" if state.round_tripped else "inspection only"
    detail = f"{state.detail} ({proof})"
    if state.side_effects:
        detail = f"{detail}; {'; '.join(state.side_effects)}"
    return _check(
        name,
        "pass" if state.usable else "fail",
        detail,
        exits.OK if state.usable else exits.CREDENTIAL_STORE_FAILED,
    )


def _skip(name: str, detail: str) -> Check:
    return _check(name, "skip", detail)


def _safe_error_detail(exc: Exception) -> str:
    message = getattr(exc, "message", None)
    return message if isinstance(message, str) and message else "the check failed"


def _run_authenticated(profile: Any, checks: list[Check], *, transport: Any = None) -> None:
    from . import identity
    from . import secrets as secret_store
    from .session import Session, SessionError

    try:
        credential = secret_store.load_credential(profile)
    except secret_store.SecretError as exc:
        detail = "the secret backend could not read the credential; fix the backend"
        code = getattr(exc, "code", exits.PRECONDITION_FAILED)
        checks.extend(
            (
                _check(f"credential:{profile.name}", "fail", detail, code),
                _skip(f"principal:{profile.name}", "credential lookup failed"),
                _skip(f"calendar-home:{profile.name}", "credential lookup failed"),
            )
        )
        for field, entries in config.allowlists(profile):
            for index, _entry in enumerate(entries):
                checks.append(
                    _skip(
                        f"remote:{profile.name}:{field}:{index}",
                        "credential lookup failed",
                    )
                )
        return
    if credential is None:
        detail = "no credential is stored; run `ncl login`"
        checks.extend(
            (
                _skip(f"credential:{profile.name}", detail),
                _skip(f"principal:{profile.name}", detail),
                _skip(f"calendar-home:{profile.name}", detail),
            )
        )
        for field, entries in config.allowlists(profile):
            for index, _entry in enumerate(entries):
                checks.append(_skip(f"remote:{profile.name}:{field}:{index}", detail))
        return

    session = Session(profile, transport=transport)
    try:
        result = identity.discover(profile, session=session)
    except (SessionError, identity.IdentityError) as exc:
        detail = _safe_error_detail(exc)
        code = getattr(exc, "code", exits.PRECONDITION_FAILED)
        checks.append(_check(f"credential:{profile.name}", "fail", detail, code))
        checks.append(
            _check(
                f"principal:{profile.name}",
                "fail",
                detail,
                code,
            )
        )
        checks.append(_skip(f"calendar-home:{profile.name}", "principal discovery failed"))
        for field, entries in config.allowlists(profile):
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
    for field, entries in config.allowlists(profile):
        for index, entry in enumerate(entries):
            name = f"remote:{profile.name}:{field}:{index}"
            if field == "calendars" and not identity.in_calendar_home(entry, result.calendar_home):
                checks.append(
                    _check(
                        name,
                        "fail",
                        "allowlist entry is outside the calendar home",
                        exits.PRECONDITION_FAILED,
                    )
                )
                continue
            try:
                exists = _resource_exists(session, entry)
            except (SessionError, identity.IdentityError) as exc:
                checks.append(
                    _check(
                        name,
                        "fail",
                        _safe_error_detail(exc),
                        getattr(exc, "code", exits.PRECONDITION_FAILED),
                    )
                )
            else:
                checks.append(
                    _check(name, "pass", "resource exists")
                    if exists
                    else _check(
                        name,
                        "fail",
                        "resource was not found",
                        exits.TARGET_NOT_FOUND,
                    )
                )


def run(
    path: str | Path | None = None,
    profile_name: str | None = None,
    *,
    transport: Any = None,
    round_trip: bool = False,
) -> Report:
    """Run local checks and authenticated checks when a credential is present.

    The backend check inspects by default and never writes. `round_trip` opts
    into storing and removing a value, which is the only way to prove a store
    can keep a credential and the only way this command changes anything.
    """
    resolved = config.config_path(path)
    from . import secrets as secret_store

    checks: list[Check] = []
    try:
        loaded = config.load(resolved)
    except config.ConfigError as exc:
        checks.append(_check("config", "fail", _safe_error_detail(exc)))
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

        selected_backend = profile.secret_backend
        for backend in secret_store.backend_names():
            name = f"secret-backend:{profile.name}:{backend}"
            if backend == selected_backend:
                checks.append(_run_probe(name, backend, round_trip=round_trip))
            else:
                checks.append(_skip(name, "not selected by this profile"))

        for field, entries in config.allowlists(profile):
            name = f"allowlist:{profile.name}:{field}"
            if not entries:
                if field in config.REQUIRED_ALLOWLISTS:
                    checks.append(_check(name, "fail", "allowlist is empty"))
                else:
                    checks.append(_skip(name, "not configured; this profile reaches none"))
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
