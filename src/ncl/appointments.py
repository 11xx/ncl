"""Plan and validate three-event appointment bundles.

An appointment bundle is one anchor event plus owned travel and preparation
events.  The anchor owns the appointment facts; the travel description owns a
deterministic route snapshot; the other event properties are preserved on an
update rather than reconstructed from a summary or UID.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets as token_source
from dataclasses import dataclass
from typing import Any

import icalendar

from . import events, exits, mutate, plans, profiles
from .caldav import CalendarError
from .session import Session

TRAVEL_SUMMARY = "Travel"
PREPARATION_SUMMARY = "Preparation"

TRAVEL_BLOCK_START = "--- ncl appointment travel ---"
TRAVEL_BLOCK_END = "--- end ncl appointment travel ---"
_TRAVEL_FIELDS = (
    "ORIGIN",
    "DESTINATION",
    "MODE",
    "LEAVE HOME",
    "PLANNED DEPARTURE",
    "TARGET ARRIVAL",
    "ROUTE ESTIMATE",
    "STOP/WAIT MARGIN",
    "ON-SITE BUFFER",
)
_TRAVEL_URL_FIELD = "ROUTE URL"

# This sentinel distinguishes omission, which preserves an existing route URL,
# from an explicit ``None``, which removes it during an update.
PRESERVE_ROUTE_URL = object()

_TIME_RE = re.compile(
    r"T(?=(?:\d+H|\d+M|\d+S))"
    r"(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?\Z"
)
_DURATION_RE = re.compile(
    r"P(?:"
    r"(?P<weeks>\d+)W|"
    r"(?P<days>\d+)D(?P<day_time>T[^\r\n]+)?|"
    r"(?P<time_only>T[^\r\n]+)"
    r")\Z"
)


class AppointmentError(events.EventError):
    """An appointment bundle fact or resource is not usable."""


@dataclass(frozen=True)
class Timeline:
    """The four instants derived backwards from the appointment start."""

    appointment_start: dt.datetime
    appointment_end: dt.datetime
    target_arrival: dt.datetime
    planned_departure: dt.datetime
    leave_home: dt.datetime
    preparation_start: dt.datetime
    route_estimate: dt.timedelta
    on_site_buffer: dt.timedelta
    stop_wait_margin: dt.timedelta
    preparation_duration: dt.timedelta


@dataclass(frozen=True)
class _Schedule:
    summary: str
    description: str
    origin: str
    destination: str
    mode: str
    timeline: Timeline
    route_url: str | None


@dataclass(frozen=True)
class _FetchedEvent:
    reference: events.EventRef
    raw: bytes
    component: Any
    etag: str


@dataclass(frozen=True)
class _ExistingBundle:
    appointment: _FetchedEvent
    travel: _FetchedEvent
    preparation: _FetchedEvent
    route_url: str | None


def _usage(message: str) -> AppointmentError:
    return AppointmentError(message, exits.USAGE)


def _unsupported(message: str) -> AppointmentError:
    return AppointmentError(message, exits.UNSUPPORTED_STRUCTURE)


def _text(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
    one_line: bool = True,
) -> str:
    if not isinstance(value, str):
        raise _usage(f"{label} must be text")
    if not allow_empty and not value.strip():
        raise _usage(f"{label} must not be empty")
    if one_line and ("\r" in value or "\n" in value):
        raise _usage(f"{label} must be one line")
    return value


def _optional_url(value: Any, label: str = "route URL") -> str | None:
    if value is None or value == "":
        return None
    return _text(value, label)


def _instant(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        raise _usage(f"{label} must be a timezone-aware instant")
    try:
        offset = value.utcoffset()
    except (TypeError, ValueError) as exc:
        raise _usage(f"{label} must be a timezone-aware instant") from exc
    if value.tzinfo is None or offset is None:
        raise _usage(f"{label} has no timezone offset; state the offset explicitly")
    if value.microsecond or offset.microseconds:
        raise _usage(f"{label} must have whole-second precision")
    return value


def parse_instant(value: str, label: str) -> dt.datetime:
    """Parse one explicit-offset, whole-second ISO 8601 instant."""
    if not isinstance(value, str):
        raise _usage(f"{label} is not an ISO 8601 instant")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise _usage(f"{label} is not an ISO 8601 instant") from exc
    return _instant(parsed, label)


def _time_parts(value: str, label: str) -> tuple[int, int, int]:
    match = _TIME_RE.fullmatch(value)
    if match is None:
        raise _usage(
            f"{label} is not an RFC 5545 day/time duration such as PT15M or P1DT2H"
        )
    try:
        return tuple(int(match.group(name) or 0) for name in ("hours", "minutes", "seconds"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise _usage(f"{label} is not an RFC 5545 day/time duration") from exc


def parse_duration(value: str, label: str, *, positive: bool = False) -> dt.timedelta:
    """Parse the RFC 5545 day/time duration subset used by this command."""
    if not isinstance(value, str):
        raise _usage(f"{label} must be an RFC 5545 day/time duration")
    match = _DURATION_RE.fullmatch(value)
    if match is None:
        raise _usage(
            f"{label} is not an RFC 5545 day/time duration such as PT15M or P1DT2H"
        )
    try:
        if match.group("weeks") is not None:
            duration = dt.timedelta(weeks=int(match.group("weeks")))
        else:
            days = int(match.group("days") or 0)
            time_text = match.group("day_time") or match.group("time_only")
            hours, minutes, seconds = _time_parts(time_text, label) if time_text else (0, 0, 0)
            duration = dt.timedelta(
                days=days,
                hours=hours,
                minutes=minutes,
                seconds=seconds,
            )
    except (TypeError, ValueError, OverflowError) as exc:
        raise _usage(f"{label} is outside the supported duration range") from exc
    if positive and duration <= dt.timedelta(0):
        raise _usage(f"{label} must be positive")
    return duration


def _duration(value: str | dt.timedelta, label: str, *, positive: bool) -> dt.timedelta:
    if isinstance(value, str):
        return parse_duration(value, label, positive=positive)
    if not isinstance(value, dt.timedelta):
        raise _usage(f"{label} must be an RFC 5545 day/time duration")
    if value.microseconds:
        raise _usage(f"{label} must have whole-second precision")
    if value < dt.timedelta(0) or (positive and value <= dt.timedelta(0)):
        qualifier = "positive" if positive else "nonnegative"
        raise _usage(f"{label} must be {qualifier}")
    return value


def duration_text(value: dt.timedelta) -> str:
    """Render an integral timedelta in deterministic RFC 5545 day/time form."""
    if not isinstance(value, dt.timedelta) or value < dt.timedelta(0) or value.microseconds:
        raise _usage("duration cannot be rendered as RFC 5545 day/time text")
    total_seconds = value.days * 86400 + value.seconds
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if not days and not hours and not minutes and not seconds:
        return "PT0S"
    text = f"P{days}D" if days else "P"
    time = "".join(
        part
        for part in (
            f"{hours}H" if hours else "",
            f"{minutes}M" if minutes else "",
            f"{seconds}S" if seconds else "",
        )
        if part
    )
    return text + (f"T{time}" if time else "")


def calculate_timeline(
    *,
    start: dt.datetime,
    end: dt.datetime,
    route_estimate: str | dt.timedelta,
    on_site_buffer: str | dt.timedelta,
    stop_wait_margin: str | dt.timedelta,
    preparation_duration: str | dt.timedelta,
) -> Timeline:
    """Apply the frozen backward arithmetic and reject unsafe instants."""
    appointment_start = _instant(start, "appointment start")
    appointment_end = _instant(end, "appointment end")
    if appointment_end <= appointment_start:
        raise _usage("appointment end must be after appointment start")
    route = _duration(route_estimate, "route estimate", positive=True)
    buffer = _duration(on_site_buffer, "on-site buffer", positive=False)
    margin = _duration(stop_wait_margin, "stop/wait margin", positive=False)
    preparation = _duration(preparation_duration, "preparation duration", positive=True)
    try:
        target_arrival = appointment_start - buffer
        planned_departure = target_arrival - route
        leave_home = planned_departure - margin
        preparation_start = leave_home - preparation
    except (OverflowError, ValueError) as exc:
        raise _usage("appointment timeline arithmetic overflowed the supported date range") from exc
    return Timeline(
        appointment_start=appointment_start,
        appointment_end=appointment_end,
        target_arrival=target_arrival,
        planned_departure=planned_departure,
        leave_home=leave_home,
        preparation_start=preparation_start,
        route_estimate=route,
        on_site_buffer=buffer,
        stop_wait_margin=margin,
        preparation_duration=preparation,
    )


def _schedule(
    *,
    summary: str,
    description: str,
    origin: str,
    destination: str,
    mode: str,
    start: dt.datetime,
    end: dt.datetime,
    route_estimate: str | dt.timedelta,
    on_site_buffer: str | dt.timedelta,
    stop_wait_margin: str | dt.timedelta,
    preparation_duration: str | dt.timedelta,
    route_url: str | None,
) -> _Schedule:
    return _Schedule(
        summary=_text(summary, "appointment summary"),
        description=_text(
            description, "appointment description", allow_empty=True, one_line=False
        ),
        origin=_text(origin, "origin"),
        destination=_text(destination, "destination address"),
        mode=_text(mode, "travel mode"),
        timeline=calculate_timeline(
            start=start,
            end=end,
            route_estimate=route_estimate,
            on_site_buffer=on_site_buffer,
            stop_wait_margin=stop_wait_margin,
            preparation_duration=preparation_duration,
        ),
        route_url=_optional_url(route_url),
    )


def _line(value: str, label: str) -> str:
    return _text(value, label)


def _travel_description(schedule: _Schedule) -> str:
    timeline = schedule.timeline
    lines = [
        TRAVEL_BLOCK_START,
        f"ORIGIN: {_line(schedule.origin, 'origin')}",
        f"DESTINATION: {_line(schedule.destination, 'destination address')}",
        f"MODE: {_line(schedule.mode, 'travel mode')}",
        f"LEAVE HOME: {events._utc(timeline.leave_home)}",
        f"PLANNED DEPARTURE: {events._utc(timeline.planned_departure)}",
        f"TARGET ARRIVAL: {events._utc(timeline.target_arrival)}",
        f"ROUTE ESTIMATE: {duration_text(timeline.route_estimate)}",
        f"STOP/WAIT MARGIN: {duration_text(timeline.stop_wait_margin)}",
        f"ON-SITE BUFFER: {duration_text(timeline.on_site_buffer)}",
    ]
    if schedule.route_url is not None:
        lines.append(f"{_TRAVEL_URL_FIELD}: {_line(schedule.route_url, 'route URL')}")
    lines.append(TRAVEL_BLOCK_END)
    return "\n".join(lines)


def _block_bounds(description: str) -> tuple[int, int] | None:
    starts: list[int] = []
    ends: list[tuple[int, int]] = []
    offset = 0
    for line in description.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if TRAVEL_BLOCK_START in content or TRAVEL_BLOCK_END in content:
            if content == TRAVEL_BLOCK_START:
                starts.append(offset)
            elif content == TRAVEL_BLOCK_END:
                ends.append((offset, offset + len(line)))
            else:
                raise _unsupported(
                    "the travel description contains a malformed owned-block delimiter"
                )
        offset += len(line)
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1:
        raise _unsupported(
            "the travel description has duplicate or unpaired owned-block delimiters"
        )
    start = starts[0]
    end_start, end = ends[0]
    if end_start < start:
        raise _unsupported("the travel description has reversed owned-block delimiters")
    return start, end


def _block_fields(description: str) -> tuple[dict[str, str], tuple[int, int]]:
    bounds = _block_bounds(description)
    if bounds is None:
        raise _unsupported("the travel event has no well-formed owned description block")
    start, end = bounds
    lines = description[start:end].splitlines()
    if not lines or lines[0] != TRAVEL_BLOCK_START or lines[-1] != TRAVEL_BLOCK_END:
        raise _unsupported("the travel event has an invalid owned description block")
    fields: dict[str, str] = {}
    for line in lines[1:-1]:
        label, separator, value = line.partition(": ")
        if not separator or label in fields or not value:
            raise _unsupported("the travel event has an invalid owned description field")
        fields[label] = value
    expected = set(_TRAVEL_FIELDS)
    if set(fields) not in (expected, expected | {_TRAVEL_URL_FIELD}):
        raise _unsupported("the travel event has an incomplete or unknown owned description field")
    return fields, bounds


def _description(component: Any) -> str:
    value = component.get("DESCRIPTION")
    if value is None:
        return ""
    values = value if isinstance(value, list) else [value]
    return "\n".join(str(item) for item in values)


def _replace_block(description: str, block: str) -> str:
    _, bounds = _block_fields(description)
    start, end = bounds
    suffix = description[end:]
    return description[:start] + block + ("\n" if suffix else "") + suffix


def _single_component(raw: bytes, href: str) -> Any:
    try:
        calendar = icalendar.Calendar.from_ical(raw)
    except (ValueError, IndexError, TypeError) as exc:
        raise _unsupported(f"the event at {href} is not valid iCalendar") from exc
    direct = [item for item in calendar.subcomponents if item.name == "VEVENT"]
    all_events = [item for item in calendar.walk() if item.name == "VEVENT"]
    if len(direct) != 1 or len(all_events) != 1:
        raise _unsupported(
            f"the event at {href} must contain exactly one direct VEVENT"
        )
    return direct[0]


def _typed_relations(component: Any, href: str) -> tuple[tuple[str, str], ...]:
    relations: list[tuple[str, str]] = []
    for name, value in component.property_items():
        if name.upper() != "RELATED-TO":
            continue
        reltype = getattr(value, "params", {}).get("RELTYPE")
        if reltype is None or not str(reltype).strip():
            raise _unsupported(f"the event at {href} has an untyped RELATED-TO value")
        relations.append((str(value), str(reltype).upper()))
    if len(set(relations)) != len(relations):
        raise _unsupported(f"the event at {href} has duplicate RELATED-TO values")
    return tuple(relations)


def _validate_timed(component: Any, href: str) -> None:
    if component.get("DURATION") is not None:
        raise _unsupported(f"the event at {href} uses DURATION instead of an explicit DTEND")
    start = getattr(component.get("DTSTART"), "dt", None)
    end = getattr(component.get("DTEND"), "dt", None)
    try:
        _instant(start, f"the event at {href} DTSTART")
        _instant(end, f"the event at {href} DTEND")
    except AppointmentError as exc:
        raise _unsupported(str(exc)) from exc


def _load_bundle(
    profile: Any,
    *,
    session: Session,
    appointment_href: str,
    travel_href: str,
    preparation_href: str,
) -> _ExistingBundle:
    hrefs = (appointment_href, travel_href, preparation_href)
    if not all(isinstance(href, str) and href for href in hrefs):
        raise _usage("appointment, travel, and preparation hrefs are required")
    if len(set(hrefs)) != len(hrefs):
        raise _unsupported("the three appointment bundle hrefs must be distinct")

    fetched: list[_FetchedEvent] = []
    for href in hrefs:
        reference, raw = events.fetch(profile, session=session, href=href)
        if not reference.writable:
            names = ", ".join(reference.unsupported)
            raise _unsupported(
                f"the event at {reference.href} carries unsupported structure: {names}"
            )
        _validate_timed(_single_component(raw, reference.href), reference.href)
        fetched.append(
            _FetchedEvent(
                reference=reference,
                raw=raw,
                component=_single_component(raw, reference.href),
                etag=events.strong_etag(reference.etag),
            )
        )

    collection = fetched[0].reference.calendar_href
    if any(item.reference.calendar_href != collection for item in fetched[1:]):
        raise _unsupported("appointment bundle resources must share one calendar collection")
    appointment, travel, preparation = fetched
    uids = {item.reference.uid for item in fetched}
    if len(uids) != 3:
        raise _unsupported("appointment bundle resources must have distinct UIDs")
    if set(_typed_relations(appointment.component, appointment.reference.href)) != {
        (travel.reference.uid, "CHILD"),
        (preparation.reference.uid, "CHILD"),
    }:
        raise _unsupported(
            "the appointment does not name exactly its travel and preparation children"
        )
    if _typed_relations(travel.component, travel.reference.href) != (
        (appointment.reference.uid, "PARENT"),
    ):
        raise _unsupported("the travel event does not name the appointment as its parent")
    if _typed_relations(preparation.component, preparation.reference.href) != (
        (appointment.reference.uid, "PARENT"),
    ):
        raise _unsupported("the preparation event does not name the appointment as its parent")

    fields, _ = _block_fields(_description(travel.component))
    stored_url = (
        str(travel.component.get("URL"))
        if travel.component.get("URL") is not None
        else None
    )
    block_url = fields.get(_TRAVEL_URL_FIELD)
    if (stored_url is None) != (block_url is None) or stored_url != block_url:
        raise _unsupported("the travel route URL and owned description block disagree")
    return _ExistingBundle(appointment, travel, preparation, stored_url)


def _resource_href(calendar_href: str, uid: str) -> str:
    return f"{calendar_href.rstrip('/')}/{uid}.ics"


def _step(
    *,
    action: str,
    calendar_href: str,
    href: str,
    etag: str,
    role: str,
    summary: str,
    uid: str,
    start: dt.datetime,
    end: dt.datetime,
    payload: str,
    check_url: bool = False,
    url: str = "",
) -> plans.Step:
    details: dict[str, Any] = {
        "calendar_href": calendar_href,
        "role": role,
        "uid": uid,
        "start": events._utc(start),
        "end": events._utc(end),
        "all_day": False,
    }
    if check_url:
        details["url"] = url
    return plans.freeze_step(
        action=action,
        href=href,
        etag=etag,
        summary=summary,
        payload=payload.encode("utf-8"),
        content_type="text/calendar; charset=utf-8",
        details=details,
    )


def _create_steps(profile: Any, calendar_href: str, schedule: _Schedule) -> tuple[plans.Step, ...]:
    token = token_source.token_hex(16)
    appointment_uid = f"{token}.appointment@ncl"
    travel_uid = f"{token}.travel@ncl"
    preparation_uid = f"{token}.preparation@ncl"
    timeline = schedule.timeline
    appointment = mutate.build_event(
        uid=appointment_uid,
        summary=schedule.summary,
        start=timeline.appointment_start,
        end=timeline.appointment_end,
        description=schedule.description,
        location=schedule.destination,
        related_to=((travel_uid, "CHILD"), (preparation_uid, "CHILD")),
    )
    travel = mutate.build_event(
        uid=travel_uid,
        summary=TRAVEL_SUMMARY,
        start=timeline.leave_home,
        end=timeline.target_arrival,
        description=_travel_description(schedule),
        url=schedule.route_url or "",
        related_to=((appointment_uid, "PARENT"),),
    )
    preparation = mutate.build_event(
        uid=preparation_uid,
        summary=PREPARATION_SUMMARY,
        start=timeline.preparation_start,
        end=timeline.leave_home,
        related_to=((appointment_uid, "PARENT"),),
    )
    return (
        _step(
            action="cal.create",
            calendar_href=calendar_href,
            href=_resource_href(calendar_href, appointment_uid),
            etag="",
            role="appointment",
            summary=schedule.summary,
            uid=appointment_uid,
            start=timeline.appointment_start,
            end=timeline.appointment_end,
            payload=appointment,
        ),
        _step(
            action="cal.create",
            calendar_href=calendar_href,
            href=_resource_href(calendar_href, travel_uid),
            etag="",
            role="travel",
            summary=TRAVEL_SUMMARY,
            uid=travel_uid,
            start=timeline.leave_home,
            end=timeline.target_arrival,
            payload=travel,
            check_url=True,
            url=schedule.route_url or "",
        ),
        _step(
            action="cal.create",
            calendar_href=calendar_href,
            href=_resource_href(calendar_href, preparation_uid),
            etag="",
            role="preparation",
            summary=PREPARATION_SUMMARY,
            uid=preparation_uid,
            start=timeline.preparation_start,
            end=timeline.leave_home,
            payload=preparation,
        ),
    )


def plan_create(
    profile: Any,
    *,
    calendar_href: str,
    summary: str,
    start: dt.datetime,
    end: dt.datetime,
    origin: str,
    destination: str,
    mode: str,
    route_estimate: str | dt.timedelta,
    on_site_buffer: str | dt.timedelta,
    stop_wait_margin: str | dt.timedelta,
    preparation_duration: str | dt.timedelta,
    route_url: str | None = None,
    description: str = "",
) -> plans.Plan:
    """Freeze an appointment, travel, and preparation creation bundle."""
    if not profiles.in_scope(calendar_href, list(profile.calendars)):
        raise CalendarError(
            f"{calendar_href} is outside this profile's calendar allowlist",
            exits.SCOPE_DENIED,
        )
    schedule = _schedule(
        summary=summary,
        description=description,
        origin=origin,
        destination=destination,
        mode=mode,
        start=start,
        end=end,
        route_estimate=route_estimate,
        on_site_buffer=on_site_buffer,
        stop_wait_margin=stop_wait_margin,
        preparation_duration=preparation_duration,
        route_url=route_url,
    )
    return plans.write_bundle(
        profile=profile,
        summary=schedule.summary,
        steps=_create_steps(profile, calendar_href, schedule),
    )


def _update_steps(
    bundle: _ExistingBundle,
    schedule: _Schedule,
    *,
    summary: str | None,
    description: str | None,
    route_url_was_explicit: bool,
) -> tuple[plans.Step, ...]:
    timeline = schedule.timeline
    appointment_changes: dict[str, Any] = {
        "DTSTART": timeline.appointment_start,
        "DTEND": timeline.appointment_end,
        "LOCATION": schedule.destination,
    }
    if summary is not None:
        appointment_changes["SUMMARY"] = schedule.summary
    if description is not None:
        appointment_changes["DESCRIPTION"] = schedule.description
    appointment = mutate.patch_event(bundle.appointment.raw, appointment_changes)

    travel_changes: dict[str, Any] = {
        "DTSTART": timeline.leave_home,
        "DTEND": timeline.target_arrival,
        "DESCRIPTION": _replace_block(
            _description(bundle.travel.component), _travel_description(schedule)
        ),
    }
    if route_url_was_explicit:
        travel_changes["URL"] = schedule.route_url
    travel = mutate.patch_event(bundle.travel.raw, travel_changes)
    preparation = mutate.patch_event(
        bundle.preparation.raw,
        {
            "DTSTART": timeline.preparation_start,
            "DTEND": timeline.leave_home,
        },
    )
    return (
        _step(
            action="cal.update",
            calendar_href=bundle.appointment.reference.calendar_href,
            href=bundle.appointment.reference.href,
            etag=bundle.appointment.etag,
            role="appointment",
            summary=schedule.summary,
            uid=bundle.appointment.reference.uid,
            start=timeline.appointment_start,
            end=timeline.appointment_end,
            payload=appointment,
        ),
        _step(
            action="cal.update",
            calendar_href=bundle.travel.reference.calendar_href,
            href=bundle.travel.reference.href,
            etag=bundle.travel.etag,
            role="travel",
            summary=bundle.travel.reference.summary,
            uid=bundle.travel.reference.uid,
            start=timeline.leave_home,
            end=timeline.target_arrival,
            payload=travel,
            check_url=route_url_was_explicit,
            url=schedule.route_url or "",
        ),
        _step(
            action="cal.update",
            calendar_href=bundle.preparation.reference.calendar_href,
            href=bundle.preparation.reference.href,
            etag=bundle.preparation.etag,
            role="preparation",
            summary=bundle.preparation.reference.summary,
            uid=bundle.preparation.reference.uid,
            start=timeline.preparation_start,
            end=timeline.leave_home,
            payload=preparation,
        ),
    )


def plan_update(
    profile: Any,
    *,
    session: Session,
    appointment_href: str,
    travel_href: str,
    preparation_href: str,
    start: dt.datetime,
    end: dt.datetime,
    origin: str,
    destination: str,
    mode: str,
    route_estimate: str | dt.timedelta,
    on_site_buffer: str | dt.timedelta,
    stop_wait_margin: str | dt.timedelta,
    preparation_duration: str | dt.timedelta,
    summary: str | None = None,
    description: str | None = None,
    route_url: str | object | None = PRESERVE_ROUTE_URL,
    clear_route_url: bool = False,
) -> plans.Plan:
    """Freeze a reschedule against exactly the three named event resources."""
    if clear_route_url and route_url is not PRESERVE_ROUTE_URL and route_url is not None:
        raise _usage("--clear-route-url cannot be combined with --route-url")
    timeline = calculate_timeline(
        start=start,
        end=end,
        route_estimate=route_estimate,
        on_site_buffer=on_site_buffer,
        stop_wait_margin=stop_wait_margin,
        preparation_duration=preparation_duration,
    )
    if summary is not None:
        _text(summary, "appointment summary")
    if description is not None:
        _text(description, "appointment description", allow_empty=True, one_line=False)
    explicit_url = route_url is not PRESERVE_ROUTE_URL or clear_route_url
    requested_url = None if clear_route_url else route_url
    if requested_url is not PRESERVE_ROUTE_URL:
        requested_url = _optional_url(requested_url)
    bundle = _load_bundle(
        profile,
        session=session,
        appointment_href=appointment_href,
        travel_href=travel_href,
        preparation_href=preparation_href,
    )
    next_summary = bundle.appointment.reference.summary if summary is None else summary
    next_description = (
        _description(bundle.appointment.component) if description is None else description
    )
    next_url = bundle.route_url if requested_url is PRESERVE_ROUTE_URL else requested_url
    schedule = _Schedule(
        summary=_text(next_summary, "appointment summary"),
        description=next_description,
        origin=_text(origin, "origin"),
        destination=_text(destination, "destination address"),
        mode=_text(mode, "travel mode"),
        timeline=timeline,
        route_url=next_url,
    )
    return plans.write_bundle(
        profile=profile,
        summary=schedule.summary,
        steps=_update_steps(
            bundle,
            schedule,
            summary=summary,
            description=description,
            route_url_was_explicit=explicit_url,
        ),
    )
