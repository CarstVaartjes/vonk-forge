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

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentFailureResult
from vonk_control import availability_production
from vonk_control.auth import TokenCodec
from vonk_control.availability_production import (
    RecipeImageAvailabilityScheduler,
    build_recipe_image_availability,
)
from vonk_control.bounded_json import require_mapping
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.catalog_service import CatalogService
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
    RuntimeImageAuthorization,
)
from vonk_control.recipe_builds import RecipeBuildPlan, RecipeBuildResolution
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityClaim,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
)
from vonk_control.runtime_image_preparation import PulledImageEvidence
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256


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
                image_identity=None,
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


def test_production_factory_claim_compiles_and_persists_sql_receipt(
    tmp_path, monkeypatch
) -> None:
    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-image.json")
            .read_text()
        )
    )
    model = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'authority.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        catalog = CatalogEntityService(session, clock=lambda: now)
        model_revision = catalog.create_draft(model, actor="test")
        catalog.resolve(model_revision.id, actor="test")
        recipe_revision = catalog.create_draft(
            recipe.model_dump(mode="json"), actor="test"
        )
        catalog.resolve(recipe_revision.id, actor="test")
        recipe_revision_id = recipe_revision.id

    class Transport:
        def pull_and_export(self, reference, destination, **_kwargs):
            archive = b"production availability archive"
            destination.write_bytes(archive)
            return PulledImageEvidence(
                manifest_digest="sha256:" + "e" * 64,
                requested_manifest_digest="sha256:" + "d" * 64,
                config_id="sha256:" + "c" * 64,
                local_reference=reference,
                architecture="linux/arm64",
                runtime_interface="v1",
                archive_sha256=hashlib.sha256(archive).hexdigest(),
                archive_bytes=len(archive),
            )

    monkeypatch.setattr(availability_production, "SkopeoOCIImageTransport", Transport)

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=object(),
        recipe_operations=object(),
        clock=lambda: now,
    )
    queued = production.service.start(
        recipe_revision_id, actor="operator", request_id="r" * 36
    )
    claim = production.service.claim_pending(owner_id="worker-a")[0]
    production.service.run_claim(claim)
    assert production.service.get(queued.id).state == "succeeded"
    with sessions() as session:
        authorization = session.scalar(
            select(RuntimeImageAuthorization).where(
                RuntimeImageAuthorization.recipe_revision_id == recipe_revision_id
            )
        )
        assert authorization is not None
        assert authorization.state == "authorized"
        receipt = production.service._storage.read_receipt(
            authorization.oci_archive_sha256
        )
        assert receipt.image_bytes == authorization.image_bytes
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
                content_digest=content_sha256(recipe),
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
                content_digest=content_sha256(recipe),
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
                capabilities=["recipe.build.v1"],
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
                payload={
                    "recipe_revision_id": "revision-builder",
                    "runtime": {
                        "recipe_revision_id": "revision-builder",
                        "input_intent_sha256": "a" * 64,
                    },
                    "build_input_sha256": None,
                },
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
                recipe_content_sha256=content_sha256(recipe),
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
                            "build_input_sha256": "b" * 64,
                            "image_bytes": 1,
                            "image_digest": "sha256:" + "d" * 64,
                            "oci_layout_sha256": "e" * 64,
                            "policy": {
                                "passed": True,
                                "dockerfile": "Dockerfile",
                                "findings": [],
                            },
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
        with pytest.raises(RecipeImageAvailabilityError) as failure:
            execute()
        assert failure.value.code == "recipe_image.build_capacity_wait"
        assert failure.value.retryable
    else:
        result = execute()
        assert result["build_input_sha256"] == "b" * 64
    assert builds.plan_calls == 1
    production.close()


@pytest.mark.parametrize(
    (
        "failure_kind",
        "uncertain",
        "diagnostic_category",
        "error_code",
        "has_evidence",
        "malformed_kind",
        "expected_state",
        "expected_retryable",
        "expected_code",
    ),
    [
        (
            None,
            False,
            "platform-policy",
            "permission_denied",
            True,
            False,
            "failed",
            False,
            "permission_denied",
        ),
        (
            "temporary-dependency",
            False,
            "network",
            "dependency_unavailable",
            True,
            False,
            "queued",
            True,
            "dependency_unavailable",
        ),
        (
            "uncertain-effect",
            True,
            "network",
            "operation_outcome_uncertain",
            True,
            False,
            "failed",
            False,
            "operation_outcome_uncertain",
        ),
        (
            None,
            False,
            None,
            None,
            False,
            False,
            "failed",
            False,
            "recipe_image.build_invalid",
        ),
        (
            "invalid-authority",
            False,
            "platform-policy",
            "permission_denied",
            True,
            True,
            "failed",
            False,
            "recipe_image.build_invalid",
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
    expected_state: str,
    expected_retryable: bool,
    expected_code: str,
) -> None:
    """A failed canonical build must not turn a terminal receipt into a retry.

    This crosses the production builder closure and the parent availability
    worker.  The old wrapper discarded the child node receipt and marked every
    failed build retryable, so a permission-policy refusal was automatically
    dispatched again.
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
                content_digest=content_sha256(recipe),
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
                capabilities=["recipe.build.v1"],
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
                recipe_content_sha256=content_sha256(recipe),
                source_bundle_sha256="c" * 64,
                input_intent_sha256="a" * 64,
                input_intent={},
            )

        def prepare_plan(self, revision_id: str, node_id: str, **_kwargs):
            return RecipeBuildPlan(
                build_id="00000000-0000-4000-8000-000000000743",
                recipe_revision_id=revision_id,
                recipe_content_sha256=content_sha256(recipe),
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
    assert failed.state == expected_state
    assert failed.attempt == 1
    assert failed.failure is not None
    assert failed.failure["code"] == expected_code
    assert failed.failure["retryable"] is expected_retryable
    detail = failed.failure["detail"]
    assert isinstance(detail, str)
    if has_evidence and not malformed_kind:
        if diagnostic_category is not None:
            assert detail.startswith(f"build: {diagnostic_category}: ")
            assert "child-secret-value" not in str(failed.failure["log_excerpt"])
            if diagnostic_category == "platform-policy":
                assert "<redacted>" in str(failed.failure["log_excerpt"])
        assert "PyTorch package index rejected access" in detail
    assert operations.calls == 1
    assert production.service.run_pending() == 0
    assert operations.calls == 1
    production.close()


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
                payload={
                    "recipe_revision_id": "revision-builder",
                    "runtime": {
                        "recipe_revision_id": "revision-builder",
                        "builder_node_id": "builder-node",
                    },
                    "build_input_sha256": "b" * 64,
                },
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
    with pytest.raises(RecipeImageAvailabilityError) as raised:
        production.service._builder(
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
    assert raised.value.code == "build.source_invalid"
    production.close()


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
                    capabilities=["recipe.build.v1"],
                )
                for node_id in node_ids
            ]
            + [
                Job(
                    id=operation_id,
                    request_id=operation_id,
                    kind="recipe.image.availability.v2",
                    state="running",
                    actor="operator",
                    authority_revision="revision-builder",
                    targets=["revision-builder"],
                    payload_digest="a" * 64,
                    payload={
                        "recipe_revision_id": "revision-builder",
                        "runtime": {
                            "recipe_revision_id": "revision-builder",
                            "input_intent_sha256": "a" * 64,
                        },
                        "build_input_sha256": None,
                    },
                    result=None,
                    current_attempt=1,
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
                recipe_content_sha256=content_sha256(recipe),
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
                            "build_input_sha256": plan.build_input_sha256,
                            "image_bytes": 1,
                            "image_digest": "sha256:" + "d" * 64,
                            "oci_layout_sha256": "e" * 64,
                            "policy": {
                                "passed": True,
                                "dockerfile": "Dockerfile",
                                "findings": [],
                            },
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
    selected = {result["builder_node_id"] for result in results}
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
    model_digest = content_sha256(model)
    recipe_raw = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text()
    )
    recipe_raw["models"][0]["model"]["content_sha256"] = model_digest
    recipe = RecipeDefinition.model_validate(recipe_raw)
    recipe_digest = content_sha256(recipe)
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

    def model_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=model_bytes)

    model_http = httpx.Client(
        transport=httpx.MockTransport(model_handler),
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
                recipe_content_sha256=content_sha256(recipe),
                source_bundle_sha256="c" * 64,
                input_intent_sha256=intent,
                input_intent={},
            )

        def prepare_plan(self, _revision_id: str, node_id: str, **_kwargs):
            return SimpleNamespace(
                build_input_sha256=final_input,
                builder_node_id=node_id,
                build_id="00000000-0000-4000-8000-000000000721",
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
            (production.storage.root / archive_digest).write_bytes(archive)
            return SimpleNamespace(
                id=str(uuid.uuid4()),
                state="succeeded",
                owner_id=build_id,
                result={
                    "successful_nodes": [plan.builder_node_id],
                    "failed_nodes": [],
                    "node_evidence": {
                        plan.builder_node_id: {
                            "build_input_sha256": final_input,
                            "image_digest": image_digest,
                            "oci_layout_sha256": archive_digest,
                            "image_bytes": len(archive),
                            "policy": {
                                "passed": True,
                                "dockerfile": "Dockerfile",
                                "findings": [],
                            },
                        }
                    },
                },
            )

    class Transport:
        def inspect_archive(self, archive_path, **_kwargs):
            assert archive_path.exists()
            return PulledImageEvidence(
                manifest_digest=image_digest,
                requested_manifest_digest=None,
                config_id="sha256:" + "b" * 64,
                local_reference="localhost/vonk/recipe-build@" + image_digest,
                architecture="linux/arm64",
                runtime_interface="v1",
                archive_sha256=archive_digest,
                archive_bytes=len(archive),
            )

    monkeypatch.setattr(availability_production, "SkopeoOCIImageTransport", Transport)

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
    assert waiting.failure["code"] == "recipe_image.build_capacity_wait"
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
                capabilities=["recipe.build.v1"],
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
        authorization = session.scalar(select(RuntimeImageAuthorization))
        assert authorization is not None
        assert (
            completed.result["oci_archive_sha256"] == authorization.oci_archive_sha256
        )
        persisted = session.get(Job, parent.id)
        assert persisted is not None
        assert "failure" not in persisted.payload
        assert "retry_after_at" not in persisted.payload
        assert persisted.result == completed.result
    model_cache.close()
    model_http.close()
    production.close()


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
        with pytest.raises(ValueError):
            availability_production._build_progress(session, "build-job", "builder")
