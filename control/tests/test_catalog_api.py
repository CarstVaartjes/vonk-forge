from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.responses import JSONResponse
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, AuthError, TokenCodec
from vonk_control.catalog_api import (
    PublicRecipeTopologyRole,
    _canonical_source_repository,
    _public_recipe_execution_readiness,
    _public_recipe_qualification,
    install_catalog_routes,
)
from vonk_control.catalog_service import CatalogService
from vonk_control.global_catalog import GlobalRecipeRevision
from vonk_control.models import Base
from vonk_control.recipe_contract import recipe_content_sha256
from vonk_control.recipe_library import (
    RecipeLibraryChange,
    RecipeLibraryItem,
    RecipeLibraryRelease,
    RecipeLibrarySnapshot,
)
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle

from .test_catalog_entities import model_group
from .test_catalog_service import _seed_recipe_dependencies


class Jobs:
    def get(self, job_id: str):
        raise KeyError(job_id)

    def list(self, *, limit: int = 100):
        return []

    def list_page(self, **_kwargs):
        return [], None, 0


@pytest.mark.parametrize(
    ("name", "count"),
    [("a", 1), ("model_worker", 33), ("a" * 64, 1_000)],
)
def test_public_recipe_topology_role_matches_canonical_recipe_name_and_count(
    name: str, count: int
) -> None:
    role = PublicRecipeTopologyRole(name=name, count=count, endpoint_owner=False)

    assert role.name == name
    assert role.count == count


def _catalog_app(
    codec, audits, service, global_catalog=None, recipe_library=None
) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        request.state.request_id = request.headers.get(
            "x-request-id", "00000000-0000-4000-8000-000000000001"
        )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_catalog_request(request: Request, error: RequestValidationError):
        if request.url.path.startswith("/api/v1/catalog/"):
            return JSONResponse(
                status_code=422,
                content={
                    "code": "catalog.invalid_request",
                    "detail": "catalog request is invalid",
                    "request_id": request.state.request_id,
                },
            )
        return await request_validation_exception_handler(request, error)

    def actor(request: Request) -> Actor:
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return codec.verify(authorization[7:], now=10)
        except AuthError:
            raise HTTPException(
                status_code=401, detail="authentication failed"
            ) from None

    install_catalog_routes(
        app,
        actor_dependency=Depends(actor),
        audits=audits,
        service=service,
        global_catalog=global_catalog,
        recipe_library=recipe_library,
    )
    return app


def _api_model_group() -> dict[str, object]:
    document = model_group()
    document["identity"]["slug"] = "api-synthetic"
    document["family"] = "api-synthetic"
    return document


@pytest.fixture
def recipe_document() -> dict[str, object]:
    path = Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def api(tmp_path: Path, recipe_document):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    codec = TokenCodec(b"c" * 32)
    service = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        cursors=codec.cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )
    _seed_recipe_dependencies(service, recipe_document)
    audits = MemoryAuditStore()
    app = _catalog_app(codec, audits, service)

    def headers(role: str) -> dict[str, str]:
        token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
        return {"Authorization": f"Bearer {token}"}

    return TestClient(app), headers, audits


@pytest.fixture
def bridge_api(tmp_path: Path, recipe_document):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'bridge.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    codec = TokenCodec(b"b" * 32)
    service = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        cursors=codec.cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\nUSER 65532:65532\n"})
    recipe_document = copy.deepcopy(recipe_document)
    recipe_document["build"]["context"]["sha256"] = bundle.sha256
    recipe_document["build"]["context"]["expected_bytes"] = len(bundle.archive)
    _seed_recipe_dependencies(service, recipe_document)
    digest = recipe_content_sha256(recipe_document)
    remote = GlobalRecipeRevision(
        publisher="vonk-forge",
        slug="synthetic-tiny-openai",
        recipe_id="00000000-0000-4000-8000-000000000001",
        revision_number=1,
        revision_id="10000000-0000-4000-8000-000000000001",
        content_sha256=digest,
        published_at="2026-08-07T10:00:00+00:00",
        document=recipe_document,
    )

    class Global:
        def fetch(self, uri: str):
            assert uri == remote.uri
            return remote

        def fetch_source_bundle(self, sha256: str):
            assert sha256 == bundle.sha256
            return bundle.archive

    audits = MemoryAuditStore()
    app = _catalog_app(codec, audits, service, Global())

    def headers(role: str) -> dict[str, str]:
        token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
        return {"Authorization": f"Bearer {token}"}

    return TestClient(app), headers, audits, service, remote


def test_operator_cannot_author_recipe(api, recipe_document) -> None:
    client, headers, _audits = api
    response = client.post(
        "/api/v1/catalog/recipes",
        headers=headers("operator"),
        json={"slug": "synthetic-tiny-openai", "document": recipe_document},
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("tags", "qualification", "basis"),
    [
        (
            {"accepted", "openai"},
            "cataloged",
            "explicit-accepted-metadata",
        ),
        (
            {"candidate", "openai"},
            "candidate",
            "explicit-candidate-metadata",
        ),
        (
            {"accepted", "candidate"},
            "candidate",
            "conflicting-metadata",
        ),
        (
            {"openai"},
            "candidate",
            "missing-accepted-metadata",
        ),
    ],
)
def test_public_recipe_qualification_requires_explicit_unambiguous_acceptance(
    tags: set[str], qualification: str, basis: str
) -> None:
    actual_qualification, actual_basis, detail = _public_recipe_qualification(tags)

    assert actual_qualification == qualification
    assert actual_basis == basis
    assert detail


@pytest.mark.parametrize(
    ("tags", "readiness"),
    [
        ({"accepted", "executable"}, "executable"),
        ({"accepted", "chat"}, "not-declared"),
        ({"candidate", "integration-required"}, "integration-required"),
        ({"metadata-only", "non-executable"}, "not-executable"),
        ({"integration-required", "non-executable"}, "not-executable"),
        ({"executable", "integration-required"}, "integration-required"),
        ({"metadata-only"}, "not-declared"),
    ],
)
def test_public_recipe_readiness_is_independent_and_fails_closed(
    tags: set[str], readiness: str
) -> None:
    actual, basis, detail = _public_recipe_execution_readiness(tags)

    assert actual == readiness
    assert basis
    assert detail


def test_create_list_get_and_resolve_recipe(api, recipe_document) -> None:
    client, headers, audits = api
    request_id = "20000000-0000-4000-8000-000000000002"
    created = client.post(
        "/api/v1/catalog/recipes",
        headers={**headers("administrator"), "x-request-id": request_id},
        json={"slug": "synthetic-tiny-openai", "document": recipe_document},
    )
    assert created.status_code == 201
    recipe_id = created.json()["recipe_id"]

    listed = client.get("/api/v1/catalog/recipes", headers=headers("viewer"))
    detail = client.get(
        f"/api/v1/catalog/recipes/{recipe_id}", headers=headers("viewer")
    )
    resolved = client.post(
        f"/api/v1/catalog/recipes/{recipe_id}/resolve",
        headers=headers("administrator"),
        json={"expected_revision": 1},
    )

    assert listed.status_code == detail.status_code == resolved.status_code == 200
    assert listed.json()["recipes"][0]["origin"] == "local"
    assert listed.json()["recipes"][0]["source_bundle_sha256"] == "c" * 64
    assert listed.json()["recipes"][0]["execution_harness"] == "vllm-openai"
    assert listed.json()["recipes"][0]["runtime_distribution"] == "python-312-cuda"
    assert listed.json()["recipes"][0]["topology_name"] == "solo"
    assert listed.json()["recipes"][0]["topology_mode"] == "single"
    assert listed.json()["recipes"][0]["node_count"] == 1
    assert "runtime_image" not in listed.json()["recipes"][0]
    assert detail.json()["document"] == recipe_document
    assert resolved.json()["lifecycle"] == "resolved"
    assert len(resolved.json()["content_sha256"]) == 64
    audit = audits.for_request(request_id)
    assert audit.action == "catalog.recipe.create"
    assert audit.targets == (recipe_id,)


def test_stale_draft_returns_stable_problem(api, recipe_document) -> None:
    client, headers, _audits = api
    created = client.post(
        "/api/v1/catalog/recipes",
        headers=headers("administrator"),
        json={"slug": "synthetic-tiny-openai", "document": recipe_document},
    ).json()
    recipe_document["metadata"]["title"] = "Updated"
    first = client.put(
        f"/api/v1/catalog/recipes/{created['recipe_id']}/draft",
        headers=headers("administrator"),
        json={"expected_revision": 1, "document": recipe_document},
    )
    stale = client.put(
        f"/api/v1/catalog/recipes/{created['recipe_id']}/draft",
        headers=headers("administrator"),
        json={"expected_revision": 1, "document": recipe_document},
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["code"] == "catalog.stale_revision"
    assert len(stale.json()["request_id"]) == 36


def test_resolved_recipe_accepts_a_new_draft_revision(api, recipe_document) -> None:
    client, headers, _audits = api
    created = client.post(
        "/api/v1/catalog/recipes",
        headers=headers("administrator"),
        json={"slug": "synthetic-tiny-openai", "document": recipe_document},
    ).json()
    resolved = client.post(
        f"/api/v1/catalog/recipes/{created['recipe_id']}/resolve",
        headers=headers("administrator"),
        json={"expected_revision": created["revision_number"]},
    ).json()
    changed = copy.deepcopy(recipe_document)
    changed["metadata"]["title"] = "Changed title"

    drafted = client.put(
        f"/api/v1/catalog/recipes/{created['recipe_id']}/draft",
        headers=headers("administrator"),
        json={
            "expected_revision": resolved["revision_number"],
            "document": changed,
        },
    )

    assert drafted.status_code == 200
    assert drafted.json()["lifecycle"] == "draft"
    assert drafted.json()["revision_number"] == resolved["revision_number"] + 1
    assert drafted.json()["document"] == changed


def test_recipe_body_is_bounded_and_unknown_fields_are_rejected(
    api, recipe_document
) -> None:
    client, headers, _audits = api
    response = client.post(
        "/api/v1/catalog/recipes",
        headers=headers("administrator"),
        json={
            "slug": "synthetic-tiny-openai",
            "document": recipe_document,
            "authorization": "Bearer never-reflect-me",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "catalog.invalid_request"
    assert "authorization" not in response.text.lower()
    assert "never-reflect-me" not in response.text


def test_catalog_operation_ids_are_stable(api) -> None:
    client, _headers, _audits = api
    paths = client.get("/openapi.json").json()["paths"]

    assert paths["/api/v1/catalog/recipes"]["get"]["operationId"] == "listLocalRecipes"
    assert (
        paths["/api/v1/catalog/recipes"]["post"]["operationId"] == "createLocalRecipe"
    )
    assert (
        paths["/api/v1/catalog/recipes/{recipe_id}"]["get"]["operationId"]
        == "getLocalRecipe"
    )
    assert (
        paths["/api/v1/catalog/recipes/{recipe_id}/draft"]["put"]["operationId"]
        == "updateLocalRecipeDraft"
    )
    assert (
        paths["/api/v1/catalog/recipes/{recipe_id}/resolve"]["post"]["operationId"]
        == "resolveLocalRecipe"
    )
    assert (
        paths["/api/v1/catalog/recipes/{recipe_id}/fork"]["post"]["operationId"]
        == "forkLocalRecipe"
    )
    assert (
        paths["/api/v1/catalog/source-bundles/{sha256}"]["get"]["operationId"]
        == "downloadLocalRecipeSourceBundle"
    )
    assert (
        paths["/api/v1/catalog/source-bundles/{sha256}"]["put"]["operationId"]
        == "uploadLocalRecipeSourceBundle"
    )
    assert (
        paths["/api/v1/catalog/entities"]["get"]["operationId"] == "listCatalogEntities"
    )
    assert (
        paths["/api/v1/catalog/entities"]["post"]["operationId"]
        == "createCatalogEntityDraft"
    )
    assert (
        paths["/api/v1/catalog/entities/{entity_id}"]["get"]["operationId"]
        == "getCatalogEntity"
    )
    assert (
        paths["/api/v1/catalog/entities/{entity_id}/draft"]["put"]["operationId"]
        == "reviseCatalogEntity"
    )
    assert (
        paths["/api/v1/catalog/entities/{entity_id}/resolve"]["post"]["operationId"]
        == "resolveCatalogEntity"
    )


def test_administrator_authors_and_resolves_a_catalog_entity(api) -> None:
    client, headers, audits = api
    denied = client.post(
        "/api/v1/catalog/entities",
        headers=headers("operator"),
        json={"document": _api_model_group()},
    )
    assert denied.status_code == 403
    assert denied.json() == {
        "code": "catalog.entity_forbidden",
        "detail": "administrator role is required for catalog entity authoring",
        "request_id": "00000000-0000-4000-8000-000000000001",
    }

    created = client.post(
        "/api/v1/catalog/entities",
        headers=headers("administrator"),
        json={"document": _api_model_group()},
    )
    assert created.status_code == 201
    entity_id = created.json()["entity_id"]
    assert created.json()["lifecycle"] == "draft"

    listed = client.get(
        "/api/v1/catalog/entities?kind=model-group", headers=headers("viewer")
    )
    detail = client.get(
        f"/api/v1/catalog/entities/{entity_id}", headers=headers("viewer")
    )
    resolved = client.post(
        f"/api/v1/catalog/entities/{entity_id}/resolve",
        headers=headers("administrator"),
        json={"expected_revision": 1},
    )

    assert listed.status_code == detail.status_code == resolved.status_code == 200
    assert entity_id in {item["entity_id"] for item in listed.json()["entities"]}
    assert detail.json()["revision_id"] == created.json()["revision_id"]
    assert resolved.json()["revision_number"] == 2
    assert resolved.json()["lifecycle"] == "resolved"
    assert len(resolved.json()["content_sha256"]) == 64
    assert any(event.action == "catalog.entity.resolve" for event in audits.list())


def test_entity_not_found_responses_match_the_documented_problem(api) -> None:
    client, headers, _audits = api
    missing = "10000000-0000-4000-8000-000000000099"
    request_id = "30000000-0000-4000-8000-000000000003"
    expected = {
        "code": "catalog.entity_not_found",
        "detail": "catalog entity or revision was not found",
        "request_id": request_id,
    }

    responses = [
        client.get(
            f"/api/v1/catalog/entities/{missing}",
            headers={**headers("viewer"), "x-request-id": request_id},
        ),
        client.put(
            f"/api/v1/catalog/entities/{missing}/draft",
            headers={**headers("administrator"), "x-request-id": request_id},
            json={"expected_revision": 1, "document": _api_model_group()},
        ),
        client.post(
            f"/api/v1/catalog/entities/{missing}/resolve",
            headers={**headers("administrator"), "x-request-id": request_id},
            json={"expected_revision": 1},
        ),
    ]

    assert [(response.status_code, response.json()) for response in responses] == [
        (404, expected),
        (404, expected),
        (404, expected),
    ]


def test_entity_stale_revision_returns_full_conflict_problem(api) -> None:
    client, headers, _audits = api
    created = client.post(
        "/api/v1/catalog/entities",
        headers=headers("administrator"),
        json={"document": _api_model_group()},
    ).json()
    changed = _api_model_group()
    changed["metadata"]["title"] = "Synthetic Updated"
    client.put(
        f"/api/v1/catalog/entities/{created['entity_id']}/draft",
        headers=headers("administrator"),
        json={"expected_revision": 1, "document": changed},
    )

    stale = client.post(
        f"/api/v1/catalog/entities/{created['entity_id']}/resolve",
        headers=headers("administrator"),
        json={"expected_revision": 1},
    )

    assert stale.status_code == 409
    assert stale.json() == {
        "code": "catalog.stale_entity_revision",
        "detail": "catalog entity revision changed",
        "request_id": "00000000-0000-4000-8000-000000000001",
    }


def test_entity_list_pages_one_hundred_and_one_rows(api) -> None:
    client, headers, _audits = api
    for index in range(100):
        document = _api_model_group()
        slug = f"api-page-{index:03d}"
        document["identity"]["slug"] = slug
        document["metadata"]["title"] = f"API Page {index:03d}"
        document["family"] = slug
        response = client.post(
            "/api/v1/catalog/entities",
            headers=headers("administrator"),
            json={"document": document},
        )
        assert response.status_code == 201

    first = client.get(
        "/api/v1/catalog/entities",
        params={"kind": "model-group", "limit": 100},
        headers=headers("viewer"),
    )
    cursor = first.json()["next_cursor"]
    second = client.get(
        "/api/v1/catalog/entities",
        params={"kind": "model-group", "limit": 100, "cursor": cursor},
        headers=headers("viewer"),
    )

    assert first.status_code == second.status_code == 200
    assert len(first.json()["entities"]) == 100
    assert cursor.startswith("v1.")
    assert len(second.json()["entities"]) == 1
    assert second.json()["next_cursor"] is None


def test_catalog_entity_revision_preserves_identity_and_rejects_secrets(api) -> None:
    client, headers, _audits = api
    created = client.post(
        "/api/v1/catalog/entities",
        headers=headers("administrator"),
        json={"document": _api_model_group()},
    ).json()
    changed = _api_model_group()
    changed["metadata"]["title"] = "Synthetic Updated"
    revised = client.put(
        f"/api/v1/catalog/entities/{created['entity_id']}/draft",
        headers=headers("administrator"),
        json={"expected_revision": 1, "document": changed},
    )
    assert revised.status_code == 200
    assert revised.json()["revision_number"] == 2

    secret = _api_model_group()
    secret["metadata"]["credential"] = "never-reflect-me"
    rejected = client.post(
        "/api/v1/catalog/entities",
        headers=headers("administrator"),
        json={"document": secret},
    )
    assert rejected.status_code == 422
    assert rejected.json() == {
        "code": "catalog.sensitive_field",
        "detail": "sensitive field is forbidden at $.metadata.credential",
        "request_id": "00000000-0000-4000-8000-000000000001",
    }
    assert "never-reflect-me" not in rejected.text


def test_oversized_entity_uses_entity_specific_problem_wording(api) -> None:
    client, headers, _audits = api
    document = _api_model_group()
    document["metadata"]["description"] = "x" * (256 * 1024)

    rejected = client.post(
        "/api/v1/catalog/entities",
        headers=headers("administrator"),
        json={"document": document},
    )

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "catalog.document_too_large"
    assert rejected.json()["detail"] == "catalog entity document exceeds 256 KiB"


def test_administrator_uploads_a_digest_verified_source_bundle(api) -> None:
    client, headers, audits = api
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\nUSER 65532:65532\n"})

    denied = client.put(
        f"/api/v1/catalog/source-bundles/{bundle.sha256}",
        headers={**headers("operator"), "content-type": "application/x-tar"},
        content=bundle.archive,
    )
    uploaded = client.put(
        f"/api/v1/catalog/source-bundles/{bundle.sha256}",
        headers={**headers("administrator"), "content-type": "application/x-tar"},
        content=bundle.archive,
    )

    assert denied.status_code == 403
    assert uploaded.status_code == 200
    assert uploaded.json() == {
        "sha256": bundle.sha256,
        "archive_bytes": len(bundle.archive),
        "total_bytes": bundle.manifest.total_bytes,
        "file_count": 1,
        "files": ["Dockerfile"],
    }
    downloaded = client.get(
        f"/api/v1/catalog/source-bundles/{bundle.sha256}",
        headers=headers("viewer"),
    )
    assert downloaded.status_code == 200
    assert downloaded.content == bundle.archive
    assert downloaded.headers["etag"] == f'"sha256:{bundle.sha256}"'
    assert downloaded.headers["content-disposition"].endswith(
        f'filename="vonk-source-{bundle.sha256}.tar"'
    )
    assert any(
        event.action == "catalog.source_bundle.upload" for event in audits.list()
    )


def test_source_bundle_upload_rejects_a_mismatched_digest(api) -> None:
    client, headers, _audits = api
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\nUSER 65532:65532\n"})

    response = client.put(
        f"/api/v1/catalog/source-bundles/{'f' * 64}",
        headers=headers("administrator"),
        content=bundle.archive,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "bundle.digest_mismatch"


def test_preview_and_explicit_global_import_are_separate(bridge_api) -> None:
    client, headers, audits, _service, remote = bridge_api
    preview = client.post(
        "/api/v1/catalog/imports/global/preview",
        headers=headers("administrator"),
        json={"uri": remote.uri},
    )
    assert preview.status_code == 200
    assert preview.json()["content_sha256"] == remote.content_sha256

    denied = client.post(
        "/api/v1/catalog/imports/global",
        headers=headers("viewer"),
        json={"uri": remote.uri, "expected_content_sha256": remote.content_sha256},
    )
    assert denied.status_code == 403

    imported = client.post(
        "/api/v1/catalog/imports/global",
        headers=headers("administrator"),
        json={"uri": remote.uri, "expected_content_sha256": remote.content_sha256},
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["origin"] == "global"
    assert imported.json()["lifecycle"] == "resolved"
    assert any(event.action == "catalog.global.import" for event in audits.list())


def test_explicit_recipe_library_import_records_commit_and_requires_admin(
    bridge_api,
) -> None:
    client, headers, audits, _service, remote = bridge_api
    body = {
        "library_commit": "a" * 40,
        "source_path": "recipes/library-copy.json",
        "expected_content_sha256": remote.content_sha256,
        "document": remote.document,
    }

    denied = client.post(
        "/api/v1/catalog/imports/recipe-library",
        headers=headers("viewer"),
        json=body,
    )
    imported = client.post(
        "/api/v1/catalog/imports/recipe-library",
        headers=headers("administrator"),
        json=body,
    )
    repeated = client.post(
        "/api/v1/catalog/imports/recipe-library",
        headers=headers("administrator"),
        json=body,
    )

    assert denied.status_code == 403
    assert imported.status_code == 201, imported.text
    assert repeated.status_code == 201, repeated.text
    assert imported.json()["origin"] == "recipe_library"
    assert repeated.json()["content_sha256"] == remote.content_sha256
    assert any(
        event.action == "catalog.recipe_library.import" for event in audits.list()
    )


def test_public_recipe_import_uses_global_catalog_for_manual_uri(bridge_api) -> None:
    client, headers, _audits, _service, remote = bridge_api

    preview = client.post(
        "/api/v1/catalog/imports/public/preview",
        headers=headers("administrator"),
        json={"uri": remote.uri},
    )
    imported = client.post(
        "/api/v1/catalog/imports/public",
        headers=headers("administrator"),
        json={"uri": remote.uri, "expected_content_sha256": remote.content_sha256},
    )

    assert preview.status_code == 200
    assert preview.json()["source"] == "global"
    assert imported.status_code == 201, imported.text
    assert imported.json()["origin"] == "global"


def test_public_recipe_import_has_one_contract_for_catalog_and_manual_sources(
    bridge_api,
) -> None:
    _client, _headers, audits, _service, remote = bridge_api

    class Library:
        def list(self):
            item = RecipeLibraryItem(
                library_commit="a" * 40,
                source_path="recipes/synthetic-tiny-openai.json",
                publisher=remote.publisher,
                slug=remote.slug,
                title="Synthetic Tiny OpenAI",
                description="A tiny public recipe.",
                tags=("synthetic",),
                content_sha256=remote.content_sha256,
                uri=remote.uri,
                document=remote.document,
            )
            return RecipeLibrarySnapshot(commit="a" * 40, items=(item,))

        def fetch(self, uri: str):
            assert uri == remote.uri
            return Library().list().items[0]

    app = _catalog_app(
        TokenCodec(b"b" * 32),
        audits,
        _service,
        recipe_library=Library(),
    )
    catalog_client = TestClient(app)
    token = TokenCodec(b"b" * 32).issue(
        Actor("administrator", "administrator"), ttl_seconds=100, now=0
    )
    auth = {"Authorization": f"Bearer {token}"}

    listed = catalog_client.get("/api/v1/catalog/public-recipes", headers=auth)
    preview = catalog_client.post(
        "/api/v1/catalog/imports/public/preview",
        headers=auth,
        json={"uri": remote.uri},
    )
    imported = catalog_client.post(
        "/api/v1/catalog/imports/public",
        headers=auth,
        json={"uri": remote.uri, "expected_content_sha256": remote.content_sha256},
    )

    assert listed.status_code == 200
    assert listed.json()["recipes"][0]["title"] == "Synthetic Tiny OpenAI"
    assert listed.json()["recipes"][0]["source_owner"] is None
    assert listed.json()["recipes"][0]["source_repository"] is None
    assert listed.json()["recipes"][0]["release_version"] is None
    assert listed.json()["recipes"][0]["local"]["status"] == "not-imported"
    assert preview.status_code == 200
    assert preview.json()["uri"] == remote.uri
    assert imported.status_code == 201, imported.text
    assert imported.json()["origin"] == "recipe_library"
    assert any(event.action == "catalog.public.import" for event in audits.list())


def test_public_recipe_preview_explains_known_update_since_local_digest(
    bridge_api,
) -> None:
    _client, _headers, audits, service, remote = bridge_api
    service.import_recipe_library(
        "admin",
        library_commit="a" * 40,
        source_path="recipes/synthetic-tiny-openai.json",
        document=remote.document,
        expected_content_sha256=remote.content_sha256,
        release_version="1.0.0",
        release_released_at="2026-08-14",
    )
    changed = copy.deepcopy(remote.document)
    changed["metadata"]["description"] = "Updated public recipe."
    changed_digest = recipe_content_sha256(changed)
    releases = (
        RecipeLibraryRelease(
            version="2.0.0",
            released_at="2026-08-23",
            content_sha256=changed_digest,
            upgrade_effect="rebuild",
            changes=(
                RecipeLibraryChange(
                    kind="fix",
                    summary="Removed a reverted upstream hotfix.",
                ),
            ),
        ),
        RecipeLibraryRelease(
            version="1.0.0",
            released_at="2026-08-14",
            content_sha256=remote.content_sha256,
            upgrade_effect="reinstall",
            changes=(RecipeLibraryChange(kind="initial", summary="Legacy baseline."),),
        ),
    )
    item = RecipeLibraryItem(
        library_commit="b" * 40,
        source_path="recipes/synthetic-tiny-openai.json",
        publisher=remote.publisher,
        slug=remote.slug,
        title="Synthetic Tiny OpenAI",
        description="Updated public recipe.",
        tags=("synthetic",),
        content_sha256=changed_digest,
        uri=f"vonk://catalog/{remote.publisher}/{remote.slug}@sha256:{changed_digest}",
        document=changed,
        release_history=releases,
    )

    class Library:
        def list(self):
            return RecipeLibrarySnapshot(commit="b" * 40, items=(item,))

        def fetch(self, uri: str):
            assert uri == item.uri
            return item

    app = _catalog_app(TokenCodec(b"u" * 32), audits, service, recipe_library=Library())
    client = TestClient(app)
    token = TokenCodec(b"u" * 32).issue(
        Actor("administrator", "administrator"), ttl_seconds=100, now=0
    )
    auth = {"Authorization": f"Bearer {token}"}

    listed = client.get("/api/v1/catalog/public-recipes", headers=auth)
    preview = client.post(
        "/api/v1/catalog/imports/public/preview",
        headers=auth,
        json={"uri": item.uri},
    )

    assert listed.status_code == 200, listed.text
    assert listed.json()["recipes"][0]["local"] == {
        "status": "update-available",
        "recipe_id": service.list_recipes()[0][0].recipe_id,
        "revision_number": 1,
        "content_sha256": remote.content_sha256,
        "release_version": "1.0.0",
    }
    assert preview.status_code == 200, preview.text
    assert [
        release["version"] for release in preview.json()["changes_since_local"]
    ] == ["2.0.0"]
    assert preview.json()["changes_since_local"][0]["changes"][0]["kind"] == "fix"


def test_public_recipe_import_materializes_fresh_dependencies_and_source_bundle(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures/recipes/dev-http-smoke"
    document = json.loads((fixture / "recipe.json").read_text(encoding="utf-8"))
    document["provenance"]["source_reference"] = (
        "https://github.com/MiaAI-Lab/GLM-5.2-NVFP4-AQLM-Triple-DGX-Sparks/"
        "tree/0123456789abcdef0123456789abcdef01234567"
    )
    document["metadata"]["tags"].append("executable")
    dependencies = tuple(
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((fixture / "entities").glob("*.json"))
    )
    bundle = generate_source_bundle(
        {
            path.name: path.read_bytes()
            for path in sorted((fixture / "context").iterdir())
        }
    )
    digest = recipe_content_sha256(document)
    item = RecipeLibraryItem(
        library_commit="f" * 40,
        source_path="recipes/dev-http-smoke.json",
        publisher="vonk",
        slug="dev-http-smoke",
        title="Development HTTP smoke fixture",
        description="Deterministic development recipe.",
        tags=("dev", "smoke"),
        content_sha256=digest,
        uri=f"vonk://catalog/vonk/dev-http-smoke@sha256:{digest}",
        document=document,
        dependencies=dependencies,
        source_bundle=bundle.archive,
    )

    class Library:
        def list(self):
            return RecipeLibrarySnapshot(commit="f" * 40, items=(item,))

        def fetch(self, uri: str):
            assert uri == item.uri
            return item

    engine = create_engine(
        f"sqlite:///{tmp_path / 'fresh-library.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    codec = TokenCodec(b"l" * 32)
    service = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        cursors=codec.cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )
    audits = MemoryAuditStore()
    app = _catalog_app(codec, audits, service, recipe_library=Library())
    client = TestClient(app)
    token = codec.issue(Actor("administrator", "administrator"), ttl_seconds=100, now=0)
    auth = {"Authorization": f"Bearer {token}"}

    listed = client.get("/api/v1/catalog/public-recipes", headers=auth)
    preview = client.post(
        "/api/v1/catalog/imports/public/preview",
        headers=auth,
        json={"uri": item.uri},
    )
    imported = client.post(
        "/api/v1/catalog/imports/public",
        headers=auth,
        json={"uri": item.uri, "expected_content_sha256": digest},
    )

    assert listed.status_code == 200
    assert preview.status_code == 200
    assert imported.status_code == 201, imported.text
    listed_recipe = listed.json()["recipes"][0]
    preview_recipe = preview.json()
    assert listed_recipe["model_publisher"] == "vonk"
    assert listed_recipe["model_slug"] == "dev-http-smoke"
    assert listed_recipe["model_title"] == "Development HTTP smoke model"
    assert listed_recipe["source_owner"] == "MiaAI-Lab"
    assert listed_recipe["source_repository"] == (
        "https://github.com/MiaAI-Lab/GLM-5.2-NVFP4-AQLM-Triple-DGX-Sparks"
    )
    assert listed_recipe["capabilities"] == ["chat"]
    assert listed_recipe["qualification"] == "candidate"
    assert listed_recipe["qualification_basis"] == "missing-accepted-metadata"
    assert "No explicit accepted qualification" in listed_recipe["qualification_detail"]
    assert listed_recipe["execution_readiness"] == "executable"
    assert listed_recipe["execution_readiness_basis"] == "explicit-executable-metadata"
    assert "fleet compatibility" in listed_recipe["execution_readiness_detail"]
    assert listed_recipe["runtime_distribution"] == "development-vllm-shim-arm64"
    assert listed_recipe["topology_mode"] == "single"
    assert listed_recipe["node_count"] == 1
    assert listed_recipe["topology_roles"] == [
        {"name": "entrypoint", "count": 1, "endpoint_owner": True}
    ]
    assert listed_recipe["fabric"] == {
        "connectivity": "none",
        "minimum_bandwidth_mbps": 0,
    }
    assert listed_recipe["expected_download_bytes"] == 10
    assert listed_recipe["maximum_installed_bytes_per_node"] > 0
    assert listed_recipe["maximum_runtime_memory_bytes_per_node"] > 0
    assert preview_recipe["model_slug"] == listed_recipe["model_slug"]
    assert preview_recipe["capabilities"] == listed_recipe["capabilities"]
    assert preview_recipe["source_owner"] == listed_recipe["source_owner"]
    assert preview_recipe["source_repository"] == listed_recipe["source_repository"]
    assert preview_recipe["qualification"] == listed_recipe["qualification"]
    assert preview_recipe["qualification_basis"] == listed_recipe["qualification_basis"]
    assert (
        preview_recipe["qualification_detail"] == listed_recipe["qualification_detail"]
    )
    assert preview_recipe["execution_readiness"] == listed_recipe["execution_readiness"]
    assert preview_recipe["execution_readiness_basis"] == listed_recipe["execution_readiness_basis"]
    assert preview_recipe["execution_readiness_detail"] == listed_recipe["execution_readiness_detail"]
    assert preview_recipe["topology_roles"] == listed_recipe["topology_roles"]
    assert preview_recipe["fabric"] == listed_recipe["fabric"]
    assert imported.json()["origin"] == "recipe_library"
    assert len(service.entities.list_entities(limit=100)[0]) == 5
    assert service.read_source_bundle(bundle.sha256) == bundle.archive


@pytest.mark.parametrize(
    ("source_reference", "expected"),
    [
        (
            "https://github.com/MiaAI-Lab/model-recipe/tree/" + "a" * 40,
            ("MiaAI-Lab", "https://github.com/MiaAI-Lab/model-recipe"),
        ),
        (
            "https://gitlab.com/research/models/recipe/-/tree/" + "b" * 40,
            ("research", "https://gitlab.com/research/models/recipe"),
        ),
        (
            "https://huggingface.co/datasets/bigscience/catalog/tree/" + "c" * 40,
            ("bigscience", "https://huggingface.co/datasets/bigscience/catalog"),
        ),
        (
            "https://huggingface.co/Qwen/Qwen3-32B/commit/" + "d" * 40,
            ("Qwen", "https://huggingface.co/Qwen/Qwen3-32B"),
        ),
        ("http://github.com/MiaAI-Lab/model-recipe", None),
        ("https://user@github.com/MiaAI-Lab/model-recipe", None),
        ("https://example.com/MiaAI-Lab/model-recipe", None),
        ("https://gitlab.com/research/models/recipe/tree/main", None),
    ],
)
def test_canonical_source_repository_accepts_only_safe_known_repository_urls(
    source_reference: str,
    expected: tuple[str, str] | None,
) -> None:
    assert _canonical_source_repository(source_reference) == expected


def test_publication_report_and_export_are_local_json_only(
    bridge_api, recipe_document
) -> None:
    client, headers, _audits, _service, remote = bridge_api
    local_document = copy.deepcopy(remote.document)
    local_document["identity"] = {"publisher": "local", "slug": "local-copy"}
    created = client.post(
        "/api/v1/catalog/recipes",
        headers=headers("administrator"),
        json={
            "slug": "local-copy",
            "document": local_document,
        },
    ).json()
    resolved = client.post(
        f"/api/v1/catalog/recipes/{created['recipe_id']}/resolve",
        headers=headers("administrator"),
        json={"expected_revision": 1},
    ).json()
    report = {
        "schema_version": 1,
        "recipe_sha256": resolved["content_sha256"],
        "source_bundle_sha256": local_document["build"]["context"]["sha256"],
        "build_input_sha256": "b" * 64,
        "image_digest": "sha256:" + "c" * 64,
        "topology_name": "solo",
        "node_count": 1,
        "runtime": {
            "agent_version": "1.0.0",
            "container_runtime": "podman",
            "architecture": "linux/arm64",
        },
        "checks": [
            {"name": "container.started", "passed": True},
            {"name": "endpoint.healthy", "passed": True},
            {"name": "inference.completed", "passed": True},
        ],
        "started_at": "2026-08-07T10:00:00+00:00",
        "finished_at": "2026-08-07T10:05:00+00:00",
    }
    attached = client.put(
        f"/api/v1/catalog/recipes/{created['recipe_id']}/publication-report",
        headers=headers("administrator"),
        json={"report": report},
    )
    assert attached.status_code == 200

    exported = client.post(
        f"/api/v1/catalog/recipes/{created['recipe_id']}/publication-export",
        headers=headers("administrator"),
        json={"publisher": "ada-lab"},
    )
    assert exported.status_code == 200
    assert exported.headers["content-disposition"].endswith(
        'filename="ada-lab-local-copy.json"'
    )
    assert exported.json()["recipe"]["identity"]["publisher"] == "ada-lab"
    assert set(exported.json()) == {"recipe", "test_report"}
