"""Ordered frozen mutation bundles and their resumable lifecycle.

Planning resolves a mutation and freezes the exact request that a resource
module will execute later. The plan layer owns the bundle boundary: it
validates every step before the first request, records progress while holding
the claim lock, and consumes the bundle only after every step is verified.
Resource modules know how to execute and reconcile their own steps, but they
do not own plan freshness or storage.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import json
import math
import os
import secrets as token_source
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import exits, profiles

# A plan with no recorded remote progress describes a resource that may change
# under it, so it has a short freshness window. Progress changes the expiry to
# ``None`` and the plan then remains available for resume or reconciliation.
DEFAULT_TTL_SECONDS = 900
PROGRESS_STATES = frozenset({"pending", "verified", "uncertain"})


class PlanError(RuntimeError):
    """A plan could not be written, read, validated, or applied."""

    def __init__(self, message: str, code: int = exits.ERROR) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Step:
    """One immutable resource mutation inside an ordered plan."""

    action: str
    href: str
    etag: str
    summary: str
    payload: str
    content_type: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return metadata without exposing the private request body.

        A step whose payload is itself a secret withholds its length too. For
        an ordinary body the size is useful and tells nobody anything; for a
        password it is the one property of the value worth guessing from, and
        this view is what a `--json` caller passes on.
        """
        view = {
            "action": self.action,
            "href": self.href,
            "etag": self.etag,
            "summary": self.summary,
            "content_type": self.content_type,
            "details": self.details,
        }
        if not self.details.get("secret_payload"):
            view["payload_bytes"] = len(payload_bytes(self))
        return view


@dataclass(frozen=True)
class Progress:
    """Durable execution state for one step."""

    state: str
    timestamp: float
    exit_code: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "timestamp": self.timestamp,
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True)
class Plan:
    """An ordered, nonempty bundle of frozen resource steps."""

    plan_id: str
    profile: str
    profile_fingerprint: str
    summary: str
    steps: tuple[Step, ...]
    created_at: float
    expires_at: float | None
    progress: tuple[Progress, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return the caller-visible plan view with payload bodies redacted."""
        validate_shape(self)
        return {
            "plan_id": self.plan_id,
            "profile": self.profile,
            "profile_fingerprint": self.profile_fingerprint,
            "summary": self.summary,
            "steps": [step.as_dict() for step in self.steps],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "progress": [item.as_dict() for item in self.progress],
        }


@dataclass(frozen=True)
class Dispatcher:
    """Callbacks for one app/resource action namespace."""

    validate: Callable[[Step], None]
    execute: Callable[..., dict[str, Any]]
    reconcile: Callable[..., dict[str, Any]]
    validate_bundle: Callable[[tuple[Step, ...]], None] | None = None


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


def freeze_step(
    *,
    action: str,
    href: str,
    etag: str,
    summary: str,
    payload: bytes = b"",
    content_type: str = "",
    details: Mapping[str, Any] | None = None,
) -> Step:
    """Freeze one request body and its resource metadata for a bundle."""
    if not isinstance(payload, bytes):
        raise PlanError("a frozen step payload must be bytes", exits.USAGE)
    return Step(
        action=action,
        href=href,
        etag=etag,
        summary=summary,
        payload=base64.b64encode(payload).decode("ascii"),
        content_type=content_type,
        details=dict(details or {}),
    )


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _shape_error(plan_id: str, message: str) -> PlanError:
    return PlanError(f"plan {plan_id} has {message}", exits.PLAN_STALE)


def _validate_step_shape(plan_id: str, step: Any) -> None:
    if not isinstance(step, Step):
        raise _shape_error(plan_id, "an invalid step shape")
    for name in ("action", "href", "etag", "summary", "payload", "content_type"):
        if not isinstance(getattr(step, name), str):
            raise _shape_error(plan_id, f"an invalid step {name}")
    if not isinstance(step.details, dict):
        raise _shape_error(plan_id, "invalid step details")
    try:
        base64.b64decode(step.payload, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise _shape_error(plan_id, "an invalid base64 payload") from exc


def validate_shape(plan: Plan, *, plan_id: str | None = None) -> None:
    """Reject malformed current plans and all earlier serialized shapes."""
    expected_id = plan_id or getattr(plan, "plan_id", "")
    if not isinstance(plan, Plan):
        raise PlanError("the stored plan has an unexpected shape", exits.PLAN_STALE)
    if not isinstance(plan.plan_id, str) or not plan.plan_id or (
        plan_id is not None and plan.plan_id != plan_id
    ):
        raise _shape_error(expected_id, "an invalid plan id")
    if not isinstance(plan.profile, str) or not plan.profile:
        raise _shape_error(plan.plan_id, "an invalid profile")
    if (
        not isinstance(plan.profile_fingerprint, str)
        or len(plan.profile_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in plan.profile_fingerprint)
    ):
        raise _shape_error(plan.plan_id, "an invalid profile fingerprint")
    if not isinstance(plan.summary, str):
        raise _shape_error(plan.plan_id, "an invalid bundle summary")
    if not _finite_number(plan.created_at):
        raise _shape_error(plan.plan_id, "an invalid creation timestamp")
    if plan.expires_at is not None and not _finite_number(plan.expires_at):
        raise _shape_error(plan.plan_id, "an invalid expiry timestamp")
    if plan.expires_at is not None and plan.expires_at < plan.created_at:
        raise _shape_error(plan.plan_id, "an expiry before its creation")
    if not isinstance(plan.steps, tuple) or not plan.steps:
        raise _shape_error(plan.plan_id, "a missing or empty step sequence")
    for step in plan.steps:
        _validate_step_shape(plan.plan_id, step)
    if not isinstance(plan.progress, tuple) or len(plan.progress) != len(plan.steps):
        raise _shape_error(plan.plan_id, "progress that does not match its steps")
    for progress in plan.progress:
        if not isinstance(progress, Progress):
            raise _shape_error(plan.plan_id, "an invalid progress entry")
        if progress.state not in PROGRESS_STATES:
            raise _shape_error(plan.plan_id, "an invalid progress state")
        if not _finite_number(progress.timestamp):
            raise _shape_error(plan.plan_id, "an invalid progress timestamp")
        if progress.exit_code is not None and (
            not isinstance(progress.exit_code, int) or isinstance(progress.exit_code, bool)
        ):
            raise _shape_error(plan.plan_id, "an invalid progress exit classification")


def _serialized(plan: Plan) -> dict[str, Any]:
    validate_shape(plan)
    return {
        "plan_id": plan.plan_id,
        "profile": plan.profile,
        "profile_fingerprint": plan.profile_fingerprint,
        "summary": plan.summary,
        "steps": [
            {
                "action": step.action,
                "href": step.href,
                "etag": step.etag,
                "summary": step.summary,
                "payload": step.payload,
                "content_type": step.content_type,
                "details": step.details,
            }
            for step in plan.steps
        ],
        "created_at": plan.created_at,
        "expires_at": plan.expires_at,
        "progress": [item.as_dict() for item in plan.progress],
    }


def _store(plan: Plan) -> None:
    path = _path(plan.plan_id)
    temporary = path.with_name(f".{path.name}.{token_source.token_hex(6)}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(_serialized(plan), handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError) as exc:
        with suppress(OSError):
            temporary.unlink()
        raise PlanError("the plan could not be stored", exits.PRECONDITION_FAILED) from exc


def write_bundle(
    *,
    profile: Any,
    summary: str,
    steps: Iterable[Step],
    ttl: float = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> Plan:
    """Atomically store a nonempty ordered sequence of frozen steps."""
    profile_name = profile if isinstance(profile, str) else getattr(profile, "name", None)
    if not isinstance(profile_name, str) or not profile_name:
        raise PlanError("a plan needs a profile", exits.USAGE)
    if not isinstance(summary, str):
        raise PlanError("a plan summary must be text", exits.USAGE)
    if not _finite_number(ttl) or ttl < 0:
        raise PlanError("the plan TTL must be a non-negative finite number", exits.USAGE)
    frozen_steps = tuple(steps)
    if not frozen_steps:
        raise PlanError("a plan needs at least one step", exits.USAGE)
    created = time.time() if now is None else now
    if not _finite_number(created):
        raise PlanError("the plan creation time is invalid", exits.USAGE)
    try:
        profile_hash = profiles.fingerprint(profile)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PlanError("the plan profile is not usable", exits.USAGE) from exc
    plan = Plan(
        plan_id=token_source.token_hex(8),
        profile=profile_name,
        profile_fingerprint=profile_hash,
        summary=summary,
        steps=frozen_steps,
        created_at=float(created),
        expires_at=float(created + ttl),
        progress=tuple(Progress("pending", float(created), None) for _ in frozen_steps),
    )
    validate_shape(plan)
    _store(plan)
    return plan


def write(
    *,
    profile: Any,
    summary: str,
    steps: Iterable[Step],
    ttl: float = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> Plan:
    """Store a bundle using the plan writer used by all resource planners."""
    return write_bundle(profile=profile, summary=summary, steps=steps, ttl=ttl, now=now)


def payload_bytes(step: Step) -> bytes:
    """Return one private frozen request body after validating its encoding."""
    if not isinstance(step, Step) or not isinstance(step.payload, str):
        raise PlanError("the frozen step has an invalid payload", exits.PLAN_STALE)
    try:
        return base64.b64decode(step.payload, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise PlanError("the frozen step has an invalid payload", exits.PLAN_STALE) from exc


def _decode_plan(plan_id: str, data: Any) -> Plan:
    required = {
        "plan_id",
        "profile",
        "profile_fingerprint",
        "summary",
        "steps",
        "created_at",
        "expires_at",
        "progress",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise _shape_error(plan_id, "an unexpected serialized shape")
    if data["plan_id"] != plan_id:
        raise _shape_error(plan_id, "a mismatched plan id")
    raw_steps = data["steps"]
    if not isinstance(raw_steps, list) or not raw_steps:
        raise _shape_error(plan_id, "a missing or empty step sequence")
    step_keys = {"action", "href", "etag", "summary", "payload", "content_type", "details"}
    steps: list[Step] = []
    for raw in raw_steps:
        if not isinstance(raw, dict) or set(raw) != step_keys:
            raise _shape_error(plan_id, "an unexpected step shape")
        step = Step(
            action=raw["action"],
            href=raw["href"],
            etag=raw["etag"],
            summary=raw["summary"],
            payload=raw["payload"],
            content_type=raw["content_type"],
            details=raw["details"],
        )
        _validate_step_shape(plan_id, step)
        steps.append(step)
    raw_progress = data["progress"]
    progress_keys = {"state", "timestamp", "exit_code"}
    if not isinstance(raw_progress, list) or len(raw_progress) != len(steps):
        raise _shape_error(plan_id, "progress that does not match its steps")
    progress: list[Progress] = []
    for raw in raw_progress:
        if not isinstance(raw, dict) or set(raw) != progress_keys:
            raise _shape_error(plan_id, "an unexpected progress shape")
        progress.append(
            Progress(
                state=raw["state"],
                timestamp=raw["timestamp"],
                exit_code=raw["exit_code"],
            )
        )
    plan = Plan(
        plan_id=data["plan_id"],
        profile=data["profile"],
        profile_fingerprint=data["profile_fingerprint"],
        summary=data["summary"],
        steps=tuple(steps),
        created_at=data["created_at"],
        expires_at=data["expires_at"],
        progress=tuple(progress),
    )
    validate_shape(plan, plan_id=plan_id)
    return plan


def read(plan_id: str) -> Plan:
    path = _path(plan_id)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise PlanError(f"no plan {plan_id} exists", exits.TARGET_NOT_FOUND) from exc
    except json.JSONDecodeError as exc:
        raise PlanError(f"plan {plan_id} is malformed", exits.PLAN_STALE) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanError(f"plan {plan_id} is unreadable", exits.MALFORMED_RESPONSE) from exc
    return _decode_plan(plan_id, data)


def listing() -> list[Plan]:
    """Read every stored plan, failing closed on malformed local state."""
    return [read(path.stem) for path in sorted(_directory().glob("*.json"))]


def consume(plan_id: str) -> None:
    """Delete a completed or explicitly cancelled plan."""
    try:
        _path(plan_id).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PlanError(f"plan {plan_id} could not be consumed", exits.ERROR) from exc


def update_progress(
    plan: Plan,
    index: int,
    *,
    state: str,
    exit_code: int | None,
    timestamp: float | None = None,
) -> Plan:
    """Persist one step's state with an atomic 0600 replacement.

    Callers hold :func:`claim` while invoking this function. The plan's expiry
    becomes permanent as soon as any step records remote progress, and remains
    permanent even if reconciliation later returns that step to ``pending``.
    """
    validate_shape(plan)
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(plan.steps):
        raise PlanError(f"plan {plan.plan_id} has an invalid progress index", exits.PLAN_STALE)
    if state not in PROGRESS_STATES:
        raise PlanError(f"plan {plan.plan_id} has an invalid progress state", exits.PLAN_STALE)
    if exit_code is not None and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool)
    ):
        raise PlanError(
            f"plan {plan.plan_id} has an invalid progress exit classification",
            exits.PLAN_STALE,
        )
    moment = time.time() if timestamp is None else timestamp
    if not _finite_number(moment):
        raise PlanError(f"plan {plan.plan_id} has an invalid progress timestamp", exits.USAGE)
    progress = list(plan.progress)
    progress[index] = Progress(state, float(moment), exit_code)
    updated = replace(
        plan,
        expires_at=None
        if plan.expires_at is None or state in {"verified", "uncertain"}
        else plan.expires_at,
        progress=tuple(progress),
    )
    validate_shape(updated)
    _store(updated)
    return updated


def check_fresh(plan: Plan, *, now: float | None = None) -> None:
    """Reject only an untouched plan whose current TTL has elapsed."""
    validate_shape(plan)
    if plan.expires_at is None or any(item.state != "pending" for item in plan.progress):
        return
    current = time.time() if now is None else now
    if current > plan.expires_at:
        raise PlanError(
            f"plan {plan.plan_id} expired; re-plan so the preview reflects the server",
            exits.PLAN_STALE,
        )


def _dispatcher(step: Step, dispatchers: Mapping[str, Dispatcher]) -> Dispatcher:
    matches = [
        dispatcher
        for prefix, dispatcher in dispatchers.items()
        if step.action.startswith(prefix)
    ]
    if len(matches) != 1:
        raise PlanError(f"unknown plan action {step.action!r}", exits.USAGE)
    return matches[0]


def _validate_for_lifecycle(
    profile: Any,
    plan: Plan,
    dispatchers: Mapping[str, Dispatcher],
    *,
    fresh: bool,
) -> None:
    validate_shape(plan)
    profile_name = getattr(profile, "name", None)
    if not isinstance(profile_name, str) or not profile_name:
        raise PlanError("the selected profile is invalid", exits.USAGE)
    if plan.profile != profile_name:
        raise PlanError(
            f"plan {plan.plan_id} was made for profile {plan.profile!r}", exits.USAGE
        )
    try:
        current_fingerprint = profiles.fingerprint(profile)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PlanError("the selected profile is invalid", exits.USAGE) from exc
    if plan.profile_fingerprint != current_fingerprint:
        raise PlanError(
            f"plan {plan.plan_id} was made for a different profile configuration; re-plan",
            exits.PLAN_STALE,
        )
    if fresh:
        check_fresh(plan)
    # Validate the complete action set before asking any resource module to
    # execute a request. This is what makes an unknown later step harmless.
    selected: list[Dispatcher] = []
    grouped: dict[int, list[Step]] = {}
    for step in plan.steps:
        dispatcher = _dispatcher(step, dispatchers)
        dispatcher.validate(step)
        identity = id(dispatcher)
        if identity not in grouped:
            selected.append(dispatcher)
            grouped[identity] = []
        grouped[identity].append(step)

    for dispatcher in selected:
        if dispatcher.validate_bundle is not None:
            dispatcher.validate_bundle(tuple(grouped[id(dispatcher)]))


def _exception_code(exc: Exception) -> int:
    code = getattr(exc, "code", exits.ERROR)
    return code if isinstance(code, int) and not isinstance(code, bool) else exits.ERROR


def _bundle_result(plan: Plan, results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "summary": plan.summary,
        "steps": results,
        "complete": all(item.state == "verified" for item in plan.progress),
    }


def apply(
    profile: Any,
    *,
    session: Any,
    plan: Plan,
    dispatchers: Mapping[str, Dispatcher],
) -> dict[str, Any]:
    """Execute pending steps in order and durably record each result."""
    _validate_for_lifecycle(profile, plan, dispatchers, fresh=True)
    results: list[dict[str, Any]] = []
    for index, (step, progress) in enumerate(zip(plan.steps, plan.progress, strict=True)):
        if progress.state == "verified":
            results.append(
                {
                    "index": index,
                    "action": step.action,
                    "href": step.href,
                    "state": "verified",
                    "skipped": True,
                }
            )
            continue
        if progress.state == "uncertain":
            raise PlanError(
                f"plan {plan.plan_id} step {index + 1} is uncertain; reconcile it before applying",
                exits.OUTCOME_UNCERTAIN,
            )
        dispatcher = _dispatcher(step, dispatchers)
        try:
            result = dispatcher.execute(profile, session=session, step=step)
        except Exception as exc:
            code = _exception_code(exc)
            state = "uncertain" if code == exits.OUTCOME_UNCERTAIN else "pending"
            update_progress(plan, index, state=state, exit_code=code)
            raise
        plan = update_progress(plan, index, state="verified", exit_code=exits.OK)
        results.append({"index": index, "state": "verified", **result})

    if all(item.state == "verified" for item in plan.progress):
        consume(plan.plan_id)
    if len(plan.steps) == 1 and results:
        return {key: value for key, value in results[0].items() if key not in {"index", "state"}}
    return _bundle_result(plan, results)


def reconcile(
    profile: Any,
    *,
    session: Any,
    plan: Plan,
    dispatchers: Mapping[str, Dispatcher],
) -> dict[str, Any]:
    """Read and classify only the first uncertain step in a plan."""
    _validate_for_lifecycle(profile, plan, dispatchers, fresh=False)
    try:
        index = next(index for index, item in enumerate(plan.progress) if item.state == "uncertain")
    except StopIteration as exc:
        raise PlanError(
            f"plan {plan.plan_id} has no uncertain step to reconcile", exits.USAGE
        ) from exc
    step = plan.steps[index]
    dispatcher = _dispatcher(step, dispatchers)
    try:
        outcome = dispatcher.reconcile(profile, session=session, step=step)
    except Exception as exc:
        code = _exception_code(exc)
        update_progress(plan, index, state="uncertain", exit_code=code)
        raise
    state = outcome.get("state") if isinstance(outcome, dict) else None
    if state not in PROGRESS_STATES:
        update_progress(plan, index, state="uncertain", exit_code=exits.MALFORMED_RESPONSE)
        raise PlanError(
            f"plan {plan.plan_id} reconciliation returned an invalid state",
            exits.MALFORMED_RESPONSE,
        )
    code = exits.OUTCOME_UNCERTAIN if state == "uncertain" else exits.OK
    plan = update_progress(plan, index, state=state, exit_code=code)
    if state == "uncertain":
        raise PlanError(
            f"plan {plan.plan_id} step {index + 1} remains uncertain; resolve the "
            "resource before applying",
            exits.OUTCOME_UNCERTAIN,
        )
    complete = all(item.state == "verified" for item in plan.progress)
    if complete:
        consume(plan.plan_id)
    result = {
        "plan_id": plan.plan_id,
        "index": index,
        "action": step.action,
        "href": step.href,
        "state": state,
        "complete": complete,
    }
    for key, value in outcome.items():
        if key != "state":
            result[key] = value
    return result


@contextmanager
def claim(plan_id: str):
    """Hold a plan exclusively while it is read, executed, or reconciled.

    The lock file persists, while the kernel owns lock liveness through
    ``flock``. This is the same rule used by ``login._profile_lock``.
    """
    plan_path = _path(plan_id)
    path = plan_path.parent / f"{plan_id}.lock"
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
