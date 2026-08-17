"""Precondition checks that do not contact a Nextcloud server."""

from __future__ import annotations

import contextlib
import secrets
import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, exits, profiles


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class Report:
    checks: tuple[Check, ...]

    @property
    def exit_code(self) -> int:
        return (
            exits.OK
            if all(check.status != "fail" for check in self.checks)
            else exits.PRECONDITION_FAILED
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "checks": [check.as_dict() for check in self.checks],
            "response": {str(code): message for code, message in exits.RESPONSE.items()},
        }


def probe_pass() -> tuple[bool, str]:
    """Check that pass is installed and has an initialized password store."""
    if shutil.which("pass") is None:
        return False, "pass binary is not installed"
    store = Path("~/.password-store").expanduser()
    gpg_id = store / ".gpg-id"
    if not store.is_dir() or not gpg_id.is_file():
        return False, f"{store} is not an initialized password store"
    return True, "pass and the password store are available"


def _secret_tool_call(
    command: list[str], *, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


def probe_libsecret() -> tuple[bool, str]:
    """Verify Secret Service by storing, looking up, and clearing a probe."""
    tool = shutil.which("secret-tool")
    if tool is None:
        return False, "secret-tool binary is not installed"

    attribute = secrets.token_hex(16)
    value = secrets.token_hex(24)
    command = [tool, "ncl-doctor", attribute]
    stored = False
    try:
        result = _secret_tool_call(
            [tool, "store", "--label=ncl doctor probe", *command[1:]],
            input_text=f"{value}\n",
        )
        if result.returncode != 0:
            return False, "secret-tool could not store a probe value"
        stored = True

        result = _secret_tool_call([tool, "lookup", *command[1:]])
        if result.returncode != 0 or result.stdout.rstrip("\n") != value:
            return False, "secret-tool could not look up the stored probe value"
        result = _secret_tool_call([tool, "clear", *command[1:]])
        if result.returncode != 0:
            return False, "secret-tool could not clear the probe value"
        stored = False
        return True, "Secret Service stored, returned, and cleared a probe value"
    except (OSError, subprocess.TimeoutExpired):
        return False, "secret-tool did not complete the probe"
    finally:
        if stored:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                _secret_tool_call([tool, "clear", *command[1:]])


def probe_callback_port(port: int) -> tuple[bool, str]:
    """Check that the configured OAuth callback port is bindable on loopback."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
    except OSError as exc:
        return False, f"loopback callback port {port} is not bindable: {exc}"
    return True, f"loopback callback port {port} is bindable"


def _check(name: str, status: str, detail: str) -> Check:
    return Check(name=name, status=status, detail=detail)


def _run_probe(name: str, probe: Callable[[], tuple[bool, str]]) -> Check:
    passed, detail = probe()
    return _check(name, "pass" if passed else "fail", detail)


def _skip(name: str, detail: str) -> Check:
    return _check(name, "skip", detail)


def run(path: str | Path | None = None, profile_name: str | None = None) -> Report:
    """Run every local precondition that can be evaluated without a server."""
    resolved = config.config_path(path)
    checks: list[Check] = []
    try:
        loaded = config.load(resolved)
    except config.ConfigError as exc:
        checks.append(_check("config", "fail", str(exc)))
        checks.extend(
            (
                _skip("default-profile", "configuration did not load"),
                _skip("origins", "configuration did not load"),
                _skip("secret-backends", "configuration did not load"),
                _skip("callback-ports", "configuration did not load"),
                _skip("allowlists", "configuration did not load"),
            )
        )
        return Report(tuple(checks))

    checks.append(_check("config", "pass", f"{loaded.path}: parsed and all profiles validate"))

    if loaded.default_profile in loaded.profiles:
        checks.append(
            _check("default-profile", "pass", f"default profile is {loaded.default_profile!r}")
        )
    else:
        checks.append(
            _check(
                "default-profile",
                "fail",
                f"default profile {loaded.default_profile!r} is not configured",
            )
        )

    selected_profiles = list(loaded.profiles.values())
    if profile_name is not None:
        selected = loaded.profiles.get(profile_name)
        if selected is None:
            checks.append(
                _check("selected-profile", "fail", f"profile {profile_name!r} is not configured")
            )
            selected_profiles = []
        else:
            checks.append(_check("selected-profile", "pass", f"profile is {profile_name!r}"))
            selected_profiles = [selected]

    for profile in selected_profiles:
        checks.append(
            _check(f"origin:{profile.name}", "pass", f"{profile.origin}: well-formed origin")
        )

        if profile.secret_backend == "pass":
            checks.append(_run_probe(f"secret-backend:{profile.name}:pass", probe_pass))
            checks.append(
                _skip(
                    f"secret-backend:{profile.name}:libsecret",
                    "not selected by this profile",
                )
            )
        else:
            checks.append(
                _skip(
                    f"secret-backend:{profile.name}:pass",
                    "not selected by this profile",
                )
            )
            checks.append(_run_probe(f"secret-backend:{profile.name}:libsecret", probe_libsecret))

        if profile.auth == "oauth":
            assert profile.callback_port is not None
            checks.append(
                _run_probe(
                    f"callback-port:{profile.name}",
                    lambda port=profile.callback_port: probe_callback_port(port),
                )
            )
        else:
            checks.append(
                _skip(
                    f"callback-port:{profile.name}",
                    "not required by app-password authentication",
                )
            )

        for field, entries in (
            ("calendars", profile.calendars),
            ("files_roots", profile.files_roots),
        ):
            name = f"allowlist:{profile.name}:{field}"
            if not entries:
                checks.append(_check(name, "fail", "allowlist is empty"))
                continue
            try:
                for entry in entries:
                    profiles.canonicalize_href(entry)
            except ValueError as exc:
                checks.append(_check(name, "fail", f"entry cannot be canonicalized: {exc}"))
            else:
                noun = "entry" if len(entries) == 1 else "entries"
                checks.append(_check(name, "pass", f"{len(entries)} {noun} canonicalize"))

    return Report(tuple(checks))
