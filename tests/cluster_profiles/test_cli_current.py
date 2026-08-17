from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

import cluster_profiles
from cluster_profiles import cli


@dataclass(frozen=True)
class _Model:
    value: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return self.value


class _Client:
    def nodes(self) -> _Model:
        return _Model({"nodes": [], "commit": "a" * 40})

    def endpoint(self, alias: str) -> _Model:
        return _Model({"alias": alias, "state": "withdrawn"})


def _invoke(*argv: str) -> tuple[int, dict[str, object]]:
    from contextlib import redirect_stdout
    from io import StringIO

    stdout = StringIO()
    with redirect_stdout(stdout):
        result = cli.main(argv, control_client=_Client())
    return result, json.loads(stdout.getvalue())


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("--json", "nodes", "status"), {"nodes": [], "commit": "a" * 40}),
        (
            ("--json", "endpoint", "hermes-agent"),
            {"alias": "hermes-agent", "state": "withdrawn"},
        ),
    ],
)
def test_current_cli_returns_server_models_without_profile_fallback(
    argv: tuple[str, ...], expected: dict[str, object]
) -> None:
    result, payload = _invoke(*argv)

    assert result == 0
    assert payload == expected


@pytest.mark.parametrize("command", ["validate", "prepare", "switch", "restore-default"])
def test_current_cli_has_no_retired_profile_commands(command: str) -> None:
    result, payload = _invoke("--json", command)

    assert result == 2
    assert payload["error_type"] == "arguments"


@pytest.mark.parametrize("command", ["profiles", "models"])
def test_current_admin_cli_has_no_retired_catalog_commands(command: str) -> None:
    result, payload = _invoke("--json", "admin", command)

    assert result == 2
    assert payload["error_type"] == "arguments"


def test_root_package_exports_no_profile_contract() -> None:
    assert set(cluster_profiles.__all__) == set()
