# Calendars and portable event descriptions

`ncl` treats the VEVENT as the source of truth. The structured `URL` and
`STATUS` properties are exposed by `cal events --json` and `cal show --json`;
each is `""` when the property is absent. Human-readable `cal show` includes
the URL. Human-readable event listings mark `CANCELLED` events, but do not add
URLs to every row.

A URL stored in a VEVENT can survive CalDAV synchronization while a calendar
client hides it. The structured value is therefore the dependable canonical
value, and the display difference is a client-presentation limitation rather
than a reason to rewrite the event automatically.

## Recurrence identities and targets

`cal occurrences <calendar> --from <iso> --to <iso>` performs one bounded
CalDAV time-range report and expands supported recurrence sets locally. Each
result includes the resource href and ETag, UID, master-or-override source,
the original `RECURRENCE-ID` in a reusable wire form, original start, effective
start and end, and cancellation state. Moved overrides are selected by their
original identity, not by their effective start. UTC DATE-TIME, `TZID=Zone`
DATE-TIME, and `VALUE=DATE` identities remain distinct and reusable.

The expansion combines `DTSTART`, one `RRULE`, repeatable DATE or DATE-TIME
`RDATE`, and repeatable `EXDATE`. It de-duplicates equivalent identities and
never silently truncates a result. `EXRULE`, period-valued `RDATE`, multiple
`RRULE` properties, incompatible value kinds or timezones, malformed master or
override structures, and an expansion over the fixed safety ceiling are
refused before a plan is created.

Every calendar update and deletion names `--target resource`, `series`,
`occurrence`, or `this-and-future`. Occurrence and future-split targets also
require the exact `--recurrence-id` emitted by occurrence discovery. The
ordinary resource target retains the safe non-recurring single-VEVENT subset;
generic event references continue to report recurrence as unwritable.

Series updates change only the master’s modeled non-rekeying fields and leave
all override component bytes in the frozen resource. Occurrence updates edit
an existing override in place or clone the master into a new exception after
removing recurrence-set properties. The UID and original identity remain
exact. Occurrence deletion creates or updates `STATUS:CANCELLED` on that
exception; it does not rewrite `EXDATE`.

This-and-future creates a new-UID future resource rather than using
`RANGE=THISANDFUTURE`. The recurrence set is partitioned only when the
generated identities prove the old and new sets equivalent; `COUNT` is split
by generated position, finite rule partitions are checked against their
generated identities, and only simple unbounded daily/weekly rules retain
their semantics. Complex or otherwise unprovable rule partitions are refused.
DTSTART/DTEND shifts requested by the caller are refused.
Future overrides are remapped to the new UID while retaining their original
identities and explicit properties. The plan creates the future resource with
`If-None-Match: *` before conditionally updating or deleting the old resource;
the preview warns that a partial failure may temporarily duplicate future
occurrences. The generic plan resume, uncertainty, and reconciliation lifecycle
handles the resulting ordered steps.

Organizer and attendee structures remain refused throughout this slice.

## Reminders

An update with neither alarm option preserves every existing `VALARM`. Repeating
`--alarm` replaces the complete alarm set with the supplied RFC 5545 duration
triggers. `--clear-alarms` replaces it with an empty set. The replacement and
clear forms are mutually exclusive.

## Portable description projection

`--portable-description` is explicit on both `cal create` and `cal update`. It
adds or regenerates one tool-owned plain-text block in `DESCRIPTION`:

```text
--- ncl portable fields ---
LOCATION: ...
URL: ...
STATUS: ...
CATEGORIES: ...
PRIORITY: ...
TRANSP: ...
CLASS: ...
VALARM TRIGGER: ...
--- end ncl portable fields ---
```

Only present values are emitted, and the fields always use that order. Each
alarm with a trigger gets one `VALARM TRIGGER` line. Repeating the operation
replaces the existing block, so stale projected values do not accumulate.

Authored text outside the block and unrelated VEVENT properties/components are
preserved. Without the option, an ordinary description is not rewritten. The
tool never parses prose back into structured fields and never fetches a URL.
Duplicate, nested, reversed, or unpaired delimiters are refused before a plan
is created, because replacement would otherwise be ambiguous.

## Appointment bundles

An appointment bundle is one appointment anchor, one travel block, and one
preparation block in the same allowlisted calendar. Create it with all facts
stated explicitly:

```text
ncl cal appointment create <calendar> \
  --summary "Appointment" \
  --from 2026-09-01T14:00:00-03:00 --to 2026-09-01T15:00:00-03:00 \
  --origin "Home" --destination "Rua A, 10" --mode bus \
  --route-estimate PT1H --on-site-buffer PT15M \
  --stop-wait-margin PT10M --preparation-duration PT20M \
  [--route-url <directions-uri>] [--description <appointment-prose>]
```

Starts and ends need an explicit offset and whole-second precision. Durations
use RFC 5545 day/time syntax: weeks, days, hours, minutes, and seconds are
accepted; months, years, negative values, fractional seconds, zero route or
preparation durations, and arithmetic overflow are refused. On-site buffer
and stop/wait margin may be zero. The timeline is calculated exactly backwards:

```text
target arrival    = appointment start - on-site buffer
planned departure = target arrival - route estimate
leave home        = planned departure - stop/wait margin
preparation start = leave home - preparation duration
```

The plan always contains exactly three ordered steps: appointment, travel,
preparation. Planning does not write. The appointment stores the summary,
boundaries, destination in `LOCATION`, and optional appointment prose. Travel
covers leave-home through target-arrival and stores origin, destination, mode,
all computed instants, and the three route durations in one deterministic
owned `DESCRIPTION` block. A route URL is stored in travel's `URL` property and
in its matching owned-block line; it is never fetched. Preparation covers the
backward-computed preparation interval. The command does not infer preparation
notes; notes belong on that preparation event.

The three UIDs are generated once when the plan is frozen. The appointment
names both children with `RELATED-TO;RELTYPE=CHILD`, and travel and preparation
each name the appointment with `RELATED-TO;RELTYPE=PARENT`. These relations are
typed standard iCalendar properties, not text encoded as a tuple.

Reschedule only by naming all three exact resources:

```text
ncl cal appointment update <appointment-href> <travel-href> <preparation-href> \
  --from <new-iso> --to <new-iso> --origin <origin> \
  --destination <address> --mode <mode> --route-estimate <duration> \
  --on-site-buffer <duration> --stop-wait-margin <duration> \
  --preparation-duration <duration> [--summary <summary>] \
  [--description <appointment-prose>] [--route-url <directions-uri>]
```

Update reads those exact hrefs, requires strong ETags, checks that they share
one collection, and verifies one direct timed VEVENT at each href, distinct
UIDs, and the reciprocal typed topology. Recurrence, scheduling properties,
extra VEVENTs, all-day or duration-based resources, malformed relations, and a
malformed or missing travel owned block are refused before a plan is stored.
The UID, href, and unrelated authored data survive. Appointment prose and
preparation notes are preserved when their options are omitted. Route URL
omission preserves the existing URL, `--route-url` replaces it, and
`--clear-route-url` removes both the property and the owned-block line.
Authored travel text outside the owned block is preserved; text inside the
block is replaced by the new snapshot.

Appointment bundles use the ordinary non-atomic plan lifecycle. Apply sends
appointment, travel, and preparation PUTs in that order, using
`If-None-Match: *` for creation and each captured `If-Match` ETag for update,
and reads each exact href back before recording it verified. A partial failure
does not roll back an earlier verified event. An uncertain step blocks later
steps until `ncl plan reconcile <plan-id>` classifies it. This slice adds no
bundle delete operation, routing provider, recurrence behavior, or generic
CRUD bundle marker.

Create and update remain plan/apply operations. Updates and deletions require a
strong quoted ETag observed while planning. Applying a calendar write reads the
exact resource back and compares semantic iCalendar content: identity, the
DATE-versus-DATE-TIME boundary type, start and exclusive all-day end values,
unknown properties, nested components, URL, status, and—when requested—the
projected description must remain as planned. Only server-managed `DTSTAMP` and
`LAST-MODIFIED` values may be refreshed. A client that does not render these
fields may still not show them; the projection only improves plain-text
interoperability. Property order does not affect equality, and CATEGORIES
member order is treated as a set; the same component-neutral comparison is
used for VTODO writes.

Exact event reads and calendar `PUT`/`DELETE` requests refuse redirects before
following them. A redirect before a mutation reaches a second target is a
malformed response. A calendar deletion is established only when the exact
href returns 404 after the `DELETE`; a redirect, persistent resource, or
malformed or unexpected post-write readback is outcome-uncertain. The uncertain
step blocks the ordered plan until `ncl plan reconcile <plan-id>` reads the
exact href. Reconciliation compares semantic iCalendar content: an exact
planned create or update is verified, a missing create or an update with its
old strong ETag and different content is pending, and changed conflicting state
remains uncertain. A missing delete is verified; its old strong ETag still
present is pending. Reconciliation reads only.

Plans execute steps in order, record each verified step durably, stop on the
first failure, and never roll back a verified remote effect. Applying resumes
by skipping verified steps. Partial plans do not expire, while untouched plans
retain their short freshness window. Cancelling a partial plan removes local
progress but does not undo remote effects.
`cal create --from` and `--to`, and `cal update --from` and `--to`, accept
`YYYY-MM-DD` for an all-day event and serialize it as `DTSTART;VALUE=DATE` with
an exclusive `DTEND;VALUE=DATE`, so a single day ends on the following date. An
event is all-day on both boundaries or timed on both: mixing a date with an
instant is refused, as is an end that is not after the start. Update needs both
boundaries stated together to convert between the two forms. Nothing infers a
timezone or a local midnight from a date, and readback verifies the stored
boundary kind rather than only the stamps.
