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

Create and update remain plan/apply operations. Applying a calendar plan uses
the frozen ETag where applicable, reads the resource back, and verifies the
summary, start, structured URL/status, and—when requested—the projected
description. A client that does not render these fields may still not show
them; the projection only improves plain-text interoperability.
