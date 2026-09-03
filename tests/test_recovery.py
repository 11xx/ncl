"""Trash and versions, entirely offline.

The allowlist question here is the inverted one, and getting it backwards is
the whole risk: a trash entry's own href is under `/trashbin/`, which nothing
allowlists, so what has to be bounded is where the file came from and where a
restore would put it back.
"""

from __future__ import annotations

import pytest
from test_auth import FakeTransport, response

from ncl import exits, files, plans, recovery, secrets, session
from ncl.config import Profile

PROFILE = Profile(
    "home",
    "https://cloud.example.invalid",
    "pass",
    (),
    ("/remote.php/dav/files/alice/work/",),
)
ACCOUNT = "alice"
TRASH = "https://cloud.example.invalid/remote.php/dav/trashbin/alice/trash/"
ENTRY = TRASH + "notes.md.d1788000000"
ORIGINAL = "https://cloud.example.invalid/remote.php/dav/files/alice/work/notes.md"


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setattr(
        secrets, "load_credential", lambda profile: secrets.Credential("alice", "app-password")
    )


def multistatus(*entries: str) -> bytes:
    return (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        'xmlns:nc="http://nextcloud.org/ns" xmlns:oc="http://owncloud.org/ns">'
        + "".join(entries)
        + "</d:multistatus>"
    ).encode()


def trash_entry(
    href: str = ENTRY,
    location: str = "work/notes.md",
    name: str = "notes.md",
    deleted: str = "1788000000",
    size: str = "12",
    file_id: str = "340",
) -> str:
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
        f"<d:resourcetype/><d:getcontentlength>{size}</d:getcontentlength>"
        f"<oc:fileid>{file_id}</oc:fileid>"
        f"<nc:trashbin-filename>{name}</nc:trashbin-filename>"
        f"<nc:trashbin-original-location>{location}</nc:trashbin-original-location>"
        f"<nc:trashbin-deletion-time>{deleted}</nc:trashbin-deletion-time>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def file_entry(href: str, size: str = "12") -> str:
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
        f"<d:resourcetype/><d:getcontentlength>{size}</d:getcontentlength>"
        '<d:getlastmodified>Wed, 19 Aug 2026 20:00:00 GMT</d:getlastmodified>'
        '<d:getetag>"v1"</d:getetag><d:getcontenttype>text/markdown</d:getcontenttype>'
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def transport_for(*responses):
    fake = FakeTransport(list(responses))
    return session.Session(PROFILE, transport=fake), fake


def test_a_trash_entry_reports_where_it_came_from():
    transport, _ = transport_for(response(207, multistatus(trash_entry())))

    entries = recovery.list_trash(PROFILE, session=transport, account_name=ACCOUNT)

    assert len(entries) == 1
    assert entries[0].original_location == "work/notes.md"
    assert entries[0].original_href == ORIGINAL
    assert entries[0].deleted_at == "2026-08-29T10:40:00+00:00"
    assert entries[0].in_scope is True


def test_an_entry_without_an_identifier_is_listed_and_refused_only_at_the_restore():
    """The same rule as an out-of-scope entry: sight is a read, the restore is gated.

    A restore is verified by identity, so an entry carrying none cannot be
    restored — but refusing it while listing loses every other entry in the
    bin, which is the one place a person looks for what they deleted.
    """
    nameless = trash_entry(
        href=TRASH + "old.md.d1788000002", location="work/old.md", name="old.md", file_id=""
    )
    transport, _ = transport_for(response(207, multistatus(trash_entry(), nameless)))

    entries = recovery.list_trash(PROFILE, session=transport, account_name=ACCOUNT)

    assert len(entries) == 2
    assert [entry.file_id for entry in entries if entry.name == "old.md"] == [""]

    transport, _ = transport_for(response(207, multistatus(trash_entry(), nameless)))
    with pytest.raises(files.FileError, match="carries no file identifier"):
        recovery.plan_restore(
            PROFILE,
            session=transport,
            account_name=ACCOUNT,
            href=TRASH + "old.md.d1788000002",
        )


def test_the_bin_lists_entries_it_may_not_restore():
    """Seeing what was deleted is a read; the allowlist gates the restore, not the sight."""
    outside = trash_entry(
        href=TRASH + "secret.md.d1788000001", location="private/secret.md", name="secret.md"
    )
    transport, _ = transport_for(response(207, multistatus(trash_entry(), outside)))

    entries = recovery.list_trash(PROFILE, session=transport, account_name=ACCOUNT)

    assert [item.in_scope for item in entries] == [False, True]


def test_restoring_an_entry_from_outside_the_allowlist_is_refused():
    outside = trash_entry(location="private/secret.md", name="secret.md")
    transport, fake = transport_for(response(207, multistatus(outside)))

    with pytest.raises(files.FileError, match="seen but not restored") as error:
        recovery.plan_restore(
            PROFILE, session=transport, account_name=ACCOUNT, href=ENTRY
        )

    assert error.value.code == exits.SCOPE_DENIED
    assert [item["method"] for item in fake.requests] == ["PROPFIND"]


def test_restoring_onto_an_occupied_path_is_refused_before_anything_is_sent():
    """The server renames rather than overwrites, so the plan could not keep its promise."""
    transport, fake = transport_for(
        response(207, multistatus(trash_entry())),
        response(207, multistatus(file_entry(ORIGINAL))),
    )

    with pytest.raises(files.FileError, match="is occupied") as error:
        recovery.plan_restore(
            PROFILE, session=transport, account_name=ACCOUNT, href=ENTRY
        )

    assert error.value.code == exits.CONFLICT
    assert "MOVE" not in [item["method"] for item in fake.requests]


def test_a_restore_omits_overwrite_because_the_endpoint_always_reports_existing():
    transport, _ = transport_for(
        response(207, multistatus(trash_entry())),
        response(404),
    )
    plan = recovery.plan_restore(
        PROFILE, session=transport, account_name=ACCOUNT, href=ENTRY
    )
    executing, calls = transport_for(
        response(404), response(201), response(207, fileid_response())
    )

    result = recovery.execute(PROFILE, session=executing, step=plan.steps[0])

    move = calls.requests[1]
    assert move["method"] == "MOVE"
    assert "Overwrite" not in move["headers"]
    assert move["headers"]["Destination"].endswith("/trashbin/alice/restore/notes.md")
    assert result["restored"] == ORIGINAL


def test_a_restore_the_server_accepted_but_cannot_be_found_is_uncertain():
    transport, _ = transport_for(
        response(207, multistatus(trash_entry())), response(404)
    )
    plan = recovery.plan_restore(
        PROFILE, session=transport, account_name=ACCOUNT, href=ENTRY
    )
    executing, _ = transport_for(response(404), response(201), response(404))

    with pytest.raises(files.FileError) as error:
        recovery.execute(PROFILE, session=executing, step=plan.steps[0])

    assert error.value.code == exits.OUTCOME_UNCERTAIN


def test_a_restore_rechecks_occupancy_immediately_before_the_move():
    transport, _ = transport_for(
        response(207, multistatus(trash_entry())), response(404)
    )
    plan = recovery.plan_restore(
        PROFILE, session=transport, account_name=ACCOUNT, href=ENTRY
    )
    executing, calls = transport_for(response(207, multistatus(file_entry(ORIGINAL))))

    with pytest.raises(files.FileError, match="became occupied") as error:
        recovery.execute(PROFILE, session=executing, step=plan.steps[0])

    assert error.value.code == exits.CONFLICT
    assert [item["method"] for item in calls.requests] == ["PROPFIND"]


def test_a_restore_does_not_verify_an_interloper_that_won_the_final_race():
    transport, _ = transport_for(
        response(207, multistatus(trash_entry())), response(404)
    )
    plan = recovery.plan_restore(
        PROFILE, session=transport, account_name=ACCOUNT, href=ENTRY
    )
    wrong_identity = fileid_response("999")
    executing, _ = transport_for(
        response(404), response(201), response(207, wrong_identity)
    )

    with pytest.raises(files.FileError, match="different resource") as error:
        recovery.execute(PROFILE, session=executing, step=plan.steps[0])

    assert error.value.code == exits.OUTCOME_UNCERTAIN


def test_reconciling_a_trash_restore_requires_the_frozen_file_identity():
    transport, _ = transport_for(
        response(207, multistatus(trash_entry())), response(404)
    )
    plan = recovery.plan_restore(
        PROFILE, session=transport, account_name=ACCOUNT, href=ENTRY
    )
    reconciling, _ = transport_for(
        response(404), response(207, fileid_response("999"))
    )

    outcome = recovery.reconcile(
        PROFILE, session=reconciling, step=plan.steps[0]
    )

    assert outcome["state"] == "uncertain"


def test_a_purge_records_that_it_cannot_be_taken_back():
    transport, _ = transport_for(response(207, multistatus(trash_entry())))

    plan = recovery.plan_purge(PROFILE, session=transport, account_name=ACCOUNT, href=ENTRY)

    assert plan.steps[0].action == "trash.purge"
    assert plan.steps[0].details["irreversible"] is True
    assert "permanently destroy" in plan.summary


def test_a_purge_step_that_does_not_record_irreversibility_is_stale():
    step = plans.freeze_step(
        action="trash.purge", href=ENTRY, etag="", summary="x", details={}
    )

    with pytest.raises(plans.PlanError, match="irreversible"):
        recovery.validate_step(step)


def test_purging_something_already_gone_is_the_outcome_it_wanted():
    step = plans.freeze_step(
        action="trash.purge",
        href=ENTRY,
        etag="",
        summary="x",
        details={"irreversible": True},
    )
    transport, _ = transport_for(response(404))

    assert recovery.execute(PROFILE, session=transport, step=step)["verified"] == (
        "already absent"
    )


def test_a_malformed_deletion_time_is_refused():
    transport, _ = transport_for(
        response(207, multistatus(trash_entry(deleted="not-a-time")))
    )

    with pytest.raises(files.FileError, match="malformed deletion time"):
        recovery.list_trash(PROFILE, session=transport, account_name=ACCOUNT)


def test_an_entry_without_an_original_location_is_malformed():
    entry = (
        f"<d:response><d:href>{ENTRY}</d:href><d:propstat><d:prop>"
        "<d:resourcetype/><nc:trashbin-filename>notes.md</nc:trashbin-filename>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )
    transport, _ = transport_for(response(207, multistatus(entry)))

    with pytest.raises(files.FileError, match="no original location"):
        recovery.list_trash(PROFILE, session=transport, account_name=ACCOUNT)


def test_a_server_without_a_trash_bin_says_so():
    transport, _ = transport_for(response(404))

    with pytest.raises(files.FileError, match="no trash bin") as error:
        recovery.list_trash(PROFILE, session=transport, account_name=ACCOUNT)

    assert error.value.code == exits.UNSUPPORTED_COLLECTION


# --- versions ----------------------------------------------------------------


def fileid_response(value: str = "340") -> bytes:
    return multistatus(
        f"<d:response><d:href>{ORIGINAL}</d:href><d:propstat><d:prop>"
        f"<oc:fileid>{value}</oc:fileid>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def version_entry(version_id: str, size: str = "12", label: str = "") -> str:
    base = "https://cloud.example.invalid/remote.php/dav/versions/alice/versions/340/"
    labelled = f"<nc:version-label>{label}</nc:version-label>" if label else ""
    return (
        f"<d:response><d:href>{base}{version_id}</d:href><d:propstat><d:prop>"
        f"<d:getcontentlength>{size}</d:getcontentlength>"
        "<d:getlastmodified>Wed, 19 Aug 2026 20:00:00 GMT</d:getlastmodified>"
        f"{labelled}"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def test_versions_are_keyed_by_the_servers_file_identifier():
    transport, fake = transport_for(
        response(207, fileid_response()),
        response(207, multistatus(version_entry("1788000001"), version_entry("1788000002"))),
    )

    found = recovery.list_versions(
        PROFILE, session=transport, account_name=ACCOUNT, file_href=ORIGINAL
    )

    assert [item.version_id for item in found] == ["1788000002", "1788000001"]
    assert "/versions/alice/versions/340/" in fake.requests[1]["url"]


def test_a_file_with_no_retained_versions_lists_none():
    transport, _ = transport_for(response(207, fileid_response()), response(404))

    assert (
        recovery.list_versions(
            PROFILE, session=transport, account_name=ACCOUNT, file_href=ORIGINAL
        )
        == []
    )


def test_a_file_outside_the_allowlist_has_no_versions_read():
    transport, fake = transport_for()

    with pytest.raises(files.FileError) as error:
        recovery.list_versions(
            PROFILE,
            session=transport,
            account_name=ACCOUNT,
            file_href="https://cloud.example.invalid/remote.php/dav/files/alice/other/x",
        )

    assert error.value.code == exits.SCOPE_DENIED
    assert fake.requests == []


def test_restoring_an_unknown_version_is_not_found():
    transport, _ = transport_for(
        response(207, fileid_response()),
        response(207, multistatus(version_entry("1788000001"))),
    )

    with pytest.raises(files.FileError) as error:
        recovery.plan_version_restore(
            PROFILE,
            session=transport,
            account_name=ACCOUNT,
            file_href=ORIGINAL,
            version_id="9999",
        )

    assert error.value.code == exits.TARGET_NOT_FOUND


def test_a_version_restore_names_the_revision_that_would_win():
    transport, _ = transport_for(
        response(207, fileid_response()),
        response(207, multistatus(version_entry("1788000001"))),
    )

    plan = recovery.plan_version_restore(
        PROFILE,
        session=transport,
        account_name=ACCOUNT,
        file_href=ORIGINAL,
        version_id="1788000001",
    )

    assert plan.steps[0].action == "version.restore"
    assert "1788000001" in plan.summary
    assert plan.steps[0].details["file_href"] == ORIGINAL


def test_a_version_restore_reads_the_file_back_at_the_planned_size():
    planning, _ = transport_for(
        response(207, fileid_response()),
        response(207, multistatus(version_entry("1788000001"))),
    )
    plan = recovery.plan_version_restore(
        PROFILE,
        session=planning,
        account_name=ACCOUNT,
        file_href=ORIGINAL,
        version_id="1788000001",
    )
    executing, _ = transport_for(
        response(201), response(207, multistatus(file_entry(ORIGINAL, size="12")))
    )

    result = recovery.execute(PROFILE, session=executing, step=plan.steps[0])

    assert result["verified"] is True
    assert result["size"] == 12


def test_a_version_restore_whose_readback_disagrees_is_uncertain():
    planning, _ = transport_for(
        response(207, fileid_response()),
        response(207, multistatus(version_entry("1788000001"))),
    )
    plan = recovery.plan_version_restore(
        PROFILE,
        session=planning,
        account_name=ACCOUNT,
        file_href=ORIGINAL,
        version_id="1788000001",
    )
    executing, _ = transport_for(
        response(201), response(207, multistatus(file_entry(ORIGINAL, size="13")))
    )

    with pytest.raises(files.FileError) as error:
        recovery.execute(PROFILE, session=executing, step=plan.steps[0])

    assert error.value.code == exits.OUTCOME_UNCERTAIN


def test_reconciling_a_version_restore_checks_the_file():
    planning, _ = transport_for(
        response(207, fileid_response()),
        response(207, multistatus(version_entry("1788000001"))),
    )
    step = recovery.plan_version_restore(
        PROFILE,
        session=planning,
        account_name=ACCOUNT,
        file_href=ORIGINAL,
        version_id="1788000001",
    ).steps[0]
    matching, _ = transport_for(
        response(404), response(207, multistatus(file_entry(ORIGINAL, size="12")))
    )
    disagreeing, _ = transport_for(
        response(404), response(207, multistatus(file_entry(ORIGINAL, size="13")))
    )

    assert recovery.reconcile(PROFILE, session=matching, step=step)["state"] == "verified"
    assert recovery.reconcile(PROFILE, session=disagreeing, step=step)["state"] == "uncertain"
