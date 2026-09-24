"""Library reads exercise real placement, cache, and authenticated route owners."""

import json
import uuid
from dataclasses import replace
from datetime import timedelta
from importlib import resources
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_control.auth import Actor, TokenCodec
from vonk_control.execution_plan_service import _build_package
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.library_api import install_library_routes
from vonk_control.library_assessment import LibraryAssessment
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ArtifactSetManifest, ModelCacheService
from vonk_control.models import (
    AgentNode,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    Job,
    ModelCacheSet,
    NodeInventorySnapshot,
    RecipeBuild,
    RuntimeImageAuthorization,
)
from vonk_control.recipe_runtime_specs import (
    compile_runtime_spec,
    resolve_recipe_entities,
)
from vonk_control.run_switch_operations import RunSwitchOperationService
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from cluster_profiles.cli import main
from cluster_profiles.cli_render import render_payload
from cluster_profiles.control_client import ControlClient

from .test_library_canonical_projection import _insert_canonical_rows
from .test_model_cache import _write_object_receipt
from .test_recipe_operations import NOW, setup_services
from .test_run_switch_operations import RecordingArtifactExecutor


@pytest.fixture
def assessed_library(tmp_path: Path):
    model = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    model["source"]["repository"] = "https://huggingface.co/vonk-forge/synthetic-tiny"
    digest = content_sha256(ModelDefinition.model_validate(model))

    def bind_model(recipe):
        recipe["models"][0]["model"]["content_sha256"] = digest

    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        model_transform=lambda document: document.update(model),
        recipe_transform=bind_model,
    )
    with sessions.begin() as session:
        for row in session.scalars(select(CatalogDocumentRevision)):
            session.add(
                CatalogDocumentHead(
                    kind=row.kind,
                    publisher=row.publisher,
                    slug=row.slug,
                    active_revision_id=row.id,
                )
            )
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime-images")
    cache = ModelCacheService(
        sessions,
        tmp_path / "model-cache",
        reserve_bytes=0,
        clock=lambda: NOW,
        runtime_archive_available=storage.build_archive_available,
    )
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        model_cache=cache,
        clock=lambda: NOW,
        build_archive_available=storage.build_archive_available,
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    assessment = LibraryAssessment(
        sessions,
        run_switch=service,
        model_cache=cache,
        clock=lambda: NOW,
    )
    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"a" * 32).cursor_codec(),
        clock=lambda: NOW,
        runtime_archive_available=storage.build_archive_available,
        assessment=assessment,
    )
    app = FastAPI()
    install_library_routes(
        app,
        actor_dependency=Depends(lambda: Actor("viewer", "viewer")),
        projection=projection,
    )
    return (
        projection,
        sessions,
        cache,
        service,
        app,
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        storage,
    )


def test_missing_nas_assets_do_not_hide_a_fitting_recipe_or_create_work(
    assessed_library,
    capsys,
    tmp_path,
):
    projection, sessions, _, _, app, *_ = assessed_library
    with sessions() as session:
        builds = [(row.id, row.state) for row in session.scalars(select(RecipeBuild))]
        jobs = tuple(session.scalars(select(Job.id)))
    with TestClient(app) as client:
        response = client.get(
            "/api/recipe/library", params={"all_models": True, "fits_fleet": True}
        )
        assert response.status_code == 200, response.text
        rows = response.json()["recipes"]
        assert len(rows) == 1
        check = rows[0]["assessment"]
        assert check["fleet_fit"]["state"] == "ready", check
        assert check["cache"]["state"] == "blocked"
        assert check["readiness"]["state"] == "blocked"
        assert any(
            "model-not-cached" in reason["detail"]
            for reason in check["cache"]["reasons"]
        )
        render_payload(response.json(), "recipe", action="library")
        rendered = capsys.readouterr().out
        assert "Fleet fit: ready" in rendered
        assert "Exact NAS assets: blocked" in rendered
        assert "Readiness: blocked" in rendered
        assert rendered.count(check["cache"]["reasons"][0]["detail"]) == 1
        token = tmp_path / "client-token"
        token.write_text("fixture-token")
        token.chmod(0o600)

        class OpenedResponse(BytesIO):
            def __init__(self, response):
                super().__init__(response.content)
                self.status = response.status_code
                self.headers = response.headers

            def __exit__(self, *_args: object) -> None:
                self.close()

        def opener(request, timeout):
            return OpenedResponse(
                client.get(request.selector, headers=dict(request.header_items()))
            )

        control = ControlClient("https://forge.example.test", token, opener=opener)
        assert (
            main(
                ("recipe", "library", "--all-models", "--fits-fleet", "--json"),
                control_client=control,
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["recipes"][0]["assessment"] == check
        response = client.get(
            "/api/recipe/library", params={"all_models": True, "ready": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["recipes"] == []
    with sessions() as session:
        assert [
            (row.id, row.state) for row in session.scalars(select(RecipeBuild))
        ] == builds
        assert tuple(session.scalars(select(Job.id))) == jobs
    assert (
        projection.recipe_detail(rows[0]["selector"]).assessment
        == projection.recipe_library(all_models=True).recipes[0].assessment
    )


def test_stale_capacity_is_unavailable_and_filter_cannot_report_empty_success(
    assessed_library,
):
    _, sessions, _, _, app, *_ = assessed_library
    with sessions.begin() as session:
        for row in session.scalars(select(NodeInventorySnapshot)):
            row.observed_at = NOW - timedelta(hours=1)
    with TestClient(app) as client:
        response = client.get("/api/recipe/library", params={"all_models": True})
        assert response.status_code == 200, response.text
        assert (
            response.json()["recipes"][0]["assessment"]["fleet_fit"]["state"]
            == "unavailable"
        )
        response = client.get(
            "/api/recipe/library", params={"all_models": True, "fits_fleet": True}
        )
        assert response.status_code == 503, response.text
        assert "remove the readiness filter" in response.json()["detail"]
        response = client.get(
            "/api/recipe/library",
            params={"all_models": True, "assess": False, "ready": True},
        )
        assert response.status_code == 422


def _publish_model_fixture(cache, sessions, manifest: ArtifactSetManifest):
    paths = []
    for artifact in manifest.artifacts:
        path = cache.root / "objects" / artifact.sha256[:2] / artifact.sha256
        path.parent.mkdir(parents=True, exist_ok=True)
        # Receipt publication is outside this read-boundary test; admission
        # trusts the verified receipt and checks real presence and length.
        with path.open("wb") as output:
            output.truncate(artifact.expected_bytes)
        _write_object_receipt(cache, artifact.sha256, artifact.expected_bytes)
        paths.append(path)
    with sessions.begin() as session:
        session.add(
            ModelCacheSet(
                artifact_set_sha256=manifest.digest,
                schema_version=2,
                model_content_sha256=manifest.model_content_sha256,
                recipe_revision_sha256=manifest.recipe_revision_sha256,
                manifest=manifest.document(),
                expected_bytes=manifest.expected_bytes,
                verified_bytes=manifest.expected_bytes,
                state="cached",
                created_at=NOW,
                updated_at=NOW,
                verified_at=NOW,
                last_accessed_at=NOW,
            )
        )
    return paths


def test_ready_uses_actual_nas_files_and_does_not_require_spark_copies(
    assessed_library,
):
    projection, sessions, cache, _, _, _, _, build_id, _, storage = assessed_library
    recipe = projection.recipe_library(all_models=True, assess=False).recipes[0]
    manifest = cache.resolve_artifact_set(
        recipe_revision_id=recipe.identity.recipe_revision_id
    )
    paths = _publish_model_fixture(cache, sessions, manifest)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        resolved = resolve_recipe_entities(
            session, recipe.document.model_dump(mode="json")
        )
        runtime = compile_runtime_spec(
            recipe.document,
            resolved_entities=resolved,
            role="entrypoint",
            rank=0,
            package_handle=_build_package(build),
        )
        identity = runtime["identity"]
        assert isinstance(identity, dict)
        # Seed already-authorized image metadata over the real fixture archive.
        # Authorization issuance and archive publication have their own tests.
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=recipe.identity.recipe_revision_id,
                source="controller-build",
                original_content_digest=recipe.identity.content_sha256,
                effective_execution_key=identity["execution_sha256"],
                platform_manifest_digest=build.image_digest,
                local_image_config_id="sha256:" + "4" * 64,
                oci_archive_sha256=build.oci_layout_sha256,
                image_bytes=build.image_bytes,
                build_id=build.id,
                authorized_at=NOW,
                state="authorized",
            )
        )
    ready = projection.recipe_library(all_models=True).recipes[0].assessment
    assert ready is not None
    assert ready.readiness.state == "ready", ready.readiness.model_dump(mode="json")
    assert ready.fleet_fit.state == "ready"
    assert ready.cache.state == "ready"
    assert len(projection.recipe_library(all_models=True, ready=True).recipes) == 1
    # A cached subset for the same primary model is not the exact recipe set.
    other = replace(
        manifest,
        artifacts=(
            replace(
                manifest.artifacts[0],
                key="other",
                artifact_id="other",
                path="other.safetensors",
                sha256="e" * 64,
            ),
        ),
    )
    _publish_model_fixture(cache, sessions, other)
    paths[0].unlink()
    missing = projection.recipe_library(all_models=True).recipes[0].assessment
    assert missing is not None
    assert missing.cache.state == "blocked"
    assert missing.fleet_fit.state == "ready"
    assert missing.readiness.state == "blocked"
    with paths[0].open("wb") as output:
        output.truncate(manifest.artifacts[0].expected_bytes)
    archive = storage.root / build.oci_layout_sha256
    archive.unlink()
    missing = projection.recipe_library(all_models=True).recipes[0].assessment
    assert missing is not None
    assert missing.readiness.state == "blocked"
    assert any("recipe-not-cached" in reason.detail for reason in missing.cache.reasons)


def test_fit_search_continues_after_an_ineligible_first_group(assessed_library):
    projection, sessions, *_ = assessed_library
    later = "spk_" + "f" * 32
    with sessions.begin() as session:
        node = session.scalar(select(AgentNode))
        assert node is not None
        capabilities = tuple(node.capabilities)
        session.add(
            AgentNode(
                node_id=later,
                state="active",
                architecture="linux-arm64",
                capabilities=list(capabilities),
            )
        )
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_free_bytes = 0
        snapshot.gpu_memory_free_bytes = 0
    InventoryRepository(sessions, clock=lambda: NOW).record(
        InventorySnapshotInput(
            later,
            NOW,
            10_000,
            8_000,
            10_000,
            8_000,
            10_000,
            8_000,
            1,
            False,
            capabilities,
            memory_pool="shared",
        )
    )
    rows = projection.recipe_library(all_models=True, fits_fleet=True).recipes
    assert len(rows) == 1
    assessment = rows[0].assessment
    assert assessment is not None and assessment.group is not None
    assert [node.node_id for node in assessment.group.nodes] == [later]


def test_late_assessment_is_discarded_and_selector_scan_does_not_assess(
    assessed_library, monkeypatch
):
    import vonk_control.library_assessment as assessment_module

    projection, _, _, service, app, *_ = assessed_library
    clock = [0.0]
    calls = []
    inspect = service.inspect_candidate

    def slow_inspect(*args, **kwargs):
        result = inspect(*args, **kwargs)
        calls.append(result)
        clock[0] += 6.0
        return result

    monkeypatch.setattr(
        assessment_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    monkeypatch.setattr(service, "inspect_candidate", slow_inspect)
    projection.recipe_library(all_models=True, assess=False)
    assert calls == []
    with TestClient(app) as client:
        response = client.get(
            "/api/recipe/library", params={"all_models": True, "fits_fleet": True}
        )
        assert response.status_code == 503, response.text
    assert len(calls) == 1
    assessment = projection.recipe_library(all_models=True).recipes[0].assessment
    assert assessment is not None
    assert assessment.fleet_fit.state == "unavailable"
    assert "5-second" in assessment.fleet_fit.reasons[0].detail


def test_fleet_filter_assesses_later_candidates_before_pagination(assessed_library):
    projection, sessions, *_ = assessed_library
    recipe = projection.recipe_library(all_models=True, assess=False).recipes[0]
    _insert_canonical_rows(
        sessions,
        kind="recipe",
        template=recipe.document.model_dump(mode="json"),
        count=2,
    )
    with sessions.begin() as session:
        first = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.slug == "recipe-0000"
            )
        )
        assert first is not None
        document = json.loads(json.dumps(first.document))
        memory = document["topology"]["roles"][0]["resources"]["memory"]
        memory.update(startup_peak_bytes=20_000, steady_state_bytes=20_000)
        canonical = RecipeDefinition.model_validate(document)
        successor = CatalogDocumentRevision(
            id=str(uuid.uuid4()),
            document_id=first.document_id,
            kind="recipe",
            publisher=first.publisher,
            slug=first.slug,
            revision_number=2,
            schema_version=2,
            state="active",
            document=canonical.model_dump(mode="json"),
            content_digest=content_sha256(canonical),
            projected={},
            created_by="test",
            created_at=NOW,
        )
        session.add(successor)
        session.flush()
        head = session.scalar(
            select(CatalogDocumentHead).where(
                CatalogDocumentHead.kind == "recipe",
                CatalogDocumentHead.publisher == first.publisher,
                CatalogDocumentHead.slug == first.slug,
            )
        )
        assert head is not None
        head.active_revision_id = successor.id
    page = projection.recipe_library(
        all_models=True, publisher=["test"], limit=1, sort="name", fits_fleet=True
    )
    assert [item.selector for item in page.recipes] == ["test/recipe-0001"]
    assert page.next_cursor is None
