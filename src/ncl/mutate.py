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
from .session import Session, SessionError

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


def _boundary(value: dt.datetime | dt.date, label: str) -> dt.datetime | dt.date:
    if isinstance(value, dt.datetime):
        return _stamp(value)
    if isinstance(value, dt.date):
        return value
    raise EventError(f"{label} must be a timezone-aware instant or an all-day date", exits.USAGE)


def _resource_href(calendar_href: str, uid: str) -> str:
    return f"{calendar_href.rstrip('/')}/{uid}.ics"


#: RFC 5545 orders priority the way a race does: 1 is first. Callers think in
#: "how much does this matter", so the mapping is stated once, here, rather
#: than left for each of them to get backwards.
PRIORITY_RANGE = range(1, 10)

STATUSES = ("CONFIRMED", "TENTATIVE", "CANCELLED")

CLASSES = ("PUBLIC", "PRIVATE", "CONFIDENTIAL")

PORTABLE_START = "--- ncl portable fields ---"
PORTABLE_END = "--- end ncl portable fields ---"
_PORTABLE_PROPERTIES = ("LOCATION", "URL", "STATUS", "CATEGORIES", "PRIORITY", "TRANSP", "CLASS")

# CalDAV servers may refresh these timestamps while storing a resource. Their
# presence is still validated by ``events._describe``; only a server-side value
# rewrite is outside the semantic comparison. Every other property and nested
# component remains part of the readback invariant.
_SERVER_MANAGED_PROPERTIES = frozenset({"DTSTAMP", "LAST-MODIFIED"})


def _validate_priority(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value not in PRIORITY_RANGE:
        raise EventError(
            f"priority must be 1 (highest) to 9 (lowest); got {value}", exits.USAGE
        )


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


def _property_values(component: Any, name: str) -> tuple[Any, ...]:
    value = component.get(name)
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def _portable_text(value: Any) -> str:
    """Render a property as one safe, plain-text line."""
    return str(value).replace("\r", r"\r").replace("\n", r"\n")


def _ical_text(value: Any) -> str:
    raw = value.to_ical() if hasattr(value, "to_ical") else str(value).encode("utf-8")
    return raw.decode("utf-8", "replace")


def _portable_lines(event: Any) -> list[str]:
    lines: list[str] = []
    for name in _PORTABLE_PROPERTIES:
        if name == "CATEGORIES":
            categories: list[str] = []
            for value in _property_values(event, name):
                items = getattr(value, "cats", None)
                if items is None:
                    categories.append(str(value))
                else:
                    categories.extend(str(item) for item in items)
            if categories:
                lines.append(f"{name}: {_portable_text(', '.join(categories))}")
            continue
        for value in _property_values(event, name):
            text = _portable_text(value)
            if text:
                lines.append(f"{name}: {text}")

    for component in event.subcomponents:
        if component.name != "VALARM":
            continue
        trigger = component.get("TRIGGER")
        if trigger is not None:
            lines.append(f"VALARM TRIGGER: {_portable_text(_ical_text(trigger))}")
    return lines


def _portable_bounds(description: str) -> tuple[int, int] | None:
    starts: list[tuple[int, int]] = []
    ends: list[tuple[int, int]] = []
    offset = 0
    for line in description.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if PORTABLE_START in content or PORTABLE_END in content:
            if content == PORTABLE_START:
                starts.append((offset, offset + len(line)))
            elif content == PORTABLE_END:
                ends.append((offset, offset + len(line)))
            else:
                raise EventError(
                    "the description contains a portable delimiter that is not on its own line",
                    exits.USAGE,
                )
        offset += len(line)

    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1:
        raise EventError(
            "the description contains duplicate, nested, or unpaired portable delimiters",
            exits.USAGE,
        )
    start, _ = starts[0]
    end_start, end = ends[0]
    if end_start < start:
        raise EventError(
            "the description contains reversed portable delimiters", exits.USAGE
        )
    return start, end


def _portable_description(description: str, event: Any) -> str:
    """Replace or append the canonical structured-field compatibility block."""
    bounds = _portable_bounds(description)
    block = "\n".join((PORTABLE_START, *_portable_lines(event), PORTABLE_END))
    if bounds is not None:
        start, end = bounds
        suffix = description[end:]
        return description[:start] + block + ("\n" if suffix else "") + suffix
    prose = description.rstrip("\r\n")
    return block if not prose else f"{prose}\n\n{block}"


def _description(event: Any) -> str:
    values = _property_values(event, "DESCRIPTION")
    return "\n".join(str(value) for value in values)


def _set_description(event: Any, description: str) -> None:
    event.pop("DESCRIPTION", None)
    event.add("description", description)


def _semantic_value(value: Any) -> tuple[Any, ...]:
    typed = getattr(value, "dt", None)
    if isinstance(typed, dt.datetime):
        if typed.tzinfo is None:
            return ("datetime", "floating", typed.isoformat())
        return ("datetime", "instant", typed.astimezone(dt.UTC).isoformat())
    if isinstance(typed, dt.date):
        return ("date", typed.isoformat())
    if isinstance(typed, dt.timedelta):
        return ("duration", typed.total_seconds())
    categories = getattr(value, "cats", None)
    if categories is not None:
        return ("categories", tuple(sorted(str(item) for item in categories)))
    return ("text", str(value))


def _semantic_parameter(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return tuple(sorted(_semantic_parameter(item) for item in value))
    return str(value)


def _semantic_component(component: Any) -> tuple[Any, ...]:
    properties = []
    for name, value in component.property_items():
        upper_name = name.upper()
        if upper_name in _SERVER_MANAGED_PROPERTIES:
            continue
        params = getattr(value, "params", {})
        parameters = tuple(
            sorted((key.upper(), _semantic_parameter(item)) for key, item in params.items())
        )
        properties.append((upper_name, parameters, _semantic_value(value)))
    children = sorted(
        (_semantic_component(child) for child in component.subcomponents), key=repr
    )
    return (
        component.name.upper(),
        tuple(sorted(properties, key=repr)),
        tuple(children),
    )


def _semantic_calendar(raw: bytes) -> tuple[Any, ...]:
    try:
        calendar = icalendar.Calendar.from_ical(raw)
    except (ValueError, IndexError, TypeError) as exc:
        raise EventError("the stored event is not valid iCalendar") from exc
    return _semantic_component(calendar)


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
        _validate_priority(priority)
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
    portable_description: bool = False,
    now: dt.datetime | None = None,
    sequence: int = 0,
) -> str:
    """Serialize one simple VEVENT.

    Deliberately narrow: no recurrence or attendees. Those are refused rather
    than half-modelled, because writing a structure this tool does not understand
    is how an update silently destroys what it did not read.
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
    if portable_description:
        _set_description(event, _portable_description(description, event))
    calendar.add_component(event)
    return calendar.to_ical().decode("utf-8")


def patch_event(
    raw: bytes,
    changes: dict[str, Any],
    *,
    portable_description: bool = False,
    now: dt.datetime | None = None,
) -> str:
    """Apply named changes to a stored event, preserving everything else.

    The whole resource is reparsed and re-serialized, and only the requested
    properties are touched. Components this tool has no opinion about —
    VTIMEZONE among them — and unknown X- properties survive, because dropping
    them would be a silent loss the caller sees as exit 0.
    """
    try:
        calendar = icalendar.Calendar.from_ical(raw)
    except (ValueError, IndexError, TypeError) as exc:
        raise EventError("the stored event is not valid iCalendar") from exc

    reference = events._describe(raw, calendar_href="", href="", etag="")
    if not reference.writable:
        unsupported = ", ".join(reference.unsupported)
        raise EventError(
            f"this event carries {unsupported}, which this release refuses to modify",
            exits.UNSUPPORTED_STRUCTURE,
        )

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
    original_start = getattr(event.get("DTSTART"), "dt", None)
    original_kind = events._value_kind(original_start)
    boundary_changes = {
        name.upper()
        for name, requested in changes.items()
        if name.upper() in {"DTSTART", "DTEND"} and requested is not None
    }

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
        if key == "PRIORITY" and requested is not None:
            _validate_priority(requested)
        if requested is None:
            event.pop(key, None)
            continue
        value = _boundary(requested, key) if key in {"DTSTART", "DTEND"} else requested
        if key == "DTEND":
            event.pop("DURATION", None)
        event.pop(key, None)
        event.add(key, value)

    if portable_description:
        _set_description(event, _portable_description(_description(event), event))

    start = getattr(event.get("DTSTART"), "dt", None)
    if (
        events._value_kind(start) != original_kind
        and not {"DTSTART", "DTEND"}.issubset(boundary_changes)
    ):
        raise EventError(
            "converting between all-day and timed events requires both DTSTART and DTEND",
            exits.USAGE,
        )
    events._event_bounds(event, href="", code=exits.USAGE)

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
    portable_description: bool = False,
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
        portable_description=portable_description,
    )
    href = _resource_href(calendar_href, uid)
    planned = events._describe(
        payload.encode("utf-8"), calendar_href=calendar_href, href=href, etag=""
    )
    return plans.write(
        profile=profile.name,
        action="cal.create",
        href=href,
        etag="",
        summary=summary,
        payload=payload.encode("utf-8"),
        content_type="text/calendar; charset=utf-8",
        details={
            "calendar_href": calendar_href,
            "uid": uid,
            "start": events._utc(start),
            "end": events._utc(end),
            "all_day": planned.all_day,
            "url": planned.url,
            "status": planned.status,
            "portable_description": portable_description,
        },
    )


def plan_update(
    profile: Any,
    *,
    session: Session,
    href: str,
    changes: dict[str, Any],
    portable_description: bool = False,
) -> plans.Plan:
    for name, value in changes.items():
        if name.upper() == "PRIORITY" and value is not None:
            _validate_priority(value)
    reference, raw = events.fetch(profile, session=session, href=href)
    if not reference.writable:
        unsupported = ", ".join(reference.unsupported)
        raise EventError(
            f"this event carries {unsupported}, which this release refuses to modify",
            exits.UNSUPPORTED_STRUCTURE,
        )
    if not reference.etag:
        raise EventError(
            "the server returned no ETag for this event, so an update cannot be made "
            "conditional and could overwrite a concurrent change",
            exits.MALFORMED_RESPONSE,
        )
    etag = events.strong_etag(reference.etag)
    payload = patch_event(raw, changes, portable_description=portable_description)
    updated = events._describe(
        payload.encode("utf-8"),
        calendar_href=reference.calendar_href,
        href=reference.href,
        etag=etag,
    )
    return plans.write(
        profile=profile.name,
        action="cal.update",
        href=reference.href,
        etag=etag,
        summary=updated.summary,
        payload=payload.encode("utf-8"),
        content_type="text/calendar; charset=utf-8",
        details={
            "calendar_href": reference.calendar_href,
            "uid": reference.uid,
            "start": updated.start,
            "end": updated.end,
            "all_day": updated.all_day,
            "url": updated.url,
            "status": updated.status,
            "portable_description": portable_description,
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
    etag = events.strong_etag(reference.etag)
    return plans.write(
        profile=profile.name,
        action="cal.delete",
        href=reference.href,
        etag=etag,
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


def _description_from_raw(raw: bytes) -> str:
    try:
        calendar = icalendar.Calendar.from_ical(raw)
    except (ValueError, IndexError, TypeError) as exc:
        raise EventError("the stored event is not valid iCalendar") from exc
    targets = [item for item in calendar.walk() if item.name == "VEVENT"]
    if not targets:
        raise EventError("the stored resource holds no VEVENT")
    return _description(targets[0])


def _response_url(response: Any) -> str:
    return getattr(response, "url", "") or ""


def _response_location(response: Any) -> str:
    header = getattr(response, "header", None)
    if not callable(header):
        return ""
    return header("Location") or ""


def _refuse_redirect(response: Any, *, action: str, href: str) -> None:
    if 300 <= response.status < 400 or _response_location(response):
        raise EventError(
            f"the server redirected calendar {action} at {href}; the mutation target "
            "must remain exact",
            exits.MALFORMED_RESPONSE,
        )
    response_url = _response_url(response)
    if response_url and response_url != href:
        raise EventError(
            f"the server answered calendar {action} for a different href",
            exits.MALFORMED_RESPONSE,
        )


def _verify_deleted(session: Session, href: str) -> None:
    try:
        response = session.request(
            "GET",
            href,
            headers={"Accept": "text/calendar"},
            max_redirects=0,
        )
    except (EventError, SessionError) as exc:
        raise EventError(
            f"the absence of {href} could not be verified after deletion",
            exits.OUTCOME_UNCERTAIN,
        ) from exc
    if _response_url(response) and _response_url(response) != href:
        raise EventError(
            f"the post-delete readback addressed a different href than {href}",
            exits.OUTCOME_UNCERTAIN,
        )
    if response.status == 404:
        return
    raise EventError(
        f"the server returned status {response.status} while verifying that {href} was deleted",
        exits.OUTCOME_UNCERTAIN,
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

    if plan.action == "cal.delete":
        response = session.request(
            "DELETE",
            plan.href,
            headers={"If-Match": plan.etag},
            max_redirects=0,
        )
        _refuse_redirect(response, action="deletion", href=plan.href)
    elif plan.action == "cal.create":
        # The href derives from a freshly minted UID, so anything already there
        # is a different event: refuse rather than overwrite it.
        response = session.request(
            "PUT",
            plan.href,
            headers={"Content-Type": plan.content_type, "If-None-Match": "*"},
            data=plans.payload_bytes(plan),
            max_redirects=0,
        )
        _refuse_redirect(response, action="creation", href=plan.href)
    elif plan.action == "cal.update":
        # Conditional on the ETag observed while planning, so a change that
        # landed in between conflicts instead of being silently overwritten.
        response = session.request(
            "PUT",
            plan.href,
            headers={"Content-Type": plan.content_type, "If-Match": plan.etag},
            data=plans.payload_bytes(plan),
            max_redirects=0,
        )
        _refuse_redirect(response, action="update", href=plan.href)
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
        _verify_deleted(session, plan.href)
        result["verified"] = "deleted"
        plans.consume(plan.plan_id)
        return result

    # A server may accept a PUT and still rewrite or drop part of it, so the
    # mutation is not established until the stored resource says what was asked.
    try:
        stored, stored_raw = events.fetch(profile, session=session, href=plan.href)
    except (EventError, SessionError) as exc:
        raise EventError(
            f"the server accepted {plan.action}, but its readback could not be verified",
            exits.OUTCOME_UNCERTAIN,
        ) from exc
    result["etag"] = stored.etag
    result["summary"] = stored.summary
    result["start"] = stored.start
    result["end"] = stored.end
    result["all_day"] = stored.all_day
    result["url"] = stored.url
    result["status"] = stored.status
    mismatches: list[str] = []
    if stored.summary != plan.summary:
        mismatches.append(f"summary {stored.summary!r} != {plan.summary!r}")
    if not _same_instant(stored.start, str(plan.details.get("start", ""))):
        mismatches.append(f"start {stored.start!r} != {plan.details.get('start', '')!r}")
    if not _same_instant(stored.end, str(plan.details.get("end", ""))):
        mismatches.append(f"end {stored.end!r} != {plan.details.get('end', '')!r}")
    if "all_day" in plan.details and stored.all_day != plan.details["all_day"]:
        mismatches.append(f"all_day {stored.all_day!r} != {plan.details['all_day']!r}")
    for field in ("url", "status"):
        if field in plan.details and getattr(stored, field) != plan.details[field]:
            mismatches.append(f"{field} {getattr(stored, field)!r} != {plan.details[field]!r}")
    if _semantic_calendar(stored_raw) != _semantic_calendar(plans.payload_bytes(plan)):
        mismatches.append("semantic iCalendar content differs from the planned resource")
    if plan.details.get("portable_description"):
        expected_description = _description_from_raw(plans.payload_bytes(plan))
        if _description_from_raw(stored_raw) != expected_description:
            mismatches.append("portable description differs from the planned projection")
        result["portable_description_verified"] = True
    result["verified"] = not mismatches
    if not result["verified"]:
        raise EventError(
            f"the server stored something different at {plan.href}: " + "; ".join(mismatches),
            exits.OUTCOME_UNCERTAIN,
        )
    plans.consume(plan.plan_id)
    return result
