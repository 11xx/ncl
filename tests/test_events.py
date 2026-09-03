from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import icalendar
import pytest
from test_auth import credential_reader

from ncl import cli, events, exits, mutate, plans
from ncl import session as http_session
from ncl.caldav import CalendarError
from ncl.config import Profile

PROFILE = Profile(
    "home",
    "https://cloud.example.invalid",
    "pass",
    ("/remote.php/dav/calendars/alice/work/",),
    ("/remote.php/dav/files/alice/",),
)
CAL = "https://cloud.example.invalid/remote.php/dav/calendars/alice/work/"

# An event written by a web UI: a VTIMEZONE, an alarm, an unknown X- property,
# and a category. None of it is modelled by this tool, and all of it must
# survive an update that only changes the summary.
RICH = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Some Other Client//EN
BEGIN:VTIMEZONE
TZID:America/Sao_Paulo
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:-0300
TZOFFSETTO:-0300
TZNAME:-03
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:keep-me@example
SUMMARY:Original
DTSTART;TZID=America/Sao_Paulo:20260901T110000
DTEND;TZID=America/Sao_Paulo:20260901T120000
DTSTAMP:20260817T120000Z
SEQUENCE:3
LOCATION:Somewhere
CATEGORIES:WORK
X-CUSTOM-FIELD:do-not-lose-me
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
DESCRIPTION:Reminder
END:VALARM
END:VEVENT
END:VCALENDAR
"""

RECURRING = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series@example
SUMMARY:Standup
DTSTART:20260901T090000Z
DTEND:20260901T091500Z
DTSTAMP:20260817T120000Z
RRULE:FREQ=WEEKLY
END:VEVENT
END:VCALENDAR
"""

WITH_ATTENDEE = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:meeting@example
SUMMARY:Review
DTSTART:20260901T090000Z
DTEND:20260901T100000Z
DTSTAMP:20260817T120000Z
ORGANIZER:mailto:alice@example.invalid
ATTENDEE:mailto:bob@example.invalid
END:VEVENT
END:VCALENDAR
"""


def _ref(raw=RICH, etag='"v1"'):
    return events._describe(
        raw, calendar_href=CAL, href=CAL + "keep-me.ics", etag=etag
    )


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


def test_an_event_carrying_unmodelled_structure_is_reported_unwritable():
    reference = _ref(RECURRING)
    assert reference.recurring is True
    assert reference.writable is False
    assert "RRULE" in reference.unsupported


def test_an_event_with_attendees_is_reported_unwritable():
    reference = _ref(WITH_ATTENDEE)
    assert reference.writable is False
    assert set(reference.unsupported) >= {"ORGANIZER", "ATTENDEE"}


@pytest.mark.parametrize(
    "raw",
    [
        RICH.replace(b"UID:keep-me@example\n", b""),
        RICH.replace(b"DTSTAMP:20260817T120000Z\n", b""),
        RICH.replace(b"DTSTART;TZID=America/Sao_Paulo:20260901T110000\n", b""),
        RICH.replace(
            b"DTEND;TZID=America/Sao_Paulo:20260901T120000",
            b"DTEND;TZID=America/Sao_Paulo:20260831T120000",
        ),
    ],
)
def test_malformed_stored_events_are_refused_before_exposure(raw):
    with pytest.raises(events.EventError) as error:
        _ref(raw)
    assert error.value.code == exits.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    "raw",
    [
        RICH.replace(
            b"DTEND;TZID=America/Sao_Paulo:20260901T120000",
            b"DUE;TZID=America/Sao_Paulo:20260901T120000",
        ),
        RICH.replace(
            b"DTEND;TZID=America/Sao_Paulo:20260901T120000",
            b"DTEND;TZID=America/Sao_Paulo:20260901T120000\nDURATION:PT1H",
        ),
    ],
    ids=["VTODO-DUE", "DTEND-and-DURATION"],
)
def test_vevent_rejects_vtodo_due_and_exclusive_end_conflicts(raw):
    with pytest.raises(events.EventError) as error:
        _ref(raw)

    assert error.value.code == exits.MALFORMED_RESPONSE


def test_mixed_date_and_date_time_boundaries_are_malformed():
    raw = RICH.replace(
        b"DTSTART;TZID=America/Sao_Paulo:20260901T110000",
        b"DTSTART;VALUE=DATE:20260901",
    )

    with pytest.raises(events.EventError, match="DATE and DATE-TIME"):
        _ref(raw)


def test_all_day_end_is_exclusive():
    raw = RICH.replace(
        b"DTSTART;TZID=America/Sao_Paulo:20260901T110000",
        b"DTSTART;VALUE=DATE:20260901",
    ).replace(
        b"DTEND;TZID=America/Sao_Paulo:20260901T120000",
        b"DTEND;VALUE=DATE:20260902",
    )

    reference = _ref(raw)

    assert reference.all_day is True
    assert reference.start == "20260901"
    assert reference.end == "20260902"


@pytest.mark.parametrize(
    ("property_name", "value"),
    [
        ("RRULE", "FREQ=WEEKLY"),
        ("RDATE", "20260908T110000Z"),
        ("EXDATE", "20260908T110000Z"),
        ("EXRULE", "FREQ=WEEKLY"),
        ("RECURRENCE-ID", "20260901T110000Z"),
    ],
)
def test_recurrence_structure_is_marked_recurring(property_name, value):
    raw = RICH.replace(
        b"DTSTAMP:20260817T120000Z",
        f"DTSTAMP:20260817T120000Z\n{property_name}:{value}".encode(),
    )

    reference = _ref(raw)

    assert reference.recurring is True
    assert property_name in reference.unsupported


@pytest.mark.parametrize("status", ["CONFIRMED", "TENTATIVE", "CANCELLED"])
def test_event_reference_exposes_url_and_each_status(status):
    raw = RICH.replace(
        b"END:VEVENT",
        f"URL:https://example.invalid/event\nSTATUS:{status}\nEND:VEVENT".encode(),
    )
    reference = _ref(raw)

    assert reference.url == "https://example.invalid/event"
    assert reference.status == status
    assert reference.as_dict()["url"] == "https://example.invalid/event"
    assert reference.as_dict()["status"] == status


def test_event_reference_uses_empty_values_when_url_and_status_are_absent():
    reference = _ref()

    assert reference.url == ""
    assert reference.status == ""
    assert reference.as_dict()["url"] == ""
    assert reference.as_dict()["status"] == ""


def test_cancelled_human_listing_is_marked_without_url_noise(monkeypatch):
    cancelled = _ref(
        RICH.replace(
            b"END:VEVENT",
            b"URL:https://example.invalid/event\nSTATUS:CANCELLED\nEND:VEVENT",
        )
    )
    output: list[str] = []
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: object())
    monkeypatch.setattr(cli, "_resolved_calendar", lambda profile, transport, target: CAL)
    monkeypatch.setattr(cli.events, "query", lambda *args, **kwargs: [cancelled])
    monkeypatch.setattr(cli.render, "emit", lambda value, **kwargs: output.append(value))

    result = cli._run_cal(
        SimpleNamespace(
            cal_command="events",
            profile="home",
            json=False,
            calendar=CAL,
            start="2026-09-01T00:00:00+00:00",
            end="2026-09-02T00:00:00+00:00",
        )
    )

    assert result == exits.OK
    assert "CANCELLED" in output[0]
    assert "https://example.invalid/event" not in "\n".join(output)


@pytest.mark.parametrize(
    ("raw", "expected_url", "expected_status"),
    [
        (
            RICH.replace(
                b"END:VEVENT",
                b"URL:https://example.invalid/event\nSTATUS:CANCELLED\nEND:VEVENT",
            ),
            "https://example.invalid/event",
            "CANCELLED",
        ),
        (RICH, "", ""),
    ],
)
def test_cli_events_and_show_json_expose_url_and_status(
    monkeypatch, capsys, raw, expected_url, expected_status
):
    reference = _ref(raw)
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli, "_resolved_calendar", lambda profile, transport, target: CAL)
    monkeypatch.setattr(cli.session, "Session", lambda profile: object())
    monkeypatch.setattr(cli.events, "query", lambda *args, **kwargs: [reference])
    monkeypatch.setattr(cli.events, "fetch", lambda *args, **kwargs: (reference, raw))

    code = cli.main(
        [
            "cal",
            "events",
            CAL,
            "--from",
            "2026-09-01T00:00:00+00:00",
            "--to",
            "2026-09-02T00:00:00+00:00",
            "--json",
        ]
    )
    listed = json.loads(capsys.readouterr().out)

    assert code == exits.OK
    assert listed["events"][0]["url"] == expected_url
    assert listed["events"][0]["status"] == expected_status

    code = cli.main(["cal", "show", reference.href, "--json"])
    shown = json.loads(capsys.readouterr().out)

    assert code == exits.OK
    assert shown["event"]["url"] == expected_url
    assert shown["event"]["status"] == expected_status


def test_update_alarm_options_are_mutually_exclusive():
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "cal",
                "update",
                CAL + "keep-me.ics",
                "--alarm=-PT15M",
                "--clear-alarms",
            ]
        )


def test_cli_accepts_a_separate_negative_alarm_duration():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "cal",
            "update",
            CAL + "keep-me.ics",
            "--target",
            "resource",
            "--alarm=-PT15M",
        ]
    )
    normalized = cli._normalize_alarm_values(
        [
            "cal",
            "update",
            CAL + "keep-me.ics",
            "--target",
            "resource",
            "--alarm",
            "-PT15M",
        ]
    )
    normalized_args = parser.parse_args(normalized)

    assert args.alarms == ["-PT15M"]
    assert normalized_args.alarms == ["-PT15M"]
    assert normalized[-1] == "--alarm=-PT15M"


def test_calendar_help_explains_projection_and_alarm_modes(capsys):
    with pytest.raises(SystemExit):
        cli.main(["cal", "update", "--help"])
    update_help = " ".join(capsys.readouterr().out.split())

    assert "--portable-description" in update_help
    assert "omission preserves" in update_help
    assert "--clear-alarms" in update_help
    assert "mutually exclusive" in update_help

    with pytest.raises(SystemExit):
        cli.main(["cal", "create", "--help"])
    assert "deterministic DESCRIPTION block" in capsys.readouterr().out


def _description(raw: str | bytes) -> str:
    calendar = icalendar.Calendar.from_ical(raw)
    event = next(item for item in calendar.walk() if item.name == "VEVENT")
    return str(event.get("DESCRIPTION")) if event.get("DESCRIPTION") is not None else ""


def _build_portable(**kwargs) -> str:
    start = dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC)
    end = dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC)
    return mutate.build_event(
        uid="portable@example",
        summary="Appointment",
        start=start,
        end=end,
        portable_description=True,
        **kwargs,
    )


def test_portable_description_projects_url_only():
    description = _description(_build_portable(url="https://example.invalid/event"))

    assert description == (
        "--- ncl portable fields ---\n"
        "URL: https://example.invalid/event\n"
        "--- end ncl portable fields ---"
    )


def test_portable_description_projects_location_only():
    description = _description(_build_portable(location="Room 3"))

    assert description == (
        "--- ncl portable fields ---\n"
        "LOCATION: Room 3\n"
        "--- end ncl portable fields ---"
    )


def test_portable_description_projects_all_fields_in_stable_order():
    description = _description(
        _build_portable(
            location="Room 3",
            url="https://example.invalid/event",
            status="cancelled",
            categories=("WORK", "TRAVEL"),
            priority=1,
            busy=True,
            classification="private",
            alarms=("-PT15M", "-P1D"),
        )
    )
    labels = [
        "LOCATION:",
        "URL:",
        "STATUS:",
        "CATEGORIES:",
        "PRIORITY:",
        "TRANSP:",
        "CLASS:",
        "VALARM TRIGGER:",
        "VALARM TRIGGER:",
    ]

    assert [description.index(label) for label in labels] == sorted(
        description.index(label) for label in labels
    )
    assert "STATUS: CANCELLED" in description
    assert "CATEGORIES: WORK, TRAVEL" in description
    assert "TRANSP: OPAQUE" in description
    assert "CLASS: PRIVATE" in description


def test_portable_description_is_idempotent_and_removes_stale_values():
    original = _build_portable(
        description="Travel instructions.",
        location="Old room",
        url="https://example.invalid/old",
        status="confirmed",
    )
    updated = mutate.patch_event(
        original.encode(),
        {"LOCATION": "New room", "URL": "https://example.invalid/new", "STATUS": None},
        portable_description=True,
        now=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    )
    repeated = mutate.patch_event(
        updated.encode(),
        {},
        portable_description=True,
        now=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    )

    assert _description(updated) == _description(repeated)
    assert _description(updated).startswith("Travel instructions.\n\n")
    assert "Old room" not in _description(updated)
    assert "https://example.invalid/old" not in _description(updated)
    assert "LOCATION: New room" in _description(updated)
    assert "URL: https://example.invalid/new" in _description(updated)
    assert "STATUS:" not in _description(updated)
    assert _description(updated).count(mutate.PORTABLE_START) == 1
    assert _description(updated).count(mutate.PORTABLE_END) == 1


def test_portable_description_preserves_authored_prose_and_unknown_structure():
    raw = RICH.replace(
        b"LOCATION:Somewhere",
        b"LOCATION:Somewhere\nDESCRIPTION:Bring the printed ticket.",
    )
    projected = mutate.patch_event(
        raw,
        {"SUMMARY": "Renamed"},
        portable_description=True,
        now=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    )

    assert "Bring the printed ticket." in _description(projected)
    assert "X-CUSTOM-FIELD:do-not-lose-me" in projected
    assert "VTIMEZONE" in projected
    assert "BEGIN:VALARM" in projected
    assert "TRIGGER:-PT15M" in projected


def test_alarm_omission_preserves_existing_alarms():
    patched = mutate.patch_event(RICH, {"SUMMARY": "Renamed"})

    assert patched.count("BEGIN:VALARM") == 1
    assert "TRIGGER:-PT15M" in patched


def test_alarm_clear_removes_all_existing_alarms():
    raw = mutate.build_event(
        uid="two-alarms@example",
        summary="Two alarms",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        alarms=("-PT15M", "-P1D"),
    ).encode()
    patched = mutate.patch_event(raw, {"VALARM": ()})

    assert "BEGIN:VALARM" not in patched


def test_alarm_replacement_replaces_all_existing_alarms():
    patched = mutate.patch_event(RICH, {"VALARM": ("-P1D", "-PT5M")})

    assert patched.count("BEGIN:VALARM") == 2
    assert "TRIGGER:-PT15M" not in patched
    assert "TRIGGER:-P1D" in patched
    assert "TRIGGER:-PT5M" in patched


class _EventResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        etag: str = '"v1"',
        headers: dict[str, str] | None = None,
        url: str = "",
    ):
        self.body = body
        self.status = status
        self.etag = etag
        self.headers = headers or {}
        self.url = url

    def header(self, name: str) -> str:
        if name == "ETag":
            return self.etag
        return self.headers.get(name, "")


class _EventSession:
    def __init__(self, body: bytes):
        self.body = body

    def request(self, method, url, *, headers=None, data=None, **kwargs):
        assert method == "GET"
        return _EventResponse(self.body)


class _SequenceEventSession:
    def __init__(self, *responses: _EventResponse):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, data=None, **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers, "data": data})
        return self.responses.pop(0)


class _HttpTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, data=None, timeout=None):
        self.requests.append({"method": method, "url": url, "data": data})
        return self.responses.pop(0)


def _http_response(status, *, body=b"", headers=None):
    return http_session.Response(
        status=status,
        headers=headers or {},
        body=body,
        url="",
    )


def test_report_requires_a_multistatus_root():
    session = _SequenceEventSession(_EventResponse(b"<root/>", status=207))

    with pytest.raises(events.EventError) as error:
        events.query(
            PROFILE,
            session=session,
            calendar_href=CAL,
            start=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 9, 2, tzinfo=dt.UTC),
        )

    assert error.value.code == exits.MALFORMED_RESPONSE


def test_empty_valid_report_multistatus_has_no_events():
    body = b'<d:multistatus xmlns:d="DAV:" />'

    assert (
        events.query(
            PROFILE,
            session=_SequenceEventSession(_EventResponse(body, status=207)),
            calendar_href=CAL,
            start=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 9, 2, tzinfo=dt.UTC),
        )
        == []
    )


def test_report_entry_without_successful_calendar_data_is_malformed():
    body = (
        b'<d:multistatus xmlns:d="DAV:">'
        b"<d:response><d:href>/remote.php/dav/calendars/alice/work/one.ics</d:href>"
        b"</d:response></d:multistatus>"
    )

    with pytest.raises(events.EventError) as error:
        events.query(
            PROFILE,
            session=_SequenceEventSession(_EventResponse(body, status=207)),
            calendar_href=CAL,
            start=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 9, 2, tzinfo=dt.UTC),
        )

    assert error.value.code == exits.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    "description",
    [
        "--- ncl portable fields ---\n--- ncl portable fields ---\n--- end ncl portable fields ---",
        "--- end ncl portable fields ---\n--- ncl portable fields ---",
        "--- ncl portable fields ---\nold text",
        "old text\n--- end ncl portable fields ---",
    ],
)
def test_malformed_portable_block_refuses_before_a_plan_is_written(description):
    raw = mutate.build_event(
        uid="malformed@example",
        summary="Malformed",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        description=description,
    ).encode()

    with pytest.raises(events.EventError) as error:
        mutate.plan_update(
            PROFILE,
            session=_EventSession(raw),
            href=CAL + "keep-me.ics",
            target="resource",
            changes={"SUMMARY": "No"},
            portable_description=True,
        )

    assert error.value.code == exits.USAGE
    assert plans.listing() == []


def test_plain_update_does_not_project_a_description_without_opt_in():
    raw = mutate.build_event(
        uid="plain@example",
        summary="Plain",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        description="Authored prose.",
        url="https://example.invalid/old",
    ).encode()

    patched = mutate.patch_event(raw, {"URL": "https://example.invalid/new"})

    assert _description(patched) == "Authored prose."
    assert mutate.PORTABLE_START not in _description(patched)


def test_create_plan_and_apply_verify_url_status_and_portable_description():
    plan = mutate.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Created",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        description="Authored prose.",
        url="https://example.invalid/created",
        status="cancelled",
        portable_description=True,
    )
    stored = plans.payload_bytes(plan.steps[0])
    transport = _SequenceEventSession(
        _EventResponse(b"", status=201),
        _EventResponse(stored, etag='"v2"'),
    )

    result = _apply_bundle(plan, transport)

    assert [request["method"] for request in transport.requests] == ["PUT", "GET"]
    assert result["url"] == "https://example.invalid/created"
    assert result["status"] == "CANCELLED"
    assert result["portable_description_verified"] is True
    assert result["verified"] is True
    with pytest.raises(plans.PlanError):
        plans.read(plan.plan_id)


def test_a_create_answered_412_is_uncertain_and_reconciles_when_the_content_is_its_own(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = mutate.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Created",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
    )
    stored = plans.payload_bytes(plan.steps[0])

    with pytest.raises(events.EventError) as error:
        _apply_bundle(plan, _SequenceEventSession(_EventResponse(b"", status=412)))

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    uncertain = plans.read(plan.plan_id)
    assert uncertain.progress[0].state == "uncertain"

    with plans.claim(plan.plan_id):
        result = plans.reconcile(
            PROFILE,
            session=_SequenceEventSession(_EventResponse(stored, etag='"v2"')),
            plan=uncertain,
            dispatchers=cli._dispatchers(),
        )

    assert result["state"] == "verified"
    with pytest.raises(plans.PlanError):
        plans.read(plan.plan_id)


def test_create_readback_verifies_the_planned_end():
    plan = mutate.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Created",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
    )
    altered = plans.payload_bytes(plan.steps[0]).replace(
        b"DTEND:20260901T120000Z", b"DTEND:20260901T130000Z"
    )
    transport = _SequenceEventSession(
        _EventResponse(b"", status=201),
        _EventResponse(altered, etag='"v2"'),
    )

    with pytest.raises(events.EventError) as error:
        _apply_bundle(plan, transport)

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_update_plan_and_apply_verify_changed_structured_fields_and_projection():
    initial = mutate.build_event(
        uid="update@example",
        summary="Updated",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        description="Authored prose.",
        url="https://example.invalid/old",
        status="confirmed",
    ).encode()
    plan = mutate.plan_update(
        PROFILE,
        session=_EventSession(initial),
        href=CAL + "update.ics",
        target="resource",
        changes={"URL": "https://example.invalid/new", "STATUS": "CANCELLED"},
        portable_description=True,
    )
    stored = plans.payload_bytes(plan.steps[0])
    transport = _SequenceEventSession(
        _EventResponse(b"", status=204),
        _EventResponse(stored, etag='"v2"'),
    )

    result = _apply_bundle(plan, transport)

    assert result["url"] == "https://example.invalid/new"
    assert result["status"] == "CANCELLED"
    assert result["portable_description_verified"] is True
    assert result["verified"] is True


@pytest.mark.parametrize("etag", ["*", 'W/"v1"', "v1"])
def test_update_and_delete_planning_reject_non_strong_etags(etag):
    class TaggedSession(_EventSession):
        def request(self, method, url, *, headers=None, data=None, **kwargs):
            return _EventResponse(RICH, etag=etag)

    with pytest.raises(events.EventError) as update_error:
        mutate.plan_update(
            PROFILE,
            session=TaggedSession(RICH),
            href=CAL + "keep-me.ics",
            target="resource",
            changes={"SUMMARY": "No"},
        )
    assert update_error.value.code == exits.MALFORMED_RESPONSE

    with pytest.raises(events.EventError) as delete_error:
        mutate.plan_delete(
            PROFILE,
            session=TaggedSession(RICH),
            href=CAL + "keep-me.ics",
            target="resource",
        )
    assert delete_error.value.code == exits.MALFORMED_RESPONSE
    assert plans.listing() == []


def test_update_refuses_a_vevent_with_an_extra_vtodo_before_writing_a_plan():
    raw = RICH.replace(
        b"END:VCALENDAR",
        b"BEGIN:VTODO\nUID:task@example\nEND:VTODO\nEND:VCALENDAR",
    )

    with pytest.raises(events.EventError) as error:
        mutate.plan_update(
            PROFILE,
            session=_EventSession(raw),
            href=CAL + "keep-me.ics",
            target="resource",
            changes={"SUMMARY": "No"},
        )

    assert error.value.code == exits.UNSUPPORTED_STRUCTURE
    assert plans.listing() == []


def _without_component(raw: bytes, name: str) -> bytes:
    text = raw.decode("utf-8")
    start = text.index(f"BEGIN:{name}")
    end_marker = f"END:{name}"
    end = text.index(end_marker, start) + len(end_marker)
    return (text[:start] + text[end:]).encode("utf-8")


@pytest.mark.parametrize(
    "alter",
    [
        lambda raw: raw.replace(b"X-CUSTOM-FIELD:do-not-lose-me", b""),
        lambda raw: _without_component(raw, "VALARM"),
        lambda raw: _without_component(raw, "VTIMEZONE"),
        lambda raw: raw.replace(b"20260901T120000", b"20260901T130000"),
    ],
)
def test_calendar_readback_keeps_unknown_nested_and_end_content(alter):
    plan = mutate.plan_update(
        PROFILE,
        session=_EventSession(RICH),
        href=CAL + "keep-me.ics",
        target="resource",
        changes={"SUMMARY": "Renamed"},
    )
    altered = alter(plans.payload_bytes(plan.steps[0]))
    transport = _SequenceEventSession(
        _EventResponse(b"", status=204),
        _EventResponse(altered, etag='"v2"'),
    )

    with pytest.raises(events.EventError) as error:
        _apply_bundle(plan, transport)

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_calendar_readback_compares_semantic_content_not_property_order():
    plan = mutate.plan_update(
        PROFILE,
        session=_EventSession(RICH),
        href=CAL + "keep-me.ics",
        target="resource",
        changes={"SUMMARY": "Renamed"},
    )
    lines = plans.payload_bytes(plan.steps[0]).decode("utf-8").splitlines(keepends=True)
    summary_index = next(index for index, line in enumerate(lines) if line.startswith("SUMMARY:"))
    custom_index = next(
        index for index, line in enumerate(lines) if line.startswith("X-CUSTOM-FIELD:")
    )
    lines[summary_index], lines[custom_index] = lines[custom_index], lines[summary_index]
    transport = _SequenceEventSession(
        _EventResponse(b"", status=204),
        _EventResponse("".join(lines).encode("utf-8"), etag='"v2"'),
    )

    result = _apply_bundle(plan, transport)

    assert result["verified"] is True
    with pytest.raises(plans.PlanError):
        plans.read(plan.plan_id)


def _categorised_event() -> bytes:
    return mutate.build_event(
        uid="categories@example",
        summary="Categories",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        categories=("A", "B"),
    ).encode()


def test_calendar_readback_ignores_category_member_order():
    plan = mutate.plan_update(
        PROFILE,
        session=_EventSession(_categorised_event()),
        href=CAL + "categories.ics",
        target="resource",
        changes={"SUMMARY": "Renamed"},
    )
    stored = plans.payload_bytes(plan.steps[0]).replace(b"CATEGORIES:A,B", b"CATEGORIES:B,A")
    transport = _SequenceEventSession(
        _EventResponse(b"", status=204),
        _EventResponse(stored, etag='"v2"'),
    )

    result = _apply_bundle(plan, transport)

    assert result["verified"] is True
    with pytest.raises(plans.PlanError):
        plans.read(plan.plan_id)


def test_calendar_readback_refuses_a_missing_category_member():
    plan = mutate.plan_update(
        PROFILE,
        session=_EventSession(_categorised_event()),
        href=CAL + "categories.ics",
        target="resource",
        changes={"SUMMARY": "Renamed"},
    )
    stored = plans.payload_bytes(plan.steps[0]).replace(b"CATEGORIES:A,B", b"CATEGORIES:A")
    transport = _SequenceEventSession(
        _EventResponse(b"", status=204),
        _EventResponse(stored, etag='"v2"'),
    )

    with pytest.raises(events.EventError) as error:
        _apply_bundle(plan, transport)

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_calendar_fetch_refuses_redirect_before_following_it(monkeypatch):
    other = CAL + "other.ics"
    transport = _HttpTransport(
        _http_response(307, headers={"Location": other}),
        _http_response(200, body=RICH, headers={"ETag": '"v1"'}),
    )
    monkeypatch.setattr(
        http_session.secrets,
        "get",
        credential_reader("alice", "probe"),
    )

    with pytest.raises(http_session.SessionError) as error:
        events.fetch(
            PROFILE,
            session=http_session.Session(PROFILE, transport=transport),
            href=CAL + "keep-me.ics",
        )

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert [request["method"] for request in transport.requests] == ["GET"]
    assert transport.requests[0]["url"] == CAL + "keep-me.ics"


@pytest.mark.parametrize("action", ["cal.create", "cal.update"])
def test_calendar_put_refuses_redirect_before_following_it(action, monkeypatch):
    if action == "cal.create":
        plan = mutate.plan_create(
            PROFILE,
            calendar_href=CAL,
            summary="Created",
            start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
            end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        )
        success_status = 201
    else:
        plan = mutate.plan_update(
            PROFILE,
            session=_EventSession(RICH),
            href=CAL + "keep-me.ics",
            target="resource",
            changes={"SUMMARY": "Renamed"},
        )
        success_status = 204
    other = CAL + "other.ics"
    transport = _HttpTransport(
        _http_response(307, headers={"Location": other}),
        _http_response(success_status),
        _http_response(
            200, body=plans.payload_bytes(plan.steps[0]), headers={"ETag": '"v2"'}
        ),
    )
    monkeypatch.setattr(
        http_session.secrets,
        "get",
        credential_reader("alice", "probe"),
    )

    with pytest.raises(http_session.SessionError) as error:
        _apply_bundle(plan, http_session.Session(PROFILE, transport=transport))

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert [request["method"] for request in transport.requests] == ["PUT"]
    assert transport.requests[0]["url"] == plan.steps[0].href
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_calendar_readback_redirect_is_outcome_uncertain_without_following_it(monkeypatch):
    plan = mutate.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Created",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
    )
    other = CAL + "other.ics"
    transport = _HttpTransport(
        _http_response(201),
        _http_response(307, headers={"Location": other}),
        _http_response(
            200, body=plans.payload_bytes(plan.steps[0]), headers={"ETag": '"v2"'}
        ),
    )
    monkeypatch.setattr(
        http_session.secrets,
        "get",
        credential_reader("alice", "probe"),
    )

    with pytest.raises(events.EventError) as error:
        _apply_bundle(plan, http_session.Session(PROFILE, transport=transport))

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert [request["method"] for request in transport.requests] == ["PUT", "GET"]
    assert all(request["url"] == plan.steps[0].href for request in transport.requests)
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_calendar_delete_refuses_redirect_without_following_it():
    plan = mutate.plan_delete(
        PROFILE,
        session=_EventSession(RICH),
        href=CAL + "keep-me.ics",
        target="resource",
    )
    transport = _SequenceEventSession(
        _EventResponse(
            b"",
            status=302,
            headers={"Location": CAL + "another.ics"},
        )
    )

    with pytest.raises(events.EventError) as error:
        _apply_bundle(plan, transport)

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert [request["method"] for request in transport.requests] == ["DELETE"]
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_calendar_delete_keeps_plan_when_exact_href_persists():
    plan = mutate.plan_delete(
        PROFILE,
        session=_EventSession(RICH),
        href=CAL + "keep-me.ics",
        target="resource",
    )
    transport = _SequenceEventSession(
        _EventResponse(b"", status=204),
        _EventResponse(RICH),
    )

    with pytest.raises(events.EventError) as error:
        _apply_bundle(plan, transport)

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert [request["method"] for request in transport.requests] == ["DELETE", "GET"]
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_calendar_delete_consumes_only_after_exact_href_returns_404():
    plan = mutate.plan_delete(
        PROFILE,
        session=_EventSession(RICH),
        href=CAL + "keep-me.ics",
        target="resource",
    )
    transport = _SequenceEventSession(
        _EventResponse(b"", status=204),
        _EventResponse(b"", status=404),
    )

    result = _apply_bundle(plan, transport)

    assert result["verified"] == "deleted"
    with pytest.raises(plans.PlanError) as error:
        plans.read(plan.plan_id)
    assert error.value.code == exits.TARGET_NOT_FOUND


def test_all_day_conversion_requires_both_boundaries_and_preserves_exclusive_end():
    raw = RICH.replace(
        b"DTSTART;TZID=America/Sao_Paulo:20260901T110000",
        b"DTSTART;VALUE=DATE:20260901",
    ).replace(
        b"DTEND;TZID=America/Sao_Paulo:20260901T120000",
        b"DTEND;VALUE=DATE:20260902",
    )

    with pytest.raises(events.EventError) as single_boundary:
        mutate.patch_event(
            raw,
            {"DTSTART": dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC)},
        )
    assert single_boundary.value.code == exits.USAGE

    converted = mutate.patch_event(
        raw,
        {
            "DTSTART": dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
            "DTEND": dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        },
    )
    assert _ref(converted.encode()).all_day is False

    with pytest.raises(events.EventError) as invalid_end:
        mutate.patch_event(raw, {"DTEND": dt.date(2026, 9, 1)})
    assert invalid_end.value.code == exits.USAGE

    with pytest.raises(events.EventError) as mixed_boundaries:
        mutate.patch_event(
            raw,
            {
                "DTSTART": dt.date(2026, 9, 1),
                "DTEND": dt.datetime(2026, 9, 2, tzinfo=dt.UTC),
            },
        )
    assert mixed_boundaries.value.code == exits.USAGE


def _duration_event(*, all_day: bool = False) -> bytes:
    raw = RICH.replace(
        b"DTEND;TZID=America/Sao_Paulo:20260901T120000", b"DURATION:PT1H"
    )
    if all_day:
        raw = raw.replace(
            b"DTSTART;TZID=America/Sao_Paulo:20260901T110000",
            b"DTSTART;VALUE=DATE:20260901",
        ).replace(b"DURATION:PT1H", b"DURATION:P1D")
    return raw


def _event_component(raw: str | bytes):
    calendar = icalendar.Calendar.from_ical(raw)
    return next(item for item in calendar.walk() if item.name == "VEVENT")


def test_duration_is_preserved_when_only_start_changes():
    patched = mutate.patch_event(
        _duration_event(),
        {"DTSTART": dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC)},
    )
    event = _event_component(patched)

    assert event.get("DURATION").dt == dt.timedelta(hours=1)
    assert "DTEND" not in event
    assert _ref(patched).end == "20260901T130000Z"


@pytest.mark.parametrize("to_all_day", [True, False])
def test_explicit_end_replaces_duration_during_boundary_conversion(to_all_day):
    if to_all_day:
        raw = _duration_event()
        changes = {
            "DTSTART": dt.date(2026, 9, 1),
            "DTEND": dt.date(2026, 9, 2),
        }
    else:
        raw = _duration_event(all_day=True)
        changes = {
            "DTSTART": dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
            "DTEND": dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        }

    patched = mutate.patch_event(raw, changes)
    event = _event_component(patched)

    assert "DURATION" not in event
    assert "DTEND" in event
    assert _ref(patched).all_day is to_all_day


def test_cli_create_and_apply_expose_plan_and_readback_verification(
    monkeypatch, capsys
):
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli, "_resolved_calendar", lambda profile, transport, target: CAL)
    monkeypatch.setattr(cli.session, "Session", lambda profile: _SequenceEventSession())

    code = cli.main(
        [
            "cal",
            "create",
            CAL,
            "--summary",
            "CLI event",
            "--from",
            "2026-09-01T11:00:00+00:00",
            "--to",
            "2026-09-01T12:00:00+00:00",
            "--url",
            "https://example.invalid/cli",
            "--status",
            "cancelled",
            "--portable-description",
            "--json",
        ]
    )
    planned = json.loads(capsys.readouterr().out)
    plan = plans.read(planned["plan"]["plan_id"])

    assert code == exits.CONFIRMATION_REQUIRED
    assert planned["plan"]["steps"][0]["action"] == "cal.create"
    assert "URL: https://example.invalid/cli" in _description(
        plans.payload_bytes(plan.steps[0])
    )
    assert "STATUS: CANCELLED" in _description(plans.payload_bytes(plan.steps[0]))

    stored = plans.payload_bytes(plan.steps[0])
    monkeypatch.setattr(
        cli.session,
        "Session",
        lambda profile: _SequenceEventSession(
            _EventResponse(b"", status=201), _EventResponse(stored, etag='"v2"')
        ),
    )
    code = cli.main(["apply", plan.plan_id, "--json"])
    applied = json.loads(capsys.readouterr().out)

    assert code == exits.OK
    assert applied["url"] == "https://example.invalid/cli"
    assert applied["status"] == "CANCELLED"
    assert applied["portable_description_verified"] is True


def test_an_alarm_does_not_block_editing_because_a_patch_preserves_it():
    """An alarm rides along with its event rather than blocking edits.

    Refusing here would decline to edit most events a calendar client creates.
    The alarm survives because a patch re-serializes the whole VEVENT, which is
    asserted rather than assumed.
    """
    assert _ref().writable is True
    patched = mutate.patch_event(RICH, {"SUMMARY": "Renamed"})
    assert "BEGIN:VALARM" in patched
    assert "TRIGGER:-PT15M" in patched
    assert "Renamed" in patched


def test_patch_preserves_everything_it_was_not_asked_to_change():
    """The worst defect this tool could ship is a silent loss at exit 0."""
    patched = mutate.patch_event(RICH, {"SUMMARY": "Renamed"})
    assert "Renamed" in patched
    for survivor in ("X-CUSTOM-FIELD:do-not-lose-me", "CATEGORIES:WORK", "VTIMEZONE",
                     "America/Sao_Paulo", "LOCATION:Somewhere"):
        assert survivor in patched, f"{survivor} was lost by an unrelated edit"


def test_patch_bumps_sequence_so_clients_see_a_newer_revision():
    patched = mutate.patch_event(RICH, {"SUMMARY": "Renamed"})
    assert "SEQUENCE:4" in patched


def test_patch_refuses_a_recurring_series():
    with pytest.raises(events.EventError) as error:
        mutate.patch_event(RECURRING, {"SUMMARY": "No"})
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_patch_refuses_an_event_with_attendees_because_editing_sends_mail():
    with pytest.raises(events.EventError) as error:
        mutate.patch_event(WITH_ATTENDEE, {"SUMMARY": "No"})
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_building_an_event_requires_an_unambiguous_instant():
    naive = dt.datetime(2026, 9, 1, 11, 0)
    with pytest.raises(events.EventError) as error:
        mutate.build_event(uid="u", summary="s", start=naive, end=naive)
    assert error.value.code == exits.USAGE


def test_building_an_event_refuses_a_backwards_interval():
    start = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)
    end = dt.datetime(2026, 9, 1, 11, 0, tzinfo=dt.UTC)
    with pytest.raises(events.EventError) as error:
        mutate.build_event(uid="u", summary="s", start=start, end=end)
    assert error.value.code == exits.USAGE


def test_build_event_serializes_typed_related_to_with_a_reltype_parameter():
    raw = mutate.build_event(
        uid="anchor@example",
        summary="Anchor",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        related_to=(("child@example", "child"),),
        now=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    )

    assert "RELATED-TO;RELTYPE=CHILD:child@example" in raw
    assert "RELATED-TO:('child'\\, 'CHILD')" not in raw


def test_planning_outside_the_allowlist_is_refused():
    start = dt.datetime(2026, 9, 1, 11, 0, tzinfo=dt.UTC)
    end = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)
    with pytest.raises(CalendarError) as error:
        mutate.plan_create(
            PROFILE,
            calendar_href="https://cloud.example.invalid/remote.php/dav/calendars/alice/other/",
            summary="s",
            start=start,
            end=end,
        )
    assert error.value.code == exits.SCOPE_DENIED


def test_a_query_window_must_move_forwards():
    moment = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    with pytest.raises(events.EventError) as error:
        events.query(PROFILE, session=None, calendar_href=CAL, start=moment, end=moment)
    assert error.value.code == exits.USAGE


def test_an_expired_plan_is_stale_rather_than_applied(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = _write(
        profile="home",
        action="cal.create",
        href=CAL + "x.ics",
        etag="",
        summary="s",
        payload=b"BEGIN:VCALENDAR\nEND:VCALENDAR\n",
        content_type="text/calendar; charset=utf-8",
        details={"uid": "x", "start": "20260901T110000Z", "end": "20260901T120000Z"},
        ttl=1,
        now=1000.0,
    )
    with pytest.raises(plans.PlanError) as error:
        plans.check_fresh(plan, now=2000.0)
    assert error.value.code == exits.PLAN_STALE


def test_a_plan_round_trips_and_is_consumed_once(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = _write(
        profile="home",
        action="cal.delete",
        href=CAL + "x.ics",
        etag='"v1"',
        summary="s",
        details={"uid": "x", "start": "", "end": ""},
    )
    assert plans.read(plan.plan_id).steps[0].etag == '"v1"'
    assert [item.plan_id for item in plans.listing()] == [plan.plan_id]
    plans.consume(plan.plan_id)
    assert plans.listing() == []
    with pytest.raises(plans.PlanError) as error:
        plans.read(plan.plan_id)
    assert error.value.code == exits.TARGET_NOT_FOUND


def test_a_plan_payload_is_not_echoed_in_its_summary_view(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = _write(
        profile="home",
        action="cal.create",
        href=CAL + "x.ics",
        etag="",
        summary="s",
        payload=b"BEGIN:VCALENDAR\nX-SECRET:zzz\nEND:VCALENDAR\n",
        content_type="text/calendar; charset=utf-8",
        details={"uid": "x", "start": "", "end": ""},
    )
    assert "payload" not in plan.as_dict()
    assert plan.as_dict()["steps"][0]["payload_bytes"] > 0


def test_a_plan_round_trips_arbitrary_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = _write(
        profile="home",
        action="files.write",
        href="https://cloud.example.invalid/remote.php/dav/files/alice/blob",
        etag="",
        summary="blob",
        payload=b"\x00\xff\n",
        content_type="application/octet-stream",
    )

    assert plans.payload_bytes(plans.read(plan.plan_id).steps[0]) == b"\x00\xff\n"


def test_cli_apply_claims_before_loading_and_dispatching_a_plan(monkeypatch):
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
        or SimpleNamespace(plan_id=plan_id, profile="home", action="cal.create"),
    )
    monkeypatch.setattr(cli.session, "Session", lambda profile: object())
    monkeypatch.setattr(plans, "apply", lambda *args, **kwargs: order.append("apply") or {})

    assert cli._run_apply(SimpleNamespace(plan_id="stale", json=True)) == exits.OK
    assert order == ["claim", "read", "apply"]


@pytest.mark.parametrize("plan_id", ["../escape", "nested/id"])
def test_cli_apply_rejects_invalid_plan_id_before_creating_a_lock(
    monkeypatch, capsys, plan_id
):
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    opened: list[str] = []
    original_open = plans.os.open
    monkeypatch.setattr(
        plans.os,
        "open",
        lambda path, *args: (opened.append(str(path)) or original_open(path, *args)),
    )

    code = cli.main(["apply", plan_id, "--json"])
    output = json.loads(capsys.readouterr().out)

    assert code == exits.USAGE
    assert output["code"] == exits.USAGE
    assert opened == []


def test_plan_claim_unlinks_the_lock_before_closing_its_descriptor(monkeypatch):
    lock_path = plans._directory() / "valid.lock"
    closed_while_named: list[bool] = []
    original_close = plans.os.close

    def close(descriptor):
        closed_while_named.append(lock_path.exists())
        original_close(descriptor)

    monkeypatch.setattr(plans.os, "close", close)
    with plans.claim("valid"):
        assert lock_path.exists()

    assert closed_while_named == [False]


def test_cli_apply_reports_locked_and_missing_plans_after_claiming(monkeypatch):
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    plan = _write(
        profile="home",
        action="cal.delete",
        href=CAL + "keep-me.ics",
        etag='"v1"',
        summary="Original",
    )

    with plans.claim(plan.plan_id), pytest.raises(plans.PlanError) as locked:
        cli._run_apply(SimpleNamespace(plan_id=plan.plan_id, json=True))
    assert locked.value.code == exits.LOCKED

    plans.consume(plan.plan_id)
    with pytest.raises(plans.PlanError) as missing:
        cli._run_apply(SimpleNamespace(plan_id=plan.plan_id, json=True))
    assert missing.value.code == exits.TARGET_NOT_FOUND


def test_cli_apply_rechecks_a_plan_consumed_while_claiming(monkeypatch):
    plan = _write(
        profile="home",
        action="cal.delete",
        href=CAL + "keep-me.ics",
        etag='"v1"',
        summary="Original",
    )
    dispatched: list[bool] = []

    class Claim:
        def __enter__(self):
            plans.consume(plan.plan_id)

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(plans, "claim", lambda plan_id: Claim())
    monkeypatch.setattr(
        mutate,
        "apply",
        lambda *args, **kwargs: dispatched.append(True),
    )

    with pytest.raises(plans.PlanError) as error:
        cli._run_apply(SimpleNamespace(plan_id=plan.plan_id, json=True))

    assert error.value.code == exits.TARGET_NOT_FOUND
    assert dispatched == []


def test_plan_cancel_json_is_structured(capsys):
    plan = _write(
        profile="home",
        action="cal.delete",
        href=CAL + "keep-me.ics",
        etag='"v1"',
        summary="Original",
    )

    assert cli.main(["plan", "cancel", plan.plan_id, "--json"]) == exits.OK
    assert json.loads(capsys.readouterr().out) == {
        "cancelled": plan.plan_id,
        "remote_effects_undone": False,
        "warning": "Verified remote effects were not undone.",
    }


@pytest.mark.parametrize("priority", [0, 10])
def test_update_priority_range_is_validated_before_a_plan_is_written(
    monkeypatch, capsys, priority
):
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    transport = _SequenceEventSession()
    monkeypatch.setattr(cli.session, "Session", lambda profile: transport)

    code = cli.main(
        [
            "cal",
            "update",
            CAL + "keep-me.ics",
            "--target",
            "resource",
            "--priority",
            str(priority),
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == exits.USAGE
    assert output["code"] == exits.USAGE
    assert transport.requests == []
    assert plans.listing() == []


def test_creating_an_all_day_event_serializes_exclusive_date_boundaries():
    plan = mutate.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Anniversary",
        start=dt.date(2026, 12, 30),
        end=dt.date(2026, 12, 31),
        location="Somewhere",
        url="https://example.invalid/anniversary",
    )
    payload = plans.payload_bytes(plan.steps[0])

    assert b"DTSTART;VALUE=DATE:20261230" in payload
    assert b"DTEND;VALUE=DATE:20261231" in payload
    assert b"T00:00" not in payload and b"TZID" not in payload
    assert plan.steps[0].details["all_day"] is True
    assert plan.steps[0].details["start"] == "20261230"
    assert plan.steps[0].details["end"] == "20261231"
    assert b"LOCATION:Somewhere" in payload
    assert b"URL:https://example.invalid/anniversary" in payload


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (dt.date(2026, 12, 30), dt.datetime(2026, 12, 31, 12, tzinfo=dt.UTC)),
        (dt.datetime(2026, 12, 30, 12, tzinfo=dt.UTC), dt.date(2026, 12, 31)),
    ],
)
def test_creating_an_event_refuses_mixed_date_and_instant_boundaries(start, end):
    with pytest.raises(events.EventError) as error:
        mutate.build_event(uid="mixed@example", summary="Mixed", start=start, end=end)

    assert error.value.code == exits.USAGE
    assert "all-day on both boundaries" in str(error.value)


@pytest.mark.parametrize("end", [dt.date(2026, 12, 30), dt.date(2026, 12, 29)])
def test_creating_an_all_day_event_requires_an_exclusive_end_after_the_start(end):
    with pytest.raises(events.EventError) as error:
        mutate.build_event(
            uid="backwards@example", summary="Backwards", start=dt.date(2026, 12, 30), end=end
        )

    assert error.value.code == exits.USAGE
    assert "exclusive" in str(error.value)


def test_all_day_create_readback_verifies_the_stored_boundary_kind():
    plan = mutate.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Anniversary",
        start=dt.date(2026, 12, 30),
        end=dt.date(2026, 12, 31),
    )
    stored = plans.payload_bytes(plan.steps[0])
    transport = _SequenceEventSession(
        _EventResponse(b"", status=201),
        _EventResponse(stored, etag='"v2"'),
    )

    result = _apply_bundle(plan, transport)

    assert result["all_day"] is True
    assert result["start"] == "20261230"
    assert result["end"] == "20261231"
    assert result["verified"] is True


@pytest.mark.parametrize(
    "alter",
    [
        # A server that widened the event by a day, keeping the DATE kind.
        lambda raw: raw.replace(b"DTEND;VALUE=DATE:20261231", b"DTEND;VALUE=DATE:20270101"),
        # A server that resolved the dates to a local midnight of its own.
        lambda raw: raw.replace(
            b"DTSTART;VALUE=DATE:20261230", b"DTSTART:20261230T000000Z"
        ).replace(b"DTEND;VALUE=DATE:20261231", b"DTEND:20261231T000000Z"),
    ],
)
def test_all_day_create_refuses_a_readback_that_changed_the_dates(alter):
    plan = mutate.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Anniversary",
        start=dt.date(2026, 12, 30),
        end=dt.date(2026, 12, 31),
    )
    transport = _SequenceEventSession(
        _EventResponse(b"", status=201),
        _EventResponse(alter(plans.payload_bytes(plan.steps[0])), etag='"v2"'),
    )

    with pytest.raises(events.EventError) as error:
        _apply_bundle(plan, transport)

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_cli_creates_an_all_day_event_and_states_it_without_a_timezone(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli, "_resolved_calendar", lambda profile, transport, target: CAL)
    monkeypatch.setattr(cli.session, "Session", lambda profile: _SequenceEventSession())

    code = cli.main(
        ["cal", "create", CAL, "--summary", "Anniversary", "--from", "2026-12-30",
         "--to", "2026-12-31"]
    )
    preview = capsys.readouterr().out

    assert code == exits.CONFIRMATION_REQUIRED
    assert "all-day 20261230 .. 20261231 (end date is exclusive)" in preview
    assert "00:00" not in preview


def test_cli_create_refuses_a_mixed_boundary_pair_before_writing_a_plan(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli, "_resolved_calendar", lambda profile, transport, target: CAL)
    monkeypatch.setattr(cli.session, "Session", lambda profile: _SequenceEventSession())
    before = plans.listing()

    code = cli.main(
        ["cal", "create", CAL, "--summary", "Mixed", "--from", "2026-12-30",
         "--to", "2026-12-31T12:00:00+00:00"]
    )

    assert code == exits.USAGE
    assert "all-day on both boundaries" in capsys.readouterr().err
    assert plans.listing() == before


def test_human_event_listing_marks_an_all_day_event(monkeypatch):
    all_day = _ref(
        RICH.replace(
            b"DTSTART;TZID=America/Sao_Paulo:20260901T110000", b"DTSTART;VALUE=DATE:20260901"
        ).replace(b"DTEND;TZID=America/Sao_Paulo:20260901T120000", b"DTEND;VALUE=DATE:20260902")
    )
    output: list[str] = []
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: object())
    monkeypatch.setattr(cli, "_resolved_calendar", lambda profile, transport, target: CAL)
    monkeypatch.setattr(cli.events, "query", lambda *args, **kwargs: [all_day, _ref()])
    monkeypatch.setattr(cli.render, "emit", lambda value, **kwargs: output.append(value))

    result = cli._run_cal(
        SimpleNamespace(
            cal_command="events",
            profile="home",
            json=False,
            calendar=CAL,
            start="2026-09-01T00:00:00+00:00",
            end="2026-09-03T00:00:00+00:00",
        )
    )

    assert result == exits.OK
    assert "[all-day]" in output[0]
    assert "[all-day]" not in output[2]


def test_create_and_update_help_describe_the_same_all_day_contract(capsys):
    parser = cli.build_parser()
    texts = []
    for command in ("create", "update"):
        with pytest.raises(SystemExit):
            parser.parse_args(["cal", command, "--help"])
        texts.append(capsys.readouterr().out)

    for text in texts:
        assert "YYYY-MM-DD" in text
        assert "all-day" in text
        # The exclusive end is the half of the contract a caller gets wrong.
        assert "exclusive YYYY-MM-DD end" in text


def test_cli_create_refuses_an_impossible_calendar_date(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli, "_resolved_calendar", lambda profile, transport, target: CAL)
    monkeypatch.setattr(cli.session, "Session", lambda profile: _SequenceEventSession())
    before = plans.listing()

    code = cli.main(
        ["cal", "create", CAL, "--summary", "Impossible", "--from", "2026-02-30",
         "--to", "2026-03-01"]
    )

    assert code == exits.USAGE
    assert "not a valid all-day date" in capsys.readouterr().err
    assert plans.listing() == before


@pytest.mark.parametrize(
    "body",
    [b"not iCalendar at all", b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"],
)
def test_all_day_create_readback_that_is_unreadable_is_an_uncertain_outcome(body):
    plan = mutate.plan_create(
        PROFILE,
        calendar_href=CAL,
        summary="Anniversary",
        start=dt.date(2026, 12, 30),
        end=dt.date(2026, 12, 31),
    )
    transport = _SequenceEventSession(
        _EventResponse(b"", status=201),
        _EventResponse(body, etag='"v2"'),
    )

    with pytest.raises(events.EventError) as error:
        _apply_bundle(plan, transport)

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_converting_an_event_to_all_day_preserves_everything_it_did_not_touch():
    """A boundary conversion must not cost the caller the rest of the resource."""
    payload = mutate.patch_event(
        RICH,
        {"DTSTART": dt.date(2026, 9, 1), "DTEND": dt.date(2026, 9, 2)},
    ).encode()

    assert b"DTSTART;VALUE=DATE:20260901" in payload
    assert b"DTEND;VALUE=DATE:20260902" in payload
    for retained in (
        b"BEGIN:VTIMEZONE",
        b"TZID:America/Sao_Paulo",
        b"BEGIN:VALARM",
        b"TRIGGER:-PT15M",
        b"X-CUSTOM-FIELD:do-not-lose-me",
        b"CATEGORIES:WORK",
        b"LOCATION:Somewhere",
        b"SUMMARY:Original",
        b"UID:keep-me@example",
    ):
        assert retained in payload


LONG_LOCATION = (
    "Sala 12, Edifício Central, Avenida das Nações Unidas 1000, "
    "São Paulo — the whole address, past the seventy-five octet fold width"
)
AWKWARD_DESCRIPTION = "first line; with a semicolon\nsecond, with a comma\nthird"


def _content_ref(**fields):
    """Serialize an event the way `cal create` does, then read it back."""
    payload = mutate.build_event(
        uid="content@example",
        summary="Content",
        start=dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC),
        **fields,
    )
    return _ref(payload.encode("utf-8"))


def test_folded_and_escaped_content_fields_round_trip():
    reference = _content_ref(
        location=LONG_LOCATION,
        description=AWKWARD_DESCRIPTION,
        categories=("work", "travel"),
        priority=3,
        classification="private",
        busy=False,
        color="cornflowerblue",
        alarms=("-PT15M", "-P1D"),
    )

    assert reference.location == LONG_LOCATION
    assert reference.description == AWKWARD_DESCRIPTION
    assert reference.categories == ("work", "travel")
    assert reference.priority == 3
    assert reference.classification == "PRIVATE"
    assert reference.transp == "TRANSPARENT"
    assert reference.color == "cornflowerblue"
    assert reference.alarms == ("-PT15M", "-P1D")


def test_absent_content_fields_report_absence_rather_than_a_default():
    reference = _content_ref()

    assert reference.location == ""
    assert reference.description == ""
    assert reference.categories == ()
    assert reference.priority is None
    assert reference.classification == ""
    assert reference.transp == ""
    assert reference.alarms == ()


def test_repeated_categories_flatten_to_one_tag_list():
    raw = RICH.replace(b"CATEGORIES:WORK", b"CATEGORIES:WORK,ERRAND\r\nCATEGORIES:TRAVEL")

    assert _ref(raw).categories == ("WORK", "ERRAND", "TRAVEL")


def test_an_absolute_alarm_trigger_is_reported_as_an_instant():
    raw = RICH.replace(
        b"TRIGGER:-PT15M", b"TRIGGER;VALUE=DATE-TIME:20260901T090000Z"
    )

    assert _ref(raw).alarms == ("20260901T090000Z",)


def test_a_relative_trigger_is_reusable_and_an_absolute_one_is_read_only():
    """The documented alarm contract, checked end to end against the writer."""
    relative = _ref().alarms
    assert relative == ("-PT15M",)
    # A relative trigger read back is a value `--alarm` accepts.
    assert b"TRIGGER:-PT15M" in mutate.build_event(
        uid="alarm@example",
        summary="Reminder",
        start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        alarms=relative,
    ).encode()

    absolute = _ref(
        RICH.replace(b"TRIGGER:-PT15M", b"TRIGGER;VALUE=DATE-TIME:20260901T090000Z")
    ).alarms
    assert absolute == ("20260901T090000Z",)
    with pytest.raises(events.EventError) as refused:
        mutate.build_event(
            uid="alarm@example",
            summary="Reminder",
            start=dt.datetime(2026, 9, 1, 11, tzinfo=dt.UTC),
            end=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
            alarms=absolute,
        )
    assert refused.value.code == exits.USAGE
    assert "duration" in refused.value.message


def test_cal_show_json_carries_the_content_fields(monkeypatch, capsys):
    payload = mutate.build_event(
        uid="content@example",
        summary="Content",
        start=dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC),
        location=LONG_LOCATION,
        description=AWKWARD_DESCRIPTION,
        categories=("work",),
        alarms=("-PT15M",),
    ).encode("utf-8")
    reference = _ref(payload)
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: object())
    monkeypatch.setattr(cli.events, "fetch", lambda *args, **kwargs: (reference, payload))

    code = cli.main(["cal", "show", reference.href, "--json"])
    shown = json.loads(capsys.readouterr().out)["event"]

    assert code == exits.OK
    assert shown["location"] == LONG_LOCATION
    assert shown["description"] == AWKWARD_DESCRIPTION
    assert shown["categories"] == ["work"]
    assert shown["alarms"] == ["-PT15M"]


def test_a_non_numeric_priority_is_refused_rather_than_read_as_absent():
    raw = RICH.replace(b"CATEGORIES:WORK", b"PRIORITY:soon")

    with pytest.raises(events.EventError):
        _ref(raw)


def test_a_valarm_without_a_trigger_is_refused():
    raw = RICH.replace(b"TRIGGER:-PT15M\n", b"")

    with pytest.raises(events.EventError):
        _ref(raw)
