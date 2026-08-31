"""Free/busy over the scheduling outbox, entirely offline.

The request is a read: it stores nothing and notifies nobody. What it must
never do is turn an unanswered recipient into an empty schedule, which is the
one mistake here that books a meeting on top of a real commitment.
"""

from __future__ import annotations

import datetime as dt

import pytest
from test_auth import FakeTransport, home_response, principal_response, response
from test_config import VALID
from test_scheduling import (
    PRINCIPAL,
    options_response,
    scheduling_response,
)

from ncl import cli, exits, freebusy, scheduling, secrets, session
from ncl.config import Profile

PROFILE = Profile(
    name="home",
    origin="https://cloud.example.invalid",
    secret_backend="pass",
    calendars=("/remote.php/dav/calendars/alice/",),
    files_roots=(),
)
OUTBOX = "https://cloud.example.invalid/remote.php/dav/calendars/alice/outbox/"
ALICE = "mailto:alice@example.invalid"
BOB = "mailto:bob@example.invalid"

IDENTITY = scheduling.SchedulingIdentity(
    principal_url=PRINCIPAL,
    addresses=(ALICE,),
    schedule_inbox="https://cloud.example.invalid/remote.php/dav/calendars/alice/inbox/",
    schedule_outbox=OUTBOX,
)

WINDOW = (
    dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    dt.datetime(2026, 9, 2, tzinfo=dt.UTC),
)


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setattr(
        secrets, "load_credential", lambda profile: secrets.Credential("alice", "app-password")
    )


def schedule_response(*entries: str) -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<cal:schedule-response xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
        + "".join(entries)
        + "</cal:schedule-response>"
    ).encode()


def answer(
    recipient: str = BOB,
    status: str = "2.0;Success",
    data: str | None = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//x//EN\nBEGIN:VFREEBUSY\n"
    "UID:a@b\nDTSTAMP:20260901T000000Z\nDTSTART:20260901T000000Z\nDTEND:20260902T000000Z\n"
    "FREEBUSY:20260901T090000Z/20260901T100000Z\nEND:VFREEBUSY\nEND:VCALENDAR",
) -> str:
    body = f"<cal:calendar-data>{data}</cal:calendar-data>" if data is not None else ""
    return (
        "<cal:response>"
        f"<cal:recipient><d:href>{recipient}</d:href></cal:recipient>"
        f"<cal:request-status>{status}</cal:request-status>"
        f"{body}</cal:response>"
    )


def transport_for(*responses):
    return session.Session(PROFILE, transport=FakeTransport(list(responses)))


def test_a_request_names_the_organizer_and_every_recipient_in_the_body():
    """RFC 6638 derives the originator and recipients from the body, not from headers."""
    fake = FakeTransport([response(200, schedule_response(answer()))])
    transport = session.Session(PROFILE, transport=fake)

    freebusy.query(
        PROFILE,
        session=transport,
        scheduling=IDENTITY,
        start=WINDOW[0],
        end=WINDOW[1],
        attendees=(BOB,),
        now=dt.datetime(2026, 8, 30, 12, tzinfo=dt.UTC),
    )

    sent = fake.requests[0]
    assert sent["method"] == "POST"
    assert sent["url"] == OUTBOX
    assert sent["headers"]["Content-Type"] == "text/calendar; charset=utf-8"
    body = sent["data"].decode()
    assert "METHOD:REQUEST" in body
    assert "BEGIN:VFREEBUSY" in body
    assert f"ORGANIZER:{ALICE}" in body
    assert f"ATTENDEE:{BOB}" in body
    assert "DTSTART:20260901T000000Z" in body
    assert "DTEND:20260902T000000Z" in body


def test_with_no_attendee_the_question_is_about_this_account():
    fake = FakeTransport([response(200, schedule_response(answer(recipient=ALICE)))])

    found = freebusy.query(
        PROFILE,
        session=session.Session(PROFILE, transport=fake),
        scheduling=IDENTITY,
        start=WINDOW[0],
        end=WINDOW[1],
    )

    assert f"ATTENDEE:{ALICE}" in fake.requests[0]["data"].decode()
    assert [item.recipient for item in found] == [ALICE]


def test_busy_periods_are_reported_in_utc_with_their_transparency():
    found = freebusy.query(
        PROFILE,
        session=transport_for(response(200, schedule_response(answer()))),
        scheduling=IDENTITY,
        start=WINDOW[0],
        end=WINDOW[1],
        attendees=(BOB,),
    )

    assert len(found) == 1
    assert found[0].answered is True
    assert [period.as_dict() for period in found[0].periods] == [
        {
            "start": "2026-09-01T09:00:00+00:00",
            "end": "2026-09-01T10:00:00+00:00",
            "kind": "BUSY",
        }
    ]


def test_an_unanswered_recipient_is_not_an_empty_schedule():
    found = freebusy.query(
        PROFILE,
        session=transport_for(
            response(
                200,
                schedule_response(
                    answer(status="3.7;Could not find principal", data=None)
                ),
            )
        ),
        scheduling=IDENTITY,
        start=WINDOW[0],
        end=WINDOW[1],
        attendees=(BOB,),
    )

    assert found[0].answered is False
    assert found[0].request_status == "3.7;Could not find principal"
    assert found[0].periods == ()


def test_a_recipient_the_server_did_not_answer_for_is_malformed():
    with pytest.raises(scheduling.SchedulingError, match="did not answer for") as error:
        freebusy.query(
            PROFILE,
            session=transport_for(response(200, schedule_response(answer(recipient=ALICE)))),
            scheduling=IDENTITY,
            start=WINDOW[0],
            end=WINDOW[1],
            attendees=(BOB,),
        )

    assert error.value.code == exits.MALFORMED_RESPONSE


def test_an_unreadable_recipient_reports_the_server_status_rather_than_the_parse_failure():
    """Sabre answers an unresolvable address by prefixing `mailto:` to what it was given."""
    mangled = "mailto:/remote.php/dav/principals/users/alice/"
    with pytest.raises(scheduling.SchedulingError, match="Could not find principal"):
        freebusy.query(
            PROFILE,
            session=transport_for(
                response(
                    200,
                    schedule_response(
                        answer(
                            recipient=mangled,
                            status="3.7;Could not find principal",
                            data=None,
                        )
                    ),
                )
            ),
            scheduling=IDENTITY,
            start=WINDOW[0],
            end=WINDOW[1],
            attendees=(BOB,),
        )


def test_success_without_calendar_data_is_malformed():
    with pytest.raises(scheduling.SchedulingError, match="without data"):
        freebusy.query(
            PROFILE,
            session=transport_for(
                response(200, schedule_response(answer(status="2.0;Success", data=None)))
            ),
            scheduling=IDENTITY,
            start=WINDOW[0],
            end=WINDOW[1],
            attendees=(BOB,),
        )


def test_a_forbidden_outbox_is_a_scope_refusal():
    with pytest.raises(scheduling.SchedulingError) as error:
        freebusy.query(
            PROFILE,
            session=transport_for(response(403)),
            scheduling=IDENTITY,
            start=WINDOW[0],
            end=WINDOW[1],
        )

    assert error.value.code == exits.SCOPE_DENIED


def test_an_empty_window_is_a_usage_error():
    with pytest.raises(scheduling.SchedulingError) as error:
        freebusy.query(
            PROFILE,
            session=transport_for(),
            scheduling=IDENTITY,
            start=WINDOW[1],
            end=WINDOW[0],
        )

    assert error.value.code == exits.USAGE


def test_a_repeated_attendee_is_a_usage_error():
    """The domain folds and the local part does not, so only a real repeat repeats."""
    with pytest.raises(scheduling.SchedulingError) as error:
        freebusy.query(
            PROFILE,
            session=transport_for(),
            scheduling=IDENTITY,
            start=WINDOW[0],
            end=WINDOW[1],
            attendees=(BOB, "mailto:bob@EXAMPLE.INVALID"),
        )

    assert error.value.code == exits.USAGE


def test_addresses_differing_only_in_local_part_case_are_two_recipients():
    fake = FakeTransport(
        [
            response(
                200,
                schedule_response(
                    answer(recipient=BOB), answer(recipient="mailto:Bob@example.invalid")
                ),
            )
        ]
    )

    found = freebusy.query(
        PROFILE,
        session=session.Session(PROFILE, transport=fake),
        scheduling=IDENTITY,
        start=WINDOW[0],
        end=WINDOW[1],
        attendees=(BOB, "mailto:Bob@example.invalid"),
    )

    assert [item.recipient for item in found] == [BOB, "mailto:Bob@example.invalid"]


def test_a_duplicated_answer_is_malformed():
    with pytest.raises(scheduling.SchedulingError, match="answered twice"):
        freebusy.query(
            PROFILE,
            session=transport_for(response(200, schedule_response(answer(), answer()))),
            scheduling=IDENTITY,
            start=WINDOW[0],
            end=WINDOW[1],
            attendees=(BOB,),
        )


def test_a_period_without_fbtype_reads_as_busy():
    periods = freebusy._calendar_periods(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//x//EN\nBEGIN:VFREEBUSY\nUID:a@b\n"
        "DTSTAMP:20260901T000000Z\nFREEBUSY;FBTYPE=BUSY-TENTATIVE:"
        "20260901T090000Z/20260901T100000Z\nEND:VFREEBUSY\nEND:VCALENDAR"
    )

    assert [period.kind for period in periods] == ["BUSY-TENTATIVE"]


def test_a_duration_period_is_resolved_to_its_end_instant():
    periods = freebusy._calendar_periods(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//x//EN\nBEGIN:VFREEBUSY\nUID:a@b\n"
        "DTSTAMP:20260901T000000Z\nFREEBUSY:20260901T090000Z/PT90M\n"
        "END:VFREEBUSY\nEND:VCALENDAR"
    )

    assert periods[0].end == "2026-09-01T10:30:00+00:00"


def test_a_floating_period_is_refused_instead_of_using_the_host_timezone():
    with pytest.raises(scheduling.SchedulingError, match="without a timezone offset"):
        freebusy._calendar_periods(
            "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//x//EN\nBEGIN:VFREEBUSY\nUID:a@b\n"
            "DTSTAMP:20260901T000000Z\nFREEBUSY:20260901T090000/20260901T100000\n"
            "END:VFREEBUSY\nEND:VCALENDAR"
        )


def test_the_cli_reports_each_recipient_and_its_periods(monkeypatch, tmp_path, capsys):
    import json

    directory = tmp_path / "xdg_config_home" / "ncl"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(VALID)
    transport = FakeTransport(
        [
            principal_response(),
            home_response(),
            options_response(),
            scheduling_response(),
            response(200, schedule_response(answer())),
        ]
    )
    monkeypatch.setattr(session, "UrllibTransport", lambda: transport)

    code = cli.main(
        [
            "cal", "freebusy",
            "--from", "2026-09-01T00:00:00Z",
            "--to", "2026-09-02T00:00:00Z",
            "--attendee", BOB,
            "--json",
        ]
    )

    assert code == exits.OK
    reported = json.loads(capsys.readouterr().out)["freebusy"]
    assert [item["recipient"] for item in reported] == [BOB]
    assert reported[0]["periods"][0]["start"] == "2026-09-01T09:00:00+00:00"
    posted = transport.requests[-1]
    assert posted["method"] == "POST"
    assert b"ORGANIZER:mailto:Alice@example.invalid" in posted["data"]


def test_the_cli_asks_about_this_account_when_no_attendee_is_named(monkeypatch, tmp_path):
    directory = tmp_path / "xdg_config_home" / "ncl"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(VALID)
    transport = FakeTransport(
        [
            principal_response(),
            home_response(),
            options_response(),
            scheduling_response(),
            response(
                200, schedule_response(answer(recipient="mailto:Alice@example.invalid"))
            ),
        ]
    )
    monkeypatch.setattr(session, "UrllibTransport", lambda: transport)

    code = cli.main(
        ["cal", "freebusy", "--from", "2026-09-01T00:00:00Z", "--to", "2026-09-02T00:00:00Z"]
    )

    assert code == exits.OK
    assert b"ATTENDEE:mailto:Alice@example.invalid" in transport.requests[-1]["data"]
