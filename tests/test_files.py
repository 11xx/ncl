from __future__ import annotations

import dataclasses
import hashlib
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


def test_a_file_create_answered_412_is_uncertain():
    plan = files.plan_write(
        PROFILE,
        session=FakeSession(_missing(SCRIPT)),
        href=SCRIPT,
        content=b"new",
    )

    with pytest.raises(files.FileError) as error:
        _apply_bundle(plan, FakeSession(response(412)))

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).progress[0].state == "uncertain"


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


DESTINATION = ROOT + "archive/vm%402-example"


def _stat(href, *, collection=False, size="12", etag='"v1"'):
    return response(207, multistatus(entry(href, collection=collection, size=size, etag=etag)))


def _missing(href):
    return response(207, multistatus(failed_entry(href)))


#: Twelve bytes, matching the size the metadata fixtures report, so a test that
#: changes content without changing length is expressible.
MOVED = b"contents-12."
OTHER = b"different..."


def _content(body: bytes = MOVED, *, etag: str = '"v1"'):
    return response(200, body, {"ETag": etag})


def _planned_move(transport=None):
    """Freeze one move against the standard source revision."""
    transport = transport or FakeSession(
        _stat(SCRIPT), _missing(DESTINATION), _content()
    )
    return files.plan_move(
        PROFILE, session=transport, href=SCRIPT, destination=DESTINATION
    )


def test_a_move_freezes_both_hrefs_and_refuses_to_overwrite_the_destination():
    transport = FakeSession(
        _stat(SCRIPT),
        _missing(DESTINATION),
        _content(),
        response(201),
        _stat(DESTINATION, etag='"v2"'),
        _content(etag='"v2"'),
        _missing(SCRIPT),
    )

    plan = files.plan_move(
        PROFILE, session=transport, href=SCRIPT, destination=DESTINATION
    )
    step = plan.steps[0]

    assert step.action == "files.move"
    assert step.href == SCRIPT
    assert step.details["destination"] == DESTINATION

    result = _apply_bundle(plan, transport)
    move = next(item for item in transport.requests if item["method"] == "MOVE")

    assert move["url"] == SCRIPT
    assert move["headers"]["Destination"] == DESTINATION
    assert move["headers"]["Overwrite"] == "F"
    assert move["headers"]["If-Match"] == '"v1"'
    assert result["verified"] is True
    assert result["moved_from"] == SCRIPT


def test_a_move_onto_an_occupied_destination_is_refused_while_planning():
    transport = FakeSession(_stat(SCRIPT), _stat(DESTINATION))

    with pytest.raises(files.FileError) as refusal:
        files.plan_move(PROFILE, session=transport, href=SCRIPT, destination=DESTINATION)

    assert refusal.value.code == exits.CONFLICT
    assert not any(item["method"] == "MOVE" for item in transport.requests)


def test_a_collection_cannot_be_moved():
    transport = FakeSession(_stat(ROOT + "archive/", collection=True, size=""))

    with pytest.raises(files.FileError) as refusal:
        files.plan_move(
            PROFILE, session=transport, href=ROOT + "archive/", destination=DESTINATION
        )

    assert refusal.value.code == exits.UNSUPPORTED_STRUCTURE


def test_a_destination_outside_the_allowlist_is_refused_before_any_request():
    transport = FakeSession()

    with pytest.raises(files.FileError) as refusal:
        files.plan_move(
            PROFILE,
            session=transport,
            href=SCRIPT,
            destination="https://cloud.example.invalid/remote.php/dav/files/alice/other/x",
        )

    assert refusal.value.code == exits.SCOPE_DENIED
    assert transport.requests == []


def test_a_move_the_server_reports_as_an_overwrite_is_uncertain():
    transport = FakeSession(
        _stat(SCRIPT), _missing(DESTINATION), _content(), response(204)
    )
    plan = files.plan_move(
        PROFILE, session=transport, href=SCRIPT, destination=DESTINATION
    )

    with pytest.raises(files.FileError) as refusal:
        _apply_bundle(plan, transport)

    assert refusal.value.code == exits.OUTCOME_UNCERTAIN


def test_a_move_the_server_forbids_is_a_server_refusal_not_a_conflict():
    transport = FakeSession(
        _stat(SCRIPT), _missing(DESTINATION), _content(), response(403)
    )
    plan = files.plan_move(
        PROFILE, session=transport, href=SCRIPT, destination=DESTINATION
    )

    with pytest.raises(files.FileError) as refusal:
        _apply_bundle(plan, transport)

    assert refusal.value.code == exits.SERVER_ERROR


def test_a_move_the_server_refuses_with_412_conflicts_without_moving_anything():
    transport = FakeSession(
        _stat(SCRIPT), _missing(DESTINATION), _content(), response(412)
    )
    plan = files.plan_move(
        PROFILE, session=transport, href=SCRIPT, destination=DESTINATION
    )

    with pytest.raises(files.FileError) as refusal:
        _apply_bundle(plan, transport)

    assert refusal.value.code == exits.CONFLICT


def test_a_move_whose_source_survives_is_uncertain_rather_than_verified():
    transport = FakeSession(
        _stat(SCRIPT),
        _missing(DESTINATION),
        _content(),
        response(201),
        _stat(DESTINATION, etag='"v2"'),
        _content(etag='"v2"'),
        _stat(SCRIPT),
    )
    plan = files.plan_move(
        PROFILE, session=transport, href=SCRIPT, destination=DESTINATION
    )

    with pytest.raises(files.FileError) as refusal:
        _apply_bundle(plan, transport)

    assert refusal.value.code == exits.OUTCOME_UNCERTAIN


def test_a_move_freezes_the_content_identity_it_read_under_the_source_etag():
    transport = FakeSession(_stat(SCRIPT), _missing(DESTINATION), _content())

    step = files.plan_move(
        PROFILE, session=transport, href=SCRIPT, destination=DESTINATION
    ).steps[0]
    read = next(item for item in transport.requests if item["method"] == "GET")

    assert read["url"] == SCRIPT
    assert read["headers"]["If-Match"] == '"v1"'
    assert step.details["sha256"] == hashlib.sha256(MOVED).hexdigest()


def test_a_source_that_changes_between_the_two_plan_time_reads_is_refused():
    """The metadata and the bytes must describe one revision, or there is none."""
    refused = FakeSession(_stat(SCRIPT), _missing(DESTINATION), response(412))

    with pytest.raises(files.FileError) as raced:
        files.plan_move(PROFILE, session=refused, href=SCRIPT, destination=DESTINATION)

    assert raced.value.code == exits.CONFLICT
    assert not any(item["method"] == "MOVE" for item in refused.requests)

    # A server that ignores If-Match answers with the newer revision instead.
    ignored = FakeSession(
        _stat(SCRIPT), _missing(DESTINATION), _content(OTHER, etag='"v2"')
    )

    with pytest.raises(files.FileError) as drifted:
        files.plan_move(PROFILE, session=ignored, href=SCRIPT, destination=DESTINATION)

    assert drifted.value.code == exits.CONFLICT


def test_a_destination_of_the_planned_size_but_other_content_is_uncertain():
    """Size is not identity: two revisions of one length are not the same file."""
    transport = FakeSession(
        _stat(SCRIPT),
        _missing(DESTINATION),
        _content(),
        response(201),
        _stat(DESTINATION, etag='"v2"'),
        _content(OTHER, etag='"v2"'),
    )
    plan = files.plan_move(
        PROFILE, session=transport, href=SCRIPT, destination=DESTINATION
    )

    with pytest.raises(files.FileError) as refusal:
        _apply_bundle(plan, transport)

    assert refusal.value.code == exits.OUTCOME_UNCERTAIN
    assert len(OTHER) == len(MOVED)


def test_a_destination_that_cannot_be_read_after_the_move_is_uncertain():
    plan = _planned_move()
    transport = FakeSession(
        response(201), _stat(DESTINATION, etag='"v2"'), response(500)
    )

    with pytest.raises(files.FileError) as refusal:
        _apply_bundle(plan, transport)

    assert refusal.value.code == exits.OUTCOME_UNCERTAIN


def test_a_move_step_without_a_usable_content_identity_is_stale():
    step = _planned_move().steps[0]

    for digest in (None, "", "not-hex", "AB" * 32, "ab" * 31):
        broken = dataclasses.replace(step, details={**step.details, "sha256": digest})
        with pytest.raises(plans.PlanError) as refusal:
            files.validate_step(broken)
        assert refusal.value.code == exits.PLAN_STALE


def test_reconciliation_holds_the_destination_to_the_frozen_content():
    plan = _planned_move()
    step = plan.steps[0]

    changed = FakeSession(
        _stat(DESTINATION, etag='"v2"'), _missing(SCRIPT), _content(OTHER, etag='"v2"')
    )
    assert files.reconcile(PROFILE, session=changed, step=step) == {"state": "uncertain"}

    unreadable = FakeSession(
        _stat(DESTINATION, etag='"v2"'), _missing(SCRIPT), response(500)
    )
    assert files.reconcile(PROFILE, session=unreadable, step=step) == {
        "state": "uncertain"
    }


def test_a_collection_is_created_and_read_back_as_one():
    collection = ROOT + "archive/"
    transport = FakeSession(
        _missing(collection),
        response(201),
        _stat(collection, collection=True, size=""),
    )

    plan = files.plan_mkcol(PROFILE, session=transport, href=collection)
    result = _apply_bundle(plan, transport)
    created = next(item for item in transport.requests if item["method"] == "MKCOL")

    assert created["url"] == collection
    assert result["collection"] is True
    assert result["verified"] is True


def test_creating_a_collection_that_exists_is_refused_while_planning():
    collection = ROOT + "archive/"
    transport = FakeSession(_stat(collection, collection=True, size=""))

    with pytest.raises(files.FileError) as refusal:
        files.plan_mkcol(PROFILE, session=transport, href=collection)

    assert refusal.value.code == exits.CONFLICT


def test_a_collection_the_server_reports_as_a_file_is_uncertain():
    collection = ROOT + "archive/"
    transport = FakeSession(_missing(collection), response(201), _stat(collection))
    plan = files.plan_mkcol(PROFILE, session=transport, href=collection)

    with pytest.raises(files.FileError) as refusal:
        _apply_bundle(plan, transport)

    assert refusal.value.code == exits.OUTCOME_UNCERTAIN


def test_a_move_that_never_reached_the_server_reconciles_as_pending():
    plan = _planned_move()
    reader = FakeSession(_missing(DESTINATION), _stat(SCRIPT))

    assert files.reconcile(PROFILE, session=reader, step=plan.steps[0]) == {
        "state": "pending"
    }


def test_a_completed_move_reconciles_as_verified():
    plan = _planned_move()
    reader = FakeSession(
        _stat(DESTINATION, etag='"v2"'), _missing(SCRIPT), _content(etag='"v2"')
    )

    assert files.reconcile(PROFILE, session=reader, step=plan.steps[0]) == {
        "state": "verified"
    }


def nextcloud_collection(href: str, *, etag: str = '"6a7fe35516ec7"') -> str:
    """A collection exactly as Nextcloud answers a depth-one PROPFIND.

    The properties that describe an entity body are reported as nonexistent in
    their own `404` propstat rather than omitted, which is what RFC 4918 asks
    of a server whose resource has no such property.
    """
    return f"""<d:response><d:href>{href}</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/></d:resourcetype>
      <d:getlastmodified>Sat, 15 Aug 2026 03:56:05 GMT</d:getlastmodified>
      <d:getetag>{etag}</d:getetag>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
    <d:propstat><d:prop><d:getcontentlength/><d:getcontenttype/></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
    </d:response>"""


def test_list_reads_a_collection_whose_body_properties_do_not_apply():
    body = multistatus(
        nextcloud_collection(ROOT),
        nextcloud_collection(ROOT + "Documents/", etag='"6a7fe355ce64c"'),
        entry(ROOT + "Readme.md", size="197", content_type="text/markdown"),
    )
    transport = FakeSession(response(207, body))

    found = files.list_collection(PROFILE, session=transport, href=ROOT)

    assert [(item.name, item.collection, item.size) for item in found] == [
        ("Documents", True, None),
        ("Readme.md", False, 197),
    ]
    assert found[0].etag == '"6a7fe355ce64c"'
    assert found[0].content_type == ""


def test_stat_reads_a_collection_whose_body_properties_do_not_apply():
    transport = FakeSession(response(207, multistatus(nextcloud_collection(ROOT))))

    found = files.stat_resource(PROFILE, session=transport, href=ROOT)

    assert found.collection is True
    assert found.size is None
    assert found.content_type == ""


def test_a_file_reporting_no_size_is_refused():
    body = multistatus(
        f"""<d:response><d:href>{SCRIPT}</d:href>
        <d:propstat><d:prop>
          <d:resourcetype/>
          <d:getlastmodified>Wed, 19 Aug 2026 20:00:00 GMT</d:getlastmodified>
          <d:getetag>"v1"</d:getetag>
          <d:getcontenttype>text/plain</d:getcontenttype>
        </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
        <d:propstat><d:prop><d:getcontentlength/></d:prop>
          <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
        </d:response>"""
    )

    with pytest.raises(files.FileError, match="no getcontentlength") as error:
        files.stat_resource(PROFILE, session=FakeSession(response(207, body)), href=SCRIPT)

    assert error.value.code == exits.MALFORMED_RESPONSE


def test_a_collection_reporting_no_etag_is_refused():
    body = multistatus(
        f"""<d:response><d:href>{ROOT}</d:href>
        <d:propstat><d:prop>
          <d:resourcetype><d:collection/></d:resourcetype>
          <d:getlastmodified>Wed, 19 Aug 2026 20:00:00 GMT</d:getlastmodified>
        </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
        <d:propstat><d:prop>
          <d:getetag/><d:getcontentlength/><d:getcontenttype/>
        </d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
        </d:response>"""
    )

    with pytest.raises(files.FileError, match="no getetag"):
        files.stat_resource(PROFILE, session=FakeSession(response(207, body)), href=ROOT)


def test_a_withheld_size_is_still_a_refusal_rather_than_an_absence():
    """A `403` is the server declining to answer, which is not a property that does not exist."""
    body = multistatus(
        f"""<d:response><d:href>{SCRIPT}</d:href>
        <d:propstat><d:prop>
          <d:resourcetype/>
          <d:getlastmodified>Wed, 19 Aug 2026 20:00:00 GMT</d:getlastmodified>
          <d:getetag>"v1"</d:getetag>
        </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
        <d:propstat><d:prop><d:getcontentlength/><d:getcontenttype/></d:prop>
          <d:status>HTTP/1.1 403 Forbidden</d:status></d:propstat>
        </d:response>"""
    )

    with pytest.raises(files.FileError, match="not returned successfully"):
        files.stat_resource(PROFILE, session=FakeSession(response(207, body)), href=SCRIPT)


def test_a_property_reported_both_present_and_absent_is_malformed():
    body = multistatus(
        f"""<d:response><d:href>{SCRIPT}</d:href>
        <d:propstat><d:prop>
          <d:resourcetype/>
          <d:getcontentlength>12</d:getcontentlength>
          <d:getlastmodified>Wed, 19 Aug 2026 20:00:00 GMT</d:getlastmodified>
          <d:getetag>"v1"</d:getetag>
        </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
        <d:propstat><d:prop><d:getcontentlength/></d:prop>
          <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
        </d:response>"""
    )

    with pytest.raises(files.FileError, match="repeated property getcontentlength"):
        files.stat_resource(PROFILE, session=FakeSession(response(207, body)), href=SCRIPT)


def test_a_file_without_a_media_type_is_read_rather_than_refused():
    body = multistatus(
        f"""<d:response><d:href>{SCRIPT}</d:href>
        <d:propstat><d:prop>
          <d:resourcetype/>
          <d:getcontentlength>12</d:getcontentlength>
          <d:getlastmodified>Wed, 19 Aug 2026 20:00:00 GMT</d:getlastmodified>
          <d:getetag>"v1"</d:getetag>
        </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
        <d:propstat><d:prop><d:getcontenttype/></d:prop>
          <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
        </d:response>"""
    )

    found = files.stat_resource(PROFILE, session=FakeSession(response(207, body)), href=SCRIPT)

    assert found.content_type == ""
    assert found.size == 12
