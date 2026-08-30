"""Calendar discovery over CalDAV.

A calendar is identified by its collection href, never by its display name.
Display names are chosen by the user, are not unique, and change without the
collection changing — targeting one by name picks the wrong calendar as soon as
two of them agree, which is exactly when the mistake is least visible.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import exits, profiles
from .identity import CALDAV, DAV, _element_name, _status_code
from .session import Session, SessionError, absolute_url

#: Nextcloud's own namespace, which is where the calendar colour lives.
OC = "http://owncloud.org/ns"


class CalendarError(RuntimeError):
    """A calendar collection response was not usable."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Calendar:
    """One calendar collection, addressed by href rather than by name."""

    href: str
    display_name: str
    description: str
    color: str
    components: tuple[str, ...]
    read_only: bool
    in_scope: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "href": self.href,
            "display_name": self.display_name,
            "description": self.description,
            "color": self.color,
            "components": list(self.components),
            "read_only": self.read_only,
            "in_scope": self.in_scope,
        }


_PROPS = (
    "<d:resourcetype/>"
    "<d:displayname/>"
    "<d:current-user-privilege-set/>"
    "<c:supported-calendar-component-set/>"
    "<c:calendar-description/>"
    "<o:calendar-color/>"
)

_PROPFIND = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
    f'xmlns:o="{OC}"><d:prop>{_PROPS}</d:prop></d:propfind>'
)


def _prop_elements(response: ET.Element) -> dict[tuple[str, str], ET.Element]:
    """Return this response's successfully-returned properties.

    A 207 carries one status per propstat, so a property returned inside a 404
    or 403 propstat is absent rather than empty. Reading the element without
    checking its status is how a forbidden property becomes a silent default.
    """
    found: dict[tuple[str, str], ET.Element] = {}
    for propstat in response:
        if _element_name(propstat) != (DAV, "propstat"):
            continue
        status = next(
            (item for item in propstat if _element_name(item) == (DAV, "status")), None
        )
        code = _status_code(status.text if status is not None else None)
        if code is None or not 200 <= code < 300:
            continue
        prop = next((item for item in propstat if _element_name(item) == (DAV, "prop")), None)
        if prop is None:
            continue
        for element in prop:
            found[_element_name(element)] = element
    return found


def _is_calendar(props: dict[tuple[str, str], ET.Element]) -> bool:
    resourcetype = props.get((DAV, "resourcetype"))
    if resourcetype is None:
        return False
    return any(_element_name(child) == (CALDAV, "calendar") for child in resourcetype)


def _components(props: dict[tuple[str, str], ET.Element]) -> tuple[str, ...]:
    element = props.get((CALDAV, "supported-calendar-component-set"))
    if element is None:
        return ()
    names = []
    for child in element:
        if _element_name(child) == (CALDAV, "comp"):
            name = child.get("name")
            if name:
                names.append(name)
    return tuple(names)


def _text(props: dict[tuple[str, str], ET.Element], name: tuple[str, str]) -> str:
    element = props.get(name)
    return (element.text or "").strip() if element is not None else ""


def _read_only(props: dict[tuple[str, str], ET.Element]) -> bool:
    """Whether the account may only read this collection.

    An absent privilege set is reported as read-only. The server declining to
    say what may be written is not permission to write, and a wrong answer in
    that direction is the one that loses data.
    """
    element = props.get((DAV, "current-user-privilege-set"))
    if element is None:
        return True
    for privilege in element:
        if _element_name(privilege) != (DAV, "privilege"):
            continue
        for granted in privilege:
            if _element_name(granted) in {(DAV, "write"), (DAV, "write-content"), (DAV, "all")}:
                return False
    return True


def _calendar(profile: Any, href: str, props: dict[tuple[str, str], ET.Element]) -> Calendar:
    return Calendar(
        href=href,
        display_name=_text(props, (DAV, "displayname")),
        description=_text(props, (CALDAV, "calendar-description")),
        color=_text(props, (OC, "calendar-color")),
        components=_components(props),
        read_only=_read_only(props),
        in_scope=profiles.in_scope(href, list(profile.calendars)),
    )


def list_calendars(profile: Any, *, session: Session, calendar_home: str) -> list[Calendar]:
    """Enumerate the calendars under a discovered calendar home.

    `in_scope` reports the allowlist decision rather than filtering: a caller
    setting up a profile needs to see the calendar it is about to allow, and a
    listing that hides everything unconfigured cannot be used to configure
    anything.
    """
    response = session.request(
        "PROPFIND",
        calendar_home,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=_PROPFIND,
    )
    if response.status != 207:
        raise CalendarError(
            "the calendar home did not answer with a Multi-Status response",
            exits.MALFORMED_RESPONSE,
        )

    try:
        root = ET.fromstring(response.body)
    except ET.ParseError as exc:
        raise CalendarError("the calendar listing was not valid XML") from exc
    if _element_name(root) != (DAV, "multistatus"):
        raise CalendarError("the calendar listing was not a Multi-Status response")

    home = _canonical(profile, calendar_home)
    calendars: list[Calendar] = []
    for entry in root:
        if _element_name(entry) != (DAV, "response"):
            continue
        href_element = next(
            (item for item in entry if _element_name(item) == (DAV, "href")), None
        )
        raw = (href_element.text or "").strip() if href_element is not None else ""
        if not raw:
            continue
        href = _canonical(profile, raw)
        if href == home:
            # The home itself is returned by a Depth 1 listing of its children.
            continue
        props = _prop_elements(entry)
        if not _is_calendar(props):
            continue
        calendars.append(_calendar(profile, href, props))
    return calendars


def fetch(profile: Any, *, session: Session, href: str) -> Calendar:
    """Read one calendar collection's own properties.

    Addressing the collection directly answers what a calendar *is* —
    description, colour, and the component set it will accept — which a
    listing built for addressing does not carry. The component set matters
    most: most servers fix it at creation, so a caller that discovers a
    missing VTODO afterwards has no remedy short of recreating the collection.
    """
    target = _canonical(profile, href)
    if not profiles.in_scope(target, list(profile.calendars)):
        raise CalendarError(
            f"{target} is outside this profile's calendar allowlist", exits.SCOPE_DENIED
        )
    response = session.request(
        "PROPFIND",
        target,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        data=_PROPFIND,
    )
    if response.status == 404:
        raise CalendarError(f"no calendar exists at {target}", exits.TARGET_NOT_FOUND)
    if response.status != 207:
        raise CalendarError(
            "the calendar collection did not answer with a Multi-Status response",
            exits.MALFORMED_RESPONSE,
        )
    try:
        root = ET.fromstring(response.body)
    except ET.ParseError as exc:
        raise CalendarError("the calendar collection response was not valid XML") from exc
    if _element_name(root) != (DAV, "multistatus"):
        raise CalendarError("the calendar collection response was not a Multi-Status response")

    wanted = _segments(target)
    for entry in root:
        if _element_name(entry) != (DAV, "response"):
            continue
        href_element = next((i for i in entry if _element_name(i) == (DAV, "href")), None)
        raw = (href_element.text or "").strip() if href_element is not None else ""
        if not raw or _segments(_canonical(profile, raw)) != wanted:
            continue
        status = next((i for i in entry if _element_name(i) == (DAV, "status")), None)
        code = _status_code(status.text if status is not None else None)
        if code == 404:
            raise CalendarError(f"no calendar exists at {target}", exits.TARGET_NOT_FOUND)
        props = _prop_elements(entry)
        if not _is_calendar(props):
            raise CalendarError(
                f"the resource at {target} is not a calendar collection",
                exits.UNSUPPORTED_COLLECTION,
            )
        return _calendar(profile, target, props)
    raise CalendarError(f"no calendar exists at {target}", exits.TARGET_NOT_FOUND)


def _segments(href: str) -> tuple[str, ...]:
    try:
        _, segments = profiles.canonicalize_href(href)
    except ValueError as exc:
        raise CalendarError("the server returned a malformed calendar href") from exc
    return segments


def _canonical(profile: Any, value: str) -> str:
    try:
        return absolute_url(profile, value)
    except SessionError as exc:
        raise CalendarError(exc.message, exc.code) from exc


def resolve(calendars: list[Calendar], target: str) -> Calendar:
    """Resolve a caller's target to exactly one calendar.

    An href matches exactly. A display name matches only when it is unique:
    two calendars sharing a name is an ambiguity the caller has to break, and
    guessing between them would silently act on the wrong one.
    """
    for calendar in calendars:
        if calendar.href == target:
            return calendar

    if target.startswith("/") or "://" in target:
        try:
            target_authority, target_segments = profiles.canonicalize_href(target)
        except ValueError:
            target_authority, target_segments = None, None
        if target_segments is not None:
            for calendar in calendars:
                try:
                    calendar_authority, calendar_segments = profiles.canonicalize_href(
                        calendar.href
                    )
                except ValueError:
                    continue
                if target_authority is not None and calendar_authority != target_authority:
                    continue
                if calendar_segments == target_segments:
                    return calendar

    short_target = target.rstrip("/")
    if "/" not in short_target and "://" not in short_target:
        by_segment = [
            calendar
            for calendar in calendars
            if urlsplit(calendar.href).path.rstrip("/").rsplit("/", 1)[-1] == short_target
        ]
        if len(by_segment) == 1:
            return by_segment[0]
        if len(by_segment) > 1:
            hrefs = ", ".join(sorted(calendar.href for calendar in by_segment))
            raise CalendarError(
                f"{len(by_segment)} calendars end in {target!r}; name one by href: {hrefs}",
                exits.AMBIGUOUS_TARGET,
            )
    named = [calendar for calendar in calendars if calendar.display_name == target]
    if len(named) == 1:
        return named[0]
    if not named:
        raise CalendarError(f"no calendar matched {target!r}", exits.TARGET_NOT_FOUND)
    hrefs = ", ".join(sorted(calendar.href for calendar in named))
    raise CalendarError(
        f"{len(named)} calendars are named {target!r}; name one by href: {hrefs}",
        exits.AMBIGUOUS_TARGET,
    )


def require_component(calendar: Calendar, component: str) -> Calendar:
    """Require that a resolved collection advertises one component type."""
    if component not in calendar.components:
        raise CalendarError(
            f"calendar {calendar.href} does not advertise {component}",
            exits.UNSUPPORTED_COLLECTION,
        )
    return calendar
