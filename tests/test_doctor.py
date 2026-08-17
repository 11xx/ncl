from __future__ import annotations

import json

from test_config import VALID, write_config

from ncl import checks, cli, exits


def test_broken_config_fails_doctor(tmp_path):
    path = write_config(tmp_path, VALID + "unexpected = true\n")

    report = checks.run(path)

    assert report.exit_code == exits.PRECONDITION_FAILED
    assert any(check.name == "config" and check.status == "fail" for check in report.checks)


def test_good_config_passes_without_backend_or_server(monkeypatch, tmp_path):
    path = write_config(tmp_path)
    monkeypatch.setattr(checks, "probe_pass", lambda: (True, "fixture pass"))
    monkeypatch.setattr(checks, "probe_libsecret", lambda: (False, "must be skipped"))
    monkeypatch.setattr(checks, "probe_callback_port", lambda port: (True, "fixture port"))

    report = checks.run(path)

    assert report.exit_code == exits.OK
    assert all(check.status != "fail" for check in report.checks)
    assert any(
        check.name == "secret-backend:home:libsecret" and check.status == "skip"
        for check in report.checks
    )


def test_doctor_json_contains_checks_and_full_exit_map(monkeypatch, tmp_path, capsys):
    path = write_config(tmp_path)
    monkeypatch.setenv("NCL_CONFIG", str(path))
    monkeypatch.setattr(checks, "probe_pass", lambda: (True, "fixture pass"))
    monkeypatch.setattr(checks, "probe_callback_port", lambda port: (True, "fixture port"))

    assert cli.main(["doctor", "--json"]) == exits.OK
    output = json.loads(capsys.readouterr().out)

    assert output["checks"]
    assert set(output["response"]) == {str(code) for code in exits.RESPONSE}
