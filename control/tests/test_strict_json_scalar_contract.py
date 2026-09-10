from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import pytest
from fastapi import BackgroundTasks, Depends, FastAPI, Response
from fastapi.testclient import TestClient
from pydantic import ConfigDict, Field, RootModel, ValidationError
from vonk_agent_protocol import RecipeRunObservationsWire
from vonk_control.agent_api import HostHelperGrantResponse
from vonk_control.library_contract import (
    FreshnessPolicy,
    LibraryFacetValues,
    LibraryRecipeIdentity,
    ModelLibraryResponse,
)
from vonk_control.model_cache_contract import ModelCacheOperatorRequest
from vonk_control.operation_contract import AvailabilityOperationFailure
from vonk_control.recipe_image_availability_api import RecipeOperatorRequest
from vonk_control.strict_json import (
    ControllerAPIRoute,
    StrictJSONModel,
    serialize_json_value,
)


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


class _PresenceNestedProbe(StrictJSONModel):
    required_nullable: str | None
    optional_nullable: str | None = None
    enabled: bool = False


class _PresenceResponseProbe(StrictJSONModel):
    nested: _PresenceNestedProbe
    required_nullable: str | None
    optional_nullable: str | None = None
    retries: int = 0
    labels: list[str] = Field(default_factory=list)
    engine: dict[str, object] | None = None


class _PresenceMapProbe(StrictJSONModel):
    items: dict[str, _PresenceNestedProbe]


class _PresenceRootProbe(RootModel[list[_PresenceNestedProbe]]):
    pass


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


def test_model_dump_omits_only_optional_none_values_recursively() -> None:
    value = _PresenceResponseProbe(
        nested=_PresenceNestedProbe(required_nullable=None),
        required_nullable=None,
        engine={"future_option": None},
    )

    assert serialize_json_value(value) == {
        "nested": {"required_nullable": None, "enabled": False},
        "required_nullable": None,
        "retries": 0,
        "labels": [],
        "engine": {"future_option": None},
    }


def test_presence_policy_traverses_typed_maps_and_root_models() -> None:
    nested = _PresenceNestedProbe(required_nullable=None)

    assert serialize_json_value(_PresenceMapProbe(items={"nested": nested})) == {
        "items": {"nested": {"required_nullable": None, "enabled": False}}
    }
    assert serialize_json_value(_PresenceRootProbe([nested])) == [
        {"required_nullable": None, "enabled": False}
    ]


def test_fastapi_response_uses_presence_policy_for_nested_contracts() -> None:
    app = FastAPI()
    app.router.route_class = ControllerAPIRoute

    @app.get("/presence", response_model=_PresenceResponseProbe, status_code=202)
    def presence() -> _PresenceResponseProbe:
        return _PresenceResponseProbe(
            nested=_PresenceNestedProbe(required_nullable=None),
            required_nullable=None,
            engine={"future_option": None},
        )

    with TestClient(app) as client:
        response = client.get("/presence")

    assert response.status_code == 202
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "nested": {"required_nullable": None, "enabled": False},
        "required_nullable": None,
        "retries": 0,
        "labels": [],
        "engine": {"future_option": None},
    }


def test_fastapi_presence_policy_preserves_injected_response_metadata() -> None:
    completed: list[str] = []
    app = FastAPI()
    app.router.route_class = ControllerAPIRoute

    @app.get("/presence", response_model=_PresenceResponseProbe, status_code=202)
    def presence(
        response: Response, background_tasks: BackgroundTasks
    ) -> _PresenceResponseProbe:
        response.status_code = 207
        response.headers["x-presence"] = "retained"
        response.set_cookie("presence", "retained")
        background_tasks.add_task(completed.append, "done")
        return _PresenceResponseProbe(
            nested=_PresenceNestedProbe(required_nullable=None),
            required_nullable=None,
            engine={"future_option": None},
        )

    with TestClient(app) as client:
        response = client.get("/presence")

    assert response.status_code == 207
    assert response.headers["x-presence"] == "retained"
    assert response.cookies["presence"] == "retained"
    assert completed == ["done"]


def test_fastapi_presence_policy_preserves_dependency_response_metadata() -> None:
    def set_metadata(response: Response) -> None:
        response.status_code = 208
        response.headers["x-presence"] = "dependency"
        response.set_cookie("presence", "dependency")

    app = FastAPI()
    app.router.route_class = ControllerAPIRoute

    @app.get(
        "/presence",
        dependencies=[Depends(set_metadata)],
        response_model=_PresenceResponseProbe,
    )
    def presence() -> _PresenceResponseProbe:
        return _PresenceResponseProbe(
            nested=_PresenceNestedProbe(required_nullable=None),
            required_nullable=None,
            engine={"future_option": None},
        )

    with TestClient(app) as client:
        response = client.get("/presence")

    assert response.status_code == 208
    assert response.headers["x-presence"] == "dependency"
    assert response.cookies["presence"] == "dependency"


def test_fastapi_presence_policy_traverses_mapping_response_values() -> None:
    app = FastAPI()
    app.router.route_class = ControllerAPIRoute

    @app.get("/presence", response_model=dict[str, _PresenceNestedProbe])
    def presence() -> dict[str, _PresenceNestedProbe]:
        return {"nested": _PresenceNestedProbe(required_nullable=None)}

    with TestClient(app) as client:
        response = client.get("/presence")

    assert response.status_code == 200
    assert response.json() == {
        "nested": {"required_nullable": None, "enabled": False}
    }


def test_model_library_json_roundtrip_preserves_datetime_and_strict_tags() -> None:
    snapshot = ModelLibraryResponse(
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        models=[],
        facets=LibraryFacetValues(usage=[], family=[], version=[], quantization=[]),
        next_cursor=None,
        filters={},
        freshness_policy=FreshnessPolicy(),
    )
    wire = snapshot.model_dump_json()
    restored = ModelLibraryResponse.model_validate_json(wire)
    assert restored.generated_at == snapshot.generated_at

    for invalid in (True, 2.0):
        payload = snapshot.model_dump(mode="json")
        payload["schema_version"] = invalid
        with pytest.raises(ValidationError):
            ModelLibraryResponse.model_validate_json(json.dumps(payload))

    mapping = snapshot.model_dump()
    assert ModelLibraryResponse.model_validate(mapping).generated_at == snapshot.generated_at


def test_library_identity_json_roundtrip_preserves_uuid_wire_text() -> None:
    identity = LibraryRecipeIdentity(
        recipe_id="123e4567-e89b-12d3-a456-426614174000",
        recipe_revision_id="123e4567-e89b-12d3-a456-426614174001",
        publisher="publisher",
        slug="recipe",
        content_sha256="a" * 64,
        title="Recipe",
        description="Description",
    )
    restored = LibraryRecipeIdentity.model_validate_json(identity.model_dump_json())
    assert restored.recipe_id == identity.recipe_id


def test_strict_literal_hook_preserves_json_native_representations() -> None:
    class Wire(StrictJSONModel):
        model_config = ConfigDict(strict=True)
        schema_version: Literal[2]
        observed_at: datetime
        identity: UUID
        values: tuple[int, ...]

    wire = {
        "schema_version": 2,
        "observed_at": "2026-09-07T09:14:18Z",
        "identity": "123e4567-e89b-12d3-a456-426614174000",
        "values": [1, 2],
    }
    parsed = Wire.model_validate_json(json.dumps(wire))
    assert parsed.observed_at == datetime(2026, 9, 7, 9, 14, 18, tzinfo=UTC)
    assert parsed.identity == UUID(wire["identity"])
    assert parsed.values == (1, 2)
    with pytest.raises(ValidationError):
        Wire.model_validate(wire)


def test_representative_wire_models_reject_scalar_coercion() -> None:
    with pytest.raises(ValidationError):
        ModelCacheOperatorRequest.model_validate_json(
            '{"request_key":"00000000-0000-4000-8000-000000000001","with_model":"yes"}'
        )

    with pytest.raises(ValidationError):
        RecipeRunObservationsWire.model_validate(
            {"schema_version": True, "observed_at": "2026-01-01T00:00:00Z", "runs": []}
        )
    with pytest.raises(ValidationError):
        RecipeRunObservationsWire.model_validate(
            {"schema_version": 2.0, "observed_at": "2026-01-01T00:00:00Z", "runs": []}
        )
    with pytest.raises(ValidationError):
        RecipeOperatorRequest.model_validate_json(
            '{"request_key":"00000000-0000-4000-8000-000000000001","with_model":1}'
        )
    with pytest.raises(ValidationError):
        AvailabilityOperationFailure.model_validate_json(
            '{"code":"bad","detail":"d",'
            '"retry_after_seconds":true,'
            '"retry_time":"2026-01-01T00:00:00Z"}'
        )


def test_host_grants_require_the_signed_contract_structure() -> None:
    with pytest.raises(ValidationError):
        HostHelperGrantResponse.model_validate_json(
            '{"grant":{"provider_field":{"future":true},"count":"1"}}'
        )


def test_fastapi_body_routes_reject_coercion_and_openapi_keeps_scalar_shapes() -> None:
    app = FastAPI()

    @app.post("/cache", response_model=ModelCacheOperatorRequest)
    def cache(body: ModelCacheOperatorRequest) -> ModelCacheOperatorRequest:
        return body

    @app.post("/image", response_model=RecipeOperatorRequest)
    def image(body: RecipeOperatorRequest) -> RecipeOperatorRequest:
        return body

    @app.post("/observations", response_model=RecipeRunObservationsWire)
    def observations(body: RecipeRunObservationsWire) -> RecipeRunObservationsWire:
        return body

    with TestClient(app) as client:
        assert client.post("/cache", json={"request_key": "00000000-0000-4000-8000-000000000001", "with_model": "yes"}).status_code == 422
        assert client.post("/cache", json={"request_key": "00000000-0000-4000-8000-000000000001", "with_model": True}).status_code == 200
        assert (
            client.post(
                "/image",
                json={"request_key": "00000000-0000-4000-8000-000000000001", "with_model": 1},
            ).status_code
            == 422
        )
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
                    "schema_version": 2.0,
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
                    "schema_version": 2,
                    "observed_at": "2026-01-01T00:00:00Z",
                    "runs": [],
                },
            )
            .status_code
            == 200
        )

    schemas = app.openapi()["components"]["schemas"]
    assert schemas["ModelCacheOperatorRequest"]["properties"]["with_model"]["type"] == "boolean"
    assert schemas["RecipeOperatorRequest"]["properties"]["with_model"]["type"] == "boolean"
