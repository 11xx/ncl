from __future__ import annotations

import argparse
import base64
import fcntl
import html
import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path

import pytest
from test_config import write_config

from ncl import cli, exits, identity, login, render, secrets, session
from ncl.config import Profile
from ncl.session import Response, Session, SessionError

PROFILE = Profile(
    name="home",
    origin="https://cloud.example.invalid",
    secret_backend="pass",
    calendars=("/remote.php/dav/calendars/alice/",),
    files_roots=("/remote.php/dav/files/alice/work/",),
)
SENTINEL = "fixture-secret"
CREDENTIAL_REPRESENTATIONS = tuple(
    render.credential_representations("alice", SENTINEL).values()
)
SPLIT_REPRESENTATIONS = [
    (representation, split)
    for representation in CREDENTIAL_REPRESENTATIONS
    for split in range(1, len(representation))
]


@pytest.fixture(autouse=True)
def runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, headers=None, data=None, timeout=None):
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers or {}), "data": data}
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(status: int, body: bytes = b"", headers: dict[str, str] | None = None):
    return Response(status=status, headers=headers or {}, body=body, url="")


def dav_response(properties: str) -> bytes:
    return f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/ignored</d:href>
    {properties}
  </d:response>
</d:multistatus>""".encode()


def principal_response() -> Response:
    return response(
        207,
        dav_response(
            """
    <d:propstat><d:prop><d:current-user-principal><d:href>
      /remote.php/dav/principals/users/alice/
    </d:href></d:current-user-principal></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
"""
        ),
    )


def home_response() -> Response:
    return response(
        207,
        dav_response(
            """
    <d:propstat><d:prop>
      <c:calendar-home-set><d:href>/remote.php/dav/calendars/alice/</d:href></c:calendar-home-set>
      <d:displayname>Alice</d:displayname>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
"""
        ),
    )


def credential_record(login_name: str, app_password: str) -> str:
    """The exact bytes a backend holds for one profile."""
    return secrets._encode(secrets.Credential(login_name, app_password))


def credential_reader(login_name: str, app_password: str):
    """A `secrets.get` standing in for a backend holding that one credential."""
    record = credential_record(login_name, app_password)
    return lambda profile, key: record if key == secrets.RECORD_KEY else None


def stored_credential(stored: dict[str, str]) -> dict[str, str]:
    """What a fake backend's contents amount to, as plain fields."""
    raw = stored.get(secrets.RECORD_KEY)
    if raw is None:
        return {}
    decoded = secrets._decode(raw)
    return {"login_name": decoded.login_name, "app_password": decoded.app_password}


def seed_credentials(monkeypatch, values: dict[str, str] | None = None):
    stored: dict[str, str] = {}
    if values:
        stored[secrets.RECORD_KEY] = credential_record(
            values["login_name"], values["app_password"]
        )
    monkeypatch.setattr(secrets, "get", lambda profile, key: stored.get(key))
    monkeypatch.setattr(
        secrets,
        "set",
        lambda profile, key, value: stored.__setitem__(key, value),
    )
    monkeypatch.setattr(secrets, "delete", lambda profile, key: stored.pop(key, None))
    return stored


def test_session_builds_authenticated_request(monkeypatch):
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    transport = FakeTransport([response(200, b"ok")])

    result = Session(PROFILE, transport=transport).request("GET", "/remote.php/dav/")

    assert result.status == 200
    request = transport.requests[0]
    assert request["method"] == "GET"
    assert request["url"] == "https://cloud.example.invalid/remote.php/dav/"
    expected = base64.b64encode(b"alice:fixture-secret").decode()
    assert request["headers"]["Authorization"] == f"Basic {expected}"


def test_session_refuses_cross_origin_redirect_without_resending_credentials(monkeypatch):
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    transport = FakeTransport(
        [response(302, headers={"Location": "https://other.example.invalid/"})]
    )

    with pytest.raises(SessionError) as error:
        Session(PROFILE, transport=transport).request("GET", "/remote.php/dav/")

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert len(transport.requests) == 1
    assert "fixture-secret" not in str(error.value)


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [(401, {}, exits.CREDENTIAL_REJECTED), (429, {"Retry-After": "7"}, exits.THROTTLED)],
)
def test_session_refusal_paths_do_not_leak_secret(monkeypatch, status, headers, code, capsys):
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    transport = FakeTransport([response(status, headers=headers)])

    with pytest.raises(SessionError) as error:
        Session(PROFILE, transport=transport).request("GET", "/remote.php/dav/")

    assert error.value.code == code
    assert "fixture-secret" not in str(error.value)
    rendered = capsys.readouterr()
    assert "fixture-secret" not in rendered.out
    assert "fixture-secret" not in rendered.err


def test_session_parses_invalid_retry_after_without_echoing_server_text(monkeypatch):
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    server_text = "²"
    transport = FakeTransport([response(429, headers={"Retry-After": server_text})])

    with pytest.raises(SessionError) as error:
        Session(PROFILE, transport=transport).request("GET", "/remote.php/dav/")

    assert error.value.code == exits.THROTTLED
    assert "unparseable" in str(error.value)
    assert server_text not in str(error.value)


@pytest.mark.parametrize("status, code", [(429, exits.THROTTLED), (503, exits.SERVER_ERROR)])
def test_session_bounds_retry_after_before_integer_conversion(monkeypatch, status, code):
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    old_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        server_text = "9" * 650
        transport = FakeTransport([response(status, headers={"Retry-After": server_text})])

        with pytest.raises(SessionError) as error:
            Session(PROFILE, transport=transport).request("GET", "/remote.php/dav/")
    finally:
        sys.set_int_max_str_digits(old_limit)

    assert error.value.code == code
    assert "unparseable" in str(error.value)
    assert server_text not in str(error.value)


@pytest.mark.parametrize("server_text", ["https://foo℀bar/x", "https://[malformed"])
def test_session_refuses_malformed_server_location_without_echoing_text(monkeypatch, server_text):
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    transport = FakeTransport([response(302, headers={"Location": server_text})])

    with pytest.raises(SessionError) as error:
        Session(PROFILE, transport=transport).request("GET", "/remote.php/dav/")

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert server_text not in str(error.value)


def test_session_reports_parsed_http_date_retry_after(monkeypatch):
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    retry_after = format_datetime(datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC), usegmt=True)
    transport = FakeTransport([response(429, headers={"Retry-After": retry_after})])

    with pytest.raises(SessionError) as error:
        Session(PROFILE, transport=transport).request("GET", "/remote.php/dav/")

    assert error.value.code == exits.THROTTLED
    assert "2030-01-02T03:04:05+00:00" in str(error.value)
    assert retry_after not in str(error.value)


def test_session_reports_503_as_server_error_even_with_retry_after(monkeypatch):
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    transport = FakeTransport([response(503, headers={"Retry-After": "7"})])

    with pytest.raises(SessionError) as error:
        Session(PROFILE, transport=transport).request("GET", "/remote.php/dav/")

    assert error.value.code == exits.SERVER_ERROR
    assert "7 seconds" in str(error.value)


def test_session_raises_server_error_for_500(monkeypatch):
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    transport = FakeTransport([response(500)])

    with pytest.raises(SessionError) as error:
        Session(PROFILE, transport=transport).request("GET", "/remote.php/dav/")

    assert error.value.code == exits.SERVER_ERROR


def test_session_transport_failure_does_not_leak_secret(monkeypatch, capsys):
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    transport = FakeTransport([RuntimeError("transport failed: fixture-secret")])

    with pytest.raises(SessionError) as error:
        Session(PROFILE, transport=transport).request("GET", "/remote.php/dav/")

    assert "fixture-secret" not in str(error.value)
    output = capsys.readouterr()
    assert "fixture-secret" not in output.out
    assert "fixture-secret" not in output.err


def test_identity_uses_discovered_principal_when_login_name_differs(monkeypatch):
    seed_credentials(
        monkeypatch,
        {"login_name": "alice@example.invalid", "app_password": "fixture-secret"},
    )
    transport = FakeTransport([principal_response(), home_response()])

    result = identity.discover(PROFILE, transport=transport)

    assert result.account_name == "alice"
    assert result.principal_url == (
        "https://cloud.example.invalid/remote.php/dav/principals/users/alice/"
    )
    assert result.calendar_home == "https://cloud.example.invalid/remote.php/dav/calendars/alice/"
    assert transport.requests[0]["url"] == "https://cloud.example.invalid/remote.php/dav/"
    assert transport.requests[1]["url"] == result.principal_url


def test_identity_rejects_needed_property_with_404_propstat(monkeypatch):
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    broken_home = response(
        207,
        dav_response(
            """
    <d:propstat><d:prop><c:calendar-home-set/></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
    <d:propstat><d:prop><d:displayname>Alice</d:displayname></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
"""
        ),
    )
    transport = FakeTransport([principal_response(), broken_home])

    with pytest.raises(identity.IdentityError) as error:
        identity.discover(PROFILE, transport=transport)

    assert error.value.code == exits.MALFORMED_RESPONSE


def test_login_rejects_off_origin_urls_before_opening_browser(monkeypatch):
    seed_credentials(monkeypatch)
    monkeypatch.setattr(secrets, "probe", lambda profile: True)
    monkeypatch.setattr(secrets, "has_credential", lambda profile: False)
    opened = []
    transport = FakeTransport(
        [
            response(
                200,
                json.dumps(
                    {
                        "login": "https://other.example.invalid/login",
                        "poll": {
                            "endpoint": "https://cloud.example.invalid/poll",
                            "token": "poll-token",
                        },
                    }
                ).encode(),
            )
        ]
    )

    with pytest.raises(login.LoginError) as error:
        login.authenticate(
            PROFILE,
            transport=transport,
            browser_open=opened.append,
        )

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert opened == []


@pytest.mark.parametrize(
    ("poll_payload", "expected_fragment"),
    [
        (
            {"loginName": "alice", "appPassword": "fixture-secret"},
            "omitted server",
        ),
        (
            {
                "server": "https://other.example.invalid",
                "loginName": "alice",
                "appPassword": "fixture-secret",
            },
            "server URL was refused",
        ),
        (
            {
                "server": "https://[malformed",
                "loginName": "alice",
                "appPassword": "fixture-secret",
            },
            "server URL was refused",
        ),
        (
            {
                "server": "https://cloud.example.invalid",
                "appPassword": "fixture-secret",
            },
            "could not be stored",
        ),
    ],
)
def test_login_reports_issued_credential_when_post_consent_validation_fails(
    monkeypatch, poll_payload, expected_fragment
):
    seed_credentials(monkeypatch)
    monkeypatch.setattr(secrets, "probe", lambda profile: True)
    monkeypatch.setattr(secrets, "has_credential", lambda profile: False)
    transport = FakeTransport(
        [
            response(
                200,
                json.dumps(
                    {
                        "login": "https://cloud.example.invalid/login",
                        "poll": {
                            "endpoint": "https://cloud.example.invalid/poll",
                            "token": "poll-token",
                        },
                    }
                ).encode(),
            ),
            response(200, json.dumps(poll_payload).encode()),
        ]
    )

    with pytest.raises(login.LoginError) as error:
        login.authenticate(PROFILE, transport=transport, browser_open=lambda url: True)

    assert error.value.code == exits.CREDENTIAL_STORE_FAILED
    assert "consent succeeded" in str(error.value)
    assert "application password" in str(error.value)
    assert "Security settings" in str(error.value)
    assert expected_fragment in str(error.value)
    assert "fixture-secret" not in str(error.value)


def test_login_treats_unparseable_http_200_as_issued_credential(monkeypatch, capsys):
    stored = seed_credentials(monkeypatch)
    monkeypatch.setattr(secrets, "probe", lambda profile: True)
    monkeypatch.setattr(secrets, "has_credential", lambda profile: False)
    transport = FakeTransport(
        [
            response(
                200,
                json.dumps(
                    {
                        "login": "https://cloud.example.invalid/login",
                        "poll": {
                            "endpoint": "https://cloud.example.invalid/poll",
                            "token": "poll-token",
                        },
                    }
                ).encode(),
            ),
            response(200, b'{"server":"https://cloud.example.invalid",'),
        ]
    )

    with pytest.raises(login.LoginError) as error:
        login.authenticate(PROFILE, transport=transport, browser_open=lambda url: True)

    assert error.value.code == exits.CREDENTIAL_STORE_FAILED
    assert stored == {}
    assert cli._error(error.value, True) == exits.CREDENTIAL_STORE_FAILED
    rendered = capsys.readouterr()
    assert "orphaned" in rendered.out
    assert "Security settings" in rendered.out


@pytest.mark.parametrize(
    ("endpoint", "token", "server_text"),
    [
        ("https://cloud.example.invalid/poll", "\ud800", "\ud800"),
        ("https://cloud.example.invalid/poll?state=\ud800", "poll-token", "\ud800"),
    ],
)
def test_login_refuses_unencodable_poll_request_values(
    monkeypatch, capsys, endpoint, token, server_text
):
    seed_credentials(monkeypatch)
    monkeypatch.setattr(secrets, "probe", lambda profile: True)
    monkeypatch.setattr(secrets, "has_credential", lambda profile: False)
    transport = FakeTransport(
        [
            response(
                200,
                json.dumps(
                    {
                        "login": "https://cloud.example.invalid/login",
                        "poll": {"endpoint": endpoint, "token": token},
                    }
                ).encode(),
            )
        ]
    )

    with pytest.raises(login.LoginError) as error:
        login.authenticate(PROFILE, transport=transport, browser_open=lambda url: True)

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert cli._error(error.value, True) == exits.MALFORMED_RESPONSE
    rendered = capsys.readouterr()
    assert server_text not in rendered.out + rendered.err


def test_server_supplied_text_never_reaches_rendered_messages(monkeypatch, capsys):
    location_text = "https://foo℀bar/x"
    retry_text = "²"
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    location_transport = FakeTransport([response(302, headers={"Location": location_text})])
    with pytest.raises(SessionError) as session_error:
        Session(PROFILE, transport=location_transport).request("GET", "/remote.php/dav/")

    retry_transport = FakeTransport([response(429, headers={"Retry-After": retry_text})])
    with pytest.raises(SessionError) as retry_error:
        Session(PROFILE, transport=retry_transport).request("GET", "/remote.php/dav/")

    monkeypatch.setattr(secrets, "probe", lambda profile: True)
    monkeypatch.setattr(secrets, "has_credential", lambda profile: False)
    login_transport = FakeTransport(
        [
            response(
                200,
                json.dumps(
                    {
                        "login": "https://cloud.example.invalid/login",
                        "poll": {
                            "endpoint": "https://cloud.example.invalid/poll",
                            "token": "poll-token",
                        },
                    }
                ).encode(),
            ),
            response(
                200,
                json.dumps(
                    {
                        "server": location_text,
                        "loginName": "alice",
                        "appPassword": "fixture-secret",
                    }
                ).encode(),
            ),
        ]
    )
    with pytest.raises(login.LoginError) as login_error:
        login.authenticate(PROFILE, transport=login_transport, browser_open=lambda url: True)

    assert login_error.value.code == exits.CREDENTIAL_STORE_FAILED
    for error, server_text in (
        (session_error.value, location_text),
        (retry_error.value, retry_text),
        (login_error.value, location_text),
    ):
        for json_output in (False, True):
            assert cli._error(error, json_output) == error.code
            rendered = capsys.readouterr()
            assert server_text not in rendered.out + rendered.err


def test_login_polls_404_then_stores_once_and_confirms_identity(monkeypatch):
    stored = seed_credentials(monkeypatch)
    monkeypatch.setattr(secrets, "probe", lambda profile: True)
    store_calls = []
    original_store = secrets.store_credential

    def record_store(profile, login_name, app_password):
        store_calls.append((profile, login_name, app_password))
        return original_store(profile, login_name, app_password)

    monkeypatch.setattr(secrets, "store_credential", record_store)
    transport = FakeTransport(
        [
            response(
                200,
                json.dumps(
                    {
                        "login": "https://cloud.example.invalid/login",
                        "poll": {
                            "endpoint": "https://cloud.example.invalid/poll",
                            "token": "poll-token",
                        },
                    }
                ).encode(),
            ),
            response(404),
            response(
                200,
                json.dumps(
                    {
                        "server": "https://cloud.example.invalid",
                        "loginName": "alice@example.invalid",
                        "appPassword": "fixture-secret",
                    }
                ).encode(),
            ),
            principal_response(),
            home_response(),
        ]
    )
    output = []

    result = login.authenticate(
        PROFILE,
        transport=transport,
        browser_open=lambda url: True,
        sleep=lambda seconds: None,
        output=output.append,
    )

    assert result.account_name == "alice"
    assert stored_credential(stored) == {
        "login_name": "alice@example.invalid",
        "app_password": "fixture-secret",
    }
    assert len(store_calls) == 1
    assert [request["method"] for request in transport.requests] == [
        "POST",
        "POST",
        "POST",
        "PROPFIND",
        "PROPFIND",
    ]
    # The token is form-encoded POST data, never a query parameter on a GET.
    # The server answers a GET, or a POST without the body, with 400, which is
    # indistinguishable from a real protocol error — so asserting the wire form
    # here is what keeps login working against the actual endpoint.
    for poll in transport.requests[1:3]:
        assert poll["data"] == b"token=poll-token"
        assert "token=" not in poll["url"]
        assert poll["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert output[0].startswith("Open this URL")


def test_login_store_failure_reports_orphaned_application_password(monkeypatch):
    monkeypatch.setattr(secrets, "probe", lambda profile: True)
    monkeypatch.setattr(secrets, "has_credential", lambda profile: False)
    monkeypatch.setattr(
        secrets,
        "store_credential",
        lambda profile, login_name, app_password: (_ for _ in ()).throw(
            secrets.SecretError("store failed")
        ),
    )
    transport = FakeTransport(
        [
            response(
                200,
                json.dumps(
                    {
                        "login": "https://cloud.example.invalid/login",
                        "poll": {
                            "endpoint": "https://cloud.example.invalid/poll",
                            "token": "poll-token",
                        },
                    }
                ).encode(),
            ),
            response(
                200,
                json.dumps(
                    {
                        "server": "https://cloud.example.invalid",
                        "loginName": "alice",
                        "appPassword": "fixture-secret",
                    }
                ).encode(),
            ),
        ]
    )

    with pytest.raises(login.LoginError) as error:
        login.authenticate(PROFILE, transport=transport, browser_open=lambda url: True)

    assert error.value.code == exits.CREDENTIAL_STORE_FAILED
    assert "orphaned" in str(error.value)
    assert "Security settings" in str(error.value)
    assert "fixture-secret" not in str(error.value)
    assert len(transport.requests) == 2


def test_second_login_refuses_and_points_to_logout(monkeypatch):
    monkeypatch.setattr(secrets, "probe", lambda profile: True)
    monkeypatch.setattr(secrets, "has_credential", lambda profile: True)
    transport = FakeTransport([])

    with pytest.raises(login.LoginError) as error:
        login.authenticate(PROFILE, transport=transport)

    assert error.value.code == exits.CONFLICT
    assert "ncl logout" in str(error.value)
    assert transport.requests == []


def test_force_login_revokes_existing_credential_before_starting_flow(monkeypatch):
    stored = seed_credentials(
        monkeypatch,
        {"login_name": "old-alice", "app_password": "old-secret"},
    )
    monkeypatch.setattr(secrets, "probe", lambda profile: True)
    revoked = []

    def record_logout(profile, *, transport):
        revoked.append((profile, transport))

    monkeypatch.setattr(login, "logout", record_logout)
    output = []
    transport = FakeTransport(
        [
            response(
                200,
                json.dumps(
                    {
                        "login": "https://cloud.example.invalid/login",
                        "poll": {
                            "endpoint": "https://cloud.example.invalid/poll",
                            "token": "poll-token",
                        },
                    }
                ).encode(),
            ),
            response(
                200,
                json.dumps(
                    {
                        "server": "https://cloud.example.invalid",
                        "loginName": "alice",
                        "appPassword": "fixture-secret",
                    }
                ).encode(),
            ),
            principal_response(),
            home_response(),
        ]
    )

    login.authenticate(
        PROFILE,
        force=True,
        transport=transport,
        browser_open=lambda url: True,
        output=output.append,
    )

    assert revoked == [(PROFILE, transport)]
    assert stored_credential(stored) == {"login_name": "alice", "app_password": "fixture-secret"}
    assert any("revoked before browser consent" in message for message in output)
    assert any("without a credential" in message for message in output)


def test_force_login_help_describes_preconsent_revocation():
    parser = cli.build_parser()
    login_parser = next(
        action.choices["login"]
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    help_text = " ".join(login_parser.format_help().split())

    assert "before browser consent" in help_text
    assert "without a credential" in help_text


def test_force_login_refuses_when_existing_credential_cannot_be_revoked(monkeypatch):
    seed_credentials(
        monkeypatch,
        {"login_name": "old-alice", "app_password": "old-secret"},
    )
    monkeypatch.setattr(secrets, "probe", lambda profile: True)

    def refuse_logout(profile, *, transport):
        raise login.LoginError("revocation failed", exits.REVOCATION_FAILED)

    monkeypatch.setattr(login, "logout", refuse_logout)
    transport = FakeTransport([])

    with pytest.raises(login.LoginError) as error:
        login.authenticate(PROFILE, force=True, transport=transport, browser_open=lambda url: True)

    assert error.value.code == exits.REVOCATION_FAILED
    assert "refusing" in str(error.value)
    assert "Security settings" in str(error.value)
    assert transport.requests == []


@pytest.mark.parametrize("backend", [secrets.PassBackend(), secrets.LibsecretBackend()])
def test_secret_backends_send_values_on_stdin_not_in_argv(monkeypatch, backend):
    calls = []

    def fake_run(command, *, input_text=None):
        calls.append((command, input_text))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(secrets, "_run", fake_run)

    backend.set(PROFILE, "app_password", "fixture-secret")

    assert calls
    assert "fixture-secret" not in " ".join(calls[0][0])
    assert calls[0][1] == "fixture-secret\n"


@pytest.mark.parametrize(
    ("backend", "missing_result", "failure_result"),
    [
        (
            secrets.PassBackend(),
            subprocess.CompletedProcess(
                ["pass"], 1, "", "Error: ncl/home/app_password is not in the password store.\n"
            ),
            subprocess.CompletedProcess(["pass"], 1, "", "gpg: decryption failed\n"),
        ),
        (
            secrets.LibsecretBackend(),
            subprocess.CompletedProcess(["secret-tool"], 0, "", ""),
            subprocess.CompletedProcess(["secret-tool"], 1, "", "secret service unavailable\n"),
        ),
    ],
)
def test_secret_backend_reads_distinguish_absent_from_unusable(
    monkeypatch, backend, missing_result, failure_result
):
    monkeypatch.setattr(secrets, "_run", lambda command, **kwargs: missing_result)
    assert backend.get(PROFILE, "app_password") is None

    monkeypatch.setattr(secrets, "_run", lambda command, **kwargs: failure_result)
    with pytest.raises(secrets.SecretError):
        backend.get(PROFILE, "app_password")


@pytest.mark.parametrize(
    ("backend", "found_result"),
    [
        (
            secrets.PassBackend(),
            subprocess.CompletedProcess(["pass"], 0, "stored-secret\n", ""),
        ),
        (
            secrets.LibsecretBackend(),
            subprocess.CompletedProcess(["secret-tool"], 0, "stored-secret\n", ""),
        ),
    ],
)
def test_secret_backend_reads_return_found_value(monkeypatch, backend, found_result):
    monkeypatch.setattr(secrets, "_run", lambda command, **kwargs: found_result)

    assert backend.get(PROFILE, "app_password") == "stored-secret"


_LOCK_WORKER = """
import fcntl
import os
import sys
from pathlib import Path

from ncl import exits, login
from ncl.config import Profile

mode = sys.argv[1]
if mode == "hold":
    path = Path(sys.argv[2])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.ftruncate(descriptor, 0)
    os.write(descriptor, b"2147483647\\n0\\n")
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    print("holding", flush=True)
    sys.stdin.read(1)
    os.close(descriptor)
    raise SystemExit(exits.OK)

profile = Profile(
    name=sys.argv[2],
    origin="https://cloud.example.invalid",
    secret_backend="pass",
    calendars=(),
    files_roots=(),
)
try:
    with login._profile_lock(profile):
        raise SystemExit(exits.OK)
except login.LoginError as error:
    raise SystemExit(error.code) from error
"""


def _attempt_profile_lock_in_subprocess() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _LOCK_WORKER, "attempt", PROFILE.name],
        capture_output=True,
        text=True,
        check=False,
    )


def test_kernel_lock_owner_cannot_be_bypassed_by_stale_file_contents():
    lock_path = login._lock_path(PROFILE)
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_WORKER, "hold", str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline() == "holding\n"

    try:
        contender = _attempt_profile_lock_in_subprocess()
    finally:
        assert holder.stdin is not None
        holder.stdin.write("x")
        holder.stdin.flush()
        holder_output, holder_error = holder.communicate(timeout=5)

    assert holder.returncode == exits.OK, holder_output + holder_error
    assert contender.returncode == exits.LOCKED, contender.stdout + contender.stderr


def test_login_lock_contention_is_reported_before_preflight(monkeypatch):
    descriptor = os.open(login._lock_path(PROFILE), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(secrets, "probe", lambda profile: pytest.fail("preflight ran"))

    try:
        with pytest.raises(login.LoginError) as error:
            login.authenticate(PROFILE, browser_open=lambda url: True)
    finally:
        os.close(descriptor)

    assert error.value.code == exits.LOCKED


def test_login_holds_profile_lock_through_principal_verification(monkeypatch):
    transport = FakeTransport(
        [
            response(
                200,
                json.dumps(
                    {
                        "login": "https://cloud.example.invalid/login",
                        "poll": {
                            "endpoint": "https://cloud.example.invalid/poll",
                            "token": "poll-token",
                        },
                    }
                ).encode(),
            ),
            response(
                200,
                json.dumps(
                    {
                        "server": "https://cloud.example.invalid",
                        "loginName": "alice",
                        "appPassword": SENTINEL,
                    }
                ).encode(),
            ),
        ]
    )
    seed_credentials(monkeypatch)
    monkeypatch.setattr(secrets, "probe", lambda profile: True)
    attempts = []

    def discover(profile, *, session):
        attempts.append(_attempt_profile_lock_in_subprocess())
        return identity.Identity(
            principal_url="https://cloud.example.invalid/remote.php/dav/principals/users/alice/",
            account_name="alice",
            display_name="Alice",
            calendar_home="https://cloud.example.invalid/remote.php/dav/calendars/alice/",
        )

    monkeypatch.setattr(identity, "discover", discover)

    login.authenticate(
        PROFILE,
        transport=transport,
        browser_open=lambda url: True,
    )

    assert len(attempts) == 1
    assert attempts[0].returncode == exits.LOCKED, attempts[0].stdout + attempts[0].stderr


def test_cli_replaces_untyped_exception_text_with_catalogued_message(capsys):
    assert cli._error(ValueError(SENTINEL), False) == exits.ERROR
    rendered = capsys.readouterr()
    assert SENTINEL not in rendered.err
    assert "Unexpected failure" in rendered.err


def _register_credential_representations(monkeypatch) -> None:
    monkeypatch.setattr(render, "_SECRETS", set(CREDENTIAL_REPRESENTATIONS))


@pytest.mark.parametrize(
    ("representation", "split"),
    SPLIT_REPRESENTATIONS,
    ids=[
        f"representation-{index}-split-{split}"
        for index, representation in enumerate(CREDENTIAL_REPRESENTATIONS)
        for split in range(1, len(representation))
    ],
)
def test_standard_stream_redacts_representation_split_across_writes(
    monkeypatch, representation, split
):
    output = io.StringIO()
    _register_credential_representations(monkeypatch)
    monkeypatch.setattr(sys, "stdout", output)

    with render.redacted_standard_streams():
        sys.stdout.write(representation[:split])
        sys.stdout.write(representation[split:])

    assert representation not in output.getvalue()
    assert "[redacted]" in output.getvalue()


@pytest.mark.parametrize(
    ("representation", "split"),
    SPLIT_REPRESENTATIONS,
    ids=[
        f"representation-{index}-split-{split}"
        for index, representation in enumerate(CREDENTIAL_REPRESENTATIONS)
        for split in range(1, len(representation))
    ],
)
def test_standard_stream_redacts_representation_split_across_writelines(
    monkeypatch, representation, split
):
    output = io.StringIO()
    _register_credential_representations(monkeypatch)
    monkeypatch.setattr(sys, "stdout", output)

    with render.redacted_standard_streams():
        assert sys.stdout.writelines((representation[:split], representation[split:])) is None

    assert representation not in output.getvalue()
    assert "[redacted]" in output.getvalue()


@pytest.mark.parametrize(
    ("representation", "split"),
    SPLIT_REPRESENTATIONS,
    ids=[
        f"representation-{index}-split-{split}"
        for index, representation in enumerate(CREDENTIAL_REPRESENTATIONS)
        for split in range(1, len(representation))
    ],
)
def test_standard_stream_redacts_representation_split_across_mixed_writes(
    monkeypatch, representation, split
):
    output = io.StringIO()
    _register_credential_representations(monkeypatch)
    monkeypatch.setattr(sys, "stdout", output)

    with render.redacted_standard_streams():
        sys.stdout.write(representation[:split])
        assert sys.stdout.writelines((representation[split:],)) is None

    assert representation not in output.getvalue()
    assert "[redacted]" in output.getvalue()


def test_standard_stream_flush_does_not_leak_split_secret(monkeypatch):
    output = io.StringIO()
    _register_credential_representations(monkeypatch)
    monkeypatch.setattr(sys, "stdout", output)
    split = 1

    with render.redacted_standard_streams():
        sys.stdout.write(SENTINEL[:split])
        sys.stdout.flush()
        sys.stdout.write(SENTINEL[split:])

    assert SENTINEL not in output.getvalue()
    assert "[redacted]" in output.getvalue()


def test_standard_stream_preserves_non_secret_text_ending_in_secret_prefix(monkeypatch):
    output = io.StringIO()
    _register_credential_representations(monkeypatch)
    monkeypatch.setattr(sys, "stdout", output)
    value = f"ordinary text {SENTINEL[:-1]}"

    with render.redacted_standard_streams():
        sys.stdout.write(value)

    assert output.getvalue() == value


def test_standard_stream_write_returns_consumed_argument_length(monkeypatch):
    output = io.StringIO()
    _register_credential_representations(monkeypatch)
    monkeypatch.setattr(sys, "stdout", output)

    with render.redacted_standard_streams():
        assert sys.stdout.write(SENTINEL) == len(SENTINEL)

    assert output.getvalue() == "[redacted]"


def test_standard_stream_redacts_secret_split_across_three_writes(monkeypatch):
    output = io.StringIO()
    _register_credential_representations(monkeypatch)
    monkeypatch.setattr(sys, "stdout", output)

    with render.redacted_standard_streams():
        sys.stdout.write(SENTINEL[:1])
        sys.stdout.write(SENTINEL[1:-1])
        sys.stdout.write(SENTINEL[-1:])

    assert SENTINEL not in output.getvalue()
    assert output.getvalue() == "[redacted]"


def _echo_principal_response() -> Response:
    return response(
        207,
        dav_response(
            f"""
    <d:propstat><d:prop><d:current-user-principal><d:href>
      /remote.php/dav/principals/users/{SENTINEL}/
    </d:href></d:current-user-principal></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
"""
        ),
        headers={"X-Echo": SENTINEL},
    )


def _echo_home_response() -> Response:
    return response(
        207,
        dav_response(
            f"""
    <d:propstat><d:prop>
      <c:calendar-home-set><d:href>/remote.php/dav/calendars/{SENTINEL}/</d:href></c:calendar-home-set>
      <d:displayname>{SENTINEL}</d:displayname>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
"""
        ),
        headers={"X-Echo": SENTINEL},
    )


def _display_name_response(value: str) -> Response:
    return response(
        207,
        dav_response(
            f"""
    <d:propstat><d:prop>
      <c:calendar-home-set><d:href>/remote.php/dav/calendars/alice/</d:href></c:calendar-home-set>
      <d:displayname>{html.escape(value)}</d:displayname>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
"""
        ),
    )


def test_whoami_redacts_every_registered_credential_representation(
    monkeypatch, tmp_path, capsys
):
    config_path = write_config(tmp_path)
    monkeypatch.setenv("NCL_CONFIG", str(config_path))
    seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": SENTINEL},
    )
    monkeypatch.setattr(render, "_SECRETS", set())
    encoded = base64.b64encode(f"alice:{SENTINEL}".encode()).decode("ascii")
    required = {SENTINEL, f"alice:{SENTINEL}", encoded, f"Basic {encoded}"}
    representations = tuple(render.credential_representations("alice", SENTINEL).values())

    assert set(representations) >= required
    for representation in representations:
        monkeypatch.setattr(
            session,
            "UrllibTransport",
            lambda value=representation: FakeTransport(
                [principal_response(), _display_name_response(value)]
            ),
        )

        assert cli.main(["whoami", "--json"]) == exits.OK
        rendered = capsys.readouterr()
        assert representation not in rendered.out + rendered.err
        assert "[redacted]" in rendered.out


@pytest.mark.parametrize("json_output", [False, True])
def test_public_cli_auth_commands_redact_registered_credential(
    monkeypatch, tmp_path, capsys, json_output
):
    config_path = write_config(tmp_path)
    monkeypatch.setenv("NCL_CONFIG", str(config_path))
    stored = seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": SENTINEL},
    )
    monkeypatch.setattr(render, "_SECRETS", set())
    monkeypatch.setattr(secrets, "probe", lambda profile: True)
    from test_doctor import fixture_backend

    fixture_backend(monkeypatch)
    # This drives the real `login` command, which would otherwise open a tab in
    # whoever is running the suite. The conftest guard turns that into a
    # failure; recording the URL is what the test actually needs.
    opened: list[str] = []
    monkeypatch.setattr(login.webbrowser, "open", opened.append)

    def invoke(command, transport, *, has_credentials=True):
        monkeypatch.setattr(session, "UrllibTransport", lambda: transport)
        monkeypatch.setattr(login, "UrllibTransport", lambda: transport)
        monkeypatch.setattr(secrets, "has_credential", lambda profile: has_credentials)
        args = [command]
        if json_output:
            args.append("--json")
        cli.main(args)
        rendered = capsys.readouterr()
        assert SENTINEL not in rendered.out + rendered.err

    invoke("whoami", FakeTransport([_echo_principal_response(), _echo_home_response()]))
    invoke(
        "doctor",
        FakeTransport(
            [
                _echo_principal_response(),
                _echo_home_response(),
                response(207, headers={"X-Echo": SENTINEL}),
                response(207, headers={"X-Echo": SENTINEL}),
            ]
        ),
    )

    stored.clear()
    monkeypatch.setattr(
        secrets,
        "store_credential",
        lambda profile, login_name, app_password: stored.update(
            login_name=login_name, app_password=app_password
        ),
    )
    invoke(
        "login",
        FakeTransport(
            [
                response(
                    200,
                    json.dumps(
                        {
                            "login": "https://cloud.example.invalid/login",
                            "poll": {
                                "endpoint": "https://cloud.example.invalid/poll",
                                "token": "poll-token",
                            },
                        }
                    ).encode(),
                ),
                response(
                    200,
                    json.dumps(
                        {
                            "server": "https://cloud.example.invalid",
                            "loginName": "alice",
                            "appPassword": SENTINEL,
                        }
                    ).encode(),
                ),
                _echo_principal_response(),
                _echo_home_response(),
            ]
        ),
        has_credentials=False,
    )

    stored.update(login_name="alice", app_password=SENTINEL)
    invoke(
        "logout",
        FakeTransport(
            [
                response(
                    500,
                    SENTINEL.encode(),
                    headers={"X-Echo": SENTINEL, "Retry-After": SENTINEL},
                )
            ]
        ),
    )


def test_cli_help_passes_through_redacting_stream(monkeypatch):
    writes = []
    original_redact = render._redact_text

    def track_write(value):
        writes.append(value)
        return original_redact(value)

    def reject_emit(*args, **kwargs):
        raise AssertionError("argparse help must not depend on render.emit")

    output = io.StringIO()
    monkeypatch.setattr(render, "_redact_text", track_write)
    monkeypatch.setattr(render, "emit", reject_emit)
    monkeypatch.setattr(sys, "stdout", output)

    with pytest.raises(SystemExit) as error:
        cli.main(["--help"])

    assert error.value.code == exits.OK
    assert any("Usage:" in value for value in writes)
    # Help leads with what the tool is, then the usage line — the shape the
    # sibling tools in this family use, rather than argparse's default.
    rendered = output.getvalue()
    assert rendered.startswith("ncl — ")
    assert "\nUsage: ncl " in rendered


def test_output_call_sites_keep_secondary_source_scan():
    """Keep direct writers visible as a review signal, not the output enforcement."""
    source_root = Path(__file__).parents[1] / "src" / "ncl"
    violations = []
    for path in source_root.rglob("*.py"):
        if path.name == "render.py":
            continue
        text = path.read_text()
        patterns = (
            r"\bprint\(",
            r"sys\.stdout",
            r"sys\.stderr",
            r"(?:stdout|stderr)\.write\(",
        )
        for pattern in patterns:
            if __import__("re").search(pattern, text):
                violations.append(f"{path}: {pattern}")
    assert not violations


def test_logout_removes_local_credential_when_server_revocation_fails(monkeypatch):
    stored = seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    transport = FakeTransport([response(500)])

    with pytest.raises(login.LoginError) as error:
        login.logout(PROFILE, transport=transport)

    assert error.value.code == exits.REVOCATION_FAILED
    assert stored == {}
    assert "Security settings" in str(error.value)
    assert transport.requests[0]["method"] == "DELETE"


@pytest.mark.parametrize(
    "body",
    [
        json.dumps(
            {
                "ocs": {
                    "meta": {"status": "failure", "statuscode": 997, "message": "failed"}
                }
            }
        ).encode(),
        b"not-json",
    ],
)
def test_logout_requires_successful_ocs_revocation_envelope(monkeypatch, body):
    stored = seed_credentials(
        monkeypatch,
        {"login_name": "alice", "app_password": "fixture-secret"},
    )
    transport = FakeTransport([response(200, body)])

    with pytest.raises(login.LoginError) as error:
        login.logout(PROFILE, transport=transport)

    assert error.value.code == exits.REVOCATION_FAILED
    assert stored == {}


def test_standard_stream_redacts_a_secret_completed_by_the_marker_seam(monkeypatch):
    """Replacing one secret must not assemble another out of the marker.

    `[redacted]` ends in a bracket, so a registered value beginning with one can
    be completed by the marker that replaced its neighbour.
    """
    output = io.StringIO()
    monkeypatch.setattr(render, "_SECRETS", {"ab", "]XYZ"})
    monkeypatch.setattr(sys, "stdout", output)

    with render.redacted_standard_streams():
        sys.stdout.write("a")
        sys.stdout.write("bXYZ")
        sys.stdout.write("abXYZ")

    assert "]XYZ" not in output.getvalue()
    assert "ab" not in output.getvalue()


@pytest.mark.parametrize(
    ("representation", "split"), SPLIT_REPRESENTATIONS, ids=str
)
def test_standard_stream_flush_between_halves_never_leaks(monkeypatch, representation, split):
    output = io.StringIO()
    _register_credential_representations(monkeypatch)
    monkeypatch.setattr(sys, "stdout", output)

    with render.redacted_standard_streams():
        sys.stdout.write(representation[:split])
        sys.stdout.flush()
        sys.stdout.write(representation[split:])

    rendered = output.getvalue()
    assert not any(value in rendered for value in CREDENTIAL_REPRESENTATIONS)
    assert "[redacted]" in rendered


@pytest.mark.parametrize(
    ("representation", "split"), SPLIT_REPRESENTATIONS, ids=str
)
def test_standard_error_stream_redacts_across_write_boundaries(
    monkeypatch, representation, split
):
    output = io.StringIO()
    _register_credential_representations(monkeypatch)
    monkeypatch.setattr(sys, "stderr", output)

    with render.redacted_standard_streams():
        sys.stderr.write(representation[:split])
        sys.stderr.write(representation[split:])

    rendered = output.getvalue()
    assert not any(value in rendered for value in CREDENTIAL_REPRESENTATIONS)
    assert "[redacted]" in rendered


def test_pending_text_is_released_and_streams_restored_when_the_body_raises(monkeypatch):
    output = io.StringIO()
    _register_credential_representations(monkeypatch)
    original = sys.stdout
    monkeypatch.setattr(sys, "stdout", output)

    with pytest.raises(ValueError), render.redacted_standard_streams():
        sys.stdout.write(f"kept {SENTINEL[:-1]}")
        raise ValueError("body")

    assert output.getvalue() == f"kept {SENTINEL[:-1]}"
    assert sys.stdout is output
    assert sys.stdout is not original


def test_streams_are_restored_even_when_the_final_drain_fails(monkeypatch):
    class _Refusing(io.StringIO):
        def write(self, value):
            raise OSError("stream closed")

    _register_credential_representations(monkeypatch)
    refusing = _Refusing()
    monkeypatch.setattr(sys, "stdout", refusing)

    with pytest.raises(OSError), render.redacted_standard_streams():
        sys.stdout.write(SENTINEL[:-1])

    assert sys.stdout is refusing


def test_a_registered_set_that_never_settles_suppresses_the_text_entirely(monkeypatch):
    """Failing closed beats emitting the part of a value that did settle."""
    output = io.StringIO()
    # Each replacement rebuilds a match out of the marker's own characters, so
    # the text never reaches a fixed point.
    monkeypatch.setattr(render, "_SECRETS", {"q", "ted]"})
    monkeypatch.setattr(sys, "stdout", output)

    with render.redacted_standard_streams():
        sys.stdout.write("q")

    assert "ted]" not in output.getvalue()
    assert output.getvalue() == ""


def test_a_real_credential_set_settles_without_suppressing_ordinary_text(monkeypatch):
    output = io.StringIO()
    _register_credential_representations(monkeypatch)
    monkeypatch.setattr(sys, "stdout", output)

    with render.redacted_standard_streams():
        sys.stdout.write(f"profile alice used {SENTINEL} just now")

    assert output.getvalue() == "profile alice used [redacted] just now"
