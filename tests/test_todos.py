from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from xml.sax.saxutils import escape

import icalendar
import pytest
from test_auth import credential_reader

from ncl import caldav, cli, exits, plans, todos
from ncl import session as http_session
from ncl.caldav import CalendarError
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
TASK = CAL + "child.ics"
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
    description="",
    color="",
    components=("VTODO",),
    read_only=False,
    in_scope=True,
)
EVENT_CALENDAR = caldav.Calendar(
    href=HOME + "events/",
    display_name="Events",
    description="",
    color="",
    components=("VEVENT",),
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


class HttpTransport:
    def __init__(self, *responses: Response):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, data=None, timeout=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "data": data})
        assert self.responses, f"unexpected request: {method} {url}"
        return self.responses.pop(0)


def _apply_bundle(plan, transport):
    with plans.claim(plan.plan_id):
        return plans.apply(
            PROFILE,
            session=transport,
            plan=plan,
            dispatchers=cli._dispatchers(),
        )


def _write(**kwargs):
    payload = kwargs.pop("payload", b"")
    ttl = kwargs.pop("ttl", plans.DEFAULT_TTL_SECONDS)
    now = kwargs.pop("now", None)
    summary = kwargs.pop("summary")
    profile = kwargs.pop("profile")
    if profile == PROFILE.name:
        profile = PROFILE
    return plans.write_bundle(
        profile=profile,
        summary=summary,
        steps=(plans.freeze_step(payload=payload, summary=summary, **kwargs),),
        ttl=ttl,
        now=now,
    )


def response(
    status: int,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    *,
    url: str = "",
) -> Response:
    return Response(status, headers or {}, body, url)


def replace_todo_property(raw: bytes, name: str, value, *, parameters=None) -> bytes:
    calendar = icalendar.Calendar.from_ical(raw)
    todo = next(item for item in calendar.walk() if item.name == "VTODO")
    todo.pop(name, None)
    todo.add(name, value, parameters=parameters)
    return calendar.to_ical()


def add_todo_property(raw: bytes, name: str, value, *, parameters=None) -> bytes:
    calendar = icalendar.Calendar.from_ical(raw)
    todo = next(item for item in calendar.walk() if item.name == "VTODO")
    todo.add(name, value, parameters=parameters)
    return calendar.to_ical()


def raw_todo(
    uid: str,
    summary: str,
    *,
    description: str = "",
    start: dt.datetime | dt.date | None = None,
    due: dt.datetime | dt.date | None = None,
    status: str = "",
    percent_complete: int | None = None,
    parent_uid: str = "",
    priority: int | None = None,
    sibling_relation: str = "",
    unknown: bool = False,
    alarm: bool = False,
    recurrence: bool = False,
    attendee: bool = False,
) -> bytes:
    calendar = icalendar.Calendar.from_ical(
        todos.build_todo(
            uid=uid,
            summary=summary,
            description=description,
            start=start,
            due=due,
            status=status,
            percent_complete=percent_complete,
            parent_uid=parent_uid,
            priority=priority,
            now=NOW,
        ).encode()
    )
    todo = next(item for item in calendar.walk() if item.name == "VTODO")
    if sibling_relation:
        todo.add("related-to", sibling_relation, parameters={"RELTYPE": "SIBLING"})
    if unknown:
        todo.add("x-custom-field", "do-not-lose-me")
    if alarm:
        reminder = icalendar.Alarm()
        reminder.add("action", "DISPLAY")
        reminder.add("description", "Reminder")
        reminder.add("trigger", dt.timedelta(minutes=-15))
        todo.add_component(reminder)
    if recurrence:
        todo.add("rrule", {"FREQ": ["WEEKLY"]})
    if attendee:
        todo.add("attendee", "mailto:bob@example.invalid")
    return calendar.to_ical()


def raw_nested_vtodo() -> bytes:
    calendar = icalendar.Calendar()
    calendar.add("prodid", "-//probe//EN")
    calendar.add("version", "2.0")
    event = icalendar.Event()
    event.add("uid", "event")
    event.add("dtstamp", NOW)
    nested = icalendar.Todo()
    nested.add("uid", "nested")
    nested.add("summary", "Nested")
    nested.add("dtstamp", NOW)
    event.add_component(nested)
    calendar.add_component(event)
    return calendar.to_ical()


def raw_direct_sibling_event() -> bytes:
    calendar = icalendar.Calendar.from_ical(raw_todo("sibling", "Task"))
    event = icalendar.Event()
    event.add("uid", "sibling-event")
    event.add("dtstamp", NOW)
    calendar.add_component(event)
    return calendar.to_ical()


def raw_duration_todo(
    uid: str, *, start: dt.datetime | dt.date, duration: dt.timedelta
) -> bytes:
    return add_todo_property(
        raw_todo(uid, "Duration", start=start), "DURATION", duration
    )


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


def test_list_uses_one_vtodo_report_and_derives_parent_children_with_filters():
    parent = raw_todo("parent", "Parent")
    child_a = raw_todo("child-a", "A", parent_uid="parent", status="completed")
    child_b = raw_todo("child-b", "B", parent_uid="parent", status="needs-action")
    body = report_body(
        report_entry(CAL + "parent.ics", parent, '"p"'),
        report_entry(CAL + "child-a.ics", child_a, '"a"'),
        report_entry(CAL + "child-b.ics", child_b, '"b"'),
    )
    transport = FakeSession(response(207, body))

    found = todos.query(
        PROFILE,
        session=transport,
        calendar_href=CAL,
        statuses=("completed",),
        collection_writable=True,
    )

    assert len(transport.requests) == 1
    assert transport.requests[0]["method"] == "REPORT"
    assert transport.requests[0]["headers"]["Depth"] == "1"
    assert 'name="VTODO"' in transport.requests[0]["data"]
    assert 'name="VEVENT"' not in transport.requests[0]["data"]
    assert [task.uid for task in found] == ["child-a"]
    assert found[0].parent_uid == "parent"
    assert found[0].children == ()

    transport = FakeSession(
        response(
            207,
            report_body(
                report_entry(CAL + "parent.ics", parent, '"p"'),
                report_entry(CAL + "child-a.ics", child_a, '"a"'),
                report_entry(CAL + "child-b.ics", child_b, '"b"'),
            ),
        )
    )
    all_tasks = todos.query(PROFILE, session=transport, calendar_href=CAL)
    assert all_tasks[0].uid == "parent"
    assert all_tasks[0].children == ("child-a", "child-b")


@pytest.mark.parametrize(
    "href",
    [
        CAL,
        CAL + "nested/task.ics",
        HOME + "events/task.ics",
        "https://cloud.example.invalid/remote.php/dav/calendars/bob/tasks/leak.ics",
    ],
)
def test_task_list_rejects_report_href_outside_selected_collection(
    href, monkeypatch, capsys
):
    body = report_body(report_entry(href, raw_todo("leak", "Leak"), '"v1"'))
    transport = FakeSession(response(207, body))
    _patch_cli(monkeypatch, transport, [TASK_CALENDAR])

    code = cli._main(["task", "list", "--json", CAL])
    error = json.loads(capsys.readouterr().out)

    assert code == exits.SCOPE_DENIED
    assert error["code"] == exits.SCOPE_DENIED
    assert len(transport.requests) == 1
    assert transport.requests[0]["method"] == "REPORT"
    assert transport.requests[0]["url"] == CAL


def test_due_only_creation_has_no_start_and_freezes_a_task_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    due = dt.datetime(2026, 9, 3, 17, 0, tzinfo=dt.timezone(dt.timedelta(hours=-3)))

    plan = todos.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Pay invoice",
        due=due,
        now=NOW,
    )

    payload = plans.payload_bytes(plan.steps[0]).decode()
    assert plan.steps[0].action == "task.create"
    assert "DUE:20260903T200000Z" in payload
    assert "DTSTART" not in payload
    assert plan.steps[0].details["start"] == ""
    assert plan.steps[0].details["due"] == "20260903T200000Z"
    assert plan.as_dict()["steps"][0]["payload_bytes"] > 0


def test_create_update_complete_delete_apply_conditionally_and_read_back(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    create_plan = todos.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Created",
        due=dt.date(2026, 9, 3),
        now=NOW,
    )
    created = plans.payload_bytes(create_plan.steps[0])
    create_transport = FakeSession(
        response(201), response(200, created, {"ETag": '"created"'})
    )
    result = _apply_bundle(create_plan, create_transport)
    assert result["verified"] is True
    assert create_transport.requests[0]["headers"]["If-None-Match"] == "*"
    assert create_transport.requests[1]["method"] == "GET"

    original = raw_todo("child", "Old", due=dt.date(2026, 9, 3), unknown=True)
    update_plan = todos.plan_update(
        PROFILE,
        session=FakeSession(response(200, original, {"ETag": '"v1"'})),
        href=TASK,
        changes={"SUMMARY": "New", "DESCRIPTION": "Details"},
        now=NOW,
    )
    updated = todos.patch_todo(
        original,
        {"SUMMARY": "New", "DESCRIPTION": "Details"},
        now=NOW,
    ).encode()
    update_transport = FakeSession(
        response(204), response(200, updated, {"ETag": '"v2"'})
    )
    update_result = _apply_bundle(update_plan, update_transport)
    assert update_result["verified"] is True
    assert update_transport.requests[0]["headers"]["If-Match"] == '"v1"'
    assert update_result["task"]["description"] == "Details"

    complete_plan = todos.plan_complete(
        PROFILE,
        session=FakeSession(response(200, updated, {"ETag": '"v2"'})),
        href=TASK,
        completed=dt.datetime(2026, 8, 20, 13, 0, tzinfo=dt.timezone(dt.timedelta(hours=-3))),
    )
    complete_payload = plans.payload_bytes(complete_plan.steps[0])
    assert complete_payload.count(b"STATUS:COMPLETED") == 1
    assert complete_payload.count(b"PERCENT-COMPLETE:100") == 1
    assert b"COMPLETED:20260820T160000Z" in complete_payload
    completed = todos.patch_todo(
        updated,
        {
            "STATUS": "COMPLETED",
            "COMPLETED": dt.datetime(2026, 8, 20, 16, 0, tzinfo=dt.UTC),
            "PERCENT-COMPLETE": 100,
        },
        now=dt.datetime(2026, 8, 20, 16, 0, tzinfo=dt.UTC),
    ).encode()
    complete_transport = FakeSession(
        response(204), response(200, completed, {"ETag": '"v3"'})
    )
    complete_result = _apply_bundle(complete_plan, complete_transport)
    assert complete_result["verified"] is True
    assert [request["method"] for request in complete_transport.requests] == ["PUT", "GET"]
    assert complete_transport.requests[0]["headers"]["If-Match"] == '"v2"'

    delete_plan = todos.plan_delete(
        PROFILE,
        session=FakeSession(response(200, completed, {"ETag": '"v3"'})),
        href=TASK,
    )
    delete_transport = FakeSession(response(204), response(404, url=TASK))
    delete_result = _apply_bundle(delete_plan, delete_transport)
    assert delete_result["verified"] == "deleted"
    assert delete_transport.requests[0]["headers"]["If-Match"] == '"v3"'
    assert [request["method"] for request in delete_transport.requests] == ["DELETE", "GET"]
    assert delete_transport.requests[1]["url"] == TASK


def test_scope_and_etag_conditions_refuse_before_or_during_writes():
    transport = FakeSession()
    with pytest.raises(CalendarError) as scope_error:
        todos.query(
            PROFILE,
            session=transport,
            calendar_href=HOME + "private/",
        )
    assert scope_error.value.code == exits.SCOPE_DENIED
    assert transport.requests == []

    with pytest.raises(todos.TodoError) as etag_error:
        todos.plan_update(
            PROFILE,
            session=FakeSession(response(200, raw_todo("child", "Old"))),
            href=TASK,
            changes={"SUMMARY": "New"},
        )
    assert etag_error.value.code == exits.MALFORMED_RESPONSE

    plan = _write(
        profile=PROFILE.name,
        action="task.delete",
        href=TASK,
        etag='"v1"',
        summary="Old",
        details={"uid": "child"},
    )
    with pytest.raises(plans.PlanError) as conflict_error:
        _apply_bundle(plan, FakeSession(response(412)))
    assert conflict_error.value.code == exits.CONFLICT


def test_task_fetch_refuses_redirect_before_following_it_on_real_session(monkeypatch):
    other = CAL + "other.ics"
    transport = HttpTransport(
        response(307, headers={"Location": other}, url=TASK),
        response(200, raw_todo("child", "Read me"), {"ETag": '"v1"'}, url=other),
    )
    monkeypatch.setattr(
        http_session.secrets,
        "get",
        credential_reader("alice", "fixture"),
    )

    with pytest.raises(http_session.SessionError) as error:
        todos.fetch(PROFILE, session=http_session.Session(PROFILE, transport=transport), href=TASK)

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert [request["method"] for request in transport.requests] == ["GET"]
    assert transport.requests[0]["url"] == TASK


def test_task_put_redirect_is_refused_and_plan_remains_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = todos.plan_create(PROFILE, calendar_href=CAL, summary="Created", now=NOW)
    other = CAL + "other.ics"
    transport = FakeSession(
        response(307, headers={"Location": other}, url=plan.steps[0].href)
    )

    with pytest.raises(todos.TodoError) as error:
        _apply_bundle(plan, transport)

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert [request["method"] for request in transport.requests] == ["PUT"]
    assert transport.requests[0]["url"] == plan.steps[0].href
    assert transport.requests[0]["kwargs"]["max_redirects"] == 0
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_task_readback_redirect_is_outcome_uncertain_and_plan_remains_pending(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = todos.plan_create(PROFILE, calendar_href=CAL, summary="Created", now=NOW)
    other = CAL + "other.ics"
    transport = FakeSession(
        response(201, url=plan.steps[0].href),
        response(307, headers={"Location": other}, url=plan.steps[0].href),
    )

    with pytest.raises(todos.TodoError) as error:
        _apply_bundle(plan, transport)

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert [request["method"] for request in transport.requests] == ["PUT", "GET"]
    assert all(request["url"] == plan.steps[0].href for request in transport.requests)
    assert transport.requests[1]["kwargs"]["max_redirects"] == 0
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_task_delete_keeps_plan_when_exact_href_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    raw = raw_todo("child", "Old")
    plan = todos.plan_delete(
        PROFILE,
        session=FakeSession(response(200, raw, {"ETag": '"v1"'}, url=TASK)),
        href=TASK,
    )
    transport = FakeSession(response(204, url=TASK), response(200, raw, url=TASK))

    with pytest.raises(todos.TodoError) as error:
        _apply_bundle(plan, transport)

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert [request["method"] for request in transport.requests] == ["DELETE", "GET"]
    assert all(request["url"] == TASK for request in transport.requests)
    assert transport.requests[1]["kwargs"]["max_redirects"] == 0
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_task_update_readback_compares_unknown_properties_and_keeps_plan(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    raw = raw_todo("child", "Old", unknown=True)
    plan = todos.plan_update(
        PROFILE,
        session=FakeSession(response(200, raw, {"ETag": '"v1"'}, url=TASK)),
        href=TASK,
        changes={"SUMMARY": "New"},
        now=NOW,
    )
    altered = plans.payload_bytes(plan.steps[0]).replace(
        b"X-CUSTOM-FIELD:do-not-lose-me\r\n", b""
    )
    transport = FakeSession(
        response(204, url=TASK), response(200, altered, {"ETag": '"v2"'}, url=TASK)
    )

    with pytest.raises(todos.TodoError) as error:
        _apply_bundle(plan, transport)

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_task_readback_allows_server_timestamps_but_requires_nested_components(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    raw = raw_todo("child", "Old", unknown=True, alarm=True)
    plan = todos.plan_update(
        PROFILE,
        session=FakeSession(response(200, raw, {"ETag": '"v1"'}, url=TASK)),
        href=TASK,
        changes={"SUMMARY": "New"},
        now=NOW,
    )
    timestamped = replace_todo_property(
        replace_todo_property(
            plans.payload_bytes(plan.steps[0]),
            "DTSTAMP",
            dt.datetime(2026, 8, 20, 13, tzinfo=dt.UTC),
        ),
        "LAST-MODIFIED",
        dt.datetime(2026, 8, 20, 13, 1, tzinfo=dt.UTC),
    )
    result = _apply_bundle(
        plan,
        FakeSession(response(204, url=TASK), response(200, timestamped, url=TASK)),
    )
    assert result["verified"] is True

    missing_alarm = timestamped.replace(
        b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Reminder\r\n"
        b"TRIGGER:-PT15M\r\nEND:VALARM\r\n",
        b"",
    )
    remaining = todos.plan_update(
        PROFILE,
        session=FakeSession(response(200, raw, {"ETag": '"v1"'}, url=TASK)),
        href=TASK,
        changes={"SUMMARY": "New"},
        now=NOW,
    )
    with pytest.raises(todos.TodoError) as error:
        _apply_bundle(
            remaining,
            FakeSession(response(204, url=TASK), response(200, missing_alarm, url=TASK)),
        )
    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(remaining.plan_id).plan_id == remaining.plan_id


@pytest.mark.parametrize("etag", ["*", 'W/"v1"', "v1"])
def test_task_update_and_delete_require_strong_quoted_etags(etag):
    raw = raw_todo("child", "Old")

    with pytest.raises(todos.TodoError) as update_error:
        todos.plan_update(
            PROFILE,
            session=FakeSession(response(200, raw, {"ETag": etag}, url=TASK)),
            href=TASK,
            changes={"SUMMARY": "New"},
        )
    assert update_error.value.code == exits.MALFORMED_RESPONSE

    with pytest.raises(todos.TodoError) as delete_error:
        todos.plan_delete(
            PROFILE,
            session=FakeSession(response(200, raw, {"ETag": etag}, url=TASK)),
            href=TASK,
        )
    assert delete_error.value.code == exits.MALFORMED_RESPONSE
    assert plans.listing() == []


def test_parent_relation_and_unknown_data_survive_an_unrelated_update():
    rich = raw_todo(
        "child",
        "Old",
        parent_uid="parent",
        sibling_relation="sibling",
        unknown=True,
        alarm=True,
    )
    patched = todos.patch_todo(rich, {"SUMMARY": "New"}, now=NOW)
    assert "SUMMARY:New" in patched
    assert "X-CUSTOM-FIELD:do-not-lose-me" in patched
    assert "RELATED-TO;RELTYPE=PARENT:parent" in patched
    assert "RELATED-TO;RELTYPE=SIBLING:sibling" in patched
    assert "BEGIN:VALARM" in patched


def test_task_report_entry_without_successful_calendar_data_is_malformed():
    body = (
        b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        b"<d:response><d:href>"
        + TASK.encode()
        + b"</d:href></d:response></d:multistatus>"
    )

    with pytest.raises(todos.TodoError) as error:
        todos.query(PROFILE, session=FakeSession(response(207, body)), calendar_href=CAL)

    assert error.value.code == exits.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("UID", "second"),
        ("SUMMARY", "Again"),
        ("DTSTART", dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC)),
        ("DUE", dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)),
        ("COMPLETED", NOW),
        ("STATUS", "COMPLETED"),
        ("PRIORITY", 2),
        ("PERCENT-COMPLETE", 50),
        ("SEQUENCE", 1),
        ("LOCATION", "Somewhere else"),
        ("URL", "https://example.invalid/task"),
    ],
)
def test_task_parser_rejects_duplicate_vtodo_singletons(name, value):
    raw = raw_todo(
        "child",
        "Read me",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        due=dt.datetime(2026, 9, 3, 11, tzinfo=dt.UTC),
        status="needs-action",
        percent_complete=10,
        priority=3,
    )
    raw = add_todo_property(raw, name, value)
    raw = add_todo_property(raw, name, value)

    with pytest.raises(todos.TodoError, match=name) as error:
        todos._describe(raw, calendar_href=CAL, href=TASK, etag='"v1"')

    assert error.value.code == exits.MALFORMED_RESPONSE


def test_refid_is_repeatable_for_reads_but_refused_for_generic_mutation():
    raw = add_todo_property(raw_todo("run-step", "Checkpoint"), "REFID", "run")
    raw = add_todo_property(raw, "REFID", "run")

    reference = todos._describe(raw, calendar_href=CAL, href=TASK, etag='"v1"')

    assert reference.unsupported == ("REFID",)
    assert reference.writable is False
    with pytest.raises(todos.TodoError) as error:
        todos.patch_todo(raw, {"SUMMARY": "No mutation"})
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_task_parser_rejects_duplicate_duration():
    raw = raw_todo(
        "duration",
        "Long task",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
    )
    raw = add_todo_property(raw, "DURATION", dt.timedelta(hours=2))
    raw = add_todo_property(raw, "DURATION", dt.timedelta(hours=2))

    with pytest.raises(todos.TodoError, match="DURATION") as error:
        todos._describe(raw, calendar_href=CAL, href=TASK, etag='"v1"')

    assert error.value.code == exits.MALFORMED_RESPONSE


def test_duration_task_is_readable_but_unwritable_without_losing_duration():
    raw = add_todo_property(
        raw_todo(
            "duration",
            "Long task",
            start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        ),
        "DURATION",
        dt.timedelta(hours=2),
    )
    reference = todos._describe(raw, calendar_href=CAL, href=TASK, etag='"v1"')

    assert reference.writable is False
    assert reference.unsupported == ("DURATION",)
    assert b"DURATION:PT2H\r\n" in raw

    with pytest.raises(todos.TodoError, match="DURATION") as error:
        todos.plan_update(
            PROFILE,
            session=FakeSession(response(200, raw, {"ETag": '"v1"'}, url=TASK)),
            href=TASK,
            changes={"SUMMARY": "No mutation"},
        )

    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_task_parser_rejects_due_and_duration_together():
    raw = add_todo_property(
        raw_todo(
            "duration",
            "Conflicting task",
            start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
            due=dt.datetime(2026, 9, 3, 11, tzinfo=dt.UTC),
        ),
        "DURATION",
        dt.timedelta(hours=2),
    )

    with pytest.raises(todos.TodoError, match="DUE and DURATION") as error:
        todos._describe(raw, calendar_href=CAL, href=TASK, etag='"v1"')

    assert error.value.code == exits.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("STATUS", "NOT-A-STATUS", "STATUS"),
        ("PRIORITY", 10, "PRIORITY"),
        ("PERCENT-COMPLETE", 101, "PERCENT-COMPLETE"),
    ],
)
def test_task_parser_rejects_invalid_scalar_values(name, value, message):
    raw = replace_todo_property(raw_todo("child", "Bad"), name, value)

    with pytest.raises(todos.TodoError, match=message) as error:
        todos._describe(raw, calendar_href=CAL, href=TASK, etag='"v1"')

    assert error.value.code == exits.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    "raw",
    [
        raw_todo("child", "Bad").replace(b"UID:child", b"UID:"),
        replace_todo_property(
            raw_todo("child", "Bad"),
            "DTSTAMP",
            dt.date(2026, 8, 20),
            parameters={"VALUE": "DATE"},
        ),
        replace_todo_property(raw_todo("child", "Bad"), "COMPLETED", dt.date(2026, 8, 20)),
        replace_todo_property(
            raw_todo("child", "Bad"),
            "COMPLETED",
            dt.datetime(2026, 8, 20, 12, 0),
        ),
        replace_todo_property(
            raw_todo("child", "Bad"),
            "COMPLETED",
            dt.datetime(2026, 8, 20, 12, 0),
            parameters={"TZID": "America/Sao_Paulo"},
        ),
        replace_todo_property(
            raw_todo("child", "Bad", start=dt.date(2026, 9, 1), due=dt.date(2026, 9, 3)),
            "DUE",
            dt.datetime(2026, 9, 3, tzinfo=dt.UTC),
        ),
        replace_todo_property(
            raw_todo("child", "Bad", start=dt.date(2026, 9, 1), due=dt.date(2026, 9, 3)),
            "DUE",
            dt.date(2026, 8, 31),
        ),
    ],
)
def test_task_parser_rejects_invalid_identity_and_time_shapes(raw):
    with pytest.raises(todos.TodoError) as error:
        todos._describe(raw, calendar_href=CAL, href=TASK, etag='"v1"')

    assert error.value.code == exits.MALFORMED_RESPONSE


def test_task_list_fails_closed_on_duplicate_uids_and_parent_cycles():
    duplicate_body = report_body(
        report_entry(CAL + "one.ics", raw_todo("same", "One"), '"one"'),
        report_entry(CAL + "two.ics", raw_todo("same", "Two"), '"two"'),
    )
    with pytest.raises(todos.TodoError) as duplicate:
        todos.query(PROFILE, session=FakeSession(response(207, duplicate_body)), calendar_href=CAL)
    assert duplicate.value.code == exits.AMBIGUOUS_TARGET

    cycle_body = report_body(
        report_entry(CAL + "a.ics", raw_todo("a", "A", parent_uid="b"), '"a"'),
        report_entry(CAL + "b.ics", raw_todo("b", "B", parent_uid="a"), '"b"'),
    )
    with pytest.raises(todos.TodoError) as cycle:
        todos.query(PROFILE, session=FakeSession(response(207, cycle_body)), calendar_href=CAL)
    assert cycle.value.code == exits.AMBIGUOUS_TARGET


def test_task_list_retains_a_parent_uid_that_is_outside_the_report():
    body = report_body(
        report_entry(CAL + "child.ics", raw_todo("child", "Child", parent_uid="outside"), '"v1"')
    )

    found = todos.query(PROFILE, session=FakeSession(response(207, body)), calendar_href=CAL)

    assert found[0].parent_uid == "outside"
    assert found[0].children == ()


@pytest.mark.parametrize(
    ("kwargs", "names"),
    [
        ({"recurrence": True}, {"RRULE"}),
        ({"attendee": True}, {"ATTENDEE"}),
    ],
)
def test_recurrence_and_scheduling_are_named_and_refused_on_mutation(kwargs, names):
    raw = raw_todo("blocked", "Blocked", **kwargs)
    reference = todos._describe(
        raw,
        calendar_href=CAL,
        href=CAL + "blocked.ics",
        etag='"v1"',
    )
    assert reference.writable is False
    assert names <= set(reference.unsupported)
    with pytest.raises(todos.TodoError) as error:
        todos.patch_todo(raw, {"SUMMARY": "No"})
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def _patch_cli(monkeypatch, transport, calendars):
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: transport)
    monkeypatch.setattr(cli.identity, "discover", lambda profile, session=None: IDENTITY)
    monkeypatch.setattr(
        cli.caldav,
        "list_calendars",
        lambda profile, session, calendar_home: calendars,
    )


def _task_list_json(monkeypatch, capsys, raw):
    transport = FakeSession(
        response(207, report_body(report_entry(TASK, raw, '"v1"')))
    )
    _patch_cli(monkeypatch, transport, [TASK_CALENDAR])
    code = cli._main(["task", "list", "--json", CAL])
    return code, json.loads(capsys.readouterr().out), transport


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        pytest.param("nested-vtodo", raw_nested_vtodo(), id="nested-vtodo"),
        pytest.param(
            "zero-duration",
            raw_duration_todo(
                "zero", start=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC), duration=dt.timedelta(0)
            ),
            id="zero-duration",
        ),
        pytest.param(
            "negative-duration",
            raw_duration_todo(
                "negative",
                start=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
                duration=dt.timedelta(hours=-1),
            ),
            id="negative-duration",
        ),
        pytest.param(
            "date-sub-day-duration",
            raw_duration_todo(
                "sub-day",
                start=dt.date(2026, 9, 1),
                duration=dt.timedelta(hours=1),
            ),
            id="date-sub-day-duration",
        ),
    ],
)
def test_task_list_rejects_malformed_vtodo_shapes(label, raw, monkeypatch, capsys):
    code, error, transport = _task_list_json(monkeypatch, capsys, raw)

    assert code == exits.MALFORMED_RESPONSE, label
    assert error["code"] == exits.MALFORMED_RESPONSE
    assert [request["method"] for request in transport.requests] == ["REPORT"]


def test_task_list_reads_whole_day_duration_with_date_start(monkeypatch, capsys):
    code, output, transport = _task_list_json(
        monkeypatch,
        capsys,
        raw_duration_todo(
            "whole-day", start=dt.date(2026, 9, 1), duration=dt.timedelta(days=1)
        ),
    )

    assert code == exits.OK
    assert output["tasks"][0]["uid"] == "whole-day"
    assert output["tasks"][0]["dtstart"] == "20260901"
    assert output["tasks"][0]["writable"] is False
    assert output["tasks"][0]["unsupported"] == ["DURATION"]
    assert [request["method"] for request in transport.requests] == ["REPORT"]


def test_task_list_keeps_direct_sibling_component_readable_but_unwritable(monkeypatch, capsys):
    code, output, transport = _task_list_json(monkeypatch, capsys, raw_direct_sibling_event())

    assert code == exits.OK
    assert output["tasks"][0]["writable"] is False
    assert output["tasks"][0]["unsupported"] == ["VEVENT"]
    assert [request["method"] for request in transport.requests] == ["REPORT"]


def test_cli_json_text_and_distinct_collection_exit_paths(monkeypatch, capsys):
    raw = raw_todo("child", "Read me", due=dt.date(2026, 9, 3))
    _patch_cli(monkeypatch, FakeSession(response(200, raw, {"ETag": '"v1"'})), [TASK_CALENDAR])
    assert cli._main(["task", "show", "--json", TASK]) == exits.OK
    structured = json.loads(capsys.readouterr().out)
    assert structured["task"]["due"] == "20260903"
    assert structured["task"]["dtstart"] == ""

    _patch_cli(monkeypatch, FakeSession(response(200, raw, {"ETag": '"v1"'})), [TASK_CALENDAR])
    assert cli._main(["task", "show", TASK]) == exits.OK
    text = capsys.readouterr().out
    assert "summary: Read me" in text
    assert "completed: " in text

    _patch_cli(monkeypatch, FakeSession(), [EVENT_CALENDAR])
    code = cli._main(["task", "list", "--json", "Events"])
    assert code == exits.UNSUPPORTED_COLLECTION
    error = json.loads(capsys.readouterr().out)
    assert error["code"] == exits.UNSUPPORTED_COLLECTION

    _patch_cli(monkeypatch, FakeSession(), [EVENT_CALENDAR])
    code = cli._main(["task", "create", "Events", "--summary", "No", "--json"])
    error = json.loads(capsys.readouterr().out)
    assert code == exits.UNSUPPORTED_COLLECTION
    assert error["code"] == exits.UNSUPPORTED_COLLECTION
    assert plans.listing() == []


def test_cli_apply_claims_before_loading_and_dispatching_a_task_plan(monkeypatch):
    order: list[str] = []

    class Claim:
        def __enter__(self):
            order.append("claim")

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(plans, "claim", lambda plan_id: Claim())
    monkeypatch.setattr(
        plans,
        "read",
        lambda plan_id: order.append("read")
        or SimpleNamespace(plan_id=plan_id, profile="home", action="task.create"),
    )
    monkeypatch.setattr(cli.session, "Session", lambda profile: object())
    monkeypatch.setattr(plans, "apply", lambda *args, **kwargs: order.append("apply") or {})

    assert cli._run_apply(SimpleNamespace(plan_id="stale", json=True)) == exits.OK
    assert order == ["claim", "read", "apply"]


def test_task_help_states_report_and_exact_delete_contracts(capsys):
    with pytest.raises(SystemExit):
        cli.main(["task", "list", "--help"])
    list_help = " ".join(capsys.readouterr().out.split())
    assert "one CalDAV REPORT" in list_help

    with pytest.raises(SystemExit):
        cli.main(["task", "delete", "--help"])
    delete_help = " ".join(capsys.readouterr().out.split())
    assert "exact task href is absent" in delete_help
