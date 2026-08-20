# Files over WebDAV

Nextcloud exposes account files under
`/remote.php/dav/files/<account-uid>/`. A profile names the exact subtrees that
`ncl` may reach in `files_roots`; the application password itself has access to
the whole account, so this local allowlist is the only scope boundary.

The root is an href, not a local path or a display name. Each request resolves
it against the configured origin, rejects another origin, percent-decodes one
path segment at a time, normalizes Unicode, and compares complete segments.
Consequently a root ending in `/work/` does not admit `/work-old/`, an encoded
slash, or `..`.

## Reads

`ncl files list <collection>` sends a depth-one `PROPFIND` and returns only the
collection's immediate children. `ncl files stat <resource>` uses depth zero.
Both expose stable hrefs, resource type, size where applicable, modification
time, ETag, and media type. A depth-one response naming a resource outside the
requested collection is malformed rather than extra discovery.

`ncl files read <file>` sends `GET`. UTF-8 content can be emitted through the
CLI's redacting text stream or returned as structured JSON. Arbitrary content
uses `--output <path>`, which writes the exact response bytes locally. The CLI
does not send binary content through `sys.stdout.buffer`, because that would
bypass credential redaction. A local output path is not replaced unless
`--force` is present.

Reads currently buffer one complete response in memory. Recursive traversal,
range reads, and streaming large objects are not implemented.

## Mutations

`ncl files write <href> --from <path>` reads exact local bytes into a private,
short-lived plan. A missing target is applied with `If-None-Match: *`; an
existing target requires an ETag and is applied with `If-Match`. The media type
defaults to `application/octet-stream` and can be stated with `--content-type`.

`ncl files delete <href>` also requires the ETag observed while planning.
Collections are never deleted by this command, which rules out an accidental
recursive removal.

Neither command changes the server. `ncl apply <plan-id>` performs the frozen
request as one ordered step. Every step in a bundle is validated before the
first request; verified steps are recorded and skipped on resume, execution
stops at the first failure, and no verified remote effect is rolled back. A
write is then read back and compared byte for byte. A deletion is followed by
a depth-zero lookup that must report the resource absent. An ETag mismatch is
a conflict; a successful request followed by different or indeterminate stored
state is an uncertain outcome and blocks the plan until
`ncl plan reconcile <plan-id>` reads the exact href. Reconciliation compares
exact file bytes: exact planned content is verified, a missing create is
pending, the old strong ETag with different content is pending for an existing
write, a missing delete is verified, and changed content or ETag remains
uncertain. Reconciliation reads only. Partial plans do not expire; untouched
plans retain their short freshness window. Cancelling a partial plan removes
local progress but does not undo remote effects.

The module does not create collections, copy or move resources, acquire WebDAV
locks, manage shares, or bypass a server quota. Those are separate operations
with separate safety contracts.
