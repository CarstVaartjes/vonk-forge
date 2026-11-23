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
from sqlalchemy import event, select
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_control.auth import Actor, TokenCodec
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.library.api import install_library_routes
from vonk_control.library.assessment import LibraryAssessment
from vonk_control.library_projection.service import LibraryProjection
from vonk_control.model_cache.artifacts import ArtifactSetManifest
from vonk_control.model_cache.service import ModelCacheService
from vonk_control.models.cache import ModelCacheSet, RecipeBuild
from vonk_control.models.catalog import CatalogDocumentHead, CatalogDocumentRevision
from vonk_control.models.execution import ResourceReservation
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.telemetry import NodeInventorySnapshot
from vonk_control.run_switch_operations.service import RunSwitchOperationService
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from vonk_forge_contracts import RecipeDefinition, document_sha256

from cluster_profiles.cli import main
from cluster_profiles.cli_render import render_payload
from cluster_profiles.control_client import ControlClient

from .run_switch_operations_support import RecordingArtifactExecutor
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture
from .runtime_image_fixtures import remove_test_image
from .test_library_canonical_projection import _insert_canonical_rows
from .test_recipe_operations import NOW, setup_services
from .test_run_switch_artifact_authority import _report_store


@pytest.fixture
def assessed_library(tmp_path: Path):
    model = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    model["source"]["repository"] = "https://huggingface.co/vonk-forge/synthetic-tiny"
    digest = document_sha256(model)

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
        free_fraction=0,
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
        response = client.get("/api/recipe/library", params={"fits_fleet": True})
        assert response.status_code == 200, response.text
        rows = response.json()["recipes"]
        assert len(rows) == 1
        check = rows[0]["assessment"]
        assert check["fleet_fit"]["state"] == "ready", check
        assert check["cache"]["state"] == "blocked"
        assert check["readiness"]["state"] == "blocked"
        render_payload(response.json(), "recipe", action="library", wide=True)
        rendered = capsys.readouterr().out
        assert "Fleet fit: ready" in rendered
        assert "Exact Controller assets: not cached" in rendered
        assert "Readiness: needs download" in rendered
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
                ("recipe", "library", "--fits-fleet", "--json"),
                control_client=control,
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["recipes"][0]["assessment"] == check
        response = client.get("/api/recipe/library", params={"ready": True})
        assert response.status_code == 200, response.text
        assert response.json()["recipes"] == []
    with sessions() as session:
        assert [
            (row.id, row.state) for row in session.scalars(select(RecipeBuild))
        ] == builds
        assert tuple(session.scalars(select(Job.id))) == jobs
    assert (
        projection.recipe_detail(rows[0]["selector"]).assessment
        == projection.recipe_library(assess=True).recipes[0].assessment
    )


def test_stale_capacity_is_unavailable_and_filter_cannot_report_empty_success(
    assessed_library,
):
    _, sessions, _, _, app, *_ = assessed_library
    with sessions.begin() as session:
        for row in session.scalars(select(NodeInventorySnapshot)):
            row.observed_at = NOW - timedelta(hours=1)
            row.received_at = NOW - timedelta(hours=1)
    with TestClient(app) as client:
        response = client.get("/api/recipe/library", params={"assess": True})
        assert response.status_code == 200, response.text
        assert (
            response.json()["recipes"][0]["assessment"]["fleet_fit"]["state"]
            == "unavailable"
        )
        response = client.get("/api/recipe/library", params={"fits_fleet": True})
        assert response.status_code == 200, response.text
        assert (
            response.json()["recipes"][0]["assessment"]["fleet_fit"]["state"]
            == "unavailable"
        )
        response = client.get(
            "/api/recipe/library",
            params={"assess": False, "fits_fleet": True},
        )
        assert response.status_code == 200
        assert response.json()["recipes"][0]["assessment"] is not None


def _publish_model_fixture(cache, sessions, manifest: ArtifactSetManifest):
    paths = []
    for artifact in manifest.artifacts:
        path = cache.root / "objects" / artifact.sha256[:2] / artifact.sha256
        path.parent.mkdir(parents=True, exist_ok=True)
        # Admission checks real presence and length.
        with path.open("wb") as output:
            output.truncate(artifact.expected_bytes)
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
    recipe = projection.recipe_library(assess=False).recipes[0]
    manifest = cache.resolve_artifact_set(
        recipe_revision_id=recipe.identity.recipe_revision_id
    )
    paths = _publish_model_fixture(cache, sessions, manifest)
    with sessions() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
    ready = projection.recipe_library(assess=True).recipes[0].assessment
    assert ready is not None
    assert ready.readiness.state == "ready", ready.readiness.model_dump(mode="json")
    assert ready.fleet_fit.state == "ready"
    assert ready.cache.state == "ready"
    assert len(projection.recipe_library(ready=True).recipes) == 1
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
    missing = projection.recipe_library(assess=True).recipes[0].assessment
    assert missing is not None
    assert missing.cache.state == "blocked"
    assert missing.fleet_fit.state == "ready"
    assert missing.readiness.state == "blocked"
    with paths[0].open("wb") as output:
        output.truncate(manifest.artifacts[0].expected_bytes)
    remove_test_image(storage, build.oci_layout_sha256)
    missing = projection.recipe_library(assess=True).recipes[0].assessment
    assert missing is not None
    assert missing.readiness.state == "blocked"


def _add_fitting_spark(sessions, node_id: str) -> None:
    """Enroll one more active Spark with fresh, ample capacity."""

    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    node_id,
                    observed_at=None,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
            )
        )
    InventoryRepository(sessions, clock=lambda: NOW).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": node_id,
                    "observed_at": NOW,
                    "disk_total_bytes": 10_000,
                    "disk_free_bytes": 8_000,
                    "host_memory_total_bytes": 10_000,
                    "host_memory_free_bytes": 8_000,
                    "gpu_memory_total_bytes": 10_000,
                    "gpu_memory_free_bytes": 8_000,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": (
                        "runtime.vonk.v1",
                        "recipe.image.pull.v1",
                        "recipe.operations.v1",
                    ),
                    "memory_pool": "shared",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
        )
    )


def test_fit_search_continues_after_an_ineligible_first_group(assessed_library):
    projection, sessions, *_ = assessed_library
    later = "spk_" + "f" * 32
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_free_bytes = 0
        snapshot.gpu_memory_free_bytes = 0
    _add_fitting_spark(sessions, later)
    rows = projection.recipe_library(fits_fleet=True).recipes
    assert len(rows) == 1
    assessment = rows[0].assessment
    assert assessment is not None and assessment.group is not None
    assert [node.node_id for node in assessment.group.nodes] == [later]


def test_a_later_spark_holding_what_the_nas_evicted_is_ready(
    assessed_library, monkeypatch
):
    """A load whose Sparks hold the model and image downloads no model, so the
    Library calls it ready although the NAS lacks the model, and keeps
    ordering the Spark that holds the set ahead of an earlier-sorted one."""

    projection, sessions, cache, service, _, _, _, _, nodes, _ = assessed_library
    _add_fitting_spark(sessions, "spk_" + "0" * 32)
    recipe = projection.recipe_library(assess=False).recipes[0]
    manifest = cache.resolve_artifact_set(
        recipe_revision_id=recipe.identity.recipe_revision_id
    )
    _report_store(
        sessions,
        nodes[0],
        [
            {"sha256": spec.sha256, "bytes": spec.expected_bytes}
            for spec in manifest.artifacts
        ],
    )
    inspect = service.inspect_candidate

    def image_held_on_holder(revision_id, group, **kwargs):
        plan = inspect(revision_id, group, **kwargs)
        if tuple(group) != (nodes[0],):
            return plan
        # The holder's installed workload names the image there.
        held = plan.runtime_storage.model_copy(
            update={"missing_image_distribution_bytes": 0}
        )
        return plan.model_copy(update={"runtime_storage": held})

    monkeypatch.setattr(service, "inspect_candidate", image_held_on_holder)

    assessment = projection.recipe_library(assess=True).recipes[0].assessment

    assert assessment is not None and assessment.group is not None
    assert assessment.cache.state == "blocked"
    assert assessment.fleet_fit.state == "ready"
    assert assessment.readiness.state == "ready", assessment.readiness
    assert [node.node_id for node in assessment.group.nodes] == [nodes[0]]
    assert len(projection.recipe_library(ready=True).recipes) == 1


def _hold_model(sessions, cache, projection, node_id: str) -> None:
    recipe = projection.recipe_library(assess=False).recipes[0]
    manifest = cache.resolve_artifact_set(
        recipe_revision_id=recipe.identity.recipe_revision_id
    )
    _report_store(
        sessions,
        node_id,
        [
            {"sha256": spec.sha256, "bytes": spec.expected_bytes}
            for spec in manifest.artifacts
        ],
    )


def test_a_library_pass_reads_failed_copies_once_for_every_candidate(
    assessed_library, monkeypatch
):
    """Catches one jobs scan per candidate placement."""
    from vonk_agent_protocol import LibraryAssessmentCode
    from vonk_control.library import assessment as library_assessment
    from vonk_control.library.contract import RecipeReadinessCheck

    projection, sessions, _, service, _, _, _, _, _, _ = assessed_library
    _add_fitting_spark(sessions, "spk_" + "0" * 32)
    _add_fitting_spark(sessions, "spk_" + "f" * 32)
    # No group fits, so the search inspects every candidate placement.
    blocked = library_assessment._reason(
        LibraryAssessmentCode.INSUFFICIENT_NODES, "forced"
    )
    monkeypatch.setattr(
        library_assessment.LibraryAssessment,
        "_fit",
        staticmethod(
            lambda plan: RecipeReadinessCheck(state="blocked", reasons=[blocked])
        ),
    )
    inspected: list[tuple[str, ...]] = []
    inspect = service.inspect_candidate

    def counting_inspect(revision_id, group, **kwargs):
        inspected.append(tuple(group))
        return inspect(revision_id, group, **kwargs)

    monkeypatch.setattr(service, "inspect_candidate", counting_inspect)
    statements: list[str] = []

    def record(_connection, _cursor, statement, *_args):
        if "jobs.result" in statement and "FROM jobs" in statement:
            statements.append(statement)

    engine = sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        projection.recipe_library(assess=True)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(inspected) >= 2
    assert len(statements) == 1


def test_a_failing_group_does_not_turn_a_holder_search_blocked(
    assessed_library, monkeypatch
):
    projection, sessions, cache, service, _, _, _, _, nodes, _ = assessed_library
    failing, fitting = "spk_" + "0" * 32, "spk_" + "f" * 32
    _add_fitting_spark(sessions, failing)
    _add_fitting_spark(sessions, fitting)
    _hold_model(sessions, cache, projection, nodes[0])
    inspect = service.inspect_candidate

    def fail_one(revision_id, group, **kwargs):
        if tuple(group) == (failing,):
            raise RuntimeError("read failed")
        return inspect(revision_id, group, **kwargs)

    monkeypatch.setattr(service, "inspect_candidate", fail_one)

    assessment = projection.recipe_library(assess=True).recipes[0].assessment

    assert assessment is not None
    assert assessment.readiness.state in {"unavailable", "ready"}


def test_late_assessment_is_discarded_and_selector_scan_does_not_assess(
    assessed_library, monkeypatch
):
    import vonk_control.library.assessment as assessment_module

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
    projection.recipe_library()
    projection.models()
    with TestClient(app) as client:
        response = client.get("/api/recipe/library")
        assert response.status_code == 200
        assert response.json()["recipes"][0].get("assessment") is None
    assert calls == []
    with TestClient(app) as client:
        response = client.get("/api/recipe/library", params={"fits_fleet": True})
        assert response.status_code == 200, response.text
        assert (
            response.json()["recipes"][0]["assessment"]["fleet_fit"]["state"]
            == "unavailable"
        )
    assert len(calls) == 1
    assessment = projection.recipe_library(assess=True).recipes[0].assessment
    assert assessment is not None
    assert assessment.fleet_fit.state == "unavailable"
    # A search cut off by the deadline is unknown, never "needs download".
    assert assessment.readiness.state == "unavailable"


def test_fleet_filter_assesses_later_candidates_before_pagination(assessed_library):
    projection, sessions, *_ = assessed_library
    recipe = projection.recipe_library(assess=False).recipes[0]
    _insert_canonical_rows(
        sessions,
        kind="recipe",
        template=projection.recipe_detail(recipe.selector).document.model_dump(
            mode="json"
        ),
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
        memory.update(peak_bytes=20_000)
        # An idle Spark is never refused for its declared memory estimate, so a
        # Vonk claim must hold memory for the oversized recipe to be refused.
        node_id = session.scalar(select(NodeInventorySnapshot.node_id))
        assert node_id is not None
        session.add(
            ResourceReservation(
                node_id=node_id,
                kind="unified-memory",
                resource_key="other-capacity",
                amount_bytes=1_000,
                owner_kind="recipe-build",
                owner_id=str(uuid.uuid4()),
                state="active",
                plan_digest="f" * 64,
                created_at=NOW,
            )
        )
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
            content_digest=document_sha256(canonical.model_dump(mode="json")),
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
    projection.refresh_catalog()
    page = projection.recipe_library(
        publisher=["test"], limit=1, sort="name", fits_fleet=True
    )
    assert [item.selector for item in page.recipes] == ["test/recipe-0001"]
    assert page.next_cursor is None
