"""Tags and comments on files.

Both say something *about* a file without changing its bytes. A tag is a shared
label: the same tag object is attached to files across the instance, it is
visible to everyone who can see the file, and the tag list itself is
instance-wide rather than private to an account. A comment is a note attached
to one file, addressed by the file's server identifier so it survives a rename.

Tag creation is irreversible from here. The server accepts `POST systemtags/`
from an ordinary account and refuses `DELETE systemtags/<id>` with 403, so only
an administrator can take a tag back out of the instance-wide list. That is why
creating one carries the same `irreversible` marker as a trash purge, and why
assigning one is planned like a share: it changes what other people see.

Custom DAV properties are not offered. The server answers a `PROPPATCH` of an
arbitrary property with 200 and then does not store it, so a command built on
one would report success for a write that never happened.
"""

from __future__ import annotations

import datetime as dt
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from . import exits, identity, plans
from .files import _canonical, _scoped, _segments, file_id
from .identity import DAV, _element_name, _status_code
from .session import Session, SessionError

#: The namespaces the tag and comment properties live in.
OC = "http://owncloud.org/ns"
NC = "http://nextcloud.org/ns"

TAGS_ROOT = "/remote.php/dav/systemtags/"
RELATIONS_ROOT = "/remote.php/dav/systemtags-relations/files/"
COMMENTS_ROOT = "/remote.php/dav/comments/files/"

#: Who sees a tag once it is assigned. Named in every assignment plan, because
#: the file itself reads back the same whether or not it carries one.
TAG_REACH = "everyone who can see the file"

#: Why a tag creation is not undoable by this tool.
TAG_WARNING = "a tag cannot be deleted by a non-administrator account"

_TAG_PROPFIND = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<d:propfind xmlns:d="DAV:" xmlns:o="{OC}"><d:prop>'
    "<o:id/><o:display-name/><o:user-visible/><o:user-assignable/><o:can-assign/>"
    "</d:prop></d:propfind>"
)
_FILE_TAGS_PROPFIND = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<d:propfind xmlns:d="DAV:" xmlns:nc="{NC}"><d:prop>'
    "<nc:system-tags/>"
    "</d:prop></d:propfind>"
)
_COMMENT_PROPFIND = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<d:propfind xmlns:d="DAV:" xmlns:o="{OC}"><d:prop>'
    "<o:id/><o:message/><o:actorId/><o:actorDisplayName/>"
    "<o:creationDateTime/><o:verb/><o:isUnread/>"
    "</d:prop></d:propfind>"
)


class AnnotationError(RuntimeError):
    """A tag or comment could not be read, planned, or applied."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Tag:
    """One instance-wide label, and whether this account may use it."""

    id: str
    name: str
    user_visible: bool
    user_assignable: bool
    can_assign: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "user_visible": self.user_visible,
            "user_assignable": self.user_assignable,
            "can_assign": self.can_assign,
        }


@dataclass(frozen=True)
class Comment:
    """One note on one file, and who left it."""

    id: str
    href: str
    message: str
    actor_id: str
    actor_name: str
    created: str
    verb: str
    unread: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "href": self.href,
            "message": self.message,
            "actor_id": self.actor_id,
            "actor_name": self.actor_name,
            "created": self.created,
            "verb": self.verb,
            "unread": self.unread,
        }


def _props(entry: ET.Element) -> dict[tuple[str, str], ET.Element]:
    """Collect the properties one response reports successfully."""
    found: dict[tuple[str, str], ET.Element] = {}
    for propstat in entry:
        if _element_name(propstat) != (DAV, "propstat"):
            continue
        status = next((i for i in propstat if _element_name(i) == (DAV, "status")), None)
        text = status.text if status is not None else None
        code = _status_code(text)
        if code is None:
            raise AnnotationError("the server returned a malformed propstat status")
        if not 200 <= code < 300:
            continue
        prop = next((i for i in propstat if _element_name(i) == (DAV, "prop")), None)
        if prop is None:
            continue
        for element in prop:
            found[_element_name(element)] = element
    return found


def _responses(body: bytes) -> list[tuple[str, dict[tuple[str, str], ET.Element]]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise AnnotationError("the response was not valid XML") from exc
    if _element_name(root) != (DAV, "multistatus"):
        raise AnnotationError("the response was not a Multi-Status response")
    found = []
    for entry in root:
        if _element_name(entry) != (DAV, "response"):
            continue
        href = next((i for i in entry if _element_name(i) == (DAV, "href")), None)
        raw = (href.text or "").strip() if href is not None else ""
        if not raw:
            raise AnnotationError("the response omitted a resource href")
        found.append((raw, _props(entry)))
    return found


def _text(props: dict[tuple[str, str], ET.Element], name: tuple[str, str]) -> str:
    element = props.get(name)
    return (element.text or "").strip() if element is not None else ""


def _flag(value: str) -> bool:
    text = value.strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0", ""}:
        return False
    raise AnnotationError("the server reported a flag that is neither true nor false")


def _identifier(value: str, *, what: str) -> str:
    if not value or not value.isascii() or not value.isdigit() or len(value) > 20:
        raise AnnotationError(f"the server returned a malformed {what} identifier")
    return value


def _attribute(element: ET.Element, name: str) -> str:
    """Read one attribute whether or not the server namespaces it."""
    value = element.get(f"{{{OC}}}{name}")
    if value is None:
        value = element.get(name, "")
    return value.strip()


def _last_segment(location: str) -> str:
    return urlsplit(location).path.rstrip("/").rsplit("/", 1)[-1]


def _created(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AnnotationError("the server returned a malformed comment date") from exc
    if parsed is None:
        raise AnnotationError("the server returned a malformed comment date")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).isoformat()


def _account_name(profile: Any, session: Session) -> str:
    return identity.discover(profile, session=session).account_name


def list_tags(profile: Any, *, session: Session) -> list[Tag]:
    """List every tag this account can see on the instance."""
    root = _canonical(profile, TAGS_ROOT)
    response = session.request(
        "PROPFIND",
        root,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=_TAG_PROPFIND,
    )
    if response.status != 207:
        raise AnnotationError(
            f"the tag collection answered {response.status} rather than Multi-Status"
        )
    root_segments = _segments(root)
    tags: list[Tag] = []
    for raw_href, props in _responses(response.body):
        if _segments(_canonical(profile, raw_href)) == root_segments:
            continue
        tags.append(
            Tag(
                id=_identifier(_text(props, (OC, "id")), what="tag"),
                name=_text(props, (OC, "display-name")),
                user_visible=_flag(_text(props, (OC, "user-visible"))),
                user_assignable=_flag(_text(props, (OC, "user-assignable"))),
                can_assign=_flag(_text(props, (OC, "can-assign"))),
            )
        )
    return sorted(tags, key=lambda item: (item.name, item.id))


def file_tags(profile: Any, *, session: Session, href: str) -> list[Tag]:
    """List the tags one allowlisted file carries."""
    target = _scoped(profile, href)
    response = session.request(
        "PROPFIND",
        target,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        data=_FILE_TAGS_PROPFIND,
    )
    if response.status == 404:
        raise AnnotationError(f"no file exists at {target}", exits.TARGET_NOT_FOUND)
    if response.status != 207:
        raise AnnotationError(
            f"the file answered {response.status} rather than Multi-Status"
        )
    tags: list[Tag] = []
    for _raw_href, props in _responses(response.body):
        container = props.get((NC, "system-tags"))
        if container is None:
            continue
        for element in container:
            if _element_name(element) != (NC, "system-tag"):
                continue
            tags.append(
                Tag(
                    id=_identifier(_attribute(element, "id"), what="tag"),
                    name=(element.text or "").strip(),
                    user_visible=_flag(_attribute(element, "user-visible")),
                    user_assignable=_flag(_attribute(element, "user-assignable")),
                    can_assign=_flag(_attribute(element, "can-assign")),
                )
            )
    return sorted(tags, key=lambda item: (item.name, item.id))


def list_comments(profile: Any, *, session: Session, href: str) -> list[Comment]:
    """List the comments on one allowlisted file, oldest first."""
    identifier = file_id(profile, session=session, href=_scoped(profile, href))
    return _comments(profile, session=session, file_identifier=identifier)


def _comments(profile: Any, *, session: Session, file_identifier: str) -> list[Comment]:
    collection = _canonical(profile, f"{COMMENTS_ROOT}{file_identifier}/")
    response = session.request(
        "PROPFIND",
        collection,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=_COMMENT_PROPFIND,
    )
    if response.status == 404:
        return []
    if response.status != 207:
        raise AnnotationError(
            f"the comment collection answered {response.status} rather than Multi-Status"
        )
    collection_segments = _segments(collection)
    comments: list[Comment] = []
    for raw_href, props in _responses(response.body):
        href = _canonical(profile, raw_href)
        if _segments(href) == collection_segments:
            continue
        comments.append(
            Comment(
                id=_identifier(_text(props, (OC, "id")), what="comment"),
                href=href,
                message=(props[(OC, "message")].text or "")
                if (OC, "message") in props
                else "",
                actor_id=_text(props, (OC, "actorId")),
                actor_name=_text(props, (OC, "actorDisplayName")),
                created=_created(_text(props, (OC, "creationDateTime"))),
                verb=_text(props, (OC, "verb")),
                unread=_flag(_text(props, (OC, "isUnread"))),
            )
        )
    return sorted(comments, key=lambda item: int(item.id))


def _named(tags: list[Tag], name: str) -> Tag:
    wanted = name.strip()
    matched = [item for item in tags if item.name == wanted]
    if len(matched) > 1:
        raise AnnotationError(
            f"more than one tag is named {wanted!r}; this tool addresses tags by name, "
            "so which one was meant cannot be told",
            exits.AMBIGUOUS_TARGET,
        )
    if not matched:
        raise AnnotationError(f"no tag is named {wanted!r}", exits.TARGET_NOT_FOUND)
    return matched[0]


def plan_create_tag(profile: Any, *, name: str) -> plans.Plan:
    """Freeze the creation of one instance-wide tag.

    A tag created here stays: the server refuses its deletion to anything but
    an administrator account, so the preview says so before the creation is
    approved rather than after.
    """
    label = str(name or "").strip()
    if not label:
        raise AnnotationError("a tag needs a name", exits.USAGE)
    summary = f"create tag {label}"
    return plans.write_bundle(
        profile=profile,
        summary=summary,
        steps=(
            plans.freeze_step(
                action="tag.create",
                href=_canonical(profile, TAGS_ROOT),
                etag="",
                summary=summary,
                details={
                    "name": label,
                    "irreversible": True,
                    "warning": TAG_WARNING,
                },
            ),
        ),
    )


def _assignment(
    profile: Any, *, session: Session, href: str, tag: str, assign: bool
) -> plans.Plan:
    target = _scoped(profile, href)
    identifier = file_id(profile, session=session, href=target)
    wanted = _named(list_tags(profile, session=session), tag)
    if not wanted.can_assign:
        raise AnnotationError(
            f"this account may not assign the tag {wanted.name!r}", exits.SCOPE_DENIED
        )
    carried = {item.id for item in file_tags(profile, session=session, href=target)}
    if assign and wanted.id in carried:
        raise AnnotationError(
            f"{target} already carries the tag {wanted.name!r}", exits.USAGE
        )
    if not assign and wanted.id not in carried:
        raise AnnotationError(
            f"{target} does not carry the tag {wanted.name!r}", exits.USAGE
        )
    verb = "tag" if assign else "untag"
    summary = f"{verb} {target} with {wanted.name}"
    return plans.write_bundle(
        profile=profile,
        summary=summary,
        steps=(
            plans.freeze_step(
                action="tag.assign" if assign else "tag.unassign",
                href=target,
                etag="",
                summary=summary,
                details={
                    "file_id": identifier,
                    "tag_id": wanted.id,
                    "tag_name": wanted.name,
                    "reach": TAG_REACH,
                },
            ),
        ),
    )


def plan_tag(profile: Any, *, session: Session, href: str, tag: str) -> plans.Plan:
    """Freeze the assignment of one existing tag to one allowlisted file.

    A tag is visible to everyone who can see the file and says nothing about
    itself from the file's content, so the preview names both the tag and the
    file and the assignment passes the same boundary as a share.
    """
    return _assignment(profile, session=session, href=href, tag=tag, assign=True)


def plan_untag(profile: Any, *, session: Session, href: str, tag: str) -> plans.Plan:
    """Freeze the removal of one tag from one allowlisted file."""
    return _assignment(profile, session=session, href=href, tag=tag, assign=False)


def plan_comment(
    profile: Any, *, session: Session, href: str, message: str
) -> plans.Plan:
    """Freeze one comment on one allowlisted file."""
    text = str(message or "").strip()
    if not text:
        raise AnnotationError("a comment needs a message", exits.USAGE)
    target = _scoped(profile, href)
    identifier = file_id(profile, session=session, href=target)
    summary = f"comment on {target}"
    return plans.write_bundle(
        profile=profile,
        summary=summary,
        steps=(
            plans.freeze_step(
                action="comment.create",
                href=target,
                etag="",
                summary=summary,
                details={
                    "file_id": identifier,
                    "message": text,
                    # Reconciling reads the listing back, where an earlier
                    # comment carrying the same words is indistinguishable
                    # from this one except by when it was posted. The server
                    # dates a comment to the second, so the floor is whole
                    # seconds too and a comment posted within this one counts.
                    "planned_at": dt.datetime.now(dt.UTC)
                    .replace(microsecond=0)
                    .isoformat(),
                },
            ),
        ),
    )


def plan_uncomment(
    profile: Any, *, session: Session, href: str, comment_id: str
) -> plans.Plan:
    """Freeze the removal of one comment this account left.

    The server lets an author remove their own comment and nobody else's, so a
    comment by another actor is refused here rather than sent and rejected.
    """
    target = _scoped(profile, href)
    identifier = file_id(profile, session=session, href=target)
    wanted = str(comment_id or "").strip()
    matched = [
        item
        for item in _comments(profile, session=session, file_identifier=identifier)
        if item.id == wanted
    ]
    if not matched:
        raise AnnotationError(
            f"{target} has no comment {wanted!r}", exits.TARGET_NOT_FOUND
        )
    comment = matched[0]
    account = _account_name(profile, session)
    if comment.actor_id != account:
        raise AnnotationError(
            f"comment {comment.id} was left by {comment.actor_id or 'another actor'}, "
            "and only its author may remove it",
            exits.SCOPE_DENIED,
        )
    summary = f"remove comment {comment.id} from {target}"
    return plans.write_bundle(
        profile=profile,
        summary=summary,
        steps=(
            plans.freeze_step(
                action="comment.delete",
                href=target,
                etag="",
                summary=summary,
                details={
                    "file_id": identifier,
                    "comment_id": comment.id,
                    "message": comment.message,
                },
            ),
        ),
    )


_ACTIONS = {"tag.create", "tag.assign", "tag.unassign", "comment.create", "comment.delete"}


def validate_step(step: plans.Step) -> None:
    """Reject a frozen annotation step before the bundle makes a request."""
    details = step.details
    if step.action not in _ACTIONS:
        raise plans.PlanError(f"unknown annotation action {step.action!r}", exits.USAGE)
    if plans.payload_bytes(step):
        raise plans.PlanError("annotation steps carry no payload", exits.PLAN_STALE)
    if step.action == "tag.create":
        if not str(details.get("name") or "").strip():
            raise plans.PlanError("the plan names no tag to create", exits.PLAN_STALE)
        if not details.get("irreversible"):
            raise plans.PlanError(
                "a tag creation must record that it is irreversible", exits.PLAN_STALE
            )
        return
    try:
        _identifier(str(details.get("file_id") or ""), what="file")
    except AnnotationError as exc:
        raise plans.PlanError(
            "an annotation step needs a valid file identifier", exits.PLAN_STALE
        ) from exc
    if step.action in {"tag.assign", "tag.unassign"}:
        try:
            _identifier(str(details.get("tag_id") or ""), what="tag")
        except AnnotationError as exc:
            raise plans.PlanError("the plan names no tag", exits.PLAN_STALE) from exc
        return
    if step.action == "comment.create":
        if not str(details.get("message") or ""):
            raise plans.PlanError("the plan names no message", exits.PLAN_STALE)
        return
    try:
        _identifier(str(details.get("comment_id") or ""), what="comment")
    except AnnotationError as exc:
        raise plans.PlanError("the plan names no comment", exits.PLAN_STALE) from exc


def _relation_href(profile: Any, details: dict[str, Any]) -> str:
    return _canonical(
        profile, f"{RELATIONS_ROOT}{details['file_id']}/{details['tag_id']}"
    )


def _sent(message: str) -> AnnotationError:
    return AnnotationError(message, exits.OUTCOME_UNCERTAIN)


def _created_tag(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    name = str(step.details["name"])
    body = json.dumps(
        {"name": name, "userVisible": True, "userAssignable": True}
    ).encode("utf-8")
    try:
        response = session.request(
            "POST",
            _canonical(profile, TAGS_ROOT),
            headers={"Content-Type": "application/json"},
            data=body,
        )
    except SessionError as exc:
        raise _sent(
            f"the request to create the tag {name!r} was sent but its answer was lost; "
            "reconcile decides whether the tag exists"
        ) from exc
    if response.status == 409:
        raise AnnotationError(f"a tag named {name!r} already exists", exits.CONFLICT)
    if response.status != 201:
        raise AnnotationError(
            f"creating the tag {name!r} answered {response.status}", exits.SERVER_ERROR
        )
    location = response.header("Content-Location") or ""
    if not location:
        raise _sent(
            f"the tag {name!r} was accepted but the server did not say which tag it made"
        )
    return {"id": _identifier(_last_segment(location), what="tag"), "name": name}


def execute(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Perform one frozen annotation step."""
    validate_step(step)
    details = step.details
    if step.action == "tag.create":
        return {
            "action": step.action,
            "href": step.href,
            "tag": _created_tag(profile, session=session, step=step),
        }

    if step.action in {"tag.assign", "tag.unassign"}:
        assign = step.action == "tag.assign"
        method = "PUT" if assign else "DELETE"
        accepted = {201, 204} if assign else {204, 404}
        try:
            response = session.request(method, _relation_href(profile, details))
        except SessionError as exc:
            raise _sent(
                f"the request to {'assign' if assign else 'remove'} the tag "
                f"{details['tag_name']!r} was sent but its answer was lost; reconcile "
                "decides whether it applied"
            ) from exc
        if response.status not in accepted:
            raise AnnotationError(
                f"the tag {'assignment' if assign else 'removal'} answered "
                f"{response.status}",
                exits.CONFLICT if response.status == 409 else exits.SERVER_ERROR,
            )
        try:
            carried = {
                item.id for item in file_tags(profile, session=session, href=step.href)
            }
        except (AnnotationError, SessionError) as exc:
            raise _sent(
                f"the tag {'assignment' if assign else 'removal'} was accepted but "
                f"{step.href} could not be read back"
            ) from exc
        if (details["tag_id"] in carried) != assign:
            raise _sent(
                f"the tag {'assignment' if assign else 'removal'} was accepted but "
                f"{step.href} does not read back with it "
                f"{'present' if assign else 'absent'}"
            )
        return {
            "action": step.action,
            "href": step.href,
            "tag": {"id": details["tag_id"], "name": details["tag_name"]},
            "verified": True,
        }

    collection = _canonical(profile, f"{COMMENTS_ROOT}{details['file_id']}/")
    if step.action == "comment.create":
        body = json.dumps(
            {"actorType": "users", "verb": "comment", "message": details["message"]}
        ).encode("utf-8")
        try:
            response = session.request(
                "POST",
                collection,
                headers={"Content-Type": "application/json"},
                data=body,
            )
        except SessionError as exc:
            raise _sent(
                "the comment was sent but its answer was lost; reconcile decides "
                "whether it was posted"
            ) from exc
        if response.status != 201:
            raise AnnotationError(
                f"posting the comment answered {response.status}", exits.SERVER_ERROR
            )
        location = response.header("Content-Location") or ""
        if not location:
            raise _sent(
                "the comment was accepted but the server did not say which comment it made"
            )
        posted = _identifier(_last_segment(location), what="comment")
        try:
            matched = [
                item
                for item in _comments(
                    profile, session=session, file_identifier=details["file_id"]
                )
                if item.id == posted
            ]
        except (AnnotationError, SessionError) as exc:
            raise _sent(
                "the comment was accepted but it could not be read back"
            ) from exc
        if not matched:
            raise _sent("the comment was accepted but it does not read back")
        return {
            "action": step.action,
            "href": step.href,
            "comment": matched[0].as_dict(),
            "verified": True,
        }

    comment_id = details["comment_id"]
    try:
        response = session.request("DELETE", f"{collection}{comment_id}")
    except SessionError as exc:
        raise _sent(
            f"the removal of comment {comment_id} was sent but its answer was lost; "
            "reconcile decides whether it applied"
        ) from exc
    if response.status not in {204, 404}:
        raise AnnotationError(
            f"removing the comment answered {response.status}", exits.SERVER_ERROR
        )
    try:
        remaining = {
            item.id
            for item in _comments(
                profile, session=session, file_identifier=details["file_id"]
            )
        }
    except (AnnotationError, SessionError) as exc:
        raise _sent(
            f"the removal of comment {comment_id} was accepted but the file's comments "
            "could not be read back"
        ) from exc
    if comment_id in remaining:
        raise _sent(
            f"the removal of comment {comment_id} was accepted but the comment is still there"
        )
    return {
        "action": step.action,
        "href": step.href,
        "comment_id": comment_id,
        "verified": "removed",
    }


def reconcile(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Read the server back to settle an annotation step whose outcome is unknown."""
    validate_step(step)
    details = step.details
    if step.action == "tag.create":
        matched = [
            item
            for item in list_tags(profile, session=session)
            if item.name == details["name"]
        ]
        if len(matched) == 1:
            return {
                "action": step.action,
                "state": "verified",
                "tag": matched[0].as_dict(),
            }
        return {"action": step.action, "state": "pending" if not matched else "uncertain"}

    if step.action in {"tag.assign", "tag.unassign"}:
        carried = {item.id for item in file_tags(profile, session=session, href=step.href)}
        present = details["tag_id"] in carried
        state = "verified" if present == (step.action == "tag.assign") else "pending"
        return {"action": step.action, "state": state, "tag_id": details["tag_id"]}

    comments = _comments(profile, session=session, file_identifier=details["file_id"])
    if step.action == "comment.delete":
        if any(item.id == details["comment_id"] for item in comments):
            return {"action": step.action, "state": "pending"}
        return {"action": step.action, "state": "verified", "comment_id": details["comment_id"]}

    account = _account_name(profile, session)
    planned_at = str(details.get("planned_at") or "")
    matched = [
        item
        for item in comments
        if item.message == details["message"]
        and item.actor_id == account
        and item.created >= planned_at
    ]
    if len(matched) == 1:
        return {"action": step.action, "state": "verified", "comment": matched[0].as_dict()}
    return {"action": step.action, "state": "pending" if not matched else "uncertain"}
