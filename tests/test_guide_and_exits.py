from __future__ import annotations

import argparse
import re

import pytest

from ncl import cli, exits, guide

# Every `ncl <command>` the guide mentions, including a second level such as
# `ncl profile list`. An alternation like `list|show` is split by the caller.
_MENTION = re.compile(r"\bncl ([a-z][a-z-]*)(?: ([a-z][a-z|-]*))?")


def _registered(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def test_guide_is_nonempty_and_names_only_registered_commands():
    """The guide may not promise a command that dispatch does not register.

    Derived from the parser rather than compared against a list of names known
    to be absent today. A denylist passes the moment someone documents a
    command nobody thought to add to it, which is exactly the drift this is
    here to catch.
    """
    text = guide.render()
    assert text.strip()

    top_level = _registered(cli.build_parser())
    assert top_level, "the parser registers no subcommands; this guard would pass vacuously"

    mentioned = set()
    for command, subcommand in _MENTION.findall(text):
        assert command in top_level, f"the guide names `ncl {command}`, which is not registered"
        mentioned.add(command)
        children = _registered(top_level[command])
        if not subcommand or not children:
            # A trailing word after a command that takes no subcommands is
            # prose, not a promise.
            continue
        for alternative in subcommand.split("|"):
            assert alternative in children, (
                f"the guide names `ncl {command} {alternative}`, which is not registered"
            )

    assert mentioned, "the guide names no commands at all"


def test_bare_cli_preserves_multiline_appointment_examples(capsys):
    assert cli.main([]) == exits.OK

    rendered = capsys.readouterr().out
    assert (
        "ncl cal appointment create <calendar> --summary S --from <iso> --to <iso> \\\n"
        "    --origin O --destination D --mode M --route-estimate <duration> \\\n"
        in rendered
    )
    assert (
        "ncl cal appointment update <appointment-href> <travel-href> <preparation-href> \\\n"
        "    --from <iso> --to <iso> --origin O --destination D --mode M \\\n"
        in rendered
    )


def test_top_level_help_describes_resumable_plan_application(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["--help"])

    assert error.value.code == exits.OK
    help_text = " ".join(capsys.readouterr().out.split())
    assert "apply Execute or resume a frozen plan" in help_text
    assert "Execute a frozen plan, once" not in help_text


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

def test_guide_notes_refid_repeatability():
    text = guide.render()
    assert "REFID" in text
    assert "repeatable" in text.lower()
    assert "RFC 9253" in text
    # The checkpoint repair made generic VTODO reads accept duplicate REFID
    # while run validation still requires exactly one parameter-free REFID.
    assert "Singleton VTODO properties cannot repeat except REFID" in text
