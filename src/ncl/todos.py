"""Reading and mutating VTODO resources over CalDAV.

Tasks share a calendar collection with events, but their component model is
different. This module owns that model and only reuses canonical hrefs,
allowlist checks, ETags, and the generic frozen-plan store.
"""

from __future__ import annotations

import datetime as dt
import secrets as token_source
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

import icalendar

from . import etag, exits, ical_semantics, plans, profiles
from .caldav import CalendarError, _canonical
from .identity import CALDAV, DAV, _element_name, _status_code
from .session import Session, SessionError

PRODID = "-//ai-agent-nextcloud//ncl//EN"

TODO_STATUSES = ("NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED")
PRIORITY_RANGE = range(0, 10)
PERCENT_RANGE = range(0, 101)

# These properties either describe a recurring series, participate in
# iCalendar scheduling, or are not modeled by this module. The parser names
# them in output, and mutation refuses the whole resource before changing any
# modeled property.
UNSUPPORTED_PROPERTIES = (
    "DURATION",
    "RRULE",
    "RDATE",
    "EXDATE",
    "EXRULE",
    "RECURRENCE-ID",
    "ORGANIZER",
    "ATTENDEE",
    "REQUEST-STATUS",
    "SCHEDULE-AGENT",
    "SCHEDULE-FORCE-SEND",
    "SCHEDULE-STATUS",
    "REFID",
)

# RFC 5545 VTODO properties in the required and optional singleton groups.
# Repeatable properties such as ATTENDEE, RELATED-TO, and RSTATUS are not in
# this set. RRULE is deliberately absent: RFC 5545 says it SHOULD NOT repeat,
# rather than making a repeated RRULE malformed by cardinality alone.
VTODO_SINGLETON_PROPERTIES = frozenset(
    {
        "CLASS",
        "COMPLETED",
        "CREATED",
        "DESCRIPTION",
        "DTSTART",
        "DTSTAMP",
        "DUE",
        "DURATION",
        "GEO",
        "LAST-MODIFIED",
        "LOCATION",
        "ORGANIZER",
        "PERCENT-COMPLETE",
        "PRIORITY",
        "RECURRENCE-ID",
        "REFID",
        "SEQUENCE",
        "STATUS",
        "SUMMARY",
        "UID",
        "URL",
    }
)


class TodoError(RuntimeError):
    """A VTODO resource or response was not usable."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class TodoRef:
    """A task and the stable state needed to address it conditionally."""

    href: str
    calendar_href: str
    uid: str
    summary: str
    description: str
    dtstart: str
    due: str
    completed: str
    percent_complete: int | str
    status: str
    priority: int | str
    parent_uid: str
    etag: str
    writable: bool
    unsupported: tuple[str, ...]
    children: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return every modeled field, including absent properties as empty values."""
        return {
            "href": self.href,
            "calendar_href": self.calendar_href,
            "uid": self.uid,
            "summary": self.summary,
            "description": self.description,
            "dtstart": self.dtstart,
            "due": self.due,
            "completed": self.completed,
            "percent_complete": self.percent_complete,
            "status": self.status,
            "priority": self.priority,
            "parent_uid": self.parent_uid,
            "etag": self.etag,
            "writable": self.writable,
            "unsupported": list(self.unsupported),
            "children": list(self.children),
        }


@dataclass(frozen=True)
class TodoResource:
    """One validated VTODO and the bytes returned with its report entry."""

    reference: TodoRef
    raw: bytes


def _component_text(component: Any, name: str) -> str:
    value = component.get(name)
    return str(value) if value is not None else ""


def _component_integer(component: Any, name: str) -> int | str:
    value = component.get(name)
    if value is None:
        return ""
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise TodoError(f"the task has a malformed {name} value") from exc


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.strftime("%Y%m%dT%H%M%S")
        return value.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")
    raise TodoError("the task has a malformed date-time value")


def _unsupported(component: Any, parsed: icalendar.Calendar) -> tuple[str, ...]:
    names = [name for name in UNSUPPORTED_PROPERTIES if name in component]
    components = {item.name for item in parsed.walk() if item.name.startswith("V")}
    extra = components - {"VCALENDAR", "VTIMEZONE", "VALARM", "VTODO"}
    names.extend(sorted(extra))
    return tuple(dict.fromkeys(names))


def _parent_uid(component: Any) -> tuple[str, int]:
    parent = ""
    count = 0
    for name, value in component.property_items():
        if name != "RELATED-TO":
            continue
        params = getattr(value, "params", {})
        relation = str(params.get("RELTYPE", "PARENT")).upper()
        if relation == "PARENT":
            count += 1
            if not parent:
                parent = str(value)
    return parent, count


def _property_value(component: Any, name: str) -> Any:
    property_value = component.get(name)
    return getattr(property_value, "dt", None) if property_value is not None else None


def _value_kind(value: Any) -> str | None:
    if isinstance(value, dt.datetime):
        return "DATE-TIME"
    if isinstance(value, dt.date):
        return "DATE"
    return None


def _validate_singletons(component: Any, *, href: str) -> None:
    location = f" at {href}" if href else ""
    counts: dict[str, int] = {}
    for name, _ in component.property_items():
        upper_name = name.upper()
        if upper_name in VTODO_SINGLETON_PROPERTIES:
            counts[upper_name] = counts.get(upper_name, 0) + 1
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        raise TodoError(
            f"the task{location} repeats singleton properties: {', '.join(duplicates)}"
        )


def _validate_boundaries(
    start: Any,
    due: Any,
    *,
    href: str,
    code: int,
) -> None:
    location = f" at {href}" if href else ""
    start_kind = _value_kind(start)
    due_kind = _value_kind(due)
    if start is not None and start_kind is None:
        raise TodoError(f"the task{location} has an invalid DTSTART", code)
    if due is not None and due_kind is None:
        raise TodoError(f"the task{location} has an invalid DUE", code)
    if start is not None and due is not None:
        if start_kind != due_kind:
            raise TodoError(
                f"the task{location} mixes DATE and DATE-TIME DTSTART and DUE values",
                code,
            )
        try:
            if due <= start:
                raise TodoError(f"the task{location} is due no later than it starts", code)
        except TypeError as exc:
            raise TodoError(
                f"the task{location} has incomparable DTSTART and DUE values", code
            ) from exc


def _validate_todo(component: Any, *, href: str) -> None:
    location = f" at {href}" if href else ""
    _validate_singletons(component, href=href)

    uid = component.get("UID")
    if uid is None or not str(uid).strip():
        raise TodoError(f"the task{location} has no UID")

    stamp = _property_value(component, "DTSTAMP")
    if not isinstance(stamp, dt.datetime) or stamp.tzinfo is None:
        raise TodoError(f"the task{location} has no valid DTSTAMP")

    start_property = component.get("DTSTART")
    due_property = component.get("DUE")
    duration_property = component.get("DURATION")
    start = _property_value(component, "DTSTART")
    due = _property_value(component, "DUE")
    if component.get("DTSTART") is not None and start is None:
        raise TodoError(f"the task{location} has an invalid DTSTART")
    if component.get("DUE") is not None and due is None:
        raise TodoError(f"the task{location} has an invalid DUE")
    if duration_property is not None:
        duration = _property_value(component, "DURATION")
        if not isinstance(duration, dt.timedelta):
            raise TodoError(f"the task{location} has an invalid DURATION")
        if duration <= dt.timedelta(0):
            raise TodoError(f"the task{location} has a non-positive DURATION")
        if due_property is not None:
            raise TodoError(f"the task{location} has both DUE and DURATION")
        if start_property is None:
            raise TodoError(f"the task{location} has DURATION without DTSTART")
        if _value_kind(start) == "DATE" and duration % dt.timedelta(days=1):
            raise TodoError(
                f"the task{location} has a sub-day DURATION with a DATE DTSTART"
            )
    _validate_boundaries(start, due, href=href, code=exits.MALFORMED_RESPONSE)

    completed_property = component.get("COMPLETED")
    completed = _property_value(component, "COMPLETED")
    completed_params = getattr(completed_property, "params", {})
    if (
        completed_property is not None
        and (
            not isinstance(completed, dt.datetime)
            or completed.tzinfo is None
            or completed.utcoffset() != dt.timedelta(0)
            or any(str(name).upper() == "TZID" for name in completed_params)
        )
    ):
        raise TodoError(f"the task{location} has an invalid COMPLETED value")

    status = _component_text(component, "STATUS").upper()
    if status and status not in TODO_STATUSES:
        raise TodoError(f"the task{location} has an invalid STATUS value")

    priority = _component_integer(component, "PRIORITY")
    if priority != "" and priority not in PRIORITY_RANGE:
        raise TodoError(f"the task{location} has an invalid PRIORITY value")

    percent = _component_integer(component, "PERCENT-COMPLETE")
    if percent != "" and percent not in PERCENT_RANGE:
        raise TodoError(f"the task{location} has an invalid PERCENT-COMPLETE value")


def _parse_todo(raw: bytes, *, href: str) -> tuple[icalendar.Calendar, Any]:
    try:
        parsed = icalendar.Calendar.from_ical(raw)
    except (IndexError, TypeError, ValueError) as exc:
        raise TodoError(f"the task at {href} was not valid iCalendar") from exc
    if parsed.name != "VCALENDAR":
        raise TodoError(f"the resource at {href} was not a VCALENDAR")
    todos = [item for item in parsed.walk() if item.name == "VTODO"]
    direct_todos = [item for item in parsed.subcomponents if item.name == "VTODO"]
    if len(todos) != len(direct_todos):
        raise TodoError(
            f"the resource at {href} has a VTODO nested inside another component"
        )
    if not todos:
        raise TodoError(
            f"the resource at {href} holds no VTODO",
            exits.UNSUPPORTED_COLLECTION,
        )
    if len(todos) > 1:
        raise TodoError(
            f"the resource at {href} holds more than one VTODO",
            exits.UNSUPPORTED_STRUCTURE,
        )
    _validate_todo(todos[0], href=href)
    return parsed, todos[0]


def _describe(
    raw: bytes,
    *,
    calendar_href: str,
    href: str,
    etag: str,
    collection_writable: bool | None = None,
) -> TodoRef:
    parsed, component = _parse_todo(raw, href=href)
    unsupported = _unsupported(component, parsed)
    parent_uid, parent_count = _parent_uid(component)
    if parent_count > 1:
        unsupported = tuple(dict.fromkeys((*unsupported, "RELATED-TO")))

    writable = not unsupported
    if collection_writable is not None:
        writable = writable and collection_writable

    status = _component_text(component, "STATUS").upper()
    return TodoRef(
        href=href,
        calendar_href=calendar_href,
        uid=_component_text(component, "UID"),
        summary=_component_text(component, "SUMMARY"),
        description=_component_text(component, "DESCRIPTION"),
        dtstart=_format_value(getattr(component.get("DTSTART"), "dt", None)),
        due=_format_value(getattr(component.get("DUE"), "dt", None)),
        completed=_format_value(getattr(component.get("COMPLETED"), "dt", None)),
        percent_complete=_component_integer(component, "PERCENT-COMPLETE"),
        status=status,
        priority=_component_integer(component, "PRIORITY"),
        parent_uid=parent_uid,
        etag=etag,
        writable=writable,
        unsupported=unsupported,
    )


def _report_filter() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR">'
        '<c:comp-filter name="VTODO"/>'
        "</c:comp-filter></c:filter>"
        "</c:calendar-query>"
    )


def _entry_data(entry: ET.Element) -> tuple[str, bytes]:
    href_element = next((item for item in entry if _element_name(item) == (DAV, "href")), None)
    raw_href = (href_element.text or "").strip() if href_element is not None else ""
    if not raw_href:
        raise TodoError("the CalDAV response omitted a task href")

    data: bytes | None = None
    for propstat in entry:
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
            name = _element_name(element)
            if name == (CALDAV, "calendar-data"):
                candidate = (element.text or "").encode("utf-8")
                if candidate.strip():
                    data = candidate
    if data is None:
        raise TodoError(
            f"the CalDAV response for {raw_href} contained no successful calendar-data"
        )
    return raw_href, data


def _validate_status(value: str) -> str:
    if not isinstance(value, str):
        raise TodoError(f"status must be one of {', '.join(TODO_STATUSES)}", exits.USAGE)
    normalized = value.upper()
    if normalized not in TODO_STATUSES:
        raise TodoError(
            f"status must be one of {', '.join(TODO_STATUSES)}; got {value!r}",
            exits.USAGE,
        )
    return normalized


def _validate_priority(value: int) -> int:
    if isinstance(value, bool) or value not in PRIORITY_RANGE:
        raise TodoError(f"priority must be 0 (unspecified) to 9; got {value}", exits.USAGE)
    return value


def _validate_percent(value: int) -> int:
    if isinstance(value, bool) or value not in PERCENT_RANGE:
        raise TodoError(f"percent must be between 0 and 100; got {value}", exits.USAGE)
    return value


def _stamp(value: dt.datetime | dt.date) -> dt.datetime | dt.date:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            raise TodoError("task times must carry a timezone", exits.USAGE)
        return value.astimezone(dt.UTC)
    if isinstance(value, dt.date):
        return value
    raise TodoError("task date-times must be dates or timezone-aware instants", exits.USAGE)


def _stamp_instant(value: dt.datetime) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise TodoError("completion time must carry a timezone", exits.USAGE)
    return value.astimezone(dt.UTC)


def build_todo(
    *,
    uid: str,
    summary: str,
    description: str = "",
    start: dt.datetime | dt.date | None = None,
    due: dt.datetime | dt.date | None = None,
    priority: int | None = None,
    status: str = "",
    percent_complete: int | None = None,
    parent_uid: str = "",
    refid: str = "",
    related_to: tuple[tuple[str, Mapping[str, Any]], ...] = (),
    now: dt.datetime | None = None,
    sequence: int = 0,
) -> str:
    """Serialize one VTODO while leaving recurrence and scheduling absent.

    ``refid`` and ``related_to`` are standard iCalendar extensions used by
    structured task resources. Ordinary tasks leave both at their defaults.
    """
    if not isinstance(uid, str) or not uid.strip():
        raise TodoError("task UID must be nonempty", exits.USAGE)
    if refid and (not isinstance(refid, str) or not refid.strip()):
        raise TodoError("task REFID must be nonempty", exits.USAGE)
    stamped_start = _stamp(start) if start is not None else None
    stamped_due = _stamp(due) if due is not None else None
    _validate_boundaries(
        stamped_start,
        stamped_due,
        href="",
        code=exits.USAGE,
    )
    calendar = icalendar.Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    todo = icalendar.Todo()
    todo.add("uid", uid)
    todo.add("summary", summary)
    todo.add("dtstamp", _stamp_instant(now or dt.datetime.now(dt.UTC)))
    todo.add("sequence", sequence)
    if refid:
        todo.add("refid", refid)
    if description:
        todo.add("description", description)
    if stamped_start is not None:
        todo.add("dtstart", stamped_start)
    if stamped_due is not None:
        todo.add("due", stamped_due)
    if priority is not None:
        todo.add("priority", _validate_priority(priority))
    if status:
        todo.add("status", _validate_status(status))
    if percent_complete is not None:
        todo.add("percent-complete", _validate_percent(percent_complete))
    if parent_uid:
        todo.add("related-to", parent_uid, parameters={"RELTYPE": "PARENT"})
    for target, parameters in related_to:
        if not isinstance(target, str) or not target.strip():
            raise TodoError("task relation target must be nonempty", exits.USAGE)
        todo.add("related-to", target, parameters=dict(parameters))
    calendar.add_component(todo)
    return calendar.to_ical().decode("utf-8")


def _todo_for_mutation(raw: bytes) -> tuple[icalendar.Calendar, Any]:
    parsed, component = _parse_todo(raw, href="the stored resource")
    if "REFID" in component:
        raise TodoError(
            "this task carries REFID and can only be changed through `task run`",
            exits.UNSUPPORTED_STRUCTURE,
        )
    unsupported = _unsupported(component, parsed)
    _, parent_count = _parent_uid(component)
    if parent_count > 1:
        unsupported = tuple(dict.fromkeys((*unsupported, "RELATED-TO")))
    if unsupported:
        raise TodoError(
            f"this task carries {', '.join(unsupported)}, which this release refuses to modify",
            exits.UNSUPPORTED_STRUCTURE,
        )
    return parsed, component


def _replace_parent(component: Any, parent_uid: str) -> None:
    relationships: list[tuple[Any, dict[str, Any]]] = []
    for name, value in component.property_items():
        if name != "RELATED-TO":
            continue
        params = dict(getattr(value, "params", {}))
        relation = str(params.get("RELTYPE", "PARENT")).upper()
        if relation != "PARENT":
            relationships.append((value, params))

    component.pop("RELATED-TO", None)
    for value, params in relationships:
        component.add("RELATED-TO", value, parameters=params)
    if parent_uid:
        component.add("RELATED-TO", parent_uid, parameters={"RELTYPE": "PARENT"})


def patch_todo(raw: bytes, changes: dict[str, Any], *, now: dt.datetime | None = None) -> str:
    """Apply modeled changes to one VTODO while preserving everything else."""
    calendar, todo = _todo_for_mutation(raw)
    allowed = {
        "SUMMARY",
        "COMPLETED",
        "DESCRIPTION",
        "DTSTART",
        "DUE",
        "PRIORITY",
        "STATUS",
        "PERCENT-COMPLETE",
        "RELATED-TO",
    }
    for name, requested in changes.items():
        key = name.upper()
        if key not in allowed:
            raise TodoError(
                f"task property {key} is not modeled for mutation",
                exits.UNSUPPORTED_STRUCTURE,
            )
        if key == "RELATED-TO":
            _replace_parent(todo, requested or "")
            continue
        if requested is None or requested == "":
            todo.pop(key, None)
            continue
        value = requested
        if key in {"DTSTART", "DUE"}:
            value = _stamp(value)
        elif key == "COMPLETED":
            value = _stamp_instant(value)
        elif key == "PRIORITY":
            value = _validate_priority(value)
        elif key == "STATUS":
            value = _validate_status(value)
        elif key == "PERCENT-COMPLETE":
            value = _validate_percent(value)
        todo.pop(key, None)
        todo.add(key, value)

    todo.pop("DTSTAMP", None)
    todo.add("dtstamp", _stamp_instant(now or dt.datetime.now(dt.UTC)))
    try:
        sequence = int(str(todo.get("SEQUENCE", 0)))
    except (TypeError, ValueError):
        sequence = 0
    todo.pop("SEQUENCE", None)
    todo.add("sequence", sequence + 1)
    return calendar.to_ical().decode("utf-8")


def _resource_href(calendar_href: str, uid: str) -> str:
    return f"{calendar_href.rstrip('/')}/{uid}.ics"


def _check_calendar_scope(profile: Any, calendar_href: str) -> None:
    if not profiles.in_scope(calendar_href, list(profile.calendars)):
        raise CalendarError(
            f"{calendar_href} is outside this profile's calendar allowlist",
            exits.SCOPE_DENIED,
        )


def _check_response_scope(profile: Any, calendar_href: str, href: str) -> None:
    selected_authority, selected_segments = profiles.canonicalize_href(calendar_href)
    response_authority, response_segments = profiles.canonicalize_href(href)
    direct_child = (
        response_authority == selected_authority
        and len(response_segments) == len(selected_segments) + 1
        and response_segments[:-1] == selected_segments
    )
    if not direct_child:
        raise CalendarError(
            f"{href} is not a direct child of the selected calendar collection "
            f"{calendar_href}",
            exits.SCOPE_DENIED,
        )
    if not profiles.in_scope(href, list(profile.calendars)):
        raise CalendarError(
            f"{href} is outside this profile's calendar allowlist", exits.SCOPE_DENIED
        )


def _scoped_href(profile: Any, href: str) -> str:
    target = _canonical(profile, href)
    if not profiles.in_scope(target, list(profile.calendars)):
        raise CalendarError(
            f"{target} is outside this profile's calendar allowlist", exits.SCOPE_DENIED
        )
    return target


def query_resources(
    profile: Any,
    *,
    session: Session,
    calendar_href: str,
    collection_writable: bool | None = None,
) -> list[TodoResource]:
    """Read every VTODO in one depth-one report, retaining its original bytes."""
    calendar_href = _canonical(profile, calendar_href)
    _check_calendar_scope(profile, calendar_href)
    response = session.request(
        "REPORT",
        calendar_href,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=_report_filter(),
    )
    if response.status != 207:
        raise TodoError(
            "the task query did not answer with a Multi-Status response",
            exits.MALFORMED_RESPONSE,
        )
    try:
        root = ET.fromstring(response.body)
    except ET.ParseError as exc:
        raise TodoError("the task query response was not valid XML") from exc
    if _element_name(root) != (DAV, "multistatus"):
        raise TodoError("the task query response was not a Multi-Status response")

    found: list[TodoResource] = []
    for entry in root:
        if _element_name(entry) != (DAV, "response"):
            continue
        raw_href, data = _entry_data(entry)
        if not data:
            continue
        href = _canonical(profile, raw_href)
        _check_response_scope(profile, calendar_href, href)
        found.append(
            TodoResource(
                reference=_describe(
                    data,
                    calendar_href=calendar_href,
                    href=href,
                    etag=_entry_etag(entry),
                    collection_writable=collection_writable,
                ),
                raw=data,
            )
        )

    by_uid: dict[str, TodoRef] = {}
    for resource in found:
        task = resource.reference
        if task.uid in by_uid:
            raise TodoError(
                f"the task query returned duplicate UID {task.uid!r}",
                exits.AMBIGUOUS_TARGET,
            )
        by_uid[task.uid] = task
    return found


def query(
    profile: Any,
    *,
    session: Session,
    calendar_href: str,
    statuses: tuple[str, ...] = (),
    collection_writable: bool | None = None,
) -> list[TodoRef]:
    """List tasks with one depth-one VTODO REPORT and no per-task reads."""
    normalized = tuple(_validate_status(status) for status in statuses)
    resources = query_resources(
        profile,
        session=session,
        calendar_href=calendar_href,
        collection_writable=collection_writable,
    )
    found = [resource.reference for resource in resources]

    parent_by_uid = {
        task.uid: task.parent_uid for task in found if task.uid and task.parent_uid
    }
    for uid in parent_by_uid:
        visited: set[str] = set()
        current = uid
        while current in parent_by_uid:
            if current in visited:
                raise TodoError(
                    f"the task hierarchy contains a cycle at UID {current!r}",
                    exits.AMBIGUOUS_TARGET,
                )
            visited.add(current)
            current = parent_by_uid[current]

    children: dict[str, list[str]] = {}
    for task in found:
        if task.parent_uid and task.uid:
            children.setdefault(task.parent_uid, []).append(task.uid)
    found = [
        replace(task, children=tuple(sorted(children.get(task.uid, [])))) for task in found
    ]

    if normalized:
        wanted = set(normalized)
        found = [task for task in found if task.status in wanted]
    return sorted(
        found,
        key=lambda task: (bool(task.parent_uid), task.dtstart or task.due, task.href),
    )


def _entry_etag(entry: ET.Element) -> str:
    for propstat in entry:
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
        etag = next((item for item in prop if _element_name(item) == (DAV, "getetag")), None)
        if etag is not None:
            return (etag.text or "").strip()
    return ""


def _response_url(response: Any) -> str:
    return getattr(response, "url", "") or ""


def _response_location(response: Any) -> str:
    header = getattr(response, "header", None)
    if not callable(header):
        return ""
    return header("Location") or ""


def _refuse_redirect(response: Any, *, action: str, href: str) -> None:
    if 300 <= response.status < 400 or _response_location(response):
        raise TodoError(
            f"the server redirected task {action} at {href}; the target must remain exact",
            exits.MALFORMED_RESPONSE,
        )
    response_url = _response_url(response)
    if response_url and response_url != href:
        raise TodoError(
            f"the server answered task {action} for a different href",
            exits.MALFORMED_RESPONSE,
        )


def fetch(profile: Any, *, session: Session, href: str) -> tuple[TodoRef, bytes]:
    """Read exactly one task resource and retain its original bytes."""
    target = _scoped_href(profile, href)
    response = session.request(
        "GET",
        target,
        headers={"Accept": "text/calendar"},
        max_redirects=0,
    )
    _refuse_redirect(response, action="read", href=target)
    if response.status == 404:
        raise TodoError(f"no task exists at {target}", exits.TARGET_NOT_FOUND)
    if response.status != 200:
        raise TodoError("the task could not be read", exits.SERVER_ERROR)
    calendar_href = target.rsplit("/", 1)[0] + "/"
    return (
        _describe(
            response.body,
            calendar_href=_canonical(profile, calendar_href),
            href=target,
            etag=(response.header("ETag") or "").strip(),
        ),
        response.body,
    )


def _details(reference: TodoRef) -> dict[str, Any]:
    return {
        "calendar_href": reference.calendar_href,
        "uid": reference.uid,
        "description": reference.description,
        "start": reference.dtstart,
        "due": reference.due,
        "completed": reference.completed,
        "percent_complete": reference.percent_complete,
        "status": reference.status,
        "priority": reference.priority,
        "parent_uid": reference.parent_uid,
    }


def plan_create(
    profile: Any,
    *,
    calendar_href: str,
    summary: str,
    description: str = "",
    start: dt.datetime | dt.date | None = None,
    due: dt.datetime | dt.date | None = None,
    priority: int | None = None,
    status: str = "",
    percent_complete: int | None = None,
    parent_uid: str = "",
    now: dt.datetime | None = None,
) -> plans.Plan:
    calendar_href = _canonical(profile, calendar_href)
    _check_calendar_scope(profile, calendar_href)
    uid = f"{token_source.token_hex(16)}@ncl"
    payload = build_todo(
        uid=uid,
        summary=summary,
        description=description,
        start=start,
        due=due,
        priority=priority,
        status=status,
        percent_complete=percent_complete,
        parent_uid=parent_uid,
        now=now,
    )
    reference = _describe(
        payload.encode("utf-8"),
        calendar_href=calendar_href,
        href=_resource_href(calendar_href, uid),
        etag="",
    )
    return plans.write_bundle(
        profile=profile.name,
        summary=summary,
        steps=(
            plans.freeze_step(
                action="task.create",
                href=reference.href,
                etag="",
                summary=summary,
                payload=payload.encode("utf-8"),
                content_type="text/calendar; charset=utf-8",
                details=_details(reference),
            ),
        ),
    )


def _require_etag(reference: TodoRef, operation: str) -> str:
    return _require_strong_etag(reference.etag, operation)


def _require_strong_etag(value: str, operation: str) -> str:
    candidate = etag.normalize_strong(value)
    if candidate is None:
        raise TodoError(
            f"the server returned no strong quoted ETag for this task, so {operation} "
            "cannot be made conditional",
            exits.MALFORMED_RESPONSE,
        )
    return candidate


def _reject_run_resource(reference: TodoRef, operation: str) -> None:
    if "REFID" in reference.unsupported:
        raise TodoError(
            f"ordinary task {operation} refuses REFID-bearing resources; use `task run`",
            exits.UNSUPPORTED_STRUCTURE,
        )


def plan_update(
    profile: Any,
    *,
    session: Session,
    href: str,
    changes: dict[str, Any],
    now: dt.datetime | None = None,
) -> plans.Plan:
    reference, raw = fetch(profile, session=session, href=href)
    _reject_run_resource(reference, "mutation")
    strong = _require_etag(reference, "an update")
    payload = patch_todo(raw, changes, now=now)
    updated = _describe(
        payload.encode("utf-8"),
        calendar_href=reference.calendar_href,
        href=reference.href,
        etag=strong,
    )
    return plans.write_bundle(
        profile=profile.name,
        summary=updated.summary,
        steps=(
            plans.freeze_step(
                action="task.update",
                href=reference.href,
                etag=strong,
                summary=updated.summary,
                payload=payload.encode("utf-8"),
                content_type="text/calendar; charset=utf-8",
                details=_details(updated),
            ),
        ),
    )


def plan_complete(
    profile: Any,
    *,
    session: Session,
    href: str,
    completed: dt.datetime | None = None,
) -> plans.Plan:
    reference, raw = fetch(profile, session=session, href=href)
    _reject_run_resource(reference, "completion")
    strong = _require_etag(reference, "a completion")
    captured = _stamp_instant(completed or dt.datetime.now(dt.UTC))
    payload = patch_todo(
        raw,
        {
            "STATUS": "COMPLETED",
            "COMPLETED": captured,
            "PERCENT-COMPLETE": 100,
        },
        now=captured,
    )
    updated = _describe(
        payload.encode("utf-8"),
        calendar_href=reference.calendar_href,
        href=reference.href,
        etag=strong,
    )
    return plans.write_bundle(
        profile=profile.name,
        summary=updated.summary,
        steps=(
            plans.freeze_step(
                action="task.complete",
                href=reference.href,
                etag=strong,
                summary=updated.summary,
                payload=payload.encode("utf-8"),
                content_type="text/calendar; charset=utf-8",
                details=_details(updated),
            ),
        ),
    )


def plan_delete(profile: Any, *, session: Session, href: str) -> plans.Plan:
    reference, _ = fetch(profile, session=session, href=href)
    _reject_run_resource(reference, "deletion")
    strong = _require_etag(reference, "a deletion")
    return plans.write_bundle(
        profile=profile.name,
        summary=reference.summary,
        steps=(
            plans.freeze_step(
                action="task.delete",
                href=reference.href,
                etag=strong,
                summary=reference.summary,
                details=_details(reference),
            ),
        ),
    )


def _verify_deleted(session: Session, href: str) -> None:
    try:
        response = session.request(
            "GET",
            href,
            headers={"Accept": "text/calendar"},
            max_redirects=0,
        )
        _refuse_redirect(response, action="post-delete readback", href=href)
    except (SessionError, TodoError) as exc:
        raise TodoError(
            f"the absence of {href} could not be verified after deletion",
            exits.OUTCOME_UNCERTAIN,
        ) from exc
    if response.status == 404:
        return
    raise TodoError(
        f"the server returned status {response.status} while verifying that {href} was deleted",
        exits.OUTCOME_UNCERTAIN,
    )


def _readback(profile: Any, *, session: Session, step: plans.Step) -> TodoRef:
    try:
        stored, stored_raw = fetch(profile, session=session, href=step.href)
    except (SessionError, TodoError) as exc:
        raise TodoError(
            f"the server accepted {step.action}, but its exact task readback could not be verified",
            exits.OUTCOME_UNCERTAIN,
        ) from exc
    try:
        matches = ical_semantics.calendar(stored_raw) == ical_semantics.calendar(
            plans.payload_bytes(step)
        )
    except ValueError as exc:
        raise TodoError(
            f"the server accepted {step.action}, but its task content could not be compared",
            exits.OUTCOME_UNCERTAIN,
        ) from exc
    if not matches:
        raise TodoError(
            f"the server stored different semantic task content at {step.href}",
            exits.OUTCOME_UNCERTAIN,
        )
    return stored


_ACTIONS = {"task.create", "task.update", "task.complete", "task.delete"}


def validate_step(step: plans.Step) -> None:
    """Validate task step structure without reading the server."""
    if step.action not in _ACTIONS:
        raise plans.PlanError(f"unknown task plan action {step.action!r}", exits.USAGE)
    body = plans.payload_bytes(step)
    if step.action == "task.delete":
        if body:
            raise plans.PlanError("task deletion steps must not carry a payload", exits.PLAN_STALE)
        _require_strong_etag(step.etag, "a deletion")
        return
    if not step.content_type or not body:
        raise plans.PlanError("task write steps need content and a payload", exits.PLAN_STALE)
    if step.action == "task.create" and step.etag:
        raise plans.PlanError("task creates cannot carry an ETag", exits.PLAN_STALE)
    if step.action in {"task.update", "task.complete"}:
        _require_strong_etag(step.etag, "an update")


def _task_target(profile: Any, step: plans.Step) -> str:
    return _scoped_href(profile, step.href)


def execute(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Execute one frozen task step conditionally and verify its readback."""
    validate_step(step)
    target = _task_target(profile, step)
    if step.action == "task.delete":
        response = session.request(
            "DELETE",
            target,
            headers={"If-Match": step.etag},
            max_redirects=0,
        )
        _refuse_redirect(response, action="deletion", href=target)
    elif step.action == "task.create":
        response = session.request(
            "PUT",
            target,
            headers={"Content-Type": step.content_type, "If-None-Match": "*"},
            data=plans.payload_bytes(step),
            max_redirects=0,
        )
        _refuse_redirect(response, action="creation", href=target)
    else:
        response = session.request(
            "PUT",
            target,
            headers={"Content-Type": step.content_type, "If-Match": step.etag},
            data=plans.payload_bytes(step),
            max_redirects=0,
        )
        _refuse_redirect(response, action="update", href=target)

    if response.status == 412:
        raise plans.PlanError(
            f"the task at {target} changed since the plan was made; re-plan against "
            "its current state",
            exits.CONFLICT,
        )
    if response.status == 404 and step.action != "task.create":
        raise TodoError(f"no task exists at {target}", exits.TARGET_NOT_FOUND)
    if response.status not in {200, 201, 204}:
        raise TodoError(
            f"the server refused the {step.action} with status {response.status}",
            exits.SERVER_ERROR,
        )

    result: dict[str, Any] = {
        "action": step.action,
        "href": target,
        "uid": step.details.get("uid", ""),
    }
    if step.action == "task.delete":
        _verify_deleted(session, target)
        result["verified"] = "deleted"
        return result

    stored = _readback(profile, session=session, step=step)
    result["etag"] = stored.etag
    result["task"] = stored.as_dict()
    result["verified"] = True
    return result


def reconcile(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Read the exact task and classify the frozen task operation."""
    validate_step(step)
    target = _task_target(profile, step)
    try:
        stored, raw = fetch(profile, session=session, href=target)
    except TodoError as exc:
        if exc.code == exits.TARGET_NOT_FOUND and step.action == "task.create":
            return {"state": "pending"}
        if exc.code == exits.TARGET_NOT_FOUND and step.action == "task.delete":
            return {"state": "verified"}
        if exc.code == exits.TARGET_NOT_FOUND:
            return {"state": "uncertain"}
        raise
    try:
        exact = ical_semantics.calendar(raw) == ical_semantics.calendar(plans.payload_bytes(step))
    except ValueError as exc:
        raise TodoError(
            "the task could not be semantically compared", exits.MALFORMED_RESPONSE
        ) from exc
    if step.action == "task.create":
        return {"state": "verified" if exact else "uncertain"}
    current_etag = etag.normalize_strong(stored.etag)
    old_etag = etag.normalize_strong(step.etag)
    if step.action in {"task.update", "task.complete"}:
        if exact:
            return {"state": "verified"}
        return {"state": "pending" if current_etag == old_etag else "uncertain"}
    if current_etag == old_etag:
        return {"state": "pending"}
    return {"state": "uncertain"}


def apply(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Execute one step for callers that use the resource module directly."""
    return execute(profile, session=session, step=step)
