"""Read collection changes through WebDAV sync-collection reports.

This read-only mechanism returns an opaque server cursor bound to one calendar
or address-book collection. The caller retains that cursor between invocations;
file collections do not carry one.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape

from . import exits, profiles
from .identity import _element_name, _status_code
from .session import Session, SessionError, absolute_url

DAV = "DAV:"


class SyncError(RuntimeError):
    """A collection change report could not be read safely."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Changes:
    collection: str
    cursor: str
    changed: tuple[tuple[str, str], ...]
    removed: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "cursor": self.cursor,
            "changed": [{"href": href, "etag": etag} for href, etag in self.changed],
            "removed": list(self.removed),
        }


def _canonical(profile: Any, href: str) -> str:
    try:
        return absolute_url(profile, href)
    except SessionError as exc:
        raise SyncError(exc.message, exc.code) from exc


def _direct_child(collection: str, href: str) -> bool:
    try:
        collection_authority, collection_segments = profiles.canonicalize_href(collection)
        href_authority, href_segments = profiles.canonicalize_href(href)
    except ValueError as exc:
        raise SyncError("the sync response contained a malformed href") from exc
    return (
        href_authority == collection_authority
        and len(href_segments) == len(collection_segments) + 1
        and href_segments[:-1] == collection_segments
    )


def _text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def _response_change(profile: Any, collection: str, item: ET.Element) -> tuple[str, str | None]:
    href_element = next(
        (child for child in item if _element_name(child) == (DAV, "href")), None
    )
    if not _text(href_element):
        raise SyncError("the sync response omitted a resource href")
    href = _canonical(profile, _text(href_element))
    if not _direct_child(collection, href):
        raise SyncError(
            f"the sync response href {href} is not a direct child of {collection}"
        )

    status = next(
        (child for child in item if _element_name(child) == (DAV, "status")), None
    )
    if _status_code(status.text if status is not None else None) == 404:
        return href, None

    for propstat in item:
        if _element_name(propstat) != (DAV, "propstat"):
            continue
        propstat_status = next(
            (child for child in propstat if _element_name(child) == (DAV, "status")),
            None,
        )
        code = _status_code(
            propstat_status.text if propstat_status is not None else None
        )
        if code is None or not 200 <= code < 300:
            continue
        prop = next(
            (child for child in propstat if _element_name(child) == (DAV, "prop")), None
        )
        if prop is None:
            continue
        etag = next(
            (child for child in prop if _element_name(child) == (DAV, "getetag")), None
        )
        return href, _text(etag)
    raise SyncError(f"the sync response for {href} had no successful properties")


def changes(
    profile: Any,
    *,
    session: Session,
    href: str,
    since: str | None,
    allowlist: tuple[str, ...],
) -> Changes:
    """Return resources changed or removed since a collection cursor."""
    target = _canonical(profile, href)
    if not profiles.in_scope(target, allowlist):
        raise SyncError(
            f"{target} is outside this profile's collection allowlist",
            exits.SCOPE_DENIED,
        )
    token = escape(since or "")
    body = (
        '<?xml version="1.0"?><d:sync-collection xmlns:d="DAV:">'
        f"<d:sync-token>{token}</d:sync-token>"
        "<d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop>"
        "</d:sync-collection>"
    ).encode()
    response = session.request("REPORT", target, headers={"Depth": "0"}, data=body)
    if response.status == 415:
        raise SyncError(
            f"{target} does not support sync-collection; a calendar home and the "
            "files tree carry no sync token",
            exits.UNSUPPORTED_COLLECTION,
        )
    if response.status == 403 and b"InvalidSyncToken" in response.body:
        raise SyncError(
            "the cursor is not valid for this collection; run without --since to start again",
            exits.CONFLICT,
        )
    if response.status != 207:
        raise SyncError(f"the sync-collection report returned HTTP {response.status}")

    try:
        root = ET.fromstring(response.body)
    except ET.ParseError as exc:
        raise SyncError("the sync-collection response was not valid XML") from exc
    if _element_name(root) != (DAV, "multistatus"):
        raise SyncError("the sync-collection response was not a Multi-Status response")
    cursor_element = next(
        (child for child in root if _element_name(child) == (DAV, "sync-token")), None
    )
    cursor = _text(cursor_element)
    if not cursor:
        raise SyncError("the sync-collection response omitted its sync token")

    changed: list[tuple[str, str]] = []
    removed: list[str] = []
    for item in root:
        if _element_name(item) != (DAV, "response"):
            continue
        resource_href, etag = _response_change(profile, target, item)
        if etag is None:
            removed.append(resource_href)
        else:
            changed.append((resource_href, etag))
    return Changes(target, cursor, tuple(sorted(changed)), tuple(sorted(removed)))
