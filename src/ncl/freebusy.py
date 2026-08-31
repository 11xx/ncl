"""Free/busy lookups over the scheduling outbox.

Asking when someone is free is the one scheduling question that contacts
nobody. A ``VFREEBUSY`` ``REQUEST`` posted to the authenticated account's
schedule outbox is answered by the server out of calendars this account may
never read directly, and no invitation, notification, or stored object results
from it. So it sits outside the plan/apply boundary with every other read.

What it returns is deliberately narrow. A free/busy response reports intervals
and their transparency, not what occupies them: a busy period carries no
summary, no attendees, and no href, because the server is answering for a
calendar the caller has no right to see. Reporting an interval as free when the
server refused to answer for that recipient would be the one dangerous mistake
here, so a recipient the server did not answer successfully is reported as a
failure rather than as an empty schedule.
"""

from __future__ import annotations

import datetime as dt
import secrets as token_source
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import icalendar

from . import exits
from .identity import CALDAV, DAV, _element_name
from .scheduling import SchedulingError, SchedulingIdentity, canonical_address
from .session import Session

PRODID = "-//ai-agent-nextcloud//ncl//EN"

#: The iTIP request status prefix a server uses for a delivered answer. RFC 5546
#: numbers success ``2.x``; every other class is a refusal for that recipient.
_SUCCESS = "2"

#: Transparency values a ``FREEBUSY`` period may carry. ``BUSY`` is the default
#: when the parameter is absent, which is the reading that never invents free
#: time.
_DEFAULT_FBTYPE = "BUSY"


@dataclass(frozen=True)
class Period:
    """One interval the server reported, in UTC."""

    start: str
    end: str
    kind: str

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "kind": self.kind}


@dataclass(frozen=True)
class Availability:
    """One recipient's answer, or the reason there is not one."""

    recipient: str
    request_status: str
    answered: bool
    periods: tuple[Period, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "recipient": self.recipient,
            "request_status": self.request_status,
            "answered": self.answered,
            "periods": [period.as_dict() for period in self.periods],
        }


def _instant(value: dt.datetime, *, label: str) -> str:
    if value.tzinfo is None:
        raise SchedulingError(f"{label} has no timezone offset", exits.USAGE)
    return value.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def build_request(
    *,
    organizer: str,
    recipients: tuple[str, ...],
    start: dt.datetime,
    end: dt.datetime,
    now: dt.datetime,
    uid: str,
) -> bytes:
    """Compose the ``VFREEBUSY`` ``REQUEST`` body for one outbox POST.

    RFC 6638 derives the originator and the recipients from the body rather
    than from request headers, so the ``ORGANIZER`` and the ``ATTENDEE`` list
    are the addressing, not decoration.
    """
    calendar = icalendar.Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("method", "REQUEST")
    component = icalendar.FreeBusy()
    component.add("uid", uid)
    component["DTSTAMP"] = icalendar.vText(_instant(now, label="the request timestamp"))
    component["DTSTART"] = icalendar.vText(_instant(start, label="--from"))
    component["DTEND"] = icalendar.vText(_instant(end, label="--to"))
    component["ORGANIZER"] = icalendar.vText(organizer)
    for recipient in recipients:
        component.add("attendee", icalendar.vText(recipient), encode=False)
    calendar.add_component(component)
    return calendar.to_ical()


def _period(value: Any) -> tuple[dt.datetime, dt.datetime]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise SchedulingError("a FREEBUSY period was not an interval")
    begin, finish = value
    if not isinstance(begin, dt.datetime):
        raise SchedulingError("a FREEBUSY period does not start at an instant")
    if isinstance(finish, dt.timedelta):
        finish = begin + finish
    if not isinstance(finish, dt.datetime):
        raise SchedulingError("a FREEBUSY period does not end at an instant")
    if begin.tzinfo is None or begin.utcoffset() is None:
        raise SchedulingError("a FREEBUSY period starts without a timezone offset")
    if finish.tzinfo is None or finish.utcoffset() is None:
        raise SchedulingError("a FREEBUSY period ends without a timezone offset")
    # A zero-length period is what RFC 5545 produces from a VEVENT with a
    # DATE-TIME DTSTART and no DTEND, so an ordinary calendar makes them. Only
    # a period that ends before it starts is unreadable, and this module fails
    # toward busy — refusing here discards every recipient's answer, not one
    # period.
    if finish < begin:
        raise SchedulingError("a FREEBUSY period ends before it starts")
    return begin, finish


def _periods(component: Any) -> tuple[Period, ...]:
    found: list[Period] = []
    for name, value in component.property_items():
        if name.upper() != "FREEBUSY":
            continue
        kind = str(value.params.get("FBTYPE", _DEFAULT_FBTYPE)).strip().upper()
        if not kind:
            raise SchedulingError("a FREEBUSY period carries an empty FBTYPE")
        entries = value.dt if isinstance(value.dt, list) else [value.dt]
        for entry in entries:
            begin, finish = _period(entry)
            found.append(
                Period(
                    start=begin.astimezone(dt.UTC).isoformat(),
                    end=finish.astimezone(dt.UTC).isoformat(),
                    kind=kind,
                )
            )
    return tuple(sorted(found, key=lambda period: (period.start, period.end, period.kind)))


def _calendar_periods(raw: str) -> tuple[Period, ...]:
    try:
        parsed = icalendar.Calendar.from_ical(raw)
    except (ValueError, IndexError, TypeError) as exc:
        raise SchedulingError("a free/busy answer was not valid iCalendar") from exc
    components = [child for child in parsed.walk() if child.name == "VFREEBUSY"]
    if len(components) != 1:
        raise SchedulingError("a free/busy answer did not contain exactly one VFREEBUSY")
    return _periods(components[0])


def _child(element: ET.Element, name: tuple[str, str]) -> ET.Element | None:
    return next((item for item in element if _element_name(item) == name), None)


def parse_response(body: bytes, *, expected: tuple[str, ...]) -> tuple[Availability, ...]:
    """Read a ``schedule-response`` into one answer per requested recipient.

    Every recipient asked about must appear exactly once. A missing recipient
    is malformed rather than an absence of busy time, because the two readings
    differ by exactly the meeting this answer would be used to book.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SchedulingError("the free/busy response was not valid XML") from exc
    if _element_name(root) != (CALDAV, "schedule-response"):
        raise SchedulingError("the free/busy response was not a schedule-response")

    answers: dict[str, Availability] = {}
    for entry in root:
        if _element_name(entry) != (CALDAV, "response"):
            continue
        recipient_element = _child(entry, (CALDAV, "recipient"))
        href = _child(recipient_element, (DAV, "href")) if recipient_element is not None else None
        raw_recipient = (href.text or "").strip() if href is not None else ""
        if not raw_recipient:
            raise SchedulingError("a free/busy answer named no recipient")
        status_element = _child(entry, (CALDAV, "request-status"))
        reported = (status_element.text or "").strip() if status_element is not None else ""
        try:
            recipient = canonical_address(raw_recipient, label="a free/busy recipient")
        except SchedulingError as exc:
            # The recipient is how an answer is matched to the question, so an
            # unreadable one leaves the answer unattributable. The status is
            # what actually explains it — a server that could not resolve an
            # address commonly echoes back a mangled one — so it is reported
            # instead of the parse failure it caused.
            detail = f"; the server reported {reported}" if reported else ""
            raise SchedulingError(
                f"the free/busy answer named an address this tool cannot read{detail}",
                exc.code,
            ) from exc
        if recipient in answers:
            raise SchedulingError(f"the server answered twice for {recipient}")
        status = reported
        if not status:
            raise SchedulingError(f"the free/busy answer for {recipient} carries no status")
        answered = status.split(".", 1)[0].strip() == _SUCCESS
        data_element = _child(entry, (CALDAV, "calendar-data"))
        raw = (data_element.text or "").strip() if data_element is not None else ""
        if answered and not raw:
            raise SchedulingError(
                f"the free/busy answer for {recipient} reported success without data"
            )
        answers[recipient] = Availability(
            recipient=recipient,
            request_status=status,
            answered=answered,
            periods=_calendar_periods(raw) if answered else (),
        )

    missing = [recipient for recipient in expected if recipient not in answers]
    if missing:
        raise SchedulingError(
            "the server did not answer for " + ", ".join(sorted(missing)),
            exits.MALFORMED_RESPONSE,
        )
    unexpected = [recipient for recipient in answers if recipient not in expected]
    if unexpected:
        raise SchedulingError(
            "the server answered for a recipient that was not asked about: "
            + ", ".join(sorted(unexpected))
        )
    return tuple(answers[recipient] for recipient in expected)


def query(
    profile: Any,
    *,
    session: Session,
    scheduling: SchedulingIdentity,
    start: dt.datetime,
    end: dt.datetime,
    attendees: tuple[str, ...] = (),
    now: dt.datetime | None = None,
) -> tuple[Availability, ...]:
    """Ask the scheduling outbox when each recipient is busy.

    With no attendee named the question is about this account, which is the
    common case and needs no address from the caller. The organizer is always
    one of this account's own calendar user addresses: the outbox belongs to
    this principal, and a server is right to refuse a request that claims to
    originate elsewhere.
    """
    if end <= start:
        raise SchedulingError("--to must be after --from", exits.USAGE)
    organizer = scheduling.addresses[0]
    recipients: list[str] = []
    for value in attendees or (organizer,):
        address = canonical_address(value, label="--attendee")
        if address in recipients:
            raise SchedulingError(
                f"{address} was named more than once", exits.USAGE
            )
        recipients.append(address)

    body = build_request(
        organizer=organizer,
        recipients=tuple(recipients),
        start=start,
        end=end,
        now=now or dt.datetime.now(dt.UTC),
        uid=f"{token_source.token_hex(16)}@ncl-freebusy",
    )
    response = session.request(
        "POST",
        scheduling.schedule_outbox,
        headers={"Content-Type": "text/calendar; charset=utf-8"},
        data=body,
    )
    if response.status == 403:
        raise SchedulingError(
            "the account may not query free/busy through its scheduling outbox",
            exits.SCOPE_DENIED,
        )
    if response.status not in {200, 207}:
        raise SchedulingError(
            f"the scheduling outbox answered {response.status} to a free/busy request",
            exits.MALFORMED_RESPONSE,
        )
    return parse_response(response.body, expected=tuple(recipients))
