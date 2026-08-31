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

`ncl files find <collection>` asks the server where a file is instead of
walking the tree for it. It sends a WebDAV `SEARCH` to the DAV root — which is
where Nextcloud accepts one — scoped by a path relative to that root rather
than by the collection href everything else here is addressed by, so the two
are converted in one place.

At least one of `--name`, `--content-type`, or `--modified-since` is required.
A search with no condition is a recursive listing wearing a search's clothes
and returns the whole subtree at a cost the caller did not ask for. In `--name`
the wildcards are `*` and `?`; a literal `%` or `_` in a filename survives
rather than becoming a wildcard nobody asked for. Every literal is escaped into
the request body as text, so a pattern cannot close the element around it and
change which query the server ran.

The scope check is the load-bearing part. A search names a subtree and the
server decides what matches, which makes the answer the one place a resource
outside the allowlist can enter through a request that was itself in scope.
Every result is checked against both the requested subtree and the allowlist,
and a result outside either is a refusal rather than a filtered-out row: a
server returning what it was not asked for is not a server whose other answers
can be trusted. A row the server reports as failed is malformed rather than
skipped. `--limit` is enforced on the answer as well as sent in the request, so
a server that ignores the bound is refused rather than allowed to enlarge it.

Which conditions a server answers is not uniform. A rejected search is reported
as possibly naming an unsupported condition rather than as a malformed request,
because that is the difference the caller can act on.

## Objects larger than memory

A single `PUT` needs its whole body at once — in this process, and in the plan
that froze it, where a payload is stored base64-encoded. That is right for a
note and wrong for a video: freezing a gigabyte would cost a gigabyte and a
third on disk before anything was sent.

Above `uploads.INLINE_LIMIT`, `ncl files write` freezes the *identity* of the
content instead of the content: the source path, its size, and its SHA-256.
Applying re-reads the file and hashes it while sending, and refuses if what it
read is not what the plan promised — a plan that quietly uploaded whatever the
path happens to hold now would not be a frozen plan. Below the limit nothing
changes, and the exact bytes stay in the plan.

The transfer itself is Nextcloud's chunked upload: an upload directory is
created, numbered parts are `PUT` into it, and a `MOVE` of the directory's
`.file` pseudo-resource assembles them at the destination in one server-side
operation, consuming the directory. Parts are named zero-padded so a server
ordering them lexicographically and one ordering them numerically assemble the
same bytes. Nothing exists at the destination until that final `MOVE`, so a
failure part-way through a creation leaves the destination untouched. A
replacement uploads every part first, conditionally deletes only the destination
revision whose ETag the plan froze, then assembles with `Overwrite: F`. A newer
revision is never overwritten, and a resource that appears between deletion and
assembly is left in place. Once the conditional deletion succeeds, any
unconfirmed outcome is uncertain, and the removed revision is recoverable from
the trash only where the trashbin app is enabled and its retention has not
already expired the entry. That residual window is what this design costs. The upload directory is removed on the way out, and failing to
remove it never replaces the error that caused it.

Applying and reconciling a streamed write read the stored file back in windows
and hash it, rather than pulling it into memory. A matching length is not a
matching file — two different files of one length are the ordinary case — so a
destination whose bytes do not hash to the frozen identity stays uncertain.

`ncl files read --offset <n> [--length <n>]` reads one window, and writes it at
its own offset in `--output`, because a ranged read is normally one of several.
A server may ignore `Range` and answer `200` with the whole entity; that is
legal, and returning it as though it were the requested window would misplace
every byte the caller then indexes, so it is refused instead. A `206` answer is
also held to the exact interval in `Content-Range`; bytes from another offset
are never written under the requested one.

Recursive traversal is still not implemented; `ncl files find` answers the
question it was usually wanted for.

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
