# Undoing a deletion and an overwrite

Deleting and replacing are the two mutations this tool makes that a caller
cannot take back from the surfaces it already has. Nextcloud keeps both: a
deleted file waits in a trash bin under `/remote.php/dav/trashbin/<account>/`,
and a replaced one leaves its previous content as a version under
`/remote.php/dav/versions/<account>/`. Neither lives under `/files/`, so
neither was reachable.

## The allowlist question is the inverted one

A trash entry's own href is under `/trashbin/`, which no profile allowlists.
What the allowlist has to bound is where the file *came from* and where a
restore would put it back, because that is the tree this tool is permitted to
change.

So every entry is checked against its original location. An entry whose
original location the profile does not admit is still listed — seeing what was
deleted is a read, and a bin that hid entries would answer "what did I delete"
with a filtered account of it — but it is never restored or purged.

## Restoring

A restore is a `MOVE` of the trash entry onto the server's `restore`
pseudo-collection. That collection always reports itself as existing, so
`Overwrite: F` makes every restore a `412`; the header is therefore omitted,
and the protection it would give is enforced by observations around the move
instead.

That protection matters, because the server does not overwrite when the
original path is occupied — it restores *beside* the occupant under a name
nobody asked for, such as `notes (restored).md`. A plan promising the original
path would then be describing something the server was never going to do, so a
restore onto an occupied path is refused while planning and checked again
immediately before the move, naming the remedy: move or delete what is there
first.

A trash entry normally carries Nextcloud's stable file identifier. One
described without it is still listed — the bin is where a person looks for what
they deleted, so a single unreadable entry must not hide the rest — and its
restore is refused at planning time, where the identifier is what the promise
rests on. Past the `MOVE`,
the resource at the promised location must carry that same identifier; source
absence alone is not proof, because an interloper could have won the final race
and caused the restore to land under another name. A location or identity that
cannot be confirmed afterwards is uncertainty rather than failure.

## Purging

`ncl trash purge` is the narrowest gate in the tool. Everything else it does
leaves a copy somewhere; this removes the last one, and the trash bin is where
the remedy for every other deletion lives. The frozen step records that it is
irreversible, and a purge step that does not is stale rather than applied.

Purging something already gone is the outcome the step wanted, and is reported
as such so a resumed plan stays finishable.

## Versions

Versions are keyed by the server's own file identifier rather than by path, so
a renamed file keeps its history; `oc:fileid` is read first, and a server that
reports none leaves the file's versions unaddressable rather than empty. The
current content is not among the versions listed.

Restoring a version is safe in a way purging is not: replacing the current
content makes that content a version in turn, so nothing is lost. The preview
names which revision would win.

## Not modelled

Emptying the whole bin at once is not offered: it destroys unbounded content
that no plan can meaningfully show, which is the same reason collection
deletion is not offered. Version labels are read but not written, and a
version's content cannot be read without restoring it.
