"""Scoped Nextcloud file access over WebDAV."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets as token_source
import xml.etree.ElementTree as ET
from contextlib import suppress
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from . import etag, exits, plans, profiles, relocate, uploads
from .identity import DAV, _element_name, _status_code
from .session import Session, SessionError, absolute_url


class FileError(RuntimeError):
    """A file resource or WebDAV response was not usable."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class FileRef:
    """One file or collection and the state needed for conditional writes."""

    href: str
    name: str
    collection: bool
    size: int | None
    modified: str
    etag: str
    content_type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "href": self.href,
            "name": self.name,
            "collection": self.collection,
            "size": self.size,
            "modified": self.modified,
            "etag": self.etag,
            "content_type": self.content_type,
        }


_PROPFIND = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
    "<d:getetag/><d:getcontenttype/>"
    "</d:prop></d:propfind>"
)
_PROPERTIES = frozenset(
    {
        (DAV, "resourcetype"),
        (DAV, "getcontentlength"),
        (DAV, "getlastmodified"),
        (DAV, "getetag"),
        (DAV, "getcontenttype"),
    }
)
#: The alphabet a frozen SHA-256 identity is written in, lowercase so one
#: revision has exactly one spelling in a plan.
_HEX_DIGITS = "0123456789abcdef"
_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/javascript",
        "application/json",
        "application/ld+json",
        "application/xml",
        "application/xhtml+xml",
        "image/svg+xml",
    }
)


def _canonical(profile: Any, href: str) -> str:
    try:
        return absolute_url(profile, href)
    except SessionError as exc:
        raise FileError(exc.message, exc.code) from exc


def _scoped(profile: Any, href: str) -> str:
    resolved = _canonical(profile, href)
    if not profiles.in_scope(resolved, profile.files_roots):
        raise FileError(
            f"{resolved} is outside this profile's files allowlist", exits.SCOPE_DENIED
        )
    return resolved


def _segments(href: str) -> tuple[str, ...]:
    try:
        _, segments = profiles.canonicalize_href(href)
    except ValueError as exc:
        raise FileError("the server returned a malformed file href") from exc
    return segments


def _modified(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FileError("the server returned a malformed file modification time") from exc
    if parsed is None:
        raise FileError("the server returned a malformed file modification time")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).isoformat()


def _size(value: str, *, collection: bool) -> int | None:
    if not value:
        return None
    if not value.isascii() or not value.isdigit() or len(value) > 20:
        raise FileError("the server returned a malformed file size")
    parsed = int(value)
    return None if collection else parsed


def _prop_elements(
    response: ET.Element,
) -> tuple[dict[tuple[str, str], ET.Element], frozenset[tuple[str, str]]]:
    """Split one response's properties into those returned and those that do not exist.

    A `PROPFIND` names properties that may not apply to every resource it
    reaches, and RFC 4918 has the server say so with a `404` propstat rather
    than by omission — a collection has no content length to report. So `404`
    is an answer, recorded as absence, while every other failing status is the
    server declining to say and stays a refusal: a size withheld by a `403` is
    not a size of zero.
    """
    found: dict[tuple[str, str], ET.Element] = {}
    absent: set[tuple[str, str]] = set()
    for propstat in response:
        if _element_name(propstat) != (DAV, "propstat"):
            continue
        prop = next(
            (item for item in propstat if _element_name(item) == (DAV, "prop")), None
        )
        if prop is None:
            continue
        elements = list(prop)
        status = next(
            (item for item in propstat if _element_name(item) == (DAV, "status")), None
        )
        code = _status_code(status.text if status is not None else None)
        known = {_element_name(element) for element in elements} & _PROPERTIES
        if code is None:
            if known:
                raise FileError("the WebDAV property status was malformed")
            continue
        for element in elements:
            name = _element_name(element)
            if name not in _PROPERTIES:
                continue
            if name in found or name in absent:
                raise FileError(f"the WebDAV response repeated property {name[1]}")
            if code == 404:
                absent.add(name)
                continue
            if not 200 <= code < 300:
                raise FileError(
                    f"the WebDAV property {name[1]} was not returned successfully"
                )
            found[name] = element
    return found, frozenset(absent)


#: Properties a collection is entitled not to have. A collection carries no
#: entity body, so its length and media type describe nothing; every other
#: property this tool asks for identifies the resource or dates it, and a
#: resource that cannot be identified is not one this tool will act on.
_COLLECTION_OPTIONAL = frozenset({(DAV, "getcontentlength"), (DAV, "getcontenttype")})

#: Properties any resource may lack. A server is free to store no media type
#: for a file it cannot classify, and reporting that as malformed would refuse
#: a listing over a detail no caller depends on.
_ALWAYS_OPTIONAL = frozenset({(DAV, "getcontenttype")})


def _require_applicable(
    absent: frozenset[tuple[str, str]], *, collection: bool
) -> None:
    """Refuse an absence that would leave the resource unusable.

    Size and ETag are what a conditional write is built from, so a file
    reporting either as nonexistent is not a file this tool can safely address
    later. The same absence on a collection is the server answering correctly.
    """
    optional = _ALWAYS_OPTIONAL | (_COLLECTION_OPTIONAL if collection else frozenset())
    required = sorted(name[1] for name in absent - optional)
    if required:
        kind = "collection" if collection else "resource"
        raise FileError(
            f"the WebDAV {kind} reports no {', '.join(required)}, which this tool needs "
            "to address it"
        )


def _file_ref(
    profile: Any, entry: ET.Element
) -> tuple[str, FileRef | None, int | None]:
    href_element = next(
        (item for item in entry if _element_name(item) == (DAV, "href")), None
    )
    raw_href = (href_element.text or "").strip() if href_element is not None else ""
    if not raw_href:
        raise FileError("the WebDAV response omitted a resource href")
    href = _canonical(profile, raw_href)

    response_status = next(
        (item for item in entry if _element_name(item) == (DAV, "status")), None
    )
    response_code = _status_code(
        response_status.text if response_status is not None else None
    )
    if response_code is not None and not 200 <= response_code < 300:
        return href, None, response_code

    props, absent = _prop_elements(entry)
    if not props:
        raise FileError("the WebDAV response returned no successful properties")
    resource_type = props.get((DAV, "resourcetype"))
    if resource_type is None:
        raise FileError("the WebDAV response omitted the resource type")
    collection = any(
        _element_name(item) == (DAV, "collection") for item in resource_type
    )
    _require_applicable(absent, collection=collection)
    size_element = props.get((DAV, "getcontentlength"))
    modified_element = props.get((DAV, "getlastmodified"))
    etag_element = props.get((DAV, "getetag"))
    type_element = props.get((DAV, "getcontenttype"))
    segments = _segments(href)
    return (
        href,
        FileRef(
            href=href,
            name=segments[-1],
            collection=collection,
            size=_size(
                (size_element.text or "").strip() if size_element is not None else "",
                collection=collection,
            ),
            modified=_modified(
                (modified_element.text or "").strip()
                if modified_element is not None
                else ""
            ),
            etag=(etag_element.text or "").strip() if etag_element is not None else "",
            content_type=(type_element.text or "").strip() if type_element is not None else "",
        ),
        response_code,
    )


def _multistatus(
    profile: Any, body: bytes
) -> list[tuple[str, FileRef | None, int | None]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FileError("the WebDAV response was not valid XML") from exc
    if _element_name(root) != (DAV, "multistatus"):
        raise FileError("the WebDAV response was not a Multi-Status response")
    found: list[tuple[str, FileRef | None, int | None]] = []
    seen: set[tuple[str, ...]] = set()
    for entry in root:
        if _element_name(entry) != (DAV, "response"):
            continue
        item = _file_ref(profile, entry)
        key = _segments(item[0])
        if key in seen:
            raise FileError("the WebDAV response repeated a resource href")
        seen.add(key)
        found.append(item)
    return found


def list_collection(profile: Any, *, session: Session, href: str) -> list[FileRef]:
    """List exactly one scoped collection and no descendants below its children."""
    collection_href = _scoped(profile, href)
    response = session.request(
        "PROPFIND",
        collection_href,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=_PROPFIND,
        max_redirects=0,
    )
    _refuse_redirect(response, action="listing", href=collection_href)
    if response.status == 404:
        raise FileError(f"no file collection exists at {collection_href}", exits.TARGET_NOT_FOUND)
    if response.status != 207:
        raise FileError(
            "the file collection did not answer with a Multi-Status response",
            exits.MALFORMED_RESPONSE,
        )

    parent = _segments(collection_href)
    found: list[FileRef] = []
    saw_collection = False
    for response_href, reference, response_code in _multistatus(profile, response.body):
        child = _segments(response_href)
        if child == parent:
            if reference is None:
                if response_code == 404:
                    raise FileError(
                        f"no file collection exists at {collection_href}",
                        exits.TARGET_NOT_FOUND,
                    )
                raise FileError("the WebDAV listing returned a failed collection")
            saw_collection = saw_collection or reference.collection
            continue
        if len(child) != len(parent) + 1 or child[: len(parent)] != parent:
            raise FileError(
                "the WebDAV depth-one listing returned a resource outside its collection"
            )
        if reference is None:
            if response_code == 404:
                continue
            raise FileError("the WebDAV listing returned a failed resource")
        found.append(reference)
    if not saw_collection:
        raise FileError(
            "the requested WebDAV resource is not a collection",
            exits.UNSUPPORTED_STRUCTURE,
        )
    return sorted(found, key=lambda item: item.href)


def stat_resource(
    profile: Any,
    *,
    session: Session,
    href: str,
    missing_ok: bool = False,
) -> FileRef | None:
    """Read one resource's metadata without fetching its content."""
    target = _scoped(profile, href)
    response = session.request(
        "PROPFIND",
        target,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        data=_PROPFIND,
        max_redirects=0,
    )
    _refuse_redirect(response, action="metadata read", href=target)
    if response.status == 404:
        if missing_ok:
            return None
        raise FileError(f"no file exists at {target}", exits.TARGET_NOT_FOUND)
    if response.status != 207:
        raise FileError(
            "the file metadata request did not answer with a Multi-Status response",
            exits.MALFORMED_RESPONSE,
        )

    wanted = _segments(target)
    for response_href, reference, response_code in _multistatus(profile, response.body):
        if _segments(response_href) != wanted:
            continue
        if reference is not None:
            return reference
        if response_code == 404:
            if missing_ok:
                return None
            raise FileError(f"no file exists at {target}", exits.TARGET_NOT_FOUND)
        raise FileError("the WebDAV metadata response returned a failed resource")
    raise FileError("the WebDAV response omitted the requested resource")


def read_file(
    profile: Any, *, session: Session, href: str, if_match: str = ""
) -> tuple[FileRef, bytes]:
    """Read one scoped file and retain its response metadata.

    `if_match` ties the read to one exact revision. Without it a server that
    moved on between a metadata read and this one answers with content the
    caller would wrongly attribute to the revision it asked about; with it, the
    server refuses instead.
    """
    target = _scoped(profile, href)
    headers = {"Accept": "*/*"}
    if if_match:
        headers["If-Match"] = if_match
    response = session.request("GET", target, headers=headers, max_redirects=0)
    _refuse_redirect(response, action="read", href=target)
    if response.status == 404:
        raise FileError(f"no file exists at {target}", exits.TARGET_NOT_FOUND)
    if response.status == 412:
        raise FileError(
            f"the file at {target} changed while it was being read", exits.CONFLICT
        )
    if response.status != 200:
        raise FileError("the file could not be read", exits.SERVER_ERROR)
    return (
        FileRef(
            href=target,
            name=_segments(target)[-1],
            collection=False,
            size=len(response.body),
            modified=_modified(response.header("Last-Modified") or ""),
            etag=(response.header("ETag") or "").strip(),
            content_type=(response.header("Content-Type") or "").strip(),
        ),
        response.body,
    )


def text_content(content: bytes, *, content_type: str = "") -> str:
    """Decode declared textual content before it reaches the redacting stream."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not media_type.startswith("text/") and media_type not in _TEXT_MEDIA_TYPES:
        raise FileError(
            "the file does not declare a textual media type; use --output to write its exact bytes",
            exits.UNSUPPORTED_STRUCTURE,
        )
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileError(
            "the file is not UTF-8 text; use --output to write its exact bytes",
            exits.UNSUPPORTED_STRUCTURE,
        ) from exc


def write_local(path: str | Path, content: bytes, *, force: bool = False) -> Path:
    """Write exact response bytes locally without partial replacement."""
    target = Path(path).expanduser()
    if target.exists() and not force:
        raise FileError(
            f"local output {target} already exists; pass --force to replace it",
            exits.CONFLICT,
        )
    temporary = target.with_name(f".{target.name}.ncl-{token_source.token_hex(6)}")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
        if not force and target.exists():
            raise FileError(
                f"local output {target} appeared while writing; it was not replaced",
                exits.CONFLICT,
            )
        os.replace(temporary, target)
    except FileError:
        with suppress(OSError):
            temporary.unlink()
        raise
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink()
        raise FileError(
            "the local output file could not be written", exits.PRECONDITION_FAILED
        ) from exc
    return target


def _content_type(value: str) -> str:
    candidate = value.strip()
    media_type = candidate.split(";", 1)[0]
    if (
        not candidate
        or len(candidate) > 256
        or not candidate.isascii()
        or media_type.count("/") != 1
        or any(char.isspace() for char in media_type)
        or any(ord(char) < 32 or ord(char) == 127 for char in candidate)
    ):
        raise FileError("content type is malformed", exits.USAGE)
    return candidate


def plan_write_stream(
    profile: Any,
    *,
    session: Session,
    account_name: str,
    href: str,
    source: str,
    content_type: str = "application/octet-stream",
) -> plans.Plan:
    """Freeze a write whose content is too large to hold in the plan.

    What is frozen is the identity of the content rather than the content: the
    source path, its size, and its SHA-256. Applying re-reads the file and
    hashes it while sending, so a source that changed between planning and
    applying is a refusal rather than a silent upload of something else.
    """
    target = _scoped(profile, href)
    size, digest = uploads.measure(source)
    existing = stat_resource(profile, session=session, href=target, missing_ok=True)
    if existing is not None and existing.collection:
        raise FileError(
            "a collection cannot be replaced with file content",
            exits.UNSUPPORTED_STRUCTURE,
        )
    if existing is not None:
        _strong_etag(existing.etag, "a file replacement")
    return plans.write_bundle(
        profile=profile,
        summary=_segments(target)[-1],
        steps=(
            plans.freeze_step(
                action="files.upload",
                href=target,
                etag=existing.etag if existing is not None else "",
                summary=_segments(target)[-1],
                content_type=_content_type(content_type),
                details={
                    "exists": existing is not None,
                    "size": size,
                    "sha256": digest,
                    "source": str(Path(source).expanduser().resolve()),
                    "account_name": account_name,
                    "upload_token": token_source.token_hex(16),
                },
            ),
        ),
    )


def plan_write(
    profile: Any,
    *,
    session: Session,
    href: str,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> plans.Plan:
    """Freeze a conditional file creation or replacement."""
    target = _scoped(profile, href)
    existing = stat_resource(profile, session=session, href=target, missing_ok=True)
    if existing is not None and existing.collection:
        raise FileError(
            "a collection cannot be replaced with file content",
            exits.UNSUPPORTED_STRUCTURE,
        )
    existing_etag = ""
    if existing is not None:
        existing_etag = _strong_etag(existing.etag, "a file replacement")
    return plans.write_bundle(
        profile=profile,
        summary=_segments(target)[-1],
        steps=(
            plans.freeze_step(
                action="files.write",
                href=target,
                etag=existing_etag,
                summary=_segments(target)[-1],
                payload=content,
                content_type=_content_type(content_type),
                details={
                    "exists": existing is not None,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
            ),
        ),
    )


def plan_delete(profile: Any, *, session: Session, href: str) -> plans.Plan:
    """Freeze a conditional deletion of one file, never a collection."""
    target = _scoped(profile, href)
    existing = stat_resource(profile, session=session, href=target)
    assert existing is not None
    if existing.collection:
        raise FileError("collection deletion is not supported", exits.UNSUPPORTED_STRUCTURE)
    existing_etag = _strong_etag(existing.etag, "a file deletion")
    return plans.write_bundle(
        profile=profile,
        summary=existing.name,
        steps=(
            plans.freeze_step(
                action="files.delete",
                href=target,
                etag=existing_etag,
                summary=existing.name,
                details={"size": existing.size},
            ),
        ),
    )


def _freeze_content(
    profile: Any, *, session: Session, reference: FileRef, source_etag: str
) -> str:
    """Return the SHA-256 identity of the revision a move is planned against.

    Size is not identity. Two revisions of the same length are indistinguishable
    by it, so a move verified on length alone accepts a destination holding
    content that was never planned. The digest is taken over bytes read under
    the very ETag the metadata reported, and a revision that changed between the
    two reads is refused rather than hashed — the alternative is a plan that
    promises a state which never existed.
    """
    stored, content = read_file(
        profile, session=session, href=reference.href, if_match=source_etag
    )
    returned = etag.normalize_strong(stored.etag)
    if (returned is not None and returned != source_etag) or len(content) != reference.size:
        raise FileError(
            f"{reference.href} changed while its move was being planned; plan it again",
            exits.CONFLICT,
        )
    return hashlib.sha256(content).hexdigest()


def _frozen_digest(step: plans.Step) -> str:
    """Return the content identity a move step froze, refusing a malformed one."""
    digest = step.details.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or digest.strip(_HEX_DIGITS) != ""
    ):
        raise plans.PlanError(
            "a move step needs a SHA-256 content identity", exits.PLAN_STALE
        )
    return digest


def _destination_matches(
    profile: Any, *, session: Session, step: plans.Step, destination: str
) -> FileRef:
    """Read the moved resource back and hold it to the frozen identity."""
    moved = stat_resource(profile, session=session, href=destination, missing_ok=True)
    if moved is None or moved.collection or moved.size != step.details.get("size"):
        raise FileError(
            f"the resource at {destination} is not the file that was moved",
            exits.OUTCOME_UNCERTAIN,
        )
    _, content = read_file(profile, session=session, href=destination)
    if hashlib.sha256(content).hexdigest() != _frozen_digest(step):
        raise FileError(
            f"the content at {destination} is not the content that was moved",
            exits.OUTCOME_UNCERTAIN,
        )
    return moved


def plan_move(profile: Any, *, session: Session, href: str, destination: str) -> plans.Plan:
    """Freeze the relocation of one file, never a collection.

    Read-then-write-then-delete is not a move. It is three mutations with a
    window where both copies exist and another where neither is durable, it
    drops every property the round trip does not carry, and a failure between
    the write and the delete leaves the caller to work out which half landed.
    `MOVE` relocates the resource itself, so there is no intermediate state to
    reason about.

    A collection is refused for the reason its deletion is: it holds unbounded
    content that no plan can meaningfully show.
    """
    source = _scoped(profile, href)
    target = _scoped(profile, destination)
    if _segments(source) == _segments(target):
        raise FileError(f"the file already lives at {target}", exits.USAGE)
    existing = stat_resource(profile, session=session, href=source)
    assert existing is not None
    if existing.collection:
        raise FileError("collection moves are not supported", exits.UNSUPPORTED_STRUCTURE)
    occupant = stat_resource(profile, session=session, href=target, missing_ok=True)
    if occupant is not None:
        raise FileError(
            f"{target} already holds a resource; nothing was moved", exits.CONFLICT
        )
    source_etag = _strong_etag(existing.etag, "a file move")
    digest = _freeze_content(
        profile, session=session, reference=existing, source_etag=source_etag
    )
    return plans.write_bundle(
        profile=profile,
        summary=existing.name,
        steps=(
            plans.freeze_step(
                action="files.move",
                href=source,
                etag=source_etag,
                summary=existing.name,
                details={"destination": target, "size": existing.size, "sha256": digest},
            ),
        ),
    )


def plan_mkcol(profile: Any, *, session: Session, href: str) -> plans.Plan:
    """Freeze the creation of one collection under an allowlisted root."""
    target = _scoped(profile, href)
    if stat_resource(profile, session=session, href=target, missing_ok=True) is not None:
        raise FileError(f"{target} already exists; nothing was created", exits.CONFLICT)
    return plans.write_bundle(
        profile=profile,
        summary=_segments(target)[-1],
        steps=(
            plans.freeze_step(
                action="files.mkcol",
                href=target,
                etag="",
                summary=_segments(target)[-1],
                details={},
            ),
        ),
    )


def _move_destination(profile: Any, step: plans.Step) -> str:
    return _scoped(profile, str(step.details.get("destination", "")))


def _execute_move(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    source = _file_target(profile, step)
    destination = _move_destination(profile, step)
    response = session.request(
        "MOVE",
        source,
        headers=relocate.headers(destination, etag=step.etag),
        max_redirects=0,
    )
    _refuse_redirect(response, action="move", href=source)
    relocate.classify(
        response.status, fail=FileError, source=source, destination=destination
    )
    # Past this line the server has accepted the move, so nothing that follows
    # can report a clean failure: every way of not establishing the outcome —
    # a read that fails, content that does not hash to the frozen identity, a
    # source that will not confirm its own absence — is uncertainty.
    try:
        moved = _destination_matches(
            profile, session=session, step=step, destination=destination
        )
        if stat_resource(profile, session=session, href=source, missing_ok=True) is not None:
            raise FileError(
                f"the server still reports a file at {source} after the move",
                exits.OUTCOME_UNCERTAIN,
            )
    except Exception as exc:
        if isinstance(exc, FileError) and exc.code == exits.OUTCOME_UNCERTAIN:
            # Already the right verdict, and it names which check failed.
            raise
        raise FileError(
            f"the server accepted the move to {destination}, but its outcome could "
            "not be verified",
            exits.OUTCOME_UNCERTAIN,
        ) from exc
    return {
        "action": step.action,
        "href": destination,
        "moved_from": source,
        "etag": moved.etag,
        "size": moved.size,
        "verified": True,
    }


def _execute_mkcol(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    target = _file_target(profile, step)
    response = session.request("MKCOL", target, max_redirects=0)
    _refuse_redirect(response, action="collection creation", href=target)
    relocate.classify_creation(response.status, fail=FileError, href=target)
    created = stat_resource(profile, session=session, href=target, missing_ok=True)
    if created is None or not created.collection:
        raise FileError(
            f"the server accepted the collection, but {target} does not read back as one",
            exits.OUTCOME_UNCERTAIN,
        )
    return {
        "action": step.action,
        "href": target,
        "collection": True,
        "verified": True,
    }


def _reconcile_move(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    source = _file_target(profile, step)
    destination = _move_destination(profile, step)
    moved = stat_resource(profile, session=session, href=destination, missing_ok=True)
    remaining = stat_resource(profile, session=session, href=source, missing_ok=True)
    if moved is None:
        # Neither endpoint changed: the move never reached the server.
        return {"state": "pending" if remaining is not None else "uncertain"}
    if remaining is not None or moved.collection or moved.size != step.details.get("size"):
        return {"state": "uncertain"}
    try:
        _, content = read_file(profile, session=session, href=destination)
    except FileError:
        # A destination that exists but cannot be read leaves the move
        # unestablished, which is the one thing reconciliation must not call
        # verified.
        return {"state": "uncertain"}
    if hashlib.sha256(content).hexdigest() != _frozen_digest(step):
        return {"state": "uncertain"}
    return {"state": "verified"}


def _reconcile_mkcol(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    created = stat_resource(
        profile, session=session, href=_file_target(profile, step), missing_ok=True
    )
    if created is None:
        return {"state": "pending"}
    return {"state": "verified" if created.collection else "uncertain"}


_ACTIONS = {"files.write", "files.upload", "files.delete", "files.move", "files.mkcol"}


def _strong_etag(value: str, operation: str) -> str:
    candidate = etag.normalize_strong(value)
    if candidate is None:
        raise FileError(
            f"the server returned no strong quoted ETag, so {operation} cannot be conditional",
            exits.MALFORMED_RESPONSE,
        )
    return candidate


def validate_step(step: plans.Step) -> None:
    """Validate file step structure without reading the server."""
    if step.action not in _ACTIONS:
        raise plans.PlanError(f"unknown file plan action {step.action!r}", exits.USAGE)
    body = plans.payload_bytes(step)
    if step.action == "files.move":
        if body:
            raise plans.PlanError("file move steps must not carry a payload", exits.PLAN_STALE)
        if not str(step.details.get("destination", "")):
            raise plans.PlanError("a move step needs a destination", exits.PLAN_STALE)
        _frozen_digest(step)
        _strong_etag(step.etag, "a file move")
        return
    if step.action == "files.mkcol":
        if body or step.etag:
            raise plans.PlanError(
                "collection creation steps carry neither a payload nor an ETag",
                exits.PLAN_STALE,
            )
        return
    if step.action == "files.delete":
        if body:
            raise plans.PlanError("file deletion steps must not carry a payload", exits.PLAN_STALE)
        _strong_etag(step.etag, "a file deletion")
        return
    if step.action == "files.upload":
        if body:
            raise plans.PlanError(
                "a streamed write freezes its source's identity, not its bytes",
                exits.PLAN_STALE,
            )
        _content_type(step.content_type)
        _frozen_digest(step)
        size = step.details.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise plans.PlanError("a streamed write needs a frozen size", exits.PLAN_STALE)
        for name in ("source", "account_name", "upload_token"):
            if not str(step.details.get(name) or ""):
                raise plans.PlanError(
                    f"a streamed write needs a frozen {name}", exits.PLAN_STALE
                )
        if not isinstance(step.details.get("exists"), bool):
            raise plans.PlanError(
                "file write steps need an exists classification", exits.PLAN_STALE
            )
        return
    _content_type(step.content_type)
    exists = step.details.get("exists")
    if not isinstance(exists, bool):
        raise plans.PlanError("file write steps need an exists classification", exits.PLAN_STALE)
    if exists:
        _strong_etag(step.etag, "a file replacement")
    elif step.etag:
        raise plans.PlanError("file creations cannot carry an ETag", exits.PLAN_STALE)
    # Empty files are valid frozen writes; the body only needs to be valid base64.
    _ = body


def _file_target(profile: Any, step: plans.Step) -> str:
    return _scoped(profile, step.href)


def _response_url(response: Any) -> str:
    return getattr(response, "url", "") or ""


def _response_location(response: Any) -> str:
    header = getattr(response, "header", None)
    if not callable(header):
        return ""
    return header("Location") or ""


def _refuse_redirect(response: Any, *, action: str, href: str) -> None:
    if 300 <= response.status < 400 or _response_location(response):
        raise FileError(
            f"the server redirected file {action} at {href}; the target must remain exact",
            exits.MALFORMED_RESPONSE,
        )
    response_url = _response_url(response)
    if response_url and response_url != href:
        raise FileError(
            f"the server answered file {action} for a different href",
            exits.MALFORMED_RESPONSE,
        )


def _execute_upload(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Stream a large file into place, then confirm what landed."""
    target = _file_target(profile, step)
    details = step.details
    etag = uploads.stream_upload(
        profile,
        session=session,
        account_name=str(details["account_name"]),
        source=str(details["source"]),
        destination=target,
        size=int(details["size"]),
        digest=str(details["sha256"]),
        token=str(details["upload_token"]),
        overwrite=bool(details.get("exists")),
    )
    # Past the assembling MOVE the file exists, so every way of failing to
    # confirm it is uncertainty rather than failure.
    try:
        written = stat_resource(profile, session=session, href=target)
        assert written is not None
        if written.size != int(details["size"]):
            raise FileError(
                f"the assembled file is {written.size} bytes, not {details['size']}",
                exits.OUTCOME_UNCERTAIN,
            )
    except FileError as exc:
        raise FileError(exc.message, exits.OUTCOME_UNCERTAIN) from exc
    return {
        "action": step.action,
        "href": target,
        "etag": etag or written.etag,
        "size": written.size,
        "verified": True,
    }


def execute(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Execute one frozen file step and verify the exact resource."""
    validate_step(step)
    if step.action == "files.move":
        return _execute_move(profile, session=session, step=step)
    if step.action == "files.mkcol":
        return _execute_mkcol(profile, session=session, step=step)
    if step.action == "files.upload":
        return _execute_upload(profile, session=session, step=step)
    target = _file_target(profile, step)
    if step.action == "files.write":
        condition = {"If-Match": step.etag} if step.etag else {"If-None-Match": "*"}
        response = session.request(
            "PUT",
            target,
            headers={"Content-Type": _content_type(step.content_type), **condition},
            data=plans.payload_bytes(step),
            max_redirects=0,
        )
    else:
        response = session.request(
            "DELETE",
            target,
            headers={"If-Match": step.etag},
            max_redirects=0,
        )
    _refuse_redirect(
        response,
        action="write" if step.action == "files.write" else "deletion",
        href=target,
    )

    if response.status == 412:
        raise plans.PlanError(
            f"the file at {target} changed since the plan was made; re-plan against "
            "its current state",
            exits.CONFLICT,
        )
    if response.status == 404 and step.action == "files.delete":
        raise FileError(f"no file exists at {target}", exits.TARGET_NOT_FOUND)
    if response.status not in {200, 201, 204}:
        raise FileError(
            f"the server refused the {step.action} with status {response.status}",
            exits.SERVER_ERROR,
        )

    try:
        result: dict[str, Any] = {"action": step.action, "href": target}
        if step.action == "files.delete":
            if stat_resource(profile, session=session, href=target, missing_ok=True) is not None:
                raise FileError(
                    f"the server still reports a file at {target} after deletion",
                    exits.OUTCOME_UNCERTAIN,
                )
            result["verified"] = "deleted"
            return result

        stored, content = read_file(profile, session=session, href=target)
        expected = plans.payload_bytes(step)
        result.update(
            {"etag": stored.etag, "size": stored.size, "verified": content == expected}
        )
        if not result["verified"]:
            raise FileError(
                f"the server stored different content at {target}", exits.OUTCOME_UNCERTAIN
            )
        return result
    except Exception as exc:
        raise FileError(
            f"the server accepted {step.action}, but its final state could not be verified",
            exits.OUTCOME_UNCERTAIN,
        ) from exc


def _reconcile_upload(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Settle a streamed write without reading the whole file into memory.

    An absent destination means the assembling MOVE never happened, so nothing
    landed. A present one is only verified once its bytes hash to what the plan
    froze: a matching length is not a matching file.
    """
    target = _file_target(profile, step)
    size = int(step.details["size"])
    stored = stat_resource(profile, session=session, href=target, missing_ok=True)
    if stored is None:
        return {"state": "pending"}
    if stored.collection or stored.size != size:
        return {"state": "uncertain", "size": stored.size}
    digest = uploads.remote_digest(profile, session=session, href=target, size=size)
    if digest == _frozen_digest(step):
        return {"state": "verified", "etag": stored.etag, "size": stored.size}
    return {"state": "uncertain", "size": stored.size}


def reconcile(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Read the exact file and classify the frozen file operation."""
    validate_step(step)
    if step.action == "files.move":
        return _reconcile_move(profile, session=session, step=step)
    if step.action == "files.mkcol":
        return _reconcile_mkcol(profile, session=session, step=step)
    if step.action == "files.upload":
        return _reconcile_upload(profile, session=session, step=step)
    target = _file_target(profile, step)
    try:
        stored, content = read_file(profile, session=session, href=target)
    except FileError as exc:
        if exc.code == exits.TARGET_NOT_FOUND and step.action == "files.write":
            return {"state": "pending"}
        if exc.code == exits.TARGET_NOT_FOUND and step.action == "files.delete":
            return {"state": "verified"}
        if exc.code == exits.TARGET_NOT_FOUND:
            return {"state": "uncertain"}
        raise
    if step.action == "files.write":
        if content == plans.payload_bytes(step):
            return {"state": "verified"}
        old_etag = etag.normalize_strong(step.etag)
        current_etag = etag.normalize_strong(stored.etag)
        if step.details["exists"] and current_etag == old_etag:
            return {"state": "pending"}
        return {"state": "uncertain"}
    old_etag = etag.normalize_strong(step.etag)
    current_etag = etag.normalize_strong(stored.etag)
    return {"state": "pending" if current_etag == old_etag else "uncertain"}


def apply(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Execute one step for callers that use the resource module directly."""
    return execute(profile, session=session, step=step)
