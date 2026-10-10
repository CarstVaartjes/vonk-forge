"""Current operator commands parse through the owning CLI command set."""

from __future__ import annotations

import pytest

from cluster_profiles.cli import _parser


@pytest.mark.parametrize(
    "arguments",
    [
        ("platform",),
        ("fleet",),
        ("fleet", "detail", "Atlas"),
        ("fleet", "upgrade", "Atlas", "--yes", "--json"),
        ("fleet", "upgrade", "--all", "--yes"),
        ("model", "library"),
        ("recipe", "library"),
        ("completion", "bash"),
        ("update", "--channel", "dev", "--json"),
    ],
)
def test_current_commands_parse(arguments: tuple[str, ...]) -> None:
    _parser().parse_args(arguments)
