"""Reading calendar events over CalDAV.

An event is addressed by its resource href plus its ETag, never by its summary
and never by UID alone. A UID is unique within a calendar, not across them, and
one resource can hold a recurring master plus overrides that all share it.
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import icalendar

from . import exits, profiles
from .caldav import CalendarError, _canonical
from .identity import CALDAV, DAV, _element_name, _status_code
from .session import Session

#: Components this release will modify. Anything else is read but refused for
#: writing, because rewriting a structure the tool does not model destroys the
#: parts it has no opinion about.
SUPPORTED = {"VEVENT"}

#: Properties whose presence means an event is beyond what writes may touch.
#: Recurrence needs RECURRENCE-ID semantics to identify the right component,
#: and scheduling properties make an edit send invitations or cancellations —
#: an external side effect rather than a local change.
UNSUPPORTED_PROPERTIES = ("RRULE", "RDATE", "RECURRENCE-ID", "ORGANIZER", "ATTENDEE")


class EventError(RuntimeError):
    """An event resource was not usable."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class EventRef:
    """A stable handle for one event resource."""

    calendar_href: str
    href: str
    uid: str
    etag: str
    summary: str
    url: str
    status: str
    start: str
    end: str
    all_day: bool
    recurring: bool
    writable: bool
    unsupported: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "calendar_href": self.calendar_href,
            "href": self.href,
            "uid": self.uid,
            "etag": self.etag,
            "summary": self.summary,
            "url": self.url,
            "status": self.status,
            "start": self.start,
            "end": self.end,
            "all_day": self.all_day,
            "recurring": self.recurring,
            "writable": self.writable,
            "unsupported": list(self.unsupported),
        }


def _utc(value: dt.datetime | dt.date) -> str:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.strftime("%Y%m%dT%H%M%S")
        return value.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return value.strftime("%Y%m%d")


def _range_filter(start: dt.datetime, end: dt.datetime) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
        "<c:filter><c:comp-filter name=\"VCALENDAR\">"
        "<c:comp-filter name=\"VEVENT\">"
        f'<c:time-range start="{_utc(start)}" end="{_utc(end)}"/>'
        "</c:comp-filter></c:comp-filter></c:filter>"
        "</c:calendar-query>"
    )


def _first_vevent(calendar: icalendar.Calendar) -> Any:
    for component in calendar.walk():
        if component.name == "VEVENT":
            return component
    return None


def _component_text(component: Any, name: str) -> str:
    value = component.get(name)
    return str(value) if value is not None else ""


def _describe(raw: bytes, *, calendar_href: str, href: str, etag: str) -> EventRef:
    try:
        parsed = icalendar.Calendar.from_ical(raw)
    except (ValueError, IndexError) as exc:
        raise EventError(f"the event at {href} was not valid iCalendar") from exc

    vevents = [item for item in parsed.walk() if item.name == "VEVENT"]
    component = vevents[0] if vevents else None
    if component is None:
        raise EventError(f"the resource at {href} holds no VEVENT")

    unsupported = [name for name in UNSUPPORTED_PROPERTIES if name in component]
    names = {item.name for item in parsed.walk() if item.name.startswith("V")}
    # VCALENDAR and VTIMEZONE are structural; anything else is a component this
    # release does not model well enough to rewrite safely.
    # VALARM rides along with its event: a patch reparses the whole resource
    # and re-serializes the VEVENT with its subcomponents, so an alarm survives
    # an unrelated edit untouched. Marking it unsupported would refuse to edit
    # the majority of events any calendar client creates, for a risk that was
    # measured and is not there.
    extra = names - {"VCALENDAR", "VTIMEZONE", "VALARM"} - SUPPORTED
    unsupported.extend(sorted(extra))
    if len(vevents) > 1 and "RECURRENCE-ID" not in unsupported:
        unsupported.append("RECURRENCE-ID")

    start_prop = component.get("DTSTART")
    end_prop = component.get("DTEND") or component.get("DUE")
    start_value = getattr(start_prop, "dt", None)
    end_value = getattr(end_prop, "dt", None)
    if end_value is None and start_value is not None:
        # RFC 5545 lets an event state DURATION instead of DTEND, and clients
        # commonly do. Reading only DTEND reported those events as having no
        # end at all, which reads as missing data rather than as another
        # spelling of the same fact.
        duration = getattr(component.get("DURATION"), "dt", None)
        if duration is not None:
            end_value = start_value + duration

    return EventRef(
        calendar_href=calendar_href,
        href=href,
        uid=_component_text(component, "UID"),
        etag=etag,
        summary=_component_text(component, "SUMMARY"),
        url=_component_text(component, "URL"),
        status=_component_text(component, "STATUS"),
        start=_utc(start_value) if start_value is not None else "",
        end=_utc(end_value) if end_value is not None else "",
        all_day=isinstance(start_value, dt.date) and not isinstance(start_value, dt.datetime),
        recurring="RRULE" in component or "RDATE" in component,
        writable=not unsupported,
        unsupported=tuple(dict.fromkeys(unsupported)),
    )


def query(
    profile: Any,
    *,
    session: Session,
    calendar_href: str,
    start: dt.datetime,
    end: dt.datetime,
) -> list[EventRef]:
    """List events overlapping an explicit window.

    The window is required rather than defaulted. An unbounded query against a
    calendar of any age returns everything, which is slow, large, and almost
    never what the caller meant.
    """
    if end <= start:
        raise EventError("the query window ends before it starts", exits.USAGE)
    if not profiles.in_scope(calendar_href, list(profile.calendars)):
        raise CalendarError(
            f"{calendar_href} is outside this profile's calendar allowlist",
            exits.SCOPE_DENIED,
        )

    response = session.request(
        "REPORT",
        calendar_href,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=_range_filter(start, end),
    )
    if response.status != 207:
        raise EventError(
            "the calendar query did not answer with a Multi-Status response",
            exits.MALFORMED_RESPONSE,
        )
    try:
        root = ET.fromstring(response.body)
    except ET.ParseError as exc:
        raise EventError("the calendar query response was not valid XML") from exc

    events: list[EventRef] = []
    for entry in root:
        if _element_name(entry) != (DAV, "response"):
            continue
        href_element = next((i for i in entry if _element_name(i) == (DAV, "href")), None)
        raw_href = (href_element.text or "").strip() if href_element is not None else ""
        if not raw_href:
            continue
        etag = ""
        data = b""
        for propstat in entry:
            if _element_name(propstat) != (DAV, "propstat"):
                continue
            status = next(
                (i for i in propstat if _element_name(i) == (DAV, "status")), None
            )
            code = _status_code(status.text if status is not None else None)
            if code is None or not 200 <= code < 300:
                continue
            prop = next((i for i in propstat if _element_name(i) == (DAV, "prop")), None)
            if prop is None:
                continue
            for element in prop:
                name = _element_name(element)
                if name == (DAV, "getetag"):
                    etag = (element.text or "").strip()
                elif name == (CALDAV, "calendar-data"):
                    data = (element.text or "").encode("utf-8")
        if not data:
            continue
        events.append(
            _describe(
                data,
                calendar_href=_canonical(profile, calendar_href),
                href=_canonical(profile, raw_href),
                etag=etag,
            )
        )
    events.sort(key=lambda event: (event.start, event.href))
    return events


def fetch(profile: Any, *, session: Session, href: str) -> tuple[EventRef, bytes]:
    """Read one event resource, returning its handle and its exact bytes.

    The raw bytes matter: an update has to preserve everything this tool does
    not model, and it can only do that from what the server actually stored.
    """
    if not profiles.in_scope(href, list(profile.calendars)):
        raise CalendarError(
            f"{href} is outside this profile's calendar allowlist", exits.SCOPE_DENIED
        )
    response = session.request("GET", href, headers={"Accept": "text/calendar"})
    if response.status == 404:
        raise EventError(f"no event exists at {href}", exits.TARGET_NOT_FOUND)
    if response.status != 200:
        raise EventError("the event could not be read", exits.SERVER_ERROR)
    etag = response.header("ETag") or ""
    calendar_href = href.rsplit("/", 1)[0] + "/"
    return (
        _describe(
            response.body,
            calendar_href=_canonical(profile, calendar_href),
            href=_canonical(profile, href),
            etag=etag.strip(),
        ),
        response.body,
    )
