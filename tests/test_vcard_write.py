from ncl import vcard


def test_escape_and_fold_round_trip():
    value = "A\\B; C, D\n" + "é" * 50
    line = vcard.render_property("NOTE", vcard.escape(value))

    parsed = vcard.unfold((line + "\r\n").encode())

    assert vcard._unescape(parsed[0].partition(":")[2]) == value
    assert all(len(part.encode()) <= 75 for part in line.split("\r\n"))


def test_build_renders_structured_name_and_folds_note():
    raw = vcard.build(
        uid="1@ncl", fields={"fn": "Ada", "family": "Byron", "given": "Ada", "note": "x" * 100}
    )

    assert b"N:Byron;Ada;;;\r\n" in raw
    assert b"\r\n " in raw


def test_splice_preserves_unrelated_bytes_and_manages_properties():
    raw = (
        b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Old\r\nEMAIL:a@b\r\nEMAIL:c@d\r\n"
        b"X-KEEP:yes\r\nPHOTO;ENCODING=b:AAAA\r\n BBBB\r\nEND:VCARD\r\n"
    )

    out = vcard.splice(raw, {"FN": ("New",), "EMAIL": None, "ORG": ("Acme",)})

    assert b"FN:New\r\n" in out
    assert b"EMAIL:" not in out
    assert b"ORG:Acme\r\nEND:VCARD" in out
    assert b"X-KEEP:yes\r\nPHOTO;ENCODING=b:AAAA\r\n BBBB\r\n" in out


def test_splice_keeps_lf_and_ignores_nested_properties():
    raw = b"BEGIN:VCARD\nFN:Outer\nBEGIN:X-INNER\nFN:Inner\nEND:X-INNER\nEND:VCARD\n"

    out = vcard.splice(raw, {"FN": ("Changed",)})

    assert out == (b"BEGIN:VCARD\nFN:Changed\nBEGIN:X-INNER\nFN:Inner\nEND:X-INNER\nEND:VCARD\n")
