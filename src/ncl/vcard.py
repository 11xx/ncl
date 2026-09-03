"""Reading vCard, which is not iCalendar however similar it looks.

Both are line-folded property lists with parameters, and there the resemblance
stops: vCard has its own escaping, its own structured values, and a `PHOTO`
that is routinely a hundred kilobytes of base64 sitting between two ordinary
text properties. Parsing it with a calendar library produces something that
looks right until a value contains a semicolon.

This reads the properties a contact is *addressed* and *searched* by, and
leaves everything else in the raw card. It is deliberately not a writer: a
partial model that round-trips would drop whatever it does not know, and the
raw bytes are returned alongside so nothing is lost to a caller that needs
more.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from . import exits


class VcardError(RuntimeError):
    """A vCard could not be read."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


#: Properties whose values are large binary blobs. They are reported as
#: present rather than as content: a listing that inlined a photo would be
#: dominated by base64 nobody asked for, and a terminal would be unusable.
BINARY_PROPERTIES = frozenset({"PHOTO", "LOGO", "SOUND", "KEY"})


def escape(value: str) -> str:
    """Escape one vCard text value."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold(line: str) -> str:
    """Fold a logical line without splitting a UTF-8 code point."""
    chunks: list[str] = []
    current = ""
    limit = 75
    for character in line:
        encoded = (current + character).encode("utf-8")
        if current and len(encoded) > limit:
            chunks.append(current)
            current = character
            limit = 74
        else:
            current += character
    chunks.append(current)
    return "\r\n ".join(chunks)


def render_property(name: str, value: str, parameters: Mapping[str, str] = {}) -> str:
    """Render one folded vCard property line."""
    prefix = name + "".join(f";{key}={item}" for key, item in parameters.items())
    return fold(f"{prefix}:{value}")


def build(*, uid: str, fields: Mapping[str, object]) -> bytes:
    """Build one vCard 3.0 resource from writable fields."""
    full_name = str(fields.get("fn") or "")
    if not full_name:
        raise VcardError("FN is required", exits.USAGE)
    lines = ["BEGIN:VCARD", "VERSION:3.0", render_property("UID", escape(uid))]
    lines.append(render_property("FN", escape(full_name)))
    family = escape(str(fields.get("family") or ""))
    given = escape(str(fields.get("given") or ""))
    lines.append(render_property("N", f"{family};{given};;;"))
    for field, name in (("email", "EMAIL"), ("tel", "TEL")):
        for value in fields.get(field, ()) or ():
            lines.append(render_property(name, escape(str(value))))
    for field, name in (("org", "ORG"), ("title", "TITLE"), ("note", "NOTE"), ("birthday", "BDAY")):
        value = fields.get(field)
        if value:
            lines.append(render_property(name, escape(str(value))))
    categories = fields.get("categories", ()) or ()
    if categories:
        lines.append(render_property("CATEGORIES", ",".join(escape(str(v)) for v in categories)))
    lines.append("END:VCARD")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def splice(raw: bytes, replacements: Mapping[str, tuple[str, ...] | None]) -> bytes:
    """Replace top-level properties while preserving every unrelated byte.

    Replacing a grouped property, `item1.EMAIL`, also removes the other
    properties carrying that group. A group holds the labels describing one
    value, and a label kept beside a replaced value describes something the
    card does not carry.
    """
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VcardError("the vCard was not valid UTF-8") from exc
    ending = b"\r\n" if b"\r\n" in raw else b"\n"
    physical = raw.splitlines(keepends=True)
    spans: list[tuple[int, int, str, str]] = []
    depth = 0
    card_end = -1
    index = 0
    while index < len(physical):
        start = index
        index += 1
        while index < len(physical) and physical[index][:1] in {b" ", b"\t"}:
            index += 1
        logical = b"".join(part.rstrip(b"\r\n") for part in physical[start:index])
        logical = logical.replace(b"\r", b"")
        segment, separator, value = logical.partition(b":")
        if not separator:
            continue
        prefix, _, raw_name = segment.split(b";", 1)[0].rpartition(b".")
        name = raw_name.decode("utf-8").strip().upper()
        group = prefix.decode("utf-8").strip().upper()
        if name == "BEGIN":
            depth += 1
        if depth == 1 and name not in {"BEGIN", "END"}:
            spans.append((start, index, name, group))
        if name == "END" and depth == 1:
            card_end = start
        if name == "END":
            depth -= 1
    if card_end < 0:
        raise VcardError("the vCard did not close the card it opened")
    wanted = {name.upper(): values for name, values in replacements.items()}
    first = {name: next((s for s in spans if s[2] == name), None) for name in wanted}
    replaced_groups = {group for _, _, name, group in spans if name in wanted and group}
    output: list[bytes] = []
    span_at = {
        start: (end, name if name in wanted else None)
        for start, end, name, group in spans
        if name in wanted or group in replaced_groups
    }
    inserted: set[str] = set()
    index = 0
    while index < len(physical):
        span = span_at.get(index)
        if span:
            end, name = span
            if name is None:
                index = end
                continue
            if name not in inserted:
                values = wanted[name]
                if values is not None:
                    for value in values:
                        output.append(
                            render_property(name, value).replace("\r\n", ending.decode()).encode()
                            + ending
                        )
                inserted.add(name)
            index = end
            continue
        if index == card_end:
            for name, values in wanted.items():
                if name in inserted or first[name] is not None:
                    continue
                if values is not None:
                    for value in values:
                        output.append(
                            render_property(name, value).replace("\r\n", ending.decode()).encode()
                            + ending
                        )
                inserted.add(name)
        output.append(physical[index])
        index += 1
    return b"".join(output)


@dataclass(frozen=True)
class Property:
    """One parsed vCard property line."""

    name: str
    parameters: dict[str, tuple[str, ...]]
    value: str

    def types(self) -> tuple[str, ...]:
        return tuple(item.upper() for item in self.parameters.get("TYPE", ()))


def unfold(raw: bytes) -> list[str]:
    """Undo vCard line folding.

    A continuation is a line beginning with a space or a tab, and the leading
    whitespace character is part of the folding rather than of the value.
    Folding can land anywhere, including inside a base64 blob, so this happens
    before anything else looks at a line.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VcardError("the vCard was not valid UTF-8") from exc
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in {" ", "\t"} and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return [line for line in lines if line.strip()]


def _unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            following = value[index + 1]
            out.append({"n": "\n", "N": "\n"}.get(following, following))
            index += 2
            continue
        out.append(character)
        index += 1
    return "".join(out)


def split_values(value: str) -> tuple[str, ...]:
    """Split a comma-separated value, honouring escapes.

    An escaped comma belongs to the value. Splitting first and unescaping
    afterwards turns one address containing a comma into two addresses, which
    is the kind of error that is only noticed by the person who did not get
    the mail.
    """
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            current.append(value[index : index + 2])
            index += 2
            continue
        if character == ",":
            parts.append("".join(current))
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    parts.append("".join(current))
    return tuple(_unescape(part).strip() for part in parts if part.strip())


def _parameters(segment: str) -> tuple[str, dict[str, tuple[str, ...]]]:
    """Read a property name and its parameters from the part before the colon."""
    pieces: list[str] = []
    current: list[str] = []
    quoted = False
    for character in segment:
        if character == '"':
            quoted = not quoted
            continue
        if character == ";" and not quoted:
            pieces.append("".join(current))
            current = []
            continue
        current.append(character)
    pieces.append("".join(current))

    name = pieces[0].strip().upper()
    if "." in name:
        # A grouped property is `group.NAME`; the group orders related lines
        # and is not part of the property's identity.
        name = name.rsplit(".", 1)[-1]
    if not name:
        raise VcardError("a vCard line named no property")

    parameters: dict[str, tuple[str, ...]] = {}
    for piece in pieces[1:]:
        key, separator, raw_value = piece.partition("=")
        key = key.strip().upper()
        if not key:
            continue
        if not separator:
            # vCard 2.1 writes a bare type, e.g. `TEL;HOME:`. Reading it as a
            # valueless parameter would lose the only thing it says.
            parameters.setdefault("TYPE", ())
            parameters["TYPE"] += (key,)
            continue
        values = tuple(item.strip() for item in raw_value.split(",") if item.strip())
        parameters[key] = parameters.get(key, ()) + values
    return name, parameters


#: The versions this reader implements. Escaping and structured values differ
#: between them, so a card declaring a version outside this set cannot be read
#: under these rules: refusing it is refusing to report values it would get
#: wrong. A card declaring no version is a different case and is read — the
#: rules here are the ones it is asking for, and nothing about it is
#: unreadable.
VERSIONS = frozenset({"2.1", "3.0", "4.0"})


def parse(raw: bytes) -> list[Property]:
    """Read one vCard into its properties, in the order they appear.

    A component marker carries the name of what it opens or closes, and both
    are checked. Ignoring them accepts `BEGIN:WRONG ... END:WRONG` as a
    contact, and accepts a card whose inner component was closed with the
    outer one's name — neither of which is a vCard, and both of which reach
    the caller as a contact with no fields rather than as an error.

    A property inside a nested component belongs to that component, not to the
    card, so it is skipped rather than refused. Callers read a whole address
    book in one request, and refusing a card for a component this view does
    not model would cost the book to say nothing about the contacts in it.
    Nothing is lost: the raw card travels beside the reference.

    This reads one card. Two concatenated cards reach it as one property list;
    callers go through `cards()`, which splits them.
    """
    lines = unfold(raw)
    if not lines:
        raise VcardError("the vCard was empty")
    properties: list[Property] = []
    components: list[str] = []
    for line in lines:
        segment, separator, value = line.partition(":")
        if not separator:
            raise VcardError("a vCard line carried no value")
        name, parameters = _parameters(segment)
        if name == "BEGIN":
            component = value.strip().upper()
            if not components and component != "VCARD":
                raise VcardError(f"the resource opened {component or 'nothing'}, not a vCard")
            components.append(component)
            continue
        if name == "END":
            component = value.strip().upper()
            if not components:
                raise VcardError("the vCard ended a component it had not begun")
            if components[-1] != component:
                raise VcardError(
                    f"the vCard closed {component or 'nothing'} while {components[-1]} was open"
                )
            components.pop()
            continue
        if not components:
            raise VcardError("a vCard property lay outside its own card")
        if len(components) > 1:
            continue
        properties.append(
            Property(
                name=name,
                parameters=parameters,
                value="" if name in BINARY_PROPERTIES else _unescape(value),
            )
        )
    if components:
        raise VcardError("the vCard did not end")
    for declared in every(properties, "VERSION"):
        version = declared.value.strip()
        if version and version not in VERSIONS:
            raise VcardError(
                f"the vCard declared version {version!r}; this reads {', '.join(sorted(VERSIONS))}"
            )
    return properties


def first(properties: list[Property], name: str) -> str:
    for item in properties:
        if item.name == name:
            return item.value
    return ""


def every(properties: list[Property], name: str) -> tuple[Property, ...]:
    return tuple(item for item in properties if item.name == name)


def structured(value: str) -> tuple[str, ...]:
    """Split a `;`-separated structured value such as `N` or `ADR`."""
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            current.append(value[index : index + 2])
            index += 2
            continue
        if character == ";":
            parts.append("".join(current))
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    parts.append("".join(current))
    return tuple(_unescape(part).strip() for part in parts)


def has(properties: list[Property], name: str) -> bool:
    return any(item.name == name for item in properties)


def cards(raw: bytes) -> list[list[Property]]:
    """Read every card in one resource, which is normally exactly one.

    A card ends where the component it opened is closed, not at the first
    `END` in the file. A card carrying a nested component would otherwise be
    cut in half, and its remainder read as a second contact.
    """
    parsed: list[list[Property]] = []
    current: list[str] = []
    depth = 0
    for line in unfold(raw):
        segment = line.partition(":")[0]
        name = segment.split(";", 1)[0].rsplit(".", 1)[-1].strip().upper()
        current.append(line)
        if name == "BEGIN":
            depth += 1
            continue
        if name != "END":
            continue
        depth -= 1
        if depth < 0:
            raise VcardError("the vCard ended a component it had not begun")
        if depth == 0:
            parsed.append(parse("\r\n".join(current).encode("utf-8")))
            current = []
    if current:
        raise VcardError("the vCard did not end")
    if not parsed:
        raise VcardError("the resource contained no vCard")
    return parsed
