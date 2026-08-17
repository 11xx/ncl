from __future__ import annotations

import json

import pytest
from test_auth import FakeTransport, home_response, principal_response, response
from test_config import VALID, write_config

from ncl import checks, cli, exits, secrets


def test_broken_config_fails_doctor(tmp_path):
    path = write_config(tmp_path, VALID + "unexpected = true\n")

    report = checks.run(path)

    assert report.exit_code == exits.PRECONDITION_FAILED
    assert any(check.name == "config" and check.status == "fail" for check in report.checks)


def test_good_config_passes_without_backend_or_server(monkeypatch, tmp_path):
    path = write_config(tmp_path)
    monkeypatch.setattr(checks, "probe_pass", lambda: (True, "fixture pass"))
    monkeypatch.setattr(checks, "probe_libsecret", lambda: (False, "must be skipped"))

    report = checks.run(path)

    assert report.exit_code == exits.OK
    assert all(check.status != "fail" for check in report.checks)
    assert any(
        check.name == "secret-backend:home:libsecret" and check.status == "skip"
        for check in report.checks
    )
    assert all(
        check.status == "skip"
        for check in report.checks
        if check.name.startswith(("credential:", "principal:", "calendar-home:", "remote:"))
    )


def test_doctor_authenticated_checks_resolve_identity_and_allowlists(monkeypatch, tmp_path):
    path = write_config(tmp_path)
    monkeypatch.setattr(checks, "probe_pass", lambda: (True, "fixture pass"))
    stored = {"login_name": "alice@example.invalid", "app_password": "fixture-secret"}
    monkeypatch.setattr(secrets, "get", lambda profile, key: stored.get(key))
    transport = FakeTransport(
        [
            principal_response(),
            home_response(),
            response(207),
            response(207),
        ]
    )

    report = checks.run(path, transport=transport)

    assert report.exit_code == exits.OK
    statuses = {check.name: check.status for check in report.checks}
    assert statuses["credential:home"] == "pass"
    assert statuses["principal:home"] == "pass"
    assert statuses["calendar-home:home"] == "pass"
    assert statuses["remote:home:calendars:0"] == "pass"
    assert statuses["remote:home:files_roots:0"] == "pass"
    assert [(request["method"], request["url"]) for request in transport.requests] == [
        ("PROPFIND", "https://cloud.example.invalid/remote.php/dav/"),
        (
            "PROPFIND",
            "https://cloud.example.invalid/remote.php/dav/principals/users/alice/",
        ),
        (
            "PROPFIND",
            "https://cloud.example.invalid/remote.php/dav/calendars/alice/",
        ),
        (
            "PROPFIND",
            "https://cloud.example.invalid/remote.php/dav/files/alice/work/",
        ),
    ]


@pytest.mark.parametrize(
    ("remote_status", "expected_detail"),
    [(404, "resource was not found"), (500, "server returned a server error")],
)
def test_doctor_authenticated_remote_failures_fail_with_bounded_details(
    monkeypatch, tmp_path, remote_status, expected_detail
):
    path = write_config(tmp_path)
    monkeypatch.setattr(checks, "probe_pass", lambda: (True, "fixture pass"))
    stored = {"login_name": "alice@example.invalid", "app_password": "fixture-secret"}
    monkeypatch.setattr(secrets, "get", lambda profile, key: stored.get(key))
    transport = FakeTransport(
        [
            principal_response(),
            home_response(),
            response(remote_status),
            response(207),
        ]
    )

    report = checks.run(path, transport=transport)

    assert report.exit_code == exits.PRECONDITION_FAILED
    remote = next(check for check in report.checks if check.name == "remote:home:calendars:0")
    assert remote.status == "fail"
    assert expected_detail in remote.detail
    assert [(request["method"], request["url"]) for request in transport.requests] == [
        ("PROPFIND", "https://cloud.example.invalid/remote.php/dav/"),
        (
            "PROPFIND",
            "https://cloud.example.invalid/remote.php/dav/principals/users/alice/",
        ),
        (
            "PROPFIND",
            "https://cloud.example.invalid/remote.php/dav/calendars/alice/",
        ),
        (
            "PROPFIND",
            "https://cloud.example.invalid/remote.php/dav/files/alice/work/",
        ),
    ]


def test_doctor_fails_when_secret_backend_cannot_read(monkeypatch, tmp_path):
    path = write_config(tmp_path)
    monkeypatch.setattr(checks, "probe_pass", lambda: (True, "fixture pass"))

    def unreadable(profile, key):
        raise secrets.SecretError("backend unavailable")

    monkeypatch.setattr(secrets, "get", unreadable)

    report = checks.run(path)

    assert report.exit_code == exits.PRECONDITION_FAILED
    statuses = {check.name: check.status for check in report.checks}
    assert statuses["credential:home"] == "fail"
    assert statuses["principal:home"] == "skip"
    credential = next(check for check in report.checks if check.name == "credential:home")
    assert "no credential" not in credential.detail


def test_doctor_json_contains_checks_and_full_exit_map(monkeypatch, tmp_path, capsys):
    path = write_config(tmp_path)
    monkeypatch.setenv("NCL_CONFIG", str(path))
    monkeypatch.setattr(checks, "probe_pass", lambda: (True, "fixture pass"))

    assert cli.main(["doctor", "--json"]) == exits.OK
    output = json.loads(capsys.readouterr().out)

    assert output["checks"]
    assert set(output["response"]) == {str(code) for code in exits.RESPONSE}
