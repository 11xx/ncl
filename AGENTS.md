# ncl

A deterministic CLI that lets an agent use a Nextcloud instance over CalDAV,
CardDAV, WebDAV, and OCS. It reaches calendars, tasks and checkpoint runs,
contacts, files, the trash bin and file versions, shares, and the tags and
comments on a file.

Non-goals: no MCP server, no daemon, no browser automation, no prompt layer,
no mutation outside plan/apply.

Exit codes, profiles, scope allowlists, the secret backend, the output
boundary, and the plan/apply boundary belong to the tool. Anything that knows
what a VEVENT or a vCard is belongs beside its module. A new Nextcloud app is a
new module and its commands, not a change to the foundation.

## Invariants

- The credential never becomes an argument: not `argv`, not an environment
  variable this tool sets, not a log line, error, fixture, or `--json` payload.
- Command output goes through the redacting text streams only; never raw file
  descriptors or binary buffers.
- The secret backend is chosen by the host and proven by probing; nothing
  degrades to a file.
- Failing closed is the point: each distinct failure has its own exit code, and
  none falls back to a browser, public ingress, or a guess.
- A write is never a side effect of a read. Every mutation is planned,
  previewed, applied explicitly, and read back; deletion is the narrowest gate.
- What the tool does not model, it must not destroy: an update rewrites the
  fields it was asked to change and preserves every other byte.
- Scope is allowlisted, not discovered. A correct request outside the
  allowlist is refused.
- State lives outside the repository under XDG paths, and so does every host
  fact: no hostname, account, calendar name, or personal path in the tree.

## Working here

- Alpha: breaking changes are welcome when they produce a better design, and
  no compatibility shim, alias, or legacy branch stays in the tree.
- Bare `ncl` is the only workflow surface and `--help` each command's contract;
  a behaviour change updates the guide in the same commit, and every claim in
  the guide or `docs/` traces to code.
- `docs/` states how the protocols behave, never this host's reading of them.
- Verify a claim about the server against a live instance before writing it
  into code, a comment, or `docs/`.
- Tests never touch a server: the protocol layers take an injected transport,
  and a test asserts on the request built, not only on the answer.
- Gate: `make check` (`uv run pytest`, `uv run ruff check .`, `uv build`).
  Versions are calendar dates, `YYYY.0M.0D`.
- Run non-trivial changes through `arc`.
