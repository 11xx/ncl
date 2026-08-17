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


def _stamp(moment: dt.datetime) -> None:
    if moment.tzinfo is None:
        raise EventError("event times must carry a timezone", exits.USAGE)


def _resource_href(calendar_href: str, uid: str) -> str:
    return f"{calendar_href.rstrip('/')}/{uid}.ics"


def build_event(
    *,
    uid: str,
    summary: str,
    start: dt.datetime,
    end: dt.datetime,
    description: str = "",
    location: str = "",
    now: dt.datetime | None = None,
    sequence: int = 0,
) -> str:
    """Serialize one simple VEVENT.

    Deliberately narrow: no recurrence, no attendees, no alarms. Those are
    refused rather than half-modelled, because writing a structure this tool
    does not understand is how an update silently destroys what it did not read.
    """
    _stamp(start)
    _stamp(end)
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
    if description:
        event.add("description", description)
    if location:
        event.add("location", location)
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

    for name, value in changes.items():
        key = name.upper()
        if value is None:
            event.pop(key, None)
            continue
        if key in {"DTSTART", "DTEND"}:
            _stamp(value)
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
    )
    return plans.write(
        profile=profile.name,
        action="create",
        calendar_href=calendar_href,
        href=_resource_href(calendar_href, uid),
        uid=uid,
        etag="",
        summary=summary,
        start=events._utc(start),
        end=events._utc(end),
        payload=payload,
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
        action="update",
        calendar_href=reference.calendar_href,
        href=reference.href,
        uid=reference.uid,
        etag=reference.etag,
        summary=updated.summary,
        start=updated.start,
        end=updated.end,
        payload=payload,
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
        action="delete",
        calendar_href=reference.calendar_href,
        href=reference.href,
        uid=reference.uid,
        etag=reference.etag,
        summary=reference.summary,
        start=reference.start,
        end=reference.end,
        payload="",
    )


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

    if plan.action == "delete":
        response = session.request("DELETE", plan.href, headers={"If-Match": plan.etag})
    elif plan.action == "create":
        # The href derives from a freshly minted UID, so anything already there
        # is a different event: refuse rather than overwrite it.
        response = session.request(
            "PUT",
            plan.href,
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
            data=plan.payload,
        )
    elif plan.action == "update":
        # Conditional on the ETag observed while planning, so a change that
        # landed in between conflicts instead of being silently overwritten.
        response = session.request(
            "PUT",
            plan.href,
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-Match": plan.etag},
            data=plan.payload,
        )
    else:
        raise plans.PlanError(f"unknown plan action {plan.action!r}", exits.USAGE)

    if response.status == 412:
        raise plans.PlanError(
            f"the event at {plan.href} changed since the plan was made; re-plan against "
            "its current state",
            exits.CONFLICT,
        )
    if response.status == 404 and plan.action != "create":
        raise EventError(f"no event exists at {plan.href}", exits.TARGET_NOT_FOUND)
    if response.status not in {200, 201, 204}:
        raise EventError(
            f"the server refused the {plan.action} with status {response.status}",
            exits.SERVER_ERROR,
        )

    result: dict[str, Any] = {"action": plan.action, "href": plan.href, "uid": plan.uid}
    if plan.action == "delete":
        result["verified"] = "deleted"
        plans.consume(plan.plan_id)
        return result

    # A server may accept a PUT and still rewrite or drop part of it, so the
    # mutation is not established until the stored resource says what was asked.
    stored, _ = events.fetch(profile, session=session, href=plan.href)
    result["etag"] = stored.etag
    result["summary"] = stored.summary
    result["start"] = stored.start
    result["verified"] = stored.summary == plan.summary and stored.start == plan.start
    if not result["verified"]:
        raise EventError(
            f"the server stored something different at {plan.href}: it reports summary "
            f"{stored.summary!r} starting {stored.start!r}",
            exits.OUTCOME_UNCERTAIN,
        )
    plans.consume(plan.plan_id)
    return result
