from __future__ import annotations

import datetime as dt
import json
from html import escape

import pytest

from ncl import cli, events, exits, mutate, plans, recurrence
from ncl.config import Profile

PROFILE = Profile(
    "home",
    "https://cloud.example.invalid",
    "pass",
    ("/remote.php/dav/calendars/alice/work/",),
    (),
)
CAL = "https://cloud.example.invalid/remote.php/dav/calendars/alice/work/"
RESOURCE = CAL + "series.ics"

MASTER = b"""BEGIN:VEVENT\r
UID:series@example.invalid\r
SUMMARY:Weekly review\r
DTSTART:20260901T090000Z\r
DTEND:20260901T100000Z\r
DTSTAMP:20260817T120000Z\r
SEQUENCE:4\r
RRULE:FREQ=WEEKLY;COUNT=4\r
EXDATE:20260922T090000Z\r
X-MASTER-KEEP:yes\r
BEGIN:VALARM\r
ACTION:DISPLAY\r
TRIGGER:-PT15M\r
DESCRIPTION:Reminder\r
END:VALARM\r
END:VEVENT\r
"""

MOVED_OVERRIDE = b"""BEGIN:VEVENT\r
UID:series@example.invalid\r
SUMMARY:Moved review\r
DTSTART:20260903T110000Z\r
DTEND:20260903T120000Z\r
DTSTAMP:20260817T120000Z\r
SEQUENCE:2\r
RECURRENCE-ID:20260908T090000Z\r
X-OVERRIDE-KEEP:before\r
END:VEVENT\r
"""

FUTURE_OVERRIDE = b"""BEGIN:VEVENT\r
UID:series@example.invalid\r
SUMMARY:Future exception\r
DTSTART:20260917T130000Z\r
DTEND:20260917T140000Z\r
DTSTAMP:20260817T120000Z\r
SEQUENCE:1\r
RECURRENCE-ID:20260915T090000Z\r
X-OVERRIDE-KEEP:after\r
END:VEVENT\r
"""

SERIES = (
    b"BEGIN:VCALENDAR\r\n"
    b"VERSION:2.0\r\n"
    b"PRODID:-//generic protocol fixture//EN\r\n"
    b"X-CALENDAR-KEEP:yes\r\n"
    + MASTER
    + MOVED_OVERRIDE
    + FUTURE_OVERRIDE
    + b"END:VCALENDAR\r\n"
)


class Response:
    def __init__(
        self,
        status: int,
        body: bytes = b"",
        *,
        etag: str = '"v2"',
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.etag = etag
        self.headers = headers or {}
        self.url = ""

    def header(self, name: str) -> str:
        if name == "ETag":
            return self.etag
        return self.headers.get(name, "")


class Transport:
    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, data=None, **kwargs):
        self.requests.append(
            {"method": method, "url": url, "headers": headers or {}, "data": data, "kwargs": kwargs}
        )
        if not self.responses:
            raise AssertionError(f"unexpected {method} {url}")
        return self.responses.pop(0)


def _get() -> Transport:
    return Transport(Response(200, SERIES, etag='"v1"'))


def _report(raw: bytes = SERIES, *, href: str = RESOURCE, etag: str = '"v1"') -> bytes:
    return (
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:response>"
        f"<d:href>{escape(href)}</d:href>"
        "<d:propstat><d:prop>"
        f"<d:getetag>{etag}</d:getetag>"
        f"<c:calendar-data>{escape(raw.decode())}</c:calendar-data>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        "</d:response></d:multistatus>"
    ).encode()


def _report_transport(raw: bytes = SERIES) -> Transport:
    return Transport(Response(207, _report(raw)))


def _multi_report(*items: tuple[str, bytes]) -> bytes:
    body = [
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    ]
    for href, raw in items:
        body.append(
            "<d:response>"
            f"<d:href>{escape(href)}</d:href>"
            "<d:propstat><d:prop>"
            '<d:getetag>"v1"</d:getetag>'
            f"<c:calendar-data>{escape(raw.decode())}</c:calendar-data>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
            "</d:response>"
        )
    body.append("</d:multistatus>")
    return "".join(body).encode()


def _resource(raw: bytes = SERIES) -> recurrence.Resource:
    return recurrence._validate_resource(
        raw,
        calendar_href=CAL,
        href=RESOURCE,
        etag='"v1"',
    )


def _plan_apply(plan: plans.Plan, transport: Transport):
    with plans.claim(plan.plan_id):
        return plans.apply(PROFILE, session=transport, plan=plan, dispatchers=cli._dispatchers())


@pytest.fixture(autouse=True)
def isolated_plan_store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))


def test_all_component_recurrence_detection_names_exdate_and_scheduling():
    exdate_only = SERIES.replace(b"RRULE:FREQ=WEEKLY;COUNT=4\r\n", b"")
    reference = events._describe(exdate_only, calendar_href=CAL, href=RESOURCE, etag='"v1"')
    assert reference.recurring is True
    assert "EXDATE" in reference.unsupported

    scheduled_override = SERIES.replace(
        b"X-OVERRIDE-KEEP:before",
        b"ATTENDEE:mailto:person@example.invalid\r\nX-OVERRIDE-KEEP:before",
    )
    scheduled = events._describe(scheduled_override, calendar_href=CAL, href=RESOURCE, etag='"v1"')
    assert set(scheduled.unsupported) >= {"ATTENDEE"}
    with pytest.raises(recurrence.RecurrenceError) as error:
        _resource(scheduled_override)
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        (
            "multiple-master",
            SERIES.replace(MOVED_OVERRIDE, MASTER),
        ),
        (
            "orphaned-uid",
            SERIES.replace(
                b"UID:series@example.invalid\r\nSUMMARY:Moved",
                b"UID:other@example.invalid\r\nSUMMARY:Moved",
            ),
        ),
        (
            "duplicate-identity",
            SERIES.replace(FUTURE_OVERRIDE, MOVED_OVERRIDE),
        ),
        (
            "mismatched-date-kind",
            SERIES.replace(
                b"RECURRENCE-ID:20260908T090000Z",
                b"RECURRENCE-ID;VALUE=DATE:20260908",
            ),
        ),
        (
            "nested-vevent",
            SERIES.replace(
                b"END:VALARM\r\n",
                b"END:VALARM\r\nBEGIN:VEVENT\r\nUID:nested@example.invalid\r\nEND:VEVENT\r\n",
            ),
        ),
    ],
)
def test_malformed_master_and_override_structures_fail_closed(label, raw):
    with pytest.raises(events.EventError) as error:
        _resource(raw)
    assert error.value.code in {exits.MALFORMED_RESPONSE, exits.UNSUPPORTED_STRUCTURE}, label


def test_occurrences_are_bounded_expanded_and_keep_moved_override_identity():
    transport = _report_transport()
    found = recurrence.occurrences(
        PROFILE,
        session=transport,
        calendar_href=CAL,
        start=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 30, tzinfo=dt.UTC),
    )

    assert [item.recurrence_id for item in found] == [
        "20260901T090000Z",
        "20260908T090000Z",
        "20260915T090000Z",
    ]
    moved = found[1]
    assert moved.source == "override"
    assert moved.original_start == "20260908T090000Z"
    assert moved.effective_start == "20260903T110000Z"
    assert [request["method"] for request in transport.requests] == ["REPORT"]
    assert (
        'time-range start="20260901T000000Z" end="20260930T000000Z"'
        in transport.requests[0]["data"]
    )


def test_expansion_refuses_safety_ceiling_instead_of_silently_truncating(monkeypatch):
    raw = SERIES.replace(
        b"RRULE:FREQ=WEEKLY;COUNT=4",
        b"RRULE:FREQ=MINUTELY;COUNT=3",
    ).replace(b"EXDATE:20260922T090000Z\r\n", b"")
    monkeypatch.setattr(recurrence, "MAX_EXPANSIONS", 2)
    with pytest.raises(recurrence.RecurrenceError, match="narrow"):
        recurrence.occurrences(
            PROFILE,
            session=_report_transport(raw),
            calendar_href=CAL,
            start=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 9, 1, 1, tzinfo=dt.UTC),
        )


def test_occurrence_discovery_merges_repeatable_rdates_and_exdates_without_duplicates():
    raw = SERIES.replace(
        b"EXDATE:20260922T090000Z\r\n",
        b"RDATE:20260904T090000Z,20260915T090000Z\r\n"
        b"RDATE:20260918T090000Z\r\n"
        b"EXDATE:20260922T090000Z\r\n",
    )
    found = recurrence.occurrences(
        PROFILE,
        session=_report_transport(raw),
        calendar_href=CAL,
        start=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 30, tzinfo=dt.UTC),
    )

    assert [item.recurrence_id for item in found] == [
        "20260901T090000Z",
        "20260908T090000Z",
        "20260904T090000Z",
        "20260915T090000Z",
        "20260918T090000Z",
    ]


@pytest.mark.parametrize(
    ("label", "replacement"),
    [
        (
            "EXRULE",
            (b"RRULE:FREQ=WEEKLY;COUNT=4", b"RRULE:FREQ=WEEKLY;COUNT=4\r\nEXRULE:FREQ=DAILY"),
        ),
        (
            "period RDATE",
            (b"EXDATE:20260922T090000Z\r\n", b"RDATE;VALUE=PERIOD:20260904T090000Z/PT1H\r\n"),
        ),
        (
            "multiple RRULE",
            (
                b"RRULE:FREQ=WEEKLY;COUNT=4\r\n",
                b"RRULE:FREQ=WEEKLY;COUNT=4\r\nRRULE:FREQ=DAILY\r\n",
            ),
        ),
    ],
)
def test_unsupported_recurrence_set_forms_fail_closed(label, replacement):
    raw = SERIES.replace(*replacement)
    with pytest.raises(
        recurrence.RecurrenceError, match=r"supported|period|multiple"
    ) as error:
        _resource(raw)
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE, label


def test_range_this_and_future_identity_is_refused():
    raw = SERIES.replace(
        b"RECURRENCE-ID:20260908T090000Z",
        b"RECURRENCE-ID;RANGE=THISANDFUTURE:20260908T090000Z",
    )
    with pytest.raises(recurrence.RecurrenceError, match="RANGE"):
        _resource(raw)


@pytest.mark.parametrize(
    "wire",
    [
        "20260901T090000Z",
        "TZID=America/New_York:20260901T090000",
        "VALUE=DATE:20260901",
    ],
)
def test_reusable_wire_id_forms_preserve_type_and_timezone(wire):
    parsed = recurrence.parse_wire_id(wire)
    assert parsed.text == wire
    assert parsed.kind in {"DATE", "DATE-TIME"}


@pytest.mark.parametrize(
    ("wire", "kind"),
    [
        ("TZID=America/New_York:20261101T013000", "ambiguous"),
        ("TZID=America/New_York:20260308T023000", "nonexistent"),
    ],
)
def test_recurrence_target_wire_ids_refuse_ambiguous_or_nonexistent_local_times(wire, kind):
    with pytest.raises(recurrence.RecurrenceError, match=kind) as error:
        recurrence.parse_wire_id(wire)
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_occurrence_discovery_emits_tzid_and_date_wire_id_forms():
    tzid = SERIES.replace(
        b"DTSTART:20260901T090000Z",
        b"DTSTART;TZID=America/New_York:20260901T090000",
    ).replace(
        b"DTEND:20260901T100000Z",
        b"DTEND;TZID=America/New_York:20260901T100000",
    ).replace(
        b"EXDATE:20260922T090000Z",
        b"EXDATE;TZID=America/New_York:20260922T090000",
    ).replace(MOVED_OVERRIDE, b"").replace(FUTURE_OVERRIDE, b"")
    date = SERIES.replace(
        b"DTSTART:20260901T090000Z",
        b"DTSTART;VALUE=DATE:20260901",
    ).replace(
        b"DTEND:20260901T100000Z",
        b"DTEND;VALUE=DATE:20260902",
    ).replace(
        b"EXDATE:20260922T090000Z",
        b"EXDATE;VALUE=DATE:20260922",
    ).replace(MOVED_OVERRIDE, b"").replace(FUTURE_OVERRIDE, b"")

    tzid_found = recurrence.occurrences(
        PROFILE,
        session=_report_transport(tzid),
        calendar_href=CAL,
        start=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 3, tzinfo=dt.UTC),
    )
    date_found = recurrence.occurrences(
        PROFILE,
        session=_report_transport(date),
        calendar_href=CAL,
        start=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 3, tzinfo=dt.UTC),
    )

    assert tzid_found[0].recurrence_id == "TZID=America/New_York:20260901T090000"
    assert tzid_found[0].effective_start == "20260901T130000Z"
    assert date_found[0].recurrence_id == "VALUE=DATE:20260901"
    assert date_found[0].effective_start == "20260901"


def test_cli_occurrences_renders_exact_wire_identity_json(monkeypatch, capsys):
    transport = _report_transport()
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli, "_resolved_calendar", lambda profile, session, target: CAL)
    monkeypatch.setattr(cli.session, "Session", lambda profile: transport)

    code = cli.main(
        [
            "cal",
            "occurrences",
            CAL,
            "--from",
            "2026-09-01T00:00:00+00:00",
            "--to",
            "2026-09-30T00:00:00+00:00",
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert code == exits.OK
    assert output["occurrences"][1]["recurrence_id"] == "20260908T090000Z"
    assert output["occurrences"][1]["source"] == "override"


def test_required_targets_and_safe_resource_refusal(monkeypatch):
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: object())
    with pytest.raises(SystemExit):
        cli.main(["cal", "update", RESOURCE, "--summary", "No", "--json"])

    with pytest.raises(events.EventError) as error:
        mutate.plan_update(
            PROFILE,
            session=_get(),
            href=RESOURCE,
            target="resource",
            changes={"SUMMARY": "No"},
        )
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_occurrence_report_ignores_ordinary_resources_but_validates_recurring_ones():
    ordinary = mutate.build_event(
        uid="ordinary@example.invalid",
        summary="Ordinary event",
        start=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 1, 13, tzinfo=dt.UTC),
    ).encode()
    transport = Transport(
        Response(
            207,
            _multi_report((CAL + "ordinary.ics", ordinary), (RESOURCE, SERIES)),
        )
    )

    found = recurrence.occurrences(
        PROFILE,
        session=transport,
        calendar_href=CAL,
        start=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 2, tzinfo=dt.UTC),
    )

    assert found
    assert {item.href for item in found} == {RESOURCE}


def test_occurrence_discovery_rejects_an_override_only_resource():
    raw = SERIES.replace(MASTER, b"")
    with pytest.raises(recurrence.RecurrenceError, match="exactly one"):
        recurrence.occurrences(
            PROFILE,
            session=_report_transport(raw),
            calendar_href=CAL,
            start=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 9, 30, tzinfo=dt.UTC),
        )


def test_series_update_is_master_only_and_preserves_override_component_bytes():
    plan = mutate.plan_update(
        PROFILE,
        session=_get(),
        href=RESOURCE,
        target="series",
        changes={"SUMMARY": "Renamed"},
    )
    payload = plans.payload_bytes(plan.steps[0])
    assert b"SUMMARY:Renamed\r\n" in payload
    assert b"RRULE:FREQ=WEEKLY;COUNT=4\r\n" in payload
    assert MOVED_OVERRIDE in payload
    assert FUTURE_OVERRIDE in payload
    assert b"X-MASTER-KEEP:yes\r\n" in payload
    assert b"BEGIN:VALARM\r\n" in payload
    assert b"X-CALENDAR-KEEP:yes\r\n" in payload


def test_occurrence_update_clones_master_without_recurrence_set_and_preserves_unrelated_bytes():
    plan = mutate.plan_update(
        PROFILE,
        session=_get(),
        href=RESOURCE,
        target="occurrence",
        recurrence_id="20260901T090000Z",
        changes={"SUMMARY": "One-off"},
    )
    payload = plans.payload_bytes(plan.steps[0])
    assert payload.count(b"BEGIN:VEVENT") == 4
    assert payload.count(b"RRULE:FREQ=WEEKLY;COUNT=4") == 1
    assert b"RECURRENCE-ID:20260901T090000Z\r\n" in payload
    assert b"SUMMARY:One-off\r\n" in payload
    assert MOVED_OVERRIDE in payload
    assert FUTURE_OVERRIDE in payload


def test_occurrence_existing_override_edit_keeps_uid_identity_and_other_override_exact():
    plan = mutate.plan_update(
        PROFILE,
        session=_get(),
        href=RESOURCE,
        target="occurrence",
        recurrence_id="20260908T090000Z",
        changes={"SUMMARY": "Edited moved"},
    )
    payload = plans.payload_bytes(plan.steps[0])
    assert b"UID:series@example.invalid\r\n" in payload
    assert b"RECURRENCE-ID:20260908T090000Z\r\n" in payload
    assert b"SUMMARY:Edited moved\r\n" in payload
    assert FUTURE_OVERRIDE in payload
    assert MASTER.replace(b"SUMMARY:Weekly review", b"SUMMARY:Weekly review") in payload


def test_occurrence_delete_creates_cancelled_exception_without_exdate_edit():
    plan = mutate.plan_delete(
        PROFILE,
        session=_get(),
        href=RESOURCE,
        target="occurrence",
        recurrence_id="20260901T090000Z",
    )
    payload = plans.payload_bytes(plan.steps[0])
    assert b"RECURRENCE-ID:20260901T090000Z\r\n" in payload
    assert b"STATUS:CANCELLED\r\n" in payload
    assert payload.count(b"EXDATE:") == 1
    assert b"EXDATE:20260922T090000Z\r\n" in payload
    assert MOVED_OVERRIDE in payload


def test_this_and_future_update_uses_new_uid_new_first_order_and_exact_headers():
    plan = mutate.plan_update(
        PROFILE,
        session=_get(),
        href=RESOURCE,
        target="this-and-future",
        recurrence_id="20260915T090000Z",
        changes={"SUMMARY": "Future title"},
    )
    assert [step.action for step in plan.steps] == ["cal.create", "cal.update"]
    create, update = plan.steps
    assert create.etag == ""
    assert update.etag == '"v1"'
    assert create.href != RESOURCE
    new_payload = plans.payload_bytes(create)
    old_payload = plans.payload_bytes(update)
    assert b"RANGE=THISANDFUTURE" not in new_payload + old_payload
    assert b"UID:series@example.invalid\r\n" not in new_payload
    assert b"RRULE:FREQ=WEEKLY;COUNT=2\r\n" in new_payload
    assert b"RRULE:FREQ=WEEKLY;COUNT=2\r\n" in old_payload
    assert b"RECURRENCE-ID:20260915T090000Z\r\n" in new_payload
    assert b"UID:series@example.invalid\r\n" in old_payload
    assert MOVED_OVERRIDE in old_payload
    assert b"X-OVERRIDE-KEEP:after\r\n" in new_payload
    assert plan.steps[0].details["warning"].startswith("This split is non-atomic")

    transport = Transport(
        Response(201),
        Response(200, new_payload, etag='"new"'),
        Response(204),
        Response(200, old_payload, etag='"old"'),
    )
    _plan_apply(plan, transport)
    assert [request["method"] for request in transport.requests] == ["PUT", "GET", "PUT", "GET"]
    assert transport.requests[0]["headers"]["If-None-Match"] == "*"
    assert transport.requests[2]["headers"]["If-Match"] == '"v1"'


def test_this_and_future_keeps_a_proven_simple_until_partition():
    raw = SERIES.replace(
        b"RRULE:FREQ=WEEKLY;COUNT=4",
        b"RRULE:FREQ=WEEKLY;UNTIL=20260922T090000Z",
    )
    plan = mutate.plan_update(
        PROFILE,
        session=Transport(Response(200, raw, etag='"v1"')),
        href=RESOURCE,
        target="this-and-future",
        recurrence_id="20260915T090000Z",
        changes={"SUMMARY": "Future title"},
    )

    old_payload = plans.payload_bytes(plan.steps[1])
    new_payload = plans.payload_bytes(plan.steps[0])
    assert b"RRULE:FREQ=WEEKLY;UNTIL=20260908T090000Z\r\n" in old_payload
    assert b"RRULE:FREQ=WEEKLY;UNTIL=20260922T090000Z\r\n" in new_payload


def test_this_and_future_refuses_an_unbounded_complex_rrule_partition():
    raw = SERIES.replace(
        b"RRULE:FREQ=WEEKLY;COUNT=4",
        b"RRULE:FREQ=WEEKLY;BYDAY=MO,WE",
    ).replace(MOVED_OVERRIDE, b"").replace(FUTURE_OVERRIDE, b"")
    with pytest.raises(recurrence.RecurrenceError, match="cannot prove"):
        recurrence.plan_update(
            PROFILE,
            session=Transport(Response(200, raw, etag='"v1"')),
            href=RESOURCE,
            target="this-and-future",
            recurrence_id="20260909T090000Z",
            changes={"SUMMARY": "Future title"},
        )


def test_this_and_future_partial_failure_resumes_without_recreating_new_resource():
    plan = mutate.plan_update(
        PROFILE,
        session=_get(),
        href=RESOURCE,
        target="this-and-future",
        recurrence_id="20260915T090000Z",
        changes={"SUMMARY": "Future title"},
    )
    new_payload, old_payload = (plans.payload_bytes(step) for step in plan.steps)
    first = Transport(Response(201), Response(200, new_payload, etag='"new"'), Response(500))
    with pytest.raises(events.EventError) as error:
        _plan_apply(plan, first)
    assert error.value.code == exits.SERVER_ERROR
    stored = plans.read(plan.plan_id)
    assert [item.state for item in stored.progress] == ["verified", "pending"]

    resumed = Transport(Response(204), Response(200, old_payload, etag='"old"'))
    _plan_apply(stored, resumed)
    assert [request["method"] for request in resumed.requests] == ["PUT", "GET"]
    with pytest.raises(plans.PlanError) as missing:
        plans.read(plan.plan_id)
    assert missing.value.code == exits.TARGET_NOT_FOUND


def test_this_and_future_readback_mismatch_records_uncertainty_for_reconcile():
    plan = mutate.plan_update(
        PROFILE,
        session=_get(),
        href=RESOURCE,
        target="this-and-future",
        recurrence_id="20260915T090000Z",
        changes={"SUMMARY": "Future title"},
    )
    new_payload, old_payload = (plans.payload_bytes(step) for step in plan.steps)
    altered_old = old_payload.replace(b"SUMMARY:Weekly review", b"SUMMARY:Unexpected")
    transport = Transport(
        Response(201),
        Response(200, new_payload, etag='"new"'),
        Response(204),
        Response(200, altered_old, etag='"changed"'),
    )

    with pytest.raises(events.EventError) as error:
        _plan_apply(plan, transport)
    assert error.value.code == exits.OUTCOME_UNCERTAIN
    stored = plans.read(plan.plan_id)
    assert [item.state for item in stored.progress] == ["verified", "uncertain"]

    with plans.claim(plan.plan_id), pytest.raises(plans.PlanError) as reconcile_error:
        plans.reconcile(
            PROFILE,
            session=Transport(Response(200, altered_old, etag='"changed"')),
            plan=stored,
            dispatchers=cli._dispatchers(),
        )
    assert reconcile_error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).progress[1].state == "uncertain"


def test_this_and_future_refuses_a_user_requested_time_shift():
    with pytest.raises(recurrence.RecurrenceError, match="does not shift"):
        recurrence.plan_update(
            PROFILE,
            session=_get(),
            href=RESOURCE,
            target="this-and-future",
            recurrence_id="20260915T090000Z",
            changes={"dtstart": dt.datetime(2026, 9, 16, 9, tzinfo=dt.UTC)},
        )


def test_this_and_future_delete_only_truncates_old_resource():
    plan = mutate.plan_delete(
        PROFILE,
        session=_get(),
        href=RESOURCE,
        target="this-and-future",
        recurrence_id="20260915T090000Z",
    )
    assert len(plan.steps) == 1
    assert plan.steps[0].action == "cal.update"
    payload = plans.payload_bytes(plan.steps[0])
    assert b"COUNT=2" in payload
    assert b"RECURRENCE-ID:20260915T090000Z" not in payload
    assert FUTURE_OVERRIDE not in payload


def test_scheduled_resources_are_refused_for_every_recurrence_target():
    raw = SERIES.replace(
        b"X-MASTER-KEEP:yes",
        b"ORGANIZER:mailto:organizer@example.invalid\r\nX-MASTER-KEEP:yes",
    )
    for target in ("series", "occurrence", "this-and-future"):
        kwargs = {"target": target, "changes": {"SUMMARY": "No"}}
        if target != "series":
            kwargs["recurrence_id"] = "20260901T090000Z"
        with pytest.raises(events.EventError) as error:
            recurrence.plan_update(
                PROFILE,
                session=Transport(Response(200, raw, etag='"v1"')),
                href=RESOURCE,
                **kwargs,
            )
        assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_parser_help_guide_and_docs_name_occurrence_targets(capsys):
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "cal",
            "occurrences",
            CAL,
            "--from",
            "2026-09-01T00:00:00+00:00",
            "--to",
            "2026-09-02T00:00:00+00:00",
        ]
    )
    assert args.cal_command == "occurrences"
    update = parser.parse_args(["cal", "update", RESOURCE, "--target", "series", "--summary", "x"])
    assert update.target == "series"
    with pytest.raises(SystemExit):
        parser.parse_args(["cal", "update", "--help"])
    assert "--recurrence-id" in capsys.readouterr().out
    assert "recurrence" in cli.guide.render().lower()


def test_plan_json_does_not_echo_recurrence_payload():
    plan = mutate.plan_update(
        PROFILE,
        session=_get(),
        href=RESOURCE,
        target="series",
        changes={"SUMMARY": "Redacted"},
    )
    rendered = json.dumps(plan.as_dict())
    assert "X-OVERRIDE-KEEP" not in rendered
    assert "BEGIN:VCALENDAR" not in rendered


def _single_event_calendar(event: bytes) -> bytes:
    return (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//probe//EN\r\n"
        + event
        + b"END:VCALENDAR\r\n"
    )


def _probe_event(body: bytes) -> bytes:
    return (
        b"BEGIN:VEVENT\r\nUID:probe@example.invalid\r\nSUMMARY:Probe\r\n"
        + body
        + b"END:VEVENT\r\n"
    )


def test_dtend_duration_remains_exact_across_a_dst_transition():
    raw = _single_event_calendar(
        _probe_event(
            b"DTSTART;TZID=America/New_York:20260307T013000\r\n"
            b"DTEND;TZID=America/New_York:20260307T033000\r\n"
            b"DTSTAMP:20260817T120000Z\r\n"
            b"SEQUENCE:0\r\n"
            b"RRULE:FREQ=DAILY;COUNT=3\r\n"
        )
    )
    found = recurrence.occurrences(
        PROFILE,
        session=_report_transport(raw),
        calendar_href=CAL,
        start=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        end=dt.datetime(2026, 12, 31, tzinfo=dt.UTC),
    )

    assert [(item.effective_start, item.effective_end) for item in found] == [
        ("20260307T063000Z", "20260307T083000Z"),
        ("20260308T063000Z", "20260308T083000Z"),
        ("20260309T053000Z", "20260309T073000Z"),
    ]


def test_future_split_preserves_nominal_duration_and_unrelated_bytes():
    raw = _single_event_calendar(
        _probe_event(
            b"SUMMARY:Nominal duration\r\n"
            b"DTSTART;TZID=America/New_York:20260307T090000\r\n"
            b"DURATION:P1D\r\n"
            b"DTSTAMP:20260817T120000Z\r\n"
            b"SEQUENCE:0\r\n"
            b"RRULE:FREQ=DAILY;COUNT=3\r\n"
            b"X-MASTER-KEEP:yes\r\n"
        )
    )
    plan = mutate.plan_update(
        PROFILE,
        session=Transport(Response(200, raw, etag='"v1"')),
        href=RESOURCE,
        target="this-and-future",
        recurrence_id="TZID=America/New_York:20260308T090000",
        changes={"SUMMARY": "Future duration"},
    )

    new_payload = plans.payload_bytes(plan.steps[0])
    assert b"DURATION:P1D\r\n" in new_payload
    assert b"DTEND" not in new_payload
    assert b"X-MASTER-KEEP:yes\r\n" in new_payload


def test_all_day_recurrence_without_end_uses_the_default_one_day_duration():
    raw = _single_event_calendar(
        _probe_event(
            b"DTSTART;VALUE=DATE:20260901\r\n"
            b"DTSTAMP:20260817T120000Z\r\n"
            b"SEQUENCE:0\r\n"
            b"RRULE:FREQ=DAILY;COUNT=2\r\n"
        )
    )
    found = recurrence.occurrences(
        PROFILE,
        session=_report_transport(raw),
        calendar_href=CAL,
        start=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        end=dt.datetime(2026, 9, 3, tzinfo=dt.UTC),
    )

    assert [(item.recurrence_id, item.effective_start, item.effective_end) for item in found] == [
        ("VALUE=DATE:20260901", "20260901", "20260902"),
        ("VALUE=DATE:20260902", "20260902", "20260903"),
    ]


def test_count_split_retains_explicit_dtstart_before_the_first_rrule_value():
    raw = _single_event_calendar(
        _probe_event(
            b"DTSTART:20260901T090000Z\r\n"
            b"DTEND:20260901T100000Z\r\n"
            b"DTSTAMP:20260817T120000Z\r\n"
            b"SEQUENCE:0\r\n"
            b"RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=2\r\n"
        )
    )
    plan = mutate.plan_update(
        PROFILE,
        session=Transport(Response(200, raw, etag='"v1"')),
        href=RESOURCE,
        target="this-and-future",
        recurrence_id="20260907T090000Z",
        changes={"SUMMARY": "Future"},
    )

    assert [step.action for step in plan.steps] == ["cal.create", "cal.update"]
    assert b"DTSTART:20260901T090000Z\r\n" in plans.payload_bytes(plan.steps[1])


@pytest.mark.parametrize(
    ("start", "end", "kind"),
    [
        (
            b"DTSTART;TZID=America/New_York:20261101T013000\r\n",
            b"DTEND;TZID=America/New_York:20261101T023000\r\n",
            "ambiguous",
        ),
        (
            b"DTSTART;TZID=America/New_York:20260308T023000\r\n",
            b"DTEND;TZID=America/New_York:20260308T033000\r\n",
            "nonexistent",
        ),
    ],
)
def test_ambiguous_and_nonexistent_tzid_boundaries_are_refused(start, end, kind):
    raw = _single_event_calendar(
        _probe_event(
            start
            + end
            + b"DTSTAMP:20260817T120000Z\r\n"
            + b"SEQUENCE:0\r\n"
            + b"RRULE:FREQ=DAILY;COUNT=2\r\n"
        )
    )

    with pytest.raises(recurrence.RecurrenceError, match=kind) as error:
        _resource(raw)
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_generated_tzid_gap_is_refused_during_expansion():
    raw = _single_event_calendar(
        _probe_event(
            b"DTSTART;TZID=America/New_York:20260307T023000\r\n"
            b"DTEND;TZID=America/New_York:20260307T033000\r\n"
            b"DTSTAMP:20260817T120000Z\r\n"
            b"SEQUENCE:0\r\n"
            b"RRULE:FREQ=DAILY;COUNT=2\r\n"
        )
    )

    with pytest.raises(recurrence.RecurrenceError, match="nonexistent"):
        recurrence.occurrences(
            PROFILE,
            session=_report_transport(raw),
            calendar_href=CAL,
            start=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
            end=dt.datetime(2026, 3, 12, tzinfo=dt.UTC),
        )


def test_override_boundaries_must_match_master_kind_and_timezone():
    raw = _single_event_calendar(
        _probe_event(
            b"DTSTART;TZID=America/New_York:20260901T090000\r\n"
            b"DTEND;TZID=America/New_York:20260901T100000\r\n"
            b"DTSTAMP:20260817T120000Z\r\n"
            b"SEQUENCE:0\r\n"
            b"RRULE:FREQ=DAILY;COUNT=2\r\n"
        )
        + _probe_event(
            b"SUMMARY:Moved\r\n"
            b"DTSTART;TZID=America/Los_Angeles:20260902T090000\r\n"
            b"DTEND;TZID=America/Los_Angeles:20260902T100000\r\n"
            b"DTSTAMP:20260817T120000Z\r\n"
            b"SEQUENCE:0\r\n"
            b"RECURRENCE-ID;TZID=America/New_York:20260902T090000\r\n"
        )
    )

    with pytest.raises(recurrence.RecurrenceError, match="boundaries") as error:
        _resource(raw)
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_floating_override_is_refused_before_occurrence_planning():
    raw = _single_event_calendar(
        _probe_event(
            b"DTSTART:20260901T090000Z\r\n"
            b"DTEND:20260901T100000Z\r\n"
            b"DTSTAMP:20260817T120000Z\r\n"
            b"SEQUENCE:0\r\n"
            b"RRULE:FREQ=DAILY;COUNT=2\r\n"
        )
        + _probe_event(
            b"SUMMARY:Floating\r\n"
            b"DTSTART:20260902T090000\r\n"
            b"DTEND:20260902T100000\r\n"
            b"DTSTAMP:20260817T120000Z\r\n"
            b"SEQUENCE:0\r\n"
            b"RECURRENCE-ID:20260902T090000Z\r\n"
        )
    )

    with pytest.raises(recurrence.RecurrenceError, match="floating") as error:
        _resource(raw)
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_occurrence_update_cannot_change_a_timed_master_to_a_date_exception():
    raw = _single_event_calendar(
        _probe_event(
            b"DTSTART:20260901T090000Z\r\n"
            b"DTEND:20260901T100000Z\r\n"
            b"DTSTAMP:20260817T120000Z\r\n"
            b"SEQUENCE:0\r\n"
            b"RRULE:FREQ=DAILY;COUNT=2\r\n"
        )
    )

    with pytest.raises(recurrence.RecurrenceError, match="boundaries") as error:
        mutate.plan_update(
            PROFILE,
            session=Transport(Response(200, raw, etag='"v1"')),
            href=RESOURCE,
            target="occurrence",
            recurrence_id="20260902T090000Z",
            changes={"DTSTART": dt.date(2026, 9, 2), "DTEND": dt.date(2026, 9, 3)},
        )
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


@pytest.mark.parametrize("rule", ["FREQ=HOURLY;COUNT=3", "FREQ=DAILY;UNTIL=20260903T000000Z"])
def test_date_recurrences_reject_time_based_rrule_forms(rule):
    raw = _single_event_calendar(
        _probe_event(
            b"DTSTART;VALUE=DATE:20260901\r\n"
            b"DTEND;VALUE=DATE:20260902\r\n"
            b"DTSTAMP:20260817T120000Z\r\n"
            b"SEQUENCE:0\r\n"
            + f"RRULE:{rule}\r\n".encode()
        )
    )

    with pytest.raises(recurrence.RecurrenceError, match="DATE") as error:
        _resource(raw)
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_nested_unsupported_component_is_refused_before_planning():
    raw = _single_event_calendar(
        _probe_event(
            b"DTSTART:20260901T090000Z\r\n"
            b"DTEND:20260901T100000Z\r\n"
            b"DTSTAMP:20260817T120000Z\r\n"
            b"SEQUENCE:0\r\n"
            b"RRULE:FREQ=DAILY;COUNT=2\r\n"
            b"BEGIN:VTODO\r\n"
            b"UID:nested@example.invalid\r\n"
            b"SUMMARY:Nested\r\n"
            b"END:VTODO\r\n"
        )
    )

    with pytest.raises(recurrence.RecurrenceError, match="VTODO") as error:
        _resource(raw)
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_malformed_post_write_readback_records_uncertainty():
    plan = mutate.plan_update(
        PROFILE,
        session=_get(),
        href=RESOURCE,
        target="series",
        changes={"SUMMARY": "Updated"},
    )
    malformed = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//probe//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:series@example.invalid\r\nSUMMARY:Updated\r\n"
        b"DTSTART:TZID=America/New_York:20260901T090000\r\n"
        b"DTEND:20260901T100000Z\r\nDTSTAMP:20260817T120000Z\r\n"
        b"SEQUENCE:1\r\nRRULE:FREQ=DAILY;COUNT=2\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    transport = Transport(Response(204), Response(200, malformed, etag='"v2"'))

    with plans.claim(plan.plan_id), pytest.raises(events.EventError) as error:
        plans.apply(PROFILE, session=transport, plan=plan, dispatchers=cli._dispatchers())

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    stored = plans.read(plan.plan_id)
    assert [(item.state, item.exit_code) for item in stored.progress] == [
        ("uncertain", exits.OUTCOME_UNCERTAIN)
    ]

    with plans.claim(plan.plan_id), pytest.raises(events.EventError) as reconcile_error:
        plans.reconcile(
            PROFILE,
            session=Transport(Response(200, malformed, etag='"v2"')),
            plan=stored,
            dispatchers=cli._dispatchers(),
        )
    assert reconcile_error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).progress[0].state == "uncertain"
