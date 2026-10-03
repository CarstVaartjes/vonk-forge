"""The lane drives Controllers older than its own source and must not outrun them."""

from __future__ import annotations

import json

import pytest
from vonk_control.fleet_profile_contract import FleetProfileLoadRequest

from scripts.development_slice_client import Client
from tests.acceptance.controller_contract import ContractSkew, ControllerContract
from tests.acceptance.test_spark_lifecycle import SparkLifecycle


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


def test_the_lane_loads_a_profile_with_only_what_an_older_controller_knows() -> None:
    """The lane builds its load request from today's model; #1086 sent its new
    optional field as null to a Controller that refuses unknown fields.
    """
    current = FleetProfileLoadRequest.model_json_schema()
    required = set(current["required"])
    assert required != set(current["properties"]), "no optional field to outrun"
    older = {
        **current,
        "properties": {
            name: value
            for name, value in current["properties"].items()
            if name in required
        },
    }
    contract = _contract(older, path="/api/profile/{number}/load")
    sent: list[bytes] = []

    def transport(method, path, payload, headers, timeout):
        contract.check(method, path, payload)
        assert payload is not None
        sent.append(payload)
        return 202, b'{"id":"1"}'

    lifecycle = object.__new__(SparkLifecycle)
    lifecycle.control = Client(
        "https://controller.test",
        None,
        timeout=1,
        headers={"X-Test": "1"},
        transport=transport,
    )
    lifecycle._load_canary_profile(
        {"plan_digest": "a" * 64}, request_key="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    assert json.loads(sent[0]) == {
        "request_key": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    }


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
