"""The compatibility report names what a published vonkctl would refuse."""

from __future__ import annotations

import copy
from typing import Any

from tools.client_compatibility_report import breaks

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
