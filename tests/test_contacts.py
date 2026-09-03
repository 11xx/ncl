"""Contacts and vCard reading, entirely offline.

What these tests hold to is that a card's structure survives the reduction to a
reference: an escaped comma does not split an address in two, a folded line
does not lose a character, and a hundred kilobytes of photo does not end up in
a listing.
"""

from __future__ import annotations

import json
import re
from unittest.mock import Mock

import pytest
from test_auth import FakeTransport, home_response, principal_response, response

from ncl import cli, contacts, exits, plans, secrets, session, vcard
from ncl.config import Profile

PROFILE = Profile(
    name="home",
    origin="https://cloud.example.invalid",
    secret_backend="pass",
    calendars=(),
    files_roots=(),
    addressbooks=("/remote.php/dav/addressbooks/users/alice/contacts/",),
)
BOOK = "https://cloud.example.invalid/remote.php/dav/addressbooks/users/alice/contacts/"
CARD_HREF = BOOK + "leon"
PRINCIPAL = "https://cloud.example.invalid/remote.php/dav/principals/users/alice/"
CONFIG = """
default_profile = "home"

[profiles.home]
origin = "https://cloud.example.invalid"
secret_backend = "pass"
calendars = []
files_roots = []
addressbooks = ["/remote.php/dav/addressbooks/users/alice/contacts/"]
"""

CARD = (
    "BEGIN:VCARD\r\n"
    "VERSION:3.0\r\n"
    "UID:leon-1\r\n"
    "FN:Leon Green\r\n"
    "N:Green;Leon;;;\r\n"
    "EMAIL;TYPE=HOME:leon@example.invalid\r\n"
    "TEL;TYPE=HOME,VOICE:+15550000\r\n"
    "ORG:Example Co;Sales\r\n"
    "TITLE:Manager\r\n"
    "CATEGORIES:Work,Friends\r\n"
    "NOTE:An example\r\n"
    "BDAY:20000101\r\n"
    "PHOTO;ENCODING=b;TYPE=PNG:" + "A" * 4000 + "\r\n"
    "END:VCARD\r\n"
)


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setattr(
        secrets, "load_credential", lambda profile: secrets.Credential("alice", "app-password")
    )


def multistatus(*entries: str) -> bytes:
    return (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        'xmlns:card="urn:ietf:params:xml:ns:carddav">' + "".join(entries) + "</d:multistatus>"
    ).encode()


def card_entry(href: str = CARD_HREF, etag: str = '"v1"', card: str = CARD) -> str:
    escaped = card.replace("&", "&amp;").replace("<", "&lt;")
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
        f"<d:getetag>{etag}</d:getetag>"
        f"<card:address-data>{escaped}</card:address-data>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def book_entry(href: str = BOOK, name: str = "Contacts", writable: bool = True) -> str:
    privileges = (
        "<d:current-user-privilege-set><d:privilege><d:write/></d:privilege>"
        "</d:current-user-privilege-set>"
        if writable
        else ""
    )
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
        "<d:resourcetype><d:collection/><card:addressbook/></d:resourcetype>"
        f"<d:displayname>{name}</d:displayname>{privileges}"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def transport_for(*responses):
    return session.Session(PROFILE, transport=FakeTransport(list(responses)))


# --- vCard reading -----------------------------------------------------------


def test_a_folded_line_loses_only_the_folding():
    parsed = vcard.parse(b"BEGIN:VCARD\r\nFN:Leon\r\n  Green\r\nEND:VCARD\r\n")

    assert vcard.first(parsed, "FN") == "Leon Green"


def test_an_escaped_comma_does_not_split_one_value_into_two():
    assert vcard.split_values("Green\\, Leon,Friends") == ("Green, Leon", "Friends")


def test_a_structured_value_splits_on_unescaped_semicolons_only():
    assert vcard.structured("Green;Leon;;;") == ("Green", "Leon", "", "", "")
    assert vcard.structured(r"A\;B;C") == ("A;B", "C")


def test_a_bare_parameter_is_read_as_a_type():
    """vCard 2.1 writes `TEL;HOME:`; reading it as valueless loses the only thing it says."""
    parsed = vcard.parse(b"BEGIN:VCARD\r\nTEL;HOME:+1\r\nEND:VCARD\r\n")

    assert parsed[0].types() == ("HOME",)


def test_a_grouped_property_is_named_by_its_property():
    parsed = vcard.parse(b"BEGIN:VCARD\r\nitem1.EMAIL:a@b.invalid\r\nEND:VCARD\r\n")

    assert vcard.first(parsed, "EMAIL") == "a@b.invalid"


def test_a_binary_property_is_recorded_as_present_and_not_as_content():
    raw = ("BEGIN:VCARD\r\nPHOTO;ENCODING=b:" + "A" * 500 + "\r\nEND:VCARD\r\n").encode()
    parsed = vcard.parse(raw)

    assert vcard.has(parsed, "PHOTO")
    assert vcard.first(parsed, "PHOTO") == ""


def test_a_card_that_never_ends_is_refused():
    with pytest.raises(vcard.VcardError, match="did not end"):
        vcard.cards(b"BEGIN:VCARD\r\nFN:x\r\n")


def test_a_line_without_a_value_is_refused():
    with pytest.raises(vcard.VcardError, match="carried no value"):
        vcard.parse(b"BEGIN:VCARD\r\nFN\r\nEND:VCARD\r\n")


def test_a_resource_that_is_not_a_card_is_refused():
    raw = b"BEGIN:WRONG\r\nVERSION:3.0\r\nUID:1\r\nFN:Nobody\r\nEND:WRONG\r\n"

    with pytest.raises(vcard.VcardError, match="not a vCard"):
        contacts.reference(PROFILE, book_href=BOOK, href=CARD_HREF, etag="", raw=raw)


def test_a_component_closed_under_another_name_is_refused():
    raw = b"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:1\r\nBEGIN:X-INNER\r\nEND:VCARD\r\nEND:X-INNER\r\n"

    with pytest.raises(vcard.VcardError, match="while X-INNER was open"):
        contacts.reference(PROFILE, book_href=BOOK, href=CARD_HREF, etag="", raw=raw)


def test_a_nested_component_does_not_split_the_card():
    raw = (
        b"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:1\r\nFN:Leon Green\r\n"
        b"BEGIN:X-INNER\r\nX-FOO:1\r\nEND:X-INNER\r\nEMAIL:leon@example.invalid\r\n"
        b"END:VCARD\r\n"
    )

    found = contacts.reference(PROFILE, book_href=BOOK, href=CARD_HREF, etag="", raw=raw)

    assert found.full_name == "Leon Green"
    assert found.emails == ("leon@example.invalid",)
    assert not vcard.has(vcard.cards(raw)[0], "X-FOO")


def test_a_card_this_reader_refuses_is_named_by_its_href():
    raw = b"BEGIN:VCARD\r\nVERSION:9.9\r\nUID:1\r\nFN:Leon Green\r\nEND:VCARD\r\n"

    with pytest.raises(vcard.VcardError, match=re.escape(CARD_HREF)):
        contacts.reference(PROFILE, book_href=BOOK, href=CARD_HREF, etag="", raw=raw)


def test_a_blank_version_is_read_as_undeclared():
    raw = b"BEGIN:VCARD\r\nVERSION:\r\nUID:1\r\nFN:Leon Green\r\nEND:VCARD\r\n"

    found = contacts.reference(PROFILE, book_href=BOOK, href=CARD_HREF, etag="", raw=raw)

    assert found.full_name == "Leon Green"


def test_a_version_this_reader_does_not_implement_is_refused():
    raw = b"BEGIN:VCARD\r\nVERSION:9.9\r\nUID:1\r\nFN:Leon Green\r\nEND:VCARD\r\n"

    with pytest.raises(vcard.VcardError, match=re.escape("this reads 2.1, 3.0, 4.0")):
        contacts.reference(PROFILE, book_href=BOOK, href=CARD_HREF, etag="", raw=raw)


# --- references --------------------------------------------------------------


def test_a_reference_carries_what_a_contact_is_found_by():
    found = contacts.reference(
        PROFILE, book_href=BOOK, href=CARD_HREF, etag='"v1"', raw=CARD.encode()
    )

    assert found.full_name == "Leon Green"
    assert found.family_name == "Green"
    assert found.given_name == "Leon"
    assert found.emails == ("leon@example.invalid",)
    assert found.organisation == "Example Co"
    assert found.categories == ("Work", "Friends")
    assert found.has_photo is True


def test_a_photo_never_reaches_the_reference():
    found = contacts.reference(
        PROFILE, book_href=BOOK, href=CARD_HREF, etag='"v1"', raw=CARD.encode()
    )

    assert "A" * 100 not in json.dumps(found.as_dict())


def test_a_card_without_a_uid_is_refused():
    raw = b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:No One\r\nEND:VCARD\r\n"

    with pytest.raises(contacts.ContactError, match="no UID"):
        contacts.reference(PROFILE, book_href=BOOK, href=CARD_HREF, etag="", raw=raw)


def test_a_resource_holding_two_cards_is_refused():
    raw = (CARD + CARD).encode()

    with pytest.raises(contacts.ContactError) as error:
        contacts.reference(PROFILE, book_href=BOOK, href=CARD_HREF, etag="", raw=raw)

    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_structure_the_reference_does_not_model_is_named():
    raw = CARD.replace("END:VCARD", "KIND:group\r\nMEMBER:urn:uuid:1\r\nEND:VCARD").encode()

    found = contacts.reference(PROFILE, book_href=BOOK, href=CARD_HREF, etag="", raw=raw)

    assert set(found.unsupported) == {"KIND", "MEMBER"}


def test_a_redirected_contact_read_is_refused():
    transport = Mock()
    transport.request.return_value = response(302, headers={"Location": BOOK + "other"})

    with pytest.raises(contacts.ContactError) as error:
        contacts.fetch(PROFILE, session=transport, href=CARD_HREF)

    assert error.value.code == exits.MALFORMED_RESPONSE
    transport.request.assert_called_once_with(
        "GET", CARD_HREF, headers={"Accept": "text/vcard"}, max_redirects=0
    )


# --- collections -------------------------------------------------------------


def test_listing_makes_one_report_and_sorts_by_name():
    second = CARD.replace("Leon Green", "Ada Byron").replace("UID:leon-1", "UID:ada-1")
    fake = FakeTransport(
        [response(207, multistatus(card_entry(), card_entry(BOOK + "ada", card=second)))]
    )

    found = contacts.list_contacts(
        PROFILE, session=session.Session(PROFILE, transport=fake), book_href=BOOK
    )

    assert [item.full_name for item in found] == ["Ada Byron", "Leon Green"]
    assert [item["method"] for item in fake.requests] == ["REPORT"]


def test_a_book_outside_the_allowlist_sends_nothing():
    fake = FakeTransport([])

    with pytest.raises(contacts.ContactError) as error:
        contacts.list_contacts(
            PROFILE,
            session=session.Session(PROFILE, transport=fake),
            book_href="https://cloud.example.invalid/remote.php/dav/addressbooks/users/alice/other/",
        )

    assert error.value.code == exits.SCOPE_DENIED
    assert fake.requests == []


def test_a_result_that_is_not_a_direct_child_is_malformed():
    transport = transport_for(
        response(207, multistatus(card_entry(href=BOOK + "nested/leon")))
    )

    with pytest.raises(contacts.ContactError, match="not a direct child"):
        contacts.list_contacts(PROFILE, session=transport, book_href=BOOK)


def test_a_repeated_href_is_malformed():
    transport = transport_for(response(207, multistatus(card_entry(), card_entry())))

    with pytest.raises(contacts.ContactError, match="repeated a resource href"):
        contacts.list_contacts(PROFILE, session=transport, book_href=BOOK)


def test_books_report_the_allowlist_decision_rather_than_filtering():
    other = BOOK.replace("/contacts/", "/system/")
    home_href = BOOK.rsplit("contacts/", 1)[0]
    transport = transport_for(
        response(207, multistatus(book_entry(), book_entry(other, "Accounts", writable=False)))
    )

    books = contacts.list_books(PROFILE, session=transport, home_href=home_href)

    assert [(book.display_name, book.in_scope, book.read_only) for book in books] == [
        ("Contacts", True, False),
        ("Accounts", False, True),
    ]


def test_a_book_is_resolved_by_final_segment_or_name():
    books = contacts.list_books(
        PROFILE,
        session=transport_for(response(207, multistatus(book_entry()))),
        home_href=BOOK.rsplit("contacts/", 1)[0],
    )

    assert contacts.resolve(books, "contacts").href == BOOK
    assert contacts.resolve(books, "Contacts").href == BOOK
    with pytest.raises(contacts.ContactError) as error:
        contacts.resolve(books, "nope")
    assert error.value.code == exits.TARGET_NOT_FOUND


def test_search_matches_name_mail_phone_and_organisation():
    found = contacts.list_contacts(
        PROFILE, session=transport_for(response(207, multistatus(card_entry()))), book_href=BOOK
    )

    assert contacts.search(found, term="green")
    assert contacts.search(found, term="EXAMPLE CO")
    assert contacts.search(found, term="15550000")
    assert not contacts.search(found, term="nobody")


def test_an_empty_search_term_is_a_usage_error():
    with pytest.raises(contacts.ContactError) as error:
        contacts.search([], term="  ")

    assert error.value.code == exits.USAGE


def test_the_cli_lists_contacts_without_the_photo(monkeypatch, tmp_path, capsys):
    directory = tmp_path / "xdg_config_home" / "ncl"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(CONFIG)
    home_body = (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        'xmlns:card="urn:ietf:params:xml:ns:carddav"><d:response><d:href>/p</d:href>'
        "<d:propstat><d:prop><card:addressbook-home-set><d:href>"
        f"{BOOK.rsplit('contacts/', 1)[0]}</d:href></card:addressbook-home-set></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
    ).encode()
    monkeypatch.setattr(
        session,
        "UrllibTransport",
        lambda: FakeTransport(
            [
                principal_response(),
                home_response(),
                response(207, home_body),
                response(207, multistatus(book_entry())),
                response(207, multistatus(card_entry())),
            ]
        ),
    )

    code = cli.main(["contacts", "list", "contacts", "--json"])

    assert code == exits.OK
    payload = capsys.readouterr().out
    assert "A" * 100 not in payload
    assert json.loads(payload)["contacts"][0]["full_name"] == "Leon Green"


def test_create_plan_freezes_conditional_vcard(monkeypatch):
    monkeypatch.setattr(contacts.token_source, "token_hex", lambda _: "abc")

    plan = contacts.plan_create(PROFILE, book_href=BOOK, fields={"fn": "Ada"})
    step = plan.steps[0]

    assert step.action == "contact.create"
    assert step.href == BOOK + "abc@ncl.vcf"
    assert step.etag == ""
    assert step.content_type.startswith("text/vcard")
    assert b"UID:abc@ncl" in plans.payload_bytes(step)
    assert b"FN:Ada" in plans.payload_bytes(step)


def test_update_plan_splices_and_freezes_strong_etag():
    fake = FakeTransport([response(200, CARD.encode(), headers={"ETag": '"v1"'})])

    plan = contacts.plan_update(
        PROFILE,
        session=session.Session(PROFILE, transport=fake),
        href=CARD_HREF,
        changes={"fn": "Changed"},
    )
    body = plans.payload_bytes(plan.steps[0])

    assert plan.steps[0].etag == '"v1"'
    assert b"FN:Changed\r\n" in body
    assert ("PHOTO;ENCODING=b;TYPE=PNG:" + "A" * 4000 + "\r\n").encode() in body


def test_update_plan_keeps_the_name_parts_it_was_not_asked_for():
    card = CARD.replace("N:Green;Leon;;;", "N:Green;Leon;Ada;Dr.;PhD")
    fake = FakeTransport([response(200, card.encode(), headers={"ETag": '"v1"'})])

    plan = contacts.plan_update(
        PROFILE,
        session=session.Session(PROFILE, transport=fake),
        href=CARD_HREF,
        changes={"family": "Grey"},
    )

    assert b"N:Grey;Leon;Ada;Dr.;PhD\r\n" in plans.payload_bytes(plan.steps[0])


@pytest.mark.parametrize(
    "raw",
    [
        CARD.replace("END:VCARD", "KIND:group\r\nMEMBER:x\r\nEND:VCARD").encode(),
        (CARD + CARD).encode(),
    ],
)
def test_update_refuses_group_and_multi_card_resources(raw):
    transport = Mock()
    transport.request.return_value = response(200, raw, headers={"ETag": '"v1"'})

    with pytest.raises(contacts.ContactError) as error:
        contacts.plan_update(PROFILE, session=transport, href=CARD_HREF, changes={"fn": "Changed"})

    assert error.value.code == exits.UNSUPPORTED_STRUCTURE


def test_update_refuses_a_missing_strong_etag():
    transport = Mock()
    transport.request.return_value = response(200, CARD.encode())

    with pytest.raises(contacts.ContactError) as error:
        contacts.plan_update(PROFILE, session=transport, href=CARD_HREF, changes={"fn": "Changed"})

    assert error.value.code == exits.MALFORMED_RESPONSE


def test_a_surname_with_an_escaped_semicolon_survives_the_typed_view_and_an_update():
    card = CARD.replace("N:Green;Leon;;;", "N:Smith\\;Jones;Ada;;;").encode()
    fake = FakeTransport([response(200, card, headers={"ETag": '"v1"'})])

    view = contacts.reference(PROFILE, book_href=BOOK, href=CARD_HREF, etag='"v1"', raw=card)
    plan = contacts.plan_update(
        PROFILE,
        session=session.Session(PROFILE, transport=fake),
        href=CARD_HREF,
        changes={"given": "Bea"},
    )

    assert view.family_name == "Smith;Jones"
    assert view.given_name == "Ada"
    assert b"N:Smith\\;Jones;Bea;;;\r\n" in plans.payload_bytes(plan.steps[0])


def test_cli_update_without_changes_is_usage():
    assert cli.main(["contacts", "update", CARD_HREF]) == exits.USAGE


def test_setting_and_clearing_one_field_is_a_usage_error():
    assert cli.main(["contacts", "update", CARD_HREF, "--org", "Acme", "--clear", "org"]) == (
        exits.USAGE
    )


def contact_step(action, *, payload=None, etag='"v1"'):
    payload = CARD.encode() if payload is None else payload
    return plans.freeze_step(
        action=action,
        href=CARD_HREF,
        etag=etag,
        summary="Leon Green",
        payload=b"" if action == "contact.delete" else payload,
        content_type="" if action == "contact.delete" else "text/vcard; charset=utf-8",
        details={"book_href": BOOK, "uid": "leon-1", "full_name": "Leon Green"},
    )


def test_create_precondition_failure_is_uncertain():
    transport = Mock()
    transport.request.return_value = response(412)

    with pytest.raises(contacts.ContactError) as error:
        contacts.execute(PROFILE, session=transport, step=contact_step("contact.create", etag=""))

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert transport.request.call_args.kwargs["headers"]["If-None-Match"] == "*"


def test_update_readback_equal_modulo_revision_is_verified():
    stored = CARD.replace("END:VCARD", "REV:20260903T120000Z\r\nEND:VCARD").encode()
    transport = Mock()
    transport.request.side_effect = [response(204), response(200, stored, headers={"ETag": '"v2"'})]

    result = contacts.execute(PROFILE, session=transport, step=contact_step("contact.update"))

    assert result["verified"] is True
    assert transport.request.call_args_list[0].kwargs["headers"]["If-Match"] == '"v1"'


def test_update_readback_difference_is_uncertain():
    transport = Mock()
    transport.request.side_effect = [
        response(204),
        response(200, CARD.replace("FN:Leon Green", "FN:Other").encode(),
                 headers={"ETag": '"v2"'}),
    ]

    with pytest.raises(contacts.ContactError) as error:
        contacts.execute(PROFILE, session=transport, step=contact_step("contact.update"))

    assert error.value.code == exits.OUTCOME_UNCERTAIN


def test_delete_verifies_not_found():
    transport = Mock()
    transport.request.side_effect = [response(204), response(404)]

    result = contacts.execute(PROFILE, session=transport, step=contact_step("contact.delete"))

    assert result["verified"] == "deleted"


def test_a_delete_whose_readback_fails_is_uncertain():
    transport = Mock()
    transport.request.side_effect = [
        response(204),
        session.SessionError("the connection failed", exits.UNREACHABLE),
    ]
    plan = plans.write_bundle(
        profile=PROFILE, summary="Leon Green", steps=(contact_step("contact.delete"),)
    )

    with pytest.raises(contacts.ContactError) as error, plans.claim(plan.plan_id):
        plans.apply(
            PROFILE,
            session=transport,
            plan=plan,
            dispatchers={
                "contact.": plans.Dispatcher(
                    contacts.validate_step, contacts.execute, contacts.reconcile
                )
            },
        )

    assert error.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).progress[0].state == "uncertain"


@pytest.mark.parametrize(
    ("action", "responses", "state"),
    [
        ("contact.create", [response(404)], "pending"),
        ("contact.delete", [response(404)], "verified"),
        ("contact.update", [response(404)], "uncertain"),
        ("contact.create", [response(200, CARD.encode(), headers={"ETag": '"v2"'})],
         "verified"),
        ("contact.update", [response(200, CARD.replace("FN:Leon Green", "FN:Other").encode(),
                                             headers={"ETag": '"v1"'})], "pending"),
        ("contact.delete", [response(200, CARD.encode(), headers={"ETag": '"v2"'})],
         "uncertain"),
    ],
)
def test_reconcile_classifies_contact_state(action, responses, state):
    transport = Mock()
    transport.request.side_effect = responses

    step = contact_step(action, etag="" if action == "contact.create" else '"v1"')
    assert contacts.reconcile(PROFILE, session=transport, step=step) == {"state": state}
