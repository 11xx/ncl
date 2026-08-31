"""File shares over the OCS sharing API.

A share is the one mutation in this tool that changes who *else* can reach a
resource. A public link in particular hands the file to anyone holding a URL,
with no account and no further check, and the link is not recallable once it
has been read — so creating one is treated as an outward-facing act and passes
the same frozen plan boundary as a write, with the reach it grants named in the
preview rather than discovered afterwards.

Shares are addressed the way everything else in this tool is: by DAV href,
checked against the profile's files allowlist. The OCS API speaks account-
relative paths instead, so the two are converted here, once, and a share of a
path this profile may not reach is refused before any request is made.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse
from dataclasses import dataclass
from typing import Any

from . import exits, ocs, plans, profiles
from .session import Session

_API = ("apps", "files_sharing", "api", "v1", "shares")

#: The DAV collection every account's own files hang under.
_FILES_ROOT = "/remote.php/dav/files/"

#: OCS share types, named. Only the three this tool creates are writable; the
#: rest are read so that a listing tells the truth about who already has
#: access, which is the question a listing exists to answer.
SHARE_TYPES = {
    0: "user",
    1: "group",
    3: "public_link",
    4: "email",
    6: "federated",
    7: "circle",
    10: "talk_conversation",
    11: "deck",
    12: "sciencemesh",
}
CREATABLE = {"user": 0, "group": 1, "public_link": 3}

#: The WebDAV permission bits OCS packs into one integer.
_PERMISSION_BITS = ((1, "read"), (2, "update"), (4, "create"), (8, "delete"), (16, "share"))
_PERMISSION_MASK = sum(bit for bit, _name in _PERMISSION_BITS)

#: What each named permission set grants. `read` is the only safe default: a
#: share created with more reach than asked for is not discoverable by reading
#: the command that made it.
PERMISSION_SETS = {"read": 1, "write": 1 | 2 | 4 | 8, "all": 1 | 2 | 4 | 8 | 16}


class ShareError(RuntimeError):
    """A share could not be read, planned, or applied."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Share:
    """One share, and the reach it grants."""

    share_id: str
    share_type: str
    href: str
    path: str
    permissions: tuple[str, ...]
    recipient: str
    owner: str
    url: str
    expires: str
    note: str
    label: str
    password_protected: bool
    writable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "share_id": self.share_id,
            "share_type": self.share_type,
            "href": self.href,
            "path": self.path,
            "permissions": list(self.permissions),
            "recipient": self.recipient,
            "owner": self.owner,
            "url": self.url,
            "expires": self.expires,
            "note": self.note,
            "label": self.label,
            "password_protected": self.password_protected,
            "writable": self.writable,
        }

    @property
    def public(self) -> bool:
        return self.share_type in {"public_link", "email"}


def _account_root(account_name: str) -> str:
    return f"{_FILES_ROOT}{urllib.parse.quote(account_name, safe='')}/"


def dav_href(profile: Any, *, account_name: str, path: str) -> str:
    """Resolve an account-relative OCS path to the DAV href that names it."""
    relative = str(path or "").strip()
    if not relative.startswith("/"):
        relative = "/" + relative
    quoted = urllib.parse.quote(relative.lstrip("/"), safe="/")
    return f"{profile.origin.rstrip('/')}{_account_root(account_name)}{quoted}"


def ocs_path(profile: Any, *, account_name: str, href: str) -> str:
    """Reduce an allowlisted DAV href to the account-relative path OCS wants.

    The allowlist check happens here rather than at the API boundary because
    OCS takes a path with no origin in it: by the time a request is built there
    is nothing left to check it against.
    """
    from .session import SessionError, absolute_url

    try:
        resolved = absolute_url(profile, href)
    except SessionError as exc:
        raise ShareError(exc.message, exc.code) from exc
    if not profiles.in_scope(resolved, profile.files_roots):
        raise ShareError(
            f"{resolved} is outside this profile's files allowlist", exits.SCOPE_DENIED
        )
    root = _account_root(account_name)
    path = urllib.parse.urlsplit(resolved).path
    if not path.startswith(root):
        raise ShareError(
            f"{resolved} is not under this account's own files, so it cannot be shared "
            "from here",
            exits.SCOPE_DENIED,
        )
    relative = urllib.parse.unquote(path[len(root) :])
    return "/" + relative.strip("/")


def _permissions(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ShareError("a share reported permissions this tool cannot read")
    unknown = value & ~_PERMISSION_MASK
    if unknown:
        raise ShareError(
            f"a share reported unmodelled permission bits {unknown}; its reach is unknown"
        )
    return tuple(name for bit, name in _PERMISSION_BITS if value & bit)


def _text(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _share(profile: Any, record: Any, *, account_name: str) -> Share:
    entry = ocs.require_object(record, label="share")
    share_id = _text(entry, "id")
    if not share_id:
        raise ShareError("a share carried no id")
    raw_type = entry.get("share_type")
    if isinstance(raw_type, str) and raw_type.strip().isdigit():
        raw_type = int(raw_type)
    if not isinstance(raw_type, int) or isinstance(raw_type, bool):
        raise ShareError(f"share {share_id} carried no numeric share type")
    kind = SHARE_TYPES.get(raw_type, f"unsupported:{raw_type}")
    path = _text(entry, "path")
    if not path:
        raise ShareError(f"share {share_id} named no path")
    return Share(
        share_id=share_id,
        share_type=kind,
        href=dav_href(profile, account_name=account_name, path=path),
        path=path,
        permissions=_permissions(entry.get("permissions")),
        recipient=_text(entry, "share_with"),
        owner=_text(entry, "uid_owner"),
        url=_text(entry, "url"),
        expires=_text(entry, "expiration"),
        note=_text(entry, "note"),
        label=_text(entry, "label"),
        password_protected=bool(_text(entry, "password")),
        writable=kind in CREATABLE,
    )


def list_shares(
    profile: Any,
    *,
    session: Session,
    account_name: str,
    href: str | None = None,
    subfiles: bool = False,
) -> list[Share]:
    """List shares, over the whole account or over one allowlisted resource.

    Without an href this reports every share the account has made, including
    ones over paths outside the allowlist. That is deliberate: the allowlist
    bounds what this tool may *reach*, and a listing that hid an existing
    public link because its path was unconfigured would answer "who can see my
    files" with a reassuring lie.
    """
    query: dict[str, str] = {}
    if href is not None:
        query["path"] = ocs_path(profile, account_name=account_name, href=href)
        query["reshares"] = "true"
        if subfiles:
            query["subfiles"] = "true"
    elif subfiles:
        raise ShareError("--subfiles describes one collection, so it needs a target", exits.USAGE)

    data = ocs.request(
        profile,
        session=session,
        method="GET",
        url=ocs.path(*_API, query=query),
    )
    return [
        _share(profile, record, account_name=account_name)
        for record in ocs.require_list(data, label="shares")
    ]


def fetch(profile: Any, *, session: Session, account_name: str, share_id: str) -> Share:
    """Read one share by the id a listing reported."""
    data = ocs.request(
        profile,
        session=session,
        method="GET",
        url=ocs.path(*_API, _share_id(share_id)),
    )
    records = data if isinstance(data, list) else [data]
    if len(records) != 1:
        raise ShareError(f"share {share_id} did not resolve to one share")
    return _share(profile, records[0], account_name=account_name)


def _share_id(value: str) -> str:
    text = str(value or "").strip()
    if not text or not text.isascii() or not text.isdigit():
        raise ShareError(f"{value!r} is not a share id", exits.USAGE)
    return text


def _expiry(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = dt.date.fromisoformat(value.strip())
    except (AttributeError, ValueError) as exc:
        raise ShareError("--expires is not an ISO 8601 date", exits.USAGE) from exc
    if parsed <= dt.datetime.now(dt.UTC).date():
        raise ShareError("--expires is not in the future", exits.USAGE)
    return parsed.isoformat()


def plan_create(
    profile: Any,
    *,
    session: Session,
    account_name: str,
    href: str,
    share_type: str,
    recipient: str | None = None,
    permissions: str = "read",
    password: str | None = None,
    expires: str | None = None,
    note: str | None = None,
    label: str | None = None,
) -> plans.Plan:
    """Freeze the creation of one share, naming the reach it would grant.

    Nothing is sent here. The preview carries who would gain access and at what
    permission, because a share is not observable from the resource it exposes:
    reading the file afterwards looks exactly the same whether or not the world
    can also read it.
    """
    if share_type not in CREATABLE:
        raise ShareError(
            f"{share_type} shares are not created by this tool", exits.UNSUPPORTED_STRUCTURE
        )
    if permissions not in PERMISSION_SETS:
        raise ShareError(f"{permissions!r} is not a permission set", exits.USAGE)
    if share_type == "public_link":
        if recipient is not None:
            raise ShareError("a public link has no recipient to name", exits.USAGE)
    elif not (recipient or "").strip():
        raise ShareError(f"a {share_type} share names who it is for", exits.USAGE)

    path = ocs_path(profile, account_name=account_name, href=href)
    existing = list_shares(profile, session=session, account_name=account_name, href=href)
    reach = (
        "anyone holding the link"
        if share_type == "public_link"
        else f"{share_type} {str(recipient).strip()}"
    )
    return plans.write_bundle(
        profile=profile,
        summary=f"share {path} with {reach}",
        steps=(
            plans.freeze_step(
                action="share.create",
                href=dav_href(profile, account_name=account_name, path=path),
                etag="",
                summary=f"share {path} with {reach}",
                details={
                    "path": path,
                    "share_type": share_type,
                    "recipient": str(recipient).strip() if recipient else "",
                    "permissions": permissions,
                    "grants": list(_permissions(PERMISSION_SETS[permissions])),
                    "reach": reach,
                    "expires": _expiry(expires) or "",
                    "note": (note or "").strip(),
                    "label": (label or "").strip(),
                    "password_protected": password is not None,
                    "secret_payload": True,
                    "existing_shares": [item.share_id for item in existing],
                },
                payload=(password or "").encode("utf-8"),
            ),
        ),
    )


def plan_delete(
    profile: Any, *, session: Session, account_name: str, share_id: str
) -> plans.Plan:
    """Freeze the removal of one share, after reading what it currently grants."""
    share = fetch(profile, session=session, account_name=account_name, share_id=share_id)
    if not profiles.in_scope(share.href, profile.files_roots):
        raise ShareError(
            f"share {share.share_id} is outside this profile's files allowlist, so it can "
            "be seen but not revoked",
            exits.SCOPE_DENIED,
        )
    return plans.write_bundle(
        profile=profile,
        summary=f"revoke the {share.share_type} share of {share.path}",
        steps=(
            plans.freeze_step(
                action="share.delete",
                href=share.href,
                etag="",
                summary=f"revoke the {share.share_type} share of {share.path}",
                details={
                    "share_id": share.share_id,
                    "share_type": share.share_type,
                    "path": share.path,
                    "recipient": share.recipient,
                    "grants": list(share.permissions),
                },
            ),
        ),
    )


def validate_step(step: plans.Step) -> None:
    """Reject a frozen share step before the bundle makes its first request."""
    details = step.details
    if step.action == "share.create":
        kind = details.get("share_type")
        if kind not in CREATABLE:
            raise plans.PlanError(
                "the plan names a share type this tool does not create", exits.PLAN_STALE
            )
        if details.get("permissions") not in PERMISSION_SETS:
            raise plans.PlanError("the plan names no permission set", exits.PLAN_STALE)
        if not str(details.get("path") or "").startswith("/"):
            raise plans.PlanError("the plan names no shared path", exits.PLAN_STALE)
        recipient = str(details.get("recipient") or "")
        if (kind == "public_link") != (not recipient):
            raise plans.PlanError(
                "the plan's share type and recipient disagree", exits.PLAN_STALE
            )
        return
    if step.action == "share.delete":
        if not str(details.get("share_id") or "").isdigit():
            raise plans.PlanError("the plan names no share to revoke", exits.PLAN_STALE)
        return
    raise plans.PlanError(f"unsupported share action {step.action}", exits.PLAN_STALE)


def _created(profile: Any, data: Any, *, account_name: str) -> Share:
    records = data if isinstance(data, list) else [data]
    if len(records) != 1:
        raise ShareError(
            "the server did not report exactly one created share", exits.OUTCOME_UNCERTAIN
        )
    return _share(profile, records[0], account_name=account_name)


def execute(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Perform one frozen share step."""
    details = step.details
    if step.action == "share.create":
        password = plans.payload_bytes(step).decode("utf-8") or None
        form = {
            "path": details["path"],
            "shareType": CREATABLE[details["share_type"]],
            "permissions": PERMISSION_SETS[details["permissions"]],
        }
        if details.get("recipient"):
            form["shareWith"] = details["recipient"]
        if password is not None:
            form["password"] = password
        for key, name in (("expires", "expireDate"), ("note", "note"), ("label", "label")):
            if details.get(key):
                form[name] = details[key]
        try:
            data = ocs.request(
                profile,
                session=session,
                method="POST",
                url=ocs.path(*_API),
                form=form,
            )
        except ocs.OcsError as exc:
            raise ShareError(exc.message, exc.code) from exc
        share = _created(profile, data, account_name=_account_of(details["path"], step.href))
        return {
            "action": step.action,
            "href": step.href,
            "share": share.as_dict(),
            "granted_beyond_plan": _widened(details, share),
        }

    share_id = details["share_id"]
    try:
        ocs.request(
            profile,
            session=session,
            method="DELETE",
            url=ocs.path(*_API, share_id),
        )
    except ocs.OcsError as exc:
        if exc.code == exits.TARGET_NOT_FOUND:
            # A share that is already gone is the outcome this step wanted, and
            # reporting it as a failure would make a resumed plan unfinishable.
            return {"action": step.action, "href": step.href, "revoked": share_id,
                    "already_absent": True}
        raise ShareError(exc.message, exc.code) from exc
    return {"action": step.action, "href": step.href, "revoked": share_id}


def _widened(details: dict[str, Any], share: Share) -> list[str]:
    """Name any permission the server granted that the plan did not promise.

    A server is free to grant more than it was asked for — Nextcloud adds the
    share bit to every public link — and the preview a person approved said
    otherwise. Reporting the difference is what keeps the approval honest;
    refusing it would refuse every public link this server makes.
    """
    planned = set(_permissions(PERMISSION_SETS[details["permissions"]]))
    return sorted(set(share.permissions) - planned)


def _account_of(path: str, href: str) -> str:
    """Recover the account name a frozen href was built against."""
    marker = _FILES_ROOT
    split = urllib.parse.urlsplit(href).path
    if not split.startswith(marker):
        raise ShareError("the frozen share href does not name an account's files")
    return urllib.parse.unquote(split[len(marker) :].split("/", 1)[0])


def reconcile(profile: Any, *, session: Session, step: plans.Step) -> dict[str, Any]:
    """Read the server back to settle a share step whose outcome is unknown."""
    account_name = _account_of(step.details.get("path", ""), step.href)
    if step.action == "share.delete":
        share_id = step.details["share_id"]
        try:
            fetch(profile, session=session, account_name=account_name, share_id=share_id)
        except ShareError as exc:
            if exc.code == exits.TARGET_NOT_FOUND:
                return {"action": step.action, "state": "verified", "revoked": share_id}
            raise
        return {"action": step.action, "state": "pending", "share_id": share_id}

    existing = list_shares(
        profile, session=session, account_name=account_name, href=step.href
    )
    before = set(step.details.get("existing_shares") or ())
    created = [item for item in existing if item.share_id not in before]
    if len(created) == 1:
        return {"action": step.action, "state": "verified", "share": created[0].as_dict()}
    if not created:
        return {"action": step.action, "state": "pending"}
    raise ShareError(
        "more than one new share exists on this path, so which one this plan created "
        "cannot be told apart; revoke by id after listing",
        exits.OUTCOME_UNCERTAIN,
    )
