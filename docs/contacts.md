# Contacts over CardDAV

An address book is a DAV collection like a calendar: discovered from the same
principal, listed the same way, held to an allowlist of its own. Nextcloud
serves them under `/remote.php/dav/addressbooks/users/<account>/` and advertises
`addressbook` in the `DAV` header of an `OPTIONS` on the principal, so the
surface exists whether or not the Contacts app's web interface is installed.

`addressbooks` is an optional profile key. An absent one means no address book
is reachable — an unstated scope is an empty allowlist, never an open one — and
`ncl doctor` reports it as unconfigured rather than as a fault, because a
profile that does not use contacts is not a broken profile.

`ncl contacts books` reports the allowlist decision instead of applying it, for
the same reason `cal list` does: a listing that hid every unconfigured book
could not be used to configure one. A server generates books of its own — an
`Accounts` book of system users, a `Recently contacted` book — and those appear
as read-only.

## Change detection

`ncl contacts changes <book> [--since <cursor>]` uses one `sync-collection`
`REPORT`. The first call without `--since` lists every resource and returns a
cursor; passing it back reports only cards changed or removed since the call.
The cursor is opaque server state belonging to that one collection, so the
caller keeps it and pairs it with the same collection. An unknown cursor is a
conflict; start again with a fresh call without `--since`. The files tree
offers no sync token, so `files` has no corresponding command.

## vCard is not iCalendar

Both are line-folded property lists with parameters, and there the resemblance
stops. vCard has its own escaping, its own structured values, and a `PHOTO`
that is routinely a hundred kilobytes of base64 between two ordinary text
properties. Parsing one with a calendar library produces something that looks
right until a value contains a semicolon.

`ncl.vcard` reads what a contact is addressed and searched by:

- Folding is undone before anything else reads a line, because a fold can land
  anywhere, including inside a base64 blob.
- A comma splits a value only when it is not escaped. Splitting first and
  unescaping afterwards turns one address containing a comma into two, which is
  noticed only by the person who did not get the mail.
- A structured value such as `N` or `ADR` splits on unescaped semicolons.
- A bare parameter is read as a type. vCard 2.1 writes `TEL;HOME:`, and reading
  that as a valueless parameter loses the only thing the line says.
- A grouped property, `item1.EMAIL`, is named by its property; the group orders
  related lines and is not part of the identity.
- `PHOTO`, `LOGO`, `SOUND`, and `KEY` are recorded as present and never as
  content. A listing that inlined a photo would be dominated by base64 nobody
  asked for.
- A component marker carries the name of what it opens or closes, and both are
  checked. The outermost component is `VCARD` or the resource is not a card,
  and a card ends where the component it opened is closed rather than at the
  first `END` — otherwise a card holding a nested component is cut in half and
  its remainder read as a second contact.
- A property inside a nested component belongs to that component, not to the
  card: it is skipped rather than refused, and the raw card carries it to a
  caller that needs it.
- A declared `VERSION` must be one this reader implements: `2.1`, `3.0`, or
  `4.0`. Escaping and structured values differ between versions, so a card
  declaring one this reader does not implement cannot be read under these
  rules, and refusing it is refusing to report values it would get wrong. A
  card declaring no version is read: the rules here are the ones it is asking
  for.
- A refusal names the card's href. A listing reads every card in a book, so a
  message saying only what was wrong leaves the operator without which card
  to fix.

## Reading contacts

A contact is addressed by resource href, never by name. Two people share a name
far more often than two events share a summary, and a contact resolved by name
is the wrong person rather than a missing one.
Each contact read stays on its exact href and refuses a redirect.

`ncl contacts list <book>` makes one bounded `addressbook-query` `REPORT`, not
one request per card. Every response entry must carry an href that is a direct
child of the requested book and address data; a repeated href is malformed
rather than an alternate observation. A resource holding more than one card is
refused, because this tool addresses one contact per resource.

A reference carries the full name, the structured family and given names, every
mail address and phone number, the organisation, title, categories, note,
birthday, and whether a photo is present. Structure it does not model —
`KIND`, `MEMBER`, `RELATED`, `GEO` — is named in `unsupported` rather than
silently dropped, and `ncl contacts show` returns the raw card beside the
reference so nothing is lost to a caller that needs more.

`ncl contacts find <book> <term>` matches case-insensitively against name, mail,
phone, and organisation. The match happens locally rather than as a CardDAV
`addressbook-query` filter: those filters are per-property text matches whose
semantics differ across servers, and a search that quietly means something else
on the next server is worse than one that costs a listing.

## Writing contacts

`contacts create`, `contacts update`, and `contacts delete` use the same frozen
plan lifecycle as calendar mutations. They write full name, structured family
and given names, repeatable email addresses and phone numbers, organisation,
title, note, birthday, and categories.

An update splices only the requested top-level properties into the raw card.
Every other byte, including `PHOTO`, unknown properties, folding, and nested
components, remains untouched. Changing the family or given name rewrites only
those two parts of `N`, so additional names, prefixes, and suffixes survive an
edit that did not name them. Replacing a grouped property, `item1.EMAIL`, also
removes the labels grouped with it, because they describe the value being
replaced. Group cards carrying `KIND` or `MEMBER`, resources holding multiple
cards, and resources without a strong ETag are refused rather than rewritten
around.

Creates use `If-None-Match: *`; updates and deletes use the strong ETag in
`If-Match`. A successful write is read back and compared as vCard properties,
allowing the server to alter `REV`, `PRODID`, and line folding. Any other
difference leaves the outcome uncertain.
