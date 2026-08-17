from __future__ import annotations

import json

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


def test_doctor_json_contains_checks_and_full_exit_map(monkeypatch, tmp_path, capsys):
    path = write_config(tmp_path)
    monkeypatch.setenv("NCL_CONFIG", str(path))
    monkeypatch.setattr(checks, "probe_pass", lambda: (True, "fixture pass"))

    assert cli.main(["doctor", "--json"]) == exits.OK
    output = json.loads(capsys.readouterr().out)

    assert output["checks"]
    assert set(output["response"]) == {str(code) for code in exits.RESPONSE}
