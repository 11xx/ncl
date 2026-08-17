"""Secret storage for profile-scoped Nextcloud credentials."""

from __future__ import annotations

import secrets as token_source
import shutil
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from . import exits


class SecretError(RuntimeError):
    """A secret backend could not complete an operation."""

    def __init__(self, message: str, code: int = exits.PRECONDITION_FAILED) -> None:
        self.code = code
        super().__init__(message)


def _profile_name(profile: Any) -> str:
    return profile.name if hasattr(profile, "name") else str(profile)


def _backend_name(profile: Any) -> str:
    return profile.secret_backend if hasattr(profile, "secret_backend") else str(profile)


def _run(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretError("the secret backend did not complete the request") from exc


def _check_key(key: str) -> str:
    if key not in {"app_password", "login_name"}:
        raise SecretError("unsupported credential key")
    return key


@dataclass(frozen=True)
class PassBackend:
    """A password-store backend using stdin for secret input."""

    executable: str = "pass"

    def _path(self, profile: Any, key: str) -> str:
        return f"ncl/{_profile_name(profile)}/{_check_key(key)}"

    def get(self, profile: Any, key: str) -> str | None:
        result = _run([self.executable, "show", self._path(profile, key)])
        if result.returncode != 0:
            return None
        value = result.stdout.rstrip("\n")
        return value or None

    def set(self, profile: Any, key: str, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise SecretError("cannot store an empty credential")
        result = _run(
            [self.executable, "insert", "--multiline", "--force", self._path(profile, key)],
            input_text=f"{value}\n",
        )
        if result.returncode != 0:
            raise SecretError("the pass backend could not store the credential")

    def delete(self, profile: Any, key: str) -> None:
        result = _run([self.executable, "rm", "--force", self._path(profile, key)])
        if result.returncode != 0:
            raise SecretError("the pass backend could not remove the credential")

    def probe(self) -> tuple[bool, str]:
        if shutil.which(self.executable) is None:
            return False, "pass binary is not installed"
        profile = f"probe-{token_source.token_hex(12)}"
        key = "app_password"
        value = token_source.token_urlsafe(24)
        stored = False
        try:
            self.set(profile, key, value)
            stored = True
            if self.get(profile, key) != value:
                return False, "pass could not return the stored probe value"
            self.delete(profile, key)
            stored = False
            return True, "pass stored, returned, and removed a probe value"
        except SecretError:
            return False, "pass could not round-trip a probe value"
        finally:
            if stored:
                with suppress(SecretError):
                    self.delete(profile, key)


@dataclass(frozen=True)
class LibsecretBackend:
    """A Secret Service backend using secret-tool attributes."""

    executable: str = "secret-tool"

    def _attributes(self, profile: Any, key: str) -> list[str]:
        return ["service", "ncl", "profile", _profile_name(profile), "key", _check_key(key)]

    def get(self, profile: Any, key: str) -> str | None:
        result = _run([self.executable, "lookup", *self._attributes(profile, key)])
        if result.returncode != 0:
            return None
        value = result.stdout.rstrip("\n")
        return value or None

    def set(self, profile: Any, key: str, value: str) -> None:
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

    def delete(self, profile: Any, key: str) -> None:
        result = _run([self.executable, "clear", *self._attributes(profile, key)])
        if result.returncode != 0:
            raise SecretError("the libsecret backend could not remove the credential")

    def probe(self) -> tuple[bool, str]:
        if shutil.which(self.executable) is None:
            return False, "secret-tool binary is not installed"
        profile = f"probe-{token_source.token_hex(12)}"
        key = "app_password"
        value = token_source.token_urlsafe(24)
        stored = False
        try:
            self.set(profile, key, value)
            stored = True
            if self.get(profile, key) != value:
                return False, "Secret Service could not return the stored probe value"
            self.delete(profile, key)
            stored = False
            return True, "Secret Service stored, returned, and removed a probe value"
        except SecretError:
            return False, "Secret Service could not round-trip a probe value"
        finally:
            if stored:
                with suppress(SecretError):
                    self.delete(profile, key)


def _backend(profile_or_name: Any):
    name = _backend_name(profile_or_name)
    if name == "pass":
        return PassBackend()
    if name == "libsecret":
        return LibsecretBackend()
    raise SecretError("unsupported secret backend")


def get(profile: Any, key: str) -> str | None:
    """Read one credential from the profile's configured backend."""
    return _backend(profile).get(profile, key)


def set(profile: Any, key: str, value: str) -> None:
    """Write one credential without putting its value in a command argument."""
    _backend(profile).set(profile, key, value)


def delete(profile: Any, key: str) -> None:
    """Delete one credential from the profile's configured backend."""
    _backend(profile).delete(profile, key)


def probe(profile: Any | None = None) -> bool:
    """Return whether the profile's backend can round-trip a throwaway value."""
    backend = _backend(profile) if profile is not None else PassBackend()
    return backend.probe()[0]


def probe_backend(name: str) -> tuple[bool, str]:
    """Return the detailed result used by doctor for a configured backend."""
    if name == "pass":
        return PassBackend().probe()
    if name == "libsecret":
        return LibsecretBackend().probe()
    return False, "unsupported secret backend"


def store_credentials(profile: Any, login_name: str, app_password: str) -> None:
    """Replace both credential entries as one recoverable operation."""
    if not login_name or not app_password:
        raise SecretError("the server returned an empty credential")
    old = {key: get(profile, key) for key in ("app_password", "login_name")}
    changed: list[str] = []
    try:
        set(profile, "app_password", app_password)
        changed.append("app_password")
        set(profile, "login_name", login_name)
        changed.append("login_name")
    except SecretError:
        for key in reversed(changed):
            try:
                if old[key] is None:
                    delete(profile, key)
                else:
                    set(profile, key, old[key])
            except SecretError:
                pass
        raise
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        for key in reversed(changed):
            try:
                if old[key] is None:
                    delete(profile, key)
                else:
                    set(profile, key, old[key])
            except SecretError:
                pass
        raise SecretError("the secret backend could not store the credential") from exc


def has_credentials(profile: Any) -> bool:
    """Return whether either credential entry is already populated."""
    return bool(get(profile, "app_password") or get(profile, "login_name"))


def clear_credentials(profile: Any) -> None:
    """Remove both local credential entries."""
    errors: list[SecretError] = []
    for key in ("app_password", "login_name"):
        try:
            if get(profile, key) is None:
                continue
            delete(profile, key)
        except SecretError as exc:
            errors.append(exc)
    if errors:
        raise SecretError("the secret backend could not remove the local credential")
