"""The profile number may be given before or after the command."""

from __future__ import annotations

import json

import pytest

from cluster_profiles import cli
from cluster_profiles.cli_help import subcommands

UUID = "33333333-3333-4333-8333-333333333333"

# Every command that acts on a numbered profile, with the arguments it needs.
PROFILE_COMMANDS = [
    ("profile",),
    ("profile", "list"),
    ("profile", "add", "vonk-forge/qwen-code", "--spark", "Atlas"),
    ("profile", "remove", "coding"),
    ("profile", "configure", "--name", "Coding"),
    ("profile", "export"),
    ("profile", "import", "--file", "p.json", "--expected-revision", "0"),
    ("profile", "load"),
    ("profile", "cancel", UUID),
    ("profile", "progress"),
    ("profile", "endpoint"),
    ("run", "Qwen Code"),
]


@pytest.mark.parametrize("command", PROFILE_COMMANDS)
def test_profile_flag_is_accepted_after_the_command(command):
    before = cli._parser().parse_args(["--profile", "2", *command])
    after = cli._parser().parse_args([*command, "--profile", "2"])

    assert before.profile_number == after.profile_number == 2


def test_profile_flag_before_the_command_survives_a_command_without_it():
    args = cli._parser().parse_args(["--profile", "3", "profile", "load", "--yes"])

    assert args.profile_number == 3


def test_profile_flag_after_the_command_wins_over_none():
    assert (
        cli._parser().parse_args(["profile", "load", "--profile", "4"]).profile_number
        == 4
    )
    assert cli._parser().parse_args(["profile", "list"]).profile_number is None


def _help(path):
    parser = cli._parser()
    for name in path:
        commands = subcommands(parser)
        assert commands is not None
        parser = commands.choices[name]
    return parser.format_help()


def test_command_help_names_the_profile_a_change_requires():
    help_text = _help(("profile", "load"))

    assert help_text.splitlines()[0] == "vonkctl profile load --profile <n> [flags]"


def test_a_command_that_changes_a_profile_tells_how_to_name_it(capsys):
    class Unreachable:
        request_timeout_seconds = 1.0

        def request(self, *_args, **_kwargs):
            raise AssertionError("no request may be sent without a profile number")

    status = cli.main(
        ("--json", "profile", "load", "--yes"), control_client=Unreachable()
    )

    document = json.loads(capsys.readouterr().out)
    assert status == 2
    assert "id" not in document
    assert (
        cli._parser()
        .parse_args(("--json", "profile", "load", "--profile", "1", "--yes"))
        .profile_number
        == 1
    )
