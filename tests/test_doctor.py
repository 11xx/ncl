from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from test_auth import (
    FakeTransport,
    credential_reader,
    home_response,
    principal_response,
    response,
)
from test_config import VALID, write_config

from ncl import checks, cli, exits, identity, secrets


def resource_response(href: str):
    body = (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        f"<d:response><d:href>{href}</d:href>"
        "<d:propstat><d:prop><d:resourcetype/></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        "</d:response></d:multistatus>"
    ).encode()
    return response(207, body)


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
    monkeypatch.setattr(
        secrets, "get", credential_reader("alice@example.invalid", "fixture-secret")
    )
    transport = FakeTransport(
        [
            principal_response(),
            home_response(),
            resource_response("/remote.php/dav/calendars/alice/"),
            resource_response("/remote.php/dav/files/alice/work/"),
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
    monkeypatch.setattr(
        secrets, "get", credential_reader("alice@example.invalid", "fixture-secret")
    )
    transport = FakeTransport(
        [
            principal_response(),
            home_response(),
            response(remote_status),
            resource_response("/remote.php/dav/files/alice/work/"),
        ]
    )

    report = checks.run(path, transport=transport)

    expected_code = exits.TARGET_NOT_FOUND if remote_status == 404 else exits.SERVER_ERROR
    assert report.exit_code == expected_code
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

    def unreadable(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "gpg: decryption failed\n")

    monkeypatch.setattr(secrets, "_run", unreadable)

    report = checks.run(path)

    assert report.exit_code == exits.PRECONDITION_FAILED
    statuses = {check.name: check.status for check in report.checks}
    assert statuses["credential:home"] == "fail"
    assert statuses["principal:home"] == "skip"
    credential = next(check for check in report.checks if check.name == "credential:home")
    assert "no credential" not in credential.detail
    assert "fix the backend" in credential.detail


def test_doctor_preserves_credential_rejection_code_and_json_remediation(
    monkeypatch, tmp_path
):
    path = write_config(tmp_path)
    monkeypatch.setattr(checks, "probe_pass", lambda: (True, "fixture pass"))
    monkeypatch.setattr(
        secrets, "get", credential_reader("alice@example.invalid", "fixture-secret")
    )

    report = checks.run(path, transport=FakeTransport([response(401)]))
    output = report.as_dict()

    assert report.exit_code == exits.CREDENTIAL_REJECTED
    assert output["code"] == exits.CREDENTIAL_REJECTED
    assert output["exit_code"] == exits.CREDENTIAL_REJECTED
    assert output["remediation"] == exits.RESPONSE[exits.CREDENTIAL_REJECTED]
    assert all(
        check.code == exits.CREDENTIAL_REJECTED
        for check in report.checks
        if check.status == "fail"
    )


@pytest.mark.parametrize("body", [b"", b"<root/>"])
def test_resource_existence_rejects_bodyless_or_wrong_root_207(body):
    session = SimpleNamespace(
        request=lambda *args, **kwargs: response(207, body),
    )

    with pytest.raises(identity.IdentityError) as error:
        checks._resource_exists(session, "/remote.php/dav/files/alice/work/")

    assert getattr(error.value, "code", None) == exits.MALFORMED_RESPONSE


def test_doctor_json_contains_checks_and_full_exit_map(monkeypatch, tmp_path, capsys):
    path = write_config(tmp_path)
    monkeypatch.setenv("NCL_CONFIG", str(path))
    monkeypatch.setattr(checks, "probe_pass", lambda: (True, "fixture pass"))

    assert cli.main(["doctor", "--json"]) == exits.OK
    output = json.loads(capsys.readouterr().out)

    assert output["checks"]
    assert set(output["response"]) == {str(code) for code in exits.RESPONSE}
