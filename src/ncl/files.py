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

from . import etag, exits, plans, profiles
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


def _prop_elements(response: ET.Element) -> dict[tuple[str, str], ET.Element]:
    found: dict[tuple[str, str], ET.Element] = {}
    for propstat in response:
        if _element_name(propstat) != (DAV, "propstat"):
            continue
        status = next(
            (item for item in propstat if _element_name(item) == (DAV, "status")), None
        )
        code = _status_code(status.text if status is not None else None)
        if code is None or not 200 <= code < 300:
            continue
        prop = next(
            (item for item in propstat if _element_name(item) == (DAV, "prop")), None
        )
        if prop is None:
            continue
        for element in prop:
            found[_element_name(element)] = element
    return found


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

    props = _prop_elements(entry)
    if not props:
        raise FileError("the WebDAV response returned no successful properties")
    resource_type = props.get((DAV, "resourcetype"))
    collection = resource_type is not None and any(
        _element_name(item) == (DAV, "collection") for item in resource_type
    )
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
    return [
        _file_ref(profile, entry)
        for entry in root
        if _element_name(entry) == (DAV, "response")
    ]


def list_collection(profile: Any, *, session: Session, href: str) -> list[FileRef]:
    """List exactly one scoped collection and no descendants below its children."""
    collection_href = _scoped(profile, href)
    response = session.request(
        "PROPFIND",
        collection_href,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=_PROPFIND,
    )
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


def read_file(profile: Any, *, session: Session, href: str) -> tuple[FileRef, bytes]:
    """Read one scoped file and retain its response metadata."""
    target = _scoped(profile, href)
    response = session.request(
        "GET", target, headers={"Accept": "*/*"}, max_redirects=0
    )
    _refuse_redirect(response, action="read", href=target)
    if response.status == 404:
        raise FileError(f"no file exists at {target}", exits.TARGET_NOT_FOUND)
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


def text_content(content: bytes) -> str:
    """Decode content that can safely pass through the redacting text stream."""
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
        profile=profile.name,
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
        profile=profile.name,
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


_ACTIONS = {"files.write", "files.delete"}


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
    if step.action == "files.delete":
        if body:
            raise plans.PlanError("file deletion steps must not carry a payload", exits.PLAN_STALE)
        _strong_etag(step.etag, "a file deletion")
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


def execute(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Execute one frozen file step and verify the exact resource."""
    validate_step(step)
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


def reconcile(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Read the exact file and classify the frozen file operation."""
    validate_step(step)
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
