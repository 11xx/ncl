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


class _Formatter(argparse.HelpFormatter):
    """Two-column help with room for a sentence, not a fragment.

    argparse's default wraps descriptions at whatever the flag names leave
    over, which for a command set of any size is a ragged column two words
    wide. A fixed, generous split reads like the help of the other tools in
    this family.
    """

    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=32, width=96)

    def _format_action(self, action: argparse.Action) -> str:
        # The subcommand group prints its own metavar above the commands it
        # holds, which says nothing the commands do not. Render the children
        # and drop the header.
        if isinstance(action, argparse._SubParsersAction):
            return "".join(
                super(_Formatter, self)._format_action(child)
                for child in action._get_subactions()
            )
        return super()._format_action(action)


class _Parser(argparse.ArgumentParser):
    """An argparse parser that renders the way the sibling tools do.

    The description leads, the usage line follows it, and the sections are
    named for what they hold rather than for argparse's internal categories.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", _Formatter)
        super().__init__(*args, **kwargs)
        self._positionals.title = "Commands"
        self._optionals.title = "Options"
        for action in self._actions:
            if isinstance(action, argparse._HelpAction):
                action.help = "Print help"

    def format_help(self) -> str:
        formatter = self._get_formatter()
        if self.description:
            formatter.add_text(self.description)
        formatter.add_usage(
            self.usage, self._actions, self._mutually_exclusive_groups, prefix="Usage: "
        )
        for group in self._action_groups:
            if not group._group_actions:
                continue
            formatter.start_section(group.title)
            formatter.add_text(group.description)
            formatter.add_arguments(group._group_actions)
            formatter.end_section()
        formatter.add_text(self.epilog)
        return formatter.format_help()


def _add_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON"
    )
    parser.add_argument(
        "--profile", default=argparse.SUPPRESS, help="Select a configured profile"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="ncl",
        description=(
            "ncl — programmatic Nextcloud access for agents: calendars today, other "
            "Nextcloud apps as they are added. Run `ncl` with no arguments for the "
            "workflow guide."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument("--profile", help="Select a configured profile")
    commands = parser.add_subparsers(
        dest="command", metavar="<command>", parser_class=_Parser
    )

    doctor = commands.add_parser("doctor", help="Check every local precondition")
    _add_options(doctor)

    profile = commands.add_parser("profile", help="Inspect configured profiles")
    profile_commands = profile.add_subparsers(
        dest="profile_command", required=True, metavar="<command>", parser_class=_Parser
    )
    list_profiles = profile_commands.add_parser("list", help="List configured profiles")
    _add_options(list_profiles)
    show_profile = profile_commands.add_parser("show", help="Show one profile")
    _add_options(show_profile)

    login_command = commands.add_parser("login", help="Obtain a credential through browser consent")
    _add_options(login_command)
    login_command.add_argument(
        "--force",
        action="store_true",
        help=(
            "Revoke an existing stored credential before browser consent; "
            "cancelling then leaves the profile without a credential"
        ),
    )
    login_command.add_argument(
        "--timeout",
        type=float,
        default=1200,
        help="Seconds to wait for browser consent",
    )

    logout_command = commands.add_parser("logout", help="Revoke the credential, then forget it")
    _add_options(logout_command)

    whoami = commands.add_parser("whoami", help="Show which account the credential reaches")
    _add_options(whoami)

    cal = commands.add_parser("cal",
        help="Calendars and events",
        description=(
            "Calendars and events. A calendar is addressed by href, never by display name."
        ),)
    cal_commands = cal.add_subparsers(
        dest="cal_command", required=True, metavar="<command>", parser_class=_Parser
    )
    cal_list = cal_commands.add_parser("list", help="Discover calendars, with href and scope")
    _add_options(cal_list)

    cal_events = cal_commands.add_parser("events", help="List events overlapping a required window")
    _add_options(cal_events)
    cal_events.add_argument("calendar", help="Calendar href, or an unambiguous display name")
    cal_events.add_argument(
        "--from", dest="start", required=True, help="Start instant, ISO 8601 with an offset"
    )
    cal_events.add_argument(
        "--to", dest="end", required=True, help="End instant, ISO 8601 with an offset"
    )

    cal_show = cal_commands.add_parser("show", help="Read one event by href")
    _add_options(cal_show)
    cal_show.add_argument("href", help="Event resource href")

    cal_create = cal_commands.add_parser("create", help="Plan a new event; changes nothing yet")
    _add_options(cal_create)
    cal_create.add_argument("calendar", help="Calendar href, or an unambiguous display name")
    cal_create.add_argument("--summary", required=True)
    cal_create.add_argument(
        "--from", dest="start", required=True, help="Start instant, ISO 8601 with an offset"
    )
    cal_create.add_argument(
        "--to", dest="end", required=True, help="End instant, ISO 8601 with an offset"
    )
    cal_create.add_argument("--description", default="")
    cal_create.add_argument("--location", default="")

    cal_update = cal_commands.add_parser(
        "update", help="Plan a change to one event; changes nothing yet"
    )
    _add_options(cal_update)
    cal_update.add_argument("href", help="Event resource href")
    cal_update.add_argument("--summary")
    cal_update.add_argument("--from", dest="start", help="Start instant, ISO 8601 with an offset")
    cal_update.add_argument("--to", dest="end", help="End instant, ISO 8601 with an offset")
    cal_update.add_argument("--description")
    cal_update.add_argument("--location")

    cal_delete = cal_commands.add_parser("delete", help="Plan a deletion; changes nothing yet")
    _add_options(cal_delete)
    cal_delete.add_argument("href", help="Event resource href")

    plan = commands.add_parser("plan",
        help="Inspect frozen mutations",
        description=(
            "Pending mutations. A plan changes nothing until `ncl apply` runs it."
        ),)
    plan_commands = plan.add_subparsers(
        dest="plan_command", required=True, metavar="<command>", parser_class=_Parser
    )
    plan_list = plan_commands.add_parser("list", help="List pending plans")
    _add_options(plan_list)
    plan_show = plan_commands.add_parser("show", help="Show one plan in full")
    _add_options(plan_show)
    plan_show.add_argument("plan_id")
    plan_cancel = plan_commands.add_parser("cancel", help="Discard a plan without applying it")
    _add_options(plan_cancel)
    plan_cancel.add_argument("plan_id")

    apply_command = commands.add_parser("apply", help="Execute a frozen plan, once")
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
