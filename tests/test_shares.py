"""Shares and the OCS envelope, entirely offline.

A share is the one mutation here that hands a resource to somebody else, so
what these tests hold to is that nothing reaches the server without a plan,
that the plan says who would gain access, and that a link password never
appears in a caller-visible view.
"""

from __future__ import annotations

import json

import pytest
from test_auth import FakeTransport, home_response, principal_response, response

from ncl import cli, exits, ocs, plans, secrets, session, shares
from ncl.config import Profile

PROFILE = Profile(
    name="home",
    origin="https://cloud.example.invalid",
    secret_backend="pass",
    calendars=(),
    files_roots=("/remote.php/dav/files/alice/work/",),
)
ACCOUNT = "alice"
HREF = "https://cloud.example.invalid/remote.php/dav/files/alice/work/notes.md"
CONFIG = """
default_profile = "home"

[profiles.home]
origin = "https://cloud.example.invalid"
secret_backend = "pass"
calendars = []
files_roots = ["/remote.php/dav/files/alice/work/"]
"""


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setattr(
        secrets, "load_credential", lambda profile: secrets.Credential("alice", "app-password")
    )


def envelope(data, *, status: int = 200, message: str = "OK") -> bytes:
    return json.dumps(
        {"ocs": {"meta": {"status": "ok", "statuscode": status, "message": message}, "data": data}}
    ).encode()


def ocs_response(data, *, http: int = 200, status: int = 200, message: str = "OK"):
    return response(
        http, envelope(data, status=status, message=message),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


def share_record(**overrides):
    record = {
        "id": "7",
        "share_type": 3,
        "path": "/work/notes.md",
        "permissions": 17,
        "share_with": None,
        "uid_owner": "alice",
        "url": "https://cloud.example.invalid/s/token",
        "expiration": "",
        "note": "",
        "label": "",
        "password": None,
    }
    record.update(overrides)
    return record


def transport_for(*responses):
    return session.Session(PROFILE, transport=FakeTransport(list(responses)))


def test_every_ocs_call_identifies_itself_as_an_api_request_and_asks_for_json():
    """Without the header Nextcloud answers a login page, which parses as neither."""
    fake = FakeTransport([ocs_response([])])

    ocs.request(
        PROFILE,
        session=session.Session(PROFILE, transport=fake),
        method="GET",
        url=ocs.path("apps", "files_sharing", "api", "v1", "shares"),
    )

    sent = fake.requests[0]
    assert sent["headers"]["OCS-APIRequest"] == "true"
    assert sent["headers"]["Accept"] == "application/json"
    assert "format=json" in sent["url"]


def test_a_non_json_answer_names_the_missing_api_treatment():
    with pytest.raises(ocs.OcsError, match="did not treat this as an API request"):
        ocs.request(
            PROFILE,
            session=transport_for(
                response(200, b"<html>login</html>", headers={"Content-Type": "text/html"})
            ),
            method="GET",
            url=ocs.path("cloud", "user"),
        )


def test_an_ocs_status_carries_the_servers_own_reason():
    with pytest.raises(ocs.OcsError, match="Wrong path") as error:
        ocs.request(
            PROFILE,
            session=transport_for(
                ocs_response(
                    None, http=404, status=404,
                    message="Wrong path, file/folder does not exist",
                )
            ),
            method="GET",
            url=ocs.path("apps", "files_sharing", "api", "v1", "shares"),
        )

    assert error.value.code == exits.TARGET_NOT_FOUND


def test_an_envelope_without_data_is_malformed_rather_than_empty():
    body = json.dumps({"ocs": {"meta": {"statuscode": 200}}}).encode()
    with pytest.raises(ocs.OcsError, match="carried no data"):
        ocs.request(
            PROFILE,
            session=transport_for(
                response(200, body, headers={"Content-Type": "application/json"})
            ),
            method="GET",
            url=ocs.path("cloud", "user"),
        )


def test_a_listing_reads_the_reach_of_each_share():
    found = shares.list_shares(
        PROFILE,
        session=transport_for(ocs_response([share_record()])),
        account_name=ACCOUNT,
    )

    assert len(found) == 1
    assert found[0].share_type == "public_link"
    assert found[0].permissions == ("read", "share")
    assert found[0].href == HREF
    assert found[0].public is True


def test_an_unmodelled_permission_bit_is_named_and_does_not_end_the_listing():
    """One share this tool cannot fully read must not hide the ones it can.

    The unscoped listing answers "who can see my files", so refusing the whole
    response over a bit above the modelled set answers it with silence. The
    name reports the reach instead, as an unrecognised share type does.
    """
    found = shares.list_shares(
        PROFILE,
        session=transport_for(
            ocs_response([share_record(permissions=33), share_record(id="2")])
        ),
        account_name=ACCOUNT,
    )

    assert len(found) == 2
    assert found[0].permissions == ("read", "unmodelled:32")
    assert found[1].permissions == ("read", "share")


def test_a_share_type_this_tool_does_not_create_is_still_reported():
    """A listing answers who can already see a file, so it hides nothing it cannot write."""
    found = shares.list_shares(
        PROFILE,
        session=transport_for(ocs_response([share_record(share_type=6, share_with="bob@remote")])),
        account_name=ACCOUNT,
    )

    assert found[0].share_type == "federated"
    assert found[0].writable is False


def test_a_share_type_the_tool_has_no_name_for_is_labelled_rather_than_dropped():
    found = shares.list_shares(
        PROFILE,
        session=transport_for(ocs_response([share_record(share_type=99)])),
        account_name=ACCOUNT,
    )

    assert found[0].share_type == "unsupported:99"
    assert found[0].writable is False


def test_a_path_outside_the_allowlist_is_refused_before_any_request():
    fake = FakeTransport([])

    with pytest.raises(shares.ShareError) as error:
        shares.list_shares(
            PROFILE,
            session=session.Session(PROFILE, transport=fake),
            account_name=ACCOUNT,
            href="https://cloud.example.invalid/remote.php/dav/files/alice/private/x",
        )

    assert error.value.code == exits.SCOPE_DENIED
    assert fake.requests == []


def test_a_path_under_another_account_is_refused():
    profile = Profile(
        "home", "https://cloud.example.invalid", "pass", (),
        ("/remote.php/dav/files/bob/work/",),
    )
    with pytest.raises(shares.ShareError) as error:
        shares.ocs_path(
            profile,
            account_name=ACCOUNT,
            href="https://cloud.example.invalid/remote.php/dav/files/bob/work/x",
        )

    assert error.value.code == exits.SCOPE_DENIED


def test_planning_a_public_link_sends_nothing_and_names_what_it_grants():
    fake = FakeTransport([ocs_response([])])

    plan = shares.plan_create(
        PROFILE,
        session=session.Session(PROFILE, transport=fake),
        account_name=ACCOUNT,
        href=HREF,
        share_type="public_link",
    )

    step = plan.steps[0]
    assert step.action == "share.create"
    assert step.details["reach"] == "anyone holding the link"
    assert step.details["grants"] == ["read"]
    # The one request is the existing-share read the reconcile baseline needs.
    assert [item["method"] for item in fake.requests] == ["GET"]


def test_planning_an_update_names_what_it_gains_and_withdraws():
    gaining = FakeTransport([ocs_response(share_record(permissions=1))])
    plan = shares.plan_update(
        PROFILE,
        session=session.Session(PROFILE, transport=gaining),
        account_name=ACCOUNT,
        share_id="7",
        permissions="write",
    )

    assert plan.steps[0].details["gained"] == ["create", "delete", "update"]
    assert plan.steps[0].details["withdrawn"] == []
    assert [item["method"] for item in gaining.requests] == ["GET"]

    withdrawing = FakeTransport([ocs_response(share_record(permissions=31))])
    plan = shares.plan_update(
        PROFILE,
        session=session.Session(PROFILE, transport=withdrawing),
        account_name=ACCOUNT,
        share_id="7",
        permissions="read",
    )

    assert plan.steps[0].details["gained"] == []
    assert plan.steps[0].details["withdrawn"] == ["create", "delete", "share", "update"]
    assert [item["method"] for item in withdrawing.requests] == ["GET"]


def test_updating_sends_only_the_changed_fields():
    plan = shares.plan_update(
        PROFILE,
        session=transport_for(ocs_response(share_record())),
        account_name=ACCOUNT,
        share_id="7",
        note="recipient note",
        password="correct-horse",
    )
    assert "correct-horse" not in json.dumps(plan.steps[0].as_dict())
    fake = FakeTransport(
        [ocs_response(share_record(note="recipient note", password="protected"))]
    )

    shares.execute(
        PROFILE, session=session.Session(PROFILE, transport=fake), step=plan.steps[0]
    )

    body = fake.requests[0]["data"].decode()
    assert "note=recipient+note" in body
    assert "password=correct-horse" in body
    assert "permissions=" not in body


def test_an_update_that_changes_nothing_is_refused():
    with pytest.raises(shares.ShareError) as error:
        shares.plan_update(
            PROFILE,
            session=transport_for(ocs_response(share_record())),
            account_name=ACCOUNT,
            share_id="7",
            note="",
        )

    assert error.value.code == exits.USAGE


def test_an_update_of_an_out_of_scope_share_is_refused():
    with pytest.raises(shares.ShareError) as error:
        shares.plan_update(
            PROFILE,
            session=transport_for(
                ocs_response(share_record(path="/private/secret.txt"))
            ),
            account_name=ACCOUNT,
            share_id="7",
            note="private",
        )

    assert error.value.code == exits.SCOPE_DENIED


def test_an_update_whose_answer_was_lost_is_uncertain():
    plan = shares.plan_update(
        PROFILE,
        session=transport_for(ocs_response(share_record())),
        account_name=ACCOUNT,
        share_id="7",
        note="recipient note",
    )

    with plans.claim(plan.plan_id), pytest.raises(shares.ShareError) as error:
        plans.apply(
            PROFILE,
            session=transport_for(response(500)),
            plan=plan,
            dispatchers={
                "share.": plans.Dispatcher(
                    shares.validate_step, shares.execute, shares.reconcile
                )
            },
        )

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).progress[0].state == "uncertain"


def test_reconciling_an_update_reads_the_share_back():
    plan = shares.plan_update(
        PROFILE,
        session=transport_for(ocs_response(share_record(permissions=1))),
        account_name=ACCOUNT,
        share_id="7",
        permissions="write",
    )

    verified = shares.reconcile(
        PROFILE,
        session=transport_for(ocs_response(share_record(permissions=15))),
        step=plan.steps[0],
    )
    pending = shares.reconcile(
        PROFILE,
        session=transport_for(ocs_response(share_record(permissions=1))),
        step=plan.steps[0],
    )
    uncertain = shares.reconcile(
        PROFILE,
        session=transport_for(ocs_response(share_record(permissions=17))),
        step=plan.steps[0],
    )

    assert verified["state"] == "verified"
    assert verified["share"]["share_id"] == "7"
    assert pending["state"] == "pending"
    assert uncertain["state"] == "uncertain"


def test_applying_reports_permissions_the_server_widened_or_withheld():
    widening = shares.plan_update(
        PROFILE,
        session=transport_for(ocs_response(share_record(permissions=1))),
        account_name=ACCOUNT,
        share_id="7",
        permissions="write",
    )
    widened = shares.execute(
        PROFILE,
        session=transport_for(ocs_response(share_record(permissions=31))),
        step=widening.steps[0],
    )
    withholding = shares.plan_update(
        PROFILE,
        session=transport_for(ocs_response(share_record(permissions=31))),
        account_name=ACCOUNT,
        share_id="7",
        permissions="write",
    )
    withheld = shares.execute(
        PROFILE,
        session=transport_for(ocs_response(share_record(permissions=1))),
        step=withholding.steps[0],
    )

    assert widened["granted_beyond_plan"] == ["share"]
    assert widened["withheld_beyond_plan"] == []
    assert withheld["granted_beyond_plan"] == []
    assert withheld["withheld_beyond_plan"] == ["create", "delete", "update"]


def test_a_link_password_never_reaches_a_caller_visible_view():
    plan = shares.plan_create(
        PROFILE,
        session=transport_for(ocs_response([])),
        account_name=ACCOUNT,
        href=HREF,
        share_type="public_link",
        password="correct-horse",
    )

    view = json.dumps(plan.as_dict())
    assert "correct-horse" not in view
    # Its length is as revealing as any other property of a password.
    assert "payload_bytes" not in view
    assert plans.payload_bytes(plan.steps[0]) == b"correct-horse"


def test_a_public_link_refuses_a_recipient():
    with pytest.raises(shares.ShareError) as error:
        shares.plan_create(
            PROFILE,
            session=transport_for(),
            account_name=ACCOUNT,
            href=HREF,
            share_type="public_link",
            recipient="bob",
        )

    assert error.value.code == exits.USAGE


def test_a_user_share_requires_a_recipient():
    with pytest.raises(shares.ShareError) as error:
        shares.plan_create(
            PROFILE,
            session=transport_for(),
            account_name=ACCOUNT,
            href=HREF,
            share_type="user",
        )

    assert error.value.code == exits.USAGE


def test_a_past_expiry_is_refused():
    with pytest.raises(shares.ShareError) as error:
        shares.plan_create(
            PROFILE,
            session=transport_for(ocs_response([])),
            account_name=ACCOUNT,
            href=HREF,
            share_type="public_link",
            expires="2001-01-01",
        )

    assert error.value.code == exits.USAGE


def test_applying_reports_a_permission_the_server_granted_beyond_the_plan():
    """Nextcloud adds the share bit to every public link; the approval said read."""
    plan = shares.plan_create(
        PROFILE,
        session=transport_for(ocs_response([])),
        account_name=ACCOUNT,
        href=HREF,
        share_type="public_link",
    )
    result = shares.execute(
        PROFILE,
        session=transport_for(ocs_response(share_record())),
        step=plan.steps[0],
    )

    assert result["granted_beyond_plan"] == ["share"]
    assert result["share"]["url"] == "https://cloud.example.invalid/s/token"


def test_creating_sends_the_frozen_form_including_the_password():
    plan = shares.plan_create(
        PROFILE,
        session=transport_for(ocs_response([])),
        account_name=ACCOUNT,
        href=HREF,
        share_type="user",
        recipient="bob",
        permissions="write",
        password="s3cret",
    )
    fake = FakeTransport([ocs_response(share_record(share_type=0, share_with="bob"))])

    shares.execute(
        PROFILE, session=session.Session(PROFILE, transport=fake), step=plan.steps[0]
    )

    body = fake.requests[0]["data"].decode()
    assert "shareType=0" in body
    assert "shareWith=bob" in body
    assert "permissions=15" in body
    assert "password=s3cret" in body


def test_a_share_creation_whose_answer_was_lost_is_uncertain():
    plan = shares.plan_create(
        PROFILE,
        session=transport_for(ocs_response([])),
        account_name=ACCOUNT,
        href=HREF,
        share_type="public_link",
    )

    with plans.claim(plan.plan_id), pytest.raises(shares.ShareError) as error:
        plans.apply(
            PROFILE,
            session=transport_for(response(500)),
            plan=plan,
            dispatchers={
                "share.": plans.Dispatcher(
                    shares.validate_step, shares.execute, shares.reconcile
                )
            },
        )

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).progress[0].state == "uncertain"


def test_a_share_creation_the_server_refused_is_not_uncertain():
    plan = shares.plan_create(
        PROFILE,
        session=transport_for(ocs_response([])),
        account_name=ACCOUNT,
        href=HREF,
        share_type="public_link",
    )

    with plans.claim(plan.plan_id), pytest.raises(shares.ShareError) as error:
        plans.apply(
            PROFILE,
            session=transport_for(
                ocs_response([], http=403, status=403, message="forbidden")
            ),
            plan=plan,
            dispatchers={
                "share.": plans.Dispatcher(
                    shares.validate_step, shares.execute, shares.reconcile
                )
            },
        )

    assert error.value.code == exits.SCOPE_DENIED
    assert plans.read(plan.plan_id).progress[0].state == "pending"


def test_planning_a_revocation_requires_the_share_to_be_allowlisted():
    outside = share_record(id="77", path="/private/secret.txt")

    with pytest.raises(shares.ShareError, match="seen but not revoked") as error:
        shares.plan_delete(
            PROFILE,
            session=transport_for(ocs_response(outside)),
            account_name=ACCOUNT,
            share_id="77",
        )

    assert error.value.code == exits.SCOPE_DENIED


def test_planning_a_revocation_freezes_the_allowlisted_share():
    plan = shares.plan_delete(
        PROFILE,
        session=transport_for(ocs_response(share_record())),
        account_name=ACCOUNT,
        share_id="7",
    )

    assert plan.steps[0].action == "share.delete"
    assert plan.steps[0].href == HREF


def test_revoking_a_share_that_is_already_gone_is_the_outcome_it_wanted():
    step = plans.freeze_step(
        action="share.delete",
        href=HREF,
        etag="",
        summary="revoke",
        details={"share_id": "7"},
    )

    result = shares.execute(
        PROFILE,
        session=transport_for(ocs_response(None, http=404, status=404, message="not found")),
        step=step,
    )

    assert result["already_absent"] is True


def test_reconcile_identifies_the_share_this_plan_created():
    plan = shares.plan_create(
        PROFILE,
        session=transport_for(ocs_response([share_record(id="1")])),
        account_name=ACCOUNT,
        href=HREF,
        share_type="public_link",
    )

    outcome = shares.reconcile(
        PROFILE,
        session=transport_for(ocs_response([share_record(id="1"), share_record(id="2")])),
        step=plan.steps[0],
    )

    assert outcome["state"] == "verified"
    assert outcome["share"]["share_id"] == "2"


def test_reconcile_refuses_to_guess_between_two_new_shares():
    plan = shares.plan_create(
        PROFILE,
        session=transport_for(ocs_response([])),
        account_name=ACCOUNT,
        href=HREF,
        share_type="public_link",
    )

    with pytest.raises(shares.ShareError) as error:
        shares.reconcile(
            PROFILE,
            session=transport_for(ocs_response([share_record(id="1"), share_record(id="2")])),
            step=plan.steps[0],
        )

    assert error.value.code == exits.OUTCOME_UNCERTAIN


def test_reconciling_a_revocation_the_server_no_longer_knows_is_verified():
    step = plans.freeze_step(
        action="share.delete",
        href=HREF,
        etag="",
        summary="revoke",
        details={"share_id": "7"},
    )

    outcome = shares.reconcile(
        PROFILE,
        session=transport_for(ocs_response([], http=404, status=404, message="not found")),
        step=step,
    )

    assert outcome["state"] == "verified"
    assert outcome["revoked"] == "7"


def test_reconciling_a_revocation_still_present_is_pending():
    step = plans.freeze_step(
        action="share.delete",
        href=HREF,
        etag="",
        summary="revoke",
        details={"share_id": "7"},
    )

    outcome = shares.reconcile(
        PROFILE,
        session=transport_for(ocs_response([share_record()])),
        step=step,
    )

    assert outcome["state"] == "pending"
    assert outcome["share_id"] == "7"


def test_a_frozen_step_naming_an_uncreatable_share_type_is_stale():
    step = plans.freeze_step(
        action="share.create",
        href=HREF,
        etag="",
        summary="x",
        details={"share_type": "federated", "permissions": "read", "path": "/work/notes.md"},
    )

    with pytest.raises(plans.PlanError) as error:
        shares.validate_step(step)

    assert error.value.code == exits.PLAN_STALE


def test_the_cli_lists_shares(monkeypatch, tmp_path, capsys):
    directory = tmp_path / "xdg_config_home" / "ncl"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(CONFIG)
    transport = FakeTransport(
        [principal_response(), home_response(), ocs_response([share_record()])]
    )
    monkeypatch.setattr(session, "UrllibTransport", lambda: transport)

    code = cli.main(["share", "list", "--json"])

    assert code == exits.OK
    reported = json.loads(capsys.readouterr().out)["shares"]
    assert reported[0]["share_id"] == "7"
    assert reported[0]["permissions"] == ["read", "share"]
