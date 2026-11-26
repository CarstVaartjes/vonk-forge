"""The compatibility report names what a published vonkctl would refuse."""

from __future__ import annotations

import copy
import json
import urllib.error
from pathlib import Path
from typing import Any, cast

import pytest

from cluster_profiles import cli_update
from tools import client_compatibility_report
from tools.client_compatibility_report import breaks, main

_LEVELS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "levels": {"type": "array", "items": {"type": "string"}},
        "default": {"type": "string"},
    },
    "required": ["levels", "default"],
}


def _contract(
    effort: dict[str, object], *, extra_route: bool = False
) -> dict[str, Any]:
    paths: dict[str, object] = {
        "/api/recipe/library": {
            "get": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Library"}
                            }
                        }
                    }
                }
            }
        }
    }
    if extra_route:
        paths["/api/new"] = copy.deepcopy(paths["/api/recipe/library"])
    return {
        "paths": paths,
        "components": {
            "schemas": {
                "Library": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "effort": effort,
                        "state": {"enum": ["ready", "missing"], "type": "string"},
                    },
                    "required": ["effort", "state"],
                }
            }
        },
    }


def test_the_incident_shape_is_reported() -> None:
    # Contracts 4.7.0: the effort became a union tagged by ``kind``.
    tagged = copy.deepcopy(_LEVELS)
    tagged["properties"]["kind"] = {"const": "levels", "type": "string"}
    tagged["required"] = ["kind", "levels"]
    text = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"kind": {"const": "text", "type": "string"}},
        "required": ["kind"],
    }
    found = breaks(
        _contract(_LEVELS), _contract({"oneOf": [tagged, text]}, extra_route=True)
    )

    assert found == [
        "GET /api/recipe/library 200.effort: new property 'kind'",
        "GET /api/recipe/library 200.effort: 'default' may now be absent",
        "GET /api/recipe/library 200.effort: 'levels' may now be absent",
    ]


def test_the_same_contract_and_narrowing_report_nothing() -> None:
    narrowed = _contract(_LEVELS, extra_route=True)
    narrowed["components"]["schemas"]["Library"]["properties"]["state"]["enum"] = [
        "ready"
    ]

    assert breaks(_contract(_LEVELS), _contract(_LEVELS)) == []
    assert breaks(_contract(_LEVELS), narrowed) == []


def test_new_values_types_and_removed_routes_are_reported() -> None:
    wider = _contract({"type": ["string", "null"]})
    wider["components"]["schemas"]["Library"]["properties"]["state"]["enum"].append(
        "stale"
    )

    assert breaks(_contract({"type": "string"}), wider) == [
        "GET /api/recipe/library 200.effort: may now be null|string",
        "GET /api/recipe/library 200.state: may now be ['stale']",
    ]
    assert breaks(_contract(_LEVELS, extra_route=True), _contract(_LEVELS)) == [
        "GET /api/new: route removed"
    ]


def test_a_word_set_the_prior_ships_open_reads_new_words() -> None:
    shown = {"type": "string", "minLength": 1}
    prior = _contract({"type": "string"})
    prior["components"]["schemas"]["Library"]["properties"]["state"] = shown
    newer = _contract({"type": "string"})
    newer["components"]["schemas"]["Library"]["properties"]["state"]["enum"].append(
        "stale"
    )
    opened = _contract({"type": "string"})
    opened["components"]["schemas"]["Library"]["properties"]["state"] = shown

    assert breaks(prior, newer) == []
    # Against a prior that still closes the set, opening it is reported.
    found = breaks(_contract({"type": "string"}), opened)
    assert [line.split(":")[0] for line in found] == [
        "GET /api/recipe/library 200.state"
    ]


def _run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], published: object
) -> int:
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(_contract({"type": "string"}, extra_route=False)))

    def fetch(_channel: str) -> tuple[str, str, object]:
        return cast("tuple[str, str, object]", published)

    monkeypatch.setattr(client_compatibility_report, "_published_contract", fetch)
    return main(["--candidate", str(candidate), *argv])


_BREAKING = _contract({"type": "integer"})


@pytest.mark.parametrize(
    ("channel", "version", "gate", "code"),
    [
        ("stable", "1.0.0", "stable", 1),
        ("stable", "0.1.1", "stable", 0),
        ("dev", "1.0.1~dev.1", "stable", 0),
    ],
)
def test_only_a_stable_1_0_break_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    channel: str,
    version: str,
    gate: str,
    code: int,
) -> None:
    published = (f"{channel} {version}", version, _BREAKING)

    assert (
        _run(
            monkeypatch,
            tmp_path,
            ["--channel", channel, "--gate-channel", gate],
            published,
        )
        == code
    )


def test_an_unpublished_stable_pointer_does_not_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def not_found(request: Any, *, timeout: float) -> Any:
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_update, "open_https", not_found)
    monkeypatch.setattr(cli_update.time, "sleep", lambda _: None)
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(_BREAKING))

    argv = ["--candidate", str(candidate), "--channel", "stable"]

    assert main([*argv, "--gate-channel", "stable"]) == 0
