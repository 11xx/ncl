"""The bare-``ncl`` guide: orientation before the first command."""

from __future__ import annotations

GUIDE = """ncl — read and write explicitly allowlisted calendars and files on Nextcloud.

`ncl` is a deterministic command-line surface for an agent. It uses the
configured protocol endpoints instead of driving a browser. It deliberately
does not automate a browser and does not provide an MCP server.

THE ORDER
  ncl doctor                    Check the local preconditions first.
  ncl login                     Obtain and verify an application password.
  ncl whoami                    Show the authenticated principal and calendar home.
  ncl logout                    Revoke and remove the stored credential.
  work                          Later slices will add the calendar and file commands.

Configuration lives outside the repository, and calendars and file roots are
explicit non-empty allowlists; a resource outside one is refused rather than
warned about. Login Flow v2 is the only authentication mechanism. The secret
backend is probed before browser consent, and credentials are read only from
that backend.

Every command takes `--json` for machine-readable output. Exit codes are the
contract: they distinguish failures so the caller knows whether to configure,
re-observe, reconcile, or stop. `ncl doctor --json` includes the meanings of
all exit codes as well as the checks it ran.

Use `ncl <command> --help` for the exact arguments accepted by that command.
"""


def render() -> str:
    """Return the guide printed by bare ``ncl``."""
    return GUIDE
