"""Validated recurrence sets, occurrence discovery, and targeted writes.

The ordinary calendar writer intentionally refuses recurrence.  This module is
the narrower exception: it validates one direct recurring master and its
direct exceptions, expands only a bounded window, and rewrites only the
components a target names.  The resource is spliced as bytes so an exception,
timezone, alarm, unknown property, and line ending that is not part of the
requested change remains in the frozen request exactly as it was read.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets as token_source
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr

from . import events, exits, plans, profiles
from .caldav import CalendarError, _canonical
from .session import Session

RECURRENCE_PROPERTIES = frozenset({"RRULE", "RDATE", "EXDATE", "EXRULE"})
RECURRENCE_MARKERS = RECURRENCE_PROPERTIES | frozenset({"RECURRENCE-ID"})
SCHEDULING_PROPERTIES = frozenset({"ORGANIZER", "ATTENDEE"})
TARGETS = frozenset({"series", "occurrence", "this-and-future"})
MAX_EXPANSIONS = 10_000
SUPPORTED_COMPONENTS = frozenset(
    {"VCALENDAR", "VTIMEZONE", "STANDARD", "DAYLIGHT", "VEVENT", "VALARM"}
)
_DURATION_RE = re.compile(
    r"(?P<sign>[+-])?P(?:(?P<weeks>\d+)W|(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?)"
)


class RecurrenceError(events.EventError):
    """A recurrence resource or target could not be proven safe."""


@dataclass(frozen=True)
class WireId:
    """A typed recurrence identity in the CLI's reusable wire form."""

    text: str
    kind: str
    tzid: str | None
    value: dt.date | dt.datetime

    @property
    def key(self) -> tuple[str, str | None, str]:
        if self.kind == "DATE":
            assert isinstance(self.value, dt.date)
            return (self.kind, self.tzid, self.value.strftime("%Y%m%d"))
        assert isinstance(self.value, dt.datetime)
        if self.tzid is None:
            value = self.value.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        else:
            value = self.value.strftime("%Y%m%dT%H%M%S")
        return (self.kind, self.tzid, value)


@dataclass(frozen=True)
class _ComponentSpan:
    name: str
    start: int
    end_start: int
    end: int
    parent: str | None


@dataclass(frozen=True)
class _RawProperty:
    name: str
    start: int
    end: int


@dataclass(frozen=True)
class _SeriesModel:
    start: WireId
    rrule: str | None
    rdates: tuple[WireId, ...]
    exdates: tuple[WireId, ...]

    @property
    def recurring(self) -> bool:
        return bool(self.rrule or self.rdates or self.exdates)


@dataclass(frozen=True)
class _DurationModel:
    """Describe the recurrence duration without losing its RFC 5545 kind."""

    kind: str
    exact: dt.timedelta | None = None
    nominal: relativedelta | None = None

    def end(self, start: dt.date | dt.datetime) -> dt.date | dt.datetime:
        if self.kind == "exact":
            assert self.exact is not None
            if isinstance(start, dt.datetime):
                instant = _as_instant(start) + self.exact
                return instant.astimezone(start.tzinfo)
            return start + self.exact
        assert self.nominal is not None
        return start + self.nominal


@dataclass(frozen=True)
class Resource:
    """One validated recurring resource and its exact direct components."""

    raw: bytes
    calendar_href: str
    href: str
    etag: str
    calendar: icalendar.Calendar
    master: Any
    overrides: tuple[Any, ...]
    master_raw: bytes
    override_raw: tuple[bytes, ...]
    model: _SeriesModel
    override_by_key: Mapping[tuple[str, str | None, str], tuple[WireId, Any, bytes]]


@dataclass(frozen=True)
class Occurrence:
    """One logical recurrence instance returned by bounded discovery."""

    calendar_href: str
    href: str
    etag: str
    uid: str
    source: str
    recurrence_id: str
    original_start: str
    effective_start: str
    effective_end: str
    cancelled: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "calendar_href": self.calendar_href,
            "href": self.href,
            "etag": self.etag,
            "uid": self.uid,
            "source": self.source,
            "recurrence_id": self.recurrence_id,
            "original_start": self.original_start,
            "effective_start": self.effective_start,
            "effective_end": self.effective_end,
            "cancelled": self.cancelled,
        }


def _error(message: str, code: int = exits.UNSUPPORTED_STRUCTURE) -> RecurrenceError:
    return RecurrenceError(message, code)


def _zone(tzid: str, *, label: str, code: int = exits.UNSUPPORTED_STRUCTURE) -> ZoneInfo:
    try:
        return ZoneInfo(tzid)
    except ZoneInfoNotFoundError as exc:
        raise _error(f"{label} uses an unknown TZID {tzid!r}", code) from exc


def _validate_local_time(local: dt.datetime, zone: ZoneInfo, *, label: str) -> None:
    """Reject a local time that maps to zero or two instants in its TZID."""
    instants: set[dt.datetime] = set()
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(dt.UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) == local:
            instants.add(candidate.astimezone(dt.UTC))
    if len(instants) == 0:
        raise _error(f"{label} is a nonexistent local time in TZID {zone.key}")
    if len(instants) > 1:
        raise _error(f"{label} is an ambiguous local time in TZID {zone.key}")


def _property_params(value: Any) -> dict[str, str]:
    return {str(key).upper(): str(item) for key, item in value.params.items()}


def _boundary_signature(value: Any, *, label: str) -> tuple[str, str | None]:
    """Validate one DTSTART/DTEND value and return its kind and TZID."""
    try:
        typed = getattr(value, "dt", None)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _error(f"{label} has an invalid value", exits.MALFORMED_RESPONSE) from exc
    if typed is None or isinstance(typed, tuple):
        raise _error(f"{label} has an invalid value", exits.UNSUPPORTED_STRUCTURE)
    params = _property_params(value)
    value_param = params.get("VALUE")
    tzid = params.get("TZID")
    if isinstance(typed, dt.date) and not isinstance(typed, dt.datetime):
        if value_param != "DATE" or tzid is not None:
            raise _error(f"{label} has incompatible DATE parameters")
        return "DATE", None
    if not isinstance(typed, dt.datetime):
        raise _error(f"{label} has an invalid value", exits.UNSUPPORTED_STRUCTURE)
    if value_param not in {None, "DATE-TIME"}:
        raise _error(f"{label} has an unsupported DATE-TIME value kind")
    if tzid is not None:
        zone = _zone(tzid, label=label)
        local = typed.astimezone(zone).replace(tzinfo=None)
        _validate_local_time(local, zone, label=label)
        return "DATE-TIME", tzid
    if typed.tzinfo is None or typed.utcoffset() != dt.timedelta(0):
        raise _error(f"{label} has a floating or non-UTC DATE-TIME")
    try:
        wire = value.to_ical().decode("utf-8")
    except (AttributeError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise _error(f"{label} has no reusable wire value", exits.MALFORMED_RESPONSE) from exc
    if not wire.endswith("Z"):
        raise _error(f"{label} has a non-UTC DATE-TIME without a TZID")
    return "DATE-TIME", None


def _boundary_semantics(component: Any, *, label: str) -> tuple[str, str | None]:
    start = component.get("DTSTART")
    if start is None:
        raise _error(f"{label} has no DTSTART", exits.MALFORMED_RESPONSE)
    semantics = _boundary_signature(start, label=f"{label} DTSTART")
    end = component.get("DTEND")
    if end is not None and _boundary_signature(end, label=f"{label} DTEND") != semantics:
        raise _error(f"{label} has incompatible DTSTART and DTEND boundaries")
    return semantics


def _validate_component_tree(raw: bytes) -> None:
    """Reject components outside the supported VCALENDAR nesting."""
    spans = _component_spans(raw)
    for span in spans:
        if span.name not in SUPPORTED_COMPONENTS:
            raise _error(
                f"the resource carries unsupported {span.name} structure",
                exits.UNSUPPORTED_STRUCTURE,
            )
        expected_parent = {
            "VEVENT": "VCALENDAR",
            "VTIMEZONE": "VCALENDAR",
            "VALARM": "VEVENT",
            "STANDARD": "VTIMEZONE",
            "DAYLIGHT": "VTIMEZONE",
        }.get(span.name)
        if expected_parent is not None and span.parent != expected_parent:
            raise _error(
                f"the {span.name} component is nested under an unsupported parent",
                exits.UNSUPPORTED_STRUCTURE,
            )
        if span.name == "VCALENDAR" and span.parent is not None:
            raise _error("the VCALENDAR component is nested", exits.UNSUPPORTED_STRUCTURE)


def _physical_lines(raw: bytes) -> list[tuple[int, int, bytes]]:
    lines: list[tuple[int, int, bytes]] = []
    offset = 0
    while offset < len(raw):
        newline = raw.find(b"\n", offset)
        end = len(raw) if newline < 0 else newline + 1
        lines.append((offset, end, raw[offset:end]))
        offset = end
    return lines


def _line_content(line: bytes) -> bytes:
    return line.rstrip(b"\r\n")


def _structural(content: bytes) -> tuple[str, str] | None:
    upper = content.upper()
    if upper.startswith(b"BEGIN:"):
        return "BEGIN", upper[6:].decode("utf-8", "replace")
    if upper.startswith(b"END:"):
        return "END", upper[4:].decode("utf-8", "replace")
    return None


def _component_spans(raw: bytes) -> tuple[_ComponentSpan, ...]:
    stack: list[tuple[str, int, str | None]] = []
    spans: list[_ComponentSpan] = []
    for start, end, line in _physical_lines(raw):
        marker = _structural(_line_content(line))
        if marker is None:
            continue
        kind, name = marker
        if kind == "BEGIN":
            parent = stack[-1][0] if stack else None
            stack.append((name, start, parent))
            continue
        if not stack or stack[-1][0] != name:
            raise _error("the iCalendar component nesting is malformed", exits.MALFORMED_RESPONSE)
        component, component_start, parent = stack.pop()
        spans.append(_ComponentSpan(component, component_start, start, end, parent))
    if stack:
        raise _error("the iCalendar component nesting is incomplete", exits.MALFORMED_RESPONSE)
    return tuple(sorted(spans, key=lambda item: item.start))


def _line_ending(raw: bytes) -> bytes:
    for _, _, line in _physical_lines(raw):
        if line.endswith(b"\r\n"):
            return b"\r\n"
        if line.endswith(b"\n"):
            return b"\n"
        if line.endswith(b"\r"):
            return b"\r"
    return b"\r\n"


def _normalize_line_endings(raw: bytes, ending: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", ending)


def _direct_spans(raw: bytes, name: str) -> tuple[_ComponentSpan, ...]:
    return tuple(
        span for span in _component_spans(raw) if span.name == name and span.parent == "VCALENDAR"
    )


def _property_items(component: Any, name: str) -> list[Any]:
    wanted = name.upper()
    return [value for key, value in component.property_items() if key.upper() == wanted]


def _property_names(component: Any) -> set[str]:
    return {key.upper() for key, _ in component.property_items()}


def _parse_calendar(raw: bytes) -> icalendar.Calendar:
    try:
        parsed = icalendar.Calendar.from_ical(raw)
    except (ValueError, IndexError, TypeError) as exc:
        raise _error(
            "the recurring resource was not valid iCalendar", exits.MALFORMED_RESPONSE
        ) from exc
    if parsed.name != "VCALENDAR":
        raise _error("the recurring resource was not a VCALENDAR", exits.MALFORMED_RESPONSE)
    return parsed


def _parse_event(raw: bytes) -> Any:
    try:
        parsed = icalendar.Event.from_ical(raw)
    except (ValueError, IndexError, TypeError) as exc:
        raise _error(
            "the VEVENT component was not valid iCalendar", exits.MALFORMED_RESPONSE
        ) from exc
    if parsed.name != "VEVENT":
        raise _error("the component was not a VEVENT", exits.MALFORMED_RESPONSE)
    return parsed


def _direct_events(raw: bytes, parsed: icalendar.Calendar) -> tuple[tuple[Any, bytes], ...]:
    direct = [item for item in parsed.subcomponents if item.name == "VEVENT"]
    all_events = [item for item in parsed.walk() if item.name == "VEVENT"]
    if len(all_events) != len(direct):
        raise _error("nested VEVENT components are not supported", exits.UNSUPPORTED_STRUCTURE)
    direct_spans = _direct_spans(raw, "VEVENT")
    if len(direct) != len(direct_spans):
        raise _error(
            "the direct VEVENT structure could not be mapped to raw bytes",
            exits.MALFORMED_RESPONSE,
        )
    return tuple(
        (component, raw[span.start : span.end])
        for component, span in zip(direct, direct_spans, strict=True)
    )


def _typed_wire(value: Any, params: Mapping[str, Any], *, label: str) -> WireId:
    try:
        typed = getattr(value, "dt", None)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _error(f"{label} has an invalid value", exits.MALFORMED_RESPONSE) from exc
    if isinstance(typed, tuple) or typed is None:
        raise _error(f"{label} has a period or invalid value", exits.UNSUPPORTED_STRUCTURE)
    value_kind = (
        "DATE"
        if isinstance(typed, dt.date) and not isinstance(typed, dt.datetime)
        else "DATE-TIME"
    )
    raw_params = {str(key).upper(): str(item) for key, item in params.items()}
    if "RANGE" in raw_params:
        raise _error("RANGE=THISANDFUTURE recurrence identities are not supported")
    value_param = raw_params.get("VALUE")
    tzid = raw_params.get("TZID")
    if value_kind == "DATE":
        if value_param != "DATE" or tzid is not None:
            raise _error(f"{label} has incompatible DATE parameters")
        return WireId(f"VALUE=DATE:{typed.strftime('%Y%m%d')}", value_kind, None, typed)
    if value_param not in {None, "DATE-TIME"}:
        raise _error(f"{label} has an unsupported DATE-TIME value kind")
    if not isinstance(typed, dt.datetime) or typed.tzinfo is None:
        raise _error(f"{label} has a floating DATE-TIME that cannot be targeted")
    try:
        wire = value.to_ical().decode("utf-8")
    except (AttributeError, UnicodeDecodeError, TypeError, ValueError) as exc:
        raise _error(f"{label} has no reusable wire value", exits.MALFORMED_RESPONSE) from exc
    if tzid is not None:
        if wire.endswith("Z"):
            raise _error(f"{label} has a TZID but a UTC wire value")
        zone = _zone(tzid, label=label)
        local = typed.astimezone(zone).replace(tzinfo=None)
        _validate_local_time(local, zone, label=label)
        return WireId(
            f"TZID={tzid}:{local.strftime('%Y%m%dT%H%M%S')}",
            value_kind,
            tzid,
            local.replace(tzinfo=zone),
        )
    if not wire.endswith("Z") or typed.utcoffset() != dt.timedelta(0):
        raise _error(f"{label} has a non-UTC DATE-TIME without a TZID")
    return WireId(wire, value_kind, None, typed.astimezone(dt.UTC))


def parse_wire_id(text: str) -> WireId:
    """Parse the exact identity forms emitted by :func:`occurrences`."""
    if not isinstance(text, str) or text != text.strip() or not text:
        raise _error("--recurrence-id must be a reusable wire identity", exits.USAGE)
    date_match = re.fullmatch(r"VALUE=DATE:(\d{8})", text, re.IGNORECASE)
    if date_match:
        try:
            value = dt.datetime.strptime(date_match.group(1), "%Y%m%d").date()
        except ValueError as exc:
            raise _error("--recurrence-id has an invalid DATE", exits.USAGE) from exc
        return WireId(f"VALUE=DATE:{value.strftime('%Y%m%d')}", "DATE", None, value)
    tz_match = re.fullmatch(r"TZID=([^:]+):(\d{8}T\d{6})", text, re.IGNORECASE)
    if tz_match:
        tzid = tz_match.group(1)
        try:
            zone = _zone(tzid, label="--recurrence-id", code=exits.USAGE)
            local = dt.datetime.strptime(tz_match.group(2), "%Y%m%dT%H%M%S")
        except ValueError as exc:
            raise _error("--recurrence-id has an invalid TZID DATE-TIME", exits.USAGE) from exc
        _validate_local_time(local, zone, label="--recurrence-id")
        return WireId(
            f"TZID={tzid}:{local.strftime('%Y%m%dT%H%M%S')}",
            "DATE-TIME",
            tzid,
            local.replace(tzinfo=zone),
        )
    if re.fullmatch(r"\d{8}T\d{6}Z", text, re.IGNORECASE):
        try:
            value = dt.datetime.strptime(text.upper(), "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
        except ValueError as exc:
            raise _error("--recurrence-id has an invalid UTC DATE-TIME", exits.USAGE) from exc
        return WireId(value.strftime("%Y%m%dT%H%M%SZ"), "DATE-TIME", None, value)
    raise _error(
        "--recurrence-id must be UTC DATE-TIME, TZID=Zone:local-value, or VALUE=DATE:value",
        exits.USAGE,
    )


def _check_same_identity_kind(left: WireId, right: WireId, *, label: str) -> None:
    if left.kind != right.kind or left.tzid != right.tzid:
        raise _error(f"{label} does not match the master's DATE/DATE-TIME and timezone semantics")


def _rdate_items(value: Any, *, label: str) -> list[Any]:
    items = getattr(value, "dts", None)
    if items is None:
        item = getattr(value, "dt", None)
        if item is None:
            raise _error(f"{label} has no values", exits.MALFORMED_RESPONSE)
        items = [item]
    result: list[Any] = []
    for item in items:
        if isinstance(getattr(item, "dt", item), tuple):
            raise _error(f"{label} uses a period-valued recurrence date")
        result.append(item)
    return result


def _rrule_text(master: Any) -> str | None:
    values = _property_items(master, "RRULE")
    if len(values) > 1:
        raise _error("multiple RRULE properties cannot be mapped safely")
    if not values:
        return None
    return values[0].to_ical().decode("utf-8")


def _series_model(master: Any) -> _SeriesModel:
    names = _property_names(master)
    if "EXRULE" in names:
        raise _error("EXRULE recurrence is not supported")
    start_prop = master.get("DTSTART")
    if start_prop is None:
        raise _error("the recurring master has no DTSTART", exits.MALFORMED_RESPONSE)
    start = _typed_wire(start_prop, start_prop.params, label="DTSTART")
    rrule = _rrule_text(master)
    rdates: list[WireId] = []
    for index, prop in enumerate(_property_items(master, "RDATE")):
        for item in _rdate_items(prop, label=f"RDATE {index + 1}"):
            wire = _typed_wire(item, prop.params, label="RDATE")
            _check_same_identity_kind(start, wire, label="RDATE")
            rdates.append(wire)
    exdates: list[WireId] = []
    for index, prop in enumerate(_property_items(master, "EXDATE")):
        for item in _rdate_items(prop, label=f"EXDATE {index + 1}"):
            wire = _typed_wire(item, prop.params, label="EXDATE")
            _check_same_identity_kind(start, wire, label="EXDATE")
            exdates.append(wire)
    if rrule is None and not rdates and not exdates:
        raise _error("the resource is not recurring", exits.UNSUPPORTED_STRUCTURE)
    if rrule is not None:
        try:
            parts = dict(_rrule_parts(rrule))
            if {"COUNT", "UNTIL"} <= parts.keys():
                raise _error("an RRULE cannot contain both COUNT and UNTIL")
            if "COUNT" in parts and (
                not parts["COUNT"].isdigit() or int(parts["COUNT"]) <= 0
            ):
                raise _error("the RRULE COUNT must be a positive integer")
            if "INTERVAL" in parts and (
                not parts["INTERVAL"].isdigit() or int(parts["INTERVAL"]) <= 0
            ):
                raise _error("the RRULE INTERVAL must be a positive integer")
            _validate_rrule_types(start, parts)
            rrulestr(f"RRULE:{rrule}", dtstart=_rule_datetime(start))
        except (TypeError, ValueError, OverflowError) as exc:
            raise _error("the RRULE could not be expanded safely") from exc
    return _SeriesModel(start, rrule, tuple(rdates), tuple(exdates))


def _rule_datetime(wire: WireId) -> dt.datetime:
    if isinstance(wire.value, dt.datetime):
        return wire.value
    return dt.datetime.combine(wire.value, dt.time.min, tzinfo=dt.UTC)


def _wire_from_rule_value(value: dt.datetime, model: _SeriesModel) -> WireId:
    if model.start.kind == "DATE":
        return WireId(
            f"VALUE=DATE:{value.date().strftime('%Y%m%d')}",
            "DATE",
            None,
            value.date(),
        )
    if model.start.tzid is None:
        value = value.astimezone(dt.UTC)
        return WireId(value.strftime("%Y%m%dT%H%M%SZ"), "DATE-TIME", None, value)
    zone = ZoneInfo(model.start.tzid)
    local = value.astimezone(zone)
    _validate_local_time(local.replace(tzinfo=None), zone, label="an RRULE occurrence")
    return WireId(
        f"TZID={model.start.tzid}:{local.strftime('%Y%m%dT%H%M%S')}",
        "DATE-TIME",
        model.start.tzid,
        local,
    )


def _rule_object(model: _SeriesModel) -> Any | None:
    if model.rrule is None:
        return None
    try:
        return rrulestr(f"RRULE:{model.rrule}", dtstart=_rule_datetime(model.start))
    except (TypeError, ValueError, OverflowError) as exc:
        raise _error("the RRULE could not be expanded safely") from exc


def _rule_between(
    model: _SeriesModel, start: WireId | None, end: WireId | None
) -> tuple[WireId, ...]:
    rule = _rule_object(model)
    if rule is None:
        return (model.start,)
    first = _rule_datetime(start) if start is not None else _rule_datetime(model.start)
    last = _rule_datetime(end) if end is not None else dt.datetime.max.replace(tzinfo=dt.UTC)
    if first.tzinfo is None or last.tzinfo is None:
        raise _error("the RRULE window is not timezone-aware")
    try:
        values = rule.between(first, last, inc=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _error("the RRULE could not be expanded in the requested window") from exc
    if len(values) > MAX_EXPANSIONS:
        raise _error(
            f"the recurrence window expands to more than {MAX_EXPANSIONS} instances; narrow it"
        )
    return tuple(_wire_from_rule_value(value, model) for value in values)


def _identity_exists(model: _SeriesModel, target: WireId) -> bool:
    _check_same_identity_kind(model.start, target, label="the recurrence target")
    if target.key in {item.key for item in model.exdates}:
        return False
    if target.key == model.start.key or target.key in {item.key for item in model.rdates}:
        return True
    if model.rrule is None:
        return False
    return target.key in {item.key for item in _rule_between(model, model.start, target)}


def _window_wire(value: dt.datetime, model: _SeriesModel) -> WireId:
    if model.start.kind == "DATE":
        return WireId(
            f"VALUE=DATE:{value.date().strftime('%Y%m%d')}",
            "DATE",
            None,
            value.date(),
        )
    if model.start.tzid is None:
        instant = value.astimezone(dt.UTC)
        return WireId(instant.strftime("%Y%m%dT%H%M%SZ"), "DATE-TIME", None, instant)
    local = value.astimezone(ZoneInfo(model.start.tzid))
    return WireId(
        f"TZID={model.start.tzid}:{local.strftime('%Y%m%dT%H%M%S')}",
        "DATE-TIME",
        model.start.tzid,
        local,
    )


def _as_instant(value: dt.date | dt.datetime) -> dt.datetime:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            raise _error("a recurrence boundary needs a timezone offset", exits.USAGE)
        return value.astimezone(dt.UTC)
    return dt.datetime.combine(value, dt.time.min, tzinfo=dt.UTC)


def _nominal_duration(value: Any, *, label: str) -> relativedelta:
    try:
        text = value.to_ical().decode("ascii")
    except (AttributeError, UnicodeDecodeError, TypeError, ValueError) as exc:
        raise _error(f"{label} has no usable duration", exits.MALFORMED_RESPONSE) from exc
    match = _DURATION_RE.fullmatch(text)
    if match is None:
        raise _error(f"{label} is not a supported RFC 5545 duration")
    numbers = {
        name: int(match.group(name) or 0)
        for name in ("weeks", "days", "hours", "minutes", "seconds")
    }
    if not any(numbers.values()):
        raise _error(f"{label} is not a supported RFC 5545 duration")
    sign = -1 if match.group("sign") == "-" else 1
    return relativedelta(
        weeks=sign * numbers["weeks"],
        days=sign * numbers["days"],
        hours=sign * numbers["hours"],
        minutes=sign * numbers["minutes"],
        seconds=sign * numbers["seconds"],
    )


def _duration_model(component: Any) -> _DurationModel:
    start, end = events._event_bounds(component, href="", code=exits.MALFORMED_RESPONSE)
    duration = component.get("DURATION")
    if duration is not None:
        return _DurationModel(
            "nominal",
            nominal=_nominal_duration(duration, label="DURATION"),
        )
    explicit_end = component.get("DTEND")
    if explicit_end is not None:
        return _DurationModel(
            "exact",
            exact=_as_instant(end) - _as_instant(start),
        )
    if isinstance(start, dt.date) and not isinstance(start, dt.datetime):
        return _DurationModel("nominal", nominal=relativedelta(days=1))
    return _DurationModel("exact", exact=dt.timedelta(0))


def _component_effective_end(component: Any) -> dt.date | dt.datetime:
    start, end = events._event_bounds(component, href="")
    if component.get("DTEND") is not None:
        assert end is not None
        return end
    return _duration_model(component).end(start)


def _duration(master: Any) -> dt.timedelta:
    start, _ = events._event_bounds(master, href="", code=exits.MALFORMED_RESPONSE)
    end = _duration_model(master).end(start)
    return _as_instant(end) - _as_instant(start)


def _occurrence_component(resource: Resource, wire: WireId) -> tuple[str, Any, bytes]:
    found = resource.override_by_key.get(wire.key)
    if found is not None:
        return "override", found[1], found[2]
    return "master", resource.master, resource.master_raw


def _occurrence(resource: Resource, wire: WireId) -> Occurrence:
    source, component, _ = _occurrence_component(resource, wire)
    if source == "master":
        start = wire.value
        end = _duration_model(resource.master).end(start)
    else:
        start, _ = events._event_bounds(component, href=resource.href)
        end = _component_effective_end(component)
    return Occurrence(
        calendar_href=resource.calendar_href,
        href=resource.href,
        etag=resource.etag,
        uid=str(component.get("UID")),
        source=source,
        recurrence_id=wire.text,
        original_start=events._utc(wire.value),
        effective_start=events._utc(start),
        effective_end=events._utc(end) if end is not None else "",
        cancelled=str(component.get("STATUS", "")).upper() == "CANCELLED",
    )


def _resource_occurrences(
    resource: Resource, start: dt.datetime, end: dt.datetime
) -> list[Occurrence]:
    if end <= start:
        raise _error("the occurrence window ends before it starts", exits.USAGE)
    window_end = _window_wire(end, resource.model)
    lookback = _duration(resource.master)
    widened_start = start - max(lookback, dt.timedelta(days=1))
    widened = _window_wire(widened_start, resource.model)
    candidates = [resource.model.start]
    candidates.extend(_rule_between(resource.model, widened, window_end))
    candidates.extend(resource.model.rdates)
    candidates.extend(item[0] for item in resource.override_by_key.values())
    if len(candidates) > MAX_EXPANSIONS:
        raise _error(
            f"the recurrence window expands to more than {MAX_EXPANSIONS} instances; narrow it"
        )
    excluded = {item.key for item in resource.model.exdates}
    unique: dict[tuple[str, str | None, str], WireId] = {}
    for wire in candidates:
        if wire.key not in excluded:
            unique.setdefault(wire.key, wire)
    result: list[Occurrence] = []
    for key in sorted(unique, key=lambda item: (item[2], item[1] or "")):
        wire = unique[key]
        override = resource.override_by_key.get(key)
        if override is not None:
            wire = override[0]
        occurrence = _occurrence(resource, wire)
        component = _occurrence_component(resource, wire)[1]
        if override is None:
            effective_start = wire.value
            effective_end = _duration_model(resource.master).end(effective_start)
        else:
            effective_start, _ = events._event_bounds(component, href=resource.href)
            effective_end = _component_effective_end(component)
        if _as_instant(effective_end or effective_start) <= _as_instant(start):
            continue
        if _as_instant(effective_start) >= _as_instant(end):
            continue
        result.append(occurrence)
    return result


def _validate_resource(raw: bytes, *, calendar_href: str, href: str, etag: str) -> Resource:
    parsed = _parse_calendar(raw)
    _validate_component_tree(raw)
    direct = _direct_events(raw, parsed)
    if not direct:
        raise _error("the resource holds no direct VEVENT", exits.MALFORMED_RESPONSE)
    for component in parsed.subcomponents:
        if component.name not in {"VEVENT", "VTIMEZONE"}:
            raise _error(
                f"the resource carries unsupported {component.name} structure",
                exits.UNSUPPORTED_STRUCTURE,
            )
    masters = [
        (component, data)
        for component, data in direct
        if "RECURRENCE-ID" not in _property_names(component)
    ]
    overrides = [
        (component, data)
        for component, data in direct
        if "RECURRENCE-ID" in _property_names(component)
    ]
    if len(masters) != 1:
        raise _error(
            "the resource must contain exactly one direct recurrence master",
            exits.MALFORMED_RESPONSE,
        )
    master, master_raw = masters[0]
    master_boundaries = _boundary_semantics(master, label="the recurrence master")
    model = _series_model(master)
    master_uid = str(master.get("UID", "")).strip()
    if not master_uid:
        raise _error("the recurrence master has no UID", exits.MALFORMED_RESPONSE)
    events._event_bounds(master, href=href, code=exits.MALFORMED_RESPONSE)
    override_by_key: dict[tuple[str, str | None, str], tuple[WireId, Any, bytes]] = {}
    override_components: list[Any] = []
    override_raw: list[bytes] = []
    for component, data in overrides:
        names = _property_names(component)
        if names & SCHEDULING_PROPERTIES:
            raise _error(
                "scheduled recurrence resources remain unwritable",
                exits.UNSUPPORTED_STRUCTURE,
            )
        if names & RECURRENCE_PROPERTIES:
            raise _error("recurrence-set properties belong only on the direct master")
        if _boundary_semantics(component, label="the recurrence override") != master_boundaries:
            raise _error(
                "a recurrence override has incompatible DATE/DATE-TIME or timezone boundaries"
            )
        if str(component.get("UID", "")).strip() != master_uid:
            raise _error("a recurrence override has an orphaned UID")
        identities = _property_items(component, "RECURRENCE-ID")
        if len(identities) != 1:
            raise _error("a recurrence override must have exactly one RECURRENCE-ID")
        identity = _typed_wire(identities[0], identities[0].params, label="RECURRENCE-ID")
        _check_same_identity_kind(model.start, identity, label="RECURRENCE-ID")
        if identity.key in override_by_key:
            raise _error("duplicate RECURRENCE-ID values are not safe to target")
        if not _identity_exists(model, identity):
            raise _error("a recurrence override does not map to the master's recurrence set")
        events._event_bounds(component, href=href, code=exits.MALFORMED_RESPONSE)
        override_by_key[identity.key] = (identity, component, data)
        override_components.append(component)
        override_raw.append(data)
    names = set().union(*(_property_names(component) for component, _ in direct))
    if names & SCHEDULING_PROPERTIES:
        raise _error(
            "scheduled recurrence resources remain unwritable",
            exits.UNSUPPORTED_STRUCTURE,
        )
    if not model.recurring:
        raise _error("the resource is not recurring", exits.UNSUPPORTED_STRUCTURE)
    return Resource(
        raw=raw,
        calendar_href=calendar_href,
        href=href,
        etag=etag,
        calendar=parsed,
        master=master,
        overrides=tuple(override_components),
        master_raw=master_raw,
        override_raw=tuple(override_raw),
        model=model,
        override_by_key=override_by_key,
    )


def _raw_event_properties(raw: bytes) -> tuple[_RawProperty, int]:
    spans = _component_spans(raw)
    roots = [item for item in spans if item.name == "VEVENT" and item.parent is None]
    if len(roots) != 1:
        raise _error(
            "a component splice did not contain exactly one VEVENT",
            exits.MALFORMED_RESPONSE,
        )
    root = roots[0]
    properties: list[_RawProperty] = []
    stack: list[str] = []
    current_name: str | None = None
    current_start = 0
    current_end = 0

    def flush() -> None:
        nonlocal current_name, current_start, current_end
        if current_name is not None:
            properties.append(_RawProperty(current_name, current_start, current_end))
        current_name = None

    for start, end, line in _physical_lines(raw[root.start : root.end]):
        absolute_start = root.start + start
        absolute_end = root.start + end
        content = _line_content(line)
        marker = _structural(content)
        if marker is not None:
            flush()
            kind, name = marker
            if kind == "BEGIN":
                stack.append(name)
            else:
                if not stack or stack[-1] != name:
                    raise _error(
                        "the component splice nesting is malformed",
                        exits.MALFORMED_RESPONSE,
                    )
                stack.pop()
            continue
        if len(stack) != 1 or stack[-1] != "VEVENT":
            continue
        if content.startswith((b" ", b"\t")):
            if current_name is not None:
                current_end = absolute_end
            continue
        colon = content.find(b":")
        if colon <= 0:
            continue
        flush()
        current_name = content[:colon].split(b";", 1)[0].decode("utf-8", "replace").upper()
        current_start = absolute_start
        current_end = absolute_end
    flush()
    return tuple(properties), root.end_start


def _event_property_bytes(name: str, value: Any) -> bytes:
    event = icalendar.Event()
    event.add(name.lower(), value)
    data = event.to_ical()
    lines = _physical_lines(data)
    properties = [line for _, _, line in lines if _structural(_line_content(line)) is None]
    return b"".join(properties)


def _alarm_bytes(triggers: Iterable[str]) -> tuple[bytes, ...]:
    result: list[bytes] = []
    for trigger in triggers:
        alarm = icalendar.Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", "Reminder")
        try:
            alarm.add("trigger", icalendar.prop.vDuration.from_ical(trigger))
        except (TypeError, ValueError) as exc:
            raise _error(f"{trigger!r} is not an RFC 5545 duration", exits.USAGE) from exc
        result.append(alarm.to_ical())
    return tuple(result)


def _splice_event(
    raw: bytes,
    replacements: Mapping[str, bytes | tuple[bytes, ...] | None],
    *,
    remove_components: Iterable[str] = (),
) -> bytes:
    properties, end_start = _raw_event_properties(raw)
    normalized = {name.upper(): value for name, value in replacements.items()}
    edits: list[tuple[int, int, bytes]] = []
    for name, value in normalized.items():
        matching = [item for item in properties if item.name == name]
        if value is None:
            generated = b""
        elif isinstance(value, tuple):
            generated = b"".join(value)
        else:
            generated = value
        if matching:
            edits.append((matching[0].start, matching[0].end, generated))
            edits.extend((item.start, item.end, b"") for item in matching[1:])
        elif generated:
            edits.append((end_start, end_start, generated))

    component_names = {name.upper() for name in remove_components}
    spans = _component_spans(raw)
    for span in spans:
        if span.parent == "VEVENT" and span.name in component_names:
            edits.append((span.start, span.end, b""))

    if not edits:
        return raw
    edits.sort(key=lambda item: (item[0], item[1]))
    output = bytearray()
    cursor = 0
    for start, end, replacement in edits:
        if start < cursor:
            raise _error("overlapping component splice ranges", exits.MALFORMED_RESPONSE)
        output.extend(raw[cursor:start])
        ending = _line_ending(raw)
        normalized_replacement = _normalize_line_endings(replacement, ending)
        if normalized_replacement and not normalized_replacement.endswith((b"\r", b"\n")):
            normalized_replacement += ending
        output.extend(normalized_replacement)
        cursor = end
    output.extend(raw[cursor:])
    return bytes(output)


def _replace_component(raw: bytes, old: bytes, new: bytes) -> bytes:
    first = raw.find(old)
    if first < 0 or raw.find(old, first + 1) >= 0:
        raise _error(
            "the requested component could not be replaced exactly",
            exits.MALFORMED_RESPONSE,
        )
    return raw[:first] + new + raw[first + len(old) :]


def _rewrite_resource(
    resource: Resource,
    *,
    master_raw: bytes | None = None,
    removed_overrides: Iterable[int] = (),
    appended: Iterable[bytes] = (),
) -> bytes:
    spans = _direct_spans(resource.raw, "VEVENT")
    if len(spans) != 1 + len(resource.override_raw):
        raise _error(
            "the recurring resource components changed while planning",
            exits.MALFORMED_RESPONSE,
        )
    root = next(
        (
            span
            for span in _component_spans(resource.raw)
            if span.name == "VCALENDAR" and span.parent is None
        ),
        None,
    )
    if root is None:
        raise _error("the recurring resource has no VCALENDAR root", exits.MALFORMED_RESPONSE)
    edits: list[tuple[int, int, bytes]] = []
    if master_raw is not None:
        edits.append((spans[0].start, spans[0].end, master_raw))
    removed = set(removed_overrides)
    for index, span in enumerate(spans[1:]):
        if index in removed:
            edits.append((span.start, span.end, b""))
    additions = b"".join(appended)
    if additions:
        edits.append((root.end_start, root.end_start, additions))
    if not edits:
        return resource.raw
    edits.sort(key=lambda item: (item[0], item[1]))
    output = bytearray()
    cursor = 0
    for start, end, replacement in edits:
        if start < cursor:
            raise _error("the resource rewrite ranges overlap", exits.MALFORMED_RESPONSE)
        output.extend(resource.raw[cursor:start])
        output.extend(_normalize_line_endings(replacement, _line_ending(resource.raw)))
        cursor = end
    output.extend(resource.raw[cursor:])
    return bytes(output)


def _boundary(
    value: Any,
    label: str,
    *,
    preserve_tzid: str | None = None,
) -> dt.date | dt.datetime:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            raise _error(f"{label} must carry a timezone offset", exits.USAGE)
        if preserve_tzid is not None:
            try:
                return value.astimezone(ZoneInfo(preserve_tzid))
            except ZoneInfoNotFoundError as exc:
                raise _error(f"{label} uses an unknown TZID {preserve_tzid!r}") from exc
        return value.astimezone(dt.UTC)
    if isinstance(value, dt.date):
        return value
    raise _error(f"{label} is not a date or timezone-aware instant", exits.USAGE)


def _validate_changes(changes: Mapping[str, Any], *, allow_times: bool) -> dict[str, Any]:
    allowed = {
        "SUMMARY", "DESCRIPTION", "LOCATION", "PRIORITY", "CATEGORIES", "STATUS", "TRANSP",
        "URL", "CLASS", "COLOR", "RELATED-TO", "VALARM",
    }
    if allow_times:
        allowed.update({"DTSTART", "DTEND"})
    normalized: dict[str, Any] = {}
    for name, value in changes.items():
        key = name.upper()
        if key not in allowed:
            raise _error(f"recurrence mutations cannot change {key}", exits.UNSUPPORTED_STRUCTURE)
        if (
            key == "PRIORITY"
            and value is not None
            and (isinstance(value, bool) or not isinstance(value, int) or value not in range(1, 10))
        ):
            raise _error("priority must be 1 (highest) to 9 (lowest)", exits.USAGE)
        if (
            key == "STATUS"
            and value is not None
            and str(value).upper() not in {"CONFIRMED", "TENTATIVE", "CANCELLED"}
        ):
            raise _error("status is not valid for a VEVENT", exits.USAGE)
        normalized[key] = value
    return normalized


def _description(component: Any) -> str:
    value = component.get("DESCRIPTION")
    return str(value) if value is not None else ""


def _patch_component(
    raw: bytes,
    changes: Mapping[str, Any],
    *,
    portable_description: bool = False,
    now: dt.datetime | None = None,
    preserve_tzid: str | None = None,
) -> bytes:
    normalized = _validate_changes(changes, allow_times=True)
    original = _parse_event(raw)
    original_start = getattr(original.get("DTSTART"), "dt", None)
    original_kind = events._value_kind(original_start)
    replacements: dict[str, bytes | tuple[bytes, ...] | None] = {}
    for name, requested in normalized.items():
        if name == "VALARM":
            replacements[name] = _alarm_bytes(requested or ())
            continue
        if requested is None:
            replacements[name] = None
            continue
        value = requested
        if name in {"DTSTART", "DTEND"}:
            value = _boundary(requested, name, preserve_tzid=preserve_tzid)
        elif name in {"STATUS", "CLASS", "TRANSP"}:
            value = str(requested).upper()
        if name == "DTEND":
            replacements["DURATION"] = None
        if name == "RELATED-TO" and isinstance(value, (list, tuple)):
            replacements[name] = tuple(_event_property_bytes(name, item) for item in value)
        elif name == "CATEGORIES" and isinstance(value, (list, tuple)):
            replacements[name] = (_event_property_bytes(name, list(value)),)
        else:
            replacements[name] = _event_property_bytes(name, value)
    patched = _splice_event(
        raw,
        replacements,
        remove_components=("VALARM",) if "VALARM" in replacements else (),
    )
    if portable_description:
        patched_event = _parse_event(patched)
        from .mutate import _portable_description

        description = _portable_description(_description(patched_event), patched_event)
        patched = _splice_event(
            patched,
            {"DESCRIPTION": _event_property_bytes("DESCRIPTION", description)},
        )

    patched_event = _parse_event(patched)
    new_start = getattr(patched_event.get("DTSTART"), "dt", None)
    changed_boundaries = {name for name in normalized if name in {"DTSTART", "DTEND"}}
    if (
        events._value_kind(new_start) != original_kind
        and changed_boundaries != {"DTSTART", "DTEND"}
    ):
        raise _error(
            "converting between all-day and timed recurrence exceptions requires both boundaries",
            exits.USAGE,
        )
    events._event_bounds(patched_event, href="", code=exits.USAGE)
    moment = now or dt.datetime.now(dt.UTC)
    stamp = _event_property_bytes("DTSTAMP", moment.astimezone(dt.UTC))
    try:
        sequence = int(str(patched_event.get("SEQUENCE", 0)))
    except (TypeError, ValueError):
        sequence = 0
    sequence_line = _event_property_bytes("SEQUENCE", sequence + 1)
    return _splice_event(patched, {"DTSTAMP": stamp, "SEQUENCE": sequence_line})


def _exception_base(master_raw: bytes, identity: WireId) -> bytes:
    raw = _splice_event(
        master_raw,
        {name: None for name in ("RRULE", "RDATE", "EXDATE", "EXRULE", "RECURRENCE-ID")},
    )
    identity_line = f"RECURRENCE-ID:{identity.text}".encode()
    if identity.text.startswith("TZID="):
        prefix, value = identity.text.split(":", 1)
        identity_line = f"RECURRENCE-ID;{prefix}:{value}".encode()
    elif identity.text.startswith("VALUE=DATE:"):
        identity_line = f"RECURRENCE-ID;VALUE=DATE:{identity.text.split(':', 1)[1]}".encode()
    return _splice_event(raw, {"RECURRENCE-ID": identity_line + _line_ending(raw)})


def _uid_line(uid: str) -> bytes:
    return _event_property_bytes("UID", uid)


def _rrule_parts(text: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    for item in text.split(";"):
        if "=" not in item:
            raise _error("the RRULE contains a malformed part")
        key, value = item.split("=", 1)
        key = key.upper()
        if not key or not value or any(existing == key for existing, _ in parts):
            raise _error("the RRULE contains duplicate or empty parts")
        parts.append((key, value))
    if not any(key == "FREQ" for key, _ in parts):
        raise _error("the RRULE has no FREQ")
    return parts


def _validate_rrule_types(start: WireId, parts: Mapping[str, str]) -> None:
    """Reject RRULE fields that cannot preserve the master's value type."""
    until = parts.get("UNTIL")
    if start.kind == "DATE":
        if until is not None and not re.fullmatch(r"\d{8}", until):
            raise _error("a DATE DTSTART requires a DATE-valued RRULE UNTIL")
        frequency = parts.get("FREQ", "").upper()
        if frequency in {"HOURLY", "MINUTELY", "SECONDLY"}:
            raise _error("a DATE DTSTART cannot use a sub-day RRULE frequency")
        if {"BYHOUR", "BYMINUTE", "BYSECOND"} & parts.keys():
            raise _error("a DATE DTSTART cannot use time-valued RRULE parts")
    elif until is not None and not re.fullmatch(r"\d{8}T\d{6}Z", until):
        raise _error("a DATE-TIME DTSTART requires a UTC RRULE UNTIL")


def _rrule_with(text: str, *, count: int | None = None, until: str | None = None) -> str:
    parts = [(key, value) for key, value in _rrule_parts(text) if key not in {"COUNT", "UNTIL"}]
    if count is not None:
        parts.append(("COUNT", str(count)))
    elif until is not None:
        parts.append(("UNTIL", until))
    return ";".join(f"{key}={value}" for key, value in parts)


def _rrule_value_line(name: str, value: str | None) -> bytes | None:
    return None if value is None else f"{name}:{value}".encode()


def _set_wire_lines(name: str, values: Iterable[WireId], model: _SeriesModel) -> bytes | None:
    values = tuple(values)
    if not values:
        return None
    if model.start.kind == "DATE":
        prefix = f"{name};VALUE=DATE:"
    elif model.start.tzid is not None:
        prefix = f"{name};TZID={model.start.tzid}:"
    else:
        prefix = f"{name}:"
    payload = ",".join(
        item.text.split(":", 1)[1] if ":" in item.text else item.text for item in values
    )
    return (prefix + payload).encode("utf-8")


def _base_rule_values_to_cut(model: _SeriesModel, cut: WireId) -> tuple[WireId, ...]:
    if model.rrule is None:
        return ()
    return _rule_between(model, model.start, cut)


def _wire_until(value: WireId, original_text: str) -> str:
    original = dict(_rrule_parts(original_text))
    if value.kind == "DATE":
        return value.value.strftime("%Y%m%d")  # type: ignore[union-attr]
    assert isinstance(value.value, dt.datetime)
    if original.get("UNTIL", "").endswith("Z") or value.tzid is None:
        return value.value.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return value.value.strftime("%Y%m%dT%H%M%S")


def _rule_values(start: WireId, rrule: str | None) -> tuple[WireId, ...]:
    if rrule is None:
        return ()
    model = _SeriesModel(start, rrule, (), ())
    return _rule_between(model, start, None)


def _prove_rule_partition(
    model: _SeriesModel,
    cut: WireId,
    old_rule: str | None,
    new_rule: str | None,
) -> tuple[tuple[WireId, ...], int]:
    original = _rule_values(model.start, model.rrule)
    try:
        position = next(index for index, item in enumerate(original) if item.key == cut.key)
    except StopIteration as exc:
        raise _error("the RRULE partition did not contain its requested cut") from exc
    old_values = _rule_values(model.start, old_rule)
    new_values = _rule_values(cut, new_rule)
    if tuple(item.key for item in old_values) != tuple(item.key for item in original[:position]):
        raise _error("this-and-future cannot prove the old RRULE partition")
    if tuple(item.key for item in new_values) != tuple(item.key for item in original[position:]):
        raise _error("this-and-future cannot prove the new RRULE partition")
    return original, position


def _split_model(
    model: _SeriesModel, cut: WireId
) -> tuple[
    str | None,
    str | None,
    tuple[WireId, ...],
    tuple[WireId, ...],
    tuple[WireId, ...],
    tuple[WireId, ...],
    bool,
]:
    """Partition the recurrence set, returning old/new rule and date values."""
    _check_same_identity_kind(model.start, cut, label="the recurrence target")
    if not _identity_exists(model, cut):
        raise _error("the recurrence target is not an active occurrence", exits.TARGET_NOT_FOUND)
    base = _base_rule_values_to_cut(model, cut)
    base_keys = {item.key for item in base}
    if model.rrule is not None and cut.key not in base_keys:
        raise _error("this-and-future is only proven for a generated RRULE occurrence")
    old_rdates = tuple(item for item in model.rdates if item.key < cut.key)
    new_rdates = tuple(item for item in model.rdates if item.key > cut.key)
    old_exdates = tuple(item for item in model.exdates if item.key < cut.key)
    new_exdates = tuple(item for item in model.exdates if item.key >= cut.key)
    if model.rrule is None:
        if cut.key < model.start.key:
            raise _error("this-and-future cannot split before the master DTSTART")
        old_rule = None
        new_rule = None
        old_keys = {model.start.key, *(item.key for item in old_rdates)}
        if model.start.key == cut.key and old_rdates:
            raise _error("this-and-future cannot re-anchor a master with earlier RDATE values")
        old_exists = any(key != cut.key for key in old_keys)
        return old_rule, new_rule, old_rdates, new_rdates, old_exdates, new_exdates, old_exists

    parts = dict(_rrule_parts(model.rrule))
    count_text = parts.get("COUNT")
    if count_text is not None:
        try:
            total = int(count_text)
            position = next(index for index, item in enumerate(base) if item.key == cut.key)
        except (ValueError, StopIteration) as exc:
            raise _error("the RRULE COUNT could not be partitioned") from exc
        old_count = position
        new_count = total - position
        if new_count <= 0:
            raise _error("the recurrence target is outside the RRULE COUNT")
        old_rule = _rrule_with(model.rrule, count=old_count) if old_count else None
        new_rule = _rrule_with(model.rrule, count=new_count)
        _, proven_position = _prove_rule_partition(model, cut, old_rule, new_rule)
        if proven_position != position:
            raise _error("this-and-future cannot prove the RRULE COUNT position")
        old_keys = {model.start.key} if model.start.key < cut.key else set()
        old_keys.update(item.key for item in base if item.key < cut.key)
        old_keys.update(item.key for item in old_rdates)
        if old_rule is None and model.start.key == cut.key and old_rdates:
            raise _error("this-and-future cannot re-anchor a master with earlier RDATE values")
        return old_rule, new_rule, old_rdates, new_rdates, old_exdates, new_exdates, bool(old_keys)

    frequency = parts.get("FREQ", "").upper()
    if frequency not in {"DAILY", "WEEKLY"} or not set(parts) <= {
        "FREQ",
        "INTERVAL",
        "UNTIL",
    }:
        raise _error("this-and-future cannot prove the RRULE partition")
    previous = [item for item in base if item.key < cut.key]
    old_rule = (
        _rrule_with(model.rrule, until=_wire_until(previous[-1], model.rrule))
        if previous
        else None
    )
    new_rule = model.rrule
    if "UNTIL" in parts:
        _prove_rule_partition(model, cut, old_rule, new_rule)
    old_keys = {model.start.key} if model.start.key < cut.key else set()
    old_keys.update(item.key for item in previous)
    old_keys.update(item.key for item in old_rdates)
    if old_rule is None and model.start.key == cut.key and old_rdates:
        raise _error("this-and-future cannot re-anchor a master with earlier RDATE values")
    return old_rule, new_rule, old_rdates, new_rdates, old_exdates, new_exdates, bool(old_keys)


def _apply_recurrence_set(
    raw: bytes,
    model: _SeriesModel,
    *,
    rrule: str | None,
    rdates: Iterable[WireId],
    exdates: Iterable[WireId],
) -> bytes:
    return _splice_event(
        raw,
        {
            "RRULE": _rrule_value_line("RRULE", rrule),
            "RDATE": _set_wire_lines("RDATE", rdates, model),
            "EXDATE": _set_wire_lines("EXDATE", exdates, model),
            "EXRULE": None,
        },
    )


def _remap_uid(raw: bytes, uid: str) -> bytes:
    return _splice_event(raw, {"UID": _uid_line(uid)})


def _planned_reference(
    resource: Resource, raw: bytes, *, href: str, etag_value: str
) -> events.EventRef:
    return events._describe(raw, calendar_href=resource.calendar_href, href=href, etag=etag_value)


def _step_details(
    reference: events.EventRef,
    *,
    target: str,
    recurrence_id: str = "",
    **extra: Any,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "calendar_href": reference.calendar_href,
        "uid": reference.uid,
        "start": reference.start,
        "end": reference.end,
        "all_day": reference.all_day,
        "url": reference.url,
        "status": reference.status,
        "recurrence_target": target,
    }
    if recurrence_id:
        details["recurrence_id"] = recurrence_id
    details.update(extra)
    return details


def _update_step(
    resource: Resource, raw: bytes, *, target: str, recurrence_id: str = ""
) -> plans.Step:
    reference = _planned_reference(resource, raw, href=resource.href, etag_value=resource.etag)
    return plans.freeze_step(
        action="cal.update",
        href=resource.href,
        etag=resource.etag,
        summary=reference.summary,
        payload=raw,
        content_type="text/calendar; charset=utf-8",
        details=_step_details(reference, target=target, recurrence_id=recurrence_id),
    )


def _delete_step(
    resource: Resource, *, target: str, recurrence_id: str = ""
) -> plans.Step:
    reference = _planned_reference(
        resource,
        resource.raw,
        href=resource.href,
        etag_value=resource.etag,
    )
    return plans.freeze_step(
        action="cal.delete",
        href=resource.href,
        etag=resource.etag,
        summary=reference.summary,
        details=_step_details(reference, target=target, recurrence_id=recurrence_id),
    )


def _create_step(
    resource: Resource,
    raw: bytes,
    *,
    href: str,
    target: str,
    warning: str = "",
) -> plans.Step:
    reference = _planned_reference(resource, raw, href=href, etag_value="")
    details = _step_details(reference, target=target, warning=warning)
    return plans.freeze_step(
        action="cal.create",
        href=href,
        etag="",
        summary=reference.summary,
        payload=raw,
        content_type="text/calendar; charset=utf-8",
        details=details,
    )


def _strong_etag(resource: Resource) -> str:
    if not resource.etag:
        raise _error(
            "the server returned no ETag for this recurring resource",
            exits.MALFORMED_RESPONSE,
        )
    return events.strong_etag(resource.etag)


def _resource_for_plan(profile: Any, session: Session, href: str) -> Resource:
    reference, raw = events.fetch(profile, session=session, href=href)
    return _validate_resource(
        raw,
        calendar_href=reference.calendar_href,
        href=reference.href,
        etag=reference.etag,
    )


def _validate_planned_resource(
    resource: Resource, raw: bytes, *, href: str, etag: str
) -> None:
    """Validate the final resource before freezing any recurrence mutation."""
    _validate_component_tree(raw)
    parsed = _parse_calendar(raw)
    direct = _direct_events(raw, parsed)
    if any(_property_names(component) & RECURRENCE_MARKERS for component, _ in direct):
        _validate_resource(raw, calendar_href=resource.calendar_href, href=href, etag=etag)
    else:
        events._describe(raw, calendar_href=resource.calendar_href, href=href, etag=etag)


def _resolve_target(resource: Resource, text: str) -> WireId:
    target = parse_wire_id(text)
    if not _identity_exists(resource.model, target):
        raise _error("the recurrence target is not an active occurrence", exits.TARGET_NOT_FOUND)
    return target


def _append_override(resource: Resource, base: bytes, *, override: bytes) -> bytes:
    return _rewrite_resource(resource, master_raw=base, appended=(override,))


def plan_update(
    profile: Any,
    *,
    session: Session,
    href: str,
    target: str,
    changes: Mapping[str, Any],
    recurrence_id: str = "",
    portable_description: bool = False,
) -> plans.Plan:
    if target not in TARGETS:
        raise _error(f"unknown recurrence target {target!r}", exits.USAGE)
    if target == "series" and recurrence_id:
        raise _error("--recurrence-id belongs only to an occurrence target", exits.USAGE)
    if target in {"occurrence", "this-and-future"} and not recurrence_id:
        raise _error("--recurrence-id is required for an occurrence target", exits.USAGE)
    resource = _resource_for_plan(profile, session, href)
    etag_value = _strong_etag(resource)
    if resource.etag != etag_value:
        resource = Resource(**{**resource.__dict__, "etag": etag_value})
    if target == "series":
        if recurrence_id:
            raise _error("--recurrence-id belongs only to an occurrence target", exits.USAGE)
        normalized = _validate_changes(changes, allow_times=False)
        master = _patch_component(
            resource.master_raw,
            normalized,
            portable_description=portable_description,
        )
        planned = _rewrite_resource(resource, master_raw=master)
        _validate_planned_resource(resource, planned, href=resource.href, etag=resource.etag)
        step = _update_step(resource, planned, target=target)
        return plans.write_bundle(profile=profile.name, summary=step.summary, steps=(step,))
    if not recurrence_id:
        raise _error("--recurrence-id is required for an occurrence target", exits.USAGE)
    identity = _resolve_target(resource, recurrence_id)
    if target == "occurrence":
        normalized = _validate_changes(changes, allow_times=True)
        existing = resource.override_by_key.get(identity.key)
        if existing is None:
            base = _exception_base(resource.master_raw, identity)
            if "DTSTART" not in normalized and identity.key != resource.model.start.key:
                normalized = dict(normalized)
                normalized["DTSTART"] = identity.value
                if resource.master.get("DTEND") is not None:
                    normalized["DTEND"] = _duration_model(resource.master).end(identity.value)
            exception = _patch_component(
                base,
                normalized,
                portable_description=portable_description,
            )
            planned = _append_override(resource, resource.master_raw, override=exception)
        else:
            exception = _patch_component(
                existing[2],
                normalized,
                portable_description=portable_description,
            )
            planned = _rewrite_resource(resource, master_raw=resource.master_raw)
            planned = _replace_component(planned, existing[2], exception)
        _validate_planned_resource(resource, planned, href=resource.href, etag=resource.etag)
        step = _update_step(resource, planned, target=target, recurrence_id=identity.text)
        return plans.write_bundle(profile=profile.name, summary=step.summary, steps=(step,))

    if any(name.upper() in {"DTSTART", "DTEND"} for name in changes):
        raise _error("this-and-future does not shift DTSTART or DTEND", exits.UNSUPPORTED_STRUCTURE)
    normalized = _validate_changes(changes, allow_times=False)
    (
        old_rule,
        new_rule,
        old_rdates,
        new_rdates,
        old_exdates,
        new_exdates,
        old_exists,
    ) = _split_model(resource.model, identity)
    new_uid = f"{token_source.token_hex(16)}@ncl"
    old_master = _apply_recurrence_set(
        resource.master_raw,
        resource.model,
        rrule=old_rule,
        rdates=old_rdates,
        exdates=old_exdates,
    )
    if old_exists:
        old_master = _patch_component(old_master, {})
    new_master = _apply_recurrence_set(
        resource.master_raw,
        resource.model,
        rrule=new_rule,
        rdates=new_rdates,
        exdates=new_exdates,
    )
    new_changes = dict(normalized)
    new_changes["DTSTART"] = identity.value
    if resource.master.get("DTEND") is not None:
        new_changes["DTEND"] = _duration_model(resource.master).end(identity.value)
    new_master = _patch_component(
        new_master,
        new_changes,
        portable_description=portable_description,
        preserve_tzid=resource.model.start.tzid,
    )
    new_master = _remap_uid(new_master, new_uid)
    future_overrides: list[bytes] = []
    removed_indices: list[int] = []
    for index, (override_identity, _, override_raw) in enumerate(resource.override_by_key.values()):
        if override_identity.key >= identity.key:
            future_overrides.append(_remap_uid(override_raw, new_uid))
            removed_indices.append(index)
    old_raw = _rewrite_resource(resource, master_raw=old_master, removed_overrides=removed_indices)
    new_raw = _rewrite_resource(
        resource,
        master_raw=new_master,
        removed_overrides=range(len(resource.override_raw)),
        appended=future_overrides,
    )
    new_href = f"{resource.calendar_href.rstrip('/')}/{new_uid}.ics"
    _validate_planned_resource(resource, new_raw, href=new_href, etag="")
    if old_exists:
        _validate_planned_resource(resource, old_raw, href=resource.href, etag=resource.etag)
    warning = (
        "This split is non-atomic: apply creates the future resource before "
        "changing the old resource; "
        "a partial failure may temporarily duplicate future occurrences."
    )
    create = _create_step(resource, new_raw, href=new_href, target=target, warning=warning)
    if old_exists:
        old = _update_step(resource, old_raw, target=target, recurrence_id=identity.text)
    else:
        old = _delete_step(resource, target=target, recurrence_id=identity.text)
    return plans.write_bundle(
        profile=profile.name,
        summary=f"{create.summary} (non-atomic future split)",
        steps=(create, old),
    )


def plan_delete(
    profile: Any,
    *,
    session: Session,
    href: str,
    target: str,
    recurrence_id: str = "",
) -> plans.Plan:
    if target not in TARGETS:
        raise _error(f"unknown recurrence target {target!r}", exits.USAGE)
    if target == "series" and recurrence_id:
        raise _error("--recurrence-id belongs only to an occurrence target", exits.USAGE)
    if target in {"occurrence", "this-and-future"} and not recurrence_id:
        raise _error("--recurrence-id is required for an occurrence target", exits.USAGE)
    resource = _resource_for_plan(profile, session, href)
    resource = Resource(**{**resource.__dict__, "etag": _strong_etag(resource)})
    if target == "series":
        if recurrence_id:
            raise _error("--recurrence-id belongs only to an occurrence target", exits.USAGE)
        step = _delete_step(resource, target=target)
        return plans.write_bundle(profile=profile.name, summary=step.summary, steps=(step,))
    if target == "occurrence":
        identity = _resolve_target(resource, recurrence_id)
        existing = resource.override_by_key.get(identity.key)
        if existing is None:
            exception = _patch_component(
                _exception_base(resource.master_raw, identity),
                {"STATUS": "CANCELLED"},
            )
            planned = _append_override(resource, resource.master_raw, override=exception)
        else:
            exception = _patch_component(existing[2], {"STATUS": "CANCELLED"})
            planned = _replace_component(resource.raw, existing[2], exception)
        _validate_planned_resource(resource, planned, href=resource.href, etag=resource.etag)
        step = _update_step(resource, planned, target=target, recurrence_id=identity.text)
        return plans.write_bundle(profile=profile.name, summary=step.summary, steps=(step,))
    if target == "this-and-future":
        identity = _resolve_target(resource, recurrence_id)
        old_rule, _, old_rdates, _, old_exdates, _, old_exists = _split_model(
            resource.model, identity
        )
        old_master = _apply_recurrence_set(
            resource.master_raw,
            resource.model,
            rrule=old_rule,
            rdates=old_rdates,
            exdates=old_exdates,
        )
        removed = [
            index
            for index, (item, _, _) in enumerate(resource.override_by_key.values())
            if item.key >= identity.key
        ]
        if old_exists:
            old_master = _patch_component(old_master, {})
            old_raw = _rewrite_resource(resource, master_raw=old_master, removed_overrides=removed)
            _validate_planned_resource(resource, old_raw, href=resource.href, etag=resource.etag)
            step = _update_step(resource, old_raw, target=target, recurrence_id=identity.text)
        else:
            step = _delete_step(resource, target=target, recurrence_id=identity.text)
        return plans.write_bundle(profile=profile.name, summary=step.summary, steps=(step,))
    raise _error(f"unknown recurrence target {target!r}", exits.USAGE)


def occurrences(
    profile: Any,
    *,
    session: Session,
    calendar_href: str,
    start: dt.datetime,
    end: dt.datetime,
) -> list[Occurrence]:
    """Discover validated recurrence instances through one bounded REPORT."""
    if end <= start:
        raise _error("the occurrence window ends before it starts", exits.USAGE)
    if not profiles.in_scope(calendar_href, list(profile.calendars)):
        raise CalendarError(
            f"{calendar_href} is outside this profile's calendar allowlist", exits.SCOPE_DENIED
        )
    resources = events.query_raw(
        profile,
        session=session,
        calendar_href=calendar_href,
        start=start,
        end=end,
    )
    found: list[Occurrence] = []
    for href, etag_value, raw in resources:
        parsed = _parse_calendar(raw)
        direct = _direct_events(raw, parsed)
        if not any(_property_names(component) & RECURRENCE_MARKERS for component, _ in direct):
            continue
        resource = _validate_resource(
            raw,
            calendar_href=_canonical(profile, calendar_href),
            href=href,
            etag=etag_value,
        )
        found.extend(_resource_occurrences(resource, start, end))
    found.sort(key=lambda item: (item.effective_start, item.href, item.recurrence_id))
    return found
