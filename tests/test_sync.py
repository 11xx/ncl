from __future__ import annotations

import json

import pytest
from test_auth import FakeTransport, response

from ncl import cli, exits, sync
from ncl.config import Profile

PROFILE = Profile(
    name="home",
    origin="https://cloud.example.invalid",
    secret_backend="pass",
    calendars=("/remote.php/dav/calendars/alice/work/",),
    files_roots=(),
    addressbooks=("/remote.php/dav/addressbooks/users/alice/contacts/",),
)
CALENDAR = "https://cloud.example.invalid/remote.php/dav/calendars/alice/work/"
BOOK = "https://cloud.example.invalid/remote.php/dav/addressbooks/users/alice/contacts/"
CURSOR = "http://sabre.io/ns/sync/7"


def multistatus(entries: str = "", cursor: str | None = CURSOR) -> bytes:
    token = "" if cursor is None else f"<d:sync-token>{cursor}</d:sync-token>"
    return (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        f"{token}{entries}</d:multistatus>"
    ).encode()


def changed(href: str, etag: str | None = None) -> str:
    property_value = "" if etag is None else etag
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
        f"<d:getetag>{property_value}</d:getetag></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


@pytest.mark.parametrize("since", [None, "http://sabre.io/ns/sync/6"])
def test_report_body_and_depth_are_exact(since):
    transport = FakeTransport([response(207, multistatus())])

    sync.changes(
        PROFILE, session=transport, href=CALENDAR, since=since, allowlist=PROFILE.calendars
    )

    assert transport.requests == [
        {
            "method": "REPORT",
            "url": CALENDAR,
            "headers": {"Depth": "0"},
            "data": (
                '<?xml version="1.0"?><d:sync-collection xmlns:d="DAV:">'
                f"<d:sync-token>{since or ''}</d:sync-token>"
                "<d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop>"
                "</d:sync-collection>"
            ).encode(),
            "timeout": None,
        }
    ]


def test_initial_report_returns_cursor_and_every_changed_href_sorted():
    first = CALENDAR + "z.ics"
    second = CALENDAR + "a.ics"
    transport = FakeTransport(
        [response(207, multistatus(changed(first, '"z"') + changed(second, '"a"')))]
    )

    result = sync.changes(
        PROFILE, session=transport, href=CALENDAR, since=None, allowlist=PROFILE.calendars
    )

    assert result == sync.Changes(
        collection=CALENDAR,
        cursor=CURSOR,
        changed=((second, '"a"'), (first, '"z"')),
        removed=(),
    )


def test_top_level_404_is_a_removal():
    href = CALENDAR + "gone.ics"
    entry = (
        f"<d:response><d:href>{href}</d:href>"
        "<d:status>HTTP/1.1 404 Not Found</d:status></d:response>"
    )
    transport = FakeTransport([response(207, multistatus(entry))])

    result = sync.changes(
        PROFILE, session=transport, href=CALENDAR, since=CURSOR, allowlist=PROFILE.calendars
    )

    assert result.removed == (href,)
    assert result.changed == ()


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (response(403, b"<s:exception>InvalidSyncToken</s:exception>"), exits.CONFLICT),
        (response(415), exits.UNSUPPORTED_COLLECTION),
        (response(207, multistatus(cursor=None)), exits.MALFORMED_RESPONSE),
        (
            response(207, multistatus(changed("/remote.php/dav/calendars/alice/other/x.ics"))),
            exits.MALFORMED_RESPONSE,
        ),
    ],
)
def test_invalid_server_answers_are_mapped(reply, code):
    transport = FakeTransport([reply])

    with pytest.raises(sync.SyncError) as error:
        sync.changes(
            PROFILE,
            session=transport,
            href=CALENDAR,
            since=CURSOR,
            allowlist=PROFILE.calendars,
        )

    assert error.value.code == code


def test_collection_outside_allowlist_is_refused_without_a_request():
    transport = FakeTransport([])

    with pytest.raises(sync.SyncError) as error:
        sync.changes(
            PROFILE,
            session=transport,
            href="/remote.php/dav/calendars/alice/private/",
            since=None,
            allowlist=PROFILE.calendars,
        )

    assert error.value.code == exits.SCOPE_DENIED
    assert transport.requests == []


def test_cal_changes_json_renders_cursor_changed_and_removed(monkeypatch, capsys):
    result = sync.Changes(
        collection=CALENDAR,
        cursor=CURSOR,
        changed=((CALENDAR + "one.ics", '"1"'),),
        removed=(CALENDAR + "gone.ics",),
    )
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: object())
    monkeypatch.setattr(cli, "_resolved_calendar", lambda profile, transport, target: CALENDAR)
    monkeypatch.setattr(cli.sync, "changes", lambda *args, **kwargs: result)

    code = cli.main(["--json", "cal", "changes", "Work"])
    shown = json.loads(capsys.readouterr().out)

    assert code == exits.OK
    assert shown == result.as_dict()
