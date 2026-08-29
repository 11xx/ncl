"""Scheduling discovery and role classification, entirely offline.

Every request here is answered by an injected transport. A scheduling test that
reached a real server would be asking that server to notify real people, which
is precisely the effect the contract exists to gate.
"""

from __future__ import annotations

import icalendar
import pytest
from test_auth import FakeTransport, dav_response, home_response, principal_response, response
from test_config import VALID

from ncl import cli, exits, identity, scheduling, secrets, session
from ncl.config import Profile

PROFILE = Profile(
    name="home",
    origin="https://cloud.example.invalid",
    secret_backend="pass",
    calendars=("/remote.php/dav/calendars/alice/",),
    files_roots=(),
)
PRINCIPAL = "https://cloud.example.invalid/remote.php/dav/principals/users/alice/"


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setattr(
        secrets, "load_credential", lambda profile: secrets.Credential("alice", "app-password")
    )


def options_response(tokens: str = "1, 3, calendar-access, calendar-auto-schedule"):
    return response(200, headers={"DAV": tokens})


def scheduling_response(
    addresses: str = (
        "<d:href>mailto:Alice@Example.INVALID</d:href>"
        "<d:href>/remote.php/dav/principals/users/alice/</d:href>"
    ),
    inbox: str = "<d:href>/remote.php/dav/calendars/alice/inbox/</d:href>",
    outbox: str = "<d:href>/remote.php/dav/calendars/alice/outbox/</d:href>",
    status: str = "HTTP/1.1 200 OK",
):
    return response(
        207,
        dav_response(
            f"""
    <d:propstat><d:prop>
      <c:calendar-user-address-set>{addresses}</c:calendar-user-address-set>
      <c:schedule-inbox-URL>{inbox}</c:schedule-inbox-URL>
      <c:schedule-outbox-URL>{outbox}</c:schedule-outbox-URL>
    </d:prop><d:status>{status}</d:status></d:propstat>
"""
        ),
    )


def discover(responses) -> tuple[scheduling.SchedulingIdentity, list[dict]]:
    transport = FakeTransport(responses)
    authenticated = session.Session(PROFILE, transport=transport)
    result = identity.Identity(
        principal_url=PRINCIPAL,
        account_name="alice",
        display_name="Alice",
        calendar_home="https://cloud.example.invalid/remote.php/dav/calendars/alice/",
    )
    return (
        scheduling.discover(PROFILE, session=authenticated, identity=result),
        transport.requests,
    )


def identity_for(*addresses: str) -> scheduling.SchedulingIdentity:
    return scheduling.SchedulingIdentity(
        principal_url=PRINCIPAL,
        addresses=tuple(sorted(addresses)),
        schedule_inbox=f"{PRINCIPAL}inbox/",
        schedule_outbox=f"{PRINCIPAL}outbox/",
    )


def event(organizer: str | None, attendees: tuple[str, ...], **params) -> icalendar.Event:
    component = icalendar.Event()
    component.add("UID", "event-1")
    if organizer is not None:
        component.add("ORGANIZER", organizer, parameters=params.get("organizer_params"))
    for attendee in attendees:
        component.add("ATTENDEE", attendee, parameters=params.get("attendee_params"))
    return component


def test_discovery_reports_canonical_addresses_and_boxes():
    result, _ = discover([options_response(), scheduling_response()])

    assert result.addresses == ("mailto:Alice@example.invalid",)
    assert result.schedule_inbox == (
        "https://cloud.example.invalid/remote.php/dav/calendars/alice/inbox/"
    )
    assert result.schedule_outbox == (
        "https://cloud.example.invalid/remote.php/dav/calendars/alice/outbox/"
    )


def test_discovery_asks_exactly_the_documented_requests():
    _, requests = discover([options_response(), scheduling_response()])

    assert [(item["method"], item["url"]) for item in requests] == [
        ("OPTIONS", PRINCIPAL),
        ("PROPFIND", PRINCIPAL),
    ]
    propfind = requests[1]
    assert propfind["headers"]["Depth"] == "0"
    for name in ("calendar-user-address-set", "schedule-inbox-URL", "schedule-outbox-URL"):
        assert f"<c:{name}/>".encode() in propfind["data"]


def test_discovery_refuses_a_server_that_does_not_schedule():
    with pytest.raises(scheduling.SchedulingError) as error:
        discover([options_response("1, 3, calendar-access")])

    assert error.value.code == exits.PRECONDITION_FAILED
    assert "calendar-auto-schedule" in error.value.message


def test_discovery_refuses_a_property_returned_under_a_failure_status():
    with pytest.raises(scheduling.SchedulingError) as error:
        discover([options_response(), scheduling_response(status="HTTP/1.1 404 Not Found")])

    assert error.value.code == exits.MALFORMED_RESPONSE


def test_discovery_refuses_an_empty_property():
    with pytest.raises(scheduling.SchedulingError) as error:
        discover([options_response(), scheduling_response(inbox="<d:href></d:href>")])

    assert "empty href" in error.value.message


def test_discovery_refuses_a_cross_origin_box():
    with pytest.raises(scheduling.SchedulingError) as error:
        discover(
            [
                options_response(),
                scheduling_response(outbox="<d:href>https://elsewhere.invalid/outbox/</d:href>"),
            ]
        )

    assert "outside the configured origin" in error.value.message


def test_discovery_refuses_two_urls_for_one_box():
    with pytest.raises(scheduling.SchedulingError) as error:
        discover(
            [
                options_response(),
                scheduling_response(
                    inbox="<d:href>/a/inbox/</d:href><d:href>/b/inbox/</d:href>"
                ),
            ]
        )

    assert "more than one URL" in error.value.message


def test_discovery_refuses_an_address_set_holding_no_mailbox():
    with pytest.raises(scheduling.SchedulingError) as error:
        discover(
            [
                options_response(),
                scheduling_response(
                    addresses="<d:href>/remote.php/dav/principals/users/alice/</d:href>"
                ),
            ]
        )

    assert error.value.code == exits.PRECONDITION_FAILED


def test_duplicate_addresses_collapse_to_one():
    result, _ = discover(
        [
            options_response(),
            scheduling_response(
                addresses=(
                    "<d:href>mailto:alice@example.invalid</d:href>"
                    "<d:href>mailto:alice@EXAMPLE.invalid</d:href>"
                )
            ),
        ]
    )

    assert result.addresses == ("mailto:alice@example.invalid",)


@pytest.mark.parametrize(
    "value",
    [
        "https://cloud.example.invalid/principals/users/alice/",
        "mailto:alice@example.invalid?subject=hi",
        "mailto:alice",
        "mailto:@example.invalid",
        "mailto:alice@one@two",
        "mailto:ali ce@example.invalid",
        "mailto:ali%FFce@example.invalid",
        "",
    ],
)
def test_canonical_address_refuses_anything_but_one_mailbox(value):
    with pytest.raises(scheduling.SchedulingError) as error:
        scheduling.canonical_address(value, label="ORGANIZER")

    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_canonical_address_folds_only_the_case_insensitive_parts():
    assert (
        scheduling.canonical_address("MAILTO:Alice.B@Example.INVALID", label="ORGANIZER")
        == "mailto:Alice.B@example.invalid"
    )


def test_organizer_role_exposes_every_attendee_as_a_recipient_at_risk():
    role = scheduling.classify(
        event(
            "mailto:alice@example.invalid",
            ("mailto:zoe@example.invalid", "mailto:bob@example.invalid"),
        ),
        identity_for("mailto:alice@example.invalid"),
    )

    assert role.role == "organizer"
    assert role.address == "mailto:alice@example.invalid"
    assert role.recipients_at_risk == (
        "mailto:bob@example.invalid",
        "mailto:zoe@example.invalid",
    )


def test_attendee_role_exposes_only_the_organizer():
    role = scheduling.classify(
        event(
            "mailto:carol@example.invalid",
            ("mailto:alice@example.invalid", "mailto:bob@example.invalid"),
        ),
        identity_for("mailto:alice@example.invalid"),
    )

    assert role.role == "attendee"
    assert role.address == "mailto:alice@example.invalid"
    assert role.recipients_at_risk == ("mailto:carol@example.invalid",)


def test_an_alias_in_the_address_set_still_matches():
    role = scheduling.classify(
        event("mailto:alice@example.invalid", ("mailto:bob@example.invalid",)),
        identity_for("mailto:a.liddell@example.invalid", "mailto:alice@example.invalid"),
    )

    assert role.role == "organizer"


def test_a_mixed_role_is_ambiguous_rather_than_organizer_owned():
    with pytest.raises(scheduling.SchedulingError) as error:
        scheduling.classify(
            event(
                "mailto:alice@example.invalid",
                ("mailto:alice@example.invalid", "mailto:bob@example.invalid"),
            ),
            identity_for("mailto:alice@example.invalid"),
        )

    assert error.value.code == exits.AMBIGUOUS_TARGET


def test_two_addresses_of_this_account_attending_is_ambiguous():
    with pytest.raises(scheduling.SchedulingError) as error:
        scheduling.classify(
            event(
                "mailto:carol@example.invalid",
                ("mailto:alice@example.invalid", "mailto:a.liddell@example.invalid"),
            ),
            identity_for("mailto:alice@example.invalid", "mailto:a.liddell@example.invalid"),
        )

    assert error.value.code == exits.AMBIGUOUS_TARGET


def test_an_uninvolved_account_holds_no_role():
    with pytest.raises(scheduling.SchedulingError) as error:
        scheduling.classify(
            event("mailto:carol@example.invalid", ("mailto:bob@example.invalid",)),
            identity_for("mailto:alice@example.invalid"),
        )

    assert error.value.code == exits.UNSUPPORTED_STRUCTURE
    assert "neither the organizer nor an attendee" in error.value.message


def test_a_repeated_attendee_refuses():
    with pytest.raises(scheduling.SchedulingError) as error:
        scheduling.classify(
            event(
                "mailto:alice@example.invalid",
                ("mailto:bob@example.invalid", "mailto:bob@EXAMPLE.invalid"),
            ),
            identity_for("mailto:alice@example.invalid"),
        )

    assert error.value.code == exits.UNSUPPORTED_STRUCTURE
    assert "more than once" in error.value.message


@pytest.mark.parametrize("attendees", [(), ("mailto:bob@example.invalid",)])
def test_a_non_scheduling_event_holds_no_role(attendees):
    organizer = None if attendees else "mailto:alice@example.invalid"
    with pytest.raises(scheduling.SchedulingError) as error:
        scheduling.classify(
            event(organizer, attendees), identity_for("mailto:alice@example.invalid")
        )

    assert error.value.code == exits.UNSUPPORTED_STRUCTURE
    assert "not a scheduling object" in error.value.message


def test_more_than_one_organizer_refuses():
    component = event("mailto:alice@example.invalid", ("mailto:bob@example.invalid",))
    component.add("ORGANIZER", "mailto:carol@example.invalid")

    with pytest.raises(scheduling.SchedulingError) as error:
        scheduling.classify(component, identity_for("mailto:alice@example.invalid"))

    assert "more than one ORGANIZER" in error.value.message


@pytest.mark.parametrize("agent", ["CLIENT", "NONE", "SOMETHING-ELSE"])
def test_a_participant_the_server_does_not_schedule_refuses(agent):
    component = event(
        "mailto:alice@example.invalid",
        ("mailto:bob@example.invalid",),
        attendee_params={"SCHEDULE-AGENT": agent},
    )

    with pytest.raises(scheduling.SchedulingError) as error:
        scheduling.classify(component, identity_for("mailto:alice@example.invalid"))

    assert error.value.code == exits.UNSUPPORTED_STRUCTURE
    assert "SCHEDULE-AGENT" in error.value.message


def test_an_explicit_server_agent_is_accepted():
    component = event(
        "mailto:alice@example.invalid",
        ("mailto:bob@example.invalid",),
        attendee_params={"SCHEDULE-AGENT": "server"},
    )

    assert scheduling.classify(component, identity_for("mailto:alice@example.invalid")).role == (
        "organizer"
    )


def _run(monkeypatch, tmp_path, argv, responses):
    directory = tmp_path / "xdg_config_home" / "ncl"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(VALID)
    transport = FakeTransport(responses)
    monkeypatch.setattr(session, "UrllibTransport", lambda: transport)
    return cli.main(argv), transport


def test_whoami_reports_the_scheduling_identity_on_request(monkeypatch, tmp_path, capsys):
    code, _ = _run(
        monkeypatch,
        tmp_path,
        ["whoami", "--scheduling", "--json"],
        [principal_response(), home_response(), options_response(), scheduling_response()],
    )

    assert code == exits.OK
    import json

    reported = json.loads(capsys.readouterr().out)["scheduling"]
    assert reported["addresses"] == ["mailto:Alice@example.invalid"]
    assert reported["schedule_inbox"].endswith("/calendars/alice/inbox/")


def test_whoami_without_the_flag_asks_nothing_about_scheduling(monkeypatch, tmp_path, capsys):
    code, transport = _run(
        monkeypatch, tmp_path, ["whoami"], [principal_response(), home_response()]
    )

    assert code == exits.OK
    assert [item["method"] for item in transport.requests] == ["PROPFIND", "PROPFIND"]
    assert "scheduling" not in capsys.readouterr().out


def test_whoami_renders_the_scheduling_identity_for_a_person(monkeypatch, tmp_path, capsys):
    """The human path flattens the nested identity instead of printing a dict.

    A rendered `{'addresses': [...]}` is the shape of the JSON output leaking
    into the output that exists because the caller did not ask for JSON.
    """
    code, _ = _run(
        monkeypatch,
        tmp_path,
        ["whoami", "--scheduling"],
        [
            principal_response(),
            home_response(),
            options_response(),
            scheduling_response(
                addresses=(
                    "<d:href>mailto:alice@example.invalid</d:href>"
                    "<d:href>mailto:a.liddell@example.invalid</d:href>"
                )
            ),
        ],
    )

    assert code == exits.OK
    rendered = capsys.readouterr().out
    assert "account_name: alice" in rendered
    assert (
        "scheduling.addresses: mailto:a.liddell@example.invalid, "
        "mailto:alice@example.invalid" in rendered
    )
    assert "scheduling.schedule_inbox: https://" in rendered
    assert "[" not in rendered


def test_whoami_scheduling_refusal_keeps_its_exit_code(monkeypatch, tmp_path):
    code, _ = _run(
        monkeypatch,
        tmp_path,
        ["whoami", "--scheduling"],
        [principal_response(), home_response(), options_response("1, 3, calendar-access")],
    )

    assert code == exits.PRECONDITION_FAILED
