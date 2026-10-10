from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentFailureResult, LifecycleState, canonical_message
from vonk_control import availability_production
from vonk_control.auth import Actor, TokenCodec
from vonk_control.availability_production import (
    RecipeImageAvailabilityScheduler,
    build_recipe_image_availability,
)
from vonk_control.bounded_json import require_mapping
from vonk_control.catalog_service import CatalogService
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
)
from vonk_control.recipe_builds import (
    RecipeBuildPlan,
    RecipeBuildResolution,
    RecipeBuildService,
    RecipeSourcePolicyError,
)
from vonk_control.recipe_image_availability import (
    BuildUnsettled,
    RecipeImageAvailabilityClaim,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_image_availability_api import install_recipe_operator_routes
from vonk_control.recipe_image_availability_contract import AvailabilityBuildReceipt
from vonk_control.revision_images import revision_images
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
)
from vonk_control.source_policy import SourcePolicyFinding, SourcePolicyReport
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

from .runtime_image_fixtures import place_test_image
from .test_recipe_builds import _write_controller_build_receipt
from .test_recipe_builds import setup as _build_setup
from .test_recipe_image_availability import _typed_availability_payload


class _Service(RecipeImageAvailabilityService):
    """Scheduler double that never touches durable storage."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.claimed = 0

    def claim_pending(
        self, *, limit: int = 4, owner_id: str | None = None
    ) -> tuple[RecipeImageAvailabilityClaim, ...]:
        del owner_id
        if self.claimed:
            return ()
        self.claimed += 1
        return (
            RecipeImageAvailabilityClaim(
                operation_id="operation",
                recipe_revision_id="revision",
                build_input_sha256=None,
                claim_owner="owner",
                execution_attempt=1,
            ),
        )[:limit]

    def run_claim(self, claim: RecipeImageAvailabilityClaim) -> None:
        del claim
        self.started.set()
        self.release.wait(5)

    def claim_update(self, owner: str):
        return None

    def reconcile_cancellations(self, *, limit: int = 8) -> int:
        return 0

    def advance_removals(self, *, limit: int = 1) -> int:
        return 0


def test_scheduler_submits_durable_claim_without_waiting_for_image_io() -> None:
    service = _Service()
    scheduler = RecipeImageAvailabilityScheduler(service, max_workers=1)
    started = time.monotonic()
    assert scheduler.tick() == 1
    elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert service.started.wait(1)
    service.release.set()
    scheduler.close()
    assert scheduler.executor._shutdown is True


def test_scheduler_close_is_idempotent_and_stops_new_claims() -> None:
    service = _Service()
    scheduler = RecipeImageAvailabilityScheduler(service, max_workers=1)
    scheduler.close()
    scheduler.close()
    assert scheduler.tick() == 0


def test_production_factory_separates_api_service_and_worker_scheduler(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'availability.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    class Settings:
        agent_artifact_root = tmp_path / "api-artifacts"

    kwargs = {
        "sessions": sessions,
        "settings": Settings(),
        "managed_catalog_sync": None,
        "recipe_builds": object(),
        "recipe_operations": object(),
        "clock": lambda: datetime.now(UTC),
    }
    api = build_recipe_image_availability(**kwargs)
    assert api.scheduler is None
    assert api.storage.root == tmp_path / "api-artifacts" / "image-cache"
    api.close()

    worker = build_recipe_image_availability(
        **kwargs,
        artifact_root=tmp_path / "worker-artifacts",
        with_scheduler=True,
    )
    assert worker.scheduler is not None
    scheduler = worker.scheduler
    worker.close()
    assert scheduler.executor._shutdown is True


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("damage", [None, "source", "runtime", "runtime-exhaustion"])
def test_recipe_download_api_reuses_verified_cached_source_build(
    tmp_path, monkeypatch, damage
) -> None:
    sessions, bundles, now, node_id, revision = _build_setup(
        tmp_path, recipe_slug="cached-source-image"
    )
    artifact_root = tmp_path / "artifacts"
    storage = FilesystemRuntimeImageStorage(artifact_root)
    builds = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
        prepared_builds=storage.find_build,
    )
    original_document = revision.document
    with sessions() as session:
        dependencies = [
            row.document
            for row in session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "model"
                )
            )
        ]
        current = session.get(CatalogDocumentRevision, revision.id)
        assert current is not None
        source_digest = current.projected["source_bundle_sha256"]
        assert isinstance(source_digest, str)
    catalog = CatalogService(
        sessions, clock=lambda: now, cursors=TokenCodec(b"c" * 32).cursor_codec()
    )

    class CatalogReobserver:
        calls = 0

        def automatic(self):
            self.calls += 1
            catalog.import_recipe_library(
                "test",
                library_commit="a" * 40,
                source_path="recipe.json",
                document=original_document,
                expected_content_sha256=revision.content_digest,
                dependency_documents=dependencies,
                source_bundle_sha256=source_digest,
            )

    reobserver = CatalogReobserver()
    if damage == "source":
        # Current ingestion policy supplies the build fixture's inputs too.
        reobserver.automatic()
        catalog.refresh_build_policy()
        from vonk_control.catalog_revision_contract import (
            read_catalog_projection,
            write_catalog_projection,
        )

        with sessions.begin() as session:
            current = session.get(CatalogDocumentRevision, revision.id)
            assert current is not None
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == revision.id)
                .values(
                    projected=write_catalog_projection(
                        read_catalog_projection(current).model_copy(
                            update={"build_model_artifacts": None}
                        )
                    )
                )
            )
        reobserver.calls = 0
        from vonk_control.inventory_repository import (
            InventoryRepository,
            InventorySnapshotInput,
        )

        now += timedelta(seconds=1)
        inventory = InventoryRepository(sessions, clock=lambda: now)
        inventory.record(
            InventorySnapshotInput(
                node_id,
                now,
                2 * 1024**4,
                1024**4,
                1024**4,
                1024**4,
                1024**4,
                1024**4,
                1,
                False,
                (
                    "recipe.build.v1",
                    "recipe.build.egress-proxy.v1",
                    "recipe.image.import.v1",
                    "runtime.vonk.v1",
                    "recipe.image.pull.v1",
                ),
                memory_pool="separate",
            )
        )
    plan = builds.plan(revision.id, node_id, now=now)
    archive = b"verified cached source-build OCI archive"
    archive_digest = hashlib.sha256(archive).hexdigest()
    image_digest = "sha256:" + "b" * 64
    builds.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest=image_digest,
        oci_layout_sha256=archive_digest,
        image_bytes=len(archive),
        now=now,
    )
    stored_receipt = _write_controller_build_receipt(
        storage,
        archive=archive,
        image_digest=image_digest,
        build_id=plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        distribution_content_sha256=revision.content_digest,
    )
    resolved = builds.resolve(revision.id)
    assert resolved.cached
    assert resolved.oci_layout_sha256 == stored_receipt.oci_archive_sha256
    assert storage.existing_archive(archive_digest, len(archive)).is_file()

    class NoNetworkTransport:
        def inspect_archive(self, *_args, **_kwargs):
            pytest.fail("a complete verified build receipt must be reused")

    monkeypatch.setattr(
        availability_production, "OciLayoutImageTransport", NoNetworkTransport
    )

    class Operations:
        build_calls = 0

        def build(self, *_args, **_kwargs):
            self.build_calls += 1
            raise AssertionError("a verified cached build must not be dispatched")

    operations = Operations()
    if damage == "source":
        with sessions.begin() as session:
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == revision.id)
                .values(document={"damaged": True})
            )
    if damage in {"runtime", "runtime-exhaustion"}:
        compile_owner = availability_production._compile_consistent_runtime
        compile_calls = []

        def compile_again(*args, **kwargs):
            compile_calls.append(True)
            if len(compile_calls) <= (3 if damage == "runtime-exhaustion" else 1):
                raise OSError("local runtime evidence unavailable")
            return compile_owner(*args, **kwargs)

        monkeypatch.setattr(
            availability_production, "_compile_consistent_runtime", compile_again
        )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=artifact_root,
        managed_catalog_sync=reobserver,
        recipe_builds=builds,
        recipe_operations=operations,
        clock=lambda: now,
    )
    app = FastAPI()
    install_recipe_operator_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        service=production.service,
    )
    request_key = str(uuid.uuid4())
    try:
        with TestClient(app) as client:
            accepted = client.post(
                f"/api/recipe/{revision.publisher}/{revision.slug}/download",
                json={"request_key": request_key},
            )
            # Submission re-observes local projection loss inside its bounded
            # admission budget; recovery may finish in this same request.
            assert accepted.status_code == 202, accepted.text
            assert accepted.json()["recipe_revision_id"] == revision.id
            assert production.service.run_pending() == 1

        completed = production.service.get(accepted.json()["id"])
        assert completed.state == "succeeded", completed.failure
        assert completed.result is not None
        assert completed.result["build_id"] == plan.build_id
        assert completed.result["build_input_sha256"] == plan.build_input_sha256
        assert completed.result["image_digest"] == image_digest
        assert completed.result["oci_archive_sha256"] == archive_digest
        assert operations.build_calls == 0
        assert reobserver.calls == (
            0 if damage is None else 2 if damage == "runtime-exhaustion" else 1
        )
        fresh = production.service.start(
            revision.id, actor="operator", request_id=str(uuid.uuid4())
        )
        production.service.run_pending()
        assert production.service.get(fresh.id).state == "succeeded"
        assert operations.build_calls == 0
        with sessions() as session:
            # The recipe's images are derived from its builds: nothing records
            # that the revision may use this archive.
            images = revision_images(session, [revision.id])[revision.id]
            assert [image.archive_sha256 for image in images] == [archive_digest]
    finally:
        production.close()


def test_source_build_without_builder_queues_provisional_parent(
    tmp_path, monkeypatch
) -> None:
    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-source-build.json")
            .read_text()
        )
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'saturated.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            CatalogDocumentRevision(
                id="saturated-revision",
                document_id="saturated-document",
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=recipe.model_dump(mode="json"),
                content_digest=document_sha256(recipe.model_dump(mode="json")),
                artifact_key="c" * 64,
                execution_key="a" * 64,
                projected={},
                created_by="test",
                created_at=now,
            )
        )

    class Builds:
        def resolve(self, _revision_id: str):
            return SimpleNamespace(
                cached=False,
                input_intent_sha256="a" * 64,
                build_input_sha256=None,
                build_id=None,
            )

        def plan(self, *_args, **_kwargs):
            raise AssertionError("builder planning must remain dispatch-time")

    monkeypatch.setattr(
        availability_production,
        "resolve_recipe_entities",
        lambda _session, _document: {},
    )
    monkeypatch.setattr(
        availability_production,
        "_compile_consistent_runtime",
        lambda *_args, **_kwargs: {
            "input_intent_sha256": "a" * 64,
            "interface": "vonk.runtime.v1",
            "architecture": "linux/arm64",
            "image": "sha256:" + "d" * 64,
        },
    )

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=Builds(),
        recipe_operations=object(),
        clock=lambda: now,
    )
    queued = production.service.start(
        "saturated-revision",
        actor="operator",
        request_id="s" * 36,
    )
    assert queued.state == "queued"
    assert queued.build_input_sha256 is None
    with sessions() as session:
        row = session.get(CatalogDocumentRevision, "saturated-revision")
        assert row is not None
    production.close()


def test_stored_policy_projection_observes_then_fresh_authority_is_admitted(
    tmp_path, monkeypatch
) -> None:
    """A recipe whose stored source the policy refuses must not read as a transient
    outage: a retry reads the same source, and the operator needs the file and line."""

    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-source-build.json")
            .read_text()
        )
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'policy.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            CatalogDocumentRevision(
                id="policy-revision",
                document_id="policy-document",
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=recipe.model_dump(mode="json"),
                content_digest=document_sha256(recipe.model_dump(mode="json")),
                artifact_key="c" * 64,
                execution_key="a" * 64,
                projected={},
                created_by="test",
                created_at=now,
            )
        )
    report = SourcePolicyReport(
        False,
        "b" * 64,
        "Dockerfile",
        (
            SourcePolicyFinding(
                "dockerfile.heredoc_forbidden",
                "Dockerfile",
                106,
                "Dockerfile heredocs are not accepted",
            ),
        ),
    )

    class Builds:
        damaged = True
        calls = 0

        def resolve(self, _revision_id: str):
            self.calls += 1
            if self.damaged:
                raise RecipeSourcePolicyError(report)
            receipt = _write_controller_build_receipt(
                storage,
                archive=b"recovered policy build archive",
                image_digest="sha256:" + "d" * 64,
                build_id="00000000-0000-4000-8000-000000000743",
                build_input_sha256="b" * 64,
                distribution_content_sha256=document_sha256(
                    recipe.model_dump(mode="json")
                ),
            )
            return SimpleNamespace(
                cached=True,
                recipe_revision_id=_revision_id,
                input_intent_sha256="a" * 64,
                build_id=receipt.build_id,
                builder_node_id="recovered-builder",
                build_input_sha256=receipt.build_input_sha256,
                image_digest=receipt.image_digest,
                oci_layout_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
            )

    storage = FilesystemRuntimeImageStorage(tmp_path / "artifacts")
    builds = Builds()

    monkeypatch.setattr(
        availability_production,
        "resolve_recipe_entities",
        lambda _session, _document: {},
    )

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=object(),
        clock=lambda: now,
    )
    ended = production.service.start(
        "policy-revision", actor="operator", request_id="p" * 36
    )
    assert ended.state == LifecycleState.FAILED.value and ended.artifact is None
    assert builds.calls > 1
    with sessions() as session:
        assert session.get(Job, ended.id) is None
    builds.damaged = False
    monkeypatch.setattr(
        availability_production,
        "_compile_consistent_runtime",
        lambda *args, **kwargs: {
            "architecture": "linux/arm64",
            "interface": "vonk.runtime.v1",
            "build_input_sha256": "b" * 64,
        },
    )
    fresh = production.service.start(
        "policy-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert fresh.id != ended.id
    assert fresh.state == LifecycleState.QUEUED.value
    assert production.service.run_pending() == 1
    assert production.service.get(fresh.id).state == LifecycleState.SUCCEEDED.value, (
        production.service.get(fresh.id)
    )
    production.close()


def test_authority_resolves_builds_without_an_open_transaction(
    tmp_path, monkeypatch
) -> None:
    """Build resolution touches managed storage, so the read transaction must
    already be closed when it runs."""

    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-source-build.json")
            .read_text()
        )
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'scope.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            CatalogDocumentRevision(
                id="scope-revision",
                document_id="scope-document",
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=recipe.model_dump(mode="json"),
                content_digest=document_sha256(recipe.model_dump(mode="json")),
                artifact_key="c" * 64,
                execution_key="a" * 64,
                projected={},
                created_by="test",
                created_at=now,
            )
        )
    checked_out: list[int] = []

    class Builds:
        def resolve(self, _revision_id: str):
            checked_out.append(cast(Any, engine.pool).checkedout())
            return SimpleNamespace(
                cached=False,
                input_intent_sha256="a" * 64,
                build_input_sha256=None,
                build_id=None,
            )

        def plan(self, *_args, **_kwargs):
            raise AssertionError("builder planning must remain dispatch-time")

    monkeypatch.setattr(
        availability_production,
        "resolve_recipe_entities",
        lambda _session, _document: {},
    )
    monkeypatch.setattr(
        availability_production,
        "_compile_consistent_runtime",
        lambda *_args, **_kwargs: {
            "input_intent_sha256": "a" * 64,
            "interface": "vonk.runtime.v1",
            "architecture": "linux/arm64",
            "image": "sha256:" + "d" * 64,
        },
    )

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=Builds(),
        recipe_operations=object(),
        clock=lambda: now,
    )
    production.service.start(
        "scope-revision",
        actor="operator",
        request_id="t" * 36,
    )

    assert checked_out == [0]
    production.close()


@pytest.mark.parametrize("capacity_busy", [False, True])
@pytest.mark.usefixtures("damaged_json_rows")
def test_builder_reuses_selected_plan_without_a_second_capacity_admission(
    tmp_path,
    capacity_busy,
) -> None:
    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-source-build.json")
            .read_text()
        )
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'builder.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id="builder-node-000000000000000000000000000000",
                state="active",
                architecture="linux-arm64",
            )
        )
        session.add(
            Job(
                id="00000000-0000-4000-8000-000000000701",
                request_id="00000000-0000-4000-8000-000000000702",
                kind="recipe.image.availability.v2",
                state="running",
                actor="operator",
                authority_revision="revision-builder",
                targets=["revision-builder"],
                payload_digest="a" * 64,
                payload=_typed_availability_payload(
                    {
                        "recipe_revision_id": "revision-builder",
                        "runtime": {
                            "recipe_revision_id": "revision-builder",
                            "input_intent_sha256": "a" * 64,
                        },
                        "build_input_sha256": None,
                        "claim_until": (now - timedelta(seconds=1)).isoformat(),
                    },
                    recipe,
                ),
                result=None,
                current_attempt=1,
                created_at=now,
                updated_at=now,
            )
        )

    class Builds:
        def __init__(self) -> None:
            self.plan_calls = 0

        def resolve(self, _revision_id: str):
            return RecipeBuildResolution(
                recipe_revision_id=_revision_id,
                recipe_content_sha256=document_sha256(recipe.model_dump(mode="json")),
                source_bundle_sha256="c" * 64,
                input_intent_sha256="a" * 64,
                input_intent={},
            )

        def prepare_plan(self, *_args, **_kwargs):
            self.plan_calls += 1
            if self.plan_calls > 1:
                raise AssertionError("selected plan must not be admitted twice")
            return SimpleNamespace(
                build_input_sha256="b" * 64,
                builder_node_id="builder-node-000000000000000000000000000000",
                build_id="00000000-0000-4000-8000-000000000703",
                policy_report=None,
            )

        def persist_plan_in_session(self, _session, plan, **_kwargs):
            return plan

    class Operations:
        def build(self, plan, **_kwargs):
            if capacity_busy:
                from vonk_control.recipe_builds import RecipeBuildAdmissionBusy

                raise RecipeBuildAdmissionBusy()
            return SimpleNamespace(
                id=str(uuid.uuid4()),
                state="succeeded",
                owner_id="build-id",
                result={
                    "successful_nodes": [plan.builder_node_id],
                    "failed_nodes": [],
                    "node_evidence": {
                        plan.builder_node_id: {
                            "image_bytes": 1,
                            "image_digest": "sha256:" + "d" * 64,
                            "oci_layout_sha256": "e" * 64,
                        }
                    },
                },
            )

    builds = Builds()

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=Operations(),
        clock=lambda: now,
    )
    assert production.service._builder is not None

    claim = production.service.claim_pending(limit=1)[0]

    def execute():
        assert production.service._builder is not None
        return production.service._builder(
            recipe,
            {
                "recipe_revision_id": "revision-builder",
                "input_intent_sha256": "a" * 64,
            },
            claim=claim,
            build_input_sha256="",
            force=False,
            progress=lambda _progress: None,
        )

    if capacity_busy:
        failure = execute()
        assert isinstance(failure, BuildUnsettled)
        assert failure.retryable
    else:
        result = execute()
        assert not isinstance(result, BuildUnsettled)
        assert (
            AvailabilityBuildReceipt.model_validate_json(
                canonical_message(result)
            ).build_input_sha256
            == "b" * 64
        )
    assert builds.plan_calls == 1
    production.close()


@pytest.mark.usefixtures("damaged_json_rows")
def test_busy_spark_makes_build_wait_until_it_is_idle(tmp_path) -> None:
    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-source-build.json")
            .read_text()
        )
    )
    node_id = "builder-node-000000000000000000000000000000"
    engine = create_engine(f"sqlite:///{tmp_path / 'busy.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    busy_id = "00000000-0000-4000-8000-000000000801"
    waiting_id = "00000000-0000-4000-8000-000000000802"

    def availability_job(job_id: str, request_id: str, payload: dict) -> Job:
        return Job(
            id=job_id,
            request_id=request_id,
            kind="recipe.image.availability.v2",
            state="running",
            actor="operator",
            authority_revision="revision-builder",
            targets=["revision-builder"],
            payload_digest="a" * 64,
            payload=_typed_availability_payload(
                {
                    "recipe_revision_id": "revision-builder",
                    "build_input_sha256": None,
                    **payload,
                },
                recipe,
            ),
            result=None,
            current_attempt=1,
            created_at=now,
            updated_at=now,
        )

    with sessions.begin() as session:
        session.add(
            AgentNode(node_id=node_id, state="active", architecture="linux-arm64")
        )
        session.add(
            availability_job(
                busy_id,
                "00000000-0000-4000-8000-000000000803",
                {
                    "runtime": {"builder_node_id": node_id},
                    "claim_until": (now + timedelta(hours=1)).isoformat(),
                },
            )
        )
        session.add(
            availability_job(
                waiting_id,
                "00000000-0000-4000-8000-000000000804",
                {
                    "runtime": {
                        "recipe_revision_id": "revision-builder",
                        "input_intent_sha256": "a" * 64,
                    },
                    "claim_until": (now - timedelta(seconds=1)).isoformat(),
                },
            )
        )

    class Builds:
        def resolve(self, revision_id: str):
            return RecipeBuildResolution(
                recipe_revision_id=revision_id,
                recipe_content_sha256=document_sha256(recipe.model_dump(mode="json")),
                source_bundle_sha256="c" * 64,
                input_intent_sha256="a" * 64,
                input_intent={},
            )

        def prepare_plan(self, _revision_id: str, candidate: str, **_kwargs):
            return SimpleNamespace(
                build_input_sha256="b" * 64,
                builder_node_id=candidate,
                build_id="00000000-0000-4000-8000-000000000805",
                policy_report=None,
            )

        def persist_plan_in_session(self, _session, plan, **_kwargs):
            return plan

    class Operations:
        def build(self, plan, **_kwargs):
            return SimpleNamespace(
                id=str(uuid.uuid4()),
                state="succeeded",
                owner_id="build-id",
                result={
                    "successful_nodes": [plan.builder_node_id],
                    "failed_nodes": [],
                    "node_evidence": {
                        plan.builder_node_id: {
                            "image_bytes": 1,
                            "image_digest": "sha256:" + "d" * 64,
                            "oci_layout_sha256": "e" * 64,
                        }
                    },
                },
            )

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=Builds(),
        recipe_operations=Operations(),
        clock=lambda: now,
    )
    assert production.service._builder is not None
    (claim,) = production.service.claim_pending(limit=1)
    assert claim.operation_id == waiting_id

    def execute():
        assert production.service._builder is not None
        return production.service._builder(
            recipe,
            {
                "recipe_revision_id": "revision-builder",
                "input_intent_sha256": "a" * 64,
            },
            claim=claim,
            build_input_sha256="",
            force=False,
            progress=lambda _progress: None,
        )

    failure = execute()
    assert isinstance(failure, BuildUnsettled)
    assert failure.retryable

    with sessions.begin() as session:
        busy = session.get(Job, busy_id)
        assert busy is not None
        busy.payload = dict(busy.payload) | {"image_result": {"done": True}}
    settled = execute()
    assert not isinstance(settled, BuildUnsettled)
    assert (
        AvailabilityBuildReceipt.model_validate_json(
            canonical_message(settled)
        ).builder_node_id
        == node_id
    )
    production.close()


def _prebuilt_policy(*, used: bool) -> dict[str, object]:
    return {
        "passed": True,
        "source_bundle_sha256": "c" * 64,
        "dockerfile": "Dockerfile",
        "findings": [],
        "builder_binary_digest": "d" * 64,
        "artifact_format": "oci-layout",
        **(
            {
                "prebuilt_image": "ghcr.io/example/image@sha256:" + "d" * 64,
                "prebuilt_decision": {
                    "code": "prebuilt.used",
                    "detail": "pulling the catalog's prebuilt image",
                },
            }
            if used
            else {
                "prebuilt_decision": {
                    "code": "prebuilt.not_pinned",
                    "detail": "the signed catalog pins no prebuilt image",
                }
            }
        ),
    }


@pytest.mark.parametrize("prebuilt", [True, False])
@pytest.mark.usefixtures("damaged_json_rows")
def test_a_prebuilt_pull_does_not_wait_for_a_free_builder_and_holds_none(
    tmp_path, prebuilt: bool
) -> None:
    """The Controller pulls a prebuilt image; no Spark builds, so none is held."""

    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-source-build.json")
            .read_text()
        )
    )
    node_id = "builder-node-000000000000000000000000000000"
    engine = create_engine(f"sqlite:///{tmp_path / 'prebuilt.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    occupant_id = "00000000-0000-4000-8000-000000000811"
    waiting_id = "00000000-0000-4000-8000-000000000812"

    def availability_job(job_id: str, request_id: str, payload: dict) -> Job:
        return Job(
            id=job_id,
            request_id=request_id,
            kind="recipe.image.availability.v2",
            state="running",
            actor="operator",
            authority_revision="revision-builder",
            targets=["revision-builder"],
            payload_digest="a" * 64,
            payload=_typed_availability_payload(
                {
                    "recipe_revision_id": "revision-builder",
                    "build_input_sha256": None,
                    **payload,
                },
                recipe,
            ),
            result=None,
            current_attempt=1,
            created_at=now,
            updated_at=now,
        )

    with sessions.begin() as session:
        session.add(
            AgentNode(node_id=node_id, state="active", architecture="linux-arm64")
        )
        # Another preparation holds the only builder (it is waiting for its
        # model, no image yet).
        session.add(
            availability_job(
                occupant_id,
                "00000000-0000-4000-8000-000000000813",
                {
                    "runtime": {"builder_node_id": node_id},
                    "claim_until": (now + timedelta(hours=1)).isoformat(),
                },
            )
        )
        session.add(
            availability_job(
                waiting_id,
                "00000000-0000-4000-8000-000000000814",
                {
                    "runtime": {
                        "recipe_revision_id": "revision-builder",
                        "input_intent_sha256": "a" * 64,
                    },
                    "claim_until": (now - timedelta(seconds=1)).isoformat(),
                },
            )
        )

    class Builds:
        def resolve(self, revision_id: str):
            return RecipeBuildResolution(
                recipe_revision_id=revision_id,
                recipe_content_sha256=document_sha256(recipe.model_dump(mode="json")),
                source_bundle_sha256="c" * 64,
                input_intent_sha256="a" * 64,
                input_intent={},
            )

        def prepare_plan(self, _revision_id: str, candidate: str, **_kwargs):
            return SimpleNamespace(
                build_input_sha256="b" * 64,
                builder_node_id=candidate,
                build_id="00000000-0000-4000-8000-000000000815",
                policy_report=_prebuilt_policy(used=prebuilt),
            )

        def persist_plan_in_session(self, _session, plan, **_kwargs):
            return plan

    class Operations:
        def build(self, plan, **_kwargs):
            return SimpleNamespace(
                id=str(uuid.uuid4()),
                state="succeeded",
                owner_id="build-id",
                result={
                    "successful_nodes": [plan.builder_node_id],
                    "failed_nodes": [],
                    "node_evidence": {
                        plan.builder_node_id: {
                            "image_bytes": 1,
                            "image_digest": "sha256:" + "d" * 64,
                            "oci_layout_sha256": "e" * 64,
                        }
                    },
                },
            )

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=Builds(),
        recipe_operations=Operations(),
        clock=lambda: now,
    )
    assert production.service._builder is not None
    (claim,) = production.service.claim_pending(limit=1)
    assert claim.operation_id == waiting_id

    def execute():
        assert production.service._builder is not None
        return production.service._builder(
            recipe,
            {
                "recipe_revision_id": "revision-builder",
                "input_intent_sha256": "a" * 64,
            },
            claim=claim,
            build_input_sha256="",
            force=False,
            progress=lambda _progress: None,
        )

    if prebuilt:
        settled = execute()
        assert not isinstance(settled, BuildUnsettled)
        assert (
            AvailabilityBuildReceipt.model_validate_json(
                canonical_message(settled)
            ).builder_node_id
            == node_id
        )
        with sessions() as session:
            waiting = session.get(Job, waiting_id)
            assert waiting is not None
            # It now holds no builder: a Spark build may take the Spark.
            assert waiting.payload["prebuilt_pull"] is True
    else:
        failure = execute()
        assert isinstance(failure, BuildUnsettled)
    production.close()


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_prebuilt_pull_does_not_occupy_the_builder_a_spark_build_needs(
    tmp_path,
) -> None:
    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-source-build.json")
            .read_text()
        )
    )
    node_id = "builder-node-000000000000000000000000000000"
    engine = create_engine(f"sqlite:///{tmp_path / 'prebuilt-holder.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    waiting_id = "00000000-0000-4000-8000-000000000822"

    def availability_job(job_id: str, request_id: str, payload: dict) -> Job:
        return Job(
            id=job_id,
            request_id=request_id,
            kind="recipe.image.availability.v2",
            state="running",
            actor="operator",
            authority_revision="revision-builder",
            targets=["revision-builder"],
            payload_digest="a" * 64,
            payload=_typed_availability_payload(
                {
                    "recipe_revision_id": "revision-builder",
                    "build_input_sha256": None,
                    **payload,
                },
                recipe,
            ),
            result=None,
            current_attempt=1,
            created_at=now,
            updated_at=now,
        )

    with sessions.begin() as session:
        session.add(
            AgentNode(node_id=node_id, state="active", architecture="linux-arm64")
        )
        # A preparation pulling a prebuilt image on this nominal builder, and a
        # prebuilt build job the Controller executes: neither holds the Spark.
        session.add(
            availability_job(
                "00000000-0000-4000-8000-000000000821",
                "00000000-0000-4000-8000-000000000823",
                {
                    "runtime": {"builder_node_id": node_id},
                    "prebuilt_pull": True,
                    "claim_until": (now + timedelta(hours=1)).isoformat(),
                },
            )
        )
        session.add(
            Job(
                id="00000000-0000-4000-8000-000000000824",
                request_id="00000000-0000-4000-8000-000000000825",
                kind="recipe.build.v1",
                state="running",
                actor="operator",
                authority_revision="b" * 64,
                targets=[node_id],
                payload_digest="a" * 64,
                payload={"prebuilt_image": "ghcr.io/example/image@sha256:" + "d" * 64},
                result=None,
                current_attempt=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            availability_job(
                waiting_id,
                "00000000-0000-4000-8000-000000000826",
                {
                    "runtime": {
                        "recipe_revision_id": "revision-builder",
                        "input_intent_sha256": "a" * 64,
                    },
                    "claim_until": (now - timedelta(seconds=1)).isoformat(),
                },
            )
        )

    class Builds:
        def resolve(self, revision_id: str):
            return RecipeBuildResolution(
                recipe_revision_id=revision_id,
                recipe_content_sha256=document_sha256(recipe.model_dump(mode="json")),
                source_bundle_sha256="c" * 64,
                input_intent_sha256="a" * 64,
                input_intent={},
            )

        def prepare_plan(self, _revision_id: str, candidate: str, **_kwargs):
            # A Spark build: it needs the builder free of other Spark work.
            return SimpleNamespace(
                build_input_sha256="b" * 64,
                builder_node_id=candidate,
                build_id="00000000-0000-4000-8000-000000000827",
                policy_report=_prebuilt_policy(used=False),
            )

        def persist_plan_in_session(self, _session, plan, **_kwargs):
            return plan

    class Operations:
        def build(self, plan, **_kwargs):
            return SimpleNamespace(
                id=str(uuid.uuid4()),
                state="succeeded",
                owner_id="build-id",
                result={
                    "successful_nodes": [plan.builder_node_id],
                    "failed_nodes": [],
                    "node_evidence": {
                        plan.builder_node_id: {
                            "image_bytes": 1,
                            "image_digest": "sha256:" + "d" * 64,
                            "oci_layout_sha256": "e" * 64,
                        }
                    },
                },
            )

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=Builds(),
        recipe_operations=Operations(),
        clock=lambda: now,
    )
    assert production.service._builder is not None
    claims = production.service.claim_pending(limit=3)
    claim = next(item for item in claims if item.operation_id == waiting_id)
    result = production.service._builder(
        recipe,
        {
            "recipe_revision_id": "revision-builder",
            "input_intent_sha256": "a" * 64,
        },
        claim=claim,
        build_input_sha256="",
        force=False,
        progress=lambda _progress: None,
    )
    assert not isinstance(result, BuildUnsettled)
    assert (
        AvailabilityBuildReceipt.model_validate_json(
            canonical_message(result)
        ).builder_node_id
        == node_id
    )
    production.close()


@pytest.mark.parametrize(
    (
        "failure_kind",
        "uncertain",
        "diagnostic_category",
        "error_code",
        "has_evidence",
        "malformed_kind",
    ),
    [
        (None, False, "platform-policy", "permission_denied", True, False),
        (
            "temporary-dependency",
            False,
            "network",
            "dependency_unavailable",
            True,
            False,
        ),
        (
            "uncertain-effect",
            True,
            "network",
            "operation_outcome_uncertain",
            True,
            False,
        ),
        (None, False, None, None, False, False),
        (
            "invalid-authority",
            False,
            "platform-policy",
            "permission_denied",
            True,
            True,
        ),
    ],
)
def test_builder_parent_preserves_typed_failure_and_retry_policy(
    tmp_path,
    monkeypatch,
    failure_kind: str | None,
    uncertain: bool,
    diagnostic_category: str | None,
    error_code: str | None,
    has_evidence: bool,
    malformed_kind: bool,
) -> None:
    """A child ending cannot poison later preparation under fresh authority.

    The parent owns bounded exact-output observation. Each new execution still
    passes the Controller's normal authorization; child taxonomy is not a
    second permanent gate.
    """
    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-source-build.json")
            .read_text()
        )
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'builder-failure.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    revision_id = "builder-failure-revision"
    builder_node_id = "builder-node-000000000000000000000000000000"
    with sessions.begin() as session:
        session.add(
            CatalogDocumentRevision(
                id=revision_id,
                document_id="builder-failure-document",
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=recipe.model_dump(mode="json"),
                content_digest=document_sha256(recipe.model_dump(mode="json")),
                artifact_key="c" * 64,
                execution_key="a" * 64,
                projected={},
                created_by="test",
                created_at=now,
            )
        )
        session.add(
            AgentNode(
                node_id=builder_node_id,
                state="active",
                architecture="linux-arm64",
            )
        )

    monkeypatch.setattr(
        availability_production,
        "resolve_recipe_entities",
        lambda _session, _document: {},
    )
    monkeypatch.setattr(
        availability_production,
        "_compile_consistent_runtime",
        lambda *_args, **_kwargs: {
            "input_intent_sha256": "a" * 64,
            "interface": "vonk.runtime.v1",
            "architecture": "linux/arm64",
            "image": "sha256:" + "a" * 64,
        },
    )

    class Builds:
        def resolve(self, revision_id: str):
            return RecipeBuildResolution(
                recipe_revision_id=revision_id,
                recipe_content_sha256=document_sha256(recipe.model_dump(mode="json")),
                source_bundle_sha256="c" * 64,
                input_intent_sha256="a" * 64,
                input_intent={},
            )

        def prepare_plan(self, revision_id: str, node_id: str, **_kwargs):
            return RecipeBuildPlan(
                build_id="00000000-0000-4000-8000-000000000743",
                recipe_revision_id=revision_id,
                recipe_content_sha256=document_sha256(recipe.model_dump(mode="json")),
                source_bundle_sha256="c" * 64,
                agent_payload={},
                build_input_sha256="b" * 64,
                builder_node_id=node_id,
            )

        def persist_plan_in_session(self, _session, plan, **_kwargs):
            return plan

    child_evidence: dict[str, object] | None = None
    if has_evidence:
        child_document: dict[str, object] = {
            "reason": "Package source rejected the build request",
            "summary": "PyTorch package index rejected access",
            "status": "failed",
            "operation": "recipe.build.v1",
            "stage": "build",
        }
        if failure_kind is not None:
            child_document["failure_kind"] = failure_kind
        if error_code is not None:
            child_document["error_code"] = error_code
        if uncertain:
            child_document["uncertain"] = True
        if failure_kind == "temporary-dependency":
            child_document["retry_after_seconds"] = 2
        if diagnostic_category is not None:
            stderr = (
                "HTTP 403 Forbidden from package index; Authorization: Bearer child-secret-value"
                if diagnostic_category == "platform-policy"
                else "HTTP 503 package index temporarily unavailable"
            )
            child_document["diagnostics"] = {
                "schema_version": 1,
                "collected_at": now.isoformat(),
                "phase": "build",
                "category": diagnostic_category,
                "stdout": {
                    "text": "",
                    "truncated": False,
                    "dropped_bytes": 0,
                    "dropped_lines": 0,
                },
                "stderr": {
                    "text": stderr,
                    "truncated": False,
                    "dropped_bytes": 0,
                    "dropped_lines": 0,
                },
                "versions": [],
                "sandbox": [],
                "storage": [],
                "preflight": [],
                "collector_errors": [],
            }
        child_evidence = AgentFailureResult.model_validate_json(
            json.dumps(child_document)
        ).model_dump(mode="json", exclude_none=True)
        if malformed_kind:
            child_evidence["failure_kind"] = "unknown-failure-kind"

    class Operations:
        calls = 0

        def build(self, plan, **_kwargs):
            self.calls += 1
            if self.calls > 1:
                # The fault has cleared: a fresh request builds normally.
                receipt = _write_controller_build_receipt(
                    production.storage,
                    archive=b"recovered builder archive",
                    image_digest="sha256:" + "d" * 64,
                    build_id="build-id",
                    build_input_sha256=plan.build_input_sha256,
                    distribution_content_sha256=plan.recipe_content_sha256,
                )
                return SimpleNamespace(
                    id=str(uuid.uuid4()),
                    state="succeeded",
                    owner_id="build-id",
                    result={
                        "successful_nodes": [plan.builder_node_id],
                        "failed_nodes": [],
                        "node_evidence": {
                            plan.builder_node_id: {
                                "image_bytes": receipt.image_bytes,
                                "image_digest": receipt.image_digest,
                                "oci_layout_sha256": receipt.oci_archive_sha256,
                            }
                        },
                    },
                )
            node_evidence = (
                {plan.builder_node_id: child_evidence}
                if child_evidence is not None
                else {}
            )
            return SimpleNamespace(
                id="00000000-0000-4000-8000-000000000744",
                state="failed",
                result={
                    "successful_nodes": [],
                    "failed_nodes": [plan.builder_node_id],
                    "node_evidence": node_evidence,
                },
            )

    operations = Operations()

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=Builds(),
        recipe_operations=operations,
        clock=lambda: now,
    )
    queued = production.service.start(
        revision_id,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )

    assert production.service.run_pending() == 1
    failed = production.service.get(queued.id)
    assert failed.attempt == 1
    assert failed.failure is not None
    expected_retryable = error_code != "permission_denied" or malformed_kind
    expected_code = (
        "recipe_image.build_invalid"
        if not has_evidence or malformed_kind
        else error_code
    )
    assert failed.state == ("queued" if expected_retryable else "failed")
    assert failed.failure["retryable"] is expected_retryable
    assert failed.failure["code"] == expected_code
    detail = failed.failure["detail"]
    assert isinstance(detail, str)
    if has_evidence and not malformed_kind:
        if diagnostic_category is not None:
            assert "child-secret-value" not in str(failed.failure["log_excerpt"])
            if diagnostic_category == "platform-policy":
                assert "<redacted>" in str(failed.failure["log_excerpt"])
        assert "PyTorch package index rejected access" in detail
    assert operations.calls == 1
    assert production.service.run_pending() == 0
    assert operations.calls == 1
    fresh = production.service.start(
        revision_id, actor="operator", request_id=str(uuid.uuid4())
    )
    assert fresh.id != queued.id
    assert fresh.id != failed.id
    assert fresh.state == "queued"
    assert production.service.run_pending() == 1
    assert operations.calls == 2, production.service.get(fresh.id)
    assert production.service.get(fresh.id).state == LifecycleState.SUCCEEDED.value, (
        production.service.get(fresh.id)
    )
    now += timedelta(days=2)
    # Expiry reconciliation releases both owners without another dispatch.
    assert production.service.run_pending() == 0
    for operation_id in (queued.id, fresh.id):
        ended = production.service.get(operation_id)
        assert ended.next_attempt_at is None
    after_expiry = production.service.start(
        revision_id, actor="operator", request_id=str(uuid.uuid4())
    )
    assert after_expiry.state == "queued"
    assert after_expiry.id not in (queued.id, fresh.id)
    assert production.service.run_pending() == 1
    assert operations.calls == 3
    production.close()


@pytest.mark.usefixtures("damaged_json_rows")
def test_builder_source_error_is_not_mislabeled_as_capacity_wait(tmp_path) -> None:
    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-source-build.json")
            .read_text()
        )
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'builder-error.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            Job(
                id="00000000-0000-4000-8000-000000000703",
                request_id=str(uuid.uuid4()),
                kind="recipe.image.availability.v2",
                state="running",
                actor="operator",
                authority_revision="revision-builder",
                targets=["revision-builder"],
                payload_digest="a" * 64,
                payload=_typed_availability_payload(
                    {
                        "recipe_revision_id": "revision-builder",
                        "runtime": {
                            "recipe_revision_id": "revision-builder",
                            "builder_node_id": "builder-node",
                        },
                        "build_input_sha256": "b" * 64,
                        "claim_until": (now - timedelta(seconds=1)).isoformat(),
                    },
                    recipe,
                ),
                current_attempt=1,
                created_at=now,
                updated_at=now,
            )
        )

    class Builds:
        def resolve(self, _revision_id: str):
            return SimpleNamespace(input_intent_sha256="a" * 64)

        def prepare_plan(self, *_args, **_kwargs):
            raise RecipeImageAvailabilityError(
                "build.source_invalid", "canonical source is invalid"
            )

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=Builds(),
        recipe_operations=object(),
        clock=lambda: datetime.now(UTC),
    )
    assert production.service._builder is not None
    claim = production.service.claim_pending(limit=1)[0]
    raised = production.service._builder(
        recipe,
        {
            "recipe_revision_id": "revision-builder",
            "builder_node_id": "builder-node",
        },
        claim=claim,
        build_input_sha256="b" * 64,
        force=False,
        progress=lambda _progress: None,
    )
    assert isinstance(raised, BuildUnsettled)
    production.close()


@pytest.mark.usefixtures("damaged_json_rows")
def test_postgres_builder_transaction_does_not_cross_session_block(
    tmp_path, postgres_engine
) -> None:
    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-source-build.json")
            .read_text()
        )
    )
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    node_ids = (
        "spk_" + "1" * 32,
        "spk_" + "2" * 32,
    )
    operation_ids = (
        "00000000-0000-4000-8000-000000000711",
        "00000000-0000-4000-8000-000000000712",
    )
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(
                    node_id=node_id,
                    state="active",
                    architecture="linux-arm64",
                )
                for node_id in node_ids
            ]
            + [
                Job(
                    id=operation_id,
                    request_id=operation_id,
                    kind="recipe.image.availability.v2",
                    state="queued",
                    actor="operator",
                    authority_revision="revision-builder",
                    targets=["revision-builder"],
                    payload_digest="a" * 64,
                    payload=_typed_availability_payload(
                        {
                            "recipe_revision_id": "revision-builder",
                            "runtime": {
                                "recipe_revision_id": "revision-builder",
                                "input_intent_sha256": "a" * 64,
                            },
                            "build_input_sha256": None,
                        },
                        recipe,
                    ),
                    result=None,
                    current_attempt=0,
                    created_at=now,
                    updated_at=now,
                )
                for operation_id in operation_ids
            ]
        )
        session.add(
            CatalogDocument(
                id="document-builder",
                kind="recipe",
                publisher="test",
                slug="builder",
                title="Builder test recipe",
                created_by="test",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            CatalogDocumentRevision(
                id="revision-builder",
                document_id="document-builder",
                kind="recipe",
                publisher="test",
                slug="builder",
                revision_number=1,
                schema_version=2,
                state="active",
                document={},
                content_digest="e" * 64,
                projected={},
                created_by="test",
                created_at=now,
            )
        )

    barrier = threading.Barrier(2)
    preparation_lock = threading.Lock()
    prepared_threads: set[int] = set()
    persisted_nodes: list[str] = []

    class Builds:
        def resolve(self, _revision_id: str):
            return RecipeBuildResolution(
                recipe_revision_id=_revision_id,
                recipe_content_sha256=document_sha256(recipe.model_dump(mode="json")),
                source_bundle_sha256="c" * 64,
                input_intent_sha256="a" * 64,
                input_intent={},
            )

        def prepare_plan(self, _revision_id: str, node_id: str, **_kwargs):
            thread_id = threading.get_ident()
            with preparation_lock:
                first_preparation = thread_id not in prepared_threads
                prepared_threads.add(thread_id)
            if first_preparation:
                barrier.wait(timeout=5)
            suffix = node_id[-1]
            return SimpleNamespace(
                build_input_sha256=(suffix * 64),
                builder_node_id=node_id,
                build_id=str(uuid.uuid4()),
                policy_report=None,
            )

        def persist_plan_in_session(self, _session, plan, **_kwargs):
            assert _session.in_transaction()
            persisted_nodes.append(plan.builder_node_id)
            build_id = plan.build_id
            _session.add(
                RecipeBuild(
                    id=build_id,
                    recipe_revision_id="revision-builder",
                    builder_node_id=plan.builder_node_id,
                    source_bundle_sha256="c" * 64,
                    build_input_sha256=plan.build_input_sha256,
                    state="planned",
                    policy_report={},
                    plan={"build_id": build_id},
                    created_at=now,
                    updated_at=now,
                )
            )
            _session.flush()
            return plan

    class Operations:
        def build(self, plan, **_kwargs):
            return SimpleNamespace(
                id=str(uuid.uuid4()),
                state="succeeded",
                owner_id="build-id",
                result={
                    "successful_nodes": [plan.builder_node_id],
                    "failed_nodes": [],
                    "node_evidence": {
                        plan.builder_node_id: {
                            "image_bytes": 1,
                            "image_digest": "sha256:" + "d" * 64,
                            "oci_layout_sha256": "e" * 64,
                        }
                    },
                },
            )

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    builds = Builds()
    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=Operations(),
        clock=lambda: now,
    )

    claims = {
        claim.operation_id: claim for claim in production.service.claim_pending(limit=2)
    }

    def dispatch(operation_id: str):
        assert production.service._builder is not None
        return production.service._builder(
            recipe,
            {"recipe_revision_id": "revision-builder"},
            claim=claims[operation_id],
            build_input_sha256="",
            force=False,
            progress=lambda _progress: None,
        )

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        futures = tuple(
            executor.submit(dispatch, operation_id) for operation_id in operation_ids
        )
        results = tuple(future.result(timeout=8) for future in futures)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    selected = {
        AvailabilityBuildReceipt.model_validate_json(
            canonical_message(result)
        ).builder_node_id
        for result in results
        if not isinstance(result, BuildUnsettled)
    }
    assert selected == set(node_ids)
    assert set(persisted_nodes) == set(node_ids)
    with sessions() as session:
        builds = tuple(session.scalars(select(RecipeBuild)))
    assert {build.builder_node_id for build in builds} == set(node_ids)
    with sessions() as session:
        assigned: set[str] = set()
        for operation_id in operation_ids:
            job = session.get(Job, operation_id)
            assert job is not None
            payload = require_mapping(job.payload, "job payload")
            runtime = require_mapping(payload["runtime"], "job runtime")
            assigned.add(str(runtime["builder_node_id"]))
    assert assigned == set(node_ids)
    production.close()


@pytest.mark.usefixtures("damaged_json_rows")
def test_postgres_connected_source_build_queues_model_child_until_builder_eligible(
    tmp_path, postgres_engine, monkeypatch
) -> None:
    model_bytes = b"connected model bytes"
    model_raw = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    model_raw["source"] = {
        "repository": "https://huggingface.co/acme/synthetic-tiny",
        "revision": "0" * 40,
    }
    model_raw["files"][0]["sha256"] = hashlib.sha256(model_bytes).hexdigest()
    model_raw["files"][0]["size_bytes"] = len(model_bytes)

    model = ModelDefinition.model_validate(model_raw)
    model_digest = document_sha256(model.model_dump(mode="json"))
    recipe_raw = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text()
    )
    recipe_raw["models"][0]["model"]["content_sha256"] = model_digest
    recipe = RecipeDefinition.model_validate(recipe_raw)
    recipe_digest = document_sha256(recipe.model_dump(mode="json"))
    now = datetime.now(UTC)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions,
        clock=lambda: now,
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
    )
    connected_recipe_revision_id = catalog.import_recipe_library(
        "test",
        library_commit="0" * 40,
        source_path="recipes/synthetic-tiny.json",
        document=recipe.model_dump(mode="json"),
        expected_content_sha256=recipe_digest,
        dependency_documents=[model.model_dump(mode="json")],
        source_bundle_sha256="c" * 64,
    ).id

    def model_handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, request=request, content=model_bytes)

    model_http = httpx2.Client(
        transport=httpx2.MockTransport(model_handler),
        follow_redirects=False,
    )
    model_cache = ModelCacheService(
        sessions,
        tmp_path / "model-cache",
        reserve_bytes=0,
        max_parallel_downloads=1,
        fixture_sources=True,
        http_client=model_http,
    )
    intent = "f" * 64
    final_input = "e" * 64
    image_digest = "sha256:" + "a" * 64
    archive = b"connected oci archive"
    archive_digest = hashlib.sha256(archive).hexdigest()

    class Builds:
        def resolve(self, _revision_id: str):
            return RecipeBuildResolution(
                recipe_revision_id=_revision_id,
                recipe_content_sha256=document_sha256(recipe.model_dump(mode="json")),
                source_bundle_sha256="c" * 64,
                input_intent_sha256=intent,
                input_intent={},
            )

        def prepare_plan(self, _revision_id: str, node_id: str, **_kwargs):
            return SimpleNamespace(
                build_input_sha256=final_input,
                builder_node_id=node_id,
                build_id="00000000-0000-4000-8000-000000000721",
                policy_report=None,
            )

        def persist_plan_in_session(self, _session, plan, **_kwargs):
            return plan

    class Operations:
        def build(self, plan, *, build_input_sha256: str, **_kwargs):
            assert build_input_sha256 == final_input
            build_id = "00000000-0000-4000-8000-000000000721"
            with sessions.begin() as session:
                session.add(
                    RecipeBuild(
                        id=build_id,
                        recipe_revision_id=connected_recipe_revision_id,
                        builder_node_id=plan.builder_node_id,
                        source_bundle_sha256="c" * 64,
                        build_input_sha256=final_input,
                        state="succeeded",
                        policy_report={"builder_binary_digest": "d" * 64},
                        plan={},
                        image_digest=image_digest,
                        oci_layout_sha256=archive_digest,
                        image_bytes=len(archive),
                        created_at=now,
                        updated_at=now,
                    )
                )
            production.storage.root.mkdir(parents=True, exist_ok=True)
            place_test_image(production.storage, archive_digest, len(archive))
            return SimpleNamespace(
                id=str(uuid.uuid4()),
                state="succeeded",
                owner_id=build_id,
                result={
                    "successful_nodes": [plan.builder_node_id],
                    "failed_nodes": [],
                    "node_evidence": {
                        plan.builder_node_id: {
                            "image_digest": image_digest,
                            "oci_layout_sha256": archive_digest,
                            "image_bytes": len(archive),
                        }
                    },
                },
            )

    class Transport:
        def inspect_archive(self, archive_path, **_kwargs):
            assert archive_path.exists()
            return PulledImageEvidence(
                manifest_digest=image_digest,
                config_id="sha256:" + "b" * 64,
                local_reference="localhost/vonk/recipe-build@" + image_digest,
                architecture="linux/arm64",
                runtime_interface="v1",
                archive_sha256=archive_digest,
                archive_bytes=len(archive),
            )

    monkeypatch.setattr(availability_production, "OciLayoutImageTransport", Transport)

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=Builds(),
        recipe_operations=Operations(),
        model_cache=model_cache,
        clock=lambda: now,
    )
    parent = production.service.start(
        connected_recipe_revision_id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000722",
    )
    assert parent.state == "queued"
    assert parent.model_child is None

    assert production.service.run_pending() == 1
    waiting = production.service.get(parent.id)
    assert waiting.state == "queued"
    assert waiting.failure is not None
    # The wait names its cause and is exposed as a typed blocker, not silence.
    assert waiting.next_attempt_at is not None
    assert waiting.model_child is not None

    for _ in range(100):
        model_cache.tick()
        child = model_cache.get_operation(str(waiting.model_child["id"]))
        if child.state == "succeeded":
            break
        time.sleep(0.01)
    assert child.state == "succeeded"
    with sessions() as session:
        session.add(
            AgentNode(
                node_id="spk_" + "7" * 32,
                state="active",
                architecture="linux-arm64",
            )
        )
        session.commit()
        row = session.get(Job, parent.id)
        assert row is not None
        row.payload = dict(row.payload) | {
            "retry_after_at": (now - timedelta(seconds=1)).isoformat()
        }
        session.commit()

    assert production.service.run_pending() == 1
    completed = production.service.get(parent.id)
    assert completed.state == "succeeded", completed.failure
    assert completed.failure is None
    assert completed.supported_actions == ()
    assert completed.result is not None
    model_child = require_mapping(completed.result["model_child"], "model child")
    assert model_child["state"] == "succeeded"
    with sessions() as session:
        persisted = session.get(Job, parent.id)
        assert persisted is not None
        images = revision_images(session, [persisted.authority_revision])
        assert completed.result["oci_archive_sha256"] in {
            image.archive_sha256 for image in images[persisted.authority_revision]
        }
        assert "failure" not in persisted.payload
        assert "retry_after_at" not in persisted.payload
        assert persisted.result == completed.result
    model_cache.close()
    model_http.close()
    production.close()


@pytest.mark.usefixtures("damaged_json_rows")
def test_build_progress_reads_current_attempt_upload_from_persisted_json() -> None:
    from vonk_control.models import AgentOperation, AgentOperationAttempt

    engine = create_engine("sqlite://")
    AgentOperation.metadata.tables[AgentOperation.__tablename__].create(engine)
    AgentOperationAttempt.metadata.tables[AgentOperationAttempt.__tablename__].create(
        engine
    )
    sessions = sessionmaker(bind=engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            AgentOperation(
                id="operation",
                parent_job_id="build-job",
                node_id="builder",
                kind="recipe.build.v1",
                payload_digest="a" * 64,
                payload={},
                authority_revision="current",
                state="running",
                current_attempt=2,
                created_at=now,
                updated_at=now,
            )
        )
        for attempt, completed in [(1, 999), (2, 128)]:
            session.add(
                AgentOperationAttempt(
                    id=f"attempt-{attempt}",
                    operation_id="operation",
                    attempt=attempt,
                    fence=f"fence-{attempt}",
                    lease_deadline=now,
                    agent_certificate_serial="serial",
                    state="running",
                    progress={
                        "phase": "uploading",
                        "completed_bytes": completed,
                        "total_bytes": 256,
                        "total_bytes_known": True,
                        "members": [],
                    },
                )
            )
    with sessions() as session:
        progress = availability_production._build_progress(
            session, "build-job", "builder"
        )
        assert progress is not None
        assert progress.phase == "uploading"
        assert progress.completed_bytes == 128
        assert progress.total_bytes == 256
        assert (
            availability_production._build_progress(session, "build-job", "other")
            is None
        )
        current = session.get(AgentOperationAttempt, "attempt-2")
        assert current is not None
        stale_progress: dict[str, object] = {"completed_bytes": 128}
        current.progress = stale_progress
        session.flush()
        with pytest.raises(Exception) as _ending:
            availability_production._build_progress(session, "build-job", "builder")


def test_unknown_build_planning_preserves_typed_reason_and_retry_policy() -> None:
    """A stale plan must re-enter observation rather than become terminal debt."""
    from vonk_agent_protocol import RecipeBuildCode, WaitReason
    from vonk_control.recipe_builds import RecipeBuildUnknown

    failure = availability_production._planning_failure(
        RecipeBuildUnknown(
            RecipeBuildCode.PLAN_INVALID,
            "stored plan is unavailable",
            reason=WaitReason.STALE_PLAN,
        )
    )
    assert failure.retryable is True
