"""Server-side file search, entirely offline.

The scope check is what these tests exist for. A search names a subtree and the
server decides what matches, so the answer is the one place a resource outside
the allowlist can enter through a request that was itself in scope.
"""

from __future__ import annotations

import json

import pytest
from test_auth import FakeTransport, response
from test_files import ROOT, entry, multistatus

from ncl import exits, files, search, secrets, session
from ncl.config import Profile

PROFILE = Profile(
    "home",
    "https://cloud.example.invalid",
    "pass",
    (),
    ("/remote.php/dav/files/alice/Violentmonkey/",),
)


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setattr(
        secrets, "load_credential", lambda profile: secrets.Credential("alice", "app-password")
    )


def transport_for(*responses):
    fake = FakeTransport(list(responses))
    return session.Session(PROFILE, transport=fake), fake


def test_a_search_is_sent_to_the_dav_root_scoped_by_a_root_relative_path():
    transport, fake = transport_for(response(207, multistatus(entry(ROOT + "a.md"))))

    search.find(PROFILE, session=transport, href=ROOT, name="*.md")

    sent = fake.requests[0]
    assert sent["method"] == "SEARCH"
    assert sent["url"].endswith("/remote.php/dav/")
    body = sent["data"].decode()
    assert "<d:href>/files/alice/Violentmonkey</d:href>" in body
    assert "<d:literal>%.md</d:literal>" in body
    assert "<d:depth>infinity</d:depth>" in body


def test_wildcards_translate_and_protocol_wildcards_in_a_name_survive():
    assert search.like_pattern("*.md") == "%.md"
    assert search.like_pattern("note?.txt") == "note_.txt"
    assert search.like_pattern("100%_done") == "100\\%\\_done"


def test_a_pattern_cannot_close_the_element_around_it():
    body = search.build_request(scope="/files/alice", name="</d:literal><d:evil/>")

    assert b"<d:evil/>" not in body
    assert b"&lt;/d:literal&gt;" in body


def test_a_search_with_no_condition_is_a_usage_error():
    with pytest.raises(files.FileError) as error:
        search.build_request(scope="/files/alice")

    assert error.value.code == exits.USAGE


def test_conditions_combine_with_and():
    body = search.build_request(
        scope="/files/alice",
        name="*.md",
        content_type="text/markdown",
        modified_since="2026-08-01T00:00:00+00:00",
    ).decode()

    assert "<d:and>" in body
    assert "<d:eq><d:prop><d:getcontenttype/></d:prop>" in body
    assert "<d:literal>2026-08-01T00:00:00Z</d:literal>" in body


def test_a_naive_modified_since_is_refused():
    with pytest.raises(files.FileError) as error:
        search.build_request(scope="/files/alice", modified_since="2026-08-01T00:00:00")

    assert error.value.code == exits.USAGE


def test_a_result_outside_the_searched_subtree_is_refused():
    """The server decided what matched, so its answer is where a scope escape lands."""
    escaped = "https://cloud.example.invalid/remote.php/dav/files/alice/private/secret.md"
    transport, _ = transport_for(
        response(207, multistatus(entry(ROOT + "a.md"), entry(escaped)))
    )

    with pytest.raises(files.FileError, match="outside the subtree") as error:
        search.find(PROFILE, session=transport, href=ROOT, name="*.md")

    assert error.value.code == exits.SCOPE_DENIED


def test_a_result_outside_the_allowlist_is_refused_even_inside_the_subtree():
    profile = Profile(
        "home", "https://cloud.example.invalid", "pass", (),
        ("/remote.php/dav/files/alice/Violentmonkey/allowed/",),
    )
    transport = session.Session(
        profile,
        transport=FakeTransport([response(207, multistatus(entry(ROOT + "allowed/a.md")))]),
    )

    found = search.find(profile, session=transport, href=ROOT + "allowed/", name="*.md")

    assert [item.name for item in found] == ["a.md"]


def test_the_searched_collection_is_not_one_of_its_own_results():
    transport, _ = transport_for(
        response(
            207,
            multistatus(entry(ROOT, collection=True, size=""), entry(ROOT + "a.md")),
        )
    )

    found = search.find(PROFILE, session=transport, href=ROOT, name="*")

    assert [item.name for item in found] == ["a.md"]


def test_a_failed_result_row_is_malformed_rather_than_skipped():
    from test_files import failed_entry

    transport, _ = transport_for(
        response(207, multistatus(entry(ROOT + "a.md"), failed_entry(ROOT + "b.md")))
    )

    with pytest.raises(files.FileError, match="with status 404"):
        search.find(PROFILE, session=transport, href=ROOT, name="*.md")


def test_a_rejected_search_says_the_server_may_not_support_a_condition():
    transport, _ = transport_for(response(400))

    with pytest.raises(files.FileError, match="may not support") as error:
        search.find(PROFILE, session=transport, href=ROOT, name="*.md")

    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_searching_outside_the_allowlist_sends_nothing():
    transport, fake = transport_for()

    with pytest.raises(files.FileError) as error:
        search.find(
            PROFILE,
            session=transport,
            href="https://cloud.example.invalid/remote.php/dav/files/alice/other/",
            name="*",
        )

    assert error.value.code == exits.SCOPE_DENIED
    assert fake.requests == []


def test_a_limit_outside_the_supported_range_is_a_usage_error():
    with pytest.raises(files.FileError) as error:
        search.build_request(scope="/files/alice", name="*", limit=0)

    assert error.value.code == exits.USAGE


def test_the_cli_reports_found_files(monkeypatch, tmp_path, capsys):
    directory = tmp_path / "xdg_config_home" / "ncl"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(
        """
default_profile = "home"

[profiles.home]
origin = "https://cloud.example.invalid"
secret_backend = "pass"
calendars = []
files_roots = ["/remote.php/dav/files/alice/Violentmonkey/"]
"""
    )
    monkeypatch.setattr(
        session,
        "UrllibTransport",
        lambda: FakeTransport([response(207, multistatus(entry(ROOT + "a.md")))]),
    )
    from ncl import cli

    code = cli.main(["files", "find", ROOT, "--name", "*.md", "--json"])

    assert code == exits.OK
    assert json.loads(capsys.readouterr().out)["files"][0]["name"] == "a.md"
