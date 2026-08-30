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
requested collection is malformed rather than extra discovery. Each response
needs a resource type, and duplicate resource hrefs are malformed rather than
alternate observations.

A `PROPFIND` asks for properties that do not apply to every resource it
reaches, and a server says so with a `404` propstat rather than by omission: a
collection has no entity body, so it reports no content length and no media
type. That is an answer, and it is read as absence. Every other failing status
is the server declining to say, which stays a refusal — a size withheld by a
`403` is not a size of zero.

Absence is then allowed only where it leaves the resource addressable. A
collection may lack a content length and a media type, and any resource may
lack a media type. A file reporting no content length or no ETag is refused,
because both are what a later conditional write is built from.

`ncl files read <file>` sends `GET`. Content declared as a textual media type
and encoded as UTF-8 can be emitted through the CLI's redacting text stream or
returned as structured JSON. Unknown, binary, or non-UTF-8 content uses
`--output <path>`, which writes the exact response bytes locally. The CLI does
not send binary content through `sys.stdout.buffer`, because that would bypass
credential redaction. A local output path is not replaced unless `--force` is
present. Every file request, including a listing, must answer for its exact
href; redirects are refused.

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
request as one ordered step. The plan also stores an opaque fingerprint of the
selected profile's origin, backend, calendars, and file roots; changing that
configuration makes the plan stale. Every step in a bundle is validated before
the first request; verified steps are recorded and skipped on resume, execution
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

## Relocation and collections

`ncl files move <href> --to <href>` relocates one file with a single `MOVE`.
The alternative — read, write to the new href, delete the old one — is three
mutations with a window where both copies exist and another where neither is
durable, and it silently drops every property the round trip does not carry.

Both endpoints pass the files allowlist, and both are checked while planning:
checking only the source would let a correct request move a resource out of the
configured scope. `Overwrite: F` is sent on every move and is not configurable,
so a destination that already holds something is a conflict rather than a
target to replace. The `If-Match` header carries the source ETag observed while
planning, which RFC 4918 applies to the source of a `MOVE`; a server that
ignores it leaves the move unconditional on the source, which is why the
readback is what establishes the outcome. Planning freezes the source's content
identity: a SHA-256 over bytes read under the very ETag the metadata reported,
refusing the
plan outright if the two reads describe different revisions. Size is not
identity — two revisions of one length are indistinguishable by it — so a move
is verified by reading the destination back as a file of the planned size
*whose content hashes to the frozen digest*, and confirming the source returns
absent. Once the server has accepted the `MOVE`, no failure past that point is
a clean one: a read that fails, content that does not match, or a source that
will not confirm its own absence all report an uncertain outcome, and
reconciliation classifies the same way. A server reporting 204, which means the
destination was overwritten, is likewise uncertain: `Overwrite: F` was sent to
prevent exactly that.

Collections are refused as move sources for the reason they are refused as
delete targets: they hold unbounded content that no plan can meaningfully show.

`ncl files mkcol <href>` creates one collection under an allowlisted root and
verifies that the href reads back as a collection. Creation is the one mutation
that enlarges what the tool can reach, because the allowlist is a prefix list
and a new collection under an allowed prefix is admitted as soon as it exists.
A parent that does not exist is reported as a missing target rather than
created implicitly, and an href that already exists is a conflict.

The module does not copy resources, acquire WebDAV locks, manage shares, or
bypass a server quota. `COPY` is the one relocation method left out: a copy can
be had with `read` plus `write`, losing only properties and atomicity, whereas
a move cannot be simulated safely at all.
