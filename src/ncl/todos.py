"""Reading and mutating VTODO resources over CalDAV.

Tasks share a calendar collection with events, but their component model is
different. This module owns that model and only reuses canonical hrefs,
allowlist checks, ETags, and the generic frozen-plan store.
"""

from __future__ import annotations

import datetime as dt
import secrets as token_source
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from typing import Any

import icalendar

from . import exits, plans, profiles
from .caldav import CalendarError, _canonical
from .identity import CALDAV, DAV, _element_name, _status_code
from .session import Session

PRODID = "-//ai-agent-nextcloud//ncl//EN"

TODO_STATUSES = ("NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED")
PRIORITY_RANGE = range(0, 10)
PERCENT_RANGE = range(0, 101)

# These properties either describe a recurring series or participate in
# iCalendar scheduling. The parser names them in output, and mutation refuses
# the whole resource before changing any modeled property.
UNSUPPORTED_PROPERTIES = (
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


def _parse_todo(raw: bytes, *, href: str) -> tuple[icalendar.Calendar, Any]:
    try:
        parsed = icalendar.Calendar.from_ical(raw)
    except (IndexError, TypeError, ValueError) as exc:
        raise TodoError(f"the task at {href} was not valid iCalendar") from exc
    todos = [item for item in parsed.walk() if item.name == "VTODO"]
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

    data = b""
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
                data = (element.text or "").encode("utf-8")
    return raw_href, data if data else b""


def _validate_status(value: str) -> str:
    normalized = value.upper()
    if normalized not in TODO_STATUSES:
        raise TodoError(
            f"status must be one of {', '.join(TODO_STATUSES)}; got {value!r}",
            exits.USAGE,
        )
    return normalized


def _validate_priority(value: int) -> int:
    if value not in PRIORITY_RANGE:
        raise TodoError(f"priority must be 0 (unspecified) to 9; got {value}", exits.USAGE)
    return value


def _validate_percent(value: int) -> int:
    if value not in PERCENT_RANGE:
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
    now: dt.datetime | None = None,
    sequence: int = 0,
) -> str:
    """Serialize one VTODO while leaving recurrence and scheduling absent."""
    calendar = icalendar.Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    todo = icalendar.Todo()
    todo.add("uid", uid)
    todo.add("summary", summary)
    todo.add("dtstamp", _stamp_instant(now or dt.datetime.now(dt.UTC)))
    todo.add("sequence", sequence)
    if description:
        todo.add("description", description)
    if start is not None:
        todo.add("dtstart", _stamp(start))
    if due is not None:
        todo.add("due", _stamp(due))
    if priority is not None:
        todo.add("priority", _validate_priority(priority))
    if status:
        todo.add("status", _validate_status(status))
    if percent_complete is not None:
        todo.add("percent-complete", _validate_percent(percent_complete))
    if parent_uid:
        todo.add("related-to", parent_uid, parameters={"RELTYPE": "PARENT"})
    calendar.add_component(todo)
    return calendar.to_ical().decode("utf-8")


def _todo_for_mutation(raw: bytes) -> tuple[icalendar.Calendar, Any]:
    parsed, component = _parse_todo(raw, href="the stored resource")
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


def _scoped_href(profile: Any, href: str) -> str:
    target = _canonical(profile, href)
    if not profiles.in_scope(target, list(profile.calendars)):
        raise CalendarError(
            f"{target} is outside this profile's calendar allowlist", exits.SCOPE_DENIED
        )
    return target


def query(
    profile: Any,
    *,
    session: Session,
    calendar_href: str,
    statuses: tuple[str, ...] = (),
    collection_writable: bool | None = None,
) -> list[TodoRef]:
    """List tasks with one depth-one VTODO REPORT and no per-task reads."""
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

    found: list[TodoRef] = []
    for entry in root:
        if _element_name(entry) != (DAV, "response"):
            continue
        raw_href, data = _entry_data(entry)
        if not data:
            continue
        href = _canonical(profile, raw_href)
        found.append(
            _describe(
                data,
                calendar_href=calendar_href,
                href=href,
                etag=_entry_etag(entry),
                collection_writable=collection_writable,
            )
        )

    children: dict[str, list[str]] = {}
    for task in found:
        if task.parent_uid and task.uid:
            children.setdefault(task.parent_uid, []).append(task.uid)
    found = [
        replace(task, children=tuple(sorted(children.get(task.uid, [])))) for task in found
    ]

    normalized = tuple(_validate_status(status) for status in statuses)
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


def fetch(profile: Any, *, session: Session, href: str) -> tuple[TodoRef, bytes]:
    """Read exactly one task resource and retain its original bytes."""
    target = _scoped_href(profile, href)
    response = session.request("GET", target, headers={"Accept": "text/calendar"})
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
    return plans.write(
        profile=profile.name,
        action="task.create",
        href=reference.href,
        etag="",
        summary=summary,
        payload=payload.encode("utf-8"),
        content_type="text/calendar; charset=utf-8",
        details=_details(reference),
    )


def _require_etag(reference: TodoRef, operation: str) -> None:
    if not reference.etag:
        raise TodoError(
            f"the server returned no ETag for this task, so {operation} cannot be made conditional",
            exits.MALFORMED_RESPONSE,
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
    _require_etag(reference, "an update")
    payload = patch_todo(raw, changes, now=now)
    updated = _describe(
        payload.encode("utf-8"),
        calendar_href=reference.calendar_href,
        href=reference.href,
        etag=reference.etag,
    )
    return plans.write(
        profile=profile.name,
        action="task.update",
        href=reference.href,
        etag=reference.etag,
        summary=updated.summary,
        payload=payload.encode("utf-8"),
        content_type="text/calendar; charset=utf-8",
        details=_details(updated),
    )


def plan_complete(
    profile: Any,
    *,
    session: Session,
    href: str,
    completed: dt.datetime | None = None,
) -> plans.Plan:
    reference, raw = fetch(profile, session=session, href=href)
    _require_etag(reference, "a completion")
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
        etag=reference.etag,
    )
    return plans.write(
        profile=profile.name,
        action="task.complete",
        href=reference.href,
        etag=reference.etag,
        summary=updated.summary,
        payload=payload.encode("utf-8"),
        content_type="text/calendar; charset=utf-8",
        details=_details(updated),
    )


def plan_delete(profile: Any, *, session: Session, href: str) -> plans.Plan:
    reference, _ = fetch(profile, session=session, href=href)
    _require_etag(reference, "a deletion")
    return plans.write(
        profile=profile.name,
        action="task.delete",
        href=reference.href,
        etag=reference.etag,
        summary=reference.summary,
        details=_details(reference),
    )


def _matches(reference: TodoRef, plan: plans.Plan) -> bool:
    expected = {
        "summary": plan.summary,
        "description": plan.details.get("description", ""),
        "start": plan.details.get("start", ""),
        "due": plan.details.get("due", ""),
        "completed": plan.details.get("completed", ""),
        "percent_complete": plan.details.get("percent_complete", ""),
        "status": plan.details.get("status", ""),
        "priority": plan.details.get("priority", ""),
        "parent_uid": plan.details.get("parent_uid", ""),
    }
    actual = {
        "summary": reference.summary,
        "description": reference.description,
        "start": reference.dtstart,
        "due": reference.due,
        "completed": reference.completed,
        "percent_complete": reference.percent_complete,
        "status": reference.status,
        "priority": reference.priority,
        "parent_uid": reference.parent_uid,
    }
    return actual == expected


def apply(profile: Any, *, session: Session, plan: plans.Plan) -> dict[str, Any]:
    """Execute one frozen task plan conditionally and read successful writes back."""
    if plan.profile != profile.name:
        raise plans.PlanError(
            f"plan {plan.plan_id} was made for profile {plan.profile!r}", exits.USAGE
        )
    plans.check_fresh(plan)
    if not profiles.in_scope(plan.href, list(profile.calendars)):
        raise CalendarError(
            f"{plan.href} is outside this profile's calendar allowlist", exits.SCOPE_DENIED
        )

    if plan.action == "task.delete":
        response = session.request("DELETE", plan.href, headers={"If-Match": plan.etag})
    elif plan.action == "task.create":
        response = session.request(
            "PUT",
            plan.href,
            headers={"Content-Type": plan.content_type, "If-None-Match": "*"},
            data=plans.payload_bytes(plan),
        )
    elif plan.action in {"task.update", "task.complete"}:
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
            f"the task at {plan.href} changed since the plan was made; re-plan against "
            "its current state",
            exits.CONFLICT,
        )
    if response.status == 404 and plan.action != "task.create":
        raise TodoError(f"no task exists at {plan.href}", exits.TARGET_NOT_FOUND)
    if response.status not in {200, 201, 204}:
        raise TodoError(
            f"the server refused the {plan.action} with status {response.status}",
            exits.SERVER_ERROR,
        )

    result: dict[str, Any] = {
        "action": plan.action,
        "href": plan.href,
        "uid": plan.details.get("uid", ""),
    }
    if plan.action == "task.delete":
        result["verified"] = "deleted"
        plans.consume(plan.plan_id)
        return result

    stored, _ = fetch(profile, session=session, href=plan.href)
    result["etag"] = stored.etag
    result["task"] = stored.as_dict()
    result["verified"] = _matches(stored, plan)
    if not result["verified"]:
        raise TodoError(
            f"the server stored different task fields at {plan.href}", exits.OUTCOME_UNCERTAIN
        )
    plans.consume(plan.plan_id)
    return result
