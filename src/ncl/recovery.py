"""Undoing a deletion and an overwrite.

Deleting and replacing are the two mutations this tool performs that a caller
cannot take back from the surfaces it already has. Nextcloud keeps both:
a deleted file waits in a trash bin, and a replaced one leaves its previous
content as a version. Neither lives under `/files/`, so neither was reachable.

The allowlist question is different here, and getting it backwards would be the
whole risk. A trash entry's own href is under `/trashbin/`, which no profile
allowlists; what the allowlist has to bound is where the file *came from* and
where restoring would put it back, because that is the tree this tool is
permitted to change. So every entry is checked against its original location,
and an entry whose original location the profile does not admit is listed —
seeing what is in the bin is a read — but never restored or purged.

Purging is the narrowest gate in this module. Everything else here creates
content or moves it; a purge destroys the last copy, and there is nothing
further back to recover it from.
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from . import exits, plans, profiles, relocate
from .files import FileError, _canonical, _segments, file_id
from .identity import DAV, _element_name, _status_code
from .session import Session, SessionError

#: Nextcloud's own namespace, where the trash properties live.
NC = "http://nextcloud.org/ns"
OC = "http://owncloud.org/ns"

TRASH_ROOT = "/remote.php/dav/trashbin/"
VERSION_ROOT = "/remote.php/dav/versions/"
FILES_ROOT = "/remote.php/dav/files/"


@dataclass(frozen=True)
class TrashEntry:
    """One file waiting in the trash bin, and where it came from."""

    href: str
    name: str
    original_location: str
    original_href: str
    deleted_at: str
    size: int | None
    file_id: str
    collection: bool
    in_scope: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "href": self.href,
            "name": self.name,
            "original_location": self.original_location,
            "original_href": self.original_href,
            "deleted_at": self.deleted_at,
            "size": self.size,
            "file_id": self.file_id,
            "collection": self.collection,
            "in_scope": self.in_scope,
        }


@dataclass(frozen=True)
class Version:
    """One retained revision of a file."""

    href: str
    file_href: str
    version_id: str
    label: str
    size: int | None
    modified: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "href": self.href,
            "file_href": self.file_href,
            "version_id": self.version_id,
            "label": self.label,
            "size": self.size,
            "modified": self.modified,
        }


_TRASH_PROPFIND = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<d:propfind xmlns:d="DAV:" xmlns:nc="{NC}" xmlns:o="{OC}"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/>"
    "<o:fileid/>"
    "<nc:trashbin-filename/><nc:trashbin-original-location/><nc:trashbin-deletion-time/>"
    "</d:prop></d:propfind>"
)
_VERSION_PROPFIND = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<d:propfind xmlns:d="DAV:" xmlns:nc="{NC}"><d:prop>'
    "<d:getcontentlength/><d:getlastmodified/><d:getetag/><nc:version-label/>"
    "</d:prop></d:propfind>"
)
def _props(entry: ET.Element) -> dict[tuple[str, str], ET.Element]:
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


def _text(props: dict[tuple[str, str], ET.Element], name: tuple[str, str]) -> str:
    element = props.get(name)
    return (element.text or "").strip() if element is not None else ""


def _responses(body: bytes) -> list[tuple[str, dict[tuple[str, str], ET.Element]]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FileError("the response was not valid XML") from exc
    if _element_name(root) != (DAV, "multistatus"):
        raise FileError("the response was not a Multi-Status response")
    found = []
    for entry in root:
        if _element_name(entry) != (DAV, "response"):
            continue
        href_element = next((i for i in entry if _element_name(i) == (DAV, "href")), None)
        raw = (href_element.text or "").strip() if href_element is not None else ""
        if not raw:
            raise FileError("the response omitted a resource href")
        found.append((raw, _props(entry)))
    return found


def _deleted_at(value: str) -> str:
    if not value:
        return ""
    if not value.isascii() or not value.isdigit() or len(value) > 12:
        raise FileError("the server returned a malformed deletion time")
    return dt.datetime.fromtimestamp(int(value), dt.UTC).isoformat()


def _size(value: str) -> int | None:
    if not value:
        return None
    if not value.isascii() or not value.isdigit() or len(value) > 20:
        raise FileError("the server returned a malformed size")
    return int(value)


def _file_identifier(value: str) -> str:
    if not value or not value.isascii() or not value.isdigit() or len(value) > 20:
        raise FileError("the server returned a malformed file identifier")
    return value


def _optional_file_identifier(value: str) -> str:
    """Read an identifier where absence is legible rather than fatal.

    Listing the trash is a read, and the guide promises it shows every entry —
    including ones outside the allowlist, which can never be restored at all.
    An entry the server describes without an identifier is still an entry
    somebody wants to see, so it is listed without one and refused later, at
    the restore that actually needs it.
    """
    return _file_identifier(value) if value else ""


def trash_root(account_name: str) -> str:
    return f"{TRASH_ROOT}{quote(account_name, safe='')}/trash/"


def _original_href(profile: Any, account_name: str, location: str) -> str:
    relative = quote(location.strip("/"), safe="/")
    return _canonical(
        profile, f"{FILES_ROOT}{quote(account_name, safe='')}/{relative}"
    )


def list_trash(profile: Any, *, session: Session, account_name: str) -> list[TrashEntry]:
    """List what is in the trash bin, with where each item came from.

    Everything is listed, including items whose original location this profile
    does not admit. Seeing what is in the bin is a read, and a bin that hid
    entries would answer "what did I delete" with a filtered account of it;
    the allowlist decision is reported instead, and it is what gates a restore.
    """
    root = _canonical(profile, trash_root(account_name))
    response = session.request(
        "PROPFIND",
        root,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=_TRASH_PROPFIND,
    )
    if response.status == 404:
        raise FileError(
            "this account has no trash bin; the server may have it disabled",
            exits.UNSUPPORTED_COLLECTION,
        )
    if response.status != 207:
        raise FileError(
            f"the trash bin answered {response.status} rather than Multi-Status"
        )

    root_segments = _segments(root)
    entries: list[TrashEntry] = []
    for raw_href, props in _responses(response.body):
        href = _canonical(profile, raw_href)
        if _segments(href) == root_segments:
            continue
        location = _text(props, (NC, "trashbin-original-location"))
        if not location:
            raise FileError("a trash entry reported no original location")
        original = _original_href(profile, account_name, location)
        resource_type = props.get((DAV, "resourcetype"))
        entries.append(
            TrashEntry(
                href=href,
                name=_text(props, (NC, "trashbin-filename")) or unquote(
                    urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1]
                ),
                original_location=location,
                original_href=original,
                deleted_at=_deleted_at(_text(props, (NC, "trashbin-deletion-time"))),
                size=_size(_text(props, (DAV, "getcontentlength"))),
                file_id=_optional_file_identifier(_text(props, (OC, "fileid"))),
                collection=resource_type is not None
                and any(_element_name(c) == (DAV, "collection") for c in resource_type),
                in_scope=profiles.in_scope(original, profile.files_roots),
            )
        )
    return sorted(entries, key=lambda item: (item.deleted_at, item.href), reverse=True)


def _entry(profile: Any, entries: list[TrashEntry], href: str) -> TrashEntry:
    target = _segments(_canonical(profile, href))
    for entry in entries:
        if _segments(entry.href) == target:
            return entry
    raise FileError(f"no trash entry exists at {href}", exits.TARGET_NOT_FOUND)


def _restorable(entry: TrashEntry) -> TrashEntry:
    if not entry.in_scope:
        raise FileError(
            f"{entry.original_location} is outside this profile's files allowlist, so this "
            "entry can be seen but not restored",
            exits.SCOPE_DENIED,
        )
    return entry


def plan_restore(
    profile: Any, *, session: Session, account_name: str, href: str
) -> plans.Plan:
    """Freeze the restoration of one trash entry to where it came from.

    A restore whose original location is occupied is refused here rather than
    sent. The server does not overwrite in that case — it renames what it
    restores, landing the file beside the occupant under a name nobody asked
    for — so a plan that promised the original path would be describing
    something the server was never going to do.
    """
    from .files import stat_resource

    entry = _restorable(
        _entry(profile, list_trash(profile, session=session, account_name=account_name), href)
    )
    if not entry.file_id:
        # The restore is verified by identity: source absence alone cannot
        # prove where the file landed. Without one there is nothing to check
        # the result against, so the plan is refused rather than made
        # unverifiable.
        raise FileError(
            f"{entry.original_location} carries no file identifier, so a restore of it "
            "could not be verified",
            exits.MALFORMED_RESPONSE,
        )
    occupant = stat_resource(
        profile, session=session, href=entry.original_href, missing_ok=True
    )
    if occupant is not None:
        raise FileError(
            f"{entry.original_location} is occupied; the server would restore beside it "
            "under a different name rather than overwrite. Move or delete what is there "
            "first",
            exits.CONFLICT,
        )
    return plans.write_bundle(
        profile=profile,
        summary=f"restore {entry.original_location}",
        steps=(
            plans.freeze_step(
                action="trash.restore",
                href=entry.href,
                etag="",
                summary=f"restore {entry.original_location}",
                details={
                    "destination": f"{TRASH_ROOT}{quote(account_name, safe='')}/restore/"
                    f"{quote(entry.name, safe='')}",
                    "original_href": entry.original_href,
                    "original_location": entry.original_location,
                    "deleted_at": entry.deleted_at,
                    "size": entry.size,
                    "file_id": entry.file_id,
                },
            ),
        ),
    )


def plan_purge(
    profile: Any, *, session: Session, account_name: str, href: str
) -> plans.Plan:
    """Freeze the permanent removal of one trash entry.

    This is the narrowest gate in the tool. Everything else it does leaves a
    copy somewhere; this removes the last one, and the trash bin is where the
    remedy for every other deletion lives.
    """
    entry = _restorable(
        _entry(profile, list_trash(profile, session=session, account_name=account_name), href)
    )
    return plans.write_bundle(
        profile=profile,
        summary=f"permanently destroy {entry.original_location}",
        steps=(
            plans.freeze_step(
                action="trash.purge",
                href=entry.href,
                etag="",
                summary=f"permanently destroy {entry.original_location}",
                details={
                    "original_location": entry.original_location,
                    "deleted_at": entry.deleted_at,
                    "size": entry.size,
                    "irreversible": True,
                },
            ),
        ),
    )


def list_versions(
    profile: Any, *, session: Session, account_name: str, file_href: str
) -> list[Version]:
    """List the retained revisions of one allowlisted file.

    Versions are keyed by the server's file identifier rather than by path, so
    a renamed file keeps its history. The current content is not among them.
    """
    from .files import _scoped

    target = _scoped(profile, file_href)
    identifier = file_id(profile, session=session, href=target)
    collection = _canonical(
        profile, f"{VERSION_ROOT}{quote(account_name, safe='')}/versions/{identifier}/"
    )
    response = session.request(
        "PROPFIND",
        collection,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=_VERSION_PROPFIND,
    )
    if response.status == 404:
        return []
    if response.status != 207:
        raise FileError(
            f"the version collection answered {response.status} rather than Multi-Status"
        )

    collection_segments = _segments(collection)
    versions: list[Version] = []
    for raw_href, props in _responses(response.body):
        href = _canonical(profile, raw_href)
        if _segments(href) == collection_segments:
            continue
        versions.append(
            Version(
                href=href,
                file_href=target,
                version_id=unquote(urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1]),
                label=_text(props, (NC, "version-label")),
                size=_size(_text(props, (DAV, "getcontentlength"))),
                modified=_text(props, (DAV, "getlastmodified")),
            )
        )
    return sorted(versions, key=lambda item: item.version_id, reverse=True)


def plan_version_restore(
    profile: Any,
    *,
    session: Session,
    account_name: str,
    file_href: str,
    version_id: str,
) -> plans.Plan:
    """Freeze the restoration of one retained revision over the current content.

    The current content is not lost by this: replacing it makes it a version in
    turn. That is what makes this a safe mutation and a purge an unsafe one,
    and the preview says which revision would win.
    """
    versions = list_versions(
        profile, session=session, account_name=account_name, file_href=file_href
    )
    matched = [item for item in versions if item.version_id == str(version_id).strip()]
    if not matched:
        raise FileError(
            f"{file_href} has no retained version {version_id!r}", exits.TARGET_NOT_FOUND
        )
    version = matched[0]
    return plans.write_bundle(
        profile=profile,
        summary=f"restore version {version.version_id} of {version.file_href}",
        steps=(
            plans.freeze_step(
                action="version.restore",
                href=version.href,
                etag="",
                summary=f"restore version {version.version_id} of {version.file_href}",
                details={
                    "destination": f"{VERSION_ROOT}{quote(account_name, safe='')}/restore/target",
                    "file_href": version.file_href,
                    "version_id": version.version_id,
                    "size": version.size,
                    "modified": version.modified,
                },
            ),
        ),
    )


_ACTIONS = {"trash.restore", "trash.purge", "version.restore"}


def validate_step(step: plans.Step) -> None:
    """Reject a frozen recovery step before the bundle makes a request."""
    if step.action not in _ACTIONS:
        raise plans.PlanError(f"unknown recovery action {step.action!r}", exits.USAGE)
    if plans.payload_bytes(step):
        raise plans.PlanError("recovery steps carry no payload", exits.PLAN_STALE)
    if step.action == "trash.purge":
        if not step.details.get("irreversible"):
            raise plans.PlanError(
                "a purge step must record that it is irreversible", exits.PLAN_STALE
            )
        return
    if not str(step.details.get("destination") or ""):
        raise plans.PlanError("a recovery step needs a destination", exits.PLAN_STALE)
    if step.action == "trash.restore":
        try:
            _file_identifier(str(step.details.get("file_id") or ""))
        except FileError as exc:
            raise plans.PlanError(
                "a trash restore needs a valid file identifier", exits.PLAN_STALE
            ) from exc
        if not str(step.details.get("original_href") or ""):
            raise plans.PlanError("a trash restore needs its original href", exits.PLAN_STALE)


def execute(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Perform one frozen recovery step."""
    validate_step(step)
    source = _canonical(profile, step.href)
    if step.action == "trash.purge":
        response = session.request("DELETE", source, max_redirects=0)
        if response.status == 404:
            return {"action": step.action, "href": source, "verified": "already absent"}
        if response.status not in {200, 204}:
            raise FileError(
                f"the purge answered {response.status}",
                exits.CONFLICT if response.status == 412 else exits.SERVER_ERROR,
            )
        return {"action": step.action, "href": source, "verified": "destroyed"}

    destination = _canonical(profile, str(step.details["destination"]))
    landed = step.details.get("original_href") or step.details.get("file_href")
    if step.action == "trash.restore":
        from .files import stat_resource

        if stat_resource(profile, session=session, href=str(landed), missing_ok=True) is not None:
            raise FileError(
                f"{landed} became occupied since the plan was made; nothing was restored",
                exits.CONFLICT,
            )
    # The restore endpoints are virtual targets that always report themselves as
    # existing, so `Overwrite: F` makes every restore a 412. The protection that
    # header would give is enforced by checking immediately before the MOVE and
    # by verifying the restored resource's server identity afterwards.
    try:
        response = session.request(
            "MOVE",
            source,
            headers={"Destination": destination},
            max_redirects=0,
        )
    except SessionError as exc:
        raise FileError(
            "the restore request was sent but its outcome could not be confirmed",
            exits.OUTCOME_UNCERTAIN,
        ) from exc
    relocate.classify(
        response.status, fail=FileError, source=source, destination=destination
    )
    if step.action == "trash.restore":
        # Past the MOVE something has been restored; the file id distinguishes
        # that entry from an interloper that raced into the promised path.
        try:
            restored_id = file_id(profile, session=session, href=str(landed))
        except Exception as exc:
            raise FileError(
                f"the restore was accepted but {landed} cannot be confirmed",
                exits.OUTCOME_UNCERTAIN,
            ) from exc
        if restored_id != step.details["file_id"]:
            raise FileError(
                f"the restore was accepted but {landed} holds a different resource; the "
                "server may have restored the entry under another name",
                exits.OUTCOME_UNCERTAIN,
            )
    else:
        from .files import stat_resource

        try:
            restored = stat_resource(
                profile, session=session, href=str(landed), missing_ok=True
            )
        except Exception as exc:
            raise FileError(
                f"the restore was accepted but {landed} cannot be confirmed",
                exits.OUTCOME_UNCERTAIN,
            ) from exc
        expected_size = step.details.get("size")
        if (
            restored is None
            or restored.collection
            or (expected_size is not None and restored.size != expected_size)
        ):
            raise FileError(
                f"the restore was accepted but {landed} does not read back as the planned revision",
                exits.OUTCOME_UNCERTAIN,
            )
    result = {
        "action": step.action,
        "href": source,
        "restored": landed,
        "verified": True,
    }
    if step.action == "version.restore":
        result["size"] = restored.size
    return result


def reconcile(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Read back whether a recovery step took effect."""
    validate_step(step)
    source = _canonical(profile, step.href)
    present = session.request(
        "PROPFIND",
        source,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        data=_VERSION_PROPFIND,
    )
    if step.action == "trash.purge":
        if present.status == 404:
            return {"action": step.action, "state": "verified", "verified": "destroyed"}
        return {"action": step.action, "state": "pending"}
    # A restore consumes the source. For trash, its absence is necessary but not
    # sufficient: an occupied target makes the server restore under another name.
    if present.status == 404:
        if step.action == "trash.restore":
            target = str(step.details["original_href"])
            try:
                restored_id = file_id(profile, session=session, href=target)
            except FileError:
                return {"action": step.action, "state": "uncertain"}
            expected = str(step.details["file_id"])
            return {
                "action": step.action,
                "state": "verified" if restored_id == expected else "uncertain",
            }
        from .files import stat_resource

        try:
            restored = stat_resource(
                profile,
                session=session,
                href=str(step.details["file_href"]),
                missing_ok=True,
            )
        except FileError:
            return {"action": step.action, "state": "uncertain"}
        expected_size = step.details.get("size")
        state = (
            "uncertain"
            if restored is None
            or restored.collection
            or (expected_size is not None and restored.size != expected_size)
            else "verified"
        )
        return {"action": step.action, "state": state}
    return {"action": step.action, "state": "pending"}
