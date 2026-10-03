"""The lane drives Controllers older than its own source and must not outrun them."""

from __future__ import annotations

import pytest

from tests.acceptance.controller_contract import ContractSkew, ControllerContract


def _contract(
    schema: dict, *, path: str = "/api/things/{number}"
) -> ControllerContract:
    return ControllerContract(
        {
            "paths": {
                path: {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Body"}
                                }
                            }
                        }
                    }
                }
            },
            "components": {"schemas": {"Body": schema}},
        },
        label="the previous Controller",
    )


def test_a_field_the_older_contract_has_never_heard_of_is_refused_by_name() -> None:
    contract = _contract(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"request_key": {"type": "string"}},
        }
    )
    contract.check("POST", "/api/things/1", b'{"request_key":"k"}')
    with pytest.raises(ContractSkew, match="body.reviewed_effects_digest"):
        contract.check(
            "POST",
            "/api/things/1",
            b'{"request_key":"k","reviewed_effects_digest":null}',
        )


def test_the_most_specific_operation_judges_a_path_with_a_slash_in_it() -> None:
    contract = ControllerContract(
        {
            "paths": {
                "/api/recipe/{selector}": {"post": {}},
                "/api/recipe/{selector}/download": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {"request_key": {}},
                                    }
                                }
                            }
                        }
                    }
                },
            },
            "components": {"schemas": {}},
        },
        label="the previous Controller",
    )
    contract.check(
        "POST", "/api/recipe/vonk-forge-test/canary/download", b'{"request_key":1}'
    )
    with pytest.raises(ContractSkew, match="other"):
        contract.check(
            "POST", "/api/recipe/vonk-forge-test/canary/download", b'{"other":1}'
        )


def test_free_form_maps_nullable_fields_and_unknown_operations() -> None:
    contract = _contract(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "labels": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "name": {"type": "string"},
            },
        }
    )
    contract.check("POST", "/api/things/1", b'{"labels":{"any":"key"},"note":null}')
    with pytest.raises(ContractSkew, match="body.name is null"):
        contract.check("POST", "/api/things/1", b'{"name":null}')
    with pytest.raises(ContractSkew, match="no operation POST /api/other"):
        contract.check("POST", "/api/other", b"{}")
    contract.check("GET", "/api/other", None)
