"""Building and applying calendar mutations.

Creation is conditional on the resource not existing; update and deletion are
conditional on the ETag observed when the plan was frozen. A mutation that
finds the world changed underneath it conflicts rather than overwriting.
"""

from __future__ import annotations

import datetime as dt
import secrets as token_source
from typing import Any

import icalendar

from . import events, exits, plans, profiles
from .caldav import CalendarError
from .events import EventError
from .session import Session

PRODID = "-//ai-agent-nextcloud//ncl//EN"


def _stamp(moment: dt.datetime) -> dt.datetime:
    """Require an unambiguous instant, and store it as UTC.

    A fixed-offset datetime serializes as `TZID="UTC-03:00"`, which names a
    time zone that no VTIMEZONE in the file defines and that is not an IANA
    identifier. Nothing can resolve it on the way back, so the value reparses
    as a naive local time and the event is invalid iCalendar even though its
    wall-clock reading looks right.

    Converting to UTC sidesteps the whole question: `Z` needs no VTIMEZONE and
    every client renders it in the reader's own zone.
    """
    if moment.tzinfo is None:
        raise EventError("event times must carry a timezone", exits.USAGE)
    return moment.astimezone(dt.UTC)


def _resource_href(calendar_href: str, uid: str) -> str:
    return f"{calendar_href.rstrip('/')}/{uid}.ics"


#: RFC 5545 orders priority the way a race does: 1 is first. Callers think in
#: "how much does this matter", so the mapping is stated once, here, rather
#: than left for each of them to get backwards.
PRIORITY_RANGE = range(1, 10)

STATUSES = ("CONFIRMED", "TENTATIVE", "CANCELLED")

CLASSES = ("PUBLIC", "PRIVATE", "CONFIDENTIAL")


def _alarm(trigger: str) -> Any:
    """A display reminder, offset from the event's start.

    The trigger is an RFC 5545 duration and is negative for "before": -PT15M is
    a quarter of an hour ahead of the start. A bare positive duration fires
    *after* the event begins, which is legal and almost never meant, so it is
    accepted only when written explicitly.
    """
    alarm = icalendar.Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", "Reminder")
    try:
        alarm.add("trigger", icalendar.prop.vDuration.from_ical(trigger))
    except (ValueError, TypeError) as exc:
        raise EventError(
            f"{trigger!r} is not an RFC 5545 duration such as -PT15M or -P1D", exits.USAGE
        ) from exc
    return alarm


def _optional_fields(
    event: Any,
    *,
    description: str = "",
    location: str = "",
    priority: int | None = None,
    categories: tuple[str, ...] = (),
    status: str = "",
    busy: bool | None = None,
    url: str = "",
    classification: str = "",
    color: str = "",
    related_to: tuple[str, ...] = (),
    alarms: tuple[str, ...] = (),
) -> None:
    """Attach the properties that tell a client how much an event matters."""
    if description:
        event.add("description", description)
    if location:
        event.add("location", location)
    if priority is not None:
        if priority not in PRIORITY_RANGE:
            raise EventError(
                f"priority must be 1 (highest) to 9 (lowest); got {priority}", exits.USAGE
            )
        event.add("priority", priority)
    if categories:
        event.add("categories", list(categories))
    if status:
        if status.upper() not in STATUSES:
            raise EventError(
                f"status must be one of {', '.join(STATUSES)}; got {status!r}", exits.USAGE
            )
        event.add("status", status.upper())
    if busy is not None:
        # OPAQUE consumes free/busy time; TRANSPARENT leaves the slot bookable,
        # which is what a reminder-shaped block wants.
        event.add("transp", "OPAQUE" if busy else "TRANSPARENT")
    if url:
        event.add("url", url)
    if classification:
        if classification.upper() not in CLASSES:
            raise EventError(
                f"class must be one of {', '.join(CLASSES)}; got {classification!r}",
                exits.USAGE,
            )
        event.add("class", classification.upper())
    if color:
        # RFC 7986 takes a CSS3 colour name; Nextcloud honours it per event.
        event.add("color", color)
    for uid in related_to:
        # RELATED-TO is what ties a preparation block, a travel block, and the
        # appointment they serve into one thing a client can follow, without
        # inventing a convention this tool would then have to defend.
        event.add("related-to", uid)
    for trigger in alarms:
        event.add_component(_alarm(trigger))


def build_event(
    *,
    uid: str,
    summary: str,
    start: dt.datetime,
    end: dt.datetime,
    description: str = "",
    location: str = "",
    priority: int | None = None,
    categories: tuple[str, ...] = (),
    status: str = "",
    busy: bool | None = None,
    url: str = "",
    classification: str = "",
    color: str = "",
    related_to: tuple[str, ...] = (),
    alarms: tuple[str, ...] = (),
    now: dt.datetime | None = None,
    sequence: int = 0,
) -> str:
    """Serialize one simple VEVENT.

    Deliberately narrow: no recurrence, no attendees, no alarms. Those are
    refused rather than half-modelled, because writing a structure this tool
    does not understand is how an update silently destroys what it did not read.
    """
    start = _stamp(start)
    end = _stamp(end)
    if end <= start:
        raise EventError("the event ends before it starts", exits.USAGE)

    calendar = icalendar.Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    event = icalendar.Event()
    event.add("uid", uid)
    event.add("summary", summary)
    event.add("dtstart", start)
    event.add("dtend", end)
    event.add("dtstamp", now or dt.datetime.now(dt.UTC))
    event.add("sequence", sequence)
    _optional_fields(
        event,
        description=description,
        location=location,
        priority=priority,
        categories=categories,
        status=status,
        busy=busy,
        url=url,
        classification=classification,
        color=color,
        related_to=related_to,
        alarms=alarms,
    )
    calendar.add_component(event)
    return calendar.to_ical().decode("utf-8")


def patch_event(raw: bytes, changes: dict[str, Any], *, now: dt.datetime | None = None) -> str:
    """Apply named changes to a stored event, preserving everything else.

    The whole resource is reparsed and re-serialized, and only the requested
    properties are touched. Components this tool has no opinion about —
    VTIMEZONE among them — and unknown X- properties survive, because dropping
    them would be a silent loss the caller sees as exit 0.
    """
    try:
        calendar = icalendar.Calendar.from_ical(raw)
    except (ValueError, IndexError) as exc:
        raise EventError("the stored event is not valid iCalendar") from exc

    targets = [item for item in calendar.walk() if item.name == "VEVENT"]
    if not targets:
        raise EventError("the stored resource holds no VEVENT")
    if len(targets) > 1:
        raise EventError(
            "the resource holds more than one VEVENT; recurrence overrides are not "
            "supported for editing in this release",
            exits.UNSUPPORTED_STRUCTURE,
        )
    event = targets[0]

    present = [name for name in events.UNSUPPORTED_PROPERTIES if name in event]
    if present:
        raise EventError(
            f"this event carries {', '.join(present)}, which this release refuses to "
            "modify because doing so can send scheduling messages or rewrite a series",
            exits.UNSUPPORTED_STRUCTURE,
        )

    for name, requested in changes.items():
        key = name.upper()
        if key == "VALARM":
            # Reminders are subcomponents, so "change the alarms" means replace
            # the set rather than set a property. An empty list removes them.
            event.subcomponents = [
                item for item in event.subcomponents if item.name != "VALARM"
            ]
            for trigger in requested or ():
                event.add_component(_alarm(trigger))
            continue
        if requested is None:
            event.pop(key, None)
            continue
        value = _stamp(requested) if key in {"DTSTART", "DTEND"} else requested
        event.pop(key, None)
        event.add(key, value)

    start = getattr(event.get("DTSTART"), "dt", None)
    end = getattr(event.get("DTEND"), "dt", None)
    if isinstance(start, dt.datetime) and isinstance(end, dt.datetime) and end <= start:
        raise EventError("the event would end before it starts", exits.USAGE)

    event.pop("DTSTAMP", None)
    event.add("dtstamp", now or dt.datetime.now(dt.UTC))
    try:
        sequence = int(str(event.get("SEQUENCE", 0)))
    except (TypeError, ValueError):
        sequence = 0
    event.pop("SEQUENCE", None)
    event.add("sequence", sequence + 1)
    return calendar.to_ical().decode("utf-8")


def plan_create(
    profile: Any,
    *,
    calendar_href: str,
    summary: str,
    start: dt.datetime,
    end: dt.datetime,
    description: str = "",
    location: str = "",
    priority: int | None = None,
    categories: tuple[str, ...] = (),
    status: str = "",
    busy: bool | None = None,
    url: str = "",
    classification: str = "",
    color: str = "",
    related_to: tuple[str, ...] = (),
    alarms: tuple[str, ...] = (),
) -> plans.Plan:
    if not profiles.in_scope(calendar_href, list(profile.calendars)):
        raise CalendarError(
            f"{calendar_href} is outside this profile's calendar allowlist",
            exits.SCOPE_DENIED,
        )
    uid = f"{token_source.token_hex(16)}@ncl"
    payload = build_event(
        uid=uid,
        summary=summary,
        start=start,
        end=end,
        description=description,
        location=location,
        priority=priority,
        categories=categories,
        status=status,
        busy=busy,
        url=url,
        classification=classification,
        color=color,
        related_to=related_to,
        alarms=alarms,
    )
    return plans.write(
        profile=profile.name,
        action="cal.create",
        href=_resource_href(calendar_href, uid),
        etag="",
        summary=summary,
        payload=payload.encode("utf-8"),
        content_type="text/calendar; charset=utf-8",
        details={
            "calendar_href": calendar_href,
            "uid": uid,
            "start": events._utc(start),
            "end": events._utc(end),
        },
    )


def plan_update(
    profile: Any,
    *,
    session: Session,
    href: str,
    changes: dict[str, Any],
) -> plans.Plan:
    reference, raw = events.fetch(profile, session=session, href=href)
    if not reference.etag:
        raise EventError(
            "the server returned no ETag for this event, so an update cannot be made "
            "conditional and could overwrite a concurrent change",
            exits.MALFORMED_RESPONSE,
        )
    payload = patch_event(raw, changes)
    updated = events._describe(
        payload.encode("utf-8"),
        calendar_href=reference.calendar_href,
        href=reference.href,
        etag=reference.etag,
    )
    return plans.write(
        profile=profile.name,
        action="cal.update",
        href=reference.href,
        etag=reference.etag,
        summary=updated.summary,
        payload=payload.encode("utf-8"),
        content_type="text/calendar; charset=utf-8",
        details={
            "calendar_href": reference.calendar_href,
            "uid": reference.uid,
            "start": updated.start,
            "end": updated.end,
        },
    )


def plan_delete(profile: Any, *, session: Session, href: str) -> plans.Plan:
    reference, _ = events.fetch(profile, session=session, href=href)
    if not reference.etag:
        raise EventError(
            "the server returned no ETag for this event, so a deletion cannot be made "
            "conditional",
            exits.MALFORMED_RESPONSE,
        )
    return plans.write(
        profile=profile.name,
        action="cal.delete",
        href=reference.href,
        etag=reference.etag,
        summary=reference.summary,
        details={
            "calendar_href": reference.calendar_href,
            "uid": reference.uid,
            "start": reference.start,
            "end": reference.end,
        },
    )


def _same_instant(stored: str, planned: str) -> bool:
    """Compare two stamps as moments, not as text.

    A server may store the instant it was given in a different but equivalent
    encoding, and comparing the formatted strings would call that a mismatch —
    reporting an uncertain outcome for a write that landed exactly as asked.
    A stamp without a zone cannot be compared to one with a zone at all, so it
    is a genuine mismatch rather than something to guess about.
    """
    if stored == planned:
        return True
    if not stored.endswith("Z") or not planned.endswith("Z"):
        return False
    fmt = "%Y%m%dT%H%M%SZ"
    try:
        return dt.datetime.strptime(stored, fmt) == dt.datetime.strptime(planned, fmt)
    except ValueError:
        return False


def apply(profile: Any, *, session: Session, plan: plans.Plan) -> dict[str, Any]:
    """Execute a frozen plan, conditionally, and read the result back."""
    if plan.profile != profile.name:
        raise plans.PlanError(
            f"plan {plan.plan_id} was made for profile {plan.profile!r}", exits.USAGE
        )
    plans.check_fresh(plan)
    if not profiles.in_scope(plan.href, list(profile.calendars)):
        raise CalendarError(
            f"{plan.href} is outside this profile's calendar allowlist", exits.SCOPE_DENIED
        )

    if plan.action == "cal.delete":
        response = session.request("DELETE", plan.href, headers={"If-Match": plan.etag})
    elif plan.action == "cal.create":
        # The href derives from a freshly minted UID, so anything already there
        # is a different event: refuse rather than overwrite it.
        response = session.request(
            "PUT",
            plan.href,
            headers={"Content-Type": plan.content_type, "If-None-Match": "*"},
            data=plans.payload_bytes(plan),
        )
    elif plan.action == "cal.update":
        # Conditional on the ETag observed while planning, so a change that
        # landed in between conflicts instead of being silently overwritten.
        response = session.request(
            "PUT",
            plan.href,
            headers={"Content-Type": plan.content_type, "If-Match": plan.etag},
            data=plans.payload_bytes(plan),
        )
    else:
        raise plans.PlanError(f"unknown plan action {plan.action!r}", exits.USAGE)

    if response.status == 412:
        raise plans.PlanError(
            f"the event at {plan.href} changed since the plan was made; re-plan against "
            "its current state",
            exits.CONFLICT,
        )
    if response.status == 404 and plan.action != "cal.create":
        raise EventError(f"no event exists at {plan.href}", exits.TARGET_NOT_FOUND)
    if response.status not in {200, 201, 204}:
        raise EventError(
            f"the server refused the {plan.action} with status {response.status}",
            exits.SERVER_ERROR,
        )

    result: dict[str, Any] = {
        "action": plan.action,
        "href": plan.href,
        "uid": plan.details.get("uid", ""),
    }
    if plan.action == "cal.delete":
        result["verified"] = "deleted"
        plans.consume(plan.plan_id)
        return result

    # A server may accept a PUT and still rewrite or drop part of it, so the
    # mutation is not established until the stored resource says what was asked.
    stored, _ = events.fetch(profile, session=session, href=plan.href)
    result["etag"] = stored.etag
    result["summary"] = stored.summary
    result["start"] = stored.start
    result["verified"] = stored.summary == plan.summary and _same_instant(
        stored.start, str(plan.details.get("start", ""))
    )
    if not result["verified"]:
        raise EventError(
            f"the server stored something different at {plan.href}: it reports summary "
            f"{stored.summary!r} starting {stored.start!r}",
            exits.OUTCOME_UNCERTAIN,
        )
    plans.consume(plan.plan_id)
    return result
