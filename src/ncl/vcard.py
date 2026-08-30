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


def parse(raw: bytes) -> list[Property]:
    """Read one vCard into its properties, in the order they appear."""
    lines = unfold(raw)
    if not lines:
        raise VcardError("the vCard was empty")
    properties: list[Property] = []
    depth = 0
    for line in lines:
        segment, separator, value = line.partition(":")
        if not separator:
            raise VcardError("a vCard line carried no value")
        name, parameters = _parameters(segment)
        if name == "BEGIN":
            depth += 1
            continue
        if name == "END":
            depth -= 1
            if depth < 0:
                raise VcardError("the vCard ended a component it had not begun")
            continue
        if depth != 1:
            raise VcardError("a vCard property lay outside its own card")
        properties.append(
            Property(
                name=name,
                parameters=parameters,
                value="" if name in BINARY_PROPERTIES else _unescape(value),
            )
        )
    if depth != 0:
        raise VcardError("the vCard did not end")
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
    """Read every card in one resource, which is normally exactly one."""
    parsed: list[list[Property]] = []
    current: list[str] = []
    for line in unfold(raw):
        name = line.partition(":")[0].split(";", 1)[0].strip().upper()
        current.append(line)
        if name == "END":
            parsed.append(parse("\r\n".join(current).encode("utf-8")))
            current = []
    if current:
        raise VcardError("the vCard did not end")
    if not parsed:
        raise VcardError("the resource contained no vCard")
    return parsed
