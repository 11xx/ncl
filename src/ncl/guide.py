"""The bare-``ncl`` guide: orientation before the first command."""

from __future__ import annotations

GUIDE = """ncl — use a Nextcloud instance programmatically, from a command line.

`ncl` is a deterministic surface for an agent. It speaks Nextcloud's protocols
directly instead of driving a browser, and is deliberately neither an MCP
server nor browser automation.

It reaches calendars through CalDAV and files through WebDAV. Profiles, scope
allowlists, exit codes, the secret backend, and the plan/apply boundary belong
to the whole tool rather than either module.

THE ORDER
  ncl doctor                    Local preconditions. Run it first; it reports
                                each failure in terms of the command that fixes
                                it.
  ncl login                     Browser consent, once. Prints a URL and waits.
  ncl whoami                    Prove the credential, and see which account and
                                calendar home it actually reached.
  ncl whoami --scheduling       Also resolve the scheduling addresses and boxes,
                                which only a server that schedules has. Ordinary
                                reads never need them, so this is opt-in.
  ncl logout                    Revoke server-side, then forget locally.

FIND A CALENDAR BEFORE NAMING ONE
  ncl cal list                  Every calendar under the calendar home: its
                                href, whether it is writable, and whether the
                                allowlist admits it.

  Address a calendar by href. A display name is chosen by the user and is not
  unique, so a command given one refuses when two calendars share it rather
  than guessing between them. A short href target matches only an exact final
  path segment: `work` never selects `subwork`.

  Listing reports the allowlist decision instead of filtering by it — a listing
  that hid everything unconfigured could not be used to configure anything.

READ
  ncl cal events <calendar> --from <iso> --to <iso>
  ncl cal occurrences <calendar> --from <iso> --to <iso>
  ncl cal show <event-href>
  ncl cal collection <calendar>
  ncl cal freebusy --from <iso> --to <iso> [--attendee mailto:...]

  The window is required, and times need an explicit UTC offset: a local time
  is ambiguous across one DST transition each year and nonexistent across the
  other. JSON event references carry every property a write can set — `url`,
  `status`, `location`, `description`, `categories`, `priority`, `class`,
  `transp`, `color`, and `alarms` — unfolded and unescaped, using
  an empty value when the VEVENT property is absent, so a mutation can be read
  back without parsing the raw `icalendar` blob. Human `cal show` displays the URL, while
  a cancelled event is marked in a human listing without putting links on every
  row. `cal occurrences` expands validated recurring resources in the bounded
  window and emits the exact reusable `RECURRENCE-ID` wire identity, including
  whether the item is the master or an override and whether it is cancelled.
  DATE events without an end occupy one day. Explicit DTEND durations remain
  exact across timezone transitions, while DURATION remains nominal. Ambiguous
  or nonexistent TZID local boundaries and generated occurrences are refused.
  Generic event references still mark recurrence and scheduling structure as
  unwritable; only the explicit target modes below may edit a recurring resource.
  `cal freebusy` asks the scheduling outbox when someone is busy. It is a read
  — nothing is stored and nobody is notified — so it needs no plan and no
  consent, but it does need scheduling addresses, which a server derives from
  an account's email address. Named with no attendee the question is about this
  account. An answer reports intervals and their transparency and nothing else,
  because the server is answering for a calendar the caller may not read. A
  recipient the server declined to answer for is reported as unanswered and
  never as an empty schedule: those two readings differ by exactly the meeting
  the answer would be used to book.

  `cal collection` answers what a calendar is rather than how to address it:
  its description, its colour as the server spells it, and the component set it
  accepts. That set is worth reading before writing into a collection someone
  else made — most servers fix it at creation, so a calendar without VTODO can
  never host a task.

UNDO A DELETION OR AN OVERWRITE
  ncl trash list
  ncl trash restore <trash-href>
  ncl trash purge <trash-href>
  ncl versions list <file-href>
  ncl versions restore <file-href> <version-id>

  The allowlist question here is the inverted one: a trash entry lives under
  `/trashbin/`, which nothing allowlists, so what is bounded is where the file
  came from and where restoring would put it back. An entry from outside the
  allowlist is listed — seeing what was deleted is a read — and never restored.

  A restore onto an occupied path is refused before anything is sent: the
  server would restore beside the occupant under a name nobody asked for, so
  the plan could not keep its promise. Move what is there first.

  `trash purge` is the narrowest gate in the tool. Everything else leaves a
  copy somewhere; this removes the last one, and the bin is where the remedy
  for every other deletion lives. Restoring a version, by contrast, is safe:
  the content it replaces becomes a version in turn.

SEE AND CHANGE WHO ELSE CAN REACH A FILE
  ncl share list [<href>] [--subfiles]
  ncl share show <id>
  ncl share create <href> (--public | --user <uid> | --group <gid>)
  ncl share delete <id>

  A share is the only mutation here that hands a resource to somebody else, and
  it is invisible from the resource itself: reading the file afterwards looks
  the same whether or not the world can also read it. So creating one is
  planned and applied like a write, and the preview names who would gain access
  and at what permission.

  An unscoped listing reports every share this account made, including over
  paths the allowlist does not admit and types this tool cannot create. The
  allowlist bounds what may be reached; a listing that hid a public link
  because its path was unconfigured would answer the wrong question.

  A link password comes from `--password-from <file>`, never an argument, and
  its length is withheld from plan output along with its value. `--permissions`
  grants `read` unless told otherwise, and applying reports any permission the
  server granted beyond the plan — Nextcloud adds the share bit to every public
  link.

READ CONTACTS
  ncl contacts books
  ncl contacts list <book>
  ncl contacts find <book> <term>
  ncl contacts show <contact-href>

  Address books are DAV collections beside the calendars, bounded by their own
  `addressbooks` allowlist. That key is optional, and absent means none is
  reachable — an unstated scope is empty, never open.

  A contact is addressed by href. Two people share a name far more often than
  two events share a summary, so a contact resolved by name is the wrong person
  rather than a missing one. Listing makes one bounded report, and a card's
  photo is reported as present rather than inlined: it is routinely a hundred
  kilobytes of base64 and answers nothing that was asked. `contacts show`
  returns the raw vCard beside the typed view, and structure the view does not
  model is named rather than dropped. Contacts are read-only here.

WORK WITH TASKS
  ncl task list <calendar> [--status <status>]
  ncl task show <task-href>

  Tasks are VTODO components in a calendar collection. The selected collection
  must advertise VTODO, and a status filter can be repeated. Listing makes one
  bounded CalDAV report and derives parent/child UIDs from that response; it
  does not read each task separately. Every response entry must carry an href
  and successful calendar data, and every resource must contain exactly one
  VTODO as a direct child of VCALENDAR, with the resource exactly one direct
  child of the selected collection and inside the profile allowlist. A VTODO
  nested inside another component is malformed; direct sibling components are
  readable but named unsupported. Missing task properties are shown as empty
  values. Singleton VTODO properties cannot repeat except REFID, which is
  repeatable per RFC 9253 and is validated per resource for runs. DUE and
  DURATION cannot coexist, and an existing COMPLETED value must be UTC.
  Duplicate UIDs and parent cycles fail closed as ambiguous; a parent outside
  the report remains visible by UID rather than being guessed or rejected.

  ncl task create <calendar> --summary S [--start <iso>] [--due <iso>]
  ncl task update <task-href> --summary S
  ncl task complete <task-href>
  ncl task delete <task-href>

  Task writes use the same ordinary frozen plan/apply boundary as calendar
  writes, and applying claims the plan before reading it. A due date does not
  require a start. Creation uses If-None-Match: *, while update, completion,
  and deletion use a strong quoted ETag. Completion is one conditional write
  that sets STATUS to COMPLETED, records one UTC COMPLETED instant, and sets
  PERCENT-COMPLETE to 100.

  Every task GET, PUT, and DELETE stays on its exact href. Creates, updates,
  and completion are read back and semantically compared, preserving unknown
  properties and nested components; only DTSTAMP and LAST-MODIFIED may be
  refreshed by the server. A deletion is verified only when the exact href
  returns 404. Redirects, persistent targets, and uncertain readbacks mark
  the step uncertain and block the plan until `ncl plan reconcile` reads the
  exact resource. Recurrence, scheduling structures, positive DURATION, and
  unsupported sibling components remain readable but are refused for mutation;
  a DATE DTSTART can use DURATION only in whole days or weeks.

PLAN APPOINTMENT TRAVEL
  ncl cal appointment create <calendar> --summary S --from <iso> --to <iso> \\
    --origin O --destination D --mode M --route-estimate <duration> \\
    --on-site-buffer <duration> --stop-wait-margin <duration> \\
    --preparation-duration <duration> [--route-url U]
  ncl cal appointment update <appointment-href> <travel-href> <preparation-href> \\
    --from <iso> --to <iso> --origin O --destination D --mode M \\
    --route-estimate <duration> --on-site-buffer <duration> \\
    --stop-wait-margin <duration> --preparation-duration <duration>

  These commands freeze one three-step bundle in appointment, travel,
  preparation order. Start and end need explicit offsets and whole seconds.
  Route estimate and preparation duration are positive RFC 5545 day/time
  durations; on-site buffer and stop/wait margin are nonnegative. The tool
  calculates target arrival, planned departure, leave-home, and preparation
  start backwards from the appointment start, using no routing provider.

  The appointment stores its summary, exact destination in LOCATION, and
  optional prose. Travel stores the stated route facts in one owned
  DESCRIPTION block and, when present, the route URL verbatim. Preparation is
  the event for preparation notes. The three generated UIDs are linked by
  reciprocal typed RELATED-TO CHILD/PARENT properties. The plan output exposes
  metadata only; it does not expose the frozen iCalendar bodies.

  Update takes the three exact hrefs and strong ETags observed by its reads.
  It refuses a different collection, recurrence or scheduling structure,
  extra VEVENTs, all-day or duration-based events, broken relations, or a
  malformed travel block. Omitted route URL preserves it; --route-url replaces
  it; --clear-route-url removes it. Authored text outside the travel block and
  unmentioned event data survive. Apply uses the ordinary resumable lifecycle:
  each exact PUT is followed by a readback, verified steps are skipped on
  resume, and an uncertain step blocks later steps until reconciliation.

ORDERED CHECKPOINT RUNS
  ncl task run create <calendar> --summary S --step A --step B
  ncl task run list <calendar>
  ncl task run show <root-href>
  ncl task run add <root-href> --summary S [--before STEP-HREF | --after STEP-HREF]
  ncl task run reorder <root-href> --step STEP-HREF --step STEP-HREF
  ncl task run edit <root-or-step-href> --summary S
  ncl task run done|skip|not-yet <step-href>

  A run is one manifest VTODO and a linear chain of checkpoint VTODOs in the
  same allowlisted collection. The manifest and every checkpoint carry the
  same standard `REFID`: the manifest UID. `FIRST`, `PARENT`, and `NEXT`
  relations use explicit `VALUE=UID`; an optional nonnegative `GAP` belongs
  only to `NEXT`. Listing and showing validate the whole graph from one
  depth-one report and derive the current checkpoint as the first one whose
  status is neither `COMPLETED` nor `CANCELLED`.

  Create, add, reorder, and edit are ordinary plans. Creation writes the
  checkpoints in chain order and the manifest last; insertion writes the new
  checkpoint before changed edge resources and the manifest last when FIRST
  changes; reorder writes changed edges in final order and the manifest last
  when needed. Authoring is refused once any checkpoint has left
  `NEEDS-ACTION`. There is no run deletion, offline queue, or automatic
  `IN-PROCESS` transition.

  `done`, `skip`, and `not-yet` are also frozen plans. Done records one UTC
  completion instant and 100 percent; skip cancels without a completion
  timestamp; not-yet returns an in-process checkpoint to NEEDS-ACTION and is a
  no-op when it is already there. Terminal transitions conflict, while done
  and skip may be recorded out of order and report that fact. An ordinary task
  update, completion, or deletion refuses a VTODO carrying REFID; use the run
  commands to preserve the graph.

  These standard relationships are stored and validated as data. The guide
  does not promise that a Nextcloud web or mobile client displays their order;
  that depends on a live server and client probe.

REACH ALLOWLISTED FILES
  ncl files list <collection-href>  List one collection, without recursion.
  ncl files find <collection-href>  Search a subtree server-side, by name,
                                    media type, or modification time.
  ncl files stat <resource-href>    Read size, mtime, media type, and ETag.
  ncl files read <file-href>        Read UTF-8 text through redacted stdout.
  ncl files read <file-href> --offset <n> --length <n> --output <path>

  File hrefs must be inside one of the profile's `files_roots`. Only content
  declared as textual and decodable as UTF-8 is sent to redacted stdout; binary,
  A write larger than a few megabytes streams in parts through the server's
  chunked upload rather than freezing its bytes in the plan. What the plan
  freezes then is the source's size and SHA-256, and applying refuses if the
  file changed underneath — nothing reaches the destination until the final
  assembling step, so a refusal leaves it untouched. A ranged read writes its
  window at its own offset, and a server that ignores the range and sends the
  whole file is refused rather than misread.

  `files find` asks the server where a file is rather than walking the tree
  for it, and needs at least one condition: a search with none is a recursive
  listing at a cost nobody asked for. `*` and `?` are the name wildcards. A
  result outside the searched subtree or outside the allowlist is a refusal,
  not a filtered row — the server chose what matched, so its answer is where
  a scope escape would arrive.

  unknown, or non-UTF-8 content requires `files read --output <path>` to write
  its exact bytes to a local file. Every file request stays on its exact href
  and refuses redirects. An existing output path is refused unless `--force`
  is explicit.

CHANGE NOTHING BY ACCIDENT
  ncl cal create <calendar> --summary S --from <iso> --to <iso>
  ncl cal create <calendar> --summary S --from <date> --to <exclusive-date>
  ncl cal update <event-href> --target resource --summary S
  ncl cal update <event-href> --target series --summary S
  ncl cal update <event-href> --target occurrence --recurrence-id <wire-id> --summary S
  ncl cal update <event-href> --target this-and-future --recurrence-id <wire-id> --summary S
  ncl cal delete <event-href> --target resource|series
  ncl cal delete <event-href> --target occurrence --recurrence-id <wire-id>
  ncl cal delete <event-href> --target this-and-future --recurrence-id <wire-id>
  ncl cal mkcalendar <calendar-href> --displayname <name>
  ncl cal move <event-href> --to <calendar>
  ncl files write <file-href> --from <local-path>
  ncl files move <file-href> --to <file-href>
  ncl files mkcol <collection-href>
  ncl files delete <file-href>

  None of these change anything. Each resolves its target, freezes exactly what
  it would do, prints it, and exits 40 with a plan id. The server is untouched
  until:

  ncl apply <plan-id>           Execute or resume that frozen plan.
  ncl plan list|show|cancel     Inspect or discard a plan.
  ncl plan reconcile <plan-id>  Read the first uncertain step and classify it.

  A move relocates the resource itself, which delete-and-recreate cannot: two
  mutations leave a window where a failure loses the resource, reconstruct only
  the properties this tool models, and either mint a new identity or leave a
  synced client reconciling a tombstone against a fresh resource. Both endpoints
  are checked against the allowlist, the resource keeps its final path segment
  so existing references still resolve, and `Overwrite: F` means an occupied
  destination refuses instead of being replaced. A move addresses the resource,
  so a recurring master travels with every override that shares its file and no
  occurrence can be moved away from its series. A file move freezes the exact
  content it read while planning and holds the destination to that identity
  afterwards, so a same-sized replacement cannot pass as the file that moved.

  Creating a collection is the one mutation that enlarges what the tool can
  reach, because the allowlist is a prefix list and a new collection under an
  allowed prefix is admitted as soon as it exists. `cal mkcalendar` accepts
  both VEVENT and VTODO unless `--component` says otherwise, since that set is
  usually fixed at creation. Deleting a collection has no verb, for the same
  reason as `files delete` on one: it destroys unbounded content that no plan
  can show.

  A plan contains one or more ordered frozen steps. Applying claims the plan
  before loading it, validates every action and step before the first request,
  then executes pending steps in order. A verified step is durably recorded and
  skipped on resume. The loop stops at the first failure, never rolls back a
  verified remote effect, and does not describe the bundle as atomic. A plan
  with any verified or uncertain progress does not expire; an untouched plan
  retains its short freshness window. The plan fingerprints the selected
  profile's origin, backend, calendars, and file roots; changing that state
  makes the plan stale before any request.

  Recurrence series updates change only the master and preserve every override.
  Occurrence updates address the original wire identity, preserve its UID, and
  create a cancelled exception for occurrence deletion. This-and-future uses a
  new UID and freezes the future-resource creation first; the preview states
  that a partial failure can temporarily duplicate future occurrences. Resume
  and reconcile use the same ordered plan lifecycle as every other mutation.

  Creation is conditional on nothing being there; replacement and deletion are
  conditional on the strong ETag read while planning, so a change that landed
  in between conflicts instead of being overwritten. An uncertain step blocks
  apply until `ncl plan reconcile` reads its exact href. Reconciliation marks
  exact planned content verified, returns a demonstrably absent or unchanged
  effect to pending for retry, and keeps changed or conflicting state
  uncertain. Reconciliation reads only; it never retries or writes the
  mutation.

  Applying claims the plan before loading it: a simultaneous claimant is
  locked out, and a plan consumed by another caller is not dispatched from an
  older observation. Cancelling a partial plan removes only local state;
  verified remote effects are not undone.

  After a calendar write the exact resource is read back and compared using
  semantic iCalendar content. Unknown properties, nested components, boundary
  values, URL, status, and any opted-in portable description must survive as
  planned; only server-managed DTSTAMP and LAST-MODIFIED values may be
  refreshed. Exact event reads and calendar PUT/DELETE requests refuse
  redirects before following them. A redirect before a mutation reaches a
  second target is malformed. A calendar deletion is established only when
  the exact href returns 404 after DELETE. A redirect, persistent resource, or
  malformed post-write readback marks the step uncertain and returns the
  outcome-uncertain code. Collection deletion is refused.

  This boundary is not authorization. `ncl` cannot tell whether a plan id came
  from whoever read the preview or from the agent that produced it. Where a
  human's approval is genuinely required, stop after planning and wait to be
  told to continue.

  `cal create` and `cal update` accept `--portable-description` when a caller
  wants selected structured fields copied into a deterministic plain-text block
  in DESCRIPTION. The block contains LOCATION, URL, STATUS, CATEGORIES,
  PRIORITY, TRANSP, CLASS, and VALARM trigger values in that order. It is an
  opt-in compatibility projection: the VEVENT properties remain canonical, a
  URL is never fetched, and prose outside the tool-owned block survives. A
  malformed, duplicated, nested, reversed, or unpaired block is refused.

  On update, omitting `--alarm` preserves existing reminders, `--alarm` replaces
  them with the repeatable offsets supplied, and `--clear-alarms` removes all of
  them. The two options are mutually exclusive. `--alarm` takes durations only,
  so a reminder read back as a relative trigger can be written again unchanged
  while one read back as an absolute instant is read-only: preserving it means
  leaving both options off. A calendar client may still
  hide URL or other structured fields; the projection only makes them readable
  to more clients and cannot force a client to display them.

WHAT IT REFUSES
  Scope is an allowlist of hrefs in configuration. The server issues no scoped
  credentials, so that allowlist is the only scope boundary that exists — a
  request outside it is refused here or nowhere.

  Every command takes `--json`. Exit codes are the contract and say what to do
  next: configure, log in, re-read, reconcile, or stop. A refusal prints what
  happened and then that standing answer, so a caller reading stderr is not
  left holding a number whose meaning lives in a table it was never shown. `ncl doctor --json`
  ships the chosen exit code and remediation alongside the checks it ran.
  Remote credential, server, and malformed-response failures retain their
  actionable codes instead of becoming a generic local failure.

  Scheduling structures remain refused, as do EXRULE, period-valued RDATE,
  multiple RRULEs, incompatible recurrence value kinds or timezones, RANGE=
  THISANDFUTURE, time-valued RRULE parts on DATE events, unsupported nested
  components, recurrence-time shifts, complex or unbounded future partitions
  whose identity mapping cannot be proven. Attendee and organizer edits are
  outside this recurrence slice.

Use `ncl <command> --help` for the exact arguments a command accepts.
"""


def render() -> str:
    """Return the guide printed by bare ``ncl``."""
    return GUIDE
