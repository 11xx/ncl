# ncl

`ncl` is a deterministic CLI that lets an agent use a Nextcloud instance over CalDAV, CardDAV, WebDAV, and OCS, with no browser automation, no MCP server, and no prompt layer. Every mutation is planned, previewed, and applied explicitly.

## Install

From a checkout, install an editable copy that puts `ncl` on `PATH` through `uv tool install`:

```console
make install
```

Alternatively, install it with pipx:

```console
pipx install .
```

`ncl` requires Python 3.11 or newer and one secret backend: `pass` or `secret-tool` (libsecret).

## Configure

Configuration lives at `~/.config/ncl/config.toml`, under `$XDG_CONFIG_HOME` when set, or at the path named by `NCL_CONFIG`.

```toml
default_profile = "home"

[profiles.home]
origin = "https://cloud.example.org"
secret_backend = "pass"
calendars = ["/remote.php/dav/calendars/alice/personal/"]
files_roots = ["/remote.php/dav/files/alice/Documents/"]
addressbooks = ["/remote.php/dav/addressbooks/users/alice/contacts/"]
```

An allowlist names the exact collections the tool may reach, and nothing else is reachable. Run `ncl cal list`, `ncl files list`, and `ncl contacts books` to see hrefs and whether the allowlist admits them.

## First run

Check the local setup, authenticate, and verify the account:

```console
ncl doctor
ncl login
ncl whoami
```

Run `ncl` for the workflow guide and `ncl <command> --help` for each command's contract.

## How an agent uses it

Reads are direct. Every write prints a plan and exits 40; `ncl apply <plan-id>` executes it. Exit codes are the contract, and `ncl doctor --json` publishes the table. Every command accepts `--json` for structured output.

## Documentation

- [Authentication](docs/auth.md) — Login Flow v2, secret storage, and credential handling.
- [Calendars](docs/calendar.md) — Calendar discovery and portable event descriptions.
- [Tasks](docs/tasks.md) — VTODO operations over CalDAV.
- [Contacts](docs/contacts.md) — Address books and contacts over CardDAV.
- [Files](docs/files.md) — File operations over WebDAV.
- [Recovery](docs/recovery.md) — Restoring deletions and overwritten content.
- [Shares](docs/shares.md) — Share operations over OCS.
- [Annotations](docs/annotations.md) — Tags and comments on files.
- [Contributor invariants](AGENTS.md) — The design and safety invariants contributors keep.

## Development

`make check` runs the tests, ruff, and the build. Tests never touch a server.

## Status

Alpha. Breaking changes are made whenever they produce a better design.

## License

This is free and unencumbered software released into the public domain under the [Unlicense](UNLICENSE).
