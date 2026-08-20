"""Component-neutral semantic comparison for iCalendar resources."""

from __future__ import annotations

import datetime as dt
from typing import Any

import icalendar


# CalDAV servers may refresh these values while storing a resource. Every
# other property and nested component remains part of the readback invariant.
SERVER_MANAGED_PROPERTIES = frozenset({"DTSTAMP", "LAST-MODIFIED"})


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
        if upper_name in SERVER_MANAGED_PROPERTIES:
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


def calendar(raw: bytes) -> tuple[Any, ...]:
    """Parse and normalize iCalendar content for semantic equality."""
    try:
        parsed = icalendar.Calendar.from_ical(raw)
    except (ValueError, IndexError, TypeError) as exc:
        raise ValueError("iCalendar content was not valid") from exc
    return _semantic_component(parsed)
