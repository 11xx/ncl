"""Calendar collection creation and event relocation over CalDAV."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from ncl import caldav, cli, events, exits, mutate, plans
from ncl.config import Profile
from ncl.session import Response

PROFILE = Profile(
    "home",
    "https://cloud.example.invalid",
    "pass",
    (
        "/remote.php/dav/calendars/alice/work/",
        "/remote.php/dav/calendars/alice/employment/",
    ),
    ("/remote.php/dav/files/alice/",),
)
HOME = "https://cloud.example.invalid/remote.php/dav/calendars/alice/"
WORK = HOME + "work/"
EMPLOYMENT = HOME + "employment/"
SOURCE = WORK + "offer.ics"
DESTINATION = EMPLOYMENT + "offer.ics"

EVENT = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Some Other Client//EN
BEGIN:VEVENT
UID:offer@example
SUMMARY:Offer signed
DTSTART:20260901T090000Z
DTEND:20260901T100000Z
DTSTAMP:20260817T120000Z
CATEGORIES:employment
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
DESCRIPTION:Reminder
END:VALARM
END:VEVENT
END:VCALENDAR
"""

COLLECTION = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
               xmlns:o="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/employment/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
      <d:displayname>{name}</d:displayname>
      <c:calendar-description>{description}</c:calendar-description>
      <o:calendar-color>{color}</o:calendar-color>
      <d:current-user-privilege-set>
        <d:privilege><d:write/></d:privilege>
      </d:current-user-privilege-set>
      <c:supported-calendar-component-set>{components}</c:supported-calendar-component-set>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>
"""


def collection(
    *,
    name: str = "Employment",
    description: str = "The relationship, not the search",
    color: str = "#2196F3",
    components: tuple[str, ...] = ("VEVENT", "VTODO"),
) -> bytes:
    return COLLECTION.format(
        name=name,
        description=description,
        color=color,
        components="".join(f'<c:comp name="{item}"/>' for item in components),
    ).encode()


class FakeSession:
    def __init__(self, *responses: Response | Exception):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, data=None, **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers, "data": data})
        assert self.responses, f"unexpected request: {method} {url}"
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def response(
    status: int,
    body: bytes = b"",
    headers: Mapping[str, str] | None = None,
) -> Response:
    return Response(status, headers or {}, body, "")


def stored_event(etag: str = '"v1"') -> Response:
    return response(200, EVENT, {"ETag": etag})


def _apply_bundle(plan, transport):
    with plans.claim(plan.plan_id):
        return plans.apply(
            PROFILE, session=transport, plan=plan, dispatchers=cli._dispatchers()
        )


def _move_plan(transport) -> plans.Plan:
    return mutate.plan_move(
        PROFILE, session=transport, href=SOURCE, destination_calendar_href=EMPLOYMENT
    )


def test_a_move_relocates_the_resource_and_leaves_nothing_behind():
    transport = FakeSession(
        stored_event(),
        response(404),
        response(201),
        stored_event('"v2"'),
        response(404),
    )

    plan = _move_plan(transport)
    step = plan.steps[0]

    assert step.action == "cal.move"
    assert step.href == SOURCE
    assert step.details["destination"] == DESTINATION

    result = _apply_bundle(plan, transport)
    move = next(item for item in transport.requests if item["method"] == "MOVE")

    assert move["url"] == SOURCE
    assert move["headers"]["Destination"] == DESTINATION
    assert move["headers"]["Overwrite"] == "F"
    assert move["headers"]["If-Match"] == '"v1"'
    assert result["verified"] is True
    assert result["href"] == DESTINATION
    assert result["moved_from"] == SOURCE
    assert result["uid"] == "offer@example"


def test_a_move_keeps_the_resource_name_so_references_survive():
    transport = FakeSession(stored_event(), response(404))

    assert _move_plan(transport).steps[0].details["destination"] == DESTINATION


def test_a_move_onto_an_occupied_destination_is_refused_while_planning():
    transport = FakeSession(stored_event(), stored_event('"other"'))

    with pytest.raises(events.EventError) as refusal:
        _move_plan(transport)

    assert refusal.value.code == exits.CONFLICT
    assert not any(item["method"] == "MOVE" for item in transport.requests)


def test_a_move_into_a_calendar_outside_the_allowlist_is_refused():
    transport = FakeSession(stored_event())

    with pytest.raises(caldav.CalendarError) as refusal:
        mutate.plan_move(
            PROFILE,
            session=transport,
            href=SOURCE,
            destination_calendar_href=HOME + "personal/",
        )

    assert refusal.value.code == exits.SCOPE_DENIED


def test_a_move_into_the_calendar_that_already_holds_it_is_a_usage_error():
    transport = FakeSession(stored_event())

    with pytest.raises(events.EventError) as refusal:
        mutate.plan_move(
            PROFILE, session=transport, href=SOURCE, destination_calendar_href=WORK
        )

    assert refusal.value.code == exits.USAGE


def test_a_destination_holding_a_different_resource_is_uncertain():
    transport = FakeSession(stored_event(), response(404))
    plan = _move_plan(transport)
    different = EVENT.replace(b"UID:offer@example", b"UID:someone-else@example")
    applying = FakeSession(response(201), response(200, different, {"ETag": '"v9"'}))

    with pytest.raises(events.EventError) as refusal:
        _apply_bundle(plan, applying)

    assert refusal.value.code == exits.OUTCOME_UNCERTAIN


def test_a_source_that_survives_the_move_is_uncertain():
    transport = FakeSession(stored_event(), response(404))
    plan = _move_plan(transport)
    applying = FakeSession(response(201), stored_event('"v2"'), stored_event('"v1"'))

    with pytest.raises(events.EventError) as refusal:
        _apply_bundle(plan, applying)

    assert refusal.value.code == exits.OUTCOME_UNCERTAIN


def test_a_move_the_server_reports_as_an_overwrite_is_uncertain():
    transport = FakeSession(stored_event(), response(404))
    plan = _move_plan(transport)
    applying = FakeSession(response(204))

    with pytest.raises(events.EventError) as refusal:
        _apply_bundle(plan, applying)

    assert refusal.value.code == exits.OUTCOME_UNCERTAIN


def test_a_move_refused_with_412_conflicts_without_moving_anything():
    transport = FakeSession(stored_event(), response(404))
    plan = _move_plan(transport)
    applying = FakeSession(response(412))

    with pytest.raises(events.EventError) as refusal:
        _apply_bundle(plan, applying)

    assert refusal.value.code == exits.CONFLICT


def test_a_move_that_never_reached_the_server_reconciles_as_pending():
    transport = FakeSession(stored_event(), response(404))
    step = _move_plan(transport).steps[0]
    reader = FakeSession(response(404), stored_event())

    assert mutate.reconcile(PROFILE, session=reader, step=step) == {"state": "pending"}


def test_a_completed_move_reconciles_as_verified():
    transport = FakeSession(stored_event(), response(404))
    step = _move_plan(transport).steps[0]
    reader = FakeSession(stored_event('"v2"'), response(404))

    assert mutate.reconcile(PROFILE, session=reader, step=step) == {"state": "verified"}


def _mkcalendar_plan(transport, **overrides) -> plans.Plan:
    arguments = {
        "href": EMPLOYMENT,
        "display_name": "Employment",
        "description": "The relationship, not the search",
        "color": "#2196F3",
    }
    arguments.update(overrides)
    return mutate.plan_mkcalendar(PROFILE, session=transport, **arguments)


def test_a_new_calendar_defaults_to_accepting_events_and_tasks():
    transport = FakeSession(response(404), response(201), response(207, collection()))

    plan = _mkcalendar_plan(transport)
    body = plans.payload_bytes(plan.steps[0]).decode()

    assert plan.steps[0].action == "cal.mkcalendar"
    assert '<c:comp name="VEVENT"/>' in body
    assert '<c:comp name="VTODO"/>' in body

    result = _apply_bundle(plan, transport)
    created = next(item for item in transport.requests if item["method"] == "MKCALENDAR")

    assert created["url"] == EMPLOYMENT
    assert "<d:displayname>Employment</d:displayname>" in created["data"].decode()
    assert result["verified"] is True
    assert result["components"] == ["VEVENT", "VTODO"]
    assert result["in_scope"] is True


def test_a_calendar_colour_is_reported_as_stored_rather_than_asserted():
    transport = FakeSession(
        response(404), response(201), response(207, collection(color="#2196F3FF"))
    )

    result = _apply_bundle(_mkcalendar_plan(transport), transport)

    assert result["verified"] is True
    assert result["color"] == "#2196F3FF"


def test_a_display_name_the_server_changed_is_uncertain():
    transport = FakeSession(
        response(404), response(201), response(207, collection(name="Something else"))
    )
    plan = _mkcalendar_plan(transport)

    with pytest.raises(caldav.CalendarError) as refusal:
        _apply_bundle(plan, transport)

    assert refusal.value.code == exits.OUTCOME_UNCERTAIN


def test_a_component_set_the_server_narrowed_is_uncertain():
    transport = FakeSession(
        response(404),
        response(201),
        response(207, collection(components=("VEVENT",))),
    )
    plan = _mkcalendar_plan(transport)

    with pytest.raises(caldav.CalendarError) as refusal:
        _apply_bundle(plan, transport)

    assert refusal.value.code == exits.OUTCOME_UNCERTAIN


def test_creating_a_calendar_that_exists_is_refused_while_planning():
    transport = FakeSession(response(207, collection()))

    with pytest.raises(caldav.CalendarError) as refusal:
        _mkcalendar_plan(transport)

    assert refusal.value.code == exits.CONFLICT
    assert not any(item["method"] == "MKCALENDAR" for item in transport.requests)


def test_creating_a_calendar_outside_the_allowlist_is_refused():
    transport = FakeSession()

    with pytest.raises(caldav.CalendarError) as refusal:
        _mkcalendar_plan(transport, href=HOME + "personal/")

    assert refusal.value.code == exits.SCOPE_DENIED
    assert transport.requests == []


def test_a_calendar_needs_a_display_name():
    transport = FakeSession()

    with pytest.raises(caldav.CalendarError) as refusal:
        _mkcalendar_plan(transport, display_name="  ")

    assert refusal.value.code == exits.USAGE


def test_a_calendar_the_server_never_created_reconciles_as_pending():
    transport = FakeSession(response(404))
    step = plans.freeze_step(
        action="cal.mkcalendar",
        href=EMPLOYMENT,
        etag="",
        summary="Employment",
        payload=b"<x/>",
        content_type="application/xml; charset=utf-8",
        details={"display_name": "Employment", "description": "", "components": []},
    )

    assert mutate.reconcile(PROFILE, session=transport, step=step) == {"state": "pending"}


def test_cal_collection_reports_what_a_calendar_is(monkeypatch, capsys):
    import json

    transport = FakeSession(response(207, collection()))
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: transport)
    monkeypatch.setattr(
        cli, "_resolved_calendar", lambda profile, session, target: EMPLOYMENT
    )

    code = cli.main(["cal", "collection", "Employment", "--json"])
    shown = json.loads(capsys.readouterr().out)["calendar"]

    assert code == exits.OK
    assert shown["description"] == "The relationship, not the search"
    assert shown["color"] == "#2196F3"
    assert shown["components"] == ["VEVENT", "VTODO"]


def test_cal_move_plans_without_changing_anything(monkeypatch, capsys):
    transport = FakeSession(stored_event(), response(404))
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: transport)
    monkeypatch.setattr(
        cli, "_resolved_calendar", lambda profile, session, target: EMPLOYMENT
    )

    code = cli.main(["cal", "move", SOURCE, "--to", "Employment"])
    printed = capsys.readouterr().out

    assert code == exits.CONFIRMATION_REQUIRED
    assert DESTINATION in printed
    assert not any(item["method"] == "MOVE" for item in transport.requests)


def test_cal_mkcalendar_plans_without_changing_anything(monkeypatch, capsys):
    transport = FakeSession(response(404))
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: transport)

    code = cli.main(
        ["cal", "mkcalendar", EMPLOYMENT, "--displayname", "Employment", "--component", "VTODO"]
    )
    printed = capsys.readouterr().out

    assert code == exits.CONFIRMATION_REQUIRED
    assert "cal.mkcalendar" in printed
    assert not any(item["method"] == "MKCALENDAR" for item in transport.requests)
