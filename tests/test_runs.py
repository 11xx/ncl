from __future__ import annotations

import base64
import datetime as dt
import json
from dataclasses import replace
from xml.sax.saxutils import escape

import icalendar
import pytest

from ncl import caldav, cli, exits, plans, runs, todos
from ncl.config import Profile
from ncl.identity import Identity
from ncl.session import Response

PROFILE = Profile(
    "home",
    "https://cloud.example.invalid",
    "pass",
    ("/remote.php/dav/calendars/alice/tasks/",),
    ("/remote.php/dav/files/alice/",),
)
HOME = "https://cloud.example.invalid/remote.php/dav/calendars/alice/"
CAL = HOME + "tasks/"
NOW = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.UTC)
IDENTITY = Identity(
    principal_url="https://cloud.example.invalid/remote.php/dav/principals/users/alice/",
    account_name="alice",
    display_name="Alice",
    calendar_home=HOME,
)
TASK_CALENDAR = caldav.Calendar(
    href=CAL,
    display_name="Tasks",
    components=("VTODO",),
    read_only=False,
    in_scope=True,
)


class FakeSession:
    def __init__(self, *responses: Response):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, data=None, **kwargs):
        self.requests.append(
            {"method": method, "url": url, "headers": headers, "data": data, "kwargs": kwargs}
        )
        assert self.responses, f"unexpected request: {method} {url}"
        return self.responses.pop(0)


def response(status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> Response:
    return Response(status, headers or {}, body, "")


def report_entry(href: str, raw: bytes, etag: str) -> str:
    return f"""<d:response><d:href>{href}</d:href><d:propstat><d:prop>
      <d:getetag>{etag}</d:getetag><c:calendar-data>{escape(raw.decode())}</c:calendar-data>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""


def report_body(*entries: str) -> bytes:
    return (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        'xmlns:c="urn:ietf:params:xml:ns:caldav">'
        + "".join(entries)
        + "</d:multistatus>"
    ).encode()


def _patch_cli(monkeypatch, transport, calendars=(TASK_CALENDAR,)):
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: transport)
    monkeypatch.setattr(cli.identity, "discover", lambda profile, session=None: IDENTITY)
    monkeypatch.setattr(
        cli.caldav,
        "list_calendars",
        lambda profile, session, calendar_home: calendars,
    )


def _created_run(
    *,
    count: int = 3,
    gaps: list[str] | None = None,
    statuses: dict[int, str] | None = None,
) -> tuple[list[plans.Step], dict[str, bytes]]:
    plan = runs.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Route",
        step_summaries=[f"Step {index}" for index in range(1, count + 1)],
        gaps=gaps,
        now=NOW,
    )
    payloads = {step.href: plans.payload_bytes(step) for step in plan.steps}
    for index, status in (statuses or {}).items():
        href = plan.steps[index].href
        raw = payloads[href]
        calendar = icalendar.Calendar.from_ical(raw)
        todo = next(item for item in calendar.walk() if item.name == "VTODO")
        todo.pop("STATUS", None)
        todo.add("STATUS", status)
        if status == "COMPLETED":
            todo.add("PERCENT-COMPLETE", 100)
            todo.add("COMPLETED", NOW)
        payloads[href] = calendar.to_ical()
    return list(plan.steps), payloads


def _run_report(payloads: dict[str, bytes], *, etags: dict[str, str] | None = None) -> bytes:
    return report_body(
        *(
            report_entry(href, raw, (etags or {}).get(href, '"v1"'))
            for href, raw in payloads.items()
        )
    )


def _replace_relations(raw: bytes, replacements: list[tuple[str, str, dict[str, str]]]) -> bytes:
    calendar = icalendar.Calendar.from_ical(raw)
    todo = next(item for item in calendar.walk() if item.name == "VTODO")
    todo.pop("RELATED-TO", None)
    for target, relation, parameters in replacements:
        todo.add(
            "RELATED-TO",
            target,
            parameters={"RELTYPE": relation, "VALUE": "UID", **parameters},
        )
    return calendar.to_ical()


def _mutate_resource(raw: bytes, callback) -> bytes:
    calendar = icalendar.Calendar.from_ical(raw)
    todo = next(item for item in calendar.walk() if item.name == "VTODO")
    callback(calendar, todo)
    return calendar.to_ical()


def test_standard_refid_and_relation_parameters_round_trip_exactly():
    raw = todos.build_todo(
        uid="run-a",
        summary="Root",
        refid="run-a",
        related_to=(
            ("run-a", {"RELTYPE": "PARENT", "VALUE": "UID"}),
            ("b", {"GAP": "PT15M", "RELTYPE": "NEXT", "VALUE": "UID"}),
        ),
        now=NOW,
    ).encode()
    component = next(
        item for item in icalendar.Calendar.from_ical(raw).walk() if item.name == "VTODO"
    )
    assert b"REFID:run-a\r\n" in raw
    assert b"RELATED-TO;RELTYPE=PARENT;VALUE=UID:run-a\r\n" in raw
    assert b"RELATED-TO;GAP=PT15M;RELTYPE=NEXT;VALUE=UID:b\r\n" in raw
    relations = [
        (str(value), dict(value.params))
        for name, value in component.property_items()
        if name == "RELATED-TO"
    ]
    assert ("run-a", {"RELTYPE": "PARENT", "VALUE": "UID"}) in relations
    assert ("b", {"GAP": "PT15M", "RELTYPE": "NEXT", "VALUE": "UID"}) in relations


@pytest.mark.parametrize(
    "mutate",
    [
        lambda calendar, todo: todo.add("REFID", "run-a"),
        lambda calendar, todo: todo.add("REFID", "run-a", parameters={"VALUE": "TEXT"}),
    ],
)
def test_run_parser_requires_one_parameter_free_refid(mutate):
    steps, payloads = _created_run(count=1)
    payloads[steps[0].href] = _mutate_resource(payloads[steps[0].href], mutate)

    with pytest.raises(todos.TodoError, match="REFID") as error:
        runs.list_runs(
            PROFILE,
            session=FakeSession(response(207, _run_report(payloads))),
            calendar_href=CAL,
        )
    assert error.value.code == exits.MALFORMED_RESPONSE


def test_create_plan_is_chain_ordered_and_root_last(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = runs.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Route",
        step_summaries=["Leave", "Collect", "Arrive"],
        gaps=["PT15M", "unknown"],
        now=NOW,
    )
    assert [step.action for step in plan.steps] == ["run.create"] * 4
    assert [step.details["kind"] for step in plan.steps] == [
        "checkpoint",
        "checkpoint",
        "checkpoint",
        "root",
    ]
    root = icalendar.Calendar.from_ical(plans.payload_bytes(plan.steps[-1]))
    root_todo = next(item for item in root.walk() if item.name == "VTODO")
    assert str(root_todo["REFID"]) == str(root_todo["UID"])
    assert "STATUS" not in root_todo
    assert "PERCENT-COMPLETE" not in root_todo
    assert "COMPLETED" not in root_todo


def test_list_and_show_use_one_report_and_derive_current_position(monkeypatch, capsys):
    steps, payloads = _created_run(gaps=["PT15M", "unknown"])
    transport = FakeSession(response(207, _run_report(payloads)))
    _patch_cli(monkeypatch, transport)
    assert cli._main(["task", "run", "list", "--json", CAL]) == exits.OK
    listing = json.loads(capsys.readouterr().out)
    assert len(transport.requests) == 1
    assert transport.requests[0]["method"] == "REPORT"
    run = listing["runs"][0]
    assert [step["summary"] for step in run["steps"]] == ["Step 1", "Step 2", "Step 3"]
    assert run["root"]["writable"] is True
    assert all(step["writable"] is True and step["unsupported"] == [] for step in run["steps"])
    assert run["gaps"] == ["PT15M", "unknown"]
    assert run["current_uid"] == next(
        step["uid"] for step in run["steps"] if step["position"] == 1
    )
    assert run["current_position"] == 1

    transport = FakeSession(response(207, _run_report(payloads)))
    _patch_cli(monkeypatch, transport)
    assert cli._main(["task", "run", "show", "--json", steps[-1].href]) == exits.OK
    shown = json.loads(capsys.readouterr().out)
    assert shown["run"]["root"]["href"] == steps[-1].href
    assert shown["run"]["root"]["writable"] is True
    assert all(step["writable"] is True for step in shown["run"]["steps"])
    assert len(transport.requests) == 1
    assert transport.requests[0]["method"] == "REPORT"


def test_run_structured_output_marks_read_only_collection_without_refid_unsupported(
    monkeypatch, capsys
):
    steps, payloads = _created_run(count=2)
    read_only = replace(TASK_CALENDAR, read_only=True)
    transport = FakeSession(response(207, _run_report(payloads)))
    _patch_cli(monkeypatch, transport, calendars=(read_only,))

    assert cli._main(["task", "run", "list", "--json", CAL]) == exits.OK
    listing = json.loads(capsys.readouterr().out)
    run = listing["runs"][0]
    assert run["root"]["writable"] is False
    assert all(
        step["writable"] is False and step["unsupported"] == [] for step in run["steps"]
    )

    shown = runs.show_run(
        PROFILE,
        session=FakeSession(response(207, _run_report(payloads))),
        root_href=steps[-1].href,
        collection_writable=False,
    ).as_dict()
    assert shown["root"]["writable"] is False
    assert all(step["writable"] is False for step in shown["steps"])


def test_current_becomes_null_and_terminal_counts_include_skips():
    _, payloads = _created_run(
        count=3,
        statuses={0: "COMPLETED", 1: "CANCELLED", 2: "COMPLETED"},
    )
    graph = runs.list_runs(
        PROFILE,
        session=FakeSession(response(207, _run_report(payloads))),
        calendar_href=CAL,
    )[0]
    value = graph.as_dict()
    assert value["current_uid"] is None
    assert value["current_position"] is None
    assert value["terminal_counts"] == {
        "completed": 2,
        "cancelled": 1,
        "terminal": 3,
        "nonterminal": 0,
    }


def test_create_cli_help_surfaces_all_run_commands(capsys):
    for command in ("create", "list", "show", "add", "reorder", "edit", "done", "skip", "not-yet"):
        with pytest.raises(SystemExit) as error:
            cli.main(["task", "run", command, "--help"])
        assert error.value.code == exits.OK
        assert command in " ".join(capsys.readouterr().out.split())


def test_create_cli_emits_a_redacted_plan_without_calendar_body(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    transport = FakeSession()
    _patch_cli(monkeypatch, transport)
    code = cli._main(
        [
            "task",
            "run",
            "create",
            "--json",
            CAL,
            "--summary",
            "Route",
            "--step",
            "One",
        ]
    )
    assert code == exits.CONFIRMATION_REQUIRED
    output = capsys.readouterr().out
    assert "BEGIN:VCALENDAR" not in output
    assert "REFID" not in output
    assert len(plans.listing()) == 1


@pytest.mark.parametrize(
    ("label", "alter", "code"),
    [
        (
            "duplicate-first",
            lambda payloads, steps: payloads.__setitem__(
                steps[-1].href,
                _mutate_resource(
                    payloads[steps[-1].href],
                    lambda calendar, todo: todo.add(
                        "RELATED-TO",
                        steps[0].details["uid"],
                        parameters={"RELTYPE": "FIRST", "VALUE": "UID"},
                    ),
                ),
            ),
            exits.MALFORMED_RESPONSE,
        ),
        (
            "missing-first-target",
            lambda payloads, steps: payloads.__setitem__(
                steps[-1].href,
                _replace_relations(
                    payloads[steps[-1].href],
                    [("missing", "FIRST", {})],
                ),
            ),
            exits.MALFORMED_RESPONSE,
        ),
        (
            "wrong-parent",
            lambda payloads, steps: payloads.__setitem__(
                steps[0].href,
                _replace_relations(
                    payloads[steps[0].href],
                    [("wrong-root", "PARENT", {}), (steps[1].details["uid"], "NEXT", {})],
                ),
            ),
            exits.MALFORMED_RESPONSE,
        ),
        (
            "duplicate-parent",
            lambda payloads, steps: payloads.__setitem__(
                steps[0].href,
                _mutate_resource(
                    payloads[steps[0].href],
                    lambda calendar, todo: todo.add(
                        "RELATED-TO",
                        steps[-1].details["uid"],
                        parameters={"RELTYPE": "PARENT", "VALUE": "UID"},
                    ),
                ),
            ),
            exits.MALFORMED_RESPONSE,
        ),
        (
            "cycle",
            lambda payloads, steps: payloads.__setitem__(
                steps[2].href,
                _replace_relations(
                    payloads[steps[2].href],
                    [
                        (steps[0].details["uid"], "PARENT", {}),
                        (steps[0].details["uid"], "NEXT", {}),
                    ],
                ),
            ),
            exits.MALFORMED_RESPONSE,
        ),
        (
            "mismatched-refid",
            lambda payloads, steps: payloads.__setitem__(
                steps[0].href,
                _mutate_resource(
                    payloads[steps[0].href],
                    lambda calendar, todo: (todo.pop("REFID", None), todo.add("REFID", "other")),
                ),
            ),
            exits.MALFORMED_RESPONSE,
        ),
        (
            "invalid-value",
            lambda payloads, steps: payloads.__setitem__(
                steps[0].href,
                _mutate_resource(
                    payloads[steps[0].href],
                    lambda calendar, todo: (
                        todo.pop("RELATED-TO", None),
                        todo.add(
                            "RELATED-TO",
                            steps[1].details["uid"],
                            parameters={"RELTYPE": "NEXT", "VALUE": "URI"},
                        ),
                        todo.add(
                            "RELATED-TO",
                            steps[-1].details["uid"],
                            parameters={"RELTYPE": "PARENT", "VALUE": "UID"},
                        ),
                    ),
                ),
            ),
            exits.MALFORMED_RESPONSE,
        ),
        (
            "invalid-gap",
            lambda payloads, steps: payloads.__setitem__(
                steps[0].href,
                _mutate_resource(
                    payloads[steps[0].href],
                    lambda calendar, todo: next(
                        value.params.__setitem__("GAP", "-PT15M")
                        for name, value in todo.property_items()
                        if name == "RELATED-TO" and str(value.params.get("RELTYPE")) == "NEXT"
                    ),
                ),
            ),
            exits.MALFORMED_RESPONSE,
        ),
        (
            "malformed-state",
            lambda payloads, steps: payloads.__setitem__(
                steps[0].href,
                _mutate_resource(
                    payloads[steps[0].href],
                    lambda calendar, todo: (
                        todo.pop("STATUS", None),
                        todo.add("STATUS", "COMPLETED"),
                    ),
                ),
            ),
            exits.MALFORMED_RESPONSE,
        ),
        (
            "recurrence",
            lambda payloads, steps: payloads.__setitem__(
                steps[0].href,
                _mutate_resource(
                    payloads[steps[0].href],
                    lambda calendar, todo: todo.add("RRULE", {"FREQ": ["WEEKLY"]}),
                ),
            ),
            exits.UNSUPPORTED_STRUCTURE,
        ),
        (
            "cross-collection",
            lambda payloads, steps: payloads.__setitem__(
                steps[0].href,
                _replace_relations(
                    payloads[steps[0].href],
                    [("https://other.example.invalid/tasks/step.ics", "NEXT", {}),
                     (steps[0].details["uid"], "PARENT", {})],
                ),
            ),
            exits.SCOPE_DENIED,
        ),
    ],
)
def test_named_graph_refusals_are_one_report_and_fail_closed(label, alter, code):
    steps, payloads = _created_run()
    alter(payloads, steps)
    transport = FakeSession(response(207, _run_report(payloads)))
    with pytest.raises(todos.TodoError) as error:
        runs.list_runs(PROFILE, session=transport, calendar_href=CAL)
    assert error.value.code == code, label
    assert [request["method"] for request in transport.requests] == ["REPORT"]


def test_disconnected_branch_and_multiple_edge_graphs_are_refused():
    steps, payloads = _created_run(count=3)
    first = steps[0].details["uid"]
    third = steps[2].details["uid"]
    payloads[steps[0].href] = _replace_relations(
        payloads[steps[0].href], [(steps[-1].details["uid"], "PARENT", {})]
    )
    payloads[steps[0].href] = _replace_relations(
        payloads[steps[0].href], [(steps[-1].details["uid"], "PARENT", {}), (third, "NEXT", {})]
    )
    payloads[steps[1].href] = _replace_relations(
        payloads[steps[1].href], [(steps[-1].details["uid"], "PARENT", {}), (third, "NEXT", {})]
    )
    with pytest.raises(todos.TodoError):
        runs.list_runs(
            PROFILE,
            session=FakeSession(response(207, _run_report(payloads))),
            calendar_href=CAL,
        )
    assert first


def test_nested_and_sibling_structures_are_not_accepted_as_run_graphs():
    steps, payloads = _created_run(count=1)

    def add_event(calendar, todo):
        event = icalendar.Event()
        event.add("UID", "sibling-event")
        event.add("DTSTAMP", NOW)
        calendar.add_component(event)

    payloads[steps[0].href] = _mutate_resource(payloads[steps[0].href], add_event)
    with pytest.raises(todos.TodoError) as error:
        runs.list_runs(
            PROFILE,
            session=FakeSession(response(207, _run_report(payloads))),
            calendar_href=CAL,
        )
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_run_shaped_resource_without_refid_is_not_silently_ignored():
    steps, payloads = _created_run(count=1)
    payloads[steps[-1].href] = _mutate_resource(
        payloads[steps[-1].href], lambda calendar, todo: todo.pop("REFID", None)
    )
    with pytest.raises(todos.TodoError) as error:
        runs.list_runs(
            PROFILE,
            session=FakeSession(response(207, _run_report(payloads))),
            calendar_href=CAL,
        )
    assert error.value.code == exits.MALFORMED_RESPONSE


def test_plan_edit_rejects_weak_etag_and_authoring_freezes_after_execution(monkeypatch):
    steps, payloads = _created_run(count=2)
    weak = {href: 'W/"v1"' for href in payloads}
    with pytest.raises(todos.TodoError) as error:
        runs.plan_edit(
            PROFILE,
            session=FakeSession(response(207, _run_report(payloads, etags=weak))),
            href=steps[0].href,
            changes={"SUMMARY": "Changed"},
        )
    assert error.value.code == exits.MALFORMED_RESPONSE

    frozen = dict(payloads)
    frozen[steps[0].href] = _mutate_resource(
        frozen[steps[0].href],
        lambda calendar, todo: (todo.pop("STATUS", None), todo.add("STATUS", "IN-PROCESS")),
    )
    for operation in (
        lambda transport: runs.plan_add(
            PROFILE,
            session=transport,
            root_href=steps[-1].href,
            summary="New",
            gap_before="unknown",
        ),
        lambda transport: runs.plan_reorder(
            PROFILE,
            session=transport,
            root_href=steps[-1].href,
            step_hrefs=[steps[0].href, steps[1].href],
        ),
        lambda transport: runs.plan_edit(
            PROFILE,
            session=transport,
            href=steps[0].href,
            changes={"SUMMARY": "Changed"},
        ),
    ):
        with pytest.raises(todos.TodoError) as error:
            operation(FakeSession(response(207, _run_report(frozen))))
        assert error.value.code == exits.CONFLICT


@pytest.mark.parametrize(
    ("changes", "plan_summary", "step_summary"),
    [
        ({"SUMMARY": "Renamed"}, "Edit run resource Renamed", "Renamed"),
        ({"SUMMARY": ""}, "Edit run resource ", ""),
    ],
)
def test_plan_edit_structured_summary_comes_from_patched_payload(
    tmp_path, monkeypatch, changes, plan_summary, step_summary
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    steps, payloads = _created_run(count=1)
    plan = runs.plan_edit(
        PROFILE,
        session=FakeSession(response(207, _run_report(payloads))),
        href=steps[0].href,
        changes=changes,
        now=NOW,
    )

    structured = plan.as_dict()
    assert structured["summary"] == plan_summary
    assert structured["steps"][0]["summary"] == step_summary


def test_add_and_reorder_have_deterministic_write_order_and_explicit_gaps(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    steps, payloads = _created_run(count=3, gaps=["PT15M", "PT30M"])
    etags = {href: f'"{index}"' for index, href in enumerate(payloads)}
    add = runs.plan_add(
        PROFILE,
        session=FakeSession(response(207, _run_report(payloads, etags=etags))),
        root_href=steps[-1].href,
        summary="Between",
        before=steps[1].href,
        gap_before="PT5M",
        gap_after="unknown",
        now=NOW,
    )
    assert [step.action for step in add.steps] == ["run.create", "run.update"]
    assert add.steps[-1].details["relation"] == "NEXT"
    assert add.steps[0].href not in payloads
    assert b"GAP=PT5M" in plans.payload_bytes(add.steps[1])

    reorder = runs.plan_reorder(
        PROFILE,
        session=FakeSession(response(207, _run_report(payloads, etags=etags))),
        root_href=steps[-1].href,
        step_hrefs=[steps[2].href, steps[0].href, steps[1].href],
        gaps=["unknown", "PT45M"],
        now=NOW,
    )
    assert isinstance(reorder, plans.Plan)
    assert [step.href for step in reorder.steps] == [
        steps[2].href,
        steps[0].href,
        steps[1].href,
        steps[-1].href,
    ]
    assert b"GAP=PT45M" in plans.payload_bytes(reorder.steps[1])


def test_add_requires_insertion_edge_gaps():
    steps, payloads = _created_run(count=2)
    with pytest.raises(todos.TodoError) as error:
        runs.plan_add(
            PROFILE,
            session=FakeSession(response(207, _run_report(payloads))),
            root_href=steps[-1].href,
            summary="No gap",
        )
    assert error.value.code == exits.USAGE


def test_run_apply_uses_conditional_headers_ordered_readback_for_create_add_and_reorder(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    create = runs.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Create",
        step_summaries=["One", "Two"],
        gaps=["PT15M"],
        now=NOW,
    )
    create_responses = []
    for step in create.steps:
        create_responses.extend(
            [response(201), response(200, plans.payload_bytes(step), {"ETag": '"new"'})]
        )
    create_transport = FakeSession(*create_responses)
    with plans.claim(create.plan_id):
        plans.apply(PROFILE, session=create_transport, plan=create, dispatchers=cli._dispatchers())
    assert [request["url"] for request in create_transport.requests[::2]] == [
        step.href for step in create.steps
    ]
    assert all(
        request["headers"]["If-None-Match"] == "*"
        for request in create_transport.requests[::2]
    )

    steps, payloads = _created_run(count=2)
    etags = {href: '"old"' for href in payloads}
    add = runs.plan_add(
        PROFILE,
        session=FakeSession(response(207, _run_report(payloads, etags=etags))),
        root_href=steps[-1].href,
        summary="Before",
        before=steps[0].href,
        gap_after="unknown",
        now=NOW,
    )
    add_responses = []
    for step in add.steps:
        add_responses.extend(
            [response(201 if step.action == "run.create" else 204),
             response(200, plans.payload_bytes(step), {"ETag": '"new"'})]
        )
    add_transport = FakeSession(*add_responses)
    with plans.claim(add.plan_id):
        plans.apply(PROFILE, session=add_transport, plan=add, dispatchers=cli._dispatchers())
    assert [request["url"] for request in add_transport.requests[::2]] == [
        step.href for step in add.steps
    ]
    assert add_transport.requests[0]["headers"]["If-None-Match"] == "*"
    assert add_transport.requests[2]["headers"]["If-Match"] == '"old"'

    reorder = runs.plan_reorder(
        PROFILE,
        session=FakeSession(response(207, _run_report(payloads, etags=etags))),
        root_href=steps[-1].href,
        step_hrefs=[steps[1].href, steps[0].href],
        gaps=["unknown"],
        now=NOW,
    )
    assert isinstance(reorder, plans.Plan)
    reorder_responses = []
    for step in reorder.steps:
        reorder_responses.extend(
            [response(204), response(200, plans.payload_bytes(step), {"ETag": '"new"'})]
        )
    reorder_transport = FakeSession(*reorder_responses)
    with plans.claim(reorder.plan_id):
        plans.apply(
            PROFILE,
            session=reorder_transport,
            plan=reorder,
            dispatchers=cli._dispatchers(),
        )
    assert reorder_transport.requests[-2]["url"] == steps[-1].href
    assert reorder_transport.requests[-2]["headers"]["If-Match"] == '"old"'


def test_run_state_transitions_freeze_exact_fields_and_out_of_order_details(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    steps, payloads = _created_run(count=2)
    etags = {href: '"v1"' for href in payloads}
    done = runs.plan_transition(
        PROFILE,
        session=FakeSession(response(207, _run_report(payloads, etags=etags))),
        href=steps[1].href,
        transition="done",
        at=dt.datetime(2026, 8, 20, 13, tzinfo=dt.timezone(dt.timedelta(hours=-3))),
    )
    assert isinstance(done, plans.Plan)
    payload = plans.payload_bytes(done.steps[0])
    assert b"STATUS:COMPLETED\r\n" in payload
    assert b"PERCENT-COMPLETE:100\r\n" in payload
    assert b"COMPLETED:20260820T160000Z\r\n" in payload
    assert done.steps[0].details["out_of_order"] is True

    percent_raw = _mutate_resource(
        payloads[steps[0].href],
        lambda calendar, todo: (todo.add("PERCENT-COMPLETE", 45), todo.add("X-KEEP", "yes")),
    )
    percent_payloads = dict(payloads)
    percent_payloads[steps[0].href] = percent_raw
    skip = runs.plan_transition(
        PROFILE,
        session=FakeSession(response(207, _run_report(percent_payloads, etags=etags))),
        href=steps[0].href,
        transition="skip",
        now=NOW,
    )
    skip_payload = plans.payload_bytes(skip.steps[0])
    assert b"STATUS:CANCELLED\r\n" in skip_payload
    assert b"PERCENT-COMPLETE:45\r\n" in skip_payload
    assert b"COMPLETED:" not in skip_payload
    assert b"X-KEEP:yes\r\n" in skip_payload

    in_process = _mutate_resource(
        payloads[steps[0].href],
        lambda calendar, todo: (todo.pop("STATUS", None), todo.add("STATUS", "IN-PROCESS")),
    )
    in_process_payloads = dict(payloads)
    in_process_payloads[steps[0].href] = in_process
    not_yet = runs.plan_transition(
        PROFILE,
        session=FakeSession(response(207, _run_report(in_process_payloads, etags=etags))),
        href=steps[0].href,
        transition="not-yet",
        now=NOW,
    )
    assert b"STATUS:NEEDS-ACTION\r\n" in plans.payload_bytes(not_yet.steps[0])
    assert b"PERCENT-COMPLETE:0\r\n" in plans.payload_bytes(not_yet.steps[0])


def test_not_yet_on_needs_action_is_a_zero_write_noop():
    steps, payloads = _created_run(count=1)
    transport = FakeSession(response(207, _run_report(payloads)))
    result = runs.plan_transition(
        PROFILE,
        session=transport,
        href=steps[0].href,
        transition="not-yet",
    )
    assert isinstance(result, runs.RunNoOp)
    assert result.result["changed"] is False
    assert len(transport.requests) == 1


@pytest.mark.parametrize("transition", ["done", "skip", "not-yet"])
def test_terminal_transitions_conflict(transition):
    steps, payloads = _created_run(count=1, statuses={0: "COMPLETED"})
    with pytest.raises(todos.TodoError) as error:
        runs.plan_transition(
            PROFILE,
            session=FakeSession(response(207, _run_report(payloads))),
            href=steps[0].href,
            transition=transition,
        )
    assert error.value.code == exits.CONFLICT


def test_unknown_properties_and_alarm_survive_run_edit():
    steps, payloads = _created_run(count=1)

    def add_unknown(calendar, todo):
        todo.add("X-CUSTOM-FIELD", "retain")
        alarm = icalendar.Alarm()
        alarm.add("ACTION", "DISPLAY")
        alarm.add("DESCRIPTION", "Reminder")
        alarm.add("TRIGGER", dt.timedelta(minutes=-15))
        todo.add_component(alarm)

    payloads[steps[0].href] = _mutate_resource(payloads[steps[0].href], add_unknown)
    plan = runs.plan_edit(
        PROFILE,
        session=FakeSession(response(207, _run_report(payloads))),
        href=steps[0].href,
        changes={"SUMMARY": "Edited"},
        now=NOW,
    )
    payload = plans.payload_bytes(plan.steps[0])
    assert b"X-CUSTOM-FIELD:retain\r\n" in payload
    assert b"BEGIN:VALARM\r\n" in payload


def test_generic_task_mutations_refuse_refid_but_task_read_remains_available():
    steps, payloads = _created_run(count=1)
    raw = payloads[steps[0].href]
    for operation in (
        lambda: todos.plan_update(
            PROFILE,
            session=FakeSession(response(200, raw, {"ETag": '"v1"'})),
            href=steps[0].href,
            changes={"SUMMARY": "No"},
        ),
        lambda: todos.plan_complete(
            PROFILE,
            session=FakeSession(response(200, raw, {"ETag": '"v1"'})),
            href=steps[0].href,
        ),
        lambda: todos.plan_delete(
            PROFILE,
            session=FakeSession(response(200, raw, {"ETag": '"v1"'})),
            href=steps[0].href,
        ),
    ):
        with pytest.raises(todos.TodoError) as error:
            operation()
        assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_run_dispatcher_prevalidates_whole_plan_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = runs.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Route",
        step_summaries=["One", "Two"],
        now=NOW,
    )
    invalid = replace(plan.steps[1], action="run.unknown")
    corrupted = replace(plan, steps=(plan.steps[0], invalid, plan.steps[2]))
    transport = FakeSession()
    with plans.claim(corrupted.plan_id), pytest.raises(plans.PlanError) as error:
        plans.apply(PROFILE, session=transport, plan=corrupted, dispatchers=cli._dispatchers())
    assert error.value.code == exits.USAGE
    assert transport.requests == []

    mismatched_details = dict(plan.steps[0].details)
    mismatched_details["uid"] = "different-resource"
    corrupted = replace(
        plan,
        steps=(replace(plan.steps[0], details=mismatched_details), *plan.steps[1:]),
    )
    transport = FakeSession()
    with plans.claim(corrupted.plan_id), pytest.raises(plans.PlanError) as error:
        plans.apply(PROFILE, session=transport, plan=corrupted, dispatchers=cli._dispatchers())
    assert error.value.code == exits.PLAN_STALE
    assert transport.requests == []


@pytest.mark.parametrize(
    "corruption", ["self-cycle", "missing-target", "branch-disconnected", "cross-run"]
)
def test_run_final_graph_contract_rejects_corrupt_create_before_any_write(
    tmp_path, monkeypatch, corruption
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = runs.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Route",
        step_summaries=["One", "Two", "Three"],
        now=NOW,
    )
    first, second, third, root = plan.steps
    payloads = {step.href: plans.payload_bytes(step) for step in plan.steps}
    if corruption == "self-cycle":
        payloads[first.href] = _replace_relations(
            payloads[first.href],
            [(root.details["uid"], "PARENT", {}), (first.details["uid"], "NEXT", {})],
        )
    elif corruption == "missing-target":
        payloads[first.href] = _replace_relations(
            payloads[first.href],
            [(root.details["uid"], "PARENT", {}), ("missing", "NEXT", {})],
        )
    elif corruption == "branch-disconnected":
        payloads[first.href] = _replace_relations(
            payloads[first.href],
            [(root.details["uid"], "PARENT", {}), (third.details["uid"], "NEXT", {})],
        )
    else:
        payloads[first.href] = _replace_relations(
            payloads[first.href],
            [("other-run", "PARENT", {}), (second.details["uid"], "NEXT", {})],
        )
    corrupted = replace(
        plan,
        steps=tuple(
            replace(
                step,
                payload=base64.b64encode(payloads[step.href]).decode("ascii"),
            )
            for step in plan.steps
        ),
    )
    transport = FakeSession()

    with plans.claim(corrupted.plan_id), pytest.raises(plans.PlanError) as error:
        plans.apply(PROFILE, session=transport, plan=corrupted, dispatchers=cli._dispatchers())

    assert error.value.code == exits.PLAN_STALE
    assert transport.requests == []


def test_run_partial_failure_resume_and_uncertain_readback(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = runs.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Route",
        step_summaries=["One", "Two"],
        now=NOW,
    )
    first, second, root = plan.steps
    first_raw = plans.payload_bytes(first)
    second_raw = plans.payload_bytes(second)
    root_raw = plans.payload_bytes(root)
    failing = FakeSession(
        response(201),
        response(200, first_raw, {"ETag": '"a"'}),
        response(412),
    )
    with plans.claim(plan.plan_id), pytest.raises(plans.PlanError) as error:
        plans.apply(PROFILE, session=failing, plan=plan, dispatchers=cli._dispatchers())
    assert error.value.code == exits.CONFLICT
    stored = plans.read(plan.plan_id)
    assert [item.state for item in stored.progress] == ["verified", "pending", "pending"]
    assert [request["method"] for request in failing.requests] == ["PUT", "GET", "PUT"]

    resumed = FakeSession(
        response(201),
        response(200, second_raw, {"ETag": '"b"'}),
        response(201),
        response(200, root_raw, {"ETag": '"r"'}),
    )
    with plans.claim(stored.plan_id):
        result = plans.apply(PROFILE, session=resumed, plan=stored, dispatchers=cli._dispatchers())
    assert result["complete"] is True
    assert [request["url"] for request in resumed.requests[::2]] == [second.href, root.href]

    uncertain_plan = runs.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Uncertain",
        step_summaries=["One"],
        now=NOW,
    )
    altered = first_raw.replace(b"SUMMARY:One", b"SUMMARY:Else")
    uncertain = FakeSession(response(201), response(200, altered, {"ETag": '"u"'}))
    with plans.claim(uncertain_plan.plan_id), pytest.raises(todos.TodoError) as error:
        plans.apply(
            PROFILE,
            session=uncertain,
            plan=uncertain_plan,
            dispatchers=cli._dispatchers(),
        )
    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(uncertain_plan.plan_id).progress[0].state == "uncertain"
