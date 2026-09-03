"""Contacts over CardDAV.

An address book is a DAV collection like a calendar, discovered from the same
principal and held to the same allowlist. What differs is what a caller wants
out of it: a calendar is read by time window, an address book by who somebody
is — a name, a mail address, an organisation — so that is what a contact
reference carries.

A contact is addressed by resource href, never by name. Two people share a
name far more often than two events share a summary, and a contact resolved by
name is the wrong person rather than a missing one.
"""

from __future__ import annotations

import secrets as token_source
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import etag, exits, plans, profiles, vcard
from .caldav import OC
from .identity import DAV, _element_name, _parse_multistatus, _status_code
from .identity import _propfind as _identity_propfind
from .session import Session, SessionError, absolute_url

CARDDAV = "urn:ietf:params:xml:ns:carddav"


class ContactError(RuntimeError):
    """An address book or contact response was not usable."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class AddressBook:
    """One address book collection, addressed by href."""

    href: str
    display_name: str
    description: str
    read_only: bool
    in_scope: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "href": self.href,
            "display_name": self.display_name,
            "description": self.description,
            "read_only": self.read_only,
            "in_scope": self.in_scope,
        }


@dataclass(frozen=True)
class ContactRef:
    """One contact, reduced to what it is found and addressed by."""

    book_href: str
    href: str
    etag: str
    uid: str
    full_name: str
    family_name: str
    given_name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    organisation: str
    title: str
    categories: tuple[str, ...]
    note: str
    birthday: str
    has_photo: bool
    unsupported: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "book_href": self.book_href,
            "href": self.href,
            "etag": self.etag,
            "uid": self.uid,
            "full_name": self.full_name,
            "family_name": self.family_name,
            "given_name": self.given_name,
            "emails": list(self.emails),
            "phones": list(self.phones),
            "organisation": self.organisation,
            "title": self.title,
            "categories": list(self.categories),
            "note": self.note,
            "birthday": self.birthday,
            "has_photo": self.has_photo,
            "unsupported": list(self.unsupported),
        }


_BOOK_PROPS = (
    "<d:resourcetype/><d:displayname/><d:current-user-privilege-set/>"
    f'<c:addressbook-description xmlns:c="{CARDDAV}"/>'
)
_BOOK_PROPFIND = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<d:propfind xmlns:d="DAV:" xmlns:o="{OC}"><d:prop>{_BOOK_PROPS}</d:prop></d:propfind>'
)
_QUERY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<c:addressbook-query xmlns:d="DAV:" xmlns:c="{CARDDAV}">'
    "<d:prop><d:getetag/><c:address-data/></d:prop>"
    "</c:addressbook-query>"
)


def _canonical(profile: Any, value: str) -> str:
    try:
        return absolute_url(profile, value)
    except SessionError as exc:
        raise ContactError(exc.message, exc.code) from exc


def _segments(href: str) -> tuple[str, ...]:
    try:
        _, segments = profiles.canonicalize_href(href)
    except ValueError as exc:
        raise ContactError("the server returned a malformed href") from exc
    return segments


def _scoped(profile: Any, href: str) -> str:
    resolved = _canonical(profile, href)
    if not profiles.in_scope(resolved, profile.addressbooks):
        raise ContactError(
            f"{resolved} is outside this profile's address book allowlist",
            exits.SCOPE_DENIED,
        )
    return resolved


def home(profile: Any, *, session: Session, principal_url: str) -> str:
    """Discover where this principal's address books live."""
    response = _identity_propfind(
        session, principal_url, f'<c:addressbook-home-set xmlns:c="{CARDDAV}"/>'
    )
    if response.status != 207:
        raise ContactError("address book discovery did not return Multi-Status")
    try:
        properties = _parse_multistatus(response.body, {(CARDDAV, "addressbook-home-set")})
    except Exception as exc:
        raise ContactError(
            "the principal reported no address book home; this server may have no "
            "contacts support",
            exits.PRECONDITION_FAILED,
        ) from exc
    element = properties[(CARDDAV, "addressbook-home-set")]
    hrefs = [
        (child.text or "").strip()
        for child in element
        if _element_name(child) == (DAV, "href")
    ]
    if len(hrefs) != 1 or not hrefs[0]:
        raise ContactError("the address book home set did not name exactly one collection")
    return _canonical(profile, hrefs[0])


def _prop_elements(entry: ET.Element) -> dict[tuple[str, str], ET.Element]:
    found: dict[tuple[str, str], ET.Element] = {}
    for propstat in entry:
        if _element_name(propstat) != (DAV, "propstat"):
            continue
        status = next((i for i in propstat if _element_name(i) == (DAV, "status")), None)
        code = _status_code(status.text if status is not None else None)
        if code is None or not 200 <= code < 300:
            continue
        prop = next((i for i in propstat if _element_name(i) == (DAV, "prop")), None)
        if prop is None:
            continue
        for element in prop:
            found[_element_name(element)] = element
    return found


def _read_only(props: dict[tuple[str, str], ET.Element]) -> bool:
    element = props.get((DAV, "current-user-privilege-set"))
    if element is None:
        return True
    for privilege in element:
        if _element_name(privilege) != (DAV, "privilege"):
            continue
        for granted in privilege:
            if _element_name(granted) in {(DAV, "write"), (DAV, "write-content"), (DAV, "all")}:
                return False
    return True


def list_books(profile: Any, *, session: Session, home_href: str) -> list[AddressBook]:
    """Enumerate the address books under a discovered home.

    As with calendars, the allowlist decision is reported rather than applied:
    a caller configuring a profile has to be able to see the book it is about
    to allow.
    """
    response = session.request(
        "PROPFIND",
        home_href,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=_BOOK_PROPFIND,
    )
    if response.status != 207:
        raise ContactError("the address book home did not answer with a Multi-Status response")
    try:
        root = ET.fromstring(response.body)
    except ET.ParseError as exc:
        raise ContactError("the address book listing was not valid XML") from exc

    home_segments = _segments(_canonical(profile, home_href))
    books: list[AddressBook] = []
    for entry in root:
        if _element_name(entry) != (DAV, "response"):
            continue
        href_element = next((i for i in entry if _element_name(i) == (DAV, "href")), None)
        raw = (href_element.text or "").strip() if href_element is not None else ""
        if not raw:
            continue
        href = _canonical(profile, raw)
        if _segments(href) == home_segments:
            continue
        props = _prop_elements(entry)
        resourcetype = props.get((DAV, "resourcetype"))
        if resourcetype is None or not any(
            _element_name(child) == (CARDDAV, "addressbook") for child in resourcetype
        ):
            continue
        description = props.get((CARDDAV, "addressbook-description"))
        display = props.get((DAV, "displayname"))
        books.append(
            AddressBook(
                href=href,
                display_name=(display.text or "").strip() if display is not None else "",
                description=(description.text or "").strip() if description is not None else "",
                read_only=_read_only(props),
                in_scope=profiles.in_scope(href, list(profile.addressbooks)),
            )
        )
    return books


def resolve(books: list[AddressBook], target: str) -> AddressBook:
    """Resolve a caller's target to exactly one address book."""
    for book in books:
        if book.href == target:
            return book
    short = target.rstrip("/")
    if "/" not in short and "://" not in short:
        matched = [
            book
            for book in books
            if urlsplit(book.href).path.rstrip("/").rsplit("/", 1)[-1] == short
        ]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            hrefs = ", ".join(sorted(book.href for book in matched))
            raise ContactError(
                f"{len(matched)} address books end in {target!r}; name one by href: {hrefs}",
                exits.AMBIGUOUS_TARGET,
            )
    named = [book for book in books if book.display_name == target]
    if len(named) == 1:
        return named[0]
    if not named:
        raise ContactError(f"no address book matched {target!r}", exits.TARGET_NOT_FOUND)
    hrefs = ", ".join(sorted(book.href for book in named))
    raise ContactError(
        f"{len(named)} address books are named {target!r}; name one by href: {hrefs}",
        exits.AMBIGUOUS_TARGET,
    )


#: Structure a contact reference does not model. A card carrying one is read
#: and reported, and the name is what says which part of it this view omits.
_UNSUPPORTED = ("MEMBER", "KIND", "RELATED", "GEO", "X-ADDRESSBOOKSERVER-KIND")


def reference(profile: Any, *, book_href: str, href: str, etag: str, raw: bytes) -> ContactRef:
    """Reduce one card to the fields it is found and addressed by."""
    try:
        parsed = vcard.cards(raw)
    except vcard.VcardError as exc:
        # A listing reads every card in a book, so a refusal that names only
        # what was wrong leaves the operator without the one thing they need
        # to act: which card.
        raise vcard.VcardError(
            f"the card at {href} could not be read: {exc.message}", exc.code
        ) from exc
    if len(parsed) != 1:
        raise ContactError(
            f"the resource at {href} holds {len(parsed)} cards; this tool addresses one "
            "contact per resource",
            exits.UNSUPPORTED_STRUCTURE,
        )
    properties = parsed[0]
    uid = vcard.first(properties, "UID")
    if not uid:
        raise ContactError(f"the contact at {href} has no UID")
    name = vcard.structured(vcard.first(properties, "N"))
    return ContactRef(
        book_href=book_href,
        href=href,
        etag=etag,
        uid=uid,
        full_name=vcard.first(properties, "FN"),
        family_name=name[0] if len(name) > 0 else "",
        given_name=name[1] if len(name) > 1 else "",
        emails=tuple(item.value for item in vcard.every(properties, "EMAIL") if item.value),
        phones=tuple(item.value for item in vcard.every(properties, "TEL") if item.value),
        organisation=vcard.structured(vcard.first(properties, "ORG"))[0]
        if vcard.has(properties, "ORG")
        else "",
        title=vcard.first(properties, "TITLE"),
        categories=vcard.split_values(vcard.first(properties, "CATEGORIES")),
        note=vcard.first(properties, "NOTE"),
        birthday=vcard.first(properties, "BDAY"),
        has_photo=vcard.has(properties, "PHOTO"),
        unsupported=tuple(name for name in _UNSUPPORTED if vcard.has(properties, name)),
    )


def _entries(profile: Any, body: bytes, *, book_href: str) -> list[tuple[str, str, bytes]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ContactError("the contact response was not valid XML") from exc
    if _element_name(root) != (DAV, "multistatus"):
        raise ContactError("the contact response was not a Multi-Status response")

    book_segments = _segments(book_href)
    found: list[tuple[str, str, bytes]] = []
    seen: set[tuple[str, ...]] = set()
    for entry in root:
        if _element_name(entry) != (DAV, "response"):
            continue
        href_element = next((i for i in entry if _element_name(i) == (DAV, "href")), None)
        raw_href = (href_element.text or "").strip() if href_element is not None else ""
        if not raw_href:
            raise ContactError("the contact response omitted a resource href")
        href = _canonical(profile, raw_href)
        segments = _segments(href)
        if segments[:-1] != book_segments or len(segments) != len(book_segments) + 1:
            raise ContactError(
                f"the response named {href}, which is not a direct child of the requested "
                "address book",
                exits.MALFORMED_RESPONSE,
            )
        if segments in seen:
            raise ContactError("the contact response repeated a resource href")
        seen.add(segments)
        props = _prop_elements(entry)
        etag_element = props.get((DAV, "getetag"))
        data_element = props.get((CARDDAV, "address-data"))
        if data_element is None:
            raise ContactError(f"the response returned no card for {href}")
        found.append(
            (
                href,
                (etag_element.text or "").strip() if etag_element is not None else "",
                (data_element.text or "").encode("utf-8"),
            )
        )
    return found


def list_contacts(profile: Any, *, session: Session, book_href: str) -> list[ContactRef]:
    """Read every contact in one allowlisted address book.

    One bounded report, not one request per card. A card's `PHOTO` is left out
    of the reference: it is routinely a hundred kilobytes of base64, and a
    listing dominated by it answers nothing the caller asked.
    """
    book = _scoped(profile, book_href)
    response = session.request(
        "REPORT",
        book,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=_QUERY,
    )
    if response.status != 207:
        raise ContactError(
            f"the address book answered {response.status} rather than Multi-Status"
        )
    return sorted(
        (
            reference(profile, book_href=book, href=href, etag=etag, raw=raw)
            for href, etag, raw in _entries(profile, response.body, book_href=book)
        ),
        key=lambda item: (item.full_name.casefold(), item.href),
    )


def fetch(profile: Any, *, session: Session, href: str) -> tuple[ContactRef, bytes]:
    """Read one contact, returning its reference and the card it came from."""
    target = _canonical(profile, href)
    segments = _segments(target)
    book = _scoped(profile, target.rsplit("/", 1)[0] + "/") if len(segments) > 1 else None
    if book is None:
        raise ContactError(f"{target} names no address book", exits.USAGE)
    response = session.request(
        "GET", target, headers={"Accept": "text/vcard"}, max_redirects=0
    )
    if (
        300 <= response.status < 400
        or response.header("Location")
        or (response.url and response.url != target)
    ):
        raise ContactError(
            f"the server redirected the contact at {target}; the resource must remain exact",
            exits.MALFORMED_RESPONSE,
        )
    if response.status == 404:
        raise ContactError(f"no contact exists at {target}", exits.TARGET_NOT_FOUND)
    if response.status != 200:
        raise ContactError(
            f"the contact answered {response.status}", exits.MALFORMED_RESPONSE
        )
    etag = response.header("ETag") or ""
    return (
        reference(profile, book_href=book, href=target, etag=etag, raw=response.body),
        response.body,
    )


def search(contacts: list[ContactRef], *, term: str) -> list[ContactRef]:
    """Filter contacts by a term matched against name, mail, and organisation.

    Matching is done here rather than by the server. A CardDAV
    `addressbook-query` filter is a per-property text match whose semantics
    differ across servers, and a search that silently means something else on
    the next server is worse than one that costs a listing.
    """
    needle = term.casefold().strip()
    if not needle:
        raise ContactError("the search term is empty", exits.USAGE)
    return [
        contact
        for contact in contacts
        if needle in contact.full_name.casefold()
        or needle in contact.organisation.casefold()
        or any(needle in address.casefold() for address in contact.emails)
        or any(needle in number.casefold() for number in contact.phones)
    ]


def _details(reference: ContactRef) -> dict[str, str]:
    return {
        "book_href": reference.book_href,
        "uid": reference.uid,
        "full_name": reference.full_name,
    }


def plan_create(profile: Any, *, book_href: str, fields: Mapping[str, object]) -> plans.Plan:
    book = _scoped(profile, book_href)
    uid = f"{token_source.token_hex(16)}@ncl"
    payload = vcard.build(uid=uid, fields=fields)
    href = book.rstrip("/") + f"/{uid}.vcf"
    full_name = str(fields.get("fn") or "")
    return plans.write_bundle(
        profile=profile,
        summary=full_name,
        steps=(
            plans.freeze_step(
                action="contact.create",
                href=href,
                etag="",
                summary=full_name,
                payload=payload,
                content_type="text/vcard; charset=utf-8",
                details={"book_href": book, "uid": uid, "full_name": full_name},
            ),
        ),
    )


def _name_parts(raw: bytes) -> tuple[str, ...]:
    """Split the card's own `N` into its parts.

    A parsed value has already had its escapes removed, so splitting one turns
    an escaped `\\;` inside a surname into a field boundary. Splitting the
    property as the server sent it keeps such a surname whole.
    """
    depth = 0
    for line in vcard.unfold(raw):
        segment, separator, value = line.partition(":")
        if not separator:
            continue
        name = segment.split(";", 1)[0].rsplit(".", 1)[-1].strip().upper()
        if name == "BEGIN":
            depth += 1
        elif name == "END":
            depth -= 1
        elif depth == 1 and name == "N":
            return vcard.structured(value)
    return ()


def _strong_etag(reference: ContactRef, action: str) -> str:
    strong = etag.normalize_strong(reference.etag)
    if not strong:
        raise ContactError(
            f"the server returned no strong ETag for this contact, so a {action} cannot "
            "be made conditional",
            exits.MALFORMED_RESPONSE,
        )
    return strong


def plan_update(
    profile: Any, *, session: Session, href: str, changes: Mapping[str, object | None]
) -> plans.Plan:
    reference_, raw = fetch(profile, session=session, href=href)
    group = set(reference_.unsupported) & {"KIND", "MEMBER"}
    if group:
        raise ContactError(
            f"this contact carries {', '.join(sorted(group))}, which cannot be spliced safely",
            exits.UNSUPPORTED_STRUCTURE,
        )
    strong = _strong_etag(reference_, "update")
    replacements: dict[str, tuple[str, ...] | None] = {}
    names = {
        "fn": "FN",
        "email": "EMAIL",
        "tel": "TEL",
        "org": "ORG",
        "title": "TITLE",
        "note": "NOTE",
        "birthday": "BDAY",
    }
    for field, name in names.items():
        if field not in changes:
            continue
        value = changes[field]
        if value is None:
            replacements[name] = None
        elif field in {"email", "tel"}:
            replacements[name] = tuple(vcard.escape(str(item)) for item in value)
        else:
            replacements[name] = (vcard.escape(str(value)),)
    if "family" in changes or "given" in changes:
        # A card's N carries additional names, prefixes, and suffixes past the
        # two parts named here; rewriting only the parts asked for keeps the
        # rest of somebody's name from vanishing on an unrelated edit.
        parts = list(_name_parts(raw))
        parts.extend("" for _ in range(5 - len(parts)))
        for position, field in ((0, "family"), (1, "given")):
            if field in changes:
                parts[position] = str(changes[field] or "")
        replacements["N"] = (";".join(vcard.escape(part) for part in parts),)
    if "categories" in changes:
        value = changes["categories"]
        replacements["CATEGORIES"] = (
            None if value is None else (",".join(vcard.escape(str(item)) for item in value),)
        )
    payload = vcard.splice(raw, replacements)
    updated = reference(
        profile, book_href=reference_.book_href, href=reference_.href, etag=strong, raw=payload
    )
    return plans.write_bundle(
        profile=profile,
        summary=updated.full_name,
        steps=(
            plans.freeze_step(
                action="contact.update",
                href=updated.href,
                etag=strong,
                summary=updated.full_name,
                payload=payload,
                content_type="text/vcard; charset=utf-8",
                details=_details(updated),
            ),
        ),
    )


def plan_delete(profile: Any, *, session: Session, href: str) -> plans.Plan:
    reference_, _ = fetch(profile, session=session, href=href)
    strong = _strong_etag(reference_, "deletion")
    return plans.write_bundle(
        profile=profile,
        summary=reference_.full_name,
        steps=(
            plans.freeze_step(
                action="contact.delete",
                href=reference_.href,
                etag=strong,
                summary=reference_.full_name,
                details=_details(reference_),
            ),
        ),
    )


_ACTIONS = {"contact.create", "contact.update", "contact.delete"}


def validate_step(step: plans.Step) -> None:
    if step.action not in _ACTIONS:
        raise plans.PlanError(f"unknown contact plan action {step.action!r}", exits.USAGE)
    body = plans.payload_bytes(step)
    if step.action == "contact.delete":
        if body:
            raise plans.PlanError(
                "contact deletion steps must not carry a payload", exits.PLAN_STALE
            )
        if not etag.normalize_strong(step.etag):
            raise plans.PlanError("contact deletion needs a strong ETag", exits.PLAN_STALE)
    elif not step.content_type or not body:
        raise plans.PlanError("contact write steps need content and a payload", exits.PLAN_STALE)
    elif step.action == "contact.create" and step.etag:
        raise plans.PlanError("contact creates cannot carry an ETag", exits.PLAN_STALE)
    elif step.action == "contact.update" and not etag.normalize_strong(step.etag):
        raise plans.PlanError("contact updates need a strong ETag", exits.PLAN_STALE)


def _semantic(raw: bytes) -> set[tuple[str, tuple[tuple[str, tuple[str, ...]], ...], str]]:
    cards_ = vcard.cards(raw)
    if len(cards_) != 1:
        raise vcard.VcardError("the resource did not contain exactly one vCard")
    return {
        (item.name, tuple(sorted(item.parameters.items())), item.value)
        for item in cards_[0]
        if item.name not in {"REV", "PRODID"}
    }


def _target(profile: Any, step: plans.Step) -> str:
    return (
        _scoped(profile, step.href.rsplit("/", 1)[0] + "/").rstrip("/")
        + "/"
        + step.href.rsplit("/", 1)[-1]
    )


def _refuse_redirect(response: Any, *, action: str, href: str) -> None:
    if (
        300 <= response.status < 400
        or response.header("Location")
        or (response.url and response.url != href)
    ):
        raise ContactError(
            f"the server redirected the contact {action} at {href}; "
            "the resource must remain exact",
            exits.MALFORMED_RESPONSE,
        )


def execute(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    validate_step(step)
    target = _target(profile, step)
    if step.action == "contact.delete":
        response = session.request(
            "DELETE", target, headers={"If-Match": step.etag}, max_redirects=0
        )
    else:
        header = {"Content-Type": step.content_type}
        header["If-None-Match" if step.action == "contact.create" else "If-Match"] = (
            "*" if step.action == "contact.create" else step.etag
        )
        response = session.request(
            "PUT", target, headers=header, data=plans.payload_bytes(step), max_redirects=0
        )
    _refuse_redirect(response, action=step.action, href=target)
    if response.status == 412:
        if step.action == "contact.create":
            raise ContactError(
                f"{target} already holds a resource; reconcile decides whether it is this plan's",
                exits.OUTCOME_UNCERTAIN,
            )
        raise plans.PlanError(
            f"the contact at {target} changed since the plan was made; "
            "re-plan against its current state",
            exits.CONFLICT,
        )
    if response.status == 404 and step.action != "contact.create":
        raise ContactError(f"no contact exists at {target}", exits.TARGET_NOT_FOUND)
    if response.status not in {200, 201, 204}:
        raise ContactError(
            f"the server refused the {step.action} with status {response.status}",
            exits.SERVER_ERROR,
        )
    result = {"action": step.action, "href": target, "uid": step.details.get("uid", "")}
    if step.action == "contact.delete":
        try:
            check = session.request(
                "GET", target, headers={"Accept": "text/vcard"}, max_redirects=0
            )
        except (ContactError, SessionError) as exc:
            raise ContactError(
                f"the absence of {target} could not be verified after deletion",
                exits.OUTCOME_UNCERTAIN,
            ) from exc
        if check.status != 404:
            raise ContactError(
                "the server accepted deletion, but the contact is still readable",
                exits.OUTCOME_UNCERTAIN,
            )
        result["verified"] = "deleted"
        return result
    try:
        stored, raw = fetch(profile, session=session, href=target)
        exact = _semantic(raw) == _semantic(plans.payload_bytes(step))
    except (ContactError, vcard.VcardError, ValueError, TypeError) as exc:
        raise ContactError(
            f"the server accepted {step.action}, but its readback could not be verified",
            exits.OUTCOME_UNCERTAIN,
        ) from exc
    if not exact:
        raise ContactError(
            f"the server stored something different at {target}", exits.OUTCOME_UNCERTAIN
        )
    result.update({"etag": stored.etag, "full_name": stored.full_name, "verified": True})
    return result


def reconcile(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    validate_step(step)
    target = _target(profile, step)
    try:
        stored, raw = fetch(profile, session=session, href=target)
    except ContactError as exc:
        if exc.code == exits.TARGET_NOT_FOUND:
            return {
                "state": "pending"
                if step.action == "contact.create"
                else "verified"
                if step.action == "contact.delete"
                else "uncertain"
            }
        raise ContactError(
            f"the {step.action} readback could not be reconciled", exits.OUTCOME_UNCERTAIN
        ) from exc
    try:
        if step.action != "contact.delete" and _semantic(raw) == _semantic(
            plans.payload_bytes(step)
        ):
            return {"state": "verified"}
    except (vcard.VcardError, ValueError, TypeError) as exc:
        raise ContactError(
            f"the {step.action} readback could not be reconciled",
            exits.OUTCOME_UNCERTAIN,
        ) from exc
    if etag.normalize_strong(stored.etag) == etag.normalize_strong(step.etag):
        return {"state": "pending"}
    return {"state": "uncertain"}
