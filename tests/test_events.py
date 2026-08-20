from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import icalendar
import pytest

from ncl import cli, events, exits, mutate, plans
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


def test_an_event_carrying_unmodelled_structure_is_reported_unwritable():
    reference = _ref(RECURRING)
    assert reference.recurring is True
    assert reference.writable is False
    assert "RRULE" in reference.unsupported


def test_an_event_with_attendees_is_reported_unwritable():
    reference = _ref(WITH_ATTENDEE)
    assert reference.writable is False
    assert set(reference.unsupported) >= {"ORGANIZER", "ATTENDEE"}


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
    def __init__(self, body: bytes, *, status: int = 200, etag: str = '"v1"'):
        self.body = body
        self.status = status
        self.etag = etag

    def header(self, name: str) -> str:
        return self.etag if name == "ETag" else ""


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
    stored = plans.payload_bytes(plan)
    transport = _SequenceEventSession(
        _EventResponse(b"", status=201),
        _EventResponse(stored, etag='"v2"'),
    )

    result = mutate.apply(PROFILE, session=transport, plan=plan)

    assert [request["method"] for request in transport.requests] == ["PUT", "GET"]
    assert result["url"] == "https://example.invalid/created"
    assert result["status"] == "CANCELLED"
    assert result["portable_description_verified"] is True
    assert result["verified"] is True
    with pytest.raises(plans.PlanError):
        plans.read(plan.plan_id)


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
        changes={"URL": "https://example.invalid/new", "STATUS": "CANCELLED"},
        portable_description=True,
    )
    stored = plans.payload_bytes(plan)
    transport = _SequenceEventSession(
        _EventResponse(b"", status=204),
        _EventResponse(stored, etag='"v2"'),
    )

    result = mutate.apply(PROFILE, session=transport, plan=plan)

    assert result["url"] == "https://example.invalid/new"
    assert result["status"] == "CANCELLED"
    assert result["portable_description_verified"] is True
    assert result["verified"] is True


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
    assert planned["plan"]["action"] == "cal.create"
    assert "URL: https://example.invalid/cli" in _description(plans.payload_bytes(plan))
    assert "STATUS: CANCELLED" in _description(plans.payload_bytes(plan))

    stored = plans.payload_bytes(plan)
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
    plan = plans.write(
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
    plan = plans.write(
        profile="home",
        action="cal.delete",
        href=CAL + "x.ics",
        etag='"v1"',
        summary="s",
        details={"uid": "x", "start": "", "end": ""},
    )
    assert plans.read(plan.plan_id).etag == '"v1"'
    assert [item.plan_id for item in plans.listing()] == [plan.plan_id]
    plans.consume(plan.plan_id)
    assert plans.listing() == []
    with pytest.raises(plans.PlanError) as error:
        plans.read(plan.plan_id)
    assert error.value.code == exits.TARGET_NOT_FOUND


def test_a_plan_payload_is_not_echoed_in_its_summary_view(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = plans.write(
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
    assert plan.as_dict()["payload_bytes"] > 0


def test_a_plan_round_trips_arbitrary_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = plans.write(
        profile="home",
        action="files.write",
        href="https://cloud.example.invalid/remote.php/dav/files/alice/blob",
        etag="",
        summary="blob",
        payload=b"\x00\xff\n",
        content_type="application/octet-stream",
    )

    assert plans.payload_bytes(plans.read(plan.plan_id)) == b"\x00\xff\n"
