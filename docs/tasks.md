# Tasks over CalDAV

`ncl task` reads and writes `VTODO` components in the same CalDAV collections
that `ncl cal` discovers. The collection is addressed by href or by an
unambiguous display name, and it must advertise `VTODO` in
`supported-calendar-component-set`. A collection that advertises only
`VEVENT` is refused with the unsupported-collection exit code.

## Reading

```text
ncl task list <calendar>
ncl task list <calendar> --status needs-action --status in-process
ncl task show <task-href>
```

`task list` sends one depth-one `calendar-query` REPORT filtered to `VTODO`.
Status filters are applied to that response, and parent/child relationships
come from each task's `RELATED-TO;RELTYPE=PARENT` property. The command does
not issue a request per task. Every REPORT response entry needs an href inside
the selected collection and profile allowlist, plus successful calendar data.
Each resource must contain exactly one VTODO with a nonempty UID, valid
timestamp and modeled value types, an allowed status, priority, and percentage.
RFC singleton properties cannot repeat, `DUE` and `DURATION` cannot coexist,
and an existing `COMPLETED` value must be a UTC date-time. Duplicate UIDs and
parent cycles are ambiguous and fail closed; a parent UID not present in the
collection remains visible.
`task show` reads exactly the requested href without collection discovery.

Structured output includes href, calendar href, UID, summary, description,
`DTSTART`, `DUE`, `COMPLETED`, `PERCENT-COMPLETE`, `STATUS`, `PRIORITY`, the
parent UID, ETag, writability, children, and named unsupported structures.
Absent modeled properties remain explicit empty values.

## Writes

```text
ncl task create <calendar> --summary "Review" --due 2026-09-03
ncl task update <task-href> --description "Bring the receipt"
ncl task complete <task-href>
ncl task delete <task-href>
ncl apply <plan-id>
```

All task creates, updates, completions, and deletions freeze ordinary plans;
none is a direct or automatic tick operation. `ncl apply` claims the plan
before reading it, then sends the frozen request once. Creation uses
`If-None-Match: *`; updates, completion, and deletion require the strong
quoted ETag observed while planning. A weak, wildcard, missing, or unquoted
ETag is refused.

Every exact task GET, PUT, and DELETE refuses redirects before following them.
Successful creates, updates, and completions read back the exact href and
compare semantic iCalendar content, including unknown properties and nested
components. Property order is irrelevant, and CATEGORIES member order is
treated as a set; only server-managed `DTSTAMP` and `LAST-MODIFIED` refreshes
are allowed. A delete is successful only after a GET of the exact href returns
404. A redirect, persistent target, malformed readback, or unreachable
readback returns outcome-uncertain and leaves the plan pending. Completion is
a single PUT containing `STATUS:COMPLETED`, one UTC completion instant, and
`PERCENT-COMPLETE:100`.

The modeled mutation fields are summary, description, start, due, priority,
status, percentage, and the parent UID. A due date may be supplied without a
start. `DURATION` is listed as unsupported: a valid duration-based task remains
readable but cannot be modified, so its duration is never dropped or combined
with a due date. Recurrence properties and scheduling properties are listed by
name and refused before any modeled field is changed. Unsupported sibling
components remain readable but unwritable. Unknown properties, time zones, and
nested components survive an unrelated update. List and create first require
the resolved collection to advertise `VTODO`; show and mutations address an
existing task directly, so they do not perform redundant collection discovery.
