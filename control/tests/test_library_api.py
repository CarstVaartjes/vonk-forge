from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from vonk_control import operation_api
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.library_contract import ProjectionReason as LibraryProjectionReason
from vonk_control.library_projection import (
    FreshnessPolicy,
    LibraryRecipeDetail,
    LibraryRecipeIdentity,
    LibrarySnapshot,
    OperationalState,
    ProjectionReason,
    RecipeRevisionSummary,
)

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
RECIPE_ID = "00000000-0000-4000-8000-000000000001"
REVISION_ID = "00000000-0000-4000-8000-000000000002"


class Jobs:
    def list_page(self, **_kwargs):
        return [], None, 0


class Library:
    def __init__(self) -> None:
        self.list_calls: list[tuple[int, str | None]] = []
        self.detail_calls: list[str] = []

    def list(self, *, limit: int, cursor: str | None) -> LibrarySnapshot:
        self.list_calls.append((limit, cursor))
        if cursor == "invalid":
            raise ValueError("library cursor is invalid")
        return LibrarySnapshot(
            generated_at=NOW,
            models=[],
            unlinked_recipes=[],
            next_cursor=None,
            freshness_policy=FreshnessPolicy(),
        )

    def detail(self, recipe_id: str) -> LibraryRecipeDetail:
        self.detail_calls.append(recipe_id)
        if recipe_id != RECIPE_ID:
            raise KeyError(recipe_id)
        return LibraryRecipeDetail(
            generated_at=NOW,
            recipe=LibraryRecipeIdentity(
                recipe_id=RECIPE_ID,
                slug="visible-recipe",
                title="Visible recipe title",
                description="Visible recipe description",
                source_kind="local",
            ),
            selected_revision=RecipeRevisionSummary(
                id=REVISION_ID,
                revision_number=1,
                lifecycle="draft",
                schema_version=1,
                content_sha256=None,
                created_at=NOW,
            ),
            visual_recipe=None,
            topology=None,
            operational_state=OperationalState(
                builds=[], mappings=[], installations=[], runs=[]
            ),
            placement=[],
            reasons=[
                ProjectionReason(
                    code="recipe.document_invalid",
                    detail="The stored recipe document is invalid.",
                    severity="error",
                )
            ],
        )


def _client(*, role: str = "operator"):
    codec = TokenCodec(b"k" * 32)
    library = Library()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet=lambda: {"authority_revision": "a" * 64, "nodes": []},
        library_projection=library,
        now=lambda: 10,
    )
    token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
    return TestClient(app), {"Authorization": f"Bearer {token}"}, library


def test_library_reads_require_authentication_and_use_bounded_query_inputs() -> None:
    client, operator, library = _client()

    assert client.get("/api/v1/library").status_code == 401
    assert client.get(f"/api/v1/library/recipes/{RECIPE_ID}").status_code == 401

    root = client.get("/api/v1/library", headers=operator)
    cursor_page = client.get(
        "/api/v1/library",
        headers=operator,
        params={"limit": 1, "cursor": "signed-cursor"},
    )
    detail = client.get(f"/api/v1/library/recipes/{RECIPE_ID}", headers=operator)

    assert root.status_code == 200
    assert cursor_page.status_code == 200
    assert detail.status_code == 200
    assert detail.json()["recipe"]["title"] == "Visible recipe title"
    assert library.list_calls == [(100, None), (1, "signed-cursor")]
    assert library.detail_calls == [RECIPE_ID]

    for limit in (0, 101):
        response = client.get(
            "/api/v1/library", headers=operator, params={"limit": limit}
        )
        assert response.status_code == 422
    malformed = client.get("/api/v1/library/recipes/not-a-uuid", headers=operator)
    assert malformed.status_code == 422
    assert library.detail_calls == [RECIPE_ID]


def test_library_maps_cursor_missing_and_unavailable_failures_without_mutations() -> (
    None
):
    client, operator, library = _client()

    invalid = client.get(
        "/api/v1/library", headers=operator, params={"cursor": "invalid"}
    )
    missing = client.get(
        "/api/v1/library/recipes/00000000-0000-4000-8000-000000000099",
        headers=operator,
    )
    library.list = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("database"))
    unavailable = client.get("/api/v1/library", headers=operator)

    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "library cursor is invalid"}
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Library recipe not found"}
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Library projection unavailable"}

    schema = operation_api.admin_openapi_schema(client.app)
    paths = {
        path: value
        for path, value in schema["paths"].items()
        if path.startswith("/api/v1/library")
    }
    assert set(paths) == {
        "/api/v1/library",
        "/api/v1/library/recipes/{recipe_id}",
    }
    assert all(set(path_item) == {"get"} for path_item in paths.values())


def test_library_openapi_has_stable_operations_typed_models_and_bearer_security() -> (
    None
):
    client, _operator, _library = _client()
    schema = operation_api.admin_openapi_schema(client.app)
    paths = schema["paths"]

    root = paths["/api/v1/library"]["get"]
    detail = paths["/api/v1/library/recipes/{recipe_id}"]["get"]
    assert root["operationId"] == "listLibrary"
    assert detail["operationId"] == "getLibraryRecipe"
    assert root["security"] == [{"BearerAuth": []}]
    assert detail["security"] == [{"BearerAuth": []}]
    assert root["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/LibrarySnapshot"
    }
    assert detail["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/LibraryRecipeDetail"
    }
    assert {"401", "422", "503"} <= set(root["responses"])
    assert {"401", "404", "422", "503"} <= set(detail["responses"])
    for component in (
        "LibrarySnapshot",
        "LibraryRecipeDetail",
        "PlacementRecommendation",
        "MappingPreviewTarget",
        "InstallPreviewTarget",
        "RunPreviewTarget",
    ):
        assert (
            schema["components"]["schemas"][component]["additionalProperties"] is False
        )


def test_library_openapi_exposes_bounded_job_and_territorial_contracts() -> None:
    client, _operator, _library = _client()
    schemas = client.app.openapi()["components"]["schemas"]

    for component in (
        "VisualRecipeParameter",
        "VisualInputSlot",
        "VisualInterfaceInput",
        "VisualInterfaceOutput",
        "VisualModelLicense",
        "VisualTerritorialRestrictions",
    ):
        assert schemas[component]["additionalProperties"] is False

    recipe = schemas["VisualRecipeDocument"]["properties"]
    assert recipe["parameters"]["items"] == {
        "$ref": "#/components/schemas/VisualRecipeParameter"
    }
    assert "model_license" in recipe
    restriction_properties = schemas["VisualTerritorialRestrictions"]["properties"]
    assert set(restriction_properties) == {"denied_jurisdictions", "notice"}
    assert set(schemas["VisualInterfaceInput"]["properties"]) == {
        "path",
        "required",
        "media_types",
        "max_bytes",
        "min_files",
        "max_files",
        "slots",
    }
    assert "timeout_seconds" in schemas["VisualInterface"]["properties"]


def test_library_openapi_reason_schema_has_a_unique_strict_identity() -> None:
    """Library responses must not collide with Fleet in generated clients."""

    client, _operator, _library = _client()
    schema = operation_api.admin_openapi_schema(client.app)
    schemas = schema["components"]["schemas"]
    library_reason_ref = (
        "#/components/schemas/vonk_control__library_contract__ProjectionReason"
    )
    library_reason = schemas[library_reason_ref.rsplit("/", 1)[-1]]
    fleet_reason = schemas["vonk_control__fleet_projection__ProjectionReason"]

    assert library_reason["title"] == "LibraryProjectionReason"
    assert fleet_reason["title"] == "ProjectionReason"
    assert library_reason["additionalProperties"] is False

    reachable = set()
    pending = [
        "LibrarySnapshot",
        "LibraryRecipeDetail",
    ]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        component = schemas[name]
        references: list[str] = []
        values = [component]
        while values:
            value = values.pop()
            if isinstance(value, dict):
                reference = value.get("$ref")
                if isinstance(reference, str):
                    references.append(reference)
                values.extend(value.values())
            elif isinstance(value, list):
                values.extend(value)
        for reference in references:
            if reference.startswith("#/components/schemas/"):
                dependency = reference.rsplit("/", 1)[-1]
                assert dependency in schemas
                pending.append(dependency)

    reason_references = [
        component["properties"]["reasons"]["items"]["$ref"]
        for component in (schemas[name] for name in reachable)
        if "reasons" in component.get("properties", {})
    ]
    assert reason_references
    assert set(reason_references) == {library_reason_ref}

    with pytest.raises(ValidationError):
        LibraryProjectionReason(
            code=1,
            detail="The stored recipe document is invalid.",
            severity="error",
        )
    with pytest.raises(ValidationError):
        LibraryProjectionReason(
            code="recipe.document_invalid",
            detail="The stored recipe document is invalid.",
            severity="error",
            unexpected=True,
        )
