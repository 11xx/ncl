"""Mutations are planned, then applied.

A mutation command never changes anything. It resolves its target, freezes
exactly what it would do, and writes a plan. `ncl apply <id>` executes that
frozen plan under a lock and consumes it.

The reason is not ceremony. Creating an event mints a UID, an href, and a
DTSTAMP, and a relative time like "tomorrow" resolves against the clock — so a
command that re-derived its own effect on a second invocation would not
reproduce the first. Freezing what was previewed is what makes the preview
mean anything.

What this does *not* provide is authorization. Nothing here can tell whether
the plan id came from the person who read the preview or from the same agent
that produced it. Where a human's approval is genuinely required, the workflow
has to stop after planning and wait to be told to continue.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets as token_source
import time
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import exits

#: A plan describes a resource that may change under it, so it expires.
DEFAULT_TTL_SECONDS = 900


class PlanError(RuntimeError):
    """A plan could not be written, read, or applied."""

    def __init__(self, message: str, code: int = exits.ERROR) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Plan:
    plan_id: str
    profile: str
    action: str
    calendar_href: str
    href: str
    uid: str
    etag: str
    summary: str
    start: str
    end: str
    payload: str
    created_at: float
    expires_at: float

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # The serialized body is the largest field and is not useful to a
        # caller deciding whether to apply; the summary and times are.
        data["payload_bytes"] = len(self.payload.encode("utf-8"))
        del data["payload"]
        return data


def _directory() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = Path(base) / "ncl" / "plans"
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise PlanError("the plan directory is not usable", exits.PRECONDITION_FAILED) from exc
    return path


def _path(plan_id: str) -> Path:
    if not plan_id or "/" in plan_id or plan_id.startswith("."):
        raise PlanError(f"{plan_id!r} is not a plan id", exits.USAGE)
    return _directory() / f"{plan_id}.json"


def write(
    *,
    profile: str,
    action: str,
    calendar_href: str,
    href: str,
    uid: str,
    etag: str,
    summary: str,
    start: str,
    end: str,
    payload: str,
    ttl: float = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> Plan:
    """Freeze a mutation. Nothing is sent to the server by this call."""
    created = time.time() if now is None else now
    plan = Plan(
        plan_id=token_source.token_hex(8),
        profile=profile,
        action=action,
        calendar_href=calendar_href,
        href=href,
        uid=uid,
        etag=etag,
        summary=summary,
        start=start,
        end=end,
        payload=payload,
        created_at=created,
        expires_at=created + ttl,
    )
    path = _path(plan.plan_id)
    temporary = path.with_suffix(".tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(asdict(plan), handle)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        raise PlanError("the plan could not be stored", exits.PRECONDITION_FAILED) from exc
    return plan


def read(plan_id: str) -> Plan:
    path = _path(plan_id)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise PlanError(f"no plan {plan_id} exists", exits.TARGET_NOT_FOUND) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"plan {plan_id} is unreadable", exits.MALFORMED_RESPONSE) from exc
    try:
        return Plan(**data)
    except TypeError as exc:
        raise PlanError(f"plan {plan_id} has an unexpected shape", exits.PLAN_STALE) from exc


def listing() -> list[Plan]:
    plans = []
    for path in sorted(_directory().glob("*.json")):
        try:
            plans.append(read(path.stem))
        except PlanError:
            continue
    return plans


def consume(plan_id: str) -> None:
    """Delete a plan so it cannot be applied twice."""
    try:
        _path(plan_id).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PlanError(f"plan {plan_id} could not be consumed", exits.ERROR) from exc


@contextmanager
def claim(plan_id: str):
    """Hold a plan exclusively while it is applied.

    Two callers racing the same plan id must not both send the mutation. The
    lock is a separate file held with flock, so the kernel releases it if the
    applying process dies rather than leaving a plan permanently unusable.
    """
    path = _directory() / f"{plan_id}.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise PlanError("the plan lock could not be opened", exits.PRECONDITION_FAILED) from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise PlanError("this plan is already being applied", exits.LOCKED) from None
    try:
        yield
    finally:
        os.close(descriptor)
        with suppress(OSError):
            path.unlink()


def check_fresh(plan: Plan, *, now: float | None = None) -> None:
    current = time.time() if now is None else now
    if current > plan.expires_at:
        raise PlanError(
            f"plan {plan.plan_id} expired; re-plan so the preview reflects the server",
            exits.PLAN_STALE,
        )
