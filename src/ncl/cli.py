"""Command dispatch for the small, machine-readable ``ncl`` CLI."""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path
from typing import Any

from . import (
    appointments,
    caldav,
    checks,
    events,
    exits,
    files,
    guide,
    identity,
    login,
    mutate,
    plans,
    profiles,
    recurrence,
    render,
    runs,
    secrets,
    session,
    todos,
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


def _normalize_alarm_values(argv: list[str]) -> list[str]:
    """Keep duration values beginning with ``-`` attached to ``--alarm``."""
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if (
            token == "--alarm"
            and index + 1 < len(argv)
            and argv[index + 1].startswith("-P")
        ):
            normalized.append(f"--alarm={argv[index + 1]}")
            index += 2
            continue
        normalized.append(token)
        index += 1
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="ncl",
        description=(
            "ncl — programmatic Nextcloud access for agents over CalDAV and WebDAV. "
            "Run `ncl` with no arguments for the workflow guide."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument("--profile", help="Select a configured profile")
    commands = parser.add_subparsers(
        dest="command", metavar="<command>", parser_class=_Parser
    )

    doctor = commands.add_parser("doctor", help="Check every local precondition")
    _add_options(doctor)
    doctor.add_argument(
        "--probe-secret-store",
        action="store_true",
        help="Also store and remove one value, proving the backend can keep a credential. "
        "This writes to the store: a Git-backed pass records two commits",
    )

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
        ),
    )
    cal_commands = cal.add_subparsers(
        dest="cal_command", required=True, metavar="<command>", parser_class=_Parser
    )
    cal_list = cal_commands.add_parser("list", help="Discover calendars, with href and scope")
    _add_options(cal_list)

    cal_events = cal_commands.add_parser(
        "events",
        help="List events with structured URL/status over a required window",
    )
    _add_options(cal_events)
    cal_events.add_argument("calendar", help="Calendar href, or an unambiguous display name")
    cal_events.add_argument(
        "--from", dest="start", required=True, help="Start instant, ISO 8601 with an offset"
    )
    cal_events.add_argument(
        "--to", dest="end", required=True, help="End instant, ISO 8601 with an offset"
    )

    cal_occurrences = cal_commands.add_parser(
        "occurrences",
        help="Discover recurrence instances over a required window",
    )
    _add_options(cal_occurrences)
    cal_occurrences.add_argument("calendar", help="Calendar href, or an unambiguous display name")
    cal_occurrences.add_argument(
        "--from", dest="start", required=True, help="Start instant, ISO 8601 with an offset"
    )
    cal_occurrences.add_argument(
        "--to", dest="end", required=True, help="End instant, ISO 8601 with an offset"
    )

    cal_show = cal_commands.add_parser(
        "show", help="Read one event by href, including its structured URL/status"
    )
    _add_options(cal_show)
    cal_show.add_argument("href", help="Event resource href")

    cal_create = cal_commands.add_parser("create", help="Plan a new event; changes nothing yet")
    _add_options(cal_create)
    cal_create.add_argument("calendar", help="Calendar href, or an unambiguous display name")
    cal_create.add_argument("--summary", required=True)
    cal_create.add_argument(
        "--from",
        dest="start",
        required=True,
        help="Start instant with an offset, or YYYY-MM-DD for an all-day event",
    )
    cal_create.add_argument(
        "--to",
        dest="end",
        required=True,
        help="End instant with an offset, or the exclusive YYYY-MM-DD end of an all-day event",
    )
    cal_create.add_argument("--description", default="")
    cal_create.add_argument("--location", default="")
    cal_create.add_argument(
        "--priority", type=int, help="1 is highest, 9 is lowest (RFC 5545 order)"
    )
    cal_create.add_argument(
        "--category", action="append", default=[], dest="categories",
        help="Tag the event; repeatable",
    )
    cal_create.add_argument(
        "--status", choices=["confirmed", "tentative", "cancelled"],
        help="How settled the event is",
    )
    cal_create.add_argument(
        "--free", dest="busy", action="store_false", default=None,
        help="Leave the slot bookable instead of consuming free/busy time",
    )
    cal_create.add_argument(
        "--busy", dest="busy", action="store_true",
        help="Consume free/busy time (the default a client assumes)",
    )

    cal_create.add_argument("--url", default="", help="A canonical link for the event")
    cal_create.add_argument(
        "--class", dest="classification",
        choices=["public", "private", "confidential"], help="Visibility to others",
    )
    cal_create.add_argument("--color", default="", help="CSS3 colour name (RFC 7986)")
    cal_create.add_argument(
        "--related-to", action="append", default=[], dest="related_to",
        help="UID this event belongs with; repeatable",
    )
    cal_create.add_argument(
        "--alarm", action="append", default=[], dest="alarms",
        help="Reminder offset, e.g. -PT15M or -P1D; repeatable",
    )
    cal_create.add_argument(
        "--portable-description", action="store_true",
        help="Project structured fields into a deterministic DESCRIPTION block",
    )
    cal_update = cal_commands.add_parser(
        "update", help="Plan a change to one event; changes nothing yet"
    )
    _add_options(cal_update)
    cal_update.add_argument("href", help="Event resource href")
    cal_update.add_argument(
        "--target",
        choices=["resource", "series", "occurrence", "this-and-future"],
        required=True,
        help="Explicitly select the ordinary resource, series, occurrence, or future split",
    )
    cal_update.add_argument(
        "--recurrence-id",
        help="Exact occurrence identity from `cal occurrences`",
    )
    cal_update.add_argument("--summary")
    cal_update.add_argument(
        "--from",
        dest="start",
        help="Start instant with an offset, or YYYY-MM-DD for an all-day event",
    )
    cal_update.add_argument(
        "--to",
        dest="end",
        help="End instant with an offset, or the exclusive YYYY-MM-DD end of an all-day event",
    )
    cal_update.add_argument("--description")
    cal_update.add_argument("--location")
    cal_update.add_argument("--priority", type=int, help="1 is highest, 9 is lowest")
    cal_update.add_argument(
        "--category", action="append", dest="categories", help="Replace tags; repeatable"
    )
    cal_update.add_argument("--status", choices=["confirmed", "tentative", "cancelled"])
    cal_update.add_argument("--free", dest="busy", action="store_false", default=None)
    cal_update.add_argument("--busy", dest="busy", action="store_true")
    cal_update.add_argument("--url", help="A canonical link for the event")
    cal_update.add_argument(
        "--class", dest="classification", choices=["public", "private", "confidential"]
    )
    cal_update.add_argument("--color", help="CSS3 colour name (RFC 7986)")
    cal_update.add_argument(
        "--related-to", action="append", dest="related_to",
        help="Replace the UIDs this event belongs with; repeatable",
    )
    alarm_options = cal_update.add_mutually_exclusive_group()
    alarm_options.add_argument(
        "--alarm", action="append", dest="alarms",
        help=(
            "Replace reminders with these offsets; repeatable (omission preserves), "
            "e.g. --alarm=-PT15M"
        ),
    )
    alarm_options.add_argument(
        "--clear-alarms", action="store_true",
        help="Remove every reminder; mutually exclusive with --alarm",
    )
    cal_update.add_argument(
        "--portable-description", action="store_true",
        help="Regenerate the deterministic DESCRIPTION compatibility block",
    )

    cal_delete = cal_commands.add_parser("delete", help="Plan a deletion; changes nothing yet")
    _add_options(cal_delete)
    cal_delete.add_argument("href", help="Event resource href")
    cal_delete.add_argument(
        "--target",
        choices=["resource", "series", "occurrence", "this-and-future"],
        required=True,
        help="Explicitly select the ordinary resource, series, occurrence, or future split",
    )
    cal_delete.add_argument(
        "--recurrence-id",
        help="Exact occurrence identity from `cal occurrences`",
    )

    cal_appointment = cal_commands.add_parser(
        "appointment",
        help="Plan a linked appointment, travel, and preparation bundle",
        description=(
            "Appointment bundles create or reschedule exactly three timed VEVENTs. "
            "All durations are RFC 5545 day/time values; route URLs are stored "
            "verbatim and never fetched."
        ),
    )
    appointment_commands = cal_appointment.add_subparsers(
        dest="appointment_command", required=True, metavar="<command>", parser_class=_Parser
    )

    appointment_create = appointment_commands.add_parser(
        "create", help="Plan a three-event appointment bundle; changes nothing yet"
    )
    _add_options(appointment_create)
    appointment_create.add_argument(
        "calendar", help="Calendar href, or an unambiguous display name"
    )
    appointment_create.add_argument("--summary", required=True, help="Appointment summary")
    appointment_create.add_argument(
        "--from", "--start", dest="start", required=True,
        help="Appointment start, ISO 8601 with an offset and whole seconds",
    )
    appointment_create.add_argument(
        "--to", "--end", dest="end", required=True,
        help="Appointment end, ISO 8601 with an offset and whole seconds",
    )
    appointment_create.add_argument("--origin", required=True, help="Exact origin text")
    appointment_create.add_argument(
        "--destination", required=True, help="Exact destination address for LOCATION"
    )
    appointment_create.add_argument("--mode", required=True, help="Stated travel mode")
    appointment_create.add_argument(
        "--route-estimate", required=True, help="Positive RFC 5545 duration"
    )
    appointment_create.add_argument(
        "--on-site-buffer", required=True, help="Nonnegative RFC 5545 duration"
    )
    appointment_create.add_argument(
        "--stop-wait-margin", required=True, help="Nonnegative RFC 5545 duration"
    )
    appointment_create.add_argument(
        "--preparation-duration", required=True, help="Positive RFC 5545 duration"
    )
    appointment_create.add_argument(
        "--route-url", default=None, help="Optional route URL; stored verbatim, never fetched"
    )
    appointment_create.add_argument(
        "--description", default="", help="Optional appointment prose"
    )

    appointment_update = appointment_commands.add_parser(
        "update", help="Plan a reschedule of exact appointment bundle hrefs"
    )
    _add_options(appointment_update)
    appointment_update.add_argument("appointment_href", help="Exact appointment event href")
    appointment_update.add_argument("travel_href", help="Exact travel event href")
    appointment_update.add_argument("preparation_href", help="Exact preparation event href")
    appointment_update.add_argument("--summary", help="Replacement appointment summary")
    appointment_update.add_argument(
        "--from", "--start", dest="start", required=True,
        help="New appointment start, ISO 8601 with an offset and whole seconds",
    )
    appointment_update.add_argument(
        "--to", "--end", dest="end", required=True,
        help="New appointment end, ISO 8601 with an offset and whole seconds",
    )
    appointment_update.add_argument("--origin", required=True, help="Exact origin text")
    appointment_update.add_argument(
        "--destination", required=True, help="Exact destination address for LOCATION"
    )
    appointment_update.add_argument("--mode", required=True, help="Stated travel mode")
    appointment_update.add_argument(
        "--route-estimate", required=True, help="Positive RFC 5545 duration"
    )
    appointment_update.add_argument(
        "--on-site-buffer", required=True, help="Nonnegative RFC 5545 duration"
    )
    appointment_update.add_argument(
        "--stop-wait-margin", required=True, help="Nonnegative RFC 5545 duration"
    )
    appointment_update.add_argument(
        "--preparation-duration", required=True, help="Positive RFC 5545 duration"
    )
    route_options = appointment_update.add_mutually_exclusive_group()
    route_options.add_argument(
        "--route-url", help="Replace the route URL verbatim; omission preserves it; never fetched"
    )
    route_options.add_argument(
        "--clear-route-url", action="store_true", help="Remove the route URL"
    )
    appointment_update.add_argument(
        "--description", help="Replacement appointment prose; omission preserves it"
    )

    task = commands.add_parser(
        "task",
        help="Tasks in CalDAV collections",
        description=(
            "Tasks stored as VTODO components. A collection must advertise VTODO "
            "before it can be selected."
        ),
    )
    task_commands = task.add_subparsers(
        dest="task_command", required=True, metavar="<command>", parser_class=_Parser
    )
    task_list = task_commands.add_parser(
        "list",
        help="List valid tasks with one CalDAV REPORT",
        description="List valid VTODO resources with one depth-one CalDAV REPORT.",
    )
    _add_options(task_list)
    task_list.add_argument("calendar", help="Calendar href, or an unambiguous display name")
    task_list.add_argument(
        "--status",
        action="append",
        choices=[status.lower() for status in todos.TODO_STATUSES],
        default=[],
        help="Keep tasks with this status; repeatable",
    )

    task_show = task_commands.add_parser(
        "show", help="Read exactly one task href without collection discovery"
    )
    _add_options(task_show)
    task_show.add_argument("href", help="Task resource href")

    task_create = task_commands.add_parser(
        "create", help="Plan a new VTODO in a collection that advertises VTODO"
    )
    _add_options(task_create)
    task_create.add_argument("calendar", help="Calendar href, or an unambiguous display name")
    task_create.add_argument("--summary", required=True)
    task_create.add_argument("--description", default="")
    task_create.add_argument("--start", help="Start date or ISO 8601 instant with an offset")
    task_create.add_argument("--due", help="Due date or ISO 8601 instant with an offset")
    task_create.add_argument("--priority", type=int, help="0 is unspecified; 1 is highest")
    task_create.add_argument(
        "--status", choices=[status.lower() for status in todos.TODO_STATUSES]
    )
    task_create.add_argument(
        "--percent", "--percent-complete", dest="percent_complete", type=int
    )
    task_create.add_argument("--parent", dest="parent_uid", help="Parent task UID")

    task_update = task_commands.add_parser(
        "update", help="Plan a task change while preserving unmodeled iCalendar data"
    )
    _add_options(task_update)
    task_update.add_argument("href", help="Task resource href")
    task_update.add_argument("--summary")
    task_update.add_argument("--description")
    task_update.add_argument("--start", help="Start date or ISO 8601 instant with an offset")
    task_update.add_argument("--due", help="Due date or ISO 8601 instant with an offset")
    task_update.add_argument("--priority", type=int, help="0 is unspecified; 1 is highest")
    task_update.add_argument(
        "--status", choices=[status.lower() for status in todos.TODO_STATUSES]
    )
    task_update.add_argument(
        "--percent", "--percent-complete", dest="percent_complete", type=int
    )
    task_update.add_argument("--parent", dest="parent_uid", help="Parent task UID")

    task_complete = task_commands.add_parser(
        "complete", help="Plan one conditional write for atomic task completion"
    )
    _add_options(task_complete)
    task_complete.add_argument("href", help="Task resource href")
    task_complete.add_argument(
        "--completed", help="Completion instant, ISO 8601 with an explicit offset"
    )

    task_delete = task_commands.add_parser(
        "delete",
        help="Plan deletion and verify the exact task href is absent",
        description="Plan deletion; apply verifies that the exact task href is absent (404).",
    )
    _add_options(task_delete)
    task_delete.add_argument("href", help="Task resource href")

    task_run = task_commands.add_parser(
        "run",
        help="Ordered checkpoint runs stored as standard VTODO graphs",
        description=(
            "Checkpoint runs use one VTODO manifest and standard FIRST, PARENT, "
            "NEXT, GAP, and REFID properties."
        ),
    )
    run_commands = task_run.add_subparsers(
        dest="run_command", required=True, metavar="<command>", parser_class=_Parser
    )

    run_create = run_commands.add_parser(
        "create", help="Plan a new ordered run; changes nothing yet"
    )
    _add_options(run_create)
    run_create.add_argument("calendar", help="Calendar href, or an unambiguous display name")
    run_create.add_argument("--summary", required=True)
    run_create.add_argument(
        "--step", action="append", required=True, help="Checkpoint summary; repeatable"
    )
    run_create.add_argument(
        "--gap",
        action="append",
        help="Nonnegative lag between adjacent checkpoints, or unknown; repeatable",
    )

    run_list = run_commands.add_parser(
        "list", help="List validated runs with one CalDAV REPORT"
    )
    _add_options(run_list)
    run_list.add_argument("calendar", help="Calendar href, or an unambiguous display name")

    run_show = run_commands.add_parser(
        "show", help="Read one run root and all members with one CalDAV REPORT"
    )
    _add_options(run_show)
    run_show.add_argument("root_href", help="Run root VTODO href")

    run_add = run_commands.add_parser(
        "add", help="Plan one checkpoint insertion; changes nothing yet"
    )
    _add_options(run_add)
    run_add.add_argument("root_href", help="Run root VTODO href")
    run_add.add_argument("--summary", required=True)
    insertion = run_add.add_mutually_exclusive_group()
    insertion.add_argument("--before", help="Insert before this exact checkpoint href")
    insertion.add_argument("--after", help="Insert after this exact checkpoint href")
    run_add.add_argument("--gap-before", help="Lag from predecessor, or unknown")
    run_add.add_argument("--gap-after", help="Lag to successor, or unknown")

    run_reorder = run_commands.add_parser(
        "reorder", help="Plan a complete checkpoint order; changes nothing yet"
    )
    _add_options(run_reorder)
    run_reorder.add_argument("root_href", help="Run root VTODO href")
    run_reorder.add_argument(
        "--step", action="append", required=True, help="Checkpoint href in final order; repeatable"
    )
    run_reorder.add_argument(
        "--gap",
        action="append",
        help="Nonnegative lag between adjacent checkpoints, or unknown; repeatable",
    )

    run_edit = run_commands.add_parser(
        "edit", help="Plan a modeled content/time/priority edit; changes nothing yet"
    )
    _add_options(run_edit)
    run_edit.add_argument("href", help="Run root or checkpoint VTODO href")
    run_edit.add_argument("--summary")
    run_edit.add_argument("--description")
    run_edit.add_argument("--start", help="Start date or ISO 8601 instant with an offset")
    run_edit.add_argument("--due", help="Due date or ISO 8601 instant with an offset")
    run_edit.add_argument("--priority", type=int, help="0 is unspecified; 1 is highest")

    run_done = run_commands.add_parser(
        "done", help="Plan one conditional completion transition"
    )
    _add_options(run_done)
    run_done.add_argument("href", help="Checkpoint VTODO href")
    run_done.add_argument("--at", help="Completion instant, ISO 8601 with an explicit offset")

    run_skip = run_commands.add_parser(
        "skip", help="Plan one conditional cancellation transition"
    )
    _add_options(run_skip)
    run_skip.add_argument("href", help="Checkpoint VTODO href")

    run_not_yet = run_commands.add_parser(
        "not-yet", help="Plan a checkpoint reset to NEEDS-ACTION"
    )
    _add_options(run_not_yet)
    run_not_yet.add_argument("href", help="Checkpoint VTODO href")

    file_commands = commands.add_parser(
        "files",
        help="Files over WebDAV",
        description=(
            "Scoped Nextcloud files. Resources are addressed by href under a configured "
            "files root."
        ),
    )
    file_subcommands = file_commands.add_subparsers(
        dest="files_command", required=True, metavar="<command>", parser_class=_Parser
    )
    files_list = file_subcommands.add_parser(
        "list", help="List one collection without recursion"
    )
    _add_options(files_list)
    files_list.add_argument("href", help="Collection href under an allowlisted files root")

    files_stat = file_subcommands.add_parser("stat", help="Read one resource's metadata")
    _add_options(files_stat)
    files_stat.add_argument("href", help="File or collection href")

    files_read = file_subcommands.add_parser("read", help="Read one file")
    _add_options(files_read)
    files_read.add_argument("href", help="File href")
    files_read.add_argument(
        "--output", help="Write exact bytes to this local path instead of standard output"
    )
    files_read.add_argument(
        "--force", action="store_true", help="Replace an existing --output path"
    )

    files_write = file_subcommands.add_parser(
        "write", help="Plan a file creation or replacement; changes nothing yet"
    )
    _add_options(files_write)
    files_write.add_argument("href", help="Destination file href")
    files_write.add_argument(
        "--from", dest="source", required=True, help="Local file whose exact bytes to freeze"
    )
    files_write.add_argument(
        "--content-type", default="application/octet-stream", help="Stored media type"
    )

    files_delete = file_subcommands.add_parser(
        "delete", help="Plan deletion of one file; changes nothing yet"
    )
    _add_options(files_delete)
    files_delete.add_argument("href", help="File href; collection deletion is refused")

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
    plan_reconcile = plan_commands.add_parser(
        "reconcile", help="Read the first uncertain step and classify its effect"
    )
    _add_options(plan_reconcile)
    plan_reconcile.add_argument("plan_id")
    plan_cancel = plan_commands.add_parser(
        "cancel", help="Discard a plan; --json emits a cancellation object"
    )
    _add_options(plan_cancel)
    plan_cancel.add_argument("plan_id")

    apply_command = commands.add_parser("apply", help="Execute or resume a frozen plan")
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


def _boundary_moment(value: str, label: str) -> datetime.datetime | datetime.date:
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        try:
            return datetime.date.fromisoformat(value)
        except ValueError as exc:
            raise events.EventError(f"{label} is not a valid all-day date", exits.USAGE) from exc
    return _moment(value, label)


def _todo_moment(value: str, label: str) -> datetime.datetime | datetime.date:
    """Parse a task date or an offset-bearing instant."""
    if "T" not in value and " " not in value:
        try:
            return datetime.date.fromisoformat(value)
        except ValueError as exc:
            raise todos.TodoError(
                f"{label} is not an ISO 8601 date or instant", exits.USAGE
            ) from exc
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.date.fromisoformat(value)
        except ValueError as exc:
            raise todos.TodoError(
                f"{label} is not an ISO 8601 date or instant", exits.USAGE
            ) from exc
    if parsed.tzinfo is None:
        raise todos.TodoError(
            f"{label} has no timezone offset; state the offset explicitly", exits.USAGE
        )
    return parsed


def _todo_instant(value: str, label: str) -> datetime.datetime:
    parsed = _todo_moment(value, label)
    if not isinstance(parsed, datetime.datetime):
        raise todos.TodoError(
            f"{label} must be an ISO 8601 instant with an explicit offset", exits.USAGE
        )
    return parsed


def _resolved_calendar(profile: Any, transport: session.Session, target: str) -> str:
    home = identity.discover(profile, session=transport).calendar_home
    calendars = caldav.list_calendars(profile, session=transport, calendar_home=home)
    return caldav.resolve(calendars, target).href


def _resolved_component(
    profile: Any,
    transport: session.Session,
    target: str,
    component: str,
) -> caldav.Calendar:
    home = identity.discover(profile, session=transport).calendar_home
    calendars = caldav.list_calendars(profile, session=transport, calendar_home=home)
    return caldav.require_component(caldav.resolve(calendars, target), component)


def _emit_plan(plan: Any, json_output: bool) -> int:
    if json_output:
        _json({"plan": plan.as_dict()})
    else:
        render.emit(f"planned {plan.summary or 'bundle'} ({len(plan.steps)} step(s))")
        for index, (step, progress) in enumerate(
            zip(plan.steps, plan.progress, strict=True), start=1
        ):
            render.emit(
                f"  {index}. {progress.state:9} {step.action:14} "
                f"{step.summary or step.href} ({len(plans.payload_bytes(step))} bytes)"
            )
            render.emit(f"       href    {step.href}")
            if step.details.get("all_day"):
                render.emit(
                    f"       all-day {step.details.get('start', '')} .. "
                    f"{step.details.get('end', '')} (end date is exclusive)"
                )
            if step.details.get("warning"):
                render.emit(f"       warning {step.details['warning']}")
        render.emit(f"  apply   ncl apply {plan.plan_id}")
        render.emit("  nothing has been changed on the server yet.")
    return exits.CONFIRMATION_REQUIRED


def _emit_run_graph(graph: runs.RunGraph, *, json_output: bool, many: bool = False) -> int:
    value = graph.as_dict()
    if json_output:
        _json({"runs": [value]} if many else {"run": value})
        return exits.OK
    render.emit(f"run: {graph.root.reference.summary}")
    render.emit(f"root: {graph.root.href}")
    render.emit(f"current: {graph.current_uid or 'none'}")
    render.emit(
        f"position: {graph.current_index + 1 if graph.current_index is not None else 'none'}"
    )
    for index, (step, gap) in enumerate(
        zip(graph.steps, (*graph.gaps, None), strict=True), start=1
    ):
        gap_text = "" if gap is None and index == len(graph.steps) else (gap or "unknown")
        render.emit(f"  {index}. {step.status:12} {step.reference.summary}")
        render.emit(f"      {step.href}")
        if gap_text:
            render.emit(f"      gap-to-next: {gap_text}")
    return exits.OK


def _emit_run_plan(value: Any, *, json_output: bool) -> int:
    if isinstance(value, plans.Plan):
        code = _emit_plan(value, json_output)
        if not json_output and any(
            step.details.get("out_of_order") is True for step in value.steps
        ):
            render.emit(
                "  note: this transition is out of order; current remains the "
                "earliest nonterminal checkpoint."
            )
        return code
    if json_output:
        _json(value.as_dict())
    else:
        result = value.as_dict()
        render.emit("no changes planned")
        render.emit(result.get("reason", "the requested run state is already current"))
        if "run" in result:
            run = result["run"]
            render.emit(f"current: {run.get('current_uid') or 'none'}")
    return exits.OK


def _dispatchers() -> dict[str, plans.Dispatcher]:
    return {
        "cal.": plans.Dispatcher(mutate.validate_step, mutate.execute, mutate.reconcile),
        "task.": plans.Dispatcher(todos.validate_step, todos.execute, todos.reconcile),
        "run.": plans.Dispatcher(
            runs.validate_step,
            runs.execute,
            runs.reconcile,
            runs.validate_bundle,
        ),
        "files.": plans.Dispatcher(files.validate_step, files.execute, files.reconcile),
    }


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
                status = "  [CANCELLED]" if event.status.upper() == "CANCELLED" else ""
                flag = status if event.writable else status + f"  [{', '.join(event.unsupported)}]"
                kind = "  [all-day]" if event.all_day else ""
                render.emit(f"{event.start} .. {event.end}  {event.summary}{kind}{flag}")
                render.emit(f"      {event.href}")
        return exits.OK

    if args.cal_command == "occurrences":
        href = _resolved_calendar(profile, transport, args.calendar)
        found = recurrence.occurrences(
            profile,
            session=transport,
            calendar_href=href,
            start=_moment(args.start, "--from"),
            end=_moment(args.end, "--to"),
        )
        if args.json:
            _json({"occurrences": [item.as_dict() for item in found]})
        else:
            for item in found:
                cancelled = " [CANCELLED]" if item.cancelled else ""
                render.emit(
                    f"{item.effective_start} .. {item.effective_end}  "
                    f"{item.recurrence_id}{cancelled}"
                )
                render.emit(f"      {item.href}")
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
            start=_boundary_moment(args.start, "--from"),
            end=_boundary_moment(args.end, "--to"),
            description=args.description,
            location=args.location,
            priority=args.priority,
            categories=tuple(args.categories),
            status=args.status or "",
            busy=args.busy,
            url=args.url,
            classification=args.classification or "",
            color=args.color,
            related_to=tuple(args.related_to),
            alarms=tuple(args.alarms),
            portable_description=args.portable_description,
        )
        return _emit_plan(plan, args.json)

    if args.cal_command == "update":
        target = getattr(args, "target", None)
        if not target:
            raise events.EventError("cal update requires an explicit --target", exits.USAGE)
        changes: dict[str, Any] = {}
        if args.summary is not None:
            changes["SUMMARY"] = args.summary
        if args.start is not None:
            changes["DTSTART"] = _boundary_moment(args.start, "--from")
        if args.end is not None:
            changes["DTEND"] = _boundary_moment(args.end, "--to")
        if args.description is not None:
            changes["DESCRIPTION"] = args.description
        if args.location is not None:
            changes["LOCATION"] = args.location
        if args.priority is not None:
            changes["PRIORITY"] = args.priority
        if args.categories is not None:
            changes["CATEGORIES"] = args.categories
        if args.status is not None:
            changes["STATUS"] = args.status.upper()
        if args.busy is not None:
            changes["TRANSP"] = "OPAQUE" if args.busy else "TRANSPARENT"
        if args.url is not None:
            changes["URL"] = args.url
        if args.classification is not None:
            changes["CLASS"] = args.classification.upper()
        if args.color is not None:
            changes["COLOR"] = args.color
        if args.related_to is not None:
            changes["RELATED-TO"] = args.related_to
        if args.clear_alarms:
            changes["VALARM"] = ()
        elif args.alarms is not None:
            changes["VALARM"] = tuple(args.alarms)
        if not changes and not args.portable_description:
            raise events.EventError("no changes were requested", exits.USAGE)
        plan = mutate.plan_update(
            profile,
            session=transport,
            href=args.href,
            changes=changes,
            target=target,
            recurrence_id=getattr(args, "recurrence_id", "") or "",
            portable_description=args.portable_description,
        )
        return _emit_plan(plan, args.json)

    if args.cal_command == "delete":
        target = getattr(args, "target", None)
        if not target:
            raise events.EventError("cal delete requires an explicit --target", exits.USAGE)
        plan = mutate.plan_delete(
            profile,
            session=transport,
            href=args.href,
            target=target,
            recurrence_id=getattr(args, "recurrence_id", "") or "",
        )
        return _emit_plan(plan, args.json)

    if args.cal_command == "appointment":
        if args.appointment_command == "create":
            href = _resolved_calendar(profile, transport, args.calendar)
            plan = appointments.plan_create(
                profile,
                calendar_href=href,
                summary=args.summary,
                start=appointments.parse_instant(args.start, "--from"),
                end=appointments.parse_instant(args.end, "--to"),
                origin=args.origin,
                destination=args.destination,
                mode=args.mode,
                route_estimate=appointments.parse_duration(
                    args.route_estimate, "--route-estimate", positive=True
                ),
                on_site_buffer=appointments.parse_duration(
                    args.on_site_buffer, "--on-site-buffer"
                ),
                stop_wait_margin=appointments.parse_duration(
                    args.stop_wait_margin, "--stop-wait-margin"
                ),
                preparation_duration=appointments.parse_duration(
                    args.preparation_duration, "--preparation-duration", positive=True
                ),
                route_url=args.route_url,
                description=args.description,
            )
            return _emit_plan(plan, args.json)

        if args.appointment_command == "update":
            plan = appointments.plan_update(
                profile,
                session=transport,
                appointment_href=args.appointment_href,
                travel_href=args.travel_href,
                preparation_href=args.preparation_href,
                start=appointments.parse_instant(args.start, "--from"),
                end=appointments.parse_instant(args.end, "--to"),
                origin=args.origin,
                destination=args.destination,
                mode=args.mode,
                route_estimate=appointments.parse_duration(
                    args.route_estimate, "--route-estimate", positive=True
                ),
                on_site_buffer=appointments.parse_duration(
                    args.on_site_buffer, "--on-site-buffer"
                ),
                stop_wait_margin=appointments.parse_duration(
                    args.stop_wait_margin, "--stop-wait-margin"
                ),
                preparation_duration=appointments.parse_duration(
                    args.preparation_duration, "--preparation-duration", positive=True
                ),
                summary=args.summary,
                description=args.description,
                route_url=(
                    args.route_url
                    if args.route_url is not None
                    else appointments.PRESERVE_ROUTE_URL
                ),
                clear_route_url=args.clear_route_url,
            )
            return _emit_plan(plan, args.json)

        return exits.USAGE

    return exits.USAGE


def _run_task_run(args: argparse.Namespace) -> int:
    profile = _selected_profile(args)
    transport = session.Session(profile)

    if args.run_command in {"create", "list"}:
        collection = _resolved_component(profile, transport, args.calendar, "VTODO")
        if args.run_command == "list":
            found = runs.list_runs(
                profile,
                session=transport,
                calendar_href=collection.href,
                collection_writable=not collection.read_only,
            )
            if args.json:
                _json({"runs": runs.as_list(found)})
            else:
                for graph in found:
                    _emit_run_graph(graph, json_output=False, many=True)
            return exits.OK
        plan = runs.plan_create(
            profile,
            calendar_href=collection.href,
            summary=args.summary,
            step_summaries=args.step,
            gaps=args.gap,
        )
        return _emit_run_plan(plan, json_output=args.json)

    if args.run_command == "show":
        graph = runs.show_run(profile, session=transport, root_href=args.root_href)
        return _emit_run_graph(graph, json_output=args.json)

    if args.run_command == "add":
        plan = runs.plan_add(
            profile,
            session=transport,
            root_href=args.root_href,
            summary=args.summary,
            before=args.before,
            after=args.after,
            gap_before=(args.gap_before if args.gap_before is not None else runs.MISSING),
            gap_after=(args.gap_after if args.gap_after is not None else runs.MISSING),
        )
        return _emit_run_plan(plan, json_output=args.json)

    if args.run_command == "reorder":
        plan = runs.plan_reorder(
            profile,
            session=transport,
            root_href=args.root_href,
            step_hrefs=args.step,
            gaps=args.gap,
        )
        return _emit_run_plan(plan, json_output=args.json)

    if args.run_command == "edit":
        changes: dict[str, Any] = {}
        if args.summary is not None:
            changes["SUMMARY"] = args.summary
        if args.description is not None:
            changes["DESCRIPTION"] = args.description
        if args.start is not None:
            changes["DTSTART"] = _todo_moment(args.start, "--start")
        if args.due is not None:
            changes["DUE"] = _todo_moment(args.due, "--due")
        if args.priority is not None:
            changes["PRIORITY"] = args.priority
        plan = runs.plan_edit(
            profile,
            session=transport,
            href=args.href,
            changes=changes,
        )
        return _emit_run_plan(plan, json_output=args.json)

    if args.run_command in {"done", "skip", "not-yet"}:
        at = _todo_instant(args.at, "--at") if args.run_command == "done" and args.at else None
        plan = runs.plan_transition(
            profile,
            session=transport,
            href=args.href,
            transition=args.run_command,
            at=at,
        )
        return _emit_run_plan(plan, json_output=args.json)

    return exits.USAGE


def _run_task(args: argparse.Namespace) -> int:
    if args.task_command == "run":
        return _run_task_run(args)
    profile = _selected_profile(args)
    transport = session.Session(profile)

    if args.task_command == "list":
        collection = _resolved_component(profile, transport, args.calendar, "VTODO")
        found = todos.query(
            profile,
            session=transport,
            calendar_href=collection.href,
            statuses=tuple(args.status),
            collection_writable=not collection.read_only,
        )
        if args.json:
            _json({"tasks": [task.as_dict() for task in found]})
        else:
            for task in found:
                state = task.status or ""
                when = task.dtstart or task.due
                render.emit(f"{state:12} {when:20}  {task.summary}")
                render.emit(f"      {task.href}")
                if task.parent_uid:
                    render.emit(f"      parent  {task.parent_uid}")
                if task.children:
                    render.emit(f"      children {', '.join(task.children)}")
        return exits.OK

    if args.task_command == "show":
        reference, raw = todos.fetch(profile, session=transport, href=args.href)
        if args.json:
            _json({"task": reference.as_dict(), "icalendar": raw.decode("utf-8", "replace")})
        else:
            for key, value in reference.as_dict().items():
                render.emit(f"{key}: {value}")
        return exits.OK

    if args.task_command == "create":
        collection = _resolved_component(profile, transport, args.calendar, "VTODO")
        plan = todos.plan_create(
            profile,
            calendar_href=collection.href,
            summary=args.summary,
            description=args.description,
            start=_todo_moment(args.start, "--start") if args.start else None,
            due=_todo_moment(args.due, "--due") if args.due else None,
            priority=args.priority,
            status=args.status or "",
            percent_complete=args.percent_complete,
            parent_uid=args.parent_uid or "",
        )
        return _emit_plan(plan, args.json)

    if args.task_command == "update":
        changes: dict[str, Any] = {}
        if args.summary is not None:
            changes["SUMMARY"] = args.summary
        if args.description is not None:
            changes["DESCRIPTION"] = args.description
        if args.start is not None:
            changes["DTSTART"] = _todo_moment(args.start, "--start")
        if args.due is not None:
            changes["DUE"] = _todo_moment(args.due, "--due")
        if args.priority is not None:
            changes["PRIORITY"] = args.priority
        if args.status is not None:
            changes["STATUS"] = args.status
        if args.percent_complete is not None:
            changes["PERCENT-COMPLETE"] = args.percent_complete
        if args.parent_uid is not None:
            changes["RELATED-TO"] = args.parent_uid
        if not changes:
            raise todos.TodoError("no changes were requested", exits.USAGE)
        plan = todos.plan_update(profile, session=transport, href=args.href, changes=changes)
        return _emit_plan(plan, args.json)

    if args.task_command == "complete":
        completed = _todo_instant(args.completed, "--completed") if args.completed else None
        plan = todos.plan_complete(
            profile,
            session=transport,
            href=args.href,
            completed=completed,
        )
        return _emit_plan(plan, args.json)

    if args.task_command == "delete":
        plan = todos.plan_delete(profile, session=transport, href=args.href)
        return _emit_plan(plan, args.json)

    return exits.USAGE


def _run_plan(args: argparse.Namespace) -> int:
    if args.plan_command == "list":
        pending = plans.listing()
        if args.json:
            _json({"plans": [plan.as_dict() for plan in pending]})
        else:
            for plan in pending:
                render.emit(
                    f"{plan.plan_id}  {plan.summary or 'bundle'} "
                    f"({len(plan.steps)} step(s))"
                )
                for index, (step, progress) in enumerate(
                    zip(plan.steps, plan.progress, strict=True), start=1
                ):
                    render.emit(
                        f"  {index}. {progress.state:9} {step.action:14} "
                        f"{step.summary or step.href} ({len(plans.payload_bytes(step))} bytes)"
                    )
        return exits.OK
    if args.plan_command == "show":
        plan = plans.read(args.plan_id)
        if args.json:
            _json({"plan": plan.as_dict()})
        else:
            render.emit(f"plan_id: {plan.plan_id}")
            render.emit(f"profile: {plan.profile}")
            render.emit(f"summary: {plan.summary}")
            render.emit(f"created_at: {plan.created_at}")
            render.emit(f"expires_at: {plan.expires_at}")
            for index, (step, progress) in enumerate(
                zip(plan.steps, plan.progress, strict=True), start=1
            ):
                render.emit(
                    f"step {index}: {progress.state} {step.action} "
                    f"{step.summary or step.href} ({len(plans.payload_bytes(step))} bytes)"
                )
                render.emit(f"  href: {step.href}")
                render.emit(f"  etag: {step.etag}")
                render.emit(f"  details: {step.details}")
                render.emit(
                    f"  progress: {progress.state} at {progress.timestamp} "
                    f"(exit {progress.exit_code})"
                )
        return exits.OK
    if args.plan_command == "reconcile":
        profile = _selected_profile(args)
        with plans.claim(args.plan_id):
            plan = plans.read(args.plan_id)
            transport = session.Session(profile)
            result = plans.reconcile(
                profile,
                session=transport,
                plan=plan,
                dispatchers=_dispatchers(),
            )
        if args.json:
            _json(result)
        else:
            render.emit(
                f"reconciled step {result['index'] + 1}: {result['state']} "
                f"{result['action']} {result['href']}"
            )
            if result["complete"]:
                render.emit("  plan complete; all steps are verified.")
            elif result["state"] == "pending":
                render.emit("  no remote effect was found; apply may retry this step.")
            else:
                render.emit("  the plan remains blocked until the resource is resolved.")
        return exits.OK
    if args.plan_command == "cancel":
        with plans.claim(args.plan_id):
            plans.read(args.plan_id)
            plans.consume(args.plan_id)
        warning = "Verified remote effects were not undone."
        if args.json:
            _json(
                {
                    "cancelled": args.plan_id,
                    "remote_effects_undone": False,
                    "warning": warning,
                }
            )
        else:
            render.emit(f"cancelled {args.plan_id}")
            render.emit(warning)
        return exits.OK
    return exits.USAGE


def _run_files(args: argparse.Namespace) -> int:
    profile = _selected_profile(args)
    transport = session.Session(profile)

    if args.files_command == "list":
        found = files.list_collection(profile, session=transport, href=args.href)
        if args.json:
            _json({"files": [reference.as_dict() for reference in found]})
        else:
            for reference in found:
                kind = "d" if reference.collection else "f"
                size = "-" if reference.size is None else str(reference.size)
                render.emit(f"{kind} {size:>10}  {reference.name}")
                render.emit(f"              {reference.href}")
        return exits.OK

    if args.files_command == "stat":
        reference = files.stat_resource(profile, session=transport, href=args.href)
        assert reference is not None
        if args.json:
            _json({"file": reference.as_dict()})
        else:
            for key, value in reference.as_dict().items():
                render.emit(f"{key}: {value}")
        return exits.OK

    if args.files_command == "read":
        if args.force and not args.output:
            raise files.FileError("--force requires --output", exits.USAGE)
        reference, content = files.read_file(profile, session=transport, href=args.href)
        if args.output:
            output = files.write_local(args.output, content, force=args.force)
            result = {"file": reference.as_dict(), "output": str(output)}
            if args.json:
                _json(result)
            else:
                render.emit(f"wrote {len(content)} bytes to {output}")
            return exits.OK
        text = files.text_content(content)
        if args.json:
            _json({"file": reference.as_dict(), "content": text})
        else:
            render.emit(text, end="")
        return exits.OK

    if args.files_command == "write":
        content = Path(args.source).expanduser().read_bytes()
        plan = files.plan_write(
            profile,
            session=transport,
            href=args.href,
            content=content,
            content_type=args.content_type,
        )
        return _emit_plan(plan, args.json)

    if args.files_command == "delete":
        plan = files.plan_delete(profile, session=transport, href=args.href)
        return _emit_plan(plan, args.json)

    return exits.USAGE


def _run_apply(args: argparse.Namespace) -> int:
    profile = _selected_profile(args)
    with plans.claim(args.plan_id):
        plan = plans.read(args.plan_id)
        transport = session.Session(profile)
        result = plans.apply(
            profile,
            session=transport,
            plan=plan,
            dispatchers=_dispatchers(),
        )
    if args.json:
        _json(result)
    else:
        for key, value in result.items():
            render.emit(f"{key}: {value}")
    return exits.OK


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_alarm_values(sys.argv[1:] if argv is None else argv))
    json_output = bool(args.json)

    try:
        if args.command is None:
            if json_output:
                _json({"guide": guide.render()})
            else:
                render.emit(guide.render(), end="")
            return exits.OK
        if args.command == "doctor":
            report = checks.run(
                profile_name=args.profile, round_trip=args.probe_secret_store
            )
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
        if args.command == "task":
            return _run_task(args)
        if args.command == "files":
            return _run_files(args)
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
        todos.TodoError,
        files.FileError,
        plans.PlanError,
    ) as exc:
        return _error(exc, json_output)
    except (OSError, ValueError) as exc:
        return _error(exc, json_output)


def _unexpected_error() -> int:
    render.emit_error(f"ncl: {exits.RESPONSE[exits.ERROR]}")
    return exits.ERROR


def main(argv: list[str] | None = None) -> int:
    try:
        with render.redacted_standard_streams():
            try:
                return _main(argv)
            except Exception:
                # An interpreter traceback is formatted after the stream wrapper
                # has restored the original streams, so it could expose a
                # registered value.
                return _unexpected_error()
    except Exception:
        # Cleanup failures happen after restoration and must not expose a
        # traceback either.
        return _unexpected_error()


if __name__ == "__main__":
    raise SystemExit(main())
