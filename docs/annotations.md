# Saying something about a file

A tag and a comment both describe a file without changing its bytes. Neither
lives under `/remote.php/dav/files/`: tags are instance-wide objects under
`/remote.php/dav/systemtags/`, their attachments to files under
`/remote.php/dav/systemtags-relations/files/<fileid>/`, and comments under
`/remote.php/dav/comments/files/<fileid>/`. Both address the file by the
server's own identifier, so a rename does not detach them.

## What the server answers

`PROPFIND systemtags/` at `Depth: 1` returns `207`. The collection itself
answers `oc:id`, `oc:display-name`, `oc:user-visible`, `oc:user-assignable`,
and `oc:can-assign` with a `404` propstat, and each tag is a response at
`systemtags/<id>/` carrying them in a `200` propstat, the booleans as the text
`true` or `false`.

The tags one file carries are also readable on the file itself, as the property
`nc:system-tags` in `http://nextcloud.org/ns`. Its children are `nc:system-tag`
elements whose text is the name and whose attributes are `oc:id`,
`oc:user-visible`, `oc:user-assignable`, and `oc:can-assign`. That is one
request against the file rather than one against a relations collection, so it
is what `ncl files tags` reads and what verifies an assignment afterwards.

Assigning is `PUT systemtags-relations/files/<fileid>/<tagid>` → `201`;
removing is `DELETE` on the same href → `204`.

`PROPFIND comments/files/<fileid>/` at `Depth: 1` answers one response per
comment at `.../<fileid>/<id>` with `oc:id`, `oc:message`, `oc:actorId`,
`oc:actorDisplayName`, `oc:creationDateTime` (an RFC 1123 date, rendered here
as UTC ISO 8601), `oc:verb`, and `oc:isUnread`; the collection answers with a
`404` propstat. Posting is `POST` to the collection with
`{"actorType": "users", "verb": "comment", "message": "..."}` → `201` with a
`Content-Location` naming the new comment, and removing it is a `DELETE` on
that href → `204`. The server lets an author remove their own comment and
nobody else's, so a comment by another actor is refused while planning rather
than sent and rejected.

## Creating a tag is irreversible from here

`POST systemtags/` with `{"name": ..., "userVisible": true,
"userAssignable": true}` answers `201` and a `Content-Location` naming the tag.
`DELETE systemtags/<id>` answers `403` for an ordinary account: only an
administrator can take a tag back out of the instance-wide list.

So a tag creation carries the same `irreversible` marker in its frozen step as
a trash purge does, a step without it is stale rather than applied, and the
preview says that a non-administrator account cannot delete the tag before the
creation is approved.

## The plan boundary

Every write here is planned and applied like any other: nothing is sent while
planning, and `ncl apply` performs the frozen step.

A tag is visible to everyone who can see the file, and it says nothing about
itself from the file's content — reading the file afterwards looks the same
whether or not it carries one. Assignment is therefore treated the way a share
is: the preview names the tag, the file, and that reach.

Tags are addressed by exact name, because a name is what a caller has. Two tags
sharing one name are ambiguous rather than guessed between, an unknown name is
not created on the way past, and assigning a tag a file already carries — or
removing one it does not — is refused while planning, where the answer is known
without a request.

Applying reads the result back. An assignment must show the tag on the file and
a removal must show it gone; a comment is read back by the id the server
returned, and its removal by the comment's absence from the listing. A lost
answer to a request that was already sent is uncertainty, never a retry: the
requests are unconditional, so a blind second attempt can post a second
comment. Reconciling settles it — a tag creation by how many tags carry the
name, an assignment by the file's own tags, and a posted comment by whether
this account's message appears on the file after the plan was made.

## Custom properties are not offered

The server accepts a `PROPPATCH` of an arbitrary property with `200` and then
does not store it: a following `PROPFIND` reports the property as `404`. A
command built on that would report a successful write for something that never
happened, which is the one thing a write boundary exists to prevent.
