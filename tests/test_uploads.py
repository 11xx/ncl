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
from test_files import PROFILE, ROOT, entry, multistatus

from ncl import exits, files, plans, secrets, session, uploads

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
    )

    assembling = fake.requests[-1]
    assert assembling["method"] == "MOVE"
    assert assembling["url"].endswith("/.file")
    assert assembling["headers"]["Destination"] == TARGET
    assert assembling["headers"]["Overwrite"] == "F"


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

    with pytest.raises(uploads.UploadError, match="returned 2 bytes"):
        uploads.read_range(PROFILE, session=transport, href=TARGET, offset=0, length=5)


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
