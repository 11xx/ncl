# ai-agent-nextcloud

`ncl`: a deterministic CLI that lets an agent read and write a Nextcloud
instance over CalDAV, WebDAV, and OCS, so that reaching a calendar or a file
never requires driving the web UI. Non-goals: no MCP server, no daemon, no
browser automation, no prompt layer.

## Invariants

- **The credential never becomes an argument.** It is read from the configured
  secret backend at the moment of the request and never appears in `argv`, an
  environment variable this tool sets, a log line, an error message, a test
  fixture, or a `--json` payload. `ncl login` obtains it through Login Flow v2
  and writes it straight to the backend, so the human never handles it either.
- **The secret backend is chosen by the host, not by this repository.** Not
  every machine runs a Secret Service; a backend that is merely installed is
  not a backend that works. `ncl doctor` decides by probing, and a backend that
  cannot store and retrieve a value fails loudly rather than degrading to a
  file.
- **Failing closed is the point.** No route to the instance, no credential, an
  expired credential, a path outside the allowlist, and a malformed response
  are five different failures with five different exit codes, because the
  correct reaction differs. None of them may fall back to a browser, to public
  ingress, or to a guess.
- **A write is never a side effect of a read.** Discovery, listing, and reading
  are automatic. Anything that creates, modifies, or deletes a calendar event
  or a file passes an explicit confirmation boundary, and deletion is the
  narrowest gate of all.
- **What the tool does not model, it must not destroy.** An event written by
  the Nextcloud web UI carries properties this tool has no opinion about. An
  update rewrites the fields it was asked to change and preserves the rest;
  silently dropping an attendee list or a recurrence rule is the worst defect
  this project can ship, because the caller sees exit 0.
- **Scope is allowlisted, not discovered.** Calendars and file roots reachable
  by the tool are the ones named in configuration. A correct request for a
  collection outside it is still refused.
- **State lives outside the repository, and so does every host fact.** Config
  in `$XDG_CONFIG_HOME/ncl/`, caches in `$XDG_DATA_HOME/ncl/`. No hostname,
  account name, calendar name, or personal path is committed — this repository
  is meant to be publishable, and that is a property maintained per commit
  rather than audited later.

## Working here

- **The CLI is the only workflow surface.** Bare `ncl` teaches the loop and the
  judgment around it; `--help` carries each command's contract. There is no
  external skill file or instruction fragment to install alongside, which means
  a behavior change updates the guide in the same commit — nothing else will
  catch the drift.
- **Every claim in the guide must be traceable to code.** A guide that promises
  more than the tool does is worse than none, because it is believed. Check the
  claim against the source before writing it, and cut what cannot be traced
  rather than softening it.
- **State the invariant, not this host's reading.** `docs/` describes how
  Nextcloud's protocols behave and why the code is shaped around them, and
  holds anywhere the tool runs. A sentence naming a server version, a tailnet
  name, an account, or a calendar is a reading and belongs in configuration or
  in `ncl doctor`'s output.
- **The gates.** `make check` runs all three — `uv run pytest`,
  `uv run ruff check .`, and `uv build`. Both the tools and the rules they
  enforce are pinned in `pyproject.toml` so they report the same thing on any
  host.
- **Versions are calendar dates**, `YYYY.0M.0D`, as `yt-dlp` numbers its
  releases. A release says when it was cut, which is the only thing a tool with
  no stable public API can honestly promise. Note that the build normalizes the
  padding: a declared `2026.08.17` produces artifacts named `2026.8.17`, and
  that difference is PEP 440 doing its job rather than a mistake to correct.
- **`make install` is editable**, so the `ncl` on PATH follows the checkout and
  never needs refreshing after a change. It also pins to the checkout's path:
  install from the repository, not from a worktree that is about to be removed.
- **Tests must not need a server.** The protocol layers take an injected
  transport so that request construction, response parsing, and the refusal
  paths are all exercised offline. A test that silently passes because it never
  reached the network has measured nothing; assert on the request that was
  built, not only on the answer that came back.
- **Verify a claim about the server before writing it into code, a comment, or
  `docs/`.** Nextcloud's DAV surface has behaviours its documentation does not
  state, and they differ across versions.
- Run non-trivial changes through `arc`; gates are `uv run pytest` and
  `uv run ruff check .`.
