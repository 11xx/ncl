from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

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


def test_update_alarm_options_are_mutually_exclusive():
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["cal", "update", CAL + "keep-me.ics", "--alarm", "-PT15M", "--clear-alarms"]
        )


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
