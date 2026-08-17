from __future__ import annotations

import base64
import json
import subprocess

import pytest

from ncl import exits, identity, login, secrets
from ncl.config import Profile
from ncl.session import Response, Session, SessionError

PROFILE = Profile(
    name="home",
    origin="https://cloud.example.invalid",
    secret_backend="pass",
    calendars=("/remote.php/dav/calendars/alice/",),
    files_roots=("/remote.php/dav/files/alice/work/",),
)


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


def seed_credentials(monkeypatch, values: dict[str, str] | None = None):
    stored = dict(values or {})
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
    assert "fixture-secret" not in capsys.readouterr().out
    assert "fixture-secret" not in capsys.readouterr().err


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
    monkeypatch.setattr(secrets, "has_credentials", lambda profile: False)
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


def test_login_polls_404_then_stores_once_and_confirms_identity(monkeypatch):
    stored = seed_credentials(monkeypatch)
    monkeypatch.setattr(secrets, "probe", lambda profile: True)
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
    assert stored == {
        "login_name": "alice@example.invalid",
        "app_password": "fixture-secret",
    }
    assert [request["method"] for request in transport.requests] == [
        "POST",
        "GET",
        "GET",
        "PROPFIND",
        "PROPFIND",
    ]
    assert "token=poll-token" in transport.requests[2]["url"]
    assert output[0].startswith("Open this URL")


def test_login_store_failure_reports_orphaned_application_password(monkeypatch):
    monkeypatch.setattr(secrets, "probe", lambda profile: True)
    monkeypatch.setattr(secrets, "has_credentials", lambda profile: False)
    monkeypatch.setattr(
        secrets,
        "store_credentials",
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
    monkeypatch.setattr(secrets, "has_credentials", lambda profile: True)
    transport = FakeTransport([])

    with pytest.raises(login.LoginError) as error:
        login.authenticate(PROFILE, transport=transport)

    assert error.value.code == exits.CONFLICT
    assert "ncl logout" in str(error.value)
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
    assert "fixture-secret" not in calls[0][0]
    assert calls[0][1] == "fixture-secret\n"


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
