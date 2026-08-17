"""Command dispatch for the small, machine-readable ``ncl`` CLI."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import checks, exits, guide, profiles
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
        return exits.USAGE
    except ConfigError as exc:
        return _error(exc, json_output)
    except (OSError, ValueError) as exc:
        return _error(exc, json_output)


if __name__ == "__main__":
    raise SystemExit(main())
