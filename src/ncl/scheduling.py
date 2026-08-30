"""Scheduling identity: whose calendar addresses these are, and which role they hold.

CalDAV implicit scheduling makes storing or deleting a calendar object an act
that can contact other people. Deciding whether that is allowed needs two facts
the ordinary principal model does not carry: the calendar addresses the
authenticated account answers to, and the role that account holds in the
addressed resource.

Both are kept out of :mod:`ncl.identity` on purpose. A server without
scheduling, or a principal whose scheduling properties are absent, still has a
usable calendar home — folding these properties into ordinary discovery would
turn a missing scheduling feature into a failure of `whoami` and every read.

Every refusal here fails closed. A resource whose participants cannot be
resolved to exactly one role for this account is not a resource this tool may
rewrite, because the wrong guess sends mail to real people.
"""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from . import exits
from .identity import CALDAV, DAV, Identity, IdentityError, _element_name, _parse_multistatus
from .identity import _propfind as _identity_propfind
from .session import Session, SessionError, absolute_url

#: The OPTIONS compliance token a server sets when it performs implicit
#: scheduling itself. Without it, a stored change contacts nobody, and a plan
#: that promised to expose recipients at risk would be describing a mechanism
#: the server does not implement.
AUTO_SCHEDULE = "calendar-auto-schedule"

#: The only SCHEDULE-AGENT values under which the server owns delivery. Any
#: other value moves delivery to the client or nowhere, which this tool neither
#: performs nor silently drops.
_SERVER_AGENT = "SERVER"


class SchedulingError(RuntimeError):
    """Scheduling identity could not be established, or the role is not one."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class SchedulingIdentity:
    """One account's scheduling addresses and boxes, all same-origin."""

    principal_url: str
    addresses: tuple[str, ...]
    schedule_inbox: str
    schedule_outbox: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "principal_url": self.principal_url,
            "addresses": list(self.addresses),
            "schedule_inbox": self.schedule_inbox,
            "schedule_outbox": self.schedule_outbox,
        }

    def matches(self, address: str) -> bool:
        return address in self.addresses


@dataclass(frozen=True)
class Role:
    """The single role this account holds in one scheduling object."""

    role: str
    address: str
    organizer: str
    attendees: tuple[str, ...]
    recipients_at_risk: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "address": self.address,
            "organizer": self.organizer,
            "attendees": list(self.attendees),
            "recipients_at_risk": list(self.recipients_at_risk),
        }


def canonical_address(value: Any, *, label: str) -> str:
    """Return a comparable ``mailto:`` address, or refuse the value.

    Comparison is what this is for, so the parts that are case-insensitive are
    folded and the part that is not is left alone: scheme and domain lowercase,
    local part untouched. A mailto carrying headers, more than one ``@``, or an
    empty half addresses no single person and is refused rather than trimmed
    into something that compares equal to a different address.
    """
    text = str(value or "").strip()
    scheme, separator, rest = text.partition(":")
    if not separator or scheme.lower() != "mailto":
        raise SchedulingError(
            f"{label} is not a mailto: calendar address", exits.UNSUPPORTED_STRUCTURE
        )
    if "?" in rest:
        raise SchedulingError(
            f"{label} carries mailto headers rather than a bare address",
            exits.UNSUPPORTED_STRUCTURE,
        )
    try:
        decoded = urllib.parse.unquote(rest, errors="strict")
    except UnicodeDecodeError as exc:
        raise SchedulingError(
            f"{label} is not a valid percent-encoded address", exits.UNSUPPORTED_STRUCTURE
        ) from exc
    local, separator, domain = decoded.partition("@")
    if not separator or not local or not domain or "@" in domain:
        raise SchedulingError(
            f"{label} is not a single addressable mailbox", exits.UNSUPPORTED_STRUCTURE
        )
    if any(character.isspace() for character in decoded):
        raise SchedulingError(
            f"{label} contains whitespace", exits.UNSUPPORTED_STRUCTURE
        )
    return f"mailto:{local}@{domain.lower()}"


def _hrefs(element: ET.Element, *, label: str) -> list[str]:
    values = [
        (child.text or "").strip()
        for child in element
        if _element_name(child) == (DAV, "href")
    ]
    if not values or not all(values):
        raise SchedulingError(f"the {label} property contained an empty href")
    return values


def _single_href(element: ET.Element, *, profile: Any, label: str) -> str:
    """Resolve the one URL a scheduling box property may name.

    A box is a collection this tool would address, so it is held to the same
    origin as every other request: a scheduling inbox on another host would
    take the credential with it.
    """
    values = _hrefs(element, label=label)
    if len(values) != 1:
        raise SchedulingError(f"the {label} property named more than one URL")
    try:
        return absolute_url(profile, values[0])
    except SessionError as exc:
        raise SchedulingError(
            f"the {label} property is outside the configured origin", exc.code
        ) from exc


def _requires_auto_schedule(session: Session, url: str) -> None:
    response = session.request("OPTIONS", url)
    advertised = {
        token.strip().lower() for token in (response.header("DAV") or "").split(",")
    }
    if AUTO_SCHEDULE not in advertised:
        raise SchedulingError(
            f"the server does not advertise {AUTO_SCHEDULE}; it performs no scheduling "
            "for stored calendar objects",
            exits.PRECONDITION_FAILED,
        )


def discover(
    profile: Any,
    *,
    session: Session,
    identity: Identity,
) -> SchedulingIdentity:
    """Resolve the authenticated principal's scheduling addresses and boxes."""
    _requires_auto_schedule(session, identity.principal_url)

    response = _identity_propfind(
        session,
        identity.principal_url,
        "<c:calendar-user-address-set/><c:schedule-inbox-URL/><c:schedule-outbox-URL/>",
    )
    if response.status != 207:
        raise SchedulingError("scheduling discovery did not return Multi-Status")
    wanted = {
        (CALDAV, "calendar-user-address-set"),
        (CALDAV, "schedule-inbox-URL"),
        (CALDAV, "schedule-outbox-URL"),
    }
    try:
        properties = _parse_multistatus(response.body, wanted)
    except IdentityError as exc:
        raise SchedulingError(exc.message, exc.code) from exc

    # A real address set mixes mailto: addresses with the principal's own URL.
    # The URLs are not identities anyone can be invited at, so they are dropped
    # rather than refused; an account with no mailto address at all cannot hold
    # a scheduling role and is refused here instead of at every comparison.
    candidates = _hrefs(
        properties[(CALDAV, "calendar-user-address-set")],
        label="calendar-user-address-set",
    )
    addresses = []
    for candidate in candidates:
        if not candidate.lower().startswith("mailto:"):
            continue
        address = canonical_address(candidate, label="a calendar-user-address-set entry")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise SchedulingError(
            "the principal has no mailto: calendar user address, so it can hold no "
            "scheduling role; a server derives that address from the account's email "
            "address, which is unset",
            exits.PRECONDITION_FAILED,
        )

    return SchedulingIdentity(
        principal_url=identity.principal_url,
        addresses=tuple(sorted(addresses)),
        schedule_inbox=_single_href(
            properties[(CALDAV, "schedule-inbox-URL")],
            profile=profile,
            label="schedule-inbox-URL",
        ),
        schedule_outbox=_single_href(
            properties[(CALDAV, "schedule-outbox-URL")],
            profile=profile,
            label="schedule-outbox-URL",
        ),
    )


def _values(component: Any, name: str) -> list[Any]:
    value = component.get(name)
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _agent_checked(value: Any, *, label: str) -> Any:
    agent = getattr(value, "params", {}).get("SCHEDULE-AGENT")
    if agent is not None and str(agent).strip().upper() != _SERVER_AGENT:
        raise SchedulingError(
            f"{label} sets SCHEDULE-AGENT={str(agent).strip()}; only a server-scheduled "
            "participant is supported",
            exits.UNSUPPORTED_STRUCTURE,
        )
    return value


def classify(component: Any, scheduling: SchedulingIdentity) -> Role:
    """Decide the one role this account holds in a scheduling object.

    The account is the organizer or exactly one attendee, never both and never
    neither. Anything else — a second organizer, an attendee listed twice, an
    address this tool cannot compare — leaves the role undecided, and a role
    that is undecided cannot bound who a write would contact.
    """
    organizers = _values(component, "ORGANIZER")
    if not organizers:
        raise SchedulingError(
            "the event has no ORGANIZER and is not a scheduling object",
            exits.UNSUPPORTED_STRUCTURE,
        )
    if len(organizers) > 1:
        raise SchedulingError(
            "the event names more than one ORGANIZER", exits.UNSUPPORTED_STRUCTURE
        )
    organizer = canonical_address(
        _agent_checked(organizers[0], label="ORGANIZER"), label="ORGANIZER"
    )

    raw_attendees = _values(component, "ATTENDEE")
    if not raw_attendees:
        raise SchedulingError(
            "the event has no ATTENDEE and is not a scheduling object",
            exits.UNSUPPORTED_STRUCTURE,
        )
    attendees: list[str] = []
    for entry in raw_attendees:
        address = canonical_address(
            _agent_checked(entry, label="an ATTENDEE"), label="an ATTENDEE"
        )
        if address in attendees:
            raise SchedulingError(
                f"the event lists {address} as an attendee more than once",
                exits.UNSUPPORTED_STRUCTURE,
            )
        attendees.append(address)

    organizer_owned = scheduling.matches(organizer)
    matched = [address for address in attendees if scheduling.matches(address)]
    if organizer_owned and matched:
        raise SchedulingError(
            "this account is both the organizer and an attendee of the event, so its "
            "role is ambiguous",
            exits.AMBIGUOUS_TARGET,
        )
    if organizer_owned:
        return Role(
            role="organizer",
            address=organizer,
            organizer=organizer,
            attendees=tuple(attendees),
            recipients_at_risk=tuple(sorted(set(attendees))),
        )
    if len(matched) > 1:
        raise SchedulingError(
            "this account matches more than one attendee of the event",
            exits.AMBIGUOUS_TARGET,
        )
    if matched:
        return Role(
            role="attendee",
            address=matched[0],
            organizer=organizer,
            attendees=tuple(attendees),
            recipients_at_risk=(organizer,),
        )
    raise SchedulingError(
        "this account is neither the organizer nor an attendee of the event",
        exits.UNSUPPORTED_STRUCTURE,
    )
