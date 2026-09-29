"""Tags and comments, entirely offline.

What these hold to is that saying something about a file is a write like any
other: nothing reaches the server without a plan, the preview names the tag and
the file because a tag is visible to everyone who can see the file, and a tag
creation says that it cannot be taken back.
"""

from __future__ import annotations

import datetime as dt
import json
from email.utils import format_datetime

import pytest
from test_auth import FakeTransport, home_response, principal_response, response
from test_files import PROFILE, ROOT

from ncl import annotations, cli, exits, files, plans, secrets, session

FILE = ROOT + "notes.md"
FILE_ID = "42"
CONFIG = f"""
default_profile = "home"

[profiles.home]
origin = "https://cloud.example.invalid"
secret_backend = "pass"
calendars = []
files_roots = ["{ROOT}"]
"""


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setattr(
        secrets,
        "load_credential",
        lambda profile: secrets.Credential("alice", "app-password"),
    )


def transport_for(*responses):
    fake = FakeTransport(list(responses))
    return session.Session(PROFILE, transport=fake), fake


def multistatus(*responses: str) -> bytes:
    return (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        'xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">'
        + "".join(responses)
        + "</d:multistatus>"
    ).encode()


def absent(href: str, properties: str) -> str:
    """The propstat a collection answers with for properties only its children carry."""
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{properties}</d:prop>"
        "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>"
    )


def tag_entry(
    identifier: str,
    name: str,
    *,
    visible: str = "true",
    assignable: str = "true",
    can_assign: str = "true",
) -> str:
    return f"""<d:response><d:href>/remote.php/dav/systemtags/{identifier}/</d:href>
      <d:propstat><d:prop>
        <oc:id>{identifier}</oc:id><oc:display-name>{name}</oc:display-name>
        <oc:user-visible>{visible}</oc:user-visible>
        <oc:user-assignable>{assignable}</oc:user-assignable>
        <oc:can-assign>{can_assign}</oc:can-assign>
      </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""


def tag_listing(*entries: str) -> bytes:
    return multistatus(
        absent(
            "/remote.php/dav/systemtags/",
            "<oc:id/><oc:display-name/><oc:user-visible/>"
            "<oc:user-assignable/><oc:can-assign/>",
        ),
        *entries,
    )


def file_tag_response(*tags: tuple[str, str]) -> bytes:
    carried = "".join(
        f'<nc:system-tag oc:id="{identifier}" oc:user-visible="true" '
        f'oc:user-assignable="true" oc:can-assign="true">{name}</nc:system-tag>'
        for identifier, name in tags
    )
    return multistatus(
        f"""<d:response><d:href>/remote.php/dav/files/alice/Violentmonkey/notes.md</d:href>
        <d:propstat><d:prop><nc:system-tags>{carried}</nc:system-tags></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""
    )


def fileid_response(identifier: str = FILE_ID) -> bytes:
    return multistatus(
        f"""<d:response><d:href>/remote.php/dav/files/alice/Violentmonkey/notes.md</d:href>
        <d:propstat><d:prop><oc:fileid>{identifier}</oc:fileid></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""
    )


def comment_entry(
    identifier: str,
    message: str,
    *,
    actor: str = "alice",
    created: str = "Thu, 03 Sep 2026 10:11:12 GMT",
    unread: str = "false",
) -> str:
    return f"""<d:response>
      <d:href>/remote.php/dav/comments/files/{FILE_ID}/{identifier}</d:href>
      <d:propstat><d:prop>
        <oc:id>{identifier}</oc:id><oc:message>{message}</oc:message>
        <oc:actorId>{actor}</oc:actorId><oc:actorDisplayName>Alice</oc:actorDisplayName>
        <oc:creationDateTime>{created}</oc:creationDateTime>
        <oc:verb>comment</oc:verb><oc:isUnread>{unread}</oc:isUnread>
      </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""


def comment_listing(*entries: str) -> bytes:
    return multistatus(
        absent(
            f"/remote.php/dav/comments/files/{FILE_ID}/",
            "<oc:id/><oc:message/><oc:actorId/><oc:actorDisplayName/>"
            "<oc:creationDateTime/><oc:verb/><oc:isUnread/>",
        ),
        *entries,
    )


def _apply(plan, transport):
    with plans.claim(plan.plan_id):
        return plans.apply(
            PROFILE, session=transport, plan=plan, dispatchers=cli._dispatchers()
        )


def _reconcile(plan, transport):
    with plans.claim(plan.plan_id):
        return plans.reconcile(
            PROFILE, session=transport, plan=plan, dispatchers=cli._dispatchers()
        )


def test_a_tag_listing_reads_each_tag_and_skips_the_collection_itself():
    transport, fake = transport_for(
        response(207, tag_listing(tag_entry("3", "review"), tag_entry("9", "draft")))
    )

    found = annotations.list_tags(PROFILE, session=transport)

    assert fake.requests[0]["method"] == "PROPFIND"
    assert fake.requests[0]["headers"]["Depth"] == "1"
    assert [(item.id, item.name) for item in found] == [("9", "draft"), ("3", "review")]
    assert found[0].user_visible is True
    assert found[0].can_assign is True


def test_a_tag_without_an_identifier_is_a_malformed_response():
    body = tag_listing(
        """<d:response><d:href>/remote.php/dav/systemtags/3/</d:href><d:propstat>
        <d:prop><oc:display-name>review</oc:display-name></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""
    )
    transport, _fake = transport_for(response(207, body))

    with pytest.raises(annotations.AnnotationError, match="malformed tag identifier") as error:
        annotations.list_tags(PROFILE, session=transport)

    assert error.value.code == exits.MALFORMED_RESPONSE


def test_a_propstat_without_a_readable_status_is_refused():
    body = multistatus(
        """<d:response><d:href>/remote.php/dav/systemtags/3/</d:href><d:propstat>
        <d:prop><oc:id>3</oc:id></d:prop><d:status>nonsense</d:status>
        </d:propstat></d:response>"""
    )
    transport, _fake = transport_for(response(207, body))

    with pytest.raises(annotations.AnnotationError, match="malformed propstat status"):
        annotations.list_tags(PROFILE, session=transport)


def test_the_tags_a_file_carries_are_read_from_its_own_property():
    transport, fake = transport_for(response(207, file_tag_response(("3", "review"))))

    found = annotations.file_tags(PROFILE, session=transport, href=FILE)

    assert fake.requests[0]["headers"]["Depth"] == "0"
    assert b"system-tags" in fake.requests[0]["data"]
    assert [(item.id, item.name) for item in found] == [("3", "review")]
    assert found[0].user_assignable is True


def test_a_file_outside_the_allowlist_is_refused_before_any_request():
    transport, fake = transport_for()

    with pytest.raises(files.FileError) as error:
        annotations.file_tags(
            PROFILE, session=transport, href=ROOT.replace("Violentmonkey", "elsewhere")
        )

    assert error.value.code == exits.SCOPE_DENIED
    assert fake.requests == []


def test_a_comment_listing_reads_the_message_actor_and_date():
    transport, fake = transport_for(
        response(207, fileid_response()),
        response(207, comment_listing(comment_entry("7", "looks good"))),
    )

    found = annotations.list_comments(PROFILE, session=transport, href=FILE)

    assert fake.requests[1]["url"].endswith(f"/remote.php/dav/comments/files/{FILE_ID}/")
    assert len(found) == 1
    assert found[0].id == "7"
    assert found[0].message == "looks good"
    assert found[0].actor_id == "alice"
    assert found[0].created == "2026-09-03T10:11:12+00:00"
    assert found[0].unread is False


def test_a_comment_with_an_unreadable_date_is_a_malformed_response():
    transport, _fake = transport_for(
        response(207, fileid_response()),
        response(207, comment_listing(comment_entry("7", "hi", created="not a date"))),
    )

    with pytest.raises(annotations.AnnotationError, match="malformed comment date"):
        annotations.list_comments(PROFILE, session=transport, href=FILE)


def test_creating_a_tag_is_planned_as_irreversible_and_warns_before_it_is_approved(capsys):
    plan = annotations.plan_create_tag(PROFILE, name="review")

    step = plan.steps[0]
    assert step.action == "tag.create"
    assert step.href.endswith("/remote.php/dav/systemtags/")
    assert step.details["irreversible"] is True
    assert step.details["warning"] == annotations.TAG_WARNING
    assert cli._emit_plan(plan, False) == exits.CONFIRMATION_REQUIRED
    assert annotations.TAG_WARNING in capsys.readouterr().out


def test_an_assignment_plan_resolves_the_tag_by_exact_name_and_names_its_reach():
    transport, fake = transport_for(
        response(207, fileid_response()),
        response(207, tag_listing(tag_entry("3", "review"), tag_entry("9", "reviewed"))),
        response(207, file_tag_response()),
    )

    plan = annotations.plan_tag(PROFILE, session=transport, href=FILE, tag="review")

    assert [item["method"] for item in fake.requests] == ["PROPFIND"] * 3
    step = plan.steps[0]
    assert step.action == "tag.assign"
    assert step.href == FILE
    assert step.details == {
        "file_id": FILE_ID,
        "tag_id": "3",
        "tag_name": "review",
        "reach": annotations.TAG_REACH,
    }


def test_an_unknown_tag_name_is_refused_rather_than_created_on_the_way_past():
    transport, _fake = transport_for(
        response(207, fileid_response()),
        response(207, tag_listing(tag_entry("3", "review"))),
    )

    with pytest.raises(annotations.AnnotationError, match="no tag is named") as error:
        annotations.plan_tag(PROFILE, session=transport, href=FILE, tag="missing")

    assert error.value.code == exits.TARGET_NOT_FOUND


def test_two_tags_sharing_a_name_cannot_be_told_apart_by_name():
    transport, _fake = transport_for(
        response(207, fileid_response()),
        response(207, tag_listing(tag_entry("3", "review"), tag_entry("4", "review"))),
    )

    with pytest.raises(annotations.AnnotationError, match="more than one tag") as error:
        annotations.plan_tag(PROFILE, session=transport, href=FILE, tag="review")

    assert error.value.code == exits.AMBIGUOUS_TARGET


def test_a_tag_the_file_already_carries_is_refused_at_planning():
    transport, _fake = transport_for(
        response(207, fileid_response()),
        response(207, tag_listing(tag_entry("3", "review"))),
        response(207, file_tag_response(("3", "review"))),
    )

    with pytest.raises(annotations.AnnotationError, match="already carries") as error:
        annotations.plan_tag(PROFILE, session=transport, href=FILE, tag="review")

    assert error.value.code == exits.USAGE


def test_a_tag_the_file_does_not_carry_cannot_be_removed_from_it():
    transport, _fake = transport_for(
        response(207, fileid_response()),
        response(207, tag_listing(tag_entry("3", "review"))),
        response(207, file_tag_response()),
    )

    with pytest.raises(annotations.AnnotationError, match="does not carry") as error:
        annotations.plan_untag(PROFILE, session=transport, href=FILE, tag="review")

    assert error.value.code == exits.USAGE


def test_applying_an_assignment_puts_the_relation_and_verifies_by_reading_the_file_back():
    planning, _fake = transport_for(
        response(207, fileid_response()),
        response(207, tag_listing(tag_entry("3", "review"))),
        response(207, file_tag_response()),
    )
    plan = annotations.plan_tag(PROFILE, session=planning, href=FILE, tag="review")
    applying, fake = transport_for(
        response(201), response(207, file_tag_response(("3", "review")))
    )

    result = _apply(plan, applying)

    assert fake.requests[0]["method"] == "PUT"
    assert fake.requests[0]["url"].endswith(
        f"/remote.php/dav/systemtags-relations/files/{FILE_ID}/3"
    )
    assert result["tag"] == {"id": "3", "name": "review"}


def test_an_assignment_the_file_does_not_read_back_with_is_uncertain():
    planning, _fake = transport_for(
        response(207, fileid_response()),
        response(207, tag_listing(tag_entry("3", "review"))),
        response(207, file_tag_response()),
    )
    plan = annotations.plan_tag(PROFILE, session=planning, href=FILE, tag="review")
    applying, _applied = transport_for(response(201), response(207, file_tag_response()))

    with pytest.raises(annotations.AnnotationError, match="does not read back") as error:
        _apply(plan, applying)

    assert error.value.code == exits.OUTCOME_UNCERTAIN


def test_applying_a_removal_deletes_the_relation_and_verifies_the_tag_is_gone():
    planning, _fake = transport_for(
        response(207, fileid_response()),
        response(207, tag_listing(tag_entry("3", "review"))),
        response(207, file_tag_response(("3", "review"))),
    )
    plan = annotations.plan_untag(PROFILE, session=planning, href=FILE, tag="review")
    applying, fake = transport_for(response(204), response(207, file_tag_response()))

    result = _apply(plan, applying)

    assert fake.requests[0]["method"] == "DELETE"
    assert fake.requests[0]["url"].endswith(
        f"/remote.php/dav/systemtags-relations/files/{FILE_ID}/3"
    )
    assert result["verified"] is True


def test_applying_a_tag_creation_reports_the_tag_the_server_says_it_made():
    plan = annotations.plan_create_tag(PROFILE, name="review")
    applying, fake = transport_for(
        response(201, headers={"Content-Location": "/remote.php/dav/systemtags/12"})
    )

    result = _apply(plan, applying)

    assert fake.requests[0]["method"] == "POST"
    assert json.loads(fake.requests[0]["data"]) == {
        "name": "review",
        "userVisible": True,
        "userAssignable": True,
    }
    assert result["tag"] == {"id": "12", "name": "review"}


def test_a_creation_the_server_does_not_locate_is_uncertain_rather_than_done():
    plan = annotations.plan_create_tag(PROFILE, name="review")
    applying, _fake = transport_for(response(201))

    with pytest.raises(annotations.AnnotationError, match="did not say which tag") as error:
        _apply(plan, applying)

    assert error.value.code == exits.OUTCOME_UNCERTAIN


def test_posting_a_comment_sends_the_message_and_reads_the_new_comment_back():
    planning, _fake = transport_for(response(207, fileid_response()))
    plan = annotations.plan_comment(
        PROFILE, session=planning, href=FILE, message="looks good"
    )
    applying, fake = transport_for(
        response(201, headers={"Content-Location": f"/remote.php/dav/comments/files/{FILE_ID}/7"}),
        response(207, comment_listing(comment_entry("7", "looks good"))),
    )

    result = _apply(plan, applying)

    assert fake.requests[0]["method"] == "POST"
    assert json.loads(fake.requests[0]["data"]) == {
        "actorType": "users",
        "verb": "comment",
        "message": "looks good",
    }
    assert result["comment"]["id"] == "7"


def test_a_comment_whose_answer_is_lost_is_uncertain_rather_than_retried():
    planning, _fake = transport_for(response(207, fileid_response()))
    plan = annotations.plan_comment(
        PROFILE, session=planning, href=FILE, message="looks good"
    )
    applying, _fake = transport_for(
        session.SessionError("the configured origin was unreachable", exits.UNREACHABLE)
    )

    with pytest.raises(annotations.AnnotationError, match="answer was lost") as error:
        _apply(plan, applying)

    assert error.value.code == exits.OUTCOME_UNCERTAIN


def test_a_comment_by_another_actor_cannot_be_planned_for_removal():
    transport, _fake = transport_for(
        response(207, fileid_response()),
        response(207, comment_listing(comment_entry("7", "mine", actor="bob"))),
        principal_response(),
        home_response(),
    )

    with pytest.raises(annotations.AnnotationError, match="only its author") as error:
        annotations.plan_uncomment(PROFILE, session=transport, href=FILE, comment_id="7")

    assert error.value.code == exits.SCOPE_DENIED


def test_removing_a_comment_previews_what_it_says_and_verifies_its_absence():
    planning, _fake = transport_for(
        response(207, fileid_response()),
        response(207, comment_listing(comment_entry("7", "looks good"))),
        principal_response(),
        home_response(),
    )
    plan = annotations.plan_uncomment(
        PROFILE, session=planning, href=FILE, comment_id="7"
    )
    assert plan.steps[0].details["message"] == "looks good"
    applying, fake = transport_for(response(204), response(207, comment_listing()))

    result = _apply(plan, applying)

    assert fake.requests[0]["method"] == "DELETE"
    assert fake.requests[0]["url"].endswith(f"/remote.php/dav/comments/files/{FILE_ID}/7")
    assert result["verified"] == "removed"


def test_reconciling_a_tag_creation_settles_on_how_many_tags_carry_the_name():
    plan = annotations.plan_create_tag(PROFILE, name="review")
    step = plan.steps[0]

    pending, _fake = transport_for(response(207, tag_listing()))
    assert annotations.reconcile(PROFILE, session=pending, step=step)["state"] == "pending"

    verified, _fake = transport_for(response(207, tag_listing(tag_entry("3", "review"))))
    assert annotations.reconcile(PROFILE, session=verified, step=step)["state"] == "verified"

    several, _fake = transport_for(
        response(207, tag_listing(tag_entry("3", "review"), tag_entry("4", "review")))
    )
    assert annotations.reconcile(PROFILE, session=several, step=step)["state"] == "uncertain"


def test_reconciling_an_assignment_reads_the_files_own_tags():
    planning, _fake = transport_for(
        response(207, fileid_response()),
        response(207, tag_listing(tag_entry("3", "review"))),
        response(207, file_tag_response()),
    )
    plan = annotations.plan_tag(PROFILE, session=planning, href=FILE, tag="review")
    step = plan.steps[0]

    pending, _fake = transport_for(response(207, file_tag_response()))
    assert annotations.reconcile(PROFILE, session=pending, step=step)["state"] == "pending"

    verified, _fake = transport_for(response(207, file_tag_response(("3", "review"))))
    assert annotations.reconcile(PROFILE, session=verified, step=step)["state"] == "verified"


def test_reconciling_a_removal_settles_on_the_comment_still_being_there():
    planning, _fake = transport_for(
        response(207, fileid_response()),
        response(207, comment_listing(comment_entry("7", "looks good"))),
        principal_response(),
        home_response(),
    )
    plan = annotations.plan_uncomment(PROFILE, session=planning, href=FILE, comment_id="7")
    step = plan.steps[0]

    pending, _fake = transport_for(
        response(207, comment_listing(comment_entry("7", "looks good")))
    )
    assert annotations.reconcile(PROFILE, session=pending, step=step)["state"] == "pending"

    verified, _fake = transport_for(response(207, comment_listing()))
    assert annotations.reconcile(PROFILE, session=verified, step=step)["state"] == "verified"


def test_reconciling_a_posted_comment_counts_only_this_accounts_matching_message():
    planning, _fake = transport_for(response(207, fileid_response()))
    plan = annotations.plan_comment(
        PROFILE, session=planning, href=FILE, message="looks good"
    )
    step = plan.steps[0]
    floor = dt.datetime.fromisoformat(step.details["planned_at"])
    earlier = format_datetime(floor - dt.timedelta(seconds=1), usegmt=True)
    later = format_datetime(floor + dt.timedelta(seconds=1), usegmt=True)

    pending, _fake = transport_for(
        response(207, comment_listing()), principal_response(), home_response()
    )
    assert annotations.reconcile(PROFILE, session=pending, step=step)["state"] == "pending"

    stale, _fake = transport_for(
        response(207, comment_listing(comment_entry("6", "looks good", created=earlier))),
        principal_response(),
        home_response(),
    )
    assert annotations.reconcile(PROFILE, session=stale, step=step)["state"] == "pending"

    verified, _fake = transport_for(
        response(207, comment_listing(comment_entry("7", "looks good", created=later))),
        principal_response(),
        home_response(),
    )
    assert annotations.reconcile(PROFILE, session=verified, step=step)["state"] == "verified"

    other, _fake = transport_for(
        response(
            207,
            comment_listing(comment_entry("7", "looks good", actor="bob", created=later)),
        ),
        principal_response(),
        home_response(),
    )
    assert annotations.reconcile(PROFILE, session=other, step=step)["state"] == "pending"


def test_the_cli_lists_the_instances_tags(monkeypatch, capsys):
    transport = session.Session(
        PROFILE, transport=FakeTransport([response(207, tag_listing(tag_entry("3", "review")))])
    )
    monkeypatch.setattr(cli, "_selected_profile", lambda args: PROFILE)
    monkeypatch.setattr(cli.session, "Session", lambda profile: transport)

    code = cli.main(["--json", "tag", "list"])

    assert code == exits.OK
    reported = json.loads(capsys.readouterr().out)["tags"]
    assert reported == [
        {
            "id": "3",
            "name": "review",
            "user_visible": True,
            "user_assignable": True,
            "can_assign": True,
        }
    ]
