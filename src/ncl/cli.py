"""Command dispatch for the small, machine-readable ``ncl`` CLI."""

from __future__ import annotations

import argparse
import datetime
from typing import Any

from . import (
    caldav,
    checks,
    events,
    exits,
    guide,
    identity,
    login,
    mutate,
    plans,
    profiles,
    render,
    secrets,
    session,
)
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
        help=(
            "revoke an existing stored credential before browser consent; cancelling "
            "leaves the profile without a credential"
        ),
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

    cal = commands.add_parser("cal", help="calendars and events")
    cal_commands = cal.add_subparsers(dest="cal_command", required=True)
    cal_list = cal_commands.add_parser("list", help="discover calendars by href")
    _add_options(cal_list)

    cal_events = cal_commands.add_parser("events", help="list events in a window")
    _add_options(cal_events)
    cal_events.add_argument("calendar", help="calendar href, or an unambiguous display name")
    cal_events.add_argument("--from", dest="start", required=True, help="ISO 8601 start")
    cal_events.add_argument("--to", dest="end", required=True, help="ISO 8601 end")

    cal_show = cal_commands.add_parser("show", help="read one event by href")
    _add_options(cal_show)
    cal_show.add_argument("href", help="event resource href")

    cal_create = cal_commands.add_parser("create", help="plan a new event")
    _add_options(cal_create)
    cal_create.add_argument("calendar", help="calendar href, or an unambiguous display name")
    cal_create.add_argument("--summary", required=True)
    cal_create.add_argument("--from", dest="start", required=True, help="ISO 8601 start")
    cal_create.add_argument("--to", dest="end", required=True, help="ISO 8601 end")
    cal_create.add_argument("--description", default="")
    cal_create.add_argument("--location", default="")

    cal_update = cal_commands.add_parser("update", help="plan a change to one event")
    _add_options(cal_update)
    cal_update.add_argument("href", help="event resource href")
    cal_update.add_argument("--summary")
    cal_update.add_argument("--from", dest="start", help="ISO 8601 start")
    cal_update.add_argument("--to", dest="end", help="ISO 8601 end")
    cal_update.add_argument("--description")
    cal_update.add_argument("--location")

    cal_delete = cal_commands.add_parser("delete", help="plan a deletion")
    _add_options(cal_delete)
    cal_delete.add_argument("href", help="event resource href")

    plan = commands.add_parser("plan", help="inspect frozen mutations")
    plan_commands = plan.add_subparsers(dest="plan_command", required=True)
    plan_list = plan_commands.add_parser("list", help="list pending plans")
    _add_options(plan_list)
    plan_show = plan_commands.add_parser("show", help="show one plan")
    _add_options(plan_show)
    plan_show.add_argument("plan_id")
    plan_cancel = plan_commands.add_parser("cancel", help="discard a plan")
    _add_options(plan_cancel)
    plan_cancel.add_argument("plan_id")

    apply_command = commands.add_parser("apply", help="execute a frozen plan")
    _add_options(apply_command)
    apply_command.add_argument("plan_id")

    return parser


def _json(value: Any) -> None:
    render.emit(value)


def _error(exc: Exception, json_output: bool) -> int:
    code = getattr(exc, "code", exits.ERROR)
    message = getattr(exc, "message", None)
    if not isinstance(message, str) or not message:
        message = exits.RESPONSE.get(code, "The command failed.")
    if json_output:
        _json({"error": message, "code": code, "response": exits.RESPONSE.get(code)})
    else:
        render.emit_error(f"ncl: {message}")
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
            render.emit(f"default: {loaded.default_profile}")
            for name in sorted(loaded.profiles):
                render.emit(name)
        return exits.OK

    selected = profiles.resolve(args.profile, loaded=loaded)
    value = {"profile": _profile_data(selected)}
    if args.json:
        _json(value)
    else:
        for key, item in value["profile"].items():
            render.emit(f"{key}: {item}")
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
            render.emit(f"{key}: {value}")
    return exits.OK


def _moment(value: str, label: str) -> datetime.datetime:
    """Parse an ISO 8601 instant, requiring a timezone.

    A naive local time is ambiguous twice a year and nonexistent once, and the
    server stores what it is told. Refusing is better than picking one of the
    two instants the caller might have meant.
    """
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise events.EventError(f"{label} is not an ISO 8601 instant", exits.USAGE) from exc
    if parsed.tzinfo is None:
        raise events.EventError(
            f"{label} has no timezone offset; a local time is ambiguous across a DST "
            "transition, so state the offset explicitly",
            exits.USAGE,
        )
    return parsed


def _resolved_calendar(profile: Any, transport: session.Session, target: str) -> str:
    home = identity.discover(profile, session=transport).calendar_home
    calendars = caldav.list_calendars(profile, session=transport, calendar_home=home)
    return caldav.resolve(calendars, target).href


def _emit_plan(plan: Any, json_output: bool) -> int:
    if json_output:
        _json({"plan": plan.as_dict()})
    else:
        render.emit(f"planned {plan.action}: {plan.summary or plan.uid}")
        render.emit(f"  href    {plan.href}")
        if plan.start:
            render.emit(f"  when    {plan.start} .. {plan.end}")
        render.emit(f"  apply   ncl apply {plan.plan_id}")
        render.emit("  nothing has been changed on the server yet.")
    return exits.CONFIRMATION_REQUIRED


def _run_cal(args: argparse.Namespace) -> int:
    profile = _selected_profile(args)
    transport = session.Session(profile)

    if args.cal_command == "list":
        home = identity.discover(profile, session=transport).calendar_home
        calendars = caldav.list_calendars(profile, session=transport, calendar_home=home)
        if args.json:
            _json({"calendars": [calendar.as_dict() for calendar in calendars]})
        else:
            for calendar in calendars:
                access = "ro" if calendar.read_only else "rw"
                scope = "allowed" if calendar.in_scope else "not-allowlisted"
                render.emit(f"{access} {scope:15} {calendar.display_name}")
                render.emit(f"      {calendar.href}")
        return exits.OK

    if args.cal_command == "events":
        href = _resolved_calendar(profile, transport, args.calendar)
        found = events.query(
            profile,
            session=transport,
            calendar_href=href,
            start=_moment(args.start, "--from"),
            end=_moment(args.end, "--to"),
        )
        if args.json:
            _json({"events": [event.as_dict() for event in found]})
        else:
            for event in found:
                flag = "" if event.writable else f"  [{', '.join(event.unsupported)}]"
                render.emit(f"{event.start} .. {event.end}  {event.summary}{flag}")
                render.emit(f"      {event.href}")
        return exits.OK

    if args.cal_command == "show":
        reference, raw = events.fetch(profile, session=transport, href=args.href)
        if args.json:
            _json({"event": reference.as_dict(), "icalendar": raw.decode("utf-8", "replace")})
        else:
            for key, value in reference.as_dict().items():
                render.emit(f"{key}: {value}")
        return exits.OK

    if args.cal_command == "create":
        href = _resolved_calendar(profile, transport, args.calendar)
        plan = mutate.plan_create(
            profile,
            calendar_href=href,
            summary=args.summary,
            start=_moment(args.start, "--from"),
            end=_moment(args.end, "--to"),
            description=args.description,
            location=args.location,
        )
        return _emit_plan(plan, args.json)

    if args.cal_command == "update":
        changes: dict[str, Any] = {}
        if args.summary is not None:
            changes["SUMMARY"] = args.summary
        if args.start is not None:
            changes["DTSTART"] = _moment(args.start, "--from")
        if args.end is not None:
            changes["DTEND"] = _moment(args.end, "--to")
        if args.description is not None:
            changes["DESCRIPTION"] = args.description
        if args.location is not None:
            changes["LOCATION"] = args.location
        if not changes:
            raise events.EventError("no changes were requested", exits.USAGE)
        plan = mutate.plan_update(profile, session=transport, href=args.href, changes=changes)
        return _emit_plan(plan, args.json)

    if args.cal_command == "delete":
        plan = mutate.plan_delete(profile, session=transport, href=args.href)
        return _emit_plan(plan, args.json)

    return exits.USAGE


def _run_plan(args: argparse.Namespace) -> int:
    if args.plan_command == "list":
        pending = plans.listing()
        if args.json:
            _json({"plans": [plan.as_dict() for plan in pending]})
        else:
            for plan in pending:
                render.emit(f"{plan.plan_id}  {plan.action:6} {plan.summary or plan.uid}")
        return exits.OK
    if args.plan_command == "show":
        plan = plans.read(args.plan_id)
        if args.json:
            _json({"plan": plan.as_dict()})
        else:
            for key, value in plan.as_dict().items():
                render.emit(f"{key}: {value}")
        return exits.OK
    if args.plan_command == "cancel":
        plans.consume(args.plan_id)
        render.emit(f"cancelled {args.plan_id}")
        return exits.OK
    return exits.USAGE


def _run_apply(args: argparse.Namespace) -> int:
    profile = _selected_profile(args)
    plan = plans.read(args.plan_id)
    with plans.claim(plan.plan_id):
        result = mutate.apply(profile, session=session.Session(profile), plan=plan)
    if args.json:
        _json(result)
    else:
        for key, value in result.items():
            render.emit(f"{key}: {value}")
    return exits.OK


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    json_output = bool(args.json)

    try:
        if args.command is None:
            if json_output:
                _json({"guide": guide.render()})
            else:
                render.emit(guide.render(), end="")
            return exits.OK
        if args.command == "doctor":
            report = checks.run(profile_name=args.profile)
            if json_output:
                _json(report.as_dict())
            else:
                for check in report.checks:
                    render.emit(f"{check.status:4} {check.name}: {check.detail}")
            return report.exit_code
        if args.command == "profile":
            return _run_profile(args)
        if args.command == "login":
            profile = _selected_profile(args)
            output = render.emit if not args.json else render.emit_error
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
                render.emit("Application password revoked and removed locally.")
            return exits.OK
        if args.command == "whoami":
            return _run_whoami(args)
        if args.command == "cal":
            return _run_cal(args)
        if args.command == "plan":
            return _run_plan(args)
        if args.command == "apply":
            return _run_apply(args)
        return exits.USAGE
    except ConfigError as exc:
        return _error(exc, json_output)
    except (
        login.LoginError,
        secrets.SecretError,
        session.SessionError,
        identity.IdentityError,
        caldav.CalendarError,
        events.EventError,
        plans.PlanError,
    ) as exc:
        return _error(exc, json_output)
    except (OSError, ValueError) as exc:
        return _error(exc, json_output)


def main(argv: list[str] | None = None) -> int:
    with render.redacted_standard_streams():
        return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
