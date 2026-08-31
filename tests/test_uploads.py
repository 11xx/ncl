"""Streamed writes and ranged reads, entirely offline.

A large write freezes the identity of its content rather than the content, so
what these tests hold to is that the identity is actually enforced: a source
that changed after planning must not reach the server as though it had been
approved.
"""

from __future__ import annotations

import hashlib

import pytest
from test_auth import FakeTransport, response
from test_files import PROFILE, ROOT, configure, entry, multistatus

from ncl import cli, exits, files, plans, secrets, session, uploads

ACCOUNT = "alice"
TARGET = ROOT + "big.bin"


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setattr(
        secrets, "load_credential", lambda profile: secrets.Credential("alice", "app-password")
    )


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "big.bin"
    path.write_bytes(b"abcdefghij" * 3)
    return path


def etag_response(value: str = '"planned"') -> bytes:
    return (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>'
        f"<d:href>{TARGET}</d:href><d:propstat><d:prop><d:getetag>{value}</d:getetag>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        "</d:response></d:multistatus>"
    ).encode()


def transport_for(*responses):
    fake = FakeTransport(list(responses))
    return session.Session(PROFILE, transport=fake), fake


def test_measure_reports_size_and_digest_without_holding_the_file(source):
    size, digest = uploads.measure(source)

    assert size == 30
    assert digest == hashlib.sha256(source.read_bytes()).hexdigest()


def test_parts_are_named_so_lexicographic_and_numeric_order_agree(source, monkeypatch):
    monkeypatch.setattr(uploads, "CHUNK_SIZE", 4)
    size, digest = uploads.measure(source)
    transport, fake = transport_for(
        *([response(201)] * 9),
        response(201, headers={"OC-ETag": '"assembled"'}),
    )

    uploads.stream_upload(
        PROFILE,
        session=transport,
        account_name=ACCOUNT,
        source=source,
        destination=TARGET,
        size=size,
        digest=digest,
        token="tok",
        overwrite=False,
        expected_etag="",
    )

    names = [item["url"].rsplit("/", 1)[-1] for item in fake.requests[1:-1]]
    assert names == [f"{index:05d}" for index in range(1, 9)]
    assert sorted(names) == names


def test_the_assembling_move_carries_the_destination_and_refuses_to_overwrite(source):
    size, digest = uploads.measure(source)
    transport, fake = transport_for(response(201), response(201), response(201))

    uploads.stream_upload(
        PROFILE,
        session=transport,
        account_name=ACCOUNT,
        source=source,
        destination=TARGET,
        size=size,
        digest=digest,
        token="tok",
        overwrite=False,
        expected_etag="",
    )

    assembling = fake.requests[-1]
    assert assembling["method"] == "MOVE"
    assert assembling["url"].endswith("/.file")
    assert assembling["headers"]["Destination"] == TARGET
    assert assembling["headers"]["Overwrite"] == "F"


def test_a_streamed_replacement_assembles_over_the_occupant(source):
    """The destination keeps its file identifier, so its history keeps it too.

    Nextcloud keys version history, shares, tags, and comments to the file id,
    which is the filecache row for the path: re-creating the path discards all
    of them. Assembling over the occupant also files the replaced revision as
    a version, which outlives the trash.
    """
    size, digest = uploads.measure(source)
    transport, fake = transport_for(
        response(201), response(201), response(207, etag_response()), response(204)
    )

    uploads.stream_upload(
        PROFILE,
        session=transport,
        account_name=ACCOUNT,
        source=source,
        destination=TARGET,
        size=size,
        digest=digest,
        token="tok",
        overwrite=True,
        expected_etag='"planned"',
    )

    assert [item["method"] for item in fake.requests] == ["MKCOL", "PUT", "PROPFIND", "MOVE"]
    assert "DELETE" not in [item["method"] for item in fake.requests]
    assert fake.requests[3]["headers"]["Overwrite"] == "T"
    assert fake.requests[3]["headers"]["Destination"] == TARGET


def test_a_newer_destination_revision_is_not_overwritten(source):
    """Read immediately before the MOVE, because the server enforces nothing here.

    This closes the window between planning and applying. It cannot close the
    last instant — that residual race is what the design trades for keeping the
    file's identity, and a write lost to it lands in version history.
    """
    size, digest = uploads.measure(source)
    transport, fake = transport_for(
        response(201), response(201), response(207, etag_response('"newer"')), response(204)
    )

    with pytest.raises(uploads.UploadError, match="changed since the plan") as error:
        uploads.stream_upload(
            PROFILE,
            session=transport,
            account_name=ACCOUNT,
            source=source,
            destination=TARGET,
            size=size,
            digest=digest,
            token="tok",
            overwrite=True,
            expected_etag='"planned"',
        )

    assert error.value.code == exits.CONFLICT
    assert [item["method"] for item in fake.requests] == ["MKCOL", "PUT", "PROPFIND", "DELETE"]


def test_a_destination_that_vanished_before_assembly_is_a_conflict(source):
    size, digest = uploads.measure(source)
    transport, _ = transport_for(
        response(201), response(201), response(404), response(204)
    )

    with pytest.raises(uploads.UploadError, match="no longer exists") as error:
        uploads.stream_upload(
            PROFILE,
            session=transport,
            account_name=ACCOUNT,
            source=source,
            destination=TARGET,
            size=size,
            digest=digest,
            token="tok",
            overwrite=True,
            expected_etag='"planned"',
        )

    assert error.value.code == exits.CONFLICT


def test_a_replacement_whose_assembly_is_unconfirmed_is_uncertain(source):
    """The MOVE is the only step that touches the destination, and a server
    error leaves no way to tell whether it landed."""
    size, digest = uploads.measure(source)
    transport, _ = transport_for(
        response(201), response(201), response(207, etag_response()), response(500),
        response(204),
    )

    with pytest.raises(uploads.UploadError) as error:
        uploads.stream_upload(
            PROFILE,
            session=transport,
            account_name=ACCOUNT,
            source=source,
            destination=TARGET,
            size=size,
            digest=digest,
            token="tok",
            overwrite=True,
            expected_etag='"planned"',
        )

    assert error.value.code == exits.OUTCOME_UNCERTAIN


def test_upload_directory_quotes_account_and_token_as_path_segments():
    assert uploads.upload_directory("alice/admin", "a/b") == (
        "/remote.php/dav/uploads/alice%2Fadmin/a%2Fb/"
    )


def test_a_source_that_changed_after_planning_is_never_assembled(source):
    size, digest = uploads.measure(source)
    source.write_bytes(b"something else entirely------")
    transport, fake = transport_for(response(201), response(201), response(201))

    with pytest.raises(uploads.UploadError, match="changed after the plan was frozen") as error:
        uploads.stream_upload(
            PROFILE,
            session=transport,
            account_name=ACCOUNT,
            source=source,
            destination=TARGET,
            size=size,
            digest=digest,
            token="tok",
            overwrite=False,
            expected_etag="",
        )

    assert error.value.code == exits.CONFLICT
    assert "MOVE" not in [item["method"] for item in fake.requests]


def test_a_source_that_grew_is_refused_before_the_whole_file_is_sent(source, monkeypatch):
    monkeypatch.setattr(uploads, "CHUNK_SIZE", 4)
    size, digest = uploads.measure(source)
    source.write_bytes(source.read_bytes() + b"more")
    transport, fake = transport_for(*([response(201)] * 12))

    with pytest.raises(uploads.UploadError, match="grew while it was being sent"):
        uploads.stream_upload(
            PROFILE,
            session=transport,
            account_name=ACCOUNT,
            source=source,
            destination=TARGET,
            size=size,
            digest=digest,
            token="tok",
            overwrite=False,
            expected_etag="",
        )

    assert "MOVE" not in [item["method"] for item in fake.requests]


def test_a_failed_upload_removes_its_own_directory(source):
    size, digest = uploads.measure(source)
    transport, fake = transport_for(response(201), response(403), response(204))

    with pytest.raises(uploads.UploadError, match="part 1 was refused"):
        uploads.stream_upload(
            PROFILE,
            session=transport,
            account_name=ACCOUNT,
            source=source,
            destination=TARGET,
            size=size,
            digest=digest,
            token="tok",
            overwrite=False,
            expected_etag="",
        )

    assert fake.requests[-1]["method"] == "DELETE"


def test_a_transport_failure_mid_upload_still_removes_the_directory(source):
    """A 5xx is raised by the transport, and cleanup must not depend on the error's type."""
    size, digest = uploads.measure(source)
    transport, fake = transport_for(response(201), response(503), response(204))

    with pytest.raises(session.SessionError):
        uploads.stream_upload(
            PROFILE,
            session=transport,
            account_name=ACCOUNT,
            source=source,
            destination=TARGET,
            size=size,
            digest=digest,
            token="tok",
            overwrite=False,
            expected_etag="",
        )

    assert fake.requests[-1]["method"] == "DELETE"


def test_an_occupied_destination_is_a_conflict(source):
    size, digest = uploads.measure(source)
    transport, _ = transport_for(response(201), response(201), response(412))

    with pytest.raises(uploads.UploadError) as error:
        uploads.stream_upload(
            PROFILE,
            session=transport,
            account_name=ACCOUNT,
            source=source,
            destination=TARGET,
            size=size,
            digest=digest,
            token="tok",
            overwrite=False,
            expected_etag="",
        )

    assert error.value.code == exits.CONFLICT


def test_a_server_without_chunked_upload_says_so(source):
    size, digest = uploads.measure(source)
    transport, _ = transport_for(response(404))

    with pytest.raises(uploads.UploadError, match="may not support chunked upload") as error:
        uploads.stream_upload(
            PROFILE,
            session=transport,
            account_name=ACCOUNT,
            source=source,
            destination=TARGET,
            size=size,
            digest=digest,
            token="tok",
            overwrite=False,
            expected_etag="",
        )

    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


# --- ranged reads ------------------------------------------------------------


def test_a_range_is_requested_and_its_total_reported():
    transport, fake = transport_for(
        response(206, b"12345", headers={"Content-Range": "bytes 10-14/100"})
    )

    window, total = uploads.read_range(
        PROFILE, session=transport, href=TARGET, offset=10, length=5
    )

    assert window == b"12345"
    assert total == 100
    assert fake.requests[0]["headers"]["Range"] == "bytes=10-14"


def test_an_open_ended_range_omits_the_last_byte():
    transport, fake = transport_for(
        response(206, b"tail", headers={"Content-Range": "bytes 10-13/14"})
    )

    uploads.read_range(PROFILE, session=transport, href=TARGET, offset=10, length=None)

    assert fake.requests[0]["headers"]["Range"] == "bytes=10-"


def test_a_server_ignoring_the_range_is_refused_rather_than_misread():
    """A 200 carries the whole entity, which would misplace every byte indexed after it."""
    transport, _ = transport_for(response(200, b"the whole file"))

    with pytest.raises(uploads.UploadError, match="ignored the requested range") as error:
        uploads.read_range(PROFILE, session=transport, href=TARGET, offset=10, length=4)

    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_a_short_window_is_malformed():
    transport, _ = transport_for(
        response(206, b"12", headers={"Content-Range": "bytes 0-4/100"})
    )

    with pytest.raises(uploads.UploadError, match="body length"):
        uploads.read_range(PROFILE, session=transport, href=TARGET, offset=0, length=5)


def test_a_range_for_the_wrong_offset_is_refused():
    transport, _ = transport_for(
        response(206, b"12345", headers={"Content-Range": "bytes 0-4/100"})
    )

    with pytest.raises(uploads.UploadError, match="starting at 0, not 10"):
        uploads.read_range(PROFILE, session=transport, href=TARGET, offset=10, length=5)


def test_the_cli_does_not_write_a_range_returned_for_the_wrong_offset(
    monkeypatch, tmp_path
):
    configure(monkeypatch, tmp_path)
    fake = FakeTransport(
        [response(206, b"12345", headers={"Content-Range": "bytes 0-4/100"})]
    )
    monkeypatch.setattr(session, "UrllibTransport", lambda: fake)
    output = tmp_path / "window.bin"

    code = cli.main(
        [
            "files",
            "read",
            TARGET,
            "--offset",
            "10",
            "--length",
            "5",
            "--output",
            str(output),
        ]
    )

    assert code == exits.MALFORMED_RESPONSE
    assert not output.exists()


def test_a_range_may_end_early_only_at_the_end_of_the_file():
    transport, _ = transport_for(
        response(206, b"end", headers={"Content-Range": "bytes 97-99/100"})
    )

    window, total = uploads.read_range(
        PROFILE, session=transport, href=TARGET, offset=97, length=5
    )

    assert window == b"end"
    assert total == 100


def test_a_range_beyond_the_file_is_a_usage_error():
    transport, _ = transport_for(response(416))

    with pytest.raises(uploads.UploadError) as error:
        uploads.read_range(PROFILE, session=transport, href=TARGET, offset=999, length=1)

    assert error.value.code == exits.USAGE


def test_a_window_is_written_at_its_own_offset(tmp_path):
    target = tmp_path / "out.bin"

    uploads.append_local(target, b"BBB", offset=4)
    uploads.append_local(target, b"AAAA", offset=0)

    assert target.read_bytes() == b"AAAABBB"


# --- plan integration --------------------------------------------------------


def test_a_streamed_plan_freezes_identity_rather_than_bytes(source, monkeypatch):
    transport, _ = transport_for(response(404))

    plan = files.plan_write_stream(
        PROFILE,
        session=transport,
        account_name=ACCOUNT,
        href=TARGET,
        source=str(source),
    )

    step = plan.steps[0]
    assert step.action == "files.upload"
    assert plans.payload_bytes(step) == b""
    assert step.details["size"] == 30
    assert step.details["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_a_streamed_step_carrying_a_payload_is_stale(source):
    step = plans.freeze_step(
        action="files.upload",
        href=TARGET,
        etag="",
        summary="big.bin",
        payload=b"bytes",
        content_type="application/octet-stream",
        details={
            "exists": False, "size": 5, "sha256": "0" * 64,
            "source": "/x", "account_name": "alice", "upload_token": "t",
        },
    )

    with pytest.raises(plans.PlanError, match="not its bytes"):
        files.validate_step(step)


def test_a_streamed_replacement_without_the_frozen_etag_is_stale(source):
    size, digest = uploads.measure(source)
    step = plans.freeze_step(
        action="files.upload",
        href=TARGET,
        etag="",
        summary="big.bin",
        content_type="application/octet-stream",
        details={
            "exists": True,
            "size": size,
            "sha256": digest,
            "source": str(source),
            "account_name": ACCOUNT,
            "upload_token": "t",
        },
    )

    with pytest.raises(files.FileError, match="streamed file replacement"):
        files.validate_step(step)


def test_applying_a_streamed_write_hashes_the_assembled_bytes(source):
    size, digest = uploads.measure(source)
    step = plans.freeze_step(
        action="files.upload",
        href=TARGET,
        etag="",
        summary="big.bin",
        content_type="application/octet-stream",
        details={
            "exists": False,
            "size": size,
            "sha256": digest,
            "source": str(source),
            "account_name": ACCOUNT,
            "upload_token": "t",
        },
    )
    transport, fake = transport_for(
        response(201),
        response(201),
        response(201, headers={"ETag": '"stored"'}),
        response(207, multistatus(entry(TARGET, size=str(size)))),
        response(
            206,
            source.read_bytes(),
            headers={"Content-Range": f"bytes 0-{size - 1}/{size}"},
        ),
    )

    result = files.execute(PROFILE, session=transport, step=step)

    assert result["verified"] is True
    assert [request["method"] for request in fake.requests][-2:] == ["PROPFIND", "GET"]


def test_same_sized_wrong_assembled_bytes_are_uncertain(source):
    size, digest = uploads.measure(source)
    step = plans.freeze_step(
        action="files.upload",
        href=TARGET,
        etag="",
        summary="big.bin",
        content_type="application/octet-stream",
        details={
            "exists": False,
            "size": size,
            "sha256": digest,
            "source": str(source),
            "account_name": ACCOUNT,
            "upload_token": "t",
        },
    )
    transport, _ = transport_for(
        response(201),
        response(201),
        response(201),
        response(207, multistatus(entry(TARGET, size=str(size)))),
        response(
            206,
            b"x" * size,
            headers={"Content-Range": f"bytes 0-{size - 1}/{size}"},
        ),
    )

    with pytest.raises(files.FileError) as error:
        files.execute(PROFILE, session=transport, step=step)

    assert error.value.code == exits.OUTCOME_UNCERTAIN


def test_reconcile_calls_a_matching_length_uncertain_until_the_bytes_agree(source):
    size, digest = uploads.measure(source)
    step = plans.freeze_step(
        action="files.upload",
        href=TARGET,
        etag="",
        summary="big.bin",
        content_type="application/octet-stream",
        details={
            "exists": False, "size": size, "sha256": digest,
            "source": str(source), "account_name": ACCOUNT, "upload_token": "t",
        },
    )
    stored = multistatus(entry(TARGET, size=str(size), collection=False))
    transport, _ = transport_for(
        response(207, stored),
        response(206, b"x" * size, headers={"Content-Range": f"bytes 0-{size - 1}/{size}"}),
    )

    outcome = files.reconcile(PROFILE, session=transport, step=step)

    assert outcome["state"] == "uncertain"


def test_reconcile_verifies_when_the_stored_bytes_hash_to_the_frozen_identity(source):
    size, digest = uploads.measure(source)
    step = plans.freeze_step(
        action="files.upload",
        href=TARGET,
        etag="",
        summary="big.bin",
        content_type="application/octet-stream",
        details={
            "exists": False, "size": size, "sha256": digest,
            "source": str(source), "account_name": ACCOUNT, "upload_token": "t",
        },
    )
    transport, _ = transport_for(
        response(207, multistatus(entry(TARGET, size=str(size)))),
        response(
            206,
            source.read_bytes(),
            headers={"Content-Range": f"bytes 0-{size - 1}/{size}"},
        ),
    )

    outcome = files.reconcile(PROFILE, session=transport, step=step)

    assert outcome["state"] == "verified"


def test_reconcile_reports_pending_when_nothing_was_assembled(source):
    size, digest = uploads.measure(source)
    step = plans.freeze_step(
        action="files.upload",
        href=TARGET,
        etag="",
        summary="big.bin",
        content_type="application/octet-stream",
        details={
            "exists": False, "size": size, "sha256": digest,
            "source": str(source), "account_name": ACCOUNT, "upload_token": "t",
        },
    )
    transport, _ = transport_for(response(404))

    assert files.reconcile(PROFILE, session=transport, step=step)["state"] == "pending"
