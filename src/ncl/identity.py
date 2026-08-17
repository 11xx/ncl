"""Authenticated principal and calendar-home discovery."""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from . import exits, profiles
from .session import Session, SessionError

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
_STATUS = re.compile(r"\b([1-5][0-9][0-9])\b")


class IdentityError(RuntimeError):
    """The authenticated principal response was not usable."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Identity:
    principal_url: str
    account_name: str
    display_name: str
    calendar_home: str

    def as_dict(self) -> dict[str, str]:
        return {
            "principal_url": self.principal_url,
            "account_name": self.account_name,
            "display_name": self.display_name,
            "calendar_home": self.calendar_home,
        }


def _status_code(value: str | None) -> int | None:
    if value is None:
        return None
    match = _STATUS.search(value)
    return int(match.group(1)) if match else None


def _element_name(element: ET.Element) -> tuple[str, str]:
    if element.tag.startswith("{"):
        namespace, _, local = element.tag[1:].partition("}")
        return namespace, local
    return "", element.tag


def _parse_multistatus(
    body: bytes, needed: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], ET.Element]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise IdentityError("the DAV response was not valid XML") from exc
    if _element_name(root) != (DAV, "multistatus"):
        raise IdentityError("the DAV response was not a Multi-Status response")

    wanted = set(needed)
    found: dict[tuple[str, str], ET.Element] = {}
    for response in root:
        if _element_name(response) != (DAV, "response"):
            continue
        for propstat_item in response:
            if _element_name(propstat_item) != (DAV, "propstat"):
                continue
            status_element = next(
                (item for item in propstat_item if _element_name(item) == (DAV, "status")),
                None,
            )
            code = _status_code(status_element.text if status_element is not None else None)
            prop = next(
                (item for item in propstat_item if _element_name(item) == (DAV, "prop")),
                None,
            )
            if prop is None:
                continue
            for property_element in prop:
                name = _element_name(property_element)
                if name not in wanted:
                    continue
                if code is None or not 200 <= code < 300:
                    raise IdentityError(
                        f"the DAV property {name[1]} was not returned successfully"
                    )
                found[name] = property_element

    missing = wanted - found.keys()
    if missing:
        name = sorted(missing)[0][1]
        raise IdentityError(f"the DAV response omitted required property {name}")
    return found


def _href(element: ET.Element, *, profile: Any) -> str:
    value = next((child.text for child in element if _element_name(child) == (DAV, "href")), None)
    value = value.strip() if value else value
    if not value:
        raise IdentityError("the DAV response contained an empty href")
    try:
        from .session import absolute_url

        return absolute_url(profile, value)
    except SessionError as exc:
        raise IdentityError(exc.message, exc.code) from exc


def _principal_account(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    segment = path.rsplit("/", 1)[-1]
    if not segment:
        raise IdentityError("the principal URL did not identify an account")
    return urllib.parse.unquote(segment)


def _propfind(session: Session, url: str, properties: str) -> Any:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        f"<d:prop>{properties}</d:prop>"
        "</d:propfind>"
    )
    return session.request(
        "PROPFIND",
        url,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        data=body,
    )


def discover(profile: Any, *, session: Session | None = None, transport: Any = None) -> Identity:
    """Resolve the authenticated principal without using the login name as a path."""
    session = session or Session(profile, transport=transport)
    response = _propfind(session, "/remote.php/dav/", '<d:current-user-principal/>')
    if response.status != 207:
        raise IdentityError("principal discovery did not return Multi-Status")
    principal = _parse_multistatus(response.body, {(DAV, "current-user-principal")})[
        (DAV, "current-user-principal")
    ]
    principal_url = _href(principal, profile=profile)

    response = _propfind(
        session,
        principal_url,
        '<c:calendar-home-set/><d:displayname/>',
    )
    if response.status != 207:
        raise IdentityError("calendar-home discovery did not return Multi-Status")
    properties = _parse_multistatus(
        response.body,
        {(CALDAV, "calendar-home-set"), (DAV, "displayname")},
    )
    calendar_home = _href(properties[(CALDAV, "calendar-home-set")], profile=profile)
    display_name = properties[(DAV, "displayname")].text or ""
    return Identity(
        principal_url=principal_url,
        account_name=_principal_account(principal_url),
        display_name=display_name,
        calendar_home=calendar_home,
    )


def in_calendar_home(href: str, calendar_home: str) -> bool:
    """Compare canonical path segments for a discovered calendar home."""
    parsed = urllib.parse.urlsplit(calendar_home)
    if parsed.scheme and parsed.netloc and href.startswith("/"):
        href = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, href, "", ""))
    return profiles.in_scope(href, [calendar_home])
