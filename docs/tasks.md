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
not issue a request per task. `task show` reads exactly the requested href.

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

Creation and changes freeze a conditional plan. Creation uses
`If-None-Match: *`; updates, completion, and deletion use the ETag observed
while planning. Successful creates and updates are read back before their
plans are consumed. Completion is a single PUT containing `STATUS:COMPLETED`,
one explicit completion instant, and `PERCENT-COMPLETE:100`.

The modeled mutation fields are summary, description, start, due, priority,
status, percentage, and the parent UID. A due date may be supplied without a
start. Recurrence properties and scheduling properties are listed by name and
refused before any modeled field is changed. Unknown properties, time zones,
and other preserved components survive an unrelated update.
