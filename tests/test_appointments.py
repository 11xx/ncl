from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import icalendar
import pytest

from ncl import appointments, cli, events, exits, plans
from ncl.config import Profile
from ncl.session import Response

PROFILE = Profile(
    "home",
    "https://cloud.example.invalid",
    "pass",
    ("/remote.php/dav/calendars/alice/work/",),
    ("/remote.php/dav/files/alice/",),
)
CAL = "https://cloud.example.invalid/remote.php/dav/calendars/alice/work/"
START = dt.datetime(2026, 9, 1, 14, tzinfo=dt.timezone(dt.timedelta(hours=-3)))
END = dt.datetime(2026, 9, 1, 15, tzinfo=dt.timezone(dt.timedelta(hours=-3)))


class GetSession:
    def __init__(self, resources: dict[str, tuple[bytes, str]]):
        self.resources = resources
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, data=None, **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers, "data": data})
        assert method == "GET"
        raw, etag = self.resources[url]
        return Response(200, {"ETag": etag}, raw, url)


class ScriptedSession:
    def __init__(self, *responses: Response):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, data=None, **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers, "data": data})
        assert self.responses, f"unexpected request {method} {url}"
        return self.responses.pop(0)


def _response(status: int, *, body: bytes = b"", etag: str = '"v2"', url: str = ""):
    headers = {"ETag": etag} if etag else {}
    return Response(status, headers, body, url)


def _create_plan(*, route_url: str | None = "https://maps.example/route") -> plans.Plan:
    return appointments.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Appointment",
        start=START,
        end=END,
        origin="Home",
        destination="Rua A, 10",
        mode="bus",
        route_estimate="PT1H",
        on_site_buffer="PT15M",
        stop_wait_margin="PT10M",
        preparation_duration="PT20M",
        route_url=route_url,
        description="Bring the documents.",
    )


def _component(raw: bytes):
    calendar = icalendar.Calendar.from_ical(raw)
    return next(item for item in calendar.walk() if item.name == "VEVENT")


def _description(raw: bytes) -> str:
    value = _component(raw).get("DESCRIPTION")
    return "" if value is None else str(value)


def _relations(raw: bytes) -> set[tuple[str, str]]:
    component = _component(raw)
    return {
        (str(value), str(value.params["RELTYPE"]))
        for name, value in component.property_items()
        if name == "RELATED-TO"
    }


def _resources(plan: plans.Plan, *, etag: str = '"v1"') -> dict[str, tuple[bytes, str]]:
    return {step.href: (plans.payload_bytes(step), etag) for step in plan.steps}


def _bundle_hrefs(plan: plans.Plan) -> tuple[str, str, str]:
    return tuple(step.href for step in plan.steps)


def _update_plan(
    resources: dict[str, tuple[bytes, str]],
    session: GetSession | None = None,
    **kwargs,
) -> plans.Plan:
    appointment_href, travel_href, preparation_href = tuple(resources)
    return appointments.plan_update(
        PROFILE,
        session=session or GetSession(resources),
        appointment_href=appointment_href,
        travel_href=travel_href,
        preparation_href=preparation_href,
        start=kwargs.pop("start", START + dt.timedelta(days=1)),
        end=kwargs.pop("end", END + dt.timedelta(days=1)),
        origin=kwargs.pop("origin", "Home"),
        destination=kwargs.pop("destination", "Rua B, 20"),
        mode=kwargs.pop("mode", "train"),
        route_estimate=kwargs.pop("route_estimate", "PT2H"),
        on_site_buffer=kwargs.pop("on_site_buffer", "PT10M"),
        stop_wait_margin=kwargs.pop("stop_wait_margin", "PT5M"),
        preparation_duration=kwargs.pop("preparation_duration", "PT30M"),
        **kwargs,
    )


def _apply(plan: plans.Plan, session: ScriptedSession):
    with plans.claim(plan.plan_id):
        return plans.apply(PROFILE, session=session, plan=plan, dispatchers=cli._dispatchers())


def _readback_script(
    plan: plans.Plan, *, put_status: int = 204, start_index: int = 0
) -> ScriptedSession:
    responses: list[Response] = []
    for step in plan.steps[start_index:]:
        responses.extend(
            [
                _response(put_status, url=step.href),
                _response(
                    200,
                    body=plans.payload_bytes(step),
                    etag='"v2"',
                    url=step.href,
                ),
            ]
        )
    return ScriptedSession(*responses)


def test_timeline_uses_exact_backward_arithmetic():
    timeline = appointments.calculate_timeline(
        start=START,
        end=END,
        route_estimate="PT1H",
        on_site_buffer="PT15M",
        stop_wait_margin="PT10M",
        preparation_duration="PT20M",
    )

    assert timeline.target_arrival == START - dt.timedelta(minutes=15)
    assert timeline.planned_departure == START - dt.timedelta(hours=1, minutes=15)
    assert timeline.leave_home == START - dt.timedelta(hours=1, minutes=25)
    assert timeline.preparation_start == START - dt.timedelta(hours=1, minutes=45)
    assert timeline.appointment_end == END


@pytest.mark.parametrize(
    ("value", "positive"),
    [
        ("P1M", False),
        ("P1Y", False),
        ("-PT1M", False),
        ("PT1.5S", False),
        ("PT0S", True),
    ],
)
def test_duration_parser_rejects_unsupported_or_invalid_values(value, positive):
    with pytest.raises(appointments.AppointmentError) as error:
        appointments.parse_duration(value, "duration", positive=positive)
    assert error.value.code == exits.USAGE


def test_invalid_time_and_arithmetic_inputs_are_usage_errors():
    with pytest.raises(appointments.AppointmentError) as naive:
        appointments.parse_instant("2026-09-01T14:00:00", "--from")
    assert naive.value.code == exits.USAGE

    with pytest.raises(appointments.AppointmentError) as fractional:
        appointments.parse_instant("2026-09-01T14:00:00.500+00:00", "--from")
    assert fractional.value.code == exits.USAGE

    with pytest.raises(appointments.AppointmentError) as backwards:
        appointments.calculate_timeline(
            start=END,
            end=START,
            route_estimate="PT1H",
            on_site_buffer="PT0S",
            stop_wait_margin="PT0S",
            preparation_duration="PT1M",
        )
    assert backwards.value.code == exits.USAGE

    with pytest.raises(appointments.AppointmentError) as overflow:
        appointments.calculate_timeline(
            start=dt.datetime.min.replace(tzinfo=dt.UTC),
            end=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
            route_estimate="PT1H",
            on_site_buffer="P1D",
            stop_wait_margin="PT0S",
            preparation_duration="PT1M",
        )
    assert overflow.value.code == exits.USAGE


def test_create_plan_has_three_redacted_steps_and_typed_stable_relations(monkeypatch):
    monkeypatch.setattr(appointments.token_source, "token_hex", lambda size: "bundle-token")
    plan = _create_plan(route_url=None)

    assert [step.details["role"] for step in plan.steps] == [
        "appointment",
        "travel",
        "preparation",
    ]
    assert [step.action for step in plan.steps] == ["cal.create"] * 3
    assert len({step.details["uid"] for step in plan.steps}) == 3
    assert all(
        "payload" not in step and "payload_bytes" in step
        for step in plan.as_dict()["steps"]
    )

    appointment, travel, preparation = (
        _component(plans.payload_bytes(step)) for step in plan.steps
    )
    assert appointment.get("SUMMARY") == "Appointment"
    assert appointment.get("LOCATION") == "Rua A, 10"
    assert _description(plans.payload_bytes(plan.steps[0])) == "Bring the documents."
    assert _relations(plans.payload_bytes(plan.steps[0])) == {
        (plan.steps[1].details["uid"], "CHILD"),
        (plan.steps[2].details["uid"], "CHILD"),
    }
    assert _relations(plans.payload_bytes(plan.steps[1])) == {
        (plan.steps[0].details["uid"], "PARENT")
    }
    assert _relations(plans.payload_bytes(plan.steps[2])) == {
        (plan.steps[0].details["uid"], "PARENT")
    }
    travel_description = _description(plans.payload_bytes(plan.steps[1]))
    assert "ORIGIN: Home" in travel_description
    assert "DESTINATION: Rua A, 10" in travel_description
    assert "ROUTE URL:" not in travel_description
    assert travel.get("URL") is None
    assert preparation.get("DESCRIPTION") is None


def test_appointment_help_teaches_fact_set_and_route_modes(capsys):
    with pytest.raises(SystemExit):
        cli.main(["cal", "appointment", "create", "--help"])
    create_help = " ".join(capsys.readouterr().out.split())
    for option in (
        "--origin",
        "--destination",
        "--mode",
        "--route-estimate",
        "--on-site-buffer",
        "--stop-wait-margin",
        "--preparation-duration",
        "--route-url",
    ):
        assert option in create_help
    assert "never fetched" in create_help

    with pytest.raises(SystemExit):
        cli.main(["cal", "appointment", "update", "--help"])
    update_help = " ".join(capsys.readouterr().out.split())
    assert "Exact appointment event href" in update_help
    assert "Exact travel event href" in update_help
    assert "Exact preparation event href" in update_help
    assert "--clear-route-url" in update_help
    assert "omission preserves" in update_help


def test_cli_create_plans_without_a_put_and_exposes_exact_order(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli, "_resolved_calendar", lambda profile, transport, target: CAL)
    session = SimpleNamespace(request=lambda *args, **kwargs: pytest.fail("unexpected request"))
    monkeypatch.setattr(cli.session, "Session", lambda profile: session)

    code = cli.main(
        [
            "cal",
            "appointment",
            "create",
            CAL,
            "--summary",
            "CLI appointment",
            "--from",
            "2026-09-01T14:00:00-03:00",
            "--to",
            "2026-09-01T15:00:00-03:00",
            "--origin",
            "Home",
            "--destination",
            "Rua A, 10",
            "--mode",
            "bus",
            "--route-estimate",
            "PT1H",
            "--on-site-buffer",
            "PT15M",
            "--stop-wait-margin",
            "PT10M",
            "--preparation-duration",
            "PT20M",
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == exits.CONFIRMATION_REQUIRED
    assert [step["details"]["role"] for step in output["plan"]["steps"]] == [
        "appointment",
        "travel",
        "preparation",
    ]
    assert all("payload" not in step for step in output["plan"]["steps"])


def test_update_reads_exact_hrefs_and_preserves_route_and_authored_text():
    create = _create_plan()
    resources = _resources(create)
    plans.consume(create.plan_id)
    travel_href = create.steps[1].href
    travel_raw, etag = resources[travel_href]
    travel_calendar = icalendar.Calendar.from_ical(travel_raw)
    travel_event = next(item for item in travel_calendar.walk() if item.name == "VEVENT")
    original_description = str(travel_event.get("DESCRIPTION"))
    travel_event.pop("DESCRIPTION")
    travel_event.add(
        "DESCRIPTION",
        f"Authored travel note.\n\n{original_description}\n\nKeep this.",
    )
    resources[travel_href] = (travel_calendar.to_ical(), etag)
    session = GetSession(resources)

    plan = _update_plan(resources, session=session)

    assert [request["url"] for request in session.requests] == list(resources)
    assert [step.etag for step in plan.steps] == ['"v1"'] * 3
    appointment = _component(plans.payload_bytes(plan.steps[0]))
    travel = _component(plans.payload_bytes(plan.steps[1]))
    assert appointment.get("UID") == create.steps[0].details["uid"]
    assert appointment.get("LOCATION") == "Rua B, 20"
    assert "Authored travel note." in str(travel.get("DESCRIPTION"))
    assert "Keep this." in str(travel.get("DESCRIPTION"))
    assert "ROUTE URL: https://maps.example/route" in str(travel.get("DESCRIPTION"))
    assert travel.get("URL") == "https://maps.example/route"
    assert _relations(plans.payload_bytes(plan.steps[0])) == _relations(
        plans.payload_bytes(create.steps[0])
    )
    assert _relations(plans.payload_bytes(plan.steps[1])) == _relations(
        plans.payload_bytes(create.steps[1])
    )
    assert _relations(plans.payload_bytes(plan.steps[2])) == _relations(
        plans.payload_bytes(create.steps[2])
    )
    assert [step.action for step in plan.steps] == ["cal.update"] * 3


@pytest.mark.parametrize(
    ("route_url", "clear", "expected"),
    [("https://maps.example/new", False, "https://maps.example/new"), (None, True, None)],
)
def test_update_replaces_or_clears_route_url(route_url, clear, expected):
    create = _create_plan()
    resources = _resources(create)
    plans.consume(create.plan_id)
    plan = _update_plan(
        resources,
        route_url=route_url,
        clear_route_url=clear,
    )

    travel = _component(plans.payload_bytes(plan.steps[1]))
    description = str(travel.get("DESCRIPTION"))
    assert (str(travel.get("URL")) if travel.get("URL") is not None else None) == expected
    if expected is None:
        assert "ROUTE URL:" not in description
    else:
        assert f"ROUTE URL: {expected}" in description


@pytest.mark.parametrize("bad_href", ["duplicate", "weak-etag", "bad-topology", "bad-block"])
def test_update_refuses_ambiguous_or_unsupported_bundles_before_plan_write(bad_href):
    create = _create_plan()
    resources = _resources(create)
    plans.consume(create.plan_id)
    appointment_href, travel_href, preparation_href = _bundle_hrefs(create)
    if bad_href == "duplicate":
        with pytest.raises(appointments.AppointmentError) as error:
            appointments.plan_update(
                PROFILE,
                session=GetSession(resources),
                appointment_href=appointment_href,
                travel_href=appointment_href,
                preparation_href=preparation_href,
                start=START,
                end=END,
                origin="Home",
                destination="Rua B, 20",
                mode="train",
                route_estimate="PT2H",
                on_site_buffer="PT10M",
                stop_wait_margin="PT5M",
                preparation_duration="PT30M",
            )
        assert error.value.code == exits.UNSUPPORTED_STRUCTURE
        assert plans.listing() == []
        return

    if bad_href == "weak-etag":
        resources[travel_href] = (resources[travel_href][0], 'W/"v1"')
    elif bad_href == "bad-topology":
        raw = resources[appointment_href][0]
        calendar = icalendar.Calendar.from_ical(raw)
        event = next(item for item in calendar.walk() if item.name == "VEVENT")
        event.pop("RELATED-TO")
        resources[appointment_href] = (calendar.to_ical(), '"v1"')
    else:
        raw = resources[travel_href][0]
        calendar = icalendar.Calendar.from_ical(raw)
        event = next(item for item in calendar.walk() if item.name == "VEVENT")
        description = str(event.get("DESCRIPTION"))
        event.pop("DESCRIPTION")
        event.add("DESCRIPTION", description + "\n" + appointments.TRAVEL_BLOCK_START)
        resources[travel_href] = (calendar.to_ical(), '"v1"')

    with pytest.raises((appointments.AppointmentError, events.EventError)) as error:
        _update_plan(resources)
    assert error.value.code in {exits.MALFORMED_RESPONSE, exits.UNSUPPORTED_STRUCTURE}
    assert plans.listing() == []


@pytest.mark.parametrize(
    ("property_name", "value"),
    [
        ("RRULE", {"FREQ": ["WEEKLY"]}),
        ("RDATE", START + dt.timedelta(days=7)),
        ("EXDATE", START + dt.timedelta(days=7)),
        ("EXRULE", {"FREQ": ["WEEKLY"]}),
        ("RECURRENCE-ID", START),
        ("ATTENDEE", "mailto:someone@example.invalid"),
        ("ORGANIZER", "mailto:someone@example.invalid"),
    ],
)
def test_update_refuses_recurrence_and_scheduling_structure(property_name, value):
    create = _create_plan()
    resources = _resources(create)
    plans.consume(create.plan_id)
    travel_href = create.steps[1].href
    raw = resources[travel_href][0]
    calendar = icalendar.Calendar.from_ical(raw)
    event = next(item for item in calendar.walk() if item.name == "VEVENT")
    event.add(property_name, value)
    resources[travel_href] = (calendar.to_ical(), '"v1"')

    with pytest.raises(events.EventError) as error:
        _update_plan(resources)
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE
    assert plans.listing() == []


def test_create_apply_uses_appointment_travel_preparation_put_get_order_and_headers():
    plan = _create_plan()
    session = _readback_script(plan, put_status=201)

    result = _apply(plan, session)

    assert [request["method"] for request in session.requests] == [
        "PUT",
        "GET",
        "PUT",
        "GET",
        "PUT",
        "GET",
    ]
    assert [request["url"] for request in session.requests] == [
        value for step in plan.steps for value in (step.href, step.href)
    ]
    assert session.requests[0]["headers"]["If-None-Match"] == "*"
    assert session.requests[2]["headers"]["If-None-Match"] == "*"
    assert session.requests[4]["headers"]["If-None-Match"] == "*"
    assert result["complete"] is True
    with pytest.raises(plans.PlanError) as error:
        plans.read(plan.plan_id)
    assert error.value.code == exits.TARGET_NOT_FOUND


def test_update_apply_uses_each_captured_strong_etag_and_reads_back_in_order():
    create = _create_plan()
    resources = _resources(create)
    plans.consume(create.plan_id)
    plan = _update_plan(resources)
    session = _readback_script(plan)

    result = _apply(plan, session)

    put_requests = session.requests[::2]
    get_requests = session.requests[1::2]
    assert [request["headers"]["If-Match"] for request in put_requests] == ['"v1"'] * 3
    assert [request["headers"]["Accept"] for request in get_requests] == [
        "text/calendar"
    ] * 3
    assert [request["url"] for request in put_requests] == [step.href for step in plan.steps]
    assert [request["url"] for request in get_requests] == [step.href for step in plan.steps]
    assert result["complete"] is True


def test_update_apply_stops_after_partial_failure_and_resumes_pending_step():
    create = _create_plan()
    resources = _resources(create)
    plans.consume(create.plan_id)
    plan = _update_plan(resources)
    first = plan.steps[0]
    failing = ScriptedSession(
        _response(204, url=first.href),
        _response(200, body=plans.payload_bytes(first), url=first.href),
        _response(500, url=plan.steps[1].href),
    )

    with pytest.raises(events.EventError) as error:
        _apply(plan, failing)
    assert error.value.code == exits.SERVER_ERROR
    stored = plans.read(plan.plan_id)
    assert [item.state for item in stored.progress] == ["verified", "pending", "pending"]

    resumed = _readback_script(stored, start_index=1)
    _apply(stored, resumed)
    assert [request["url"] for request in resumed.requests] == [
        value for step in stored.steps[1:] for value in (step.href, step.href)
    ]


def test_uncertain_readback_blocks_later_steps_until_reconcile():
    plan = _create_plan()
    altered = bytearray(plans.payload_bytes(plan.steps[0]))
    altered = bytes(altered).replace(b"SUMMARY:Appointment", b"SUMMARY:Different")
    session = ScriptedSession(
        _response(201, url=plan.steps[0].href),
        _response(200, body=altered, url=plan.steps[0].href),
    )

    with pytest.raises(events.EventError) as error:
        _apply(plan, session)
    assert error.value.code == exits.OUTCOME_UNCERTAIN
    stored = plans.read(plan.plan_id)
    assert [item.state for item in stored.progress] == ["uncertain", "pending", "pending"]

    reconcile = ScriptedSession(
        _response(200, body=plans.payload_bytes(plan.steps[0]), url=plan.steps[0].href)
    )
    with plans.claim(plan.plan_id):
        result = plans.reconcile(
            PROFILE,
            session=reconcile,
            plan=stored,
            dispatchers=cli._dispatchers(),
        )
    assert result["state"] == "verified"
