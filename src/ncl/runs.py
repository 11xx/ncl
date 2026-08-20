"""Ordered VTODO checkpoint runs over CalDAV.

Runs are standard iCalendar graphs: a manifest VTODO points to the first
checkpoint with ``FIRST``, checkpoints point to the manifest with ``PARENT``,
and each checkpoint points forward with ``NEXT``. This module owns that graph
and the run-specific mutations while the generic plan store owns persistence,
locking, progress, and reconciliation.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets as token_source
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import icalendar

from . import etag, exits, ical_semantics, plans, todos
from .caldav import _canonical
from .session import Session, SessionError

CONTENT_TYPE = "text/calendar; charset=utf-8"
MISSING = object()

_DURATION = re.compile(
    r"^P(?:(?P<weeks>[0-9]+)W|(?:(?P<days>[0-9]+)D)?"
    r"(?:T(?:(?P<hours>[0-9]+)H)?(?:(?P<minutes>[0-9]+)M)?"
    r"(?:(?P<seconds>[0-9]+)S)?)?)$",
    re.IGNORECASE,
)
_RELATION_PARAMETER_NAMES = frozenset({"RELTYPE", "VALUE", "GAP"})
_RELATION_TYPES = frozenset({"FIRST", "NEXT", "PARENT"})


class RunError(todos.TodoError):
    """A run graph or run mutation was not usable."""


@dataclass(frozen=True)
class Relation:
    """One validated standard run relationship."""

    reltype: str
    target: str
    gap: str | None = None


@dataclass(frozen=True)
class RunResource:
    """One report resource parsed as a run member."""

    resource: todos.TodoResource
    component: Any
    refid: str
    raw_status: str
    status: str
    percent_complete: int | str
    relations: tuple[Relation, ...]

    @property
    def reference(self) -> todos.TodoRef:
        return self.resource.reference

    @property
    def raw(self) -> bytes:
        return self.resource.raw

    @property
    def uid(self) -> str:
        return self.reference.uid

    @property
    def href(self) -> str:
        return self.reference.href

    def relation(self, reltype: str) -> Relation | None:
        matches = [item for item in self.relations if item.reltype == reltype]
        if len(matches) > 1:
            raise RunError(
                f"run resource {self.href} has duplicate {reltype} relations",
                exits.MALFORMED_RESPONSE,
            )
        return matches[0] if matches else None


@dataclass(frozen=True)
class RunGraph:
    """A validated rooted linear run."""

    calendar_href: str
    root: RunResource
    steps: tuple[RunResource, ...]
    gaps: tuple[str | None, ...]

    @property
    def current_index(self) -> int | None:
        for index, step in enumerate(self.steps):
            if step.status not in {"COMPLETED", "CANCELLED"}:
                return index
        return None

    @property
    def current_uid(self) -> str | None:
        index = self.current_index
        return self.steps[index].uid if index is not None else None

    @property
    def terminal_counts(self) -> dict[str, int]:
        completed = sum(step.status == "COMPLETED" for step in self.steps)
        cancelled = sum(step.status == "CANCELLED" for step in self.steps)
        return {
            "completed": completed,
            "cancelled": cancelled,
            "terminal": completed + cancelled,
            "nonterminal": len(self.steps) - completed - cancelled,
        }

    def _resource_dict(self, member: RunResource) -> dict[str, Any]:
        value = member.reference.as_dict()
        value["unsupported"] = [
            name for name in value["unsupported"] if name != "REFID"
        ]
        value["refid"] = member.refid
        value["raw_status"] = member.raw_status
        value["status"] = member.status
        value["percent_complete"] = member.percent_complete
        return value

    def as_dict(self) -> dict[str, Any]:
        current = self.current_index
        steps: list[dict[str, Any]] = []
        states: dict[str, dict[str, Any]] = {}
        for index, (step, gap) in enumerate(zip(self.steps, (*self.gaps, None), strict=True)):
            value = self._resource_dict(step)
            value["position"] = index + 1
            value["gap_to_next"] = "unknown" if gap is None and index < len(self.gaps) else gap
            value["terminal"] = step.status in {"COMPLETED", "CANCELLED"}
            steps.append(value)
            states[step.uid] = {
                "status": step.raw_status,
                "effective_status": step.status,
                "percent_complete": step.percent_complete,
            }
        root = self._resource_dict(self.root)
        root["raw_status"] = ""
        root["status"] = ""
        root["percent_complete"] = ""
        return {
            "href": self.root.href,
            "uid": self.root.uid,
            "refid": self.root.refid,
            "summary": self.root.reference.summary,
            "calendar_href": self.calendar_href,
            "root": root,
            "steps": steps,
            "gaps": ["unknown" if gap is None else gap for gap in self.gaps],
            "raw_state": states,
            "terminal_counts": self.terminal_counts,
            "current_uid": self.current_uid,
            "current_position": current + 1 if current is not None else None,
        }


@dataclass(frozen=True)
class RunNoOp:
    """A valid run command that intentionally emitted no write plan."""

    result: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return self.result


def _run_error(message: str, code: int = exits.MALFORMED_RESPONSE) -> RunError:
    return RunError(message, code)


def _parameter(parameters: Any, name: str, *, href: str) -> str | None:
    key = next((candidate for candidate in parameters if str(candidate).upper() == name), None)
    if key is None:
        return None
    value = parameters[key]
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise _run_error(
                f"run relation at {href} has repeated {name} parameters"
            )
        value = value[0]
    return str(value)


def _parse_gap(value: str, *, href: str) -> str:
    if not isinstance(value, str):
        raise _run_error(f"run relation at {href} has a non-text GAP parameter")
    candidate = value.upper()
    match = _DURATION.fullmatch(candidate)
    if match is None:
        raise _run_error(f"run relation at {href} has an invalid GAP {value!r}")
    parts = match.groupdict()
    if not any(item is not None for item in parts.values()):
        raise _run_error(f"run relation at {href} has an empty GAP")
    if "T" in candidate and not any(
        parts[name] is not None for name in ("hours", "minutes", "seconds")
    ):
        raise _run_error(f"run relation at {href} has an empty time GAP")
    try:
        duration = dt.timedelta(
            weeks=int(parts["weeks"] or 0),
            days=int(parts["days"] or 0),
            hours=int(parts["hours"] or 0),
            minutes=int(parts["minutes"] or 0),
            seconds=int(parts["seconds"] or 0),
        )
    except OverflowError as exc:
        raise _run_error(f"run relation at {href} has an unrepresentable GAP") from exc
    if duration < dt.timedelta(0):
        raise _run_error(f"run relation at {href} has a negative GAP")
    return candidate


def _argument_gap(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value.lower() == "unknown":
        if isinstance(value, str) and value.lower() == "unknown":
            return None
        raise RunError(f"{label} must be a nonnegative duration or unknown", exits.USAGE)
    return _parse_gap(value, href=label)


def _argument_gaps(
    values: list[str] | None, expected: int, *, label: str
) -> tuple[str | None, ...]:
    if values is None:
        return (None,) * expected
    if len(values) != expected:
        raise RunError(
            f"{label} needs exactly {expected} value(s); got {len(values)}", exits.USAGE
        )
    return tuple(_argument_gap(value, label=label) for value in values)


def _relation_parameters(value: Any, *, href: str) -> tuple[str, str, str | None]:
    parameters = getattr(value, "params", {})
    unknown = {
        str(name).upper()
        for name in parameters
        if str(name).upper() not in _RELATION_PARAMETER_NAMES
    }
    if unknown:
        names = ", ".join(sorted(unknown))
        raise _run_error(f"run relation at {href} has unsupported parameter(s): {names}")
    reltype = _parameter(parameters, "RELTYPE", href=href)
    relation_value = _parameter(parameters, "VALUE", href=href)
    gap = _parameter(parameters, "GAP", href=href)
    if reltype is None or reltype.upper() not in _RELATION_TYPES:
        raise _run_error(f"run relation at {href} has an invalid RELTYPE")
    if relation_value is None or relation_value.upper() != "UID":
        raise _run_error(f"run relation at {href} must use VALUE=UID")
    target = str(value).strip()
    if not target:
        raise _run_error(f"run relation at {href} has an empty target")
    if "://" in target or target.startswith(("/", "\\")):
        raise _run_error(
            f"run relation at {href} targets another collection instead of a UID",
            exits.SCOPE_DENIED,
        )
    relation = reltype.upper()
    if gap is not None:
        if relation != "NEXT":
            raise _run_error(f"{relation} relation at {href} cannot carry GAP")
        gap = _parse_gap(gap, href=href)
    return relation, target, gap


def _relations(component: Any, *, href: str) -> tuple[Relation, ...]:
    found: list[Relation] = []
    for name, value in component.property_items():
        if name.upper() != "RELATED-TO":
            continue
        relation, target, gap = _relation_parameters(value, href=href)
        found.append(Relation(relation, target, gap))
    return tuple(found)


def _effective_status(component: Any, *, href: str) -> tuple[str, str, int | str]:
    status_property = component.get("STATUS")
    raw_status = str(status_property).upper() if status_property is not None else ""
    status = raw_status or "NEEDS-ACTION"
    if status not in {"NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED"}:
        raise _run_error(f"run checkpoint at {href} has malformed STATUS {raw_status!r}")
    percent = todos._component_integer(component, "PERCENT-COMPLETE")
    completed = component.get("COMPLETED")
    if status == "COMPLETED":
        if completed is None or percent != 100:
            raise _run_error(
                f"run checkpoint at {href} has COMPLETED state without 100 percent and a timestamp"
            )
    elif completed is not None:
        raise _run_error(f"run checkpoint at {href} has COMPLETED outside terminal state")
    if status != "COMPLETED" and percent == 100:
        raise _run_error(f"run checkpoint at {href} has 100 percent before completion")
    return raw_status, status, percent


def _parse_run_resource(resource: todos.TodoResource) -> RunResource:
    parsed, component = todos._parse_todo(resource.raw, href=resource.reference.href)
    unsupported = set(todos._unsupported(component, parsed)) - {"REFID"}
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise RunError(
            f"run resource {resource.reference.href} carries unsupported structure: {names}",
            exits.UNSUPPORTED_STRUCTURE,
        )
    refid_property = component.get("REFID")
    if refid_property is None or not str(refid_property).strip():
        raise _run_error(
            f"run resource {resource.reference.href} has no nonempty REFID"
        )
    if getattr(refid_property, "params", {}):
        raise _run_error(f"run resource {resource.reference.href} has REFID parameters")
    raw_status, status, percent = _effective_status(component, href=resource.reference.href)
    return RunResource(
        resource=resource,
        component=component,
        refid=str(refid_property),
        raw_status=raw_status,
        status=status,
        percent_complete=percent,
        relations=_relations(component, href=resource.reference.href),
    )


def _looks_like_run_member(resource: todos.TodoResource) -> bool:
    """Recognize run-only edge types even when REFID is missing."""
    _, component = todos._parse_todo(resource.raw, href=resource.reference.href)
    for name, value in component.property_items():
        if name.upper() != "RELATED-TO":
            continue
        parameters = getattr(value, "params", {})
        relation = str(parameters.get("RELTYPE", "")).upper()
        relation_value = str(parameters.get("VALUE", "")).upper()
        if relation in {"FIRST", "NEXT"} or (
            relation == "PARENT" and relation_value == "UID"
        ):
            return True
    return False


def _require_relation_count(
    member: RunResource, relation_type: str, count: int, *, message: str
) -> None:
    if sum(item.reltype == relation_type for item in member.relations) != count:
        raise _run_error(f"run resource {member.href} {message}")


def _build_graph(calendar_href: str, members: list[RunResource], refid: str) -> RunGraph:
    candidates = [member for member in members if member.uid == refid]
    if len(candidates) != 1:
        raise _run_error(
            f"REFID {refid!r} does not identify exactly one run root",
            exits.AMBIGUOUS_TARGET if len(candidates) > 1 else exits.MALFORMED_RESPONSE,
        )
    root = candidates[0]
    if root.refid != root.uid:
        raise _run_error(f"run root {root.href} has a mismatched REFID")
    if root.component.get("STATUS") is not None:
        raise _run_error(f"run root {root.href} must not carry STATUS")
    if root.component.get("PERCENT-COMPLETE") is not None:
        raise _run_error(f"run root {root.href} must not carry PERCENT-COMPLETE")
    if root.component.get("COMPLETED") is not None:
        raise _run_error(f"run root {root.href} must not carry COMPLETED")
    _require_relation_count(root, "FIRST", 1, message="must have exactly one FIRST relation")
    if any(item.reltype != "FIRST" for item in root.relations):
        raise _run_error(f"run root {root.href} has a relation other than FIRST")
    steps = [member for member in members if member is not root]
    if not steps:
        raise _run_error(f"run root {root.href} has no checkpoints")
    by_uid = {member.uid: member for member in members}
    first = root.relation("FIRST")
    assert first is not None
    if first.gap is not None:
        raise _run_error(f"run root {root.href} FIRST relation cannot carry GAP")
    if first.target not in by_uid:
        raise _run_error(f"run root {root.href} FIRST target {first.target!r} is missing")
    if first.target == root.uid:
        raise _run_error(f"run root {root.href} FIRST target points to the root")

    outgoing: dict[str, Relation] = {}
    incoming: dict[str, int] = defaultdict(int)
    for member in steps:
        if member.refid != refid:
            raise _run_error(
                f"run resource {member.href} has REFID {member.refid!r}, expected {refid!r}"
            )
        _require_relation_count(
            member, "PARENT", 1, message="must have exactly one PARENT relation"
        )
        parent = member.relation("PARENT")
        assert parent is not None
        if parent.target != root.uid or parent.gap is not None:
            raise _run_error(f"run checkpoint {member.href} has a wrong PARENT relation")
        if any(item.reltype not in {"PARENT", "NEXT"} for item in member.relations):
            raise _run_error(f"run checkpoint {member.href} has an unsupported relation")
        next_relations = [item for item in member.relations if item.reltype == "NEXT"]
        if len(next_relations) > 1:
            raise _run_error(f"run checkpoint {member.href} has multiple outgoing NEXT edges")
        if next_relations:
            next_relation = next_relations[0]
            if next_relation.target not in by_uid or next_relation.target == root.uid:
                raise _run_error(
                    f"run checkpoint {member.href} NEXT target {next_relation.target!r} is missing"
                )
            outgoing[member.uid] = next_relation
            incoming[next_relation.target] += 1

    first_uid = first.target
    if incoming.get(first_uid, 0):
        raise _run_error("run chain has a branch or cycle entering its FIRST checkpoint")
    for member in steps:
        if member.uid != first_uid and incoming.get(member.uid, 0) != 1:
            raise _run_error(
                f"run checkpoint {member.href} is disconnected or has the wrong incoming edge"
            )

    ordered: list[RunResource] = []
    seen: set[str] = set()
    current_uid = first_uid
    while True:
        if current_uid in seen:
            raise _run_error(f"run chain contains a cycle at UID {current_uid!r}")
        current = by_uid.get(current_uid)
        if current is None or current is root:
            raise _run_error(f"run chain target {current_uid!r} is missing")
        seen.add(current_uid)
        ordered.append(current)
        next_relation = outgoing.get(current_uid)
        if next_relation is None:
            break
        current_uid = next_relation.target
    if len(ordered) != len(steps):
        raise _run_error("run graph contains disconnected checkpoints")
    gaps = tuple(
        outgoing[member.uid].gap if member.uid in outgoing else None
        for member in ordered[:-1]
    )
    return RunGraph(calendar_href, root, tuple(ordered), gaps)


def _graphs(resources: list[todos.TodoResource], calendar_href: str) -> list[RunGraph]:
    grouped: dict[str, list[RunResource]] = defaultdict(list)
    for resource in resources:
        if not any(name == "REFID" for name in resource.reference.unsupported):
            # Ordinary VTODOs remain outside run graph validation unless they
            # carry one of the run-only edge types.
            if _looks_like_run_member(resource):
                raise _run_error(
                    f"run-shaped resource {resource.reference.href} has no REFID"
                )
            continue
        member = _parse_run_resource(resource)
        grouped[member.refid].append(member)
    return sorted(
        (_build_graph(calendar_href, members, refid) for refid, members in grouped.items()),
        key=lambda graph: graph.root.href,
    )


def _collection_for_root(profile: Any, href: str) -> tuple[str, str]:
    target = todos._scoped_href(profile, href)
    if "/" not in target.rstrip("/"):
        raise RunError("a run root must be a resource href", exits.USAGE)
    collection = _canonical(profile, target.rsplit("/", 1)[0] + "/")
    todos._check_calendar_scope(profile, collection)
    todos._check_response_scope(profile, collection, target)
    return collection, target


def _read_graphs(
    profile: Any, *, session: Session, calendar_href: str, collection_writable: bool | None = None
) -> list[RunGraph]:
    resources = todos.query_resources(
        profile,
        session=session,
        calendar_href=calendar_href,
        collection_writable=collection_writable,
    )
    return _graphs(resources, _canonical(profile, calendar_href))


def list_runs(
    profile: Any,
    *,
    session: Session,
    calendar_href: str,
    collection_writable: bool | None = None,
) -> list[RunGraph]:
    """List validated runs using one collection report."""
    return _read_graphs(
        profile,
        session=session,
        calendar_href=calendar_href,
        collection_writable=collection_writable,
    )


def show_run(profile: Any, *, session: Session, root_href: str) -> RunGraph:
    """Read and validate one root and all members from one collection report."""
    collection, target = _collection_for_root(profile, root_href)
    for graph in _read_graphs(profile, session=session, calendar_href=collection):
        if graph.root.href == target:
            return graph
    raise RunError(f"no run root exists at {target}", exits.TARGET_NOT_FOUND)


def _graph_for_resource(
    profile: Any, *, session: Session, href: str
) -> tuple[RunGraph, RunResource]:
    collection, target = _collection_for_root(profile, href)
    for graph in _read_graphs(profile, session=session, calendar_href=collection):
        if graph.root.href == target:
            return graph, graph.root
        for step in graph.steps:
            if step.href == target:
                return graph, step
    raise RunError(f"no run resource exists at {target}", exits.TARGET_NOT_FOUND)


def _strong(member: RunResource, operation: str) -> str:
    value = etag.normalize_strong(member.reference.etag)
    if value is None:
        raise RunError(
            f"the server returned no strong quoted ETag for {operation} at {member.href}",
            exits.MALFORMED_RESPONSE,
        )
    return value


def _touch(component: Any, *, now: dt.datetime) -> None:
    component.pop("DTSTAMP", None)
    component.add("DTSTAMP", todos._stamp_instant(now))
    try:
        sequence = int(str(component.get("SEQUENCE", 0)))
    except (TypeError, ValueError):
        sequence = 0
    component.pop("SEQUENCE", None)
    component.add("SEQUENCE", sequence + 1)


def _component_for_patch(raw: bytes, *, href: str) -> tuple[icalendar.Calendar, Any]:
    parsed, component = todos._parse_todo(raw, href=href)
    if component.get("REFID") is None:
        raise _run_error(f"run update payload at {href} has no REFID", exits.PLAN_STALE)
    unsupported = set(todos._unsupported(component, parsed)) - {"REFID"}
    if unsupported:
        raise RunError(
            f"run update payload at {href} carries unsupported structure: "
            f"{', '.join(sorted(unsupported))}",
            exits.PLAN_STALE,
        )
    return parsed, component


def _patch_content(raw: bytes, changes: dict[str, Any], *, href: str, now: dt.datetime) -> bytes:
    calendar, component = _component_for_patch(raw, href=href)
    allowed = {"SUMMARY", "DESCRIPTION", "DTSTART", "DUE", "PRIORITY"}
    for name, requested in changes.items():
        key = name.upper()
        if key not in allowed:
            raise RunError(
                f"run edit cannot change {key}; relationships and execution state are frozen",
                exits.USAGE,
            )
        if requested is None or requested == "":
            component.pop(key, None)
            continue
        value = requested
        if key in {"DTSTART", "DUE"}:
            value = todos._stamp(value)
        elif key == "PRIORITY":
            value = todos._validate_priority(value)
        component.pop(key, None)
        component.add(key, value)
    _touch(component, now=now)
    return calendar.to_ical()


def _replace_relation(
    raw: bytes,
    *,
    href: str,
    relation_type: str,
    target: str | None,
    gap: str | None = None,
    now: dt.datetime,
) -> bytes:
    calendar, component = _component_for_patch(raw, href=href)
    retained: list[tuple[Any, dict[str, Any]]] = []
    for name, value in component.property_items():
        if name.upper() != "RELATED-TO":
            continue
        params = dict(getattr(value, "params", {}))
        relation = str(params.get("RELTYPE", "")).upper()
        if relation != relation_type:
            retained.append((value, params))
    component.pop("RELATED-TO", None)
    for value, params in retained:
        component.add("RELATED-TO", value, parameters=params)
    if target is not None:
        parameters: dict[str, Any] = {"RELTYPE": relation_type, "VALUE": "UID"}
        if gap is not None:
            parameters["GAP"] = gap
        component.add("RELATED-TO", target, parameters=parameters)
    _touch(component, now=now)
    return calendar.to_ical()


def _new_step(
    *,
    uid: str,
    summary: str,
    root_uid: str,
    next_uid: str | None,
    gap: str | None,
    now: dt.datetime,
) -> bytes:
    related = [(root_uid, {"RELTYPE": "PARENT", "VALUE": "UID"})]
    if next_uid is not None:
        parameters: dict[str, Any] = {"RELTYPE": "NEXT", "VALUE": "UID"}
        if gap is not None:
            parameters["GAP"] = gap
        related.append((next_uid, parameters))
    return todos.build_todo(
        uid=uid,
        summary=summary,
        status="NEEDS-ACTION",
        refid=root_uid,
        related_to=tuple(related),
        now=now,
    ).encode()


def _new_root(
    *, uid: str, summary: str, first_uid: str, now: dt.datetime
) -> bytes:
    return todos.build_todo(
        uid=uid,
        summary=summary,
        refid=uid,
        related_to=((first_uid, {"RELTYPE": "FIRST", "VALUE": "UID"}),),
        now=now,
    ).encode()


def _details(
    graph: RunGraph, member: RunResource, *, operation: str, **extra: Any
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "run_uid": graph.root.uid,
        "run_href": graph.root.href,
        "uid": member.uid,
        "operation": operation,
        "kind": "root" if member is graph.root else "checkpoint",
    }
    value.update(extra)
    return value


def _step(
    *,
    action: str,
    member: RunResource | None,
    href: str,
    summary: str,
    payload: bytes,
    details: dict[str, Any],
    create: bool = False,
) -> plans.Step:
    return plans.freeze_step(
        action=action,
        href=href,
        etag="" if create else _strong(member, details["operation"]),
        summary=summary,
        payload=payload,
        content_type=CONTENT_TYPE,
        details=details,
    )


def _write(profile: Any, summary: str, steps: list[plans.Step]) -> plans.Plan:
    return plans.write_bundle(profile=profile.name, summary=summary, steps=steps)


def plan_create(
    profile: Any,
    *,
    calendar_href: str,
    summary: str,
    step_summaries: list[str] | tuple[str, ...],
    gaps: list[str] | None = None,
    now: dt.datetime | None = None,
) -> plans.Plan:
    """Freeze checkpoint creates in chain order followed by the root create."""
    if not step_summaries:
        raise RunError("a run needs at least one --step", exits.USAGE)
    collection = _canonical(profile, calendar_href)
    todos._check_calendar_scope(profile, collection)
    normalized_gaps = _argument_gaps(gaps, len(step_summaries) - 1, label="--gap")
    captured = todos._stamp_instant(now or dt.datetime.now(dt.UTC))
    root_uid = f"{token_source.token_hex(16)}@ncl-run"
    step_uids = [f"{token_source.token_hex(16)}@ncl-step" for _ in step_summaries]
    steps: list[plans.Step] = []
    for index, (step_uid, step_summary) in enumerate(zip(step_uids, step_summaries, strict=True)):
        payload = _new_step(
            uid=step_uid,
            summary=step_summary,
            root_uid=root_uid,
            next_uid=step_uids[index + 1] if index + 1 < len(step_uids) else None,
            gap=normalized_gaps[index] if index < len(normalized_gaps) else None,
            now=captured,
        )
        href = todos._resource_href(collection, step_uid)
        reference = todos._describe(payload, calendar_href=collection, href=href, etag="")
        member = RunResource(
            todos.TodoResource(reference, payload),
            icalendar.Calendar.from_ical(payload).subcomponents[-1],
            root_uid,
            "NEEDS-ACTION",
            "NEEDS-ACTION",
            "",
            (),
        )
        steps.append(
            _step(
                action="run.create",
                member=member,
                href=href,
                summary=step_summary,
                payload=payload,
                details={
                    "run_uid": root_uid,
                    "uid": step_uid,
                    "operation": "creation",
                    "kind": "checkpoint",
                    "position": index + 1,
                },
                create=True,
            )
        )
    root_payload = _new_root(uid=root_uid, summary=summary, first_uid=step_uids[0], now=captured)
    root_href = todos._resource_href(collection, root_uid)
    root_reference = todos._describe(
        root_payload, calendar_href=collection, href=root_href, etag=""
    )
    root_member = RunResource(
        todos.TodoResource(root_reference, root_payload),
        icalendar.Calendar.from_ical(root_payload).subcomponents[-1],
        root_uid,
        "",
        "NEEDS-ACTION",
        "",
        (),
    )
    steps.append(
        _step(
            action="run.create",
            member=root_member,
            href=root_href,
            summary=summary,
            payload=root_payload,
            details={
                "run_uid": root_uid,
                "uid": root_uid,
                "operation": "creation",
                "kind": "root",
            },
            create=True,
        )
    )
    return _write(profile, summary, steps)


def _ensure_open(graph: RunGraph) -> None:
    if any(step.status != "NEEDS-ACTION" for step in graph.steps):
        raise RunError(
            f"run {graph.root.uid} is execution-started; authoring is frozen",
            exits.CONFLICT,
        )


def _find_step(profile: Any, graph: RunGraph, href: str, *, label: str) -> RunResource:
    target = _canonical_for_graph(profile, graph, href)
    for step in graph.steps:
        if step.href == target:
            return step
    raise RunError(
        f"{label} {target} is not a checkpoint in run {graph.root.uid}",
        exits.TARGET_NOT_FOUND,
    )


def _canonical_for_graph(profile: Any, graph: RunGraph, href: str) -> str:
    target = todos._scoped_href(profile, href)
    todos._check_response_scope(profile, graph.calendar_href, target)
    return target


def plan_add(
    profile: Any,
    *,
    session: Session,
    root_href: str,
    summary: str,
    before: str | None = None,
    after: str | None = None,
    gap_before: object = MISSING,
    gap_after: object = MISSING,
    now: dt.datetime | None = None,
) -> plans.Plan:
    if before is not None and after is not None:
        raise RunError("--before and --after are mutually exclusive", exits.USAGE)
    graph = show_run(profile, session=session, root_href=root_href)
    _ensure_open(graph)
    count = len(graph.steps)
    if before is not None:
        successor_index = next(
            (
                index
                for index, step in enumerate(graph.steps)
                if step.href == _canonical_for_graph(profile, graph, before)
            ),
            None,
        )
        if successor_index is None:
            raise RunError(
                f"--before {before} is not a checkpoint in this run",
                exits.TARGET_NOT_FOUND,
            )
        predecessor_index = successor_index - 1 if successor_index > 0 else None
    elif after is not None:
        predecessor_index = next(
            (
                index
                for index, step in enumerate(graph.steps)
                if step.href == _canonical_for_graph(profile, graph, after)
            ),
            None,
        )
        if predecessor_index is None:
            raise RunError(
                f"--after {after} is not a checkpoint in this run",
                exits.TARGET_NOT_FOUND,
            )
        successor_index = predecessor_index + 1 if predecessor_index + 1 < count else None
    else:
        predecessor_index = count - 1
        successor_index = None
    if predecessor_index is not None and gap_before is MISSING:
        raise RunError("--gap-before is required when the insertion has a predecessor", exits.USAGE)
    if successor_index is not None and gap_after is MISSING:
        raise RunError("--gap-after is required when the insertion has a successor", exits.USAGE)
    if predecessor_index is None and gap_before is not MISSING:
        raise RunError("--gap-before is not valid before the first checkpoint", exits.USAGE)
    if successor_index is None and gap_after is not MISSING:
        raise RunError("--gap-after is not valid at the end of the run", exits.USAGE)
    previous_gap = _argument_gap(
        gap_before if gap_before is not MISSING else None, label="--gap-before"
    )
    next_gap = _argument_gap(
        gap_after if gap_after is not MISSING else None, label="--gap-after"
    )
    captured = todos._stamp_instant(now or dt.datetime.now(dt.UTC))
    new_uid = f"{token_source.token_hex(16)}@ncl-step"
    successor = graph.steps[successor_index] if successor_index is not None else None
    predecessor = graph.steps[predecessor_index] if predecessor_index is not None else None
    new_payload = _new_step(
        uid=new_uid,
        summary=summary,
        root_uid=graph.root.uid,
        next_uid=successor.uid if successor is not None else None,
        gap=next_gap,
        now=captured,
    )
    new_href = todos._resource_href(graph.calendar_href, new_uid)
    new_reference = todos._describe(
        new_payload, calendar_href=graph.calendar_href, href=new_href, etag=""
    )
    new_member = RunResource(
        todos.TodoResource(new_reference, new_payload),
        icalendar.Calendar.from_ical(new_payload).subcomponents[-1],
        graph.root.uid,
        "NEEDS-ACTION",
        "NEEDS-ACTION",
        "",
        (),
    )
    steps = [
        _step(
            action="run.create",
            member=new_member,
            href=new_href,
            summary=summary,
            payload=new_payload,
            details=_details(graph, new_member, operation="creation", kind="checkpoint"),
            create=True,
        )
    ]
    if predecessor is not None:
        payload = _replace_relation(
            predecessor.raw,
            href=predecessor.href,
            relation_type="NEXT",
            target=new_uid,
            gap=previous_gap,
            now=captured,
        )
        steps.append(
            _step(
                action="run.update",
                member=predecessor,
                href=predecessor.href,
                summary=predecessor.reference.summary,
                payload=payload,
                details=_details(graph, predecessor, operation="edge update", relation="NEXT"),
            )
        )
    if predecessor is None:
        payload = _replace_relation(
            graph.root.raw,
            href=graph.root.href,
            relation_type="FIRST",
            target=new_uid,
            now=captured,
        )
        steps.append(
            _step(
                action="run.update",
                member=graph.root,
                href=graph.root.href,
                summary=graph.root.reference.summary,
                payload=payload,
                details=_details(graph, graph.root, operation="root update", relation="FIRST"),
            )
        )
    return _write(profile, f"Add checkpoint {summary}", steps)


def plan_reorder(
    profile: Any,
    *,
    session: Session,
    root_href: str,
    step_hrefs: list[str] | tuple[str, ...],
    gaps: list[str] | None = None,
    now: dt.datetime | None = None,
) -> plans.Plan | RunNoOp:
    graph = show_run(profile, session=session, root_href=root_href)
    _ensure_open(graph)
    if len(step_hrefs) != len(graph.steps):
        raise RunError(
            f"--step must name every checkpoint exactly once; expected {len(graph.steps)}",
            exits.USAGE,
        )
    canonical_hrefs = [_canonical_for_graph(profile, graph, href) for href in step_hrefs]
    existing = {step.href for step in graph.steps}
    if set(canonical_hrefs) != existing or len(set(canonical_hrefs)) != len(canonical_hrefs):
        raise RunError("--step must name every checkpoint exactly once", exits.USAGE)
    normalized_gaps = _argument_gaps(gaps, len(graph.steps) - 1, label="--gap")
    final_steps = [
        next(step for step in graph.steps if step.href == href) for href in canonical_hrefs
    ]
    captured = todos._stamp_instant(now or dt.datetime.now(dt.UTC))
    steps: list[plans.Step] = []
    for index, member in enumerate(final_steps[:-1]):
        desired_target = final_steps[index + 1].uid
        desired_gap = normalized_gaps[index]
        current = member.relation("NEXT")
        if current is not None and current.target == desired_target and current.gap == desired_gap:
            continue
        payload = _replace_relation(
            member.raw,
            href=member.href,
            relation_type="NEXT",
            target=desired_target,
            gap=desired_gap,
            now=captured,
        )
        steps.append(
            _step(
                action="run.update",
                member=member,
                href=member.href,
                summary=member.reference.summary,
                payload=payload,
                details=_details(graph, member, operation="edge update", relation="NEXT"),
            )
        )
    last = final_steps[-1]
    if last.relation("NEXT") is not None:
        payload = _replace_relation(
            last.raw,
            href=last.href,
            relation_type="NEXT",
            target=None,
            now=captured,
        )
        steps.append(
            _step(
                action="run.update",
                member=last,
                href=last.href,
                summary=last.reference.summary,
                payload=payload,
                details=_details(graph, last, operation="edge update", relation="NEXT"),
            )
        )
    if final_steps[0].uid != graph.steps[0].uid:
        payload = _replace_relation(
            graph.root.raw,
            href=graph.root.href,
            relation_type="FIRST",
            target=final_steps[0].uid,
            now=captured,
        )
        steps.append(
            _step(
                action="run.update",
                member=graph.root,
                href=graph.root.href,
                summary=graph.root.reference.summary,
                payload=payload,
                details=_details(graph, graph.root, operation="root update", relation="FIRST"),
            )
        )
    if not steps:
        return RunNoOp(
            {
                "changed": False,
                "reason": "the requested order is already stored",
                "run": graph.as_dict(),
            }
        )
    return _write(profile, f"Reorder run {graph.root.reference.summary}", steps)


def plan_edit(
    profile: Any,
    *,
    session: Session,
    href: str,
    changes: dict[str, Any],
    now: dt.datetime | None = None,
) -> plans.Plan:
    if not changes:
        raise RunError("no changes were requested", exits.USAGE)
    graph, member = _graph_for_resource(profile, session=session, href=href)
    _ensure_open(graph)
    captured = todos._stamp_instant(now or dt.datetime.now(dt.UTC))
    payload = _patch_content(member.raw, changes, href=member.href, now=captured)
    return _write(
        profile,
        f"Edit run resource {member.reference.summary}",
        [
            _step(
                action="run.update",
                member=member,
                href=member.href,
                summary=member.reference.summary,
                payload=payload,
                details=_details(graph, member, operation="content update"),
            )
        ],
    )


def plan_transition(
    profile: Any,
    *,
    session: Session,
    href: str,
    transition: str,
    at: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> plans.Plan | RunNoOp:
    if transition not in {"done", "skip", "not-yet"}:
        raise RunError(f"unknown run transition {transition!r}", exits.USAGE)
    graph, member = _graph_for_resource(profile, session=session, href=href)
    if member is graph.root:
        raise RunError("run transitions apply only to checkpoints", exits.USAGE)
    if transition == "not-yet" and member.status == "NEEDS-ACTION":
        return RunNoOp(
            {
                "changed": False,
                "reason": "the checkpoint is already NEEDS-ACTION",
                "run": graph.as_dict(),
                "step": member.reference.as_dict(),
            }
        )
    if member.status not in {"NEEDS-ACTION", "IN-PROCESS"}:
        raise RunError(
            f"cannot transition terminal checkpoint {member.uid} from {member.status}",
            exits.CONFLICT,
        )
    captured = todos._stamp_instant(at or now or dt.datetime.now(dt.UTC))
    changes: dict[str, Any]
    if transition == "done":
        changes = {
            "STATUS": "COMPLETED",
            "PERCENT-COMPLETE": 100,
            "COMPLETED": captured,
        }
    elif transition == "skip":
        changes = {"STATUS": "CANCELLED", "COMPLETED": None}
    else:
        changes = {"STATUS": "NEEDS-ACTION", "PERCENT-COMPLETE": 0, "COMPLETED": None}
    payload = _patch_transition(member.raw, changes, href=member.href, now=captured)
    out_of_order = graph.current_uid != member.uid
    return _write(
        profile,
        f"{transition} checkpoint {member.reference.summary}",
        [
            _step(
                action="run.update",
                member=member,
                href=member.href,
                summary=member.reference.summary,
                payload=payload,
                details=_details(
                    graph,
                    member,
                    operation="state transition",
                    transition=transition,
                    out_of_order=out_of_order,
                    current_uid=graph.current_uid,
                    current_position=(
                        graph.current_index + 1
                        if graph.current_index is not None
                        else None
                    ),
                ),
            )
        ],
    )


def _patch_transition(
    raw: bytes, changes: dict[str, Any], *, href: str, now: dt.datetime
) -> bytes:
    calendar, component = _component_for_patch(raw, href=href)
    component.pop("STATUS", None)
    component.add("STATUS", todos._validate_status(changes["STATUS"]))
    if "PERCENT-COMPLETE" in changes:
        component.pop("PERCENT-COMPLETE", None)
        component.add("PERCENT-COMPLETE", todos._validate_percent(changes["PERCENT-COMPLETE"]))
    if "COMPLETED" in changes:
        component.pop("COMPLETED", None)
        value = changes["COMPLETED"]
        if value is not None:
            component.add("COMPLETED", todos._stamp_instant(value))
    _touch(component, now=now)
    return calendar.to_ical()


_ACTIONS = {"run.create", "run.update"}


def validate_step(step: plans.Step) -> None:
    """Validate every frozen run write before the first remote request."""
    if step.action not in _ACTIONS:
        raise plans.PlanError(f"unknown run plan action {step.action!r}", exits.USAGE)
    payload = plans.payload_bytes(step)
    if not step.content_type or not payload:
        raise plans.PlanError("run write steps need content and a payload", exits.PLAN_STALE)
    if step.action == "run.create" and step.etag:
        raise plans.PlanError("run creates cannot carry an ETag", exits.PLAN_STALE)
    if step.action == "run.update":
        candidate = etag.normalize_strong(step.etag)
        if candidate is None:
            raise plans.PlanError(
                "run updates need a strong quoted ETag", exits.PLAN_STALE
            )
    try:
        resource = todos.TodoResource(
            todos._describe(payload, calendar_href="", href=step.href, etag=step.etag), payload
        )
        member = _parse_run_resource(resource)
        kind = step.details.get("kind")
        if kind not in {"root", "checkpoint"}:
            raise _run_error("run plan step has no valid resource kind", exits.PLAN_STALE)
        run_uid = step.details.get("run_uid")
        uid = step.details.get("uid")
        if (
            not isinstance(run_uid, str)
            or not run_uid
            or not isinstance(uid, str)
            or not uid
            or member.refid != run_uid
            or member.uid != uid
        ):
            raise _run_error(
                "run plan metadata does not match its resource payload", exits.PLAN_STALE
            )
        if kind == "root":
            if member.uid != member.refid or member.refid != run_uid:
                raise _run_error("run plan root has mismatched identity", exits.PLAN_STALE)
            if any(
                member.component.get(name) is not None
                for name in ("STATUS", "PERCENT-COMPLETE", "COMPLETED")
            ):
                raise _run_error("run plan root carries execution state", exits.PLAN_STALE)
            if len(member.relations) != 1 or member.relation("FIRST") is None:
                raise _run_error(
                    "run plan root does not have exactly one FIRST", exits.PLAN_STALE
                )
        else:
            if member.refid != run_uid or member.uid == run_uid:
                raise _run_error("run plan checkpoint has mismatched identity", exits.PLAN_STALE)
            parents = [item for item in member.relations if item.reltype == "PARENT"]
            firsts = [item for item in member.relations if item.reltype == "FIRST"]
            nexts = [item for item in member.relations if item.reltype == "NEXT"]
            if len(parents) != 1 or firsts or len(nexts) > 1:
                raise _run_error(
                    "run plan checkpoint has malformed graph edges", exits.PLAN_STALE
                )
    except todos.TodoError as exc:
        raise plans.PlanError(exc.message, exits.PLAN_STALE) from exc


def _target(profile: Any, step: plans.Step) -> str:
    return todos._scoped_href(profile, step.href)


def _readback(profile: Any, *, session: Session, step: plans.Step) -> todos.TodoRef:
    try:
        stored, raw = todos.fetch(profile, session=session, href=step.href)
    except (SessionError, todos.TodoError) as exc:
        raise RunError(
            f"the server accepted {step.action}, but its exact run readback could not be verified",
            exits.OUTCOME_UNCERTAIN,
        ) from exc
    try:
        matches = ical_semantics.calendar(raw) == ical_semantics.calendar(plans.payload_bytes(step))
    except ValueError as exc:
        raise RunError(
            f"the server accepted {step.action}, but its run content could not be compared",
            exits.OUTCOME_UNCERTAIN,
        ) from exc
    if not matches:
        raise RunError(
            f"the server stored different semantic run content at {step.href}",
            exits.OUTCOME_UNCERTAIN,
        )
    return stored


def execute(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Execute one conditional run write and verify its exact readback."""
    validate_step(step)
    target = _target(profile, step)
    if step.action == "run.create":
        response = session.request(
            "PUT",
            target,
            headers={"Content-Type": step.content_type, "If-None-Match": "*"},
            data=plans.payload_bytes(step),
            max_redirects=0,
        )
        todos._refuse_redirect(response, action="run creation", href=target)
    else:
        response = session.request(
            "PUT",
            target,
            headers={"Content-Type": step.content_type, "If-Match": step.etag},
            data=plans.payload_bytes(step),
            max_redirects=0,
        )
        todos._refuse_redirect(response, action="run update", href=target)
    if response.status == 412:
        raise plans.PlanError(
            f"the run resource at {target} changed since the plan was made; re-plan",
            exits.CONFLICT,
        )
    if response.status == 404 and step.action != "run.create":
        raise RunError(f"no run resource exists at {target}", exits.TARGET_NOT_FOUND)
    if response.status not in {200, 201, 204}:
        raise RunError(
            f"the server refused the {step.action} with status {response.status}",
            exits.SERVER_ERROR,
        )
    stored = _readback(profile, session=session, step=step)
    return {
        "action": step.action,
        "href": target,
        "uid": step.details.get("uid", ""),
        "etag": stored.etag,
        "verified": True,
        "details": step.details,
    }


def reconcile(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Classify the exact resource against one uncertain run write."""
    validate_step(step)
    target = _target(profile, step)
    try:
        stored, raw = todos.fetch(profile, session=session, href=target)
    except todos.TodoError as exc:
        if exc.code == exits.TARGET_NOT_FOUND and step.action == "run.create":
            return {"state": "pending"}
        if exc.code == exits.TARGET_NOT_FOUND:
            return {"state": "uncertain"}
        raise
    try:
        exact = ical_semantics.calendar(raw) == ical_semantics.calendar(plans.payload_bytes(step))
    except ValueError as exc:
        raise RunError(
            "the run could not be semantically compared", exits.MALFORMED_RESPONSE
        ) from exc
    if step.action == "run.create":
        return {"state": "verified" if exact else "uncertain"}
    current_etag = etag.normalize_strong(stored.etag)
    old_etag = etag.normalize_strong(step.etag)
    if exact:
        return {"state": "verified"}
    return {"state": "pending" if current_etag == old_etag else "uncertain"}


def as_list(graphs: list[RunGraph]) -> list[dict[str, Any]]:
    """Return the caller-visible structured list representation."""
    return [graph.as_dict() for graph in graphs]
