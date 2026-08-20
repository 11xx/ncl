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
not issue a request per task. Every REPORT response entry needs an href naming
exactly one direct child resource of the selected collection and profile
allowlist, plus successful calendar data. Each resource must contain exactly
one VTODO as a direct child of VCALENDAR, with a nonempty UID, valid timestamp
and modeled value types, an allowed status, priority, and percentage. A VTODO
nested inside another component is malformed; direct sibling components remain
readable and are named as unsupported. RFC singleton properties cannot repeat except `REFID` (repeatable per RFC 9253,
validated per-resource for runs), `DUE` and `DURATION` cannot coexist, and an existing `COMPLETED` value must be
a UTC date-time. Duplicate UIDs and parent cycles are ambiguous and fail
closed; a parent UID not present in the collection remains visible.
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
readback marks the step outcome-uncertain and blocks the plan. `ncl plan
reconcile <plan-id>` reads the first uncertain task: exact planned content is
verified; a missing create or an old strong ETag with different update content
is pending; a changed or conflicting resource remains uncertain. A missing
delete is verified, its old strong ETag still present is pending, and changed
state remains uncertain. Reconciliation sends reads only. Completion is a
single PUT containing `STATUS:COMPLETED`, one UTC completion instant, and
`PERCENT-COMPLETE:100`.

Plans are ordered and non-atomic. Every action is validated before the first
request, verified steps are recorded and skipped when resuming, and execution
stops at the first failure without rolling back earlier remote effects. Any
partial plan does not expire; an untouched plan retains its short freshness
window. Cancelling a partial plan removes local state but does not undo remote
effects.

The modeled mutation fields are summary, description, start, due, priority,
status, percentage, and the parent UID. A due date may be supplied without a
start. `DURATION` is listed as unsupported: a positive duration-based task
remains readable but cannot be modified, so its duration is never dropped or
combined with a due date. A task with a DATE `DTSTART` may use only a whole-day
duration, including a whole number of weeks. Recurrence properties and
scheduling properties are listed by name and refused before any modeled field
is changed. Unsupported sibling components remain readable but unwritable.
Unknown properties, time zones, and supported nested components survive an
unrelated update. List and create first require
the resolved collection to advertise `VTODO`; show and mutations address an
existing task directly, so they do not perform redundant collection discovery.

## Ordered checkpoint runs

```text
ncl task run create <calendar> --summary "Errand" --step "Leave" --step "Arrive"
ncl task run list <calendar>
ncl task run show <root-href>
ncl task run add <root-href> --summary "Collect" --after <step-href>
ncl task run reorder <root-href> --step <step-href> --step <step-href>
ncl task run edit <root-or-step-href> --summary "Updated"
ncl task run done <step-href> [--at <iso>]
ncl task run skip <step-href>
ncl task run not-yet <step-href>
```

A run is a rooted standard iCalendar graph in one allowlisted VTODO
collection. The root is a manifest with the run summary and one
`RELATED-TO;RELTYPE=FIRST;VALUE=UID` relation. Each checkpoint carries the
root UID in `REFID`, exactly one `RELATED-TO;RELTYPE=PARENT;VALUE=UID`, and at
most one forward `RELATED-TO;RELTYPE=NEXT;VALUE=UID` relation. A `NEXT` edge
may carry a nonnegative `GAP` duration; an absent gap is reported as
`unknown`. The root has no aggregate status, percentage, or completion time.

`run list` performs one depth-one VTODO REPORT and returns every validated
root in the selected collection. `run show` derives the collection from the
root href and validates that root and all members from one report. Both refuse
duplicate or missing UIDs and edges, wrong or repeated parent relations,
branches, cycles, disconnected members, mismatched `REFID`, URI or invalid
relation parameters, malformed checkpoint state, cross-collection targets,
recurrence or scheduling data, and unsupported sibling/nested structures. The
current checkpoint is derived as the first ordered checkpoint whose status is
neither `COMPLETED` nor `CANCELLED`; it becomes null when all checkpoints are
terminal. A root is never followed through guessed checkpoint hrefs.

Create writes checkpoints in chain order and the root last. Add writes the new
checkpoint, changed neighboring edge resources, and the root last when FIRST
changes. Reorder writes changed checkpoint edges in final order and the root
last when FIRST changes. Every write is an ordinary ordered frozen plan with
strong ETags for existing resources, exact conditional headers, semantic
readback, resumable progress, and the same uncertainty/reconcile behavior as
other task writes. The complete graph is validated before any apply request.
Insertion gaps must be explicit when a predecessor or successor exists;
`reorder --gap` has exactly one entry per adjacent pair when supplied.

Authoring is available only while every checkpoint is `NEEDS-ACTION` (an
absent status has that meaning). `edit` changes only ordinary summary,
description, start, due, and priority fields. It cannot change UID, REFID,
relationships, status, percentage, or completion. Once execution starts,
add, reorder, and edit are refused. There is no run deletion, offline queue,
automatic `IN-PROCESS` transition, appointment change, recurrence/scheduling
edit, WebDAV operation, or client-visibility guarantee.

`done` accepts `NEEDS-ACTION` or `IN-PROCESS`, writes `COMPLETED`, one UTC
completion instant, and 100 percent. `skip` writes `CANCELLED`, removes
`COMPLETED`, and preserves a percentage below 100. `not-yet` accepts
`IN-PROCESS`, writes `NEEDS-ACTION` and zero percent, and removes
`COMPLETED`; it is an idempotent no-op for an already `NEEDS-ACTION`
checkpoint. Terminal transitions conflict. Done and skip may be out of order,
and the plan reports that fact while current position remains derived from
the earliest nonterminal checkpoint.

Run relationships and `REFID` are standard data, not private `X-` properties.
Unknown properties and supported nested alarms that the run contract does not
own are retained by content, edge, and state edits. Ordinary `task update`,
`task complete`, and `task delete` refuse REFID-bearing VTODOs so generic CRUD
cannot bypass run integrity. These relationships are stored and validated;
this documentation does not claim that a Nextcloud web or mobile client
displays their order without a live server/client probe.
