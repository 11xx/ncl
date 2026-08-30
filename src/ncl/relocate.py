"""The shape every WebDAV relocation and collection creation shares.

`MOVE` and `MKCOL` mean the same thing to a calendar collection and to a files
collection: only the collection type differs. Deciding the flag that names a
destination, whether a collision refuses, and how the allowlist is checked once
— here — is what keeps the two modules from growing two spellings of one
operation.

The decisions this module encodes:

- **A destination is never overwritten.** `Overwrite: F` is sent on every
  relocation and is not configurable. A collision is a conflict the caller
  resolves, because the alternative silently destroys whatever sat there.
- **Both endpoints pass the allowlist.** Checking only the source would let a
  correct request move a resource out of the configured scope, which is the
  same widening the allowlist exists to refuse.
- **A relocation moves the resource, not its content.** The destination keeps
  the resource's identity, so it is verified by reading the destination back
  and confirming the source is gone — not by rewriting what was there.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from . import exits

#: Sent on every MOVE. A destination that already holds something is a
#: conflict, never a target to replace.
NO_OVERWRITE = {"Overwrite": "F"}


def headers(destination: str, *, etag: str = "") -> dict[str, str]:
    """Return the headers for one non-destructive MOVE.

    `If-Match` carries the source ETag observed while planning, which RFC 4918
    applies to the source of a MOVE. A server that ignores it leaves the move
    unconditional on the source rather than refusing wrongly, so the readback
    remains what establishes the outcome.
    """
    built = {"Destination": destination, **NO_OVERWRITE}
    if etag:
        built["If-Match"] = etag
    return built


def child_href(collection_href: str, resource_href: str) -> str:
    """Return where a resource lands under a destination collection.

    The final path segment travels with the resource. Keeping the name is what
    makes a move a move: minting a new one at the destination would leave every
    external reference to the old href pointing at nothing, for no gain.
    """
    segment = urlsplit(resource_href).path.rstrip("/").rsplit("/", 1)[-1]
    return f"{collection_href.rstrip('/')}/{segment}"


def classify(
    status: int,
    *,
    fail: Callable[[str, int], Exception],
    source: str,
    destination: str,
) -> None:
    """Raise for every MOVE status that is not an unambiguous relocation.

    A 204 means the destination was overwritten, which `Overwrite: F` was sent
    to prevent. A server reporting it has done something the plan did not
    describe, so it is uncertain rather than success.
    """
    if status == 201:
        return
    if status == 204:
        raise fail(
            f"the server reported that the move to {destination} replaced an existing "
            "resource, which Overwrite: F was sent to refuse",
            exits.OUTCOME_UNCERTAIN,
        )
    if status == 412:
        raise fail(
            f"{destination} already holds a resource, or {source} changed since the "
            "plan was made; nothing was moved",
            exits.CONFLICT,
        )
    if status == 403:
        # RFC 4918 gives 412 to an Overwrite: F collision and 403 to a move the
        # server will not perform at all. Reporting the second as a conflict
        # would tell the caller to clear a destination that is not the problem.
        raise fail(
            f"the server forbade moving {source} to {destination}; nothing was moved",
            exits.SERVER_ERROR,
        )
    if status == 404:
        raise fail(f"no resource exists at {source}", exits.TARGET_NOT_FOUND)
    if status == 409:
        raise fail(
            f"the collection holding {destination} does not exist; create it first",
            exits.TARGET_NOT_FOUND,
        )
    raise fail(
        f"the server refused the move to {destination} with status {status}",
        exits.SERVER_ERROR,
    )


def classify_creation(
    status: int,
    *,
    fail: Callable[[str, int], Exception],
    href: str,
) -> None:
    """Raise for every collection-creation status that is not a creation."""
    if status == 201:
        return
    if status == 405:
        raise fail(f"{href} already exists; nothing was created", exits.CONFLICT)
    if status == 409:
        raise fail(
            f"the collection holding {href} does not exist; create it first",
            exits.TARGET_NOT_FOUND,
        )
    raise fail(
        f"the server refused to create {href} with status {status}", exits.SERVER_ERROR
    )
