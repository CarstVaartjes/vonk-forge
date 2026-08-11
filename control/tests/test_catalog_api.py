from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.global_catalog import GlobalRecipeRevision
from vonk_control.models import Base
from vonk_control.recipe_contract import recipe_content_sha256
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle


class Jobs:
    def get(self, job_id: str):
        raise KeyError(job_id)

    def list(self, *, limit: int = 100):
        return []

    def list_page(self, **_kwargs):
        return [], None, 0


@pytest.fixture
def recipe_document() -> dict[str, object]:
    path = Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def api(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )
    codec = TokenCodec(b"c" * 32)
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
        catalog=service,
    )

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
    service = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\nUSER 65532:65532\n"})
    recipe_document = copy.deepcopy(recipe_document)
    recipe_document["build"]["context"]["sha256"] = bundle.sha256
    recipe_document["build"]["context"]["expected_bytes"] = len(bundle.archive)
    digest = recipe_content_sha256(recipe_document)
    remote = GlobalRecipeRevision(
        publisher="vonk",
        slug="qwen3-vllm",
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

    codec = TokenCodec(b"b" * 32)
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
        catalog=service,
        global_catalog=Global(),
    )

    def headers(role: str) -> dict[str, str]:
        token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
        return {"Authorization": f"Bearer {token}"}

    return TestClient(app), headers, audits, service, remote


def test_operator_cannot_author_recipe(api, recipe_document) -> None:
    client, headers, _audits = api
    response = client.post(
        "/api/v1/catalog/recipes",
        headers=headers("operator"),
        json={"slug": "qwen3-vllm", "document": recipe_document},
    )

    assert response.status_code == 403


def test_create_list_get_and_resolve_recipe(api, recipe_document) -> None:
    client, headers, audits = api
    request_id = "20000000-0000-4000-8000-000000000002"
    created = client.post(
        "/api/v1/catalog/recipes",
        headers={**headers("administrator"), "x-request-id": request_id},
        json={"slug": "qwen3-vllm", "document": recipe_document},
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
    assert listed.json()["recipes"][0]["source_bundle_sha256"] == "a" * 64
    assert listed.json()["recipes"][0]["profile_node_counts"] == [1]
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
        json={"slug": "qwen3-vllm", "document": recipe_document},
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
        json={"slug": "qwen3-vllm", "document": recipe_document},
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
            "slug": "qwen3-vllm",
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
    assert imported.status_code == 201
    assert imported.json()["origin"] == "global"
    assert imported.json()["lifecycle"] == "resolved"
    assert any(event.action == "catalog.global.import" for event in audits.list())


def test_publication_report_and_export_are_local_json_only(
    bridge_api, recipe_document
) -> None:
    client, headers, _audits, _service, _remote = bridge_api
    created = client.post(
        "/api/v1/catalog/recipes",
        headers=headers("administrator"),
        json={
            "slug": "local-copy",
            "document": {
                **recipe_document,
                "identity": {"publisher": "local", "slug": "local-copy"},
            },
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
        "source_bundle_sha256": recipe_document["build"]["context"]["sha256"],
        "build_input_sha256": "b" * 64,
        "image_digest": "sha256:" + "c" * 64,
        "deployment_profile": "solo",
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
