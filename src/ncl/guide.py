"""The bare-``ncl`` guide: orientation before the first command."""

from __future__ import annotations

GUIDE = """ncl — use a Nextcloud instance programmatically, from a command line.

`ncl` is a deterministic surface for an agent. It speaks Nextcloud's protocols
directly instead of driving a browser, and is deliberately neither an MCP
server nor browser automation.

Calendars are what it implements today, and the only thing it claims to do.
Everything outside the `cal` commands — profiles, scope allowlists, exit codes,
the plan/apply boundary — is app-agnostic and will carry the Nextcloud apps
added after them.

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
  than guessing between them.

  Listing reports the allowlist decision instead of filtering by it — a listing
  that hid everything unconfigured could not be used to configure anything.

READ
  ncl cal events <calendar> --from <iso> --to <iso>
  ncl cal show <event-href>

  The window is required, and times need an explicit UTC offset: a local time
  is ambiguous across one DST transition each year and nonexistent across the
  other. An event carrying structure this release will not rewrite — a
  recurrence rule, attendees, an alarm — is listed with what makes it
  unwritable. It can be read; it cannot be edited here.

CHANGE NOTHING BY ACCIDENT
  ncl cal create <calendar> --summary S --from <iso> --to <iso>
  ncl cal update <event-href> --summary S
  ncl cal delete <event-href>

  None of these change anything. Each resolves its target, freezes exactly what
  it would do, prints it, and exits 40 with a plan id. The server is untouched
  until:

  ncl apply <plan-id>           Execute that frozen plan, once.
  ncl plan list|show|cancel     Inspect or discard what is pending.

  Creation is conditional on nothing being there; update and deletion are
  conditional on the ETag read while planning, so a change that landed in
  between conflicts instead of being overwritten. After a write the resource is
  read back and compared: a server that accepts a PUT and stores something else
  is reported rather than assumed.

  This boundary is not authorization. `ncl` cannot tell whether a plan id came
  from whoever read the preview or from the agent that produced it. Where a
  human's approval is genuinely required, stop after planning and wait to be
  told to continue.

WHAT IT REFUSES
  Scope is an allowlist of hrefs in configuration. The server issues no scoped
  credentials, so that allowlist is the only scope boundary that exists — a
  request outside it is refused here or nowhere.

  Every command takes `--json`. Exit codes are the contract and say what to do
  next: configure, log in, re-read, reconcile, or stop. `ncl doctor --json`
  ships their meanings alongside the checks it ran.

Use `ncl <command> --help` for the exact arguments a command accepts.
"""


def render() -> str:
    """Return the guide printed by bare ``ncl``."""
    return GUIDE
