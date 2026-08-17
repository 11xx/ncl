"""Command dispatch for the small, machine-readable ``ncl`` CLI."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import checks, exits, guide, identity, login, profiles, secrets, session
from .config import ConfigError


def _add_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="emit JSON"
    )
    parser.add_argument(
        "--profile", default=argparse.SUPPRESS, help="select a configured profile"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ncl", description="Deterministic Nextcloud access")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--profile", help="select a configured profile")
    commands = parser.add_subparsers(dest="command")

    doctor = commands.add_parser("doctor", help="check local preconditions")
    _add_options(doctor)

    profile = commands.add_parser("profile", help="inspect configured profiles")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    list_profiles = profile_commands.add_parser("list", help="list profiles")
    _add_options(list_profiles)
    show_profile = profile_commands.add_parser("show", help="show one profile")
    _add_options(show_profile)

    login_command = commands.add_parser("login", help="obtain an application password")
    _add_options(login_command)
    login_command.add_argument(
        "--force",
        action="store_true",
        help="replace an existing stored credential after browser consent",
    )
    login_command.add_argument(
        "--timeout",
        type=float,
        default=1200,
        help="seconds to wait for browser consent (default: 1200)",
    )

    logout_command = commands.add_parser("logout", help="revoke and remove the credential")
    _add_options(logout_command)

    whoami = commands.add_parser("whoami", help="show the authenticated principal")
    _add_options(whoami)

    return parser


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _error(exc: Exception, json_output: bool) -> int:
    code = getattr(exc, "code", exits.ERROR)
    if json_output:
        _json({"error": str(exc), "code": code, "response": exits.RESPONSE.get(code)})
    else:
        print(f"ncl: {exc}", file=sys.stderr)
    return code


def _profile_data(profile) -> dict[str, Any]:
    return {"name": profile.name, **profile.as_dict()}


def _run_profile(args: argparse.Namespace) -> int:
    loaded = profiles.config.load()
    if args.profile_command == "list":
        values = {
            "default_profile": loaded.default_profile,
            "profiles": sorted(loaded.profiles),
        }
        if args.json:
            _json(values)
        else:
            print(f"default: {loaded.default_profile}")
            for name in sorted(loaded.profiles):
                print(name)
        return exits.OK

    selected = profiles.resolve(args.profile, loaded=loaded)
    value = {"profile": _profile_data(selected)}
    if args.json:
        _json(value)
    else:
        for key, item in value["profile"].items():
            print(f"{key}: {item}")
    return exits.OK


def _selected_profile(args: argparse.Namespace):
    loaded = profiles.config.load()
    return profiles.resolve(args.profile, loaded=loaded)


def _run_whoami(args: argparse.Namespace) -> int:
    profile = _selected_profile(args)
    result = identity.discover(profile)
    if args.json:
        _json(result.as_dict())
    else:
        for key, value in result.as_dict().items():
            print(f"{key}: {value}")
    return exits.OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    json_output = bool(args.json)

    try:
        if args.command is None:
            if json_output:
                _json({"guide": guide.render()})
            else:
                print(guide.render(), end="")
            return exits.OK
        if args.command == "doctor":
            report = checks.run(profile_name=args.profile)
            if json_output:
                _json(report.as_dict())
            else:
                for check in report.checks:
                    print(f"{check.status:4} {check.name}: {check.detail}")
            return report.exit_code
        if args.command == "profile":
            return _run_profile(args)
        if args.command == "login":
            profile = _selected_profile(args)
            output = print if not args.json else lambda message: print(message, file=sys.stderr)
            result = login.authenticate(
                profile,
                force=args.force,
                timeout=args.timeout,
                output=output,
            )
            if args.json:
                _json(result.as_dict())
            return exits.OK
        if args.command == "logout":
            profile = _selected_profile(args)
            login.logout(profile)
            if args.json:
                _json({"revoked": True, "local_deleted": True})
            else:
                print("Application password revoked and removed locally.")
            return exits.OK
        if args.command == "whoami":
            return _run_whoami(args)
        return exits.USAGE
    except ConfigError as exc:
        return _error(exc, json_output)
    except (
        login.LoginError,
        secrets.SecretError,
        session.SessionError,
        identity.IdentityError,
    ) as exc:
        return _error(exc, json_output)
    except (OSError, ValueError) as exc:
        return _error(exc, json_output)


if __name__ == "__main__":
    raise SystemExit(main())
