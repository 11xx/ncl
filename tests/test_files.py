from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from ncl import cli, exits, files, plans, secrets
from ncl.config import Profile
from ncl.session import Response, Session, SessionError

PROFILE = Profile(
    "home",
    "https://cloud.example.invalid",
    "pass",
    ("/remote.php/dav/calendars/alice/work/",),
    ("/remote.php/dav/files/alice/Violentmonkey/",),
)
ROOT = "https://cloud.example.invalid/remote.php/dav/files/alice/Violentmonkey/"
SCRIPT = ROOT + "vm%402-example"


def multistatus(*responses: str) -> bytes:
    return (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        + "".join(responses)
        + "</d:multistatus>"
    ).encode()


def entry(
    href: str,
    *,
    collection: bool = False,
    etag: str = '"v1"',
    size: str = "12",
    modified: str = "Wed, 19 Aug 2026 20:00:00 GMT",
    content_type: str = "application/json",
    prop_status: str = "HTTP/1.1 200 OK",
) -> str:
    resource_type = "<d:collection/>" if collection else ""
    return f"""<d:response><d:href>{href}</d:href><d:propstat><d:prop>
      <d:resourcetype>{resource_type}</d:resourcetype>
      <d:getcontentlength>{size}</d:getcontentlength>
      <d:getlastmodified>{modified}</d:getlastmodified>
      <d:getetag>{etag}</d:getetag><d:getcontenttype>{content_type}</d:getcontenttype>
    </d:prop><d:status>{prop_status}</d:status></d:propstat></d:response>"""


def failed_entry(href: str, status: str = "HTTP/1.1 404 Not Found") -> str:
    return f"<d:response><d:href>{href}</d:href><d:status>{status}</d:status></d:response>"


class FakeSession:
    def __init__(self, *responses: Response | Exception):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, data=None, **kwargs):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "data": data,
                "kwargs": kwargs,
            }
        )
        assert self.responses, f"unexpected request: {method} {url}"
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _apply_bundle(plan, transport):
    with plans.claim(plan.plan_id):
        return plans.apply(
            PROFILE,
            session=transport,
            plan=plan,
            dispatchers=cli._dispatchers(),
        )


def response(
    status: int,
    body: bytes = b"",
    headers: Mapping[str, str] | None = None,
    url: str = "",
) -> Response:
    return Response(status, headers or {}, body, url)


def configure(monkeypatch, tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f'''default_profile = "home"

[profiles.home]
origin = "https://cloud.example.invalid"
secret_backend = "pass"
calendars = ["/remote.php/dav/calendars/alice/work/"]
files_roots = ["{ROOT}"]
'''
    )
    monkeypatch.setenv("NCL_CONFIG", str(path))


def test_listing_is_a_depth_one_propfind_and_returns_only_children():
    body = multistatus(
        entry("/remote.php/dav/files/alice/Violentmonkey/", collection=True),
        entry("/remote.php/dav/files/alice/Violentmonkey/vm%402-example"),
        entry(
            "/remote.php/dav/files/alice/Violentmonkey/archive/",
            collection=True,
            size="",
            content_type="httpd/unix-directory",
        ),
    )
    transport = FakeSession(response(207, body))

    found = files.list_collection(PROFILE, session=transport, href=ROOT)

    assert transport.requests[0]["method"] == "PROPFIND"
    assert transport.requests[0]["headers"]["Depth"] == "1"
    assert [item.name for item in found] == ["archive", "vm@2-example"]
    assert found[0].collection is True
    assert found[1].size == 12
    assert found[1].modified == "2026-08-19T20:00:00+00:00"


def test_listing_rejects_a_depth_one_response_outside_the_collection():
    body = multistatus(
        entry("/remote.php/dav/files/alice/Violentmonkey/", collection=True),
        entry("/remote.php/dav/files/alice/elsewhere"),
    )
    with pytest.raises(files.FileError, match="outside its collection"):
        files.list_collection(PROFILE, session=FakeSession(response(207, body)), href=ROOT)


def test_listing_refuses_a_redirect_before_following_it(monkeypatch):
    outside = "https://cloud.example.invalid/remote.php/dav/files/alice/private/"
    transport = FakeSession(
        response(302, headers={"Location": outside}, url=ROOT),
        response(207, multistatus(entry(outside, collection=True)), url=outside),
    )
    record = secrets._encode(secrets.Credential("alice", "fixture"))
    monkeypatch.setattr(
        secrets,
        "get",
        lambda profile, key: record if key == secrets.RECORD_KEY else None,
    )

    with pytest.raises(SessionError) as error:
        files.list_collection(PROFILE, session=Session(PROFILE, transport=transport), href=ROOT)

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert [request["method"] for request in transport.requests] == ["PROPFIND"]
    assert transport.requests[0]["url"] == ROOT


def test_read_refuses_a_same_origin_redirect_before_following_it(monkeypatch):
    outside = "https://cloud.example.invalid/remote.php/dav/files/alice/private/secret"
    transport = FakeSession(
        response(302, headers={"Location": outside}, url=SCRIPT),
        response(200, b"outside", {"Content-Type": "text/plain"}, outside),
    )
    record = secrets._encode(secrets.Credential("alice", "fixture"))
    monkeypatch.setattr(
        secrets,
        "get",
        lambda profile, key: record if key == secrets.RECORD_KEY else None,
    )

    with pytest.raises(SessionError) as error:
        files.read_file(PROFILE, session=Session(PROFILE, transport=transport), href=SCRIPT)

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert [request["method"] for request in transport.requests] == ["GET"]
    assert transport.requests[0]["url"] == SCRIPT


def test_scope_is_checked_before_any_request():
    transport = FakeSession()
    with pytest.raises(files.FileError) as error:
        files.read_file(
            PROFILE,
            session=transport,
            href="/remote.php/dav/files/alice/private/secret",
        )
    assert error.value.code == exits.SCOPE_DENIED
    assert transport.requests == []


@pytest.mark.parametrize("href", [ROOT + "%2e%2e%2fprivate", ROOT + "name%2Fwith-slash"])
def test_scope_rejects_encoded_separators_before_any_request(href):
    transport = FakeSession()

    with pytest.raises(files.FileError) as error:
        files.read_file(PROFILE, session=transport, href=href)

    assert error.value.code == exits.SCOPE_DENIED
    assert transport.requests == []


def test_stat_requires_the_requested_resource_in_a_multistatus():
    body = multistatus(entry("/remote.php/dav/files/alice/Violentmonkey/other"))
    with pytest.raises(files.FileError, match="omitted"):
        files.stat_resource(PROFILE, session=FakeSession(response(207, body)), href=SCRIPT)


def test_stat_does_not_treat_an_unrelated_404_as_the_requested_file_missing():
    body = multistatus(failed_entry("/remote.php/dav/files/alice/Violentmonkey/other"))
    with pytest.raises(files.FileError, match="omitted"):
        files.stat_resource(
            PROFILE,
            session=FakeSession(response(207, body)),
            href=SCRIPT,
            missing_ok=True,
        )


def test_stat_can_observe_a_missing_resource_for_create_planning():
    assert (
        files.stat_resource(
            PROFILE,
            session=FakeSession(response(404)),
            href=SCRIPT,
            missing_ok=True,
        )
        is None
    )


def test_stat_does_not_read_properties_from_a_failed_propstat():
    body = multistatus(
        entry(
            "/remote.php/dav/files/alice/Violentmonkey/vm%402-example",
            prop_status="HTTP/1.1 403 Forbidden",
        )
    )
    with pytest.raises(files.FileError, match="not returned successfully"):
        files.stat_resource(PROFILE, session=FakeSession(response(207, body)), href=SCRIPT)


def test_stat_refuses_a_partial_propstat_before_planning_a_delete():
    body = multistatus(
        f"""<d:response><d:href>{SCRIPT}</d:href>
        <d:propstat><d:prop><d:getetag>\"v1\"</d:getetag></d:prop>
          <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
        <d:propstat><d:prop><d:resourcetype/><d:getcontenttype>text/plain</d:getcontenttype></d:prop>
          <d:status>HTTP/1.1 403 Forbidden</d:status></d:propstat>
        </d:response>"""
    )
    transport = FakeSession(response(207, body))

    with pytest.raises(files.FileError, match="resourcetype") as error:
        files.plan_delete(PROFILE, session=transport, href=SCRIPT)

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert [request["method"] for request in transport.requests] == ["PROPFIND"]


def test_stat_refuses_duplicate_resource_hrefs():
    body = multistatus(entry(SCRIPT), entry(SCRIPT, collection=True, etag='"v2"', size=""))
    transport = FakeSession(response(207, body))

    with pytest.raises(files.FileError, match="repeated a resource href") as error:
        files.stat_resource(PROFILE, session=transport, href=SCRIPT)

    assert error.value.code == exits.MALFORMED_RESPONSE
    assert [request["method"] for request in transport.requests] == ["PROPFIND"]


def test_read_returns_exact_content_and_response_metadata():
    content = b'{"code":"hello"}\n'
    transport = FakeSession(
        response(
            200,
            content,
            {
                "ETag": '"v2"',
                "Last-Modified": "Wed, 19 Aug 2026 20:00:00 GMT",
                "Content-Type": "application/json",
            },
        )
    )

    reference, stored = files.read_file(PROFILE, session=transport, href=SCRIPT)

    assert stored == content
    assert reference.etag == '"v2"'
    assert reference.size == len(content)


def test_binary_content_is_refused_at_the_text_output_boundary():
    with pytest.raises(files.FileError) as error:
        files.text_content(b"\x00\xff")
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_exact_binary_content_can_be_written_to_an_explicit_local_path(tmp_path):
    target = tmp_path / "blob"
    files.write_local(target, b"\x00\xff")
    assert target.read_bytes() == b"\x00\xff"

    with pytest.raises(files.FileError) as error:
        files.write_local(target, b"replacement")
    assert error.value.code == exits.CONFLICT
    assert target.read_bytes() == b"\x00\xff"


def test_write_planning_freezes_an_if_match_replacement_without_putting():
    body = multistatus(entry("/remote.php/dav/files/alice/Violentmonkey/vm%402-example"))
    transport = FakeSession(response(207, body))

    plan = files.plan_write(
        PROFILE,
        session=transport,
        href=SCRIPT,
        content=b"replacement",
        content_type="application/json",
    )

    assert [request["method"] for request in transport.requests] == ["PROPFIND"]
    assert plan.steps[0].action == "files.write"
    assert plan.steps[0].etag == '"v1"'
    assert plans.payload_bytes(plan.steps[0]) == b"replacement"


def test_write_planning_freezes_an_if_none_match_creation():
    plan = files.plan_write(
        PROFILE,
        session=FakeSession(response(404)),
        href=SCRIPT,
        content=b"new",
    )
    assert plan.steps[0].etag == ""
    assert plan.steps[0].details["exists"] is False


@pytest.mark.parametrize("bad_etag", ["", "*", 'W/"v1"', "v1"])
def test_file_replacement_and_deletion_require_strong_quoted_etags(bad_etag):
    body = multistatus(entry(SCRIPT, etag=bad_etag))
    with pytest.raises(files.FileError) as write_error:
        files.plan_write(
            PROFILE,
            session=FakeSession(response(207, body)),
            href=SCRIPT,
            content=b"replacement",
        )
    assert write_error.value.code == exits.MALFORMED_RESPONSE

    with pytest.raises(files.FileError) as delete_error:
        files.plan_delete(
            PROFILE,
            session=FakeSession(response(207, body)),
            href=SCRIPT,
        )
    assert delete_error.value.code == exits.MALFORMED_RESPONSE


def test_apply_conditionally_writes_and_verifies_exact_readback():
    plan = files.plan_write(
        PROFILE,
        session=FakeSession(response(404)),
        href=SCRIPT,
        content=b"new",
        content_type="application/json",
    )
    transport = FakeSession(
        response(201),
        response(200, b"new", {"ETag": '"stored"', "Content-Type": "application/json"}),
    )

    result = _apply_bundle(plan, transport)

    put = transport.requests[0]
    assert put["method"] == "PUT"
    assert put["headers"]["If-None-Match"] == "*"
    assert put["data"] == b"new"
    assert result["verified"] is True
    with pytest.raises(plans.PlanError):
        plans.read(plan.plan_id)


def test_apply_keeps_a_plan_when_readback_differs():
    plan = files.plan_write(
        PROFILE,
        session=FakeSession(response(404)),
        href=SCRIPT,
        content=b"new",
    )
    transport = FakeSession(response(201), response(200, b"changed"))

    with pytest.raises(files.FileError) as error:
        _apply_bundle(plan, transport)
    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert isinstance(error.value.__cause__, files.FileError)
    assert plans.read(plan.plan_id).plan_id == plan.plan_id


def test_apply_keeps_request_failure_classification_before_acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = files.plan_write(
        PROFILE,
        session=FakeSession(response(404)),
        href=SCRIPT,
        content=b"new",
    )
    transport = FakeSession(
        SessionError("the configured origin was unreachable", exits.UNREACHABLE)
    )

    with pytest.raises(SessionError) as error:
        _apply_bundle(plan, transport)

    assert error.value.code == exits.UNREACHABLE
    stored = plans.read(plan.plan_id)
    assert stored.progress[0].state == "pending"
    assert stored.progress[0].exit_code == exits.UNREACHABLE
    assert stored.expires_at is not None


def test_cli_apply_marks_an_unreachable_put_readback_uncertain_and_blocks_retry(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = files.plan_write(
        PROFILE,
        session=FakeSession(response(404)),
        href=SCRIPT,
        content=b"new",
    )
    first = FakeSession(
        response(201, url=plan.steps[0].href),
        SessionError("the configured origin was unreachable", exits.UNREACHABLE),
    )
    second = FakeSession()
    transports = iter((first, second))
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: next(transports))

    assert cli.main(["apply", plan.plan_id, "--json"]) == exits.OUTCOME_UNCERTAIN
    first_result = json.loads(capsys.readouterr().out)
    assert first_result["code"] == exits.OUTCOME_UNCERTAIN
    stored = plans.read(plan.plan_id)
    assert stored.progress[0].state == "uncertain"
    assert stored.progress[0].exit_code == exits.OUTCOME_UNCERTAIN
    assert stored.expires_at is None
    assert [request["method"] for request in first.requests] == ["PUT", "GET"]

    assert cli.main(["apply", plan.plan_id, "--json"]) == exits.OUTCOME_UNCERTAIN
    second_result = json.loads(capsys.readouterr().out)
    assert second_result["code"] == exits.OUTCOME_UNCERTAIN
    assert second.requests == []


def test_cli_apply_marks_a_post_delete_readback_failure_uncertain_and_blocks_retry(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = files.plan_delete(
        PROFILE,
        session=FakeSession(response(207, multistatus(entry(SCRIPT)))),
        href=SCRIPT,
    )
    first = FakeSession(
        response(204, url=plan.steps[0].href),
        SessionError("the configured origin was unreachable", exits.UNREACHABLE),
    )
    second = FakeSession()
    transports = iter((first, second))
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: next(transports))

    assert cli.main(["apply", plan.plan_id, "--json"]) == exits.OUTCOME_UNCERTAIN
    first_result = json.loads(capsys.readouterr().out)
    assert first_result["code"] == exits.OUTCOME_UNCERTAIN
    stored = plans.read(plan.plan_id)
    assert stored.progress[0].state == "uncertain"
    assert stored.progress[0].exit_code == exits.OUTCOME_UNCERTAIN
    assert stored.expires_at is None
    assert [request["method"] for request in first.requests] == ["DELETE", "PROPFIND"]

    assert cli.main(["apply", plan.plan_id, "--json"]) == exits.OUTCOME_UNCERTAIN
    second_result = json.loads(capsys.readouterr().out)
    assert second_result["code"] == exits.OUTCOME_UNCERTAIN
    assert second.requests == []


def test_delete_is_conditional_and_verified_missing():
    body = multistatus(entry("/remote.php/dav/files/alice/Violentmonkey/vm%402-example"))
    plan = files.plan_delete(
        PROFILE,
        session=FakeSession(response(207, body)),
        href=SCRIPT,
    )
    transport = FakeSession(response(204), response(404))

    result = _apply_bundle(plan, transport)

    assert transport.requests[0]["headers"] == {"If-Match": '"v1"'}
    assert result["verified"] == "deleted"


def test_file_reconciliation_classifies_exact_missing_old_and_changed_states():
    create = files.plan_write(
        PROFILE,
        session=FakeSession(response(404)),
        href=SCRIPT,
        content=b"new",
    )
    assert files.reconcile(
        PROFILE,
        session=FakeSession(response(404)),
        step=create.steps[0],
    ) == {"state": "pending"}
    assert files.reconcile(
        PROFILE,
        session=FakeSession(response(200, b"new", {"ETag": '"v2"'})),
        step=create.steps[0],
    ) == {"state": "verified"}
    assert files.reconcile(
        PROFILE,
        session=FakeSession(response(200, b"different", {"ETag": '"v2"'})),
        step=create.steps[0],
    ) == {"state": "uncertain"}

    existing_body = multistatus(entry(SCRIPT, etag='"old"'))
    update = files.plan_write(
        PROFILE,
        session=FakeSession(response(207, existing_body)),
        href=SCRIPT,
        content=b"new",
    )
    assert files.reconcile(
        PROFILE,
        session=FakeSession(response(200, b"old", {"ETag": '"old"'})),
        step=update.steps[0],
    ) == {"state": "pending"}
    assert files.reconcile(
        PROFILE,
        session=FakeSession(response(200, b"changed", {"ETag": '"changed"'})),
        step=update.steps[0],
    ) == {"state": "uncertain"}

    delete = files.plan_delete(
        PROFILE,
        session=FakeSession(response(207, existing_body)),
        href=SCRIPT,
    )
    assert files.reconcile(
        PROFILE,
        session=FakeSession(response(404)),
        step=delete.steps[0],
    ) == {"state": "verified"}
    assert files.reconcile(
        PROFILE,
        session=FakeSession(response(200, b"old", {"ETag": '"old"'})),
        step=delete.steps[0],
    ) == {"state": "pending"}
    assert files.reconcile(
        PROFILE,
        session=FakeSession(response(200, b"changed", {"ETag": '"changed"'})),
        step=delete.steps[0],
    ) == {"state": "uncertain"}


def test_cli_reconcile_emits_json_and_consumes_a_fully_verified_plan(monkeypatch, capsys):
    plan = files.plan_write(
        PROFILE,
        session=FakeSession(response(404)),
        href=SCRIPT,
        content=b"new",
    )
    with plans.claim(plan.plan_id):
        plans.update_progress(
            plan,
            0,
            state="uncertain",
            exit_code=exits.OUTCOME_UNCERTAIN,
        )
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(
        cli.session,
        "Session",
        lambda profile: FakeSession(response(200, b"new", {"ETag": '"v2"'})),
    )

    assert cli.main(["plan", "reconcile", plan.plan_id, "--json"]) == exits.OK
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "verified"
    assert result["complete"] is True
    with pytest.raises(plans.PlanError) as error:
        plans.read(plan.plan_id)
    assert error.value.code == exits.TARGET_NOT_FOUND

def test_collection_deletion_is_refused_during_planning():
    body = multistatus(entry("/remote.php/dav/files/alice/Violentmonkey/", collection=True))
    with pytest.raises(files.FileError) as error:
        files.plan_delete(PROFILE, session=FakeSession(response(207, body)), href=ROOT)
    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_cli_lists_files_as_structured_output(monkeypatch, tmp_path, capsys):
    configure(monkeypatch, tmp_path)
    body = multistatus(
        entry("/remote.php/dav/files/alice/Violentmonkey/", collection=True),
        entry("/remote.php/dav/files/alice/Violentmonkey/vm%402-example"),
    )
    transport = FakeSession(response(207, body))
    monkeypatch.setattr(cli.session, "Session", lambda profile: transport)

    code = cli.main(["files", "list", ROOT, "--json"])

    assert code == exits.OK
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["files"][0]["name"] == "vm@2-example"
    assert transport.requests[0]["method"] == "PROPFIND"


def test_cli_write_returns_a_frozen_plan_without_sending_put(
    monkeypatch, tmp_path, capsys
):
    configure(monkeypatch, tmp_path)
    source = tmp_path / "script.json"
    source.write_bytes(b'{"code":"hello"}')
    transport = FakeSession(response(404))
    monkeypatch.setattr(cli.session, "Session", lambda profile: transport)

    code = cli.main(
        [
            "files",
            "write",
            SCRIPT,
            "--from",
            str(source),
            "--content-type",
            "application/json",
            "--json",
        ]
    )

    assert code == exits.CONFIRMATION_REQUIRED
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["plan"]["steps"][0]["action"] == "files.write"
    assert rendered["plan"]["steps"][0]["payload_bytes"] == len(source.read_bytes())
    assert "payload" not in rendered["plan"]["steps"][0]
    assert [request["method"] for request in transport.requests] == ["PROPFIND"]


@pytest.mark.parametrize(
    ("content", "content_type"),
    [(b"\x00\xff", ""), (b"\x00\x01", "application/octet-stream")],
)
def test_cli_refuses_binary_stdout_and_directs_it_to_output(
    monkeypatch, tmp_path, capsys, content, content_type
):
    configure(monkeypatch, tmp_path)
    transport = FakeSession(response(200, content, {"Content-Type": content_type}))
    monkeypatch.setattr(cli.session, "Session", lambda profile: transport)

    code = cli.main(["files", "read", SCRIPT])

    assert code == exits.UNSUPPORTED_STRUCTURE
    assert "use --output" in capsys.readouterr().err


def test_cli_emits_declared_utf8_text_through_the_redacting_stream(monkeypatch, tmp_path, capsys):
    configure(monkeypatch, tmp_path)
    transport = FakeSession(response(200, b"hello", {"Content-Type": "text/plain"}))
    monkeypatch.setattr(cli.session, "Session", lambda profile: transport)

    code = cli.main(["files", "read", SCRIPT])

    assert code == exits.OK
    assert capsys.readouterr().out == "hello"


@pytest.mark.parametrize(
    "changed",
    [
        Profile(
            "home",
            "https://other.example.invalid",
            "pass",
            ("/remote.php/dav/calendars/alice/work/",),
            ("/remote.php/dav/files/alice/Violentmonkey/",),
        ),
        Profile(
            "home",
            "https://cloud.example.invalid",
            "pass",
            ("/remote.php/dav/calendars/alice/work/",),
            ("/remote.php/dav/files/alice/",),
        ),
    ],
)
def test_apply_refuses_profile_configuration_drift_before_any_request(changed):
    plan = files.plan_write(
        PROFILE,
        session=FakeSession(response(404)),
        href=SCRIPT,
        content=b"new",
    )
    transport = FakeSession()

    with plans.claim(plan.plan_id), pytest.raises(plans.PlanError) as error:
        plans.apply(
            changed,
            session=transport,
            plan=plan,
            dispatchers=cli._dispatchers(),
        )

    assert error.value.code == exits.PLAN_STALE
    assert transport.requests == []
    plans.consume(plan.plan_id)
