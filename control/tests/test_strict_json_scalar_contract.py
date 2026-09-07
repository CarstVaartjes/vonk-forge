from __future__ import annotations

from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ConfigDict, Field, ValidationError
from vonk_control.agent_api import (
    AgentGrantResponse,
    GrantRequest,
    RecipeRunObservationsRequest,
)
from vonk_control.model_cache_contract import ModelCacheEvictionPreviewRequest
from vonk_control.operation_contract import AvailabilityOperationFailure
from vonk_control.recipe_image_availability_api import RecipeImageAvailabilityStart
from vonk_control.strict_json import StrictJSONModel


class _LiteralSemanticsProbe(StrictJSONModel):
    model_config = ConfigDict(strict=True)
    optional_tag: Literal[1] | None = None
    mixed_string: Literal[1] | str
    mixed_integer: Literal[1] | int
    mixed_float: Literal[1] | float
    bool_tag: Literal[True]


class _AliasedLiteralProbe(StrictJSONModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    tag: Literal[1] = Field(alias="wire_tag")


def test_numeric_literal_check_preserves_union_semantics() -> None:
    accepted = _LiteralSemanticsProbe(
        optional_tag=None,
        mixed_string="future",
        mixed_integer=2,
        mixed_float=2.5,
        bool_tag=True,
    )
    assert accepted.optional_tag is None
    assert accepted.mixed_string == "future"
    assert accepted.mixed_integer == 2
    assert accepted.mixed_float == 2.5

    with pytest.raises(ValidationError):
        _LiteralSemanticsProbe(
            mixed_string="future",
            mixed_integer=2,
            mixed_float=2.5,
            bool_tag=1,
        )
    with pytest.raises(ValidationError):
        _LiteralSemanticsProbe(
            optional_tag=1.0,
            mixed_string="future",
            mixed_integer=2,
            mixed_float=2.5,
            bool_tag=True,
        )


def test_numeric_literal_check_honors_alias_input_names() -> None:
    assert _AliasedLiteralProbe.model_validate({"wire_tag": 1}).tag == 1
    with pytest.raises(ValidationError):
        _AliasedLiteralProbe.model_validate({"wire_tag": True})
    with pytest.raises(ValidationError):
        _AliasedLiteralProbe.model_validate({"tag": 1})


def test_representative_wire_models_reject_scalar_coercion() -> None:
    with pytest.raises(ValidationError):
        ModelCacheEvictionPreviewRequest.model_validate_json(
            '{"target_bytes":"1"}'
        )

    with pytest.raises(ValidationError):
        RecipeRunObservationsRequest.model_validate(
            {"schema_version": True, "observed_at": "2026-01-01T00:00:00Z", "runs": []}
        )
    with pytest.raises(ValidationError):
        RecipeRunObservationsRequest.model_validate(
            {"schema_version": 1.0, "observed_at": "2026-01-01T00:00:00Z", "runs": []}
        )
    with pytest.raises(ValidationError):
        RecipeImageAvailabilityStart.model_validate_json(
            '{"request_key":"x","recipe_revision_id":"r","force":1}'
        )
    with pytest.raises(ValidationError):
        GrantRequest.model_validate_json('{"ttl_seconds":"1"}')
    with pytest.raises(ValidationError):
        AvailabilityOperationFailure.model_validate_json(
            '{"code":"bad","detail":"d",'
            '"retry_after_seconds":true,'
            '"retry_time":"2026-01-01T00:00:00Z"}'
        )


def test_declared_content_maps_keep_their_flexible_values() -> None:
    response = AgentGrantResponse.model_validate_json(
        '{"grant":{"provider_field":{"future":true},"count":"1"}}'
    )
    assert response.grant == {"provider_field": {"future": True}, "count": "1"}


def test_fastapi_body_routes_reject_coercion_and_openapi_keeps_scalar_shapes() -> None:
    app = FastAPI()

    @app.post("/cache", response_model=ModelCacheEvictionPreviewRequest)
    def cache(body: ModelCacheEvictionPreviewRequest) -> ModelCacheEvictionPreviewRequest:
        return body

    @app.post("/image", response_model=RecipeImageAvailabilityStart)
    def image(body: RecipeImageAvailabilityStart) -> RecipeImageAvailabilityStart:
        return body

    @app.post("/grant", response_model=GrantRequest)
    def grant(body: GrantRequest) -> GrantRequest:
        return body

    @app.post("/observations", response_model=RecipeRunObservationsRequest)
    def observations(body: RecipeRunObservationsRequest) -> RecipeRunObservationsRequest:
        return body

    with TestClient(app) as client:
        assert client.post("/cache", json={"target_bytes": "1"}).status_code == 422
        assert client.post("/cache", json={"target_bytes": 1}).status_code == 200
        assert (
            client.post(
                "/image",
                json={"request_key": "x", "recipe_revision_id": "r", "force": 1},
            ).status_code
            == 422
        )
        assert client.post("/grant", json={"ttl_seconds": "1"}).status_code == 422
        assert (
            client.post(
                "/observations",
                json={
                    "schema_version": True,
                    "observed_at": "2026-01-01T00:00:00Z",
                    "runs": [],
                },
            )
            .status_code
            == 422
        )
        assert (
            client.post(
                "/observations",
                json={
                    "schema_version": 1.0,
                    "observed_at": "2026-01-01T00:00:00Z",
                    "runs": [],
                },
            )
            .status_code
            == 422
        )
        assert (
            client.post(
                "/observations",
                json={
                    "schema_version": 1,
                    "observed_at": "2026-01-01T00:00:00Z",
                    "runs": [],
                },
            )
            .status_code
            == 200
        )

    schemas = app.openapi()["components"]["schemas"]
    assert schemas["ModelCacheEvictionPreviewRequest"]["properties"]["target_bytes"][
        "type"
    ] == "integer"
    assert schemas["RecipeImageAvailabilityStart"]["properties"]["force"]["type"] == "boolean"
    assert schemas["GrantRequest"]["properties"]["ttl_seconds"]["type"] == "integer"
