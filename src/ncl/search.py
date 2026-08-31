"""Finding files without walking the tree.

A depth-one listing answers "what is in this collection". It does not answer
"where is the file", and reaching that answer by walking costs one round trip
per collection — the cost falls on whoever is waiting, and the intermediate
listings are read by nobody.

WebDAV's `SEARCH` asks the server that question directly. Nextcloud exposes it
at the DAV root, scoped by a path relative to that root rather than by the
collection href everything else in this tool is addressed by, so the two are
converted here.

The scope check is the load-bearing part. A search names a subtree and the
server decides what matches, which means the answer is the one place where a
resource outside the allowlist could enter through a request that was itself
in scope. Every result is therefore checked against both the requested subtree
and the profile allowlist, and a result outside either is a refusal rather than
a filtered-out row: a server returning what it was not asked for is not a
server whose other answers can be trusted.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlsplit

from . import exits, profiles
from .files import FileError, FileRef, _canonical, _multistatus, _scoped, _segments
from .session import Session

#: Where Nextcloud accepts a SEARCH, and the prefix its scope hrefs omit.
DAV_ROOT = "/remote.php/dav/"

#: The predicates this server actually answers. A comparison on
#: `getcontentlength` is refused with a 400, so it is not offered rather than
#: being offered and failing at the server.
_MAX_RESULTS = 5000


def _scope_href(collection: str) -> str:
    """Reduce a DAV collection href to the root-relative scope SEARCH wants."""
    path = urlsplit(collection).path
    if not path.startswith(DAV_ROOT):
        raise FileError(
            f"{collection} is not under {DAV_ROOT}, so it names no search scope",
            exits.SCOPE_DENIED,
        )
    return "/" + path[len(DAV_ROOT) :].strip("/")


def _escaped(value: str) -> str:
    """Escape one literal for XML text.

    A pattern is caller-supplied, so it reaches the request body as text and
    never as markup: a name containing `<` would otherwise close the element
    around it and change which query the server ran.
    """
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def like_pattern(glob: str) -> str:
    """Translate a shell-style name pattern into a DAV `LIKE` literal.

    `*` and `?` are what a caller types; `%` and `_` are what the protocol
    means by them, and a literal `%` or `_` in a filename must survive rather
    than becoming a wildcard the caller did not ask for.
    """
    text = str(glob or "")
    if not text:
        raise FileError("the name pattern is empty", exits.USAGE)
    out: list[str] = []
    for character in text:
        if character == "*":
            out.append("%")
        elif character == "?":
            out.append("_")
        elif character in {"%", "_", "\\"}:
            out.append("\\" + character)
        else:
            out.append(character)
    return "".join(out)


def _instant(value: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise FileError("--modified-since is not an ISO 8601 instant", exits.USAGE) from exc
    if parsed.tzinfo is None:
        raise FileError(
            "--modified-since has no timezone offset; a local time is ambiguous across a "
            "DST transition, so state the offset explicitly",
            exits.USAGE,
        )
    return parsed.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_request(
    *,
    scope: str,
    name: str | None = None,
    content_type: str | None = None,
    modified_since: str | None = None,
    limit: int | None = None,
) -> bytes:
    """Compose one `basicsearch` over a subtree.

    At least one condition is required. A search with no `where` clause is a
    recursive listing wearing a search's clothes, and it returns the whole
    subtree at a cost the caller did not ask for.
    """
    conditions: list[str] = []
    if name is not None:
        conditions.append(
            "<d:like><d:prop><d:displayname/></d:prop>"
            f"<d:literal>{_escaped(like_pattern(name))}</d:literal></d:like>"
        )
    if content_type is not None:
        conditions.append(
            "<d:eq><d:prop><d:getcontenttype/></d:prop>"
            f"<d:literal>{_escaped(content_type.strip())}</d:literal></d:eq>"
        )
    if modified_since is not None:
        conditions.append(
            "<d:gt><d:prop><d:getlastmodified/></d:prop>"
            f"<d:literal>{_instant(modified_since)}</d:literal></d:gt>"
        )
    if not conditions:
        raise FileError(
            "a search states at least one of --name, --content-type, or --modified-since",
            exits.USAGE,
        )
    where = conditions[0] if len(conditions) == 1 else f"<d:and>{''.join(conditions)}</d:and>"

    if limit is not None and (limit < 1 or limit > _MAX_RESULTS):
        raise FileError(f"--limit is between 1 and {_MAX_RESULTS}", exits.USAGE)
    bound = f"<d:limit><d:nresults>{limit}</d:nresults></d:limit>" if limit else ""

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<d:searchrequest xmlns:d="DAV:"><d:basicsearch>'
        "<d:select><d:prop>"
        "<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
        "<d:getetag/><d:getcontenttype/>"
        "</d:prop></d:select>"
        f"<d:from><d:scope><d:href>{_escaped(scope)}</d:href>"
        "<d:depth>infinity</d:depth></d:scope></d:from>"
        f"<d:where>{where}</d:where><d:orderby/>{bound}"
        "</d:basicsearch></d:searchrequest>"
    ).encode()


def _within(href: str, root_segments: tuple[str, ...]) -> bool:
    segments = _segments(href)
    return segments[: len(root_segments)] == root_segments


def find(
    profile: Any,
    *,
    session: Session,
    href: str,
    name: str | None = None,
    content_type: str | None = None,
    modified_since: str | None = None,
    limit: int | None = None,
) -> list[FileRef]:
    """Search one allowlisted subtree, and refuse any answer outside it."""
    collection = _scoped(profile, href)
    body = build_request(
        scope=_scope_href(collection),
        name=name,
        content_type=content_type,
        modified_since=modified_since,
        limit=limit,
    )
    response = session.request(
        "SEARCH",
        _canonical(profile, DAV_ROOT),
        headers={"Content-Type": "text/xml; charset=utf-8"},
        data=body,
    )
    if response.status == 400:
        raise FileError(
            "the server refused the search as malformed; it may not support one of the "
            "conditions given",
            exits.UNSUPPORTED_STRUCTURE,
        )
    if response.status != 207:
        raise FileError(
            f"the search answered {response.status} rather than Multi-Status",
            exits.MALFORMED_RESPONSE,
        )

    root_segments = _segments(collection)
    found: list[FileRef] = []
    for result_href, reference, code in _multistatus(profile, response.body):
        if reference is None:
            raise FileError(
                f"the search reported {result_href} with status {code}",
                exits.MALFORMED_RESPONSE,
            )
        if not _within(result_href, root_segments):
            raise FileError(
                f"the search returned {result_href}, which is outside the subtree it was "
                "given",
                exits.SCOPE_DENIED,
            )
        if not profiles.in_scope(result_href, profile.files_roots):
            raise FileError(
                f"the search returned {result_href}, which is outside this profile's files "
                "allowlist",
                exits.SCOPE_DENIED,
            )
        if _segments(result_href) == root_segments:
            # The searched collection is not one of its own results.
            continue
        found.append(reference)
        if limit is not None and len(found) > limit:
            raise FileError(
                f"the server returned more than the requested limit of {limit}",
                exits.MALFORMED_RESPONSE,
            )
    return sorted(found, key=lambda item: item.href)
