from __future__ import annotations

from ncl import exits, guide


def test_guide_is_nonempty_and_names_only_registered_commands():
    assert guide.render().strip()
    assert "ncl doctor" in guide.render()
    assert "ncl profile" in guide.render()
    for unregistered in ("ncl login", "ncl calendar", "ncl file", "ncl apply"):
        assert unregistered not in guide.render()


def test_every_exit_code_has_one_response():
    codes = {
        value
        for name, value in vars(exits).items()
        if name.isupper() and isinstance(value, int)
    }

    assert codes == set(exits.RESPONSE)
    response = exits.RESPONSE[exits.OUTCOME_UNCERTAIN].lower()
    assert "reconcile" in response
    assert "blind retry" in response
    assert "duplicate" in response
    assert "overwrite" in response
