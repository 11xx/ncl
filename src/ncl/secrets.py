"""Secret storage for profile-scoped Nextcloud credentials.

A usable credential is the pair of a login name and an application password, so
one profile holds one record carrying both. Storing the halves separately made
an interruption mid-replacement leave a password without its login name, and let
one request read each half from a different generation.
"""

from __future__ import annotations

import json
import os
import secrets as token_source
import shutil
import subprocess
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

from . import exits

#: What an inspection concluded, without writing anything. The states are
#: separate because the remedies are: an absent binary is installed, a locked
#: store is unlocked, an uninitialised one is set up, and only a round trip can
#: say whether a store that looks ready can actually keep a credential.
UNAVAILABLE = "unavailable"
LOCKED = "locked"
MISCONFIGURED = "misconfigured"
READY = "ready"


@dataclass(frozen=True)
class BackendState:
    """What was learned about a backend, and how it was learned."""

    state: str
    detail: str
    #: True only when a value was actually stored and removed again.
    round_tripped: bool = False
    #: Effects the caller's store has already taken on, named rather than
    #: discovered afterwards in a commit log.
    side_effects: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.state == READY


class BackendProfile(Protocol):
    """The profile fields a backend needs to address one credential."""

    name: str
    secret_backend: str


BackendProfileValue = BackendProfile | str


class SecretBackend(Protocol):
    """The structural boundary shared by every credential backend."""

    def get(self, profile: BackendProfileValue, key: str) -> str | None:
        """Read one value, returning ``None`` when the entry is absent."""
        ...

    def set(self, profile: BackendProfileValue, key: str, value: str) -> None:
        """Store one value without placing it in a command argument."""
        ...

    def delete(self, profile: BackendProfileValue, key: str) -> None:
        """Remove one value from the backend."""
        ...

    def inspect(self) -> BackendState:
        """Report backend readiness without changing the store."""
        ...

    def round_trip(self) -> BackendState:
        """Store, read, and remove a probe value."""
        ...


BackendFactory = Callable[[], SecretBackend]


class SecretError(RuntimeError):
    """A secret backend could not complete an operation."""

    def __init__(self, message: str, code: int = exits.PRECONDITION_FAILED) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


def _profile_name(profile: BackendProfileValue) -> str:
    return profile.name if hasattr(profile, "name") else str(profile)


def _backend_name(profile: BackendProfileValue) -> str:
    return profile.secret_backend if hasattr(profile, "secret_backend") else str(profile)


def _run(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    try:
        return subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretError("the secret backend did not complete the request") from exc


#: The one item a profile stores. The names beside it are the layout this
#: release replaced; they are still removed on logout so a password the tool no
#: longer reads cannot outlive the account it belonged to.
RECORD_KEY = "credential"
_SUPERSEDED_KEYS = ("app_password", "login_name")

#: A profile name nothing stores under, so inspecting cannot find a credential
#: and cannot be mistaken for a lookup of one.
_INSPECT_PROFILE = "inspect-only"

RECORD_VERSION = 1
_RECORD_FIELDS = ("version", "login_name", "app_password")


def _check_key(key: str) -> str:
    if key not in {RECORD_KEY, *_SUPERSEDED_KEYS}:
        raise SecretError("unsupported credential key")
    return key


def _pass_missing(result: subprocess.CompletedProcess[str], path: str) -> bool:
    """Recognize pass's C-locale missing-entry result, not another exit-1 error."""
    return (
        result.returncode == 1
        and result.stderr.strip() == f"Error: {path} is not in the password store."
    )


@dataclass(frozen=True)
class Credential:
    """One profile's complete, usable credential."""

    login_name: str
    app_password: str


def _encode(credential: Credential) -> str:
    return json.dumps(
        {
            "version": RECORD_VERSION,
            "login_name": credential.login_name,
            "app_password": credential.app_password,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for name, value in pairs:
        if name in seen:
            raise SecretError(_MALFORMED)
        seen[name] = value
    return seen


#: Every rejection says the same thing on purpose: the caller's only remedy is
#: the same one, and naming which field was wrong describes the stored record.
_MALFORMED = (
    "the stored credential is not readable by this release; run `ncl login` to store it again"
)


def _decode(raw: str) -> Credential:
    try:
        # A stricter load than json.loads alone: it rejects a repeated field
        # rather than silently keeping whichever copy came last.
        record = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except (ValueError, TypeError) as exc:
        raise SecretError(_MALFORMED) from exc
    if not isinstance(record, dict) or sorted(record) != sorted(_RECORD_FIELDS):
        raise SecretError(_MALFORMED)
    if record["version"] != RECORD_VERSION or isinstance(record["version"], bool):
        raise SecretError(_MALFORMED)
    values = {name: record[name] for name in ("login_name", "app_password")}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise SecretError(_MALFORMED)
    return Credential(**values)


@dataclass(frozen=True)
class PassBackend:
    """A password-store backend using stdin for secret input."""

    executable: str = "pass"

    def _path(self, profile: BackendProfileValue, key: str) -> str:
        return f"ncl/{_profile_name(profile)}/{_check_key(key)}"

    def get(self, profile: BackendProfileValue, key: str) -> str | None:
        path = self._path(profile, key)
        result = _run([self.executable, "show", path])
        if result.returncode != 0:
            if _pass_missing(result, path):
                return None
            raise SecretError("the pass backend could not read the credential")
        value = result.stdout.rstrip("\n")
        if not value:
            raise SecretError("the pass backend returned an empty credential")
        return value

    def set(self, profile: BackendProfileValue, key: str, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise SecretError("cannot store an empty credential")
        result = _run(
            [self.executable, "insert", "--multiline", "--force", self._path(profile, key)],
            input_text=f"{value}\n",
        )
        if result.returncode != 0:
            raise SecretError("the pass backend could not store the credential")

    def delete(self, profile: BackendProfileValue, key: str) -> None:
        result = _run([self.executable, "rm", "--force", self._path(profile, key)])
        if result.returncode != 0:
            raise SecretError("the pass backend could not remove the credential")

    def inspect(self) -> BackendState:
        """Report what the store looks like, writing nothing.

        A password store backed by Git turns every insertion and removal into a
        commit, so a diagnostic that round-trips by default writes history the
        caller never asked for. Listing is enough to separate an absent binary
        from an uninitialised store from one that is ready to be tried.
        """
        if shutil.which(self.executable) is None:
            return BackendState(UNAVAILABLE, "pass is not installed")
        try:
            result = _run([self.executable, "ls"])
        except SecretError:
            # A diagnostic that raises has failed at the one thing it is for.
            return BackendState(MISCONFIGURED, "pass did not respond")
        if result.returncode == 0:
            return BackendState(READY, "pass is installed and its store is initialised")
        return BackendState(
            MISCONFIGURED,
            "pass is installed but its store did not list; run `pass init`",
        )

    def round_trip(self) -> BackendState:
        """Store, read back, and remove one value, saying what that cost.

        This is the only check that proves a store can keep a credential, and
        the only one that changes it. Nothing calls it implicitly.
        """
        state = self.inspect()
        if not state.usable:
            return state
        effects = (
            "wrote and removed one entry under ncl/, which a Git-backed store "
            "records as two commits",
        )
        profile = f"probe-{token_source.token_hex(12)}"
        key = RECORD_KEY
        value = token_source.token_urlsafe(24)
        stored = False
        try:
            self.set(profile, key, value)
            stored = True
            if self.get(profile, key) != value:
                return BackendState(
                    MISCONFIGURED,
                    "pass did not return the value it stored",
                    side_effects=effects,
                )
            self.delete(profile, key)
            stored = False
            return BackendState(
                READY,
                "pass stored, returned, and removed a value",
                round_tripped=True,
                side_effects=effects,
            )
        except SecretError:
            return BackendState(
                MISCONFIGURED, "pass could not round-trip a value", side_effects=effects
            )
        finally:
            if stored:
                # The value itself is never named, only the path that may hold it.
                with suppress(SecretError):
                    self.delete(profile, key)


@dataclass(frozen=True)
class LibsecretBackend:
    """A Secret Service backend using secret-tool attributes."""

    executable: str = "secret-tool"

    def _attributes(self, profile: BackendProfileValue, key: str) -> list[str]:
        return ["service", "ncl", "profile", _profile_name(profile), "key", _check_key(key)]

    def get(self, profile: BackendProfileValue, key: str) -> str | None:
        result = _run([self.executable, "lookup", *self._attributes(profile, key)])
        if result.returncode != 0:
            if result.returncode == 1 and not result.stderr.strip():
                return None
            raise SecretError("the libsecret backend could not read the credential")
        value = result.stdout.rstrip("\n")
        return value or None

    def set(self, profile: BackendProfileValue, key: str, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise SecretError("cannot store an empty credential")
        result = _run(
            [
                self.executable,
                "store",
                "--label=ncl credential",
                *self._attributes(profile, key),
            ],
            input_text=f"{value}\n",
        )
        if result.returncode != 0:
            raise SecretError("the libsecret backend could not store the credential")

    def delete(self, profile: BackendProfileValue, key: str) -> None:
        result = _run([self.executable, "clear", *self._attributes(profile, key)])
        if result.returncode != 0:
            raise SecretError("the libsecret backend could not remove the credential")

    def inspect(self) -> BackendState:
        """Report whether Secret Service answers, writing nothing.

        A lookup for attributes nothing carries is the read-only question: the
        service answers it with an empty success, an unreachable one fails.
        """
        if shutil.which(self.executable) is None:
            return BackendState(UNAVAILABLE, "secret-tool is not installed")
        try:
            result = _run(
                [self.executable, "lookup", *self._attributes(_INSPECT_PROFILE, RECORD_KEY)]
            )
        except SecretError:
            # A locked collection can sit on an unlock prompt until `_run` times
            # out. That is a store waiting to be unlocked, not a broken one.
            return BackendState(LOCKED, "Secret Service did not answer; unlock the keyring")
        if result.returncode == 0 or (result.returncode == 1 and not result.stderr.strip()):
            return BackendState(READY, "Secret Service answered a lookup")
        detail = result.stderr.strip().lower()
        if "lock" in detail or "dismissed" in detail or "prompt" in detail:
            return BackendState(LOCKED, "Secret Service is locked; unlock the login keyring")
        return BackendState(
            MISCONFIGURED, "secret-tool is installed but Secret Service did not answer"
        )

    def round_trip(self) -> BackendState:
        """Store, read back, and remove one value, saying what that cost."""
        state = self.inspect()
        if not state.usable:
            return state
        effects = ("wrote and removed one Secret Service item labelled `ncl credential`",)
        profile = f"probe-{token_source.token_hex(12)}"
        key = RECORD_KEY
        value = token_source.token_urlsafe(24)
        stored = False
        try:
            self.set(profile, key, value)
            stored = True
            if self.get(profile, key) != value:
                return BackendState(
                    MISCONFIGURED,
                    "Secret Service did not return the value it stored",
                    side_effects=effects,
                )
            self.delete(profile, key)
            stored = False
            return BackendState(
                READY,
                "Secret Service stored, returned, and removed a value",
                round_tripped=True,
                side_effects=effects,
            )
        except SecretError:
            return BackendState(
                MISCONFIGURED,
                "Secret Service could not round-trip a value",
                side_effects=effects,
            )
        finally:
            if stored:
                with suppress(SecretError):
                    self.delete(profile, key)


_BACKEND_FACTORIES: dict[str, BackendFactory] = {
    "pass": PassBackend,
    "libsecret": LibsecretBackend,
}
DEFAULT_BACKEND = "pass"


def backend_names() -> tuple[str, ...]:
    """Return the configured backend names in their stable registry order."""
    return tuple(_BACKEND_FACTORIES)


def backend_for(name: str) -> SecretBackend:
    """Create a fresh adapter for a registered backend name."""
    try:
        factory = _BACKEND_FACTORIES[name]
    except KeyError as exc:
        raise SecretError("unsupported secret backend") from exc
    return factory()


def get(profile: BackendProfileValue, key: str) -> str | None:
    """Read one credential from the profile's configured backend."""
    return backend_for(_backend_name(profile)).get(profile, key)


def set(profile: BackendProfileValue, key: str, value: str) -> None:
    """Write one credential without putting its value in a command argument."""
    backend_for(_backend_name(profile)).set(profile, key, value)


def delete(profile: BackendProfileValue, key: str) -> None:
    """Delete one credential from the profile's configured backend."""
    backend_for(_backend_name(profile)).delete(profile, key)


def inspect_backend(name: str) -> BackendState:
    """Report a backend's state without storing or removing anything."""
    try:
        backend = backend_for(name)
    except SecretError as exc:
        return BackendState(MISCONFIGURED, exc.message)
    return backend.inspect()


def round_trip_backend(name: str) -> BackendState:
    """Prove a backend can keep a value, by storing and removing one."""
    try:
        backend = backend_for(name)
    except SecretError as exc:
        return BackendState(MISCONFIGURED, exc.message)
    return backend.round_trip()


def probe(profile: BackendProfileValue | None = None) -> bool:
    """Prove the profile's backend can keep the credential about to be issued.

    Login Flow v2 returns the application password exactly once, so this is the
    one place a real round trip is load-bearing rather than diagnostic: an
    unusable store discovered afterwards costs a second trip through consent and
    leaves an application password nobody holds.
    """
    name = _backend_name(profile) if profile is not None else DEFAULT_BACKEND
    backend = backend_for(name)
    return backend.round_trip().usable


def load_credential(profile: BackendProfileValue) -> Credential | None:
    """Return the profile's complete credential, or None if none is stored.

    One backend read yields one generation of the record, so a caller cannot
    observe a login name from one login paired with a password from another.
    """
    raw = get(profile, RECORD_KEY)
    if raw is None:
        return None
    return _decode(raw)


def store_credential(profile: BackendProfileValue, login_name: str, app_password: str) -> None:
    """Replace the profile's credential with one write.

    There is nothing to roll back and no window to be interrupted in: the record
    is written whole, so the backend holds either the previous credential or the
    new one.
    """
    if not login_name or not app_password:
        raise SecretError("the server returned an empty credential")
    set(profile, RECORD_KEY, _encode(Credential(login_name, app_password)))


def has_credential(profile: BackendProfileValue) -> bool:
    """Return whether a usable credential is stored.

    A record that cannot be decoded is not a credential, but it is also not
    absence: it is reported as a conflict so that storing over it stays a
    deliberate act.
    """
    return get(profile, RECORD_KEY) is not None


def clear_credential(profile: BackendProfileValue) -> None:
    """Remove the profile's credential, and any entry the old layout left."""
    errors: list[SecretError] = []
    for key in (RECORD_KEY, *_SUPERSEDED_KEYS):
        try:
            if get(profile, key) is None:
                continue
            delete(profile, key)
        except SecretError as exc:
            errors.append(exc)
    if errors:
        raise SecretError("the secret backend could not remove the local credential")
