"""Writing a file that does not fit in memory, and reading part of one.

A single `PUT` requires the whole body at once — in this process, and in the
plan that froze it. That is fine for a note and wrong for a video: a plan is a
JSON file holding its payload base64-encoded, so freezing a gigabyte costs a
gigabyte and a third on disk before anything is sent.

Nextcloud's chunked upload is what makes the large case possible. An upload
directory is created, numbered parts are `PUT` into it, and a `MOVE` of the
directory's `.file` pseudo-resource assembles them at the destination in one
server-side operation, consuming the directory. Parts are named zero-padded so
that a server ordering them lexicographically and one ordering them numerically
assemble the same bytes.

What a large plan freezes is therefore not the content but the *identity* of
the content: the source path, its size, and its SHA-256. Applying re-reads the
file, hashes it while sending, and refuses if what it read is not what was
promised. A plan that quietly uploaded whatever the path happens to hold now
would not be a frozen plan.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import exits
from .session import Session

#: Above this, a write streams from its source rather than freezing its bytes.
#: The threshold exists so an ordinary small write keeps its exact-payload
#: guarantee, where the source file could change under a plan and nobody would
#: be able to tell.
INLINE_LIMIT = 8 * 1024 * 1024

#: One part. Large enough that a big upload is not thousands of round trips,
#: small enough that a failure retries little and memory holds one part.
CHUNK_SIZE = 10 * 1024 * 1024

#: Where Nextcloud assembles a chunked upload.
UPLOAD_ROOT = "/remote.php/dav/uploads/"

#: The pseudo-resource whose MOVE assembles the parts.
ASSEMBLY = ".file"


class UploadError(RuntimeError):
    """A streamed transfer could not be completed."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


def measure(path: str | Path) -> tuple[int, str]:
    """Return a local file's size and SHA-256 without holding it in memory."""
    source = Path(path).expanduser()
    digest = hashlib.sha256()
    total = 0
    try:
        with source.open("rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                digest.update(block)
    except OSError as exc:
        raise UploadError(
            f"the source file could not be read: {exc.strerror}", exits.USAGE
        ) from exc
    return total, digest.hexdigest()


def _parts(path: Path, *, size: int, digest: str):
    """Yield each part while hashing, so a changed source is caught mid-send."""
    running = hashlib.sha256()
    total = 0
    index = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(CHUNK_SIZE)
            if not block:
                break
            index += 1
            total += len(block)
            running.update(block)
            if total > size:
                raise UploadError(
                    "the source file grew while it was being sent; nothing was assembled",
                    exits.CONFLICT,
                )
            yield index, block
    if total != size or running.hexdigest() != digest:
        raise UploadError(
            "the source file changed after the plan was frozen; nothing was assembled",
            exits.CONFLICT,
        )


def upload_directory(account_name: str, token: str) -> str:
    return f"{UPLOAD_ROOT}{quote(account_name, safe='')}/{quote(token, safe='')}/"


def stream_upload(
    profile: Any,
    *,
    session: Session,
    account_name: str,
    source: str | Path,
    destination: str,
    size: int,
    digest: str,
    token: str,
    overwrite: bool,
    expected_etag: str,
) -> str:
    """Send one file in parts and assemble it, returning the resulting ETag.

    Every part is present before the destination can change. A creation remains
    absent until the assembling `MOVE`. A replacement conditionally removes only
    the frozen destination revision, then assembles with overwrite disabled; an
    unconfirmed result after that deletion is uncertainty. An unfinished upload
    directory is removed without replacing the error that stopped the transfer.
    """
    path = Path(source).expanduser()
    directory = upload_directory(account_name, token)
    if overwrite and not expected_etag:
        raise UploadError("a streamed replacement needs the destination ETag", exits.CONFLICT)
    if not overwrite and expected_etag:
        raise UploadError("a streamed creation cannot carry a destination ETag", exits.CONFLICT)
    created = session.request("MKCOL", directory)
    if created.status == 405:
        raise UploadError(
            "an upload directory with this identity already exists", exits.CONFLICT
        )
    if created.status != 201:
        raise UploadError(
            f"the upload directory could not be created ({created.status}); this server "
            "may not support chunked upload",
            exits.UNSUPPORTED_STRUCTURE,
        )

    replacement_started = False
    assembly_started = False
    try:
        for index, block in _parts(path, size=size, digest=digest):
            # Five digits orders a hundred gigabytes of parts lexicographically
            # and numerically alike, which is what makes the assembled bytes
            # independent of how the server sorts them.
            response = session.request(
                "PUT",
                f"{directory}{index:05d}",
                headers={"Content-Type": "application/octet-stream"},
                data=block,
            )
            if response.status not in {200, 201, 204}:
                raise UploadError(
                    f"part {index} was refused with {response.status}; nothing was assembled",
                    exits.SERVER_ERROR,
                )

        if overwrite:
            # Nextcloud's upload assembly endpoint ignores a tagged WebDAV `If`
            # condition on the destination. Remove only the revision the plan
            # observed, then use Overwrite: F so a new occupant can never be
            # replaced in the interval before assembly.
            replacement_started = True
            removed = session.request(
                "DELETE",
                destination,
                headers={"If-Match": expected_etag},
                max_redirects=0,
            )
            if removed.status in {404, 412}:
                replacement_started = False
                raise UploadError(
                    f"{destination} changed since the plan was made; nothing was assembled",
                    exits.CONFLICT,
                )
            if removed.status not in {200, 204}:
                replacement_started = False
                raise UploadError(
                    f"the planned destination could not be removed ({removed.status})",
                    exits.SERVER_ERROR,
                )

        headers = {"Destination": destination, "Overwrite": "F"}
        assembly_started = True
        assembled = session.request(
            "MOVE", f"{directory}{ASSEMBLY}", headers=headers, max_redirects=0
        )
        if assembled.status == 412:
            assembly_started = False
            raise UploadError(
                f"{destination} gained a resource before assembly; nothing was overwritten",
                exits.OUTCOME_UNCERTAIN if replacement_started else exits.CONFLICT,
            )
        if assembled.status not in {201, 204}:
            assembly_started = False
            raise UploadError(
                f"the upload could not be assembled ({assembled.status})",
                exits.OUTCOME_UNCERTAIN if replacement_started else exits.SERVER_ERROR,
            )
        return (assembled.header("OC-ETag") or assembled.header("ETag") or "").strip()
    except Exception as exc:
        # The assembling MOVE consumes the directory, so this only ever runs
        # for an upload that did not finish; failing to clean up must not
        # replace the error that got here.
        with suppress(Exception):
            session.request("DELETE", directory)
        if (replacement_started or assembly_started) and getattr(
            exc, "code", None
        ) != exits.OUTCOME_UNCERTAIN:
            raise UploadError(
                "the destination may have changed, but the streamed write was not confirmed",
                exits.OUTCOME_UNCERTAIN,
            ) from exc
        raise


def read_range(
    profile: Any,
    *,
    session: Session,
    href: str,
    offset: int,
    length: int | None,
) -> tuple[bytes, int | None]:
    """Read part of a file, returning the bytes and the resource's full size.

    A server may ignore `Range` and answer `200` with the whole entity. That is
    legal, and silently returning it as though it were the requested window
    would misplace every byte the caller then indexes, so it is a refusal.
    """
    if offset < 0:
        raise UploadError("--offset is not negative", exits.USAGE)
    if length is not None and length < 1:
        raise UploadError("--length is at least one byte", exits.USAGE)
    end = "" if length is None else str(offset + length - 1)
    response = session.request(
        "GET", href, headers={"Range": f"bytes={offset}-{end}"}, max_redirects=0
    )
    if response.status == 404:
        raise UploadError(f"no file exists at {href}", exits.TARGET_NOT_FOUND)
    if response.status == 416:
        raise UploadError(
            "the requested range lies outside the file", exits.USAGE
        )
    if response.status == 200:
        raise UploadError(
            "the server ignored the requested range and answered with the whole file",
            exits.UNSUPPORTED_STRUCTURE,
        )
    if response.status != 206:
        raise UploadError(
            f"the ranged read answered {response.status}", exits.MALFORMED_RESPONSE
        )
    returned_start, returned_end, total = _content_range(response.header("Content-Range"))
    if returned_start != offset:
        raise UploadError(
            f"the server returned bytes starting at {returned_start}, not {offset}",
            exits.MALFORMED_RESPONSE,
        )
    returned_length = returned_end - returned_start + 1
    if len(response.body) != returned_length:
        raise UploadError(
            "the response body length did not match its Content-Range",
            exits.MALFORMED_RESPONSE,
        )
    if (
        length is not None
        and returned_length != length
        and (total is None or returned_end != total - 1 or returned_length > length)
    ):
        raise UploadError(
            f"the server returned {returned_length} bytes for a {length}-byte range",
            exits.MALFORMED_RESPONSE,
        )
    return response.body, total


_CONTENT_RANGE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)")


def _content_range(value: str | None) -> tuple[int, int, int | None]:
    """Read and validate the interval described by one 206 response."""
    candidate = (value or "").strip()
    matched = _CONTENT_RANGE.fullmatch(candidate)
    if matched is None or any(len(part) > 20 for part in matched.groups()[:2]):
        raise UploadError("the server returned a malformed Content-Range")
    start = int(matched.group(1))
    end = int(matched.group(2))
    total = None if matched.group(3) == "*" else int(matched.group(3))
    if end < start or (total is not None and (len(matched.group(3)) > 20 or end >= total)):
        raise UploadError("the server returned a malformed Content-Range")
    return start, end, total


def _total_size(value: str | None) -> int | None:
    """Read the entity length out of a `Content-Range`, when it states one."""
    return _content_range(value)[2]


def append_local(path: str | Path, content: bytes, *, offset: int) -> Path:
    """Write a window's bytes at their own offset in a local file.

    A ranged read is normally one of several, so its output is placed where it
    belongs rather than at the start of a fresh file.
    """
    target = Path(path).expanduser()
    try:
        with target.open("r+b" if target.exists() else "wb") as stream:
            stream.seek(offset)
            stream.write(content)
    except OSError as exc:
        raise UploadError(
            f"the local output could not be written: {exc.strerror}",
            exits.PRECONDITION_FAILED,
        ) from exc
    return target


def remote_digest(
    profile: Any, *, session: Session, href: str, size: int
) -> str:
    """Hash a stored file by reading it in windows rather than whole.

    Reconciling a streamed write has to answer whether the bytes that landed
    are the bytes that were promised, and size alone does not answer it — two
    different files of one length are the ordinary case, not a contrived one.
    Reading in windows pays the transfer but never the memory.
    """
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        window = min(CHUNK_SIZE, size - offset)
        block, _ = read_range(
            profile, session=session, href=href, offset=offset, length=window
        )
        digest.update(block)
        offset += len(block)
    if offset != size:
        raise UploadError(
            "the stored file did not yield the length it reported",
            exits.OUTCOME_UNCERTAIN,
        )
    return digest.hexdigest()
