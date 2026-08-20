"""The bare-``ncl`` guide: orientation before the first command."""

from __future__ import annotations

GUIDE = """ncl — use a Nextcloud instance programmatically, from a command line.

`ncl` is a deterministic surface for an agent. It speaks Nextcloud's protocols
directly instead of driving a browser, and is deliberately neither an MCP
server nor browser automation.

It reaches calendars through CalDAV and files through WebDAV. Profiles, scope
allowlists, exit codes, the secret backend, and the plan/apply boundary belong
to the whole tool rather than either module.

THE ORDER
  ncl doctor                    Local preconditions. Run it first; it reports
                                each failure in terms of the command that fixes
                                it.
  ncl login                     Browser consent, once. Prints a URL and waits.
  ncl whoami                    Prove the credential, and see which account and
                                calendar home it actually reached.
  ncl logout                    Revoke server-side, then forget locally.

FIND A CALENDAR BEFORE NAMING ONE
  ncl cal list                  Every calendar under the calendar home: its
                                href, whether it is writable, and whether the
                                allowlist admits it.

  Address a calendar by href. A display name is chosen by the user and is not
  unique, so a command given one refuses when two calendars share it rather
  than guessing between them. A short href target matches only an exact final
  path segment: `work` never selects `subwork`.

  Listing reports the allowlist decision instead of filtering by it — a listing
  that hid everything unconfigured could not be used to configure anything.

READ
  ncl cal events <calendar> --from <iso> --to <iso>
  ncl cal show <event-href>

  The window is required, and times need an explicit UTC offset: a local time
  is ambiguous across one DST transition each year and nonexistent across the
  other. JSON event references include `url` and `status`, using an empty value
  when the VEVENT property is absent. Human `cal show` displays the URL, while
  a cancelled event is marked in a human listing without putting links on every
  row. An event carrying structure this release will not rewrite — a recurrence
  rule or attendees — is listed with what makes it unwritable. It can be read;
  it cannot be edited here.

WORK WITH TASKS
  ncl task list <calendar> [--status <status>]
  ncl task show <task-href>

  Tasks are VTODO components in a calendar collection. The selected collection
  must advertise VTODO, and a status filter can be repeated. Listing makes one
  bounded CalDAV report and derives parent/child UIDs from that response; it
  does not read each task separately. Every response entry must carry an href
  and successful calendar data, and every resource must contain exactly one
  valid VTODO inside the selected collection and profile allowlist. Missing
  task properties are shown as empty values. Singleton VTODO properties cannot
  repeat, DUE and DURATION cannot coexist, and an existing COMPLETED value must
  be UTC. Duplicate UIDs and parent cycles fail closed as ambiguous; a parent
  outside the report remains visible by UID rather than being guessed or
  rejected.

  ncl task create <calendar> --summary S [--start <iso>] [--due <iso>]
  ncl task update <task-href> --summary S
  ncl task complete <task-href>
  ncl task delete <task-href>

  Task writes use the same ordinary frozen plan/apply boundary as calendar
  writes, and applying claims the plan before reading it. A due date does not
  require a start. Creation uses If-None-Match: *, while update, completion,
  and deletion use a strong quoted ETag. Completion is one conditional write
  that sets STATUS to COMPLETED, records one UTC COMPLETED instant, and sets
  PERCENT-COMPLETE to 100.

  Every task GET, PUT, and DELETE stays on its exact href. Creates, updates,
  and completion are read back and semantically compared, preserving unknown
  properties and nested components; only DTSTAMP and LAST-MODIFIED may be
  refreshed by the server. A deletion is verified only when the exact href
  returns 404. Redirects, persistent targets, and uncertain readbacks leave
  the plan pending. Recurrence, scheduling structures, DURATION, and unsupported
  sibling components remain readable but are refused for mutation.

REACH ALLOWLISTED FILES
  ncl files list <collection-href>  List one collection, without recursion.
  ncl files stat <resource-href>    Read size, mtime, media type, and ETag.
  ncl files read <file-href>        Read UTF-8 text through redacted stdout.

  File hrefs must be inside one of the profile's `files_roots`. Binary content
  never bypasses the text redaction boundary: use `files read --output <path>`
  to write its exact bytes to a local file. An existing output path is refused
  unless `--force` is explicit.

CHANGE NOTHING BY ACCIDENT
  ncl cal create <calendar> --summary S --from <iso> --to <iso>
  ncl cal update <event-href> --summary S
  ncl cal delete <event-href>
  ncl files write <file-href> --from <local-path>
  ncl files delete <file-href>

  None of these change anything. Each resolves its target, freezes exactly what
  it would do, prints it, and exits 40 with a plan id. The server is untouched
  until:

  ncl apply <plan-id>           Execute that frozen plan, once.
  ncl plan list|show|cancel     Inspect or discard what is pending.

  Creation is conditional on nothing being there; replacement and deletion are
  conditional on the strong ETag read while planning, so a change that landed
  in between conflicts instead of being overwritten. Applying claims the plan
  before loading it: a simultaneous claimant is locked out, and a plan consumed
  by another caller is not dispatched from an older observation.

  After a calendar write the exact resource is read back and compared using
  semantic iCalendar content. Unknown properties, nested components, boundary
  values, URL, status, and any opted-in portable description must survive as
  planned; only server-managed DTSTAMP and LAST-MODIFIED values may be
  refreshed. Exact event reads and calendar PUT/DELETE requests refuse
  redirects before following them. A redirect before a mutation reaches a
  second target is malformed. A calendar deletion is established only when
  the exact href returns 404 after DELETE. A redirect, persistent resource, or
  malformed post-write readback leaves the plan pending and returns the
  outcome-uncertain code. Collection deletion is refused.

  This boundary is not authorization. `ncl` cannot tell whether a plan id came
  from whoever read the preview or from the agent that produced it. Where a
  human's approval is genuinely required, stop after planning and wait to be
  told to continue.

  `cal create` and `cal update` accept `--portable-description` when a caller
  wants selected structured fields copied into a deterministic plain-text block
  in DESCRIPTION. The block contains LOCATION, URL, STATUS, CATEGORIES,
  PRIORITY, TRANSP, CLASS, and VALARM trigger values in that order. It is an
  opt-in compatibility projection: the VEVENT properties remain canonical, a
  URL is never fetched, and prose outside the tool-owned block survives. A
  malformed, duplicated, nested, reversed, or unpaired block is refused.

  On update, omitting `--alarm` preserves existing reminders, `--alarm` replaces
  them with the repeatable offsets supplied, and `--clear-alarms` removes all of
  them. The two options are mutually exclusive. A calendar client may still
  hide URL or other structured fields; the projection only makes them readable
  to more clients and cannot force a client to display them.

WHAT IT REFUSES
  Scope is an allowlist of hrefs in configuration. The server issues no scoped
  credentials, so that allowlist is the only scope boundary that exists — a
  request outside it is refused here or nowhere.

  Every command takes `--json`. Exit codes are the contract and say what to do
  next: configure, log in, re-read, reconcile, or stop. `ncl doctor --json`
  ships the chosen exit code and remediation alongside the checks it ran.
  Remote credential, server, and malformed-response failures retain their
  actionable codes instead of becoming a generic local failure.

Use `ncl <command> --help` for the exact arguments a command accepts.
"""


def render() -> str:
    """Return the guide printed by bare ``ncl``."""
    return GUIDE
