from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from vonk_control.agent_api import AgentGrantResponse, GrantRequest
from vonk_control.model_cache_contract import ModelCacheEvictionPreviewRequest
from vonk_control.operation_contract import AvailabilityOperationFailure
from vonk_control.recipe_image_availability_api import RecipeImageAvailabilityStart


def test_representative_wire_models_reject_scalar_coercion() -> None:
    with pytest.raises(ValidationError):
        ModelCacheEvictionPreviewRequest.model_validate_json(
            '{"target_bytes":"1"}'
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

    schemas = app.openapi()["components"]["schemas"]
    assert schemas["ModelCacheEvictionPreviewRequest"]["properties"]["target_bytes"][
        "type"
    ] == "integer"
    assert schemas["RecipeImageAvailabilityStart"]["properties"]["force"]["type"] == "boolean"
    assert schemas["GrantRequest"]["properties"]["ttl_seconds"]["type"] == "integer"
