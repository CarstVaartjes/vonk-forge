from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, Table, create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.cluster_mappings import ClusterMappingPlacement, ClusterMappingPlan
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
    FleetProfileApplicationView,
    FleetProfileAssignmentInput,
    FleetProfileChildOperation,
    FleetProfileChildProgress,
    FleetProfileInput,
    FleetProfileScope,
    FleetProfileSwitchAdapterResult,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildResult,
    FleetProfileSwitchChildState,
    FleetProfileVerificationResult,
)
from vonk_control.fleet_profiles import (
    FleetProfileConflict,
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    _operation_state,
    build_production_fleet_profile_service,
)
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    FleetProfile,
    FleetProfileApplication,
    InstallationNode,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.preparation_contract import RolloutPreparation
from vonk_control.recipe_operations import RecipeOperationService
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


def _json_object(value: object) -> dict[str, object]:
    """Return a decoded JSON object as the very same mutable mapping."""

    assert isinstance(value, dict)
    return value


def _nested_object(value: object, *path: str | int) -> dict[str, object]:
    """Return one nested JSON object without copying any level of it."""

    for key in path:
        if isinstance(key, int):
            assert isinstance(value, list)
            value = value[key]
        else:
            assert isinstance(value, dict)
            value = value[key]
    return _json_object(value)


def _uuid(value: int) -> str:
    return f"00000000-0000-4000-8000-{value:012x}"


def _node_id(value: int) -> str:
    return "spk_" + f"{value:032x}"


def test_profile_progress_and_results_are_closed_nested_contracts() -> None:
    operation_id = _uuid(700)
    progress = FleetProfileApplicationProgress.model_validate(
        {
            "operation_kind": "fleet-profile.apply",
            "completed_steps": 1,
            "total_steps": 1,
            "step_results": {
                "0": {
                    "operation_id": operation_id,
                    "result": {"verified": True},
                }
            },
        }
    )
    step_result = progress.step_results["0"].result
    assert isinstance(step_result, FleetProfileVerificationResult)
    assert step_result.verified is True
    assert FleetProfileApplicationResult(changed=True, completed_steps=1).model_dump(
        mode="json"
    ) == {"changed": True, "completed_steps": 1}
    adapter_result = FleetProfileSwitchAdapterResult(
        children=[
            FleetProfileSwitchChildState(
                operation_id=operation_id, kind="run", state="succeeded"
            )
        ],
        assignment_ids=[],
    )
    assert adapter_result.children[0].operation_id == operation_id
    with pytest.raises(ValidationError):
        FleetProfileApplicationProgress.model_validate(
            {"step_results": {}, "unexpected": True}
        )
    with pytest.raises(ValidationError):
        FleetProfileApplicationResult.model_validate({"completed_steps": 1})
    with pytest.raises(ValidationError):
        FleetProfileSwitchAdapterResult.model_validate({"assignment_ids": []})
    with pytest.raises(ValidationError):
        FleetProfileSwitchChildResult.model_validate(
            {"run_switch_operation_id": operation_id}
        )


def test_profile_switch_state_rejects_malformed_persisted_progress() -> None:
    with pytest.raises(FleetProfileConflict, match="progress is invalid"):
        RunSwitchFleetProfileAdapter._state(
            FleetProfileApplication(progress="invalid")
        )
    with pytest.raises(FleetProfileConflict, match="progress is invalid"):
        RunSwitchFleetProfileAdapter._state(
            FleetProfileApplication(progress={"switch_adapter": "invalid"})
        )


def test_profile_application_read_rejects_malformed_persisted_plan_and_result() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(_input(revision_id), actor="admin")
    preview = service.preview(profile.id)
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(701),
        actor="admin",
    )

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.plan = {"steps": []}
    with pytest.raises(FleetProfileConflict, match="plan is invalid"):
        service.application(application.id)

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.plan = preview.model_dump(mode="json")
        # A JSON array is never a valid stored result document; write it as the
        # driver would have persisted it rather than through the typed attribute.
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == application.id)
            .values(result=["malformed"])
        )
    with pytest.raises(FleetProfileConflict, match="result is invalid"):
        service.application(application.id)


def test_profile_worker_marks_malformed_persisted_plan_failed() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, recipe_operations=_UnreachedOperations()
    )
    profile = service.create(_input(revision_id), actor="admin")
    preview = service.preview(profile.id)
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(702),
        actor="admin",
    )

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.plan = {"steps": []}

    assert service.tick() is True
    with sessions() as session:
        failed = session.get(FleetProfileApplication, application.id)
        assert failed is not None
        assert failed.state == "failed"
        assert failed.status_reason == "Persisted Fleet profile plan is invalid"


def test_profile_worker_marks_malformed_persisted_progress_failed() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)

    class Operations(RecipeOperationService):
        def __init__(self) -> None:
            pass

        def get(self, _operation_id):
            return FleetProfileChildOperation(id=_uuid(704), state="running")

    service = FleetProfileService(
        sessions, clock=lambda: NOW, recipe_operations=Operations()
    )
    profile = service.create(_input(revision_id), actor="admin")
    preview = service.preview(profile.id)
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(705),
        actor="admin",
    )

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.current_operation_id = _uuid(706)
        # Persisted JSON can be a string even though the contract requires an
        # object; write what the driver stored rather than the typed attribute.
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == application.id)
            .values(progress="corrupt-json")
        )

    assert service.tick() is True
    with sessions() as session:
        failed = session.get(FleetProfileApplication, application.id)
        assert failed is not None
        assert failed.state == "failed"
        assert failed.status_reason == "Persisted Fleet profile progress is invalid"


def test_profile_application_read_requires_result_for_succeeded_state() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(_input(revision_id), actor="admin")
    preview = service.preview(profile.id)
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(703),
        actor="admin",
    )

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.state = "succeeded"
        row.result = None
    with pytest.raises(FleetProfileConflict, match="result is invalid"):
        service.application(application.id)


def _exact_preparation(
    node_ids: tuple[str, ...], *, observed_at: datetime = NOW
) -> RolloutPreparation:
    """Return preparation evidence from the Controller authority fixture."""

    model_digest = "a" * 64
    image_layout_digest = "e" * 64
    image_digest = "sha256:" + "d" * 64
    controller_model = {
        "state": "ready",
        "expected_bytes": 100,
        "verified_bytes": 100,
        "missing_bytes": 0,
        "verified_sha256": model_digest,
        "verified_at": observed_at,
        "source": "nas-cache",
    }
    controller_image = {
        "state": "ready",
        "expected_bytes": 100,
        "verified_bytes": 100,
        "missing_bytes": 0,
        "verified_sha256": image_layout_digest,
        "verified_at": observed_at,
        "source": "controller-build",
    }
    return RolloutPreparation.model_validate(
        {
            "model": {
                "artifact_set_sha256": model_digest,
                "model_content_sha256": "b" * 64,
                "recipe_revision_sha256": "c" * 64,
                "artifact_count": 1,
                "artifact_set_bytes": 100,
                "dependency_model_content_sha256": [],
                "completeness": "complete",
                "controller": controller_model,
                "targets": [
                    {
                        "node_id": node_id,
                        "state": "ready",
                        "expected_bytes": 100,
                        "present_bytes": 100,
                        "missing_bytes": 0,
                        "verified_sha256": model_digest,
                        "verified_at": observed_at,
                        "reason": None,
                    }
                    for node_id in node_ids
                ],
            },
            "runtime_image": {
                "image_digest": image_digest,
                "oci_layout_sha256": image_layout_digest,
                "image_bytes": 100,
                "architecture": "linux-arm64",
                "runtime_interface": "vonk.runtime.v1",
                "build_id": "build-1",
                "controller": controller_image,
                "targets": [
                    {
                        "node_id": node_id,
                        "state": "ready",
                        "expected_bytes": 100,
                        "present_bytes": 100,
                        "missing_bytes": 0,
                        "verified_sha256": image_layout_digest,
                        "imported_image_digest": image_digest,
                        "verified_at": observed_at,
                        "reason": None,
                    }
                    for node_id in node_ids
                ],
            },
            "exceptions": [],
            "target_node_ids": list(node_ids),
            "controller_ready": True,
            "targets_ready": True,
            "ready": True,
            "reasons": [],
        }
    )


class _SwitchStart(TypedDict):
    """One recorded call to the switch adapter, in the shape the tests read."""

    application_id: str
    assignment_ids: tuple[str, ...]
    assignment_scopes: tuple[tuple[str, ...], ...]
    scope_node_ids: tuple[str, ...]
    actor: str
    request_id: str


class _SwitchAdapter:
    def __init__(self) -> None:
        self.starts: list[_SwitchStart] = []
        self._states: dict[str, int] = {}
        self._operations: dict[str, list[FleetProfileChildOperation]] = {}
        self._by_request: dict[str, str] = {}

    def start(
        self,
        *,
        application_id: str,
        assignments,
        scope_node_ids: tuple[str, ...],
        actor: str,
        request_id: str,
    ) -> FleetProfileChildOperation:
        if request_id in self._by_request:
            return self._operations[self._by_request[request_id]][0]
        operation_id = _uuid(500 + len(self.starts))
        self._by_request[request_id] = operation_id
        self.starts.append(
            {
                "application_id": application_id,
                "assignment_ids": tuple(item.id for item in assignments),
                "assignment_scopes": tuple(
                    tuple(node.node_id for node in item.nodes) for item in assignments
                ),
                "scope_node_ids": scope_node_ids,
                "actor": actor,
                "request_id": request_id,
            }
        )
        progress = [
            FleetProfileChildProgress(
                phase="model-download", bytes=0, total_bytes=100
            ),
            FleetProfileChildProgress(
                phase="container-download", bytes=100, total_bytes=100
            ),
            FleetProfileChildProgress(
                phase="target-copy",
                node_ids=list(scope_node_ids),
                bytes=100,
                total_bytes=100,
            ),
            FleetProfileChildProgress(phase="start", node_ids=list(scope_node_ids)),
            FleetProfileChildProgress(
                phase="final-verify", node_ids=list(scope_node_ids)
            ),
        ]
        self._states[operation_id] = 0
        self._operations[operation_id] = [
            FleetProfileChildOperation(
                id=operation_id, state="running", progress=item
            )
            for item in progress
        ] + [
            FleetProfileChildOperation(
                id=operation_id,
                state="succeeded",
                progress=progress[-1],
                result=FleetProfileVerificationResult(verified=True),
            )
        ]
        return self._operations[operation_id][0]

    def get(self, operation_id: str, *, session: object = None) -> FleetProfileChildOperation:
        states = self._operations[operation_id]
        index = min(self._states[operation_id] + 1, len(states) - 1)
        self._states[operation_id] = index
        return states[index]


class _UnreachedOperations(RecipeOperationService):
    """Placeholder boundary a malformed persisted document must never reach."""

    def __init__(self) -> None:
        pass


def _database() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _recipe_document() -> dict[str, object]:
    return json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-image.json")
        .read_text(encoding="utf-8")
    )


def _seed(sessions: sessionmaker[Session]) -> tuple[str, str]:
    recipe_id = _uuid(1)
    revision_id = _uuid(2)
    document = _recipe_document()
    recipe = RecipeDefinition.model_validate(document)
    model = ModelDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "model-definition.json")
            .read_text(encoding="utf-8")
        )
    )
    model_id = _uuid(3)
    model_revision_id = _uuid(4)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(1),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )
        # Both documents must be persisted: the revisions below reference these
        # ids. This was `add(a, b)`, but Session.add takes one instance and a
        # private `_warn` flag, so the model document was silently discarded
        # while its revision row was still inserted.
        session.add_all(
            [
                CatalogDocument(
                    id=recipe_id,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    title=recipe.metadata.title,
                    created_by="admin",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                CatalogDocument(
                    id=model_id,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    title=model.identity.model.title,
                    created_by="admin",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.add_all(
            [
                CatalogDocumentRevision(
                id=revision_id,
                document_id=recipe_id,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                revision_number=1,
                state="active",
                schema_version=2,
                document=document,
                content_digest=content_sha256(recipe),
                execution_key="b" * 64,
                created_by="admin",
                created_at=NOW,
            ),
                CatalogDocumentRevision(
                id=model_revision_id,
                document_id=model_id,
                kind="model",
                publisher=model.identity.publisher,
                slug=model.identity.slug,
                revision_number=1,
                state="active",
                schema_version=2,
                document=model.model_dump(mode="json"),
                content_digest=content_sha256(model),
                artifact_key="a" * 64,
                created_by="admin",
                created_at=NOW,
            ),
            ]
        )
    return recipe_id, revision_id


def _input(revision_id: str, *, name: str = "Studio ready") -> FleetProfileInput:
    return FleetProfileInput.model_validate(
        {
            "name": name,
            "description": "Keep the studio Spark ready for local chat.",
            "installation_policy": "keep-cached",
            "labels": {"purpose": "interactive"},
            "favorite": True,
            "assignments": [
                {
                    "recipe_selector": "vonk-forge/synthetic-tiny-image",
                    "spark_ids": [_node_id(1)],
                    "desired_state": "running",
                    "assignment_name": "studio-chat",
                }
            ],
        }
    )


class _ProfileLifecycleSimulator(RecipeOperationService):
    """Small operation boundary that materializes each accepted lifecycle effect.

    It replaces the whole operation surface, so it never initializes the
    production service state and only implements the calls the profile
    coordinator makes.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.operations: dict[str, SimpleNamespace] = {}
        self.events: list[str] = []
        self._sequence = 100

    def _id(self) -> str:
        self._sequence += 1
        return _uuid(self._sequence)

    def _digest(self, value: object) -> str:
        return sha256(repr(value).encode()).hexdigest()

    def _operation(self, kind: str, owner_id: str) -> SimpleNamespace:
        operation_id = str(uuid4())
        operation = SimpleNamespace(
            id=operation_id, owner_id=owner_id, kind=kind, state="succeeded"
        )
        self.operations[operation_id] = operation
        self.events.append(kind)
        return operation

    def get(self, operation_id: str, *, session: object = None) -> SimpleNamespace:
        return self.operations[operation_id]

    def preview_mapping(self, revision_id, node_ids, *, parameters, actor):
        with self.sessions() as session:
            revision = session.get(CatalogDocumentRevision, revision_id)
            assert revision is not None
            topology_name = RecipeDefinition.model_validate(
                revision.document
            ).topology.name
        return ClusterMappingPlan(
            recipe_revision_id=revision_id,
            recipe_content_sha256="a" * 64,
            topology_name=topology_name,
            generation=1,
            parameters=dict(parameters),
            nodes=tuple(
                ClusterMappingPlacement(
                    node_id=node_id,
                    rank=rank,
                    role=("entrypoint" if rank == 0 else "worker"),
                    endpoint_owner=rank == 0,
                )
                for rank, node_id in enumerate(node_ids)
            ),
            placement_digest=self._digest((revision_id, tuple(node_ids))),
        )

    def create_mapping(self, plan, *, actor):
        self.events.append("create-placement")
        mapping_id = self._id()
        now = NOW
        with self.sessions.begin() as session:
            session.add(
                ClusterMapping(
                    id=mapping_id,
                    recipe_revision_id=plan.recipe_revision_id,
                    topology_name=plan.topology_name,
                    generation=plan.generation,
                    node_count=len(plan.nodes),
                    state="ready",
                    parameters=dict(plan.parameters),
                    placement_digest=plan.placement_digest,
                    endpoint_owner_node_id=plan.nodes[0].node_id,
                    created_by=actor,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add_all(
                ClusterMappingNode(
                    id=self._id(),
                    mapping_id=mapping_id,
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    endpoint_owner=node.endpoint_owner,
                    created_at=now,
                )
                for node in plan.nodes
            )
        return mapping_id

    def preview_build(self, revision_id, builder_node_id):
        build_input = self._digest(("build", revision_id))
        return SimpleNamespace(
            build_id=self._id(),
            recipe_revision_id=revision_id,
            builder_node_id=builder_node_id,
            source_bundle_sha256=self._digest(("source", revision_id)),
            build_input_sha256=build_input,
        )

    def build(self, plan, *, build_input_sha256, actor, request_id):
        build_id = plan.build_id
        image_digest = "sha256:" + self._digest(("image", plan.recipe_revision_id))
        with self.sessions.begin() as session:
            session.add(
                RecipeBuild(
                    id=build_id,
                    recipe_revision_id=plan.recipe_revision_id,
                    builder_node_id=plan.builder_node_id,
                    source_bundle_sha256=plan.source_bundle_sha256,
                    build_input_sha256=build_input_sha256,
                    state="succeeded",
                    policy_report={},
                    plan={},
                    image_digest=image_digest,
                    oci_layout_sha256=self._digest(("oci", build_id)),
                    image_bytes=1024,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        return self._operation("build", build_id)

    def preview_image_distribution(self, build_id, mapping_id, *, mapping_generation):
        self.events.append("distribute-image")
        return SimpleNamespace(
            recipe_build_id=build_id,
            mapping_id=mapping_id,
            mapping_generation=mapping_generation,
            image_digest="sha256:" + self._digest(("image", build_id)),
            node_ids=(),
            plan_digest=self._digest(("distribution", build_id, mapping_id)),
        )

    def preview_install(self, mapping_id, build_id):
        with self.sessions() as session:
            mapping = session.get(ClusterMapping, mapping_id)
            build = session.get(RecipeBuild, build_id)
            assert mapping is not None and build is not None
            nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode).where(
                        ClusterMappingNode.mapping_id == mapping_id
                    ).order_by(ClusterMappingNode.rank)
                )
            )
        return SimpleNamespace(
            mapping_id=mapping_id,
            mapping_generation=mapping.generation,
            recipe_build_id=build_id,
            recipe_revision_id=build.recipe_revision_id,
            recipe_content_sha256="a" * 64,
            image_digest=build.image_digest,
            plan_digest=self._digest(("install", mapping_id, build_id)),
            nodes=nodes,
        )

    def install(self, plan, *, plan_digest, actor, request_id):
        installation_id = self._id()
        with self.sessions.begin() as session:
            session.add(
                RecipeInstallation(
                    id=installation_id,
                    recipe_revision_id=plan.recipe_revision_id,
                    mapping_id=plan.mapping_id,
                    mapping_generation=plan.mapping_generation,
                    recipe_build_id=plan.recipe_build_id,
                    image_digest=plan.image_digest,
                    plan_digest=plan_digest,
                    plan={},
                    state="installed",
                    actor=actor,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.add_all(
                InstallationNode(
                    id=self._id(),
                    installation_id=installation_id,
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    state="installed",
                    required_bytes=1,
                    installed_bytes=1,
                    updated_at=NOW,
                )
                for node in plan.nodes
            )
        return self._operation("install", installation_id)

    def preview_run(self, installation_id, alias):
        with self.sessions() as session:
            installation = session.get(RecipeInstallation, installation_id)
            assert installation is not None
        return SimpleNamespace(
            installation_id=installation_id,
            alias=alias,
            mapping_id=installation.mapping_id,
            mapping_generation=installation.mapping_generation,
            plan_digest=self._digest(("run", installation_id, alias)),
        )

    def start(self, plan, *, plan_digest, actor, request_id):
        run_id = self._id()
        with self.sessions.begin() as session:
            installation = session.get(RecipeInstallation, plan.installation_id)
            assert installation is not None
            nodes = tuple(
                session.scalars(
                    select(InstallationNode).where(
                        InstallationNode.installation_id == plan.installation_id
                    ).order_by(InstallationNode.rank)
                )
            )
            session.add(
                RecipeRun(
                    id=run_id,
                    installation_id=plan.installation_id,
                    mapping_id=installation.mapping_id,
                    mapping_generation=installation.mapping_generation,
                    alias=plan.alias,
                    plan_digest=plan_digest,
                    plan={},
                    state="running",
                    route_state="published",
                    route_generation=1,
                    route_digest=self._digest(("route", run_id)),
                    actor=actor,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.add_all(
                RunNode(
                    id=self._id(),
                    run_id=run_id,
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    state="running",
                    port=8000 + node.rank,
                    reserved_memory_bytes=1,
                    updated_at=NOW,
                )
                for node in nodes
            )
        return self._operation("start", run_id)

    def preview_stop(self, run_id):
        return SimpleNamespace(plan_digest=self._digest(("stop", run_id)))

    def stop(self, run_id, *, plan_digest, actor, request_id):
        with self.sessions.begin() as session:
            run = session.get(RecipeRun, run_id)
            assert run is not None
            run.state = "stopped"
            run.route_state = "withdrawn"
            run.stopped_at = NOW
            run.updated_at = NOW
            for node in session.scalars(
                select(RunNode).where(RunNode.run_id == run_id)
            ):
                node.state = "stopped"
                node.updated_at = NOW
        return self._operation("stop", run_id)



def _seed_dual_solo_without_runtime_state(
    sessions: sessionmaker[Session],
) -> tuple[str, str]:
    _recipe_id, dual_revision_id = _seed(sessions)
    revisions = CatalogDocumentRevision.__table__
    assert isinstance(revisions, Table)
    dual_document = _recipe_document()
    dual_topology = _nested_object(dual_document, "topology")
    role_template = dict(_nested_object(dual_document, "topology", "roles", 0))
    dual_topology.update(
        {
            "name": "pair",
            "mode": "distributed",
            "node_count": 2,
            "roles": [
                {"name": "entrypoint", "count": 1, "endpoint_owner": True},
                {"name": "worker", "count": 1, "endpoint_owner": False},
            ],
            "parallelism": {
                "world_size": 2,
                "tensor": 2,
                "pipeline": 1,
                "data": 1,
                "backend": "nccl",
            },
            "fabric": {"connectivity": "connected", "minimum_bandwidth_mbps": 1},
            "start_order": ["worker", "entrypoint"],
            "stop_order": ["entrypoint", "worker"],
        }
    )
    dual_topology["roles"] = [
        {**role_template, "name": "entrypoint", "endpoint_owner": True},
        {**role_template, "name": "worker", "endpoint_owner": False},
    ]
    _nested_object(dual_document, "models", 0, "files", 0)["roles"] = [
        "entrypoint",
        "worker",
    ]
    RecipeDefinition.model_validate(dual_document)
    solo_revision_id = _uuid(5)
    solo_document = _recipe_document()
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )
        session.execute(
            revisions.update()
            .where(CatalogDocumentRevision.id == dual_revision_id)
            .values(
                document=dual_document,
                content_digest=content_sha256(RecipeDefinition.model_validate(dual_document)),
            )
        )
        _nested_object(solo_document, "identity")["slug"] = "synthetic-tiny-solo"
        _nested_object(solo_document, "metadata")["title"] = "Synthetic Tiny Solo"
        solo = RecipeDefinition.model_validate(solo_document)
        solo_root_id = _uuid(4)
        session.add(
            CatalogDocument(
                id=solo_root_id,
                kind="recipe",
                publisher=solo.identity.publisher,
                slug=solo.identity.slug,
                title=solo.metadata.title,
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            CatalogDocumentRevision(
                id=solo_revision_id,
                document_id=solo_root_id,
                kind="recipe",
                publisher=solo.identity.publisher,
                slug=solo.identity.slug,
                revision_number=1,
                state="active",
                schema_version=2,
                document=solo_document,
                content_digest=content_sha256(solo),
                execution_key="c" * 64,
                created_by="admin",
                created_at=NOW,
            )
        )
    return dual_revision_id, solo_revision_id


def _switch_profile(
    service: FleetProfileService, profile_id: str, request_key: str
) -> FleetProfileApplicationView:
    preview = service.preview(profile_id)
    assert preview.allowed
    application = service.apply(
        profile_id,
        plan_digest=preview.plan_digest,
        request_key=request_key,
        actor="admin",
    )
    for _ in range(128):
        if service.application(application.id).state == "succeeded":
            return service.application(application.id)
        assert service.tick() is True
    raise AssertionError("profile application did not complete")


def test_profile_apply_switches_dual_solo_idle_and_reuses_cached_installation() -> None:
    sessions = _database()
    dual_revision_id, solo_revision_id = _seed_dual_solo_without_runtime_state(sessions)
    operations = _ProfileLifecycleSimulator(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, recipe_operations=operations
    )
    profile_a = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Dual applied",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-image",
                        "spark_ids": [_node_id(1), _node_id(2)],
                        "desired_state": "running",
                        "assignment_name": "dual-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    profile_b = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Solo applied",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-solo",
                        "spark_ids": [_node_id(1)],
                        "desired_state": "running",
                        "assignment_name": "solo-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )

    _switch_profile(service, profile_a.id, _uuid(200))
    with sessions() as session:
        dual_installation = session.scalar(
            select(RecipeInstallation).where(
                RecipeInstallation.recipe_revision_id == dual_revision_id
            )
        )
        dual_build = session.scalar(
            select(RecipeBuild).where(
                RecipeBuild.recipe_revision_id == dual_revision_id
            )
        )
        assert dual_installation is not None
        dual_run = session.scalar(
            select(RecipeRun).where(RecipeRun.installation_id == dual_installation.id)
        )
        assert dual_build is not None
        assert dual_run is not None and dual_run.state == "running"
        dual_installation_id = dual_installation.id
        dual_build_id = dual_build.id
        dual_run_id = dual_run.id
    assert operations.events == [
        "create-placement",
        "build",
        "distribute-image",
        "install",
        "start",
    ]

    operations.events.clear()
    preview_b = service.preview(profile_b.id)
    kinds_b = [step.kind for step in preview_b.steps]
    assert kinds_b.index("create-placement") < kinds_b.index("stop")
    assert kinds_b.index("install") < kinds_b.index("stop") < kinds_b.index("start")
    assert any(
        reason.code == "profile.interruption_expected"
        for reason in preview_b.reasons
    )
    _switch_profile(service, profile_b.id, _uuid(201))

    with sessions() as session:
        dual_run = session.get(RecipeRun, dual_run_id)
        assert dual_run is not None and dual_run.state == "stopped"
        dual_nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == dual_run_id)
                .order_by(RunNode.rank)
            )
        )
        assert {node.node_id for node in dual_nodes} == {_node_id(1), _node_id(2)}
        assert {node.state for node in dual_nodes} == {"stopped"}
        solo_installation = session.scalar(
            select(RecipeInstallation).where(
                RecipeInstallation.recipe_revision_id == solo_revision_id
            )
        )
        assert solo_installation is not None
        solo_run = session.scalar(
            select(RecipeRun).where(RecipeRun.installation_id == solo_installation.id)
        )
        assert solo_run is not None and solo_run.state == "running"
        solo_nodes = tuple(
            session.scalars(
                select(RunNode).where(RunNode.run_id == solo_run.id)
            )
        )
        assert [node.node_id for node in solo_nodes] == [_node_id(1)]
        assert session.scalar(
            select(RunNode.id).where(
                RunNode.run_id == solo_run.id, RunNode.node_id == _node_id(2)
            )
        ) is None
        dual_installation = session.get(RecipeInstallation, dual_installation_id)
        assert dual_installation is not None
        assert dual_installation.state == "installed"
        dual_build = session.get(RecipeBuild, dual_build_id)
        assert dual_build is not None
        assert dual_build.state == "succeeded"
    assert operations.events == [
        "create-placement",
        "build",
        "distribute-image",
        "install",
        "stop",
        "start",
    ]

    operations.events.clear()
    preview_a_again = service.preview(profile_a.id)
    assert [step.kind for step in preview_a_again.steps] == ["stop", "start"]
    _switch_profile(service, profile_a.id, _uuid(202))
    with sessions() as session:
        restored_installation = session.get(RecipeInstallation, dual_installation_id)
        assert restored_installation is not None
        assert restored_installation.state == "installed"
        restored_build = session.get(RecipeBuild, dual_build_id)
        assert restored_build is not None
        assert restored_build.state == "succeeded"
        restored = tuple(
            session.scalars(
                select(RecipeRun)
                .where(
                    RecipeRun.installation_id == dual_installation_id,
                    RecipeRun.state == "running",
                )
            )
        )
        assert len(restored) == 1
        assert {
            node.node_id
            for node in session.scalars(
                select(RunNode).where(RunNode.run_id == restored[0].id)
            )
        } == {_node_id(1), _node_id(2)}
    assert operations.events == ["stop", "start"]


def test_profile_operation_projection_uses_bound_scope_and_canonical_phase() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        preparation_provider=lambda _session, _assignment, node_ids: _exact_preparation(
            node_ids
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")
    preview = service.preview(profile.id)
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(40),
        actor="admin",
    )

    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        item = service._operation_item(row)

    assert item["id"] == application.id
    assert item["parent_id"] is None
    assert item["node_ids"] == [_node_id(1)]
    assert item["kind"] == "fleet-profile.apply"
    assert item["progress"] == {"phase": "prepare"}

def test_profile_switch_delegates_non_idle_assignment_and_surfaces_child_progress() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )
    adapter = _SwitchAdapter()
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=adapter
    )
    profile_input = _input(revision_id).model_copy(
        update={"scope": FleetProfileScope(node_ids=[_node_id(1), _node_id(2)])}
    )
    profile = service.create(profile_input, actor="admin")

    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]
    assert preview.assignments[0].actions == ["switch"]
    assert preview.summary.starts == 1

    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(42),
        actor="admin",
    )
    assert service.tick() is True
    progress = service.application(application.id).progress
    assert progress.child_progress is not None
    assert progress.child_progress.phase == "model-download"
    assert adapter.starts[0]["scope_node_ids"] == (_node_id(1), _node_id(2))

    observed_phases = [progress.child_progress.phase]
    for _ in range(8):
        assert service.tick() is True
        current = service.application(application.id)
        child_progress = current.progress.child_progress
        if child_progress is not None:
            observed_phases.append(child_progress.phase)
        if current.state == "succeeded":
            break

    completed = service.application(application.id)
    assert completed.state == "succeeded"
    assert completed.result is not None
    assert completed.result.changed is True
    assert completed.result.completed_steps == 1
    assert observed_phases[:5] == [
        "model-download",
        "container-download",
        "target-copy",
        "start",
        "final-verify",
    ]
    step_result = service.application(application.id).progress.step_results["0"]
    assert isinstance(step_result.result, FleetProfileVerificationResult)
    assert step_result.result.verified is True


def test_profile_switch_adapter_plans_disjoint_assignments_once_and_resumes() -> None:
    sessions = _database()
    _dual_revision_id, _solo_revision_id = _seed_dual_solo_without_runtime_state(sessions)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(3),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )
    adapter = _SwitchAdapter()
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=adapter
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Dual plus solo",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-image",
                        "spark_ids": [_node_id(1), _node_id(2)],
                        "desired_state": "running",
                        "assignment_name": "dual-chat",
                    },
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-solo",
                        "spark_ids": [_node_id(3)],
                        "desired_state": "running",
                        "assignment_name": "solo-chat",
                    },
                ],
            }
        ),
        actor="admin",
    )

    preview = service.preview(profile.id)
    assert [step.kind for step in preview.steps] == ["switch"]
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(43),
        actor="admin",
    )
    assert service.tick() is True
    running = service.application(application.id)
    replayed_child = adapter.start(
        application_id=application.id,
        assignments=service.get(profile.id).assignments,
        scope_node_ids=tuple(preview.scope.node_ids),
        actor="admin",
        request_id=adapter.starts[0]["request_id"],
    )
    assert replayed_child.id == running.current_operation_id
    assert len(adapter.starts) == 1

    resumed = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=adapter
    )
    for _ in range(8):
        if resumed.application(application.id).state == "succeeded":
            break
        assert resumed.tick() is True

    assert resumed.application(application.id).state == "succeeded"
    assert len(adapter.starts) == 1
    assert adapter.starts[0]["scope_node_ids"] == (
        _node_id(1),
        _node_id(2),
        _node_id(3),
    )
    assignment_scopes = dict(
        zip(
            adapter.starts[0]["assignment_ids"],
            adapter.starts[0]["assignment_scopes"],
            strict=True,
        )
    )
    assert set(assignment_scopes.values()) == {
        (_node_id(1), _node_id(2)),
        (_node_id(3),),
    }
    assert tuple(adapter.starts[0]["assignment_ids"]) == tuple(
        sorted(adapter.starts[0]["assignment_ids"])
    )


def test_composite_switch_owns_unlisted_scoped_runtime_conflict_once() -> None:
    sessions = _database()
    dual_revision_id, _solo_revision_id = _seed_dual_solo_without_runtime_state(sessions)
    with sessions.begin() as session:
        session.add(
            ClusterMapping(
                id=_uuid(600),
                recipe_revision_id=dual_revision_id,
                topology_name="pair",
                generation=1,
                node_count=2,
                state="ready",
                parameters={},
                placement_digest="a" * 64,
                endpoint_owner_node_id=_node_id(1),
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add_all(
            [
                ClusterMappingNode(
                    id=_uuid(601),
                    mapping_id=_uuid(600),
                    node_id=_node_id(1),
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    created_at=NOW,
                ),
                ClusterMappingNode(
                    id=_uuid(602),
                    mapping_id=_uuid(600),
                    node_id=_node_id(2),
                    rank=1,
                    role="worker",
                    endpoint_owner=False,
                    created_at=NOW,
                ),
            ]
        )
        session.add(
            RecipeBuild(
                id=_uuid(603),
                recipe_revision_id=dual_revision_id,
                builder_node_id=_node_id(1),
                source_bundle_sha256="b" * 64,
                build_input_sha256="c" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest="sha256:" + "d" * 64,
                oci_layout_sha256="e" * 64,
                image_bytes=1024,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            RecipeInstallation(
                id=_uuid(604),
                recipe_revision_id=dual_revision_id,
                mapping_id=_uuid(600),
                mapping_generation=1,
                recipe_build_id=_uuid(603),
                image_digest="sha256:" + "d" * 64,
                plan_digest="f" * 64,
                plan={},
                state="installed",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add_all(
            [
                InstallationNode(
                    id=_uuid(605),
                    installation_id=_uuid(604),
                    node_id=_node_id(1),
                    rank=0,
                    role="entrypoint",
                    state="installed",
                    required_bytes=1,
                    installed_bytes=1,
                    updated_at=NOW,
                ),
                InstallationNode(
                    id=_uuid(606),
                    installation_id=_uuid(604),
                    node_id=_node_id(2),
                    rank=1,
                    role="worker",
                    state="installed",
                    required_bytes=1,
                    installed_bytes=1,
                    updated_at=NOW,
                ),
            ]
        )
        session.add(
            RecipeRun(
                id=_uuid(607),
                installation_id=_uuid(604),
                mapping_id=_uuid(600),
                mapping_generation=1,
                alias="unlisted-dual",
                plan_digest="1" * 64,
                plan={},
                state="running",
                route_state="published",
                route_generation=1,
                route_digest="2" * 64,
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add_all(
            [
                RunNode(
                    id=_uuid(608),
                    run_id=_uuid(607),
                    node_id=_node_id(1),
                    rank=0,
                    role="entrypoint",
                    state="running",
                    port=8000,
                    reserved_memory_bytes=1,
                    updated_at=NOW,
                ),
                RunNode(
                    id=_uuid(609),
                    run_id=_uuid(607),
                    node_id=_node_id(2),
                    rank=1,
                    role="worker",
                    state="running",
                    port=8001,
                    reserved_memory_bytes=1,
                    updated_at=NOW,
                ),
            ]
        )

    adapter = _SwitchAdapter()
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=adapter
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Two solo assignments",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-solo",
                        "spark_ids": [_node_id(1)],
                        "desired_state": "running",
                        "assignment_name": "solo-one",
                    },
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-solo",
                        "spark_ids": [_node_id(2)],
                        "desired_state": "running",
                        "assignment_name": "solo-two",
                    },
                ],
            }
        ),
        actor="admin",
    )

    preview = service.preview(profile.id)

    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]
    assert preview.summary.stops == 0
    assert set(preview.steps[0].node_ids) == {_node_id(1), _node_id(2)}
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(610),
        actor="admin",
    )
    assert service.tick() is True
    assert len(adapter.starts) == 1
    assert set(adapter.starts[0]["assignment_scopes"]) == {
        (_node_id(1),),
        (_node_id(2),),
    }
    assert service.application(application.id).current_operation_id is not None


def test_all_idle_profile_has_explicit_scope_and_no_preparation() -> None:
    sessions = _database()
    _seed(sessions)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(
        FleetProfileInput(
            name="All idle",
            assignments=[],
        ),
        actor="admin",
    )

    preview = service.preview(profile.id)

    assert preview.allowed is True
    assert preview.scope.idle_node_ids == [_node_id(1), _node_id(2)]
    assert preview.steps == []
    assert preview.preparations == []

    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(801),
        actor="admin",
    )
    assert application.state == "succeeded"
    assert application.result is not None
    assert application.result.changed is False
    assert application.result.completed_steps == 0
    readback = service.application(application.id)
    assert readback.result is not None
    assert readback.result.model_dump(mode="json") == {
        "changed": False,
        "completed_steps": 0,
    }


def test_production_profile_adapter_binds_one_real_run_switch_child(
    tmp_path: Path,
) -> None:
    """The profile child delegates exact preparation and run admission to RunSwitch."""

    from vonk_control.run_switch_operations import RunSwitchOperationService

    from .test_recipe_operations import setup_services
    from .test_run_switch_operations import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
    service = FleetProfileService(
        sessions, clock=lifecycle._clock, switch_adapter=adapter
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Production child",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "production-child",
                    }
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(640),
        actor="admin",
    )

    assert service.tick() is True
    current = service.application(application.id)
    assert current.current_operation_id == application.id
    adapter_progress = current.progress.switch_adapter
    assert isinstance(adapter_progress, FleetProfileSwitchAdapterState)
    child_id = adapter_progress.active_operation_id
    assert isinstance(child_id, str)
    child = run_switch.get(child_id)
    assert child.kind == "recipe.run-switch.v2"
    assert child.node_ids == list(nodes)
    assert child.action == "switch"
    assert current.progress.child_progress is not None
    assert current.progress.child_progress.node_ids == list(nodes)
    assert current.progress.child_progress.phase == "runtime-install"
    with sessions() as session:
        stored = session.get(FleetProfileApplication, application.id)
        assert stored is not None
        persisted_progress = FleetProfileApplicationProgress.model_validate(
            stored.progress
        )
        assert isinstance(
            persisted_progress.switch_adapter, FleetProfileSwitchAdapterState
        )

    replay = adapter.start(
        application_id=application.id,
        assignments=tuple(service._application_assignments(application.id)),
        scope_node_ids=nodes,
        actor="admin",
        request_id=_uuid(641),
    )
    assert replay.id == application.id
    assert replay.progress is not None
    assert replay.progress.node_ids == list(nodes)

    restarted_run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    restarted_adapter = RunSwitchFleetProfileAdapter(sessions, restarted_run_switch)
    restarted_service = FleetProfileService(
        sessions,
        clock=lifecycle._clock,
        switch_adapter=restarted_adapter,
    )
    assert restarted_service.tick() is True
    resumed = restarted_service.application(application.id)
    assert resumed.current_operation_id == application.id
    assert resumed.progress.switch_adapter is not None
    assert resumed.progress.switch_adapter.active_operation_id == child_id


def _transfer_result(nodes: tuple[str, ...]) -> dict[str, object]:
    """Return one real Run/Switch result document carrying a transfer phase."""

    import datetime as _datetime

    from vonk_agent_protocol import DistributionAssignment, DistributionObject

    node_id = nodes[0]
    archive_sha256 = "1" * 64
    assignment = DistributionAssignment(
        schema_version=2,
        assignment_id=_uuid(648),
        plan_digest="c" * 64,
        generation=1,
        node_id=node_id,
        expires_at=_datetime.datetime(2026, 9, 12, tzinfo=UTC),
        model_artifact_set_sha256="d" * 64,
        objects=(
            DistributionObject(name="model", sha256="e" * 64, bytes=10, kind="model"),
            DistributionObject(
                name="runtime.tar",
                sha256=archive_sha256,
                bytes=1024,
                kind="oci-archive",
            ),
        ),
        oci_image_digest="sha256:" + "f" * 64,
        oci_archive_sha256=archive_sha256,
    )
    return {
        "phase_index": 3,
        "completed_phases": ["prepare", "transfer"],
        "phase_results": [
            {
                "phase": "transfer",
                "subphase": "target-copy",
                "cached_nodes": [],
                "assignments": {
                    node_id: assignment.model_dump(mode="json"),
                },
            }
        ],
    }


def test_completed_switch_child_keeps_its_run_switch_receipt(tmp_path: Path) -> None:
    """A finished profile step must persist the child's public result tree.

    The acceptance canary reads the Run/Switch receipt back out of the profile
    application's step results.  The adapter used to answer a completed child
    from its mirrored state, whose result is the adapter's own summary, so the
    step recorded no receipt and the run could not be qualified.  The receipt
    now travels with the completed child, so it survives the tick that finished
    it and any later restart.
    """

    from vonk_control.run_switch_operations import (
        RunSwitchOperationService,
        _persisted_result,
    )

    from .test_recipe_operations import setup_services
    from .test_run_switch_operations import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
    service = FleetProfileService(
        sessions, clock=lifecycle._clock, switch_adapter=adapter
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Completed switch child",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "completed-switch-child",
                    }
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(647),
        actor="admin",
    )
    assert service.tick() is True
    started = service.application(application.id)
    assert started.progress.switch_adapter is not None
    child_id = started.progress.switch_adapter.active_operation_id
    assert isinstance(child_id, str)

    # The child finishes with its public Run/Switch result tree, exactly as the
    # real service persists one.  A transfer phase result is part of it: its
    # distribution objects are contract tuples that a JSON round trip decodes
    # as arrays, which is where the receipt used to be lost on the way back.
    with sessions.begin() as session:
        job = session.get(Job, child_id)
        assert job is not None
        job.state = "succeeded"
        job.status_reason = None
        job.result = _persisted_result(_transfer_result(nodes))
        job.updated_at = lifecycle._clock()

    assert service.tick() is True
    completed = service.application(application.id)
    assert completed.state == "succeeded", completed.status_reason
    step_results = completed.progress.step_results
    receipts = [
        value.result
        for value in step_results.values()
        if isinstance(value.result, FleetProfileSwitchChildResult)
    ]
    assert len(receipts) == 1
    # The receipt names the plan step it completed, not the child's own
    # operation kind: a switch-adapter child used to record no kind at all.
    assert {value.kind for value in step_results.values()} == {"switch"}
    receipt = receipts[0]
    assert receipt.run_switch_operation_id == child_id
    assert receipt.run_switch.phase_index == 3
    completed_phase = receipt.run_switch.phase_results[0]
    assert (completed_phase.phase, completed_phase.subphase) == (
        "transfer",
        "target-copy",
    )

    # A restart reads the same receipt out of the persisted application.
    restarted = FleetProfileService(
        sessions, clock=lifecycle._clock, switch_adapter=adapter
    )
    persisted = restarted.application(application.id)
    assert persisted.progress.step_results == completed.progress.step_results
    assert any(
        isinstance(value.result, FleetProfileSwitchChildResult)
        for value in persisted.progress.step_results.values()
    )


def test_switch_adapter_joins_the_callers_row_transaction(tmp_path: Path) -> None:
    """Advancing a child must reuse the tick's transaction, not race its row lock.

    The worker's tick holds the application row and then reads the switch child.
    An adapter that opens its own transaction waits on that same row in
    PostgreSQL: the worker deadlocks against itself, the child never advances
    and the run stays parked. SQLite has no ``SELECT ... FOR UPDATE``, so the
    caller writes the row instead to hold the same writer lock; an adapter that
    opens its own writer then fails here with "database is locked".
    """

    from vonk_control.run_switch_operations import RunSwitchOperationService

    from .test_recipe_operations import setup_services
    from .test_run_switch_operations import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    engine = create_engine(
        f"sqlite:///{tmp_path / 'operations.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 0.25},
    )
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, engine=engine
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
    service = FleetProfileService(
        sessions, clock=lifecycle._clock, switch_adapter=adapter
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Joined transaction",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "joined-transaction",
                    }
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(645),
        actor="admin",
    )
    assert service.tick() is True
    started = service.application(application.id)
    assert started.progress.switch_adapter is not None
    child_id = started.progress.switch_adapter.active_operation_id
    assert isinstance(child_id, str)

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id, with_for_update=True)
        assert row is not None
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == application.id)
            .values(status_reason="held by the profile tick")
        )

        child = adapter.get(application.id, session=session)

        assert child.state in {"queued", "running"}
        assert (
            session.scalar(
                select(FleetProfileApplication.status_reason).where(
                    FleetProfileApplication.id == application.id
                )
            )
            == "held by the profile tick"
        )
        stored = session.get(FleetProfileApplication, application.id)
        assert stored is not None
        progress = FleetProfileApplicationProgress.model_validate(stored.progress)
        assert isinstance(progress.switch_adapter, FleetProfileSwitchAdapterState)
        assert progress.switch_adapter.active_operation_id == child_id

    committed = service.application(application.id).progress.switch_adapter
    assert committed is not None
    assert committed.active_operation_id == child_id


def test_profile_tick_advances_a_switch_child_on_postgres(
    tmp_path: Path, postgres_engine: Engine
) -> None:
    """PostgreSQL takes a real row lock, so the tick must reuse its own session.

    ``SELECT ... FOR UPDATE`` blocks another writer of the same row for real
    here, which is what stalled the deployed worker: the adapter opened a second
    transaction for the application row the tick already held. With a short
    ``lock_timeout`` the wrong implementation surfaces as
    ``canceling statement due to lock timeout`` instead of a worker that stops
    advancing forever.
    """

    from vonk_control.run_switch_operations import RunSwitchOperationService

    from .test_recipe_operations import setup_services
    from .test_run_switch_operations import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    engine = create_engine(
        postgres_engine.url.render_as_string(hide_password=False),
        connect_args={"options": "-c lock_timeout=3000"},
    )
    try:
        sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
            tmp_path, engine=engine
        )
        with sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
        assert revision is not None
        run_switch = RunSwitchOperationService(
            sessions,
            lifecycle=lifecycle,
            clock=lifecycle._clock,
            artifacts=CompleteArtifactInspector(),
            artifact_phase_executor=RecordingArtifactExecutor(),
            memory_floor_bytes=50,
        )
        adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
        service = FleetProfileService(
            sessions, clock=lifecycle._clock, switch_adapter=adapter
        )
        profile = service.create(
            FleetProfileInput.model_validate(
                {
                    "name": "Postgres transaction",
                    "assignments": [
                        {
                            "recipe_selector": f"vonk-forge/{revision.slug}",
                            "spark_ids": list(nodes),
                            "desired_state": "running",
                            "assignment_name": "postgres-transaction",
                        }
                    ],
                }
            ),
            actor="admin",
        )
        preview = service.preview(profile.id)
        assert preview.allowed is True
        application = service.apply(
            profile.id,
            plan_digest=preview.plan_digest,
            request_key=_uuid(646),
            actor="admin",
        )

        assert service.tick() is True
        started = service.application(application.id)
        assert started.current_operation_id == application.id
        assert started.progress.switch_adapter is not None
        child_id = started.progress.switch_adapter.active_operation_id
        assert isinstance(child_id, str)
        # The next pass reads the child again while it holds the same row.
        assert service.tick() is True
        resumed = service.application(application.id)
        assert resumed.progress.switch_adapter is not None
        assert resumed.progress.switch_adapter.active_operation_id == child_id
    finally:
        engine.dispose()


def test_production_profile_adapter_routes_all_idle_to_one_complete_stop_child(
    tmp_path: Path,
) -> None:
    """An empty desired set still stops a complete in-scope run through RunSwitch."""

    from vonk_control.run_switch_operations import RunSwitchOperationService

    from .test_recipe_operations import (
        installed_recipe,
        setup_services,
        started_recipe,
    )
    from .test_run_switch_operations import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=_uuid(642),
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installation.owner_id,
        nodes,
        request_id=_uuid(643),
    )
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
    service = FleetProfileService(
        sessions, clock=lifecycle._clock, switch_adapter=adapter
    )
    profile = service.create(
        FleetProfileInput(
            name="All idle through RunSwitch",
            assignments=[],
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]

    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(644),
        actor="admin",
    )
    assert service.tick() is True
    current = service.application(application.id)
    assert current.current_operation_id == application.id
    state = current.progress.switch_adapter
    assert state is not None
    child_id = state.active_operation_id
    assert isinstance(child_id, str)
    child = run_switch.get(child_id)
    assert child.kind == "recipe.stop.v2"
    assert child.action == "stop"
    assert child.node_ids == list(nodes)
    with sessions() as session:
        stored_run = session.get(RecipeRun, run.owner_id)
        assert stored_run is not None and stored_run.state == "running"
        assert (
            session.scalar(select(RecipeRun.id).where(RecipeRun.id != run.owner_id))
            is None
        )


def test_profile_preparations_are_stably_ordered_and_reuse_identity() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )
        session.add(
            RecipeBuild(
                id=_uuid(60),
                recipe_revision_id=revision_id,
                builder_node_id=_node_id(1),
                source_bundle_sha256="b" * 64,
                build_input_sha256="c" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest="sha256:" + "d" * 64,
                oci_layout_sha256="e" * 64,
                image_bytes=1024,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    assignments = [
        FleetProfileAssignmentInput(
            recipe_selector="vonk-forge/synthetic-tiny-image",
            spark_ids=[_node_id(2)],
            desired_state="installed",
        ),
        FleetProfileAssignmentInput(
            recipe_selector="vonk-forge/synthetic-tiny-image",
            spark_ids=[_node_id(1)],
            desired_state="installed",
        ),
    ]
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        preparation_provider=lambda _session, _assignment, node_ids: _exact_preparation(
            node_ids
        ),
    )
    profile = service.create(
        FleetProfileInput(
            name="Two cached copies",
            assignments=assignments,
        ),
        actor="admin",
    )

    preview = service.preview(profile.id)

    next_observation = NOW

    def changing_observation_provider(
        _session, _assignment, node_ids
    ) -> RolloutPreparation:
        nonlocal next_observation
        result = _exact_preparation(node_ids, observed_at=next_observation)
        next_observation += timedelta(seconds=1)
        return result

    digest_service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        preparation_provider=changing_observation_provider,
    )
    assert digest_service.preview(profile.id).plan_digest == digest_service.preview(
        profile.id
    ).plan_digest

    assert [item.assignment_id for item in preview.preparations] == sorted(
        item.assignment_id for item in preview.preparations
    )
    assert len(preview.preparations) == 2
    assert {
        item.preparation.model.artifact_set_sha256
        for item in preview.preparations
    } == {preview.preparations[0].preparation.model.artifact_set_sha256}
    assert {
        item.preparation.runtime_image.image_digest
        for item in preview.preparations
    } == {"sha256:" + "d" * 64}
    assert {
        item.preparation.model.artifact_set_bytes
        for item in preview.preparations
    } == {100}


def test_profile_rejects_preparation_evidence_for_another_scope() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        preparation_provider=lambda _session, _assignment, _node_ids: _exact_preparation(
            (_node_id(2),)
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")

    preview = service.preview(profile.id)

    assert preview.allowed is False
    assert preview.preparations == []
    assert any(
        reason.code == "profile.preparation_scope_mismatch"
        and reason.severity == "error"
        for reason in preview.reasons
    )


def test_profile_contract_rejects_ambiguous_or_incomplete_assignments() -> None:
    value = _input(_uuid(2)).model_dump(mode="json")
    value["assignments"][0]["spark_ids"] = [_node_id(1), _node_id(1)]
    with pytest.raises(ValidationError, match="unique"):
        FleetProfileInput.model_validate(value)

    value = _input(_uuid(2)).model_dump(mode="json")
    value["assignments"][0]["spark_ids"] = []
    with pytest.raises(ValidationError, match="at least 1"):
        FleetProfileInput.model_validate(value)


def test_profile_create_is_server_owned_validated_and_digest_stable() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)

    created = service.create(_input(revision_id), actor="admin")
    loaded = service.get(created.id)
    listed = service.list()

    assert created == loaded
    assert listed.profiles == [created]
    assert created.name == "Studio ready"
    assert created.assignments[0].recipe["name"] == "Synthetic Tiny image"
    assert created.assignments[0].spark_ids == [_node_id(1)]
    assert len(created.profile_digest) == 64
    assert created.profile_digest == loaded.profile_digest


def test_profile_validation_rejects_unknown_sparks_and_recipe_topology_drift() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    value = _input(revision_id).model_dump(mode="json")
    value["assignments"][0]["spark_ids"] = [_node_id(9)]
    profile = service.create(FleetProfileInput.model_validate(value), actor="admin")
    preview = service.preview(profile.id)
    assert not preview.allowed
    assert any(reason.code == "profile.spark_unavailable" for reason in preview.reasons)

    value = _input(revision_id).model_dump(mode="json")
    value["assignments"][0]["spark_ids"] = [_node_id(1), _node_id(2)]
    value["name"] = "Topology drift"
    profile = service.create(FleetProfileInput.model_validate(value), actor="admin")
    preview = service.preview(profile.id)
    assert not preview.allowed
    assert any(reason.code == "profile.topology_incomplete" for reason in preview.reasons)


def test_profile_validation_rejects_rank_order_that_mapping_would_rewrite() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    revisions = CatalogDocumentRevision.__table__
    assert isinstance(revisions, Table)
    document = _recipe_document()
    topology = _nested_object(document, "topology")
    leader = deepcopy(_nested_object(document, "topology", "roles", 0))
    leader.update({"name": "leader", "count": 1, "endpoint_owner": True})
    worker = deepcopy(leader)
    worker.update({"name": "worker", "endpoint_owner": False})
    topology.update(
        {
            "name": "pair",
            "mode": "distributed",
            "node_count": 2,
            "roles": [leader, worker],
            "parallelism": {
                "world_size": 2,
                "tensor": 2,
                "pipeline": 1,
                "data": 1,
                "backend": "nccl",
            },
            "fabric": {"connectivity": "connected", "minimum_bandwidth_mbps": 1},
            "start_order": ["worker", "leader"],
            "stop_order": ["leader", "worker"],
        }
    )
    _nested_object(document, "models", 0, "files", 0)["roles"] = ["leader", "worker"]
    parsed_document = RecipeDefinition.model_validate(document)
    with sessions.begin() as session:
        session.execute(
            revisions.update()
            .where(CatalogDocumentRevision.id == revision_id)
            .values(
                document=document,
                content_digest=content_sha256(parsed_document),
            )
        )
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )

    value = _input(revision_id).model_dump(mode="json")
    value["assignments"][0]["spark_ids"] = [_node_id(2), _node_id(1)]
    with pytest.raises(ValidationError, match="sorted"):
        FleetProfileInput.model_validate(value)


def test_profile_preview_explains_prerequisites_then_builds_one_atomic_plan() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(_input(revision_id), actor="admin")

    build_plan = service.preview(profile.id)
    assert build_plan.allowed is True
    assert build_plan.summary.blockers == 0
    assert build_plan.summary.builds == 1
    assert build_plan.assignments[0].current_state == "not-placed"
    assert build_plan.assignments[0].actions == [
        "create-placement",
        "build",
        "distribute-image",
        "install",
        "start",
    ]

    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=_uuid(3),
                recipe_revision_id=revision_id,
                builder_node_id=_node_id(1),
                source_bundle_sha256="a" * 64,
                build_input_sha256="b" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=f"sha256:{'c' * 64}",
                oci_layout_sha256="d" * 64,
                image_bytes=1024,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert preview.summary.model_dump() == {
        "already_correct": 0,
        "placements": 1,
        "builds": 0,
        "distributions": 1,
        "installs": 1,
        "starts": 1,
        "stops": 0,
        "uninstalls": 0,
        "blockers": 0,
    }
    assert [step.kind for step in preview.steps] == [
        "create-placement",
        "distribute-image",
        "install",
        "start",
    ]
    assert len(preview.plan_digest) == 64

    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(4),
        actor="admin",
    )
    replay = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(4),
        actor="admin",
    )
    assert application == replay
    assert application.state == "queued"
    assert application.total_steps == 4
    assert application.profile_digest == preview.profile_digest
    with sessions() as session:
        stored = session.get(FleetProfileApplication, application.id)
        assert stored is not None
        stored_plan = stored.plan
        assert stored_plan["scope"] == {
            "node_ids": [_node_id(1)],
            "idle_node_ids": [],
        }


def test_profile_apply_rejects_a_stale_preview_and_request_key_reuse() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(_input(revision_id), actor="admin")

    with pytest.raises(FleetProfileConflict, match="stale"):
        service.apply(
            profile.id, plan_digest="f" * 64, request_key=_uuid(5), actor="admin"
        )

    updated = service.update(
        profile.id, _input(revision_id, name="Studio exact"), actor="admin"
    )
    assert updated.profile_digest != profile.profile_digest


def test_profile_application_resumes_from_persisted_step_context() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=_uuid(6),
                recipe_revision_id=revision_id,
                builder_node_id=_node_id(1),
                source_bundle_sha256="a" * 64,
                build_input_sha256="b" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=f"sha256:{'c' * 64}",
                oci_layout_sha256="d" * 64,
                image_bytes=1024,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(_input(revision_id), actor="admin")
    preview = service.preview(profile.id)
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(7),
        actor="admin",
    )

    class Operations(RecipeOperationService):
        def __init__(self) -> None:
            pass

        def preview_mapping(self, *_args, **_kwargs):
            return SimpleNamespace(generation=3)

        def create_mapping(self, *_args, **_kwargs):
            return _uuid(8)

        def preview_image_distribution(self, *_args, **_kwargs):
            return SimpleNamespace(node_ids=())

    restarted = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        recipe_operations=Operations(),
    )

    assert restarted.tick() is True
    after_mapping = restarted.application(application.id)
    assert after_mapping.state == "running"
    assert after_mapping.current_step == 1

    assert restarted.tick() is True
    after_distribution = restarted.application(application.id)
    assert after_distribution.current_step == 2
    assert after_distribution.current_operation_id is None


def test_profile_scope_reconciles_idle_member_and_retains_reusable_installation() -> None:
    sessions = _database()
    _recipe_id, dual_revision_id = _seed(sessions)
    revisions = CatalogDocumentRevision.__table__
    assert isinstance(revisions, Table)
    dual_document = _recipe_document()
    dual_topology = _nested_object(dual_document, "topology")
    role_template = dict(_nested_object(dual_document, "topology", "roles", 0))
    dual_topology.update(
        {
            "name": "pair",
            "mode": "distributed",
            "node_count": 2,
            "roles": [
                {"name": "entrypoint", "count": 1, "endpoint_owner": True},
                {"name": "worker", "count": 1, "endpoint_owner": False},
            ],
            "parallelism": {
                "world_size": 2,
                "tensor": 2,
                "pipeline": 1,
                "data": 1,
                "backend": "nccl",
            },
            "fabric": {"connectivity": "connected", "minimum_bandwidth_mbps": 1},
            "start_order": ["worker", "entrypoint"],
            "stop_order": ["entrypoint", "worker"],
        }
    )
    dual_topology["roles"] = [
        {**role_template, "name": "entrypoint", "endpoint_owner": True},
        {**role_template, "name": "worker", "endpoint_owner": False},
    ]
    _nested_object(dual_document, "models", 0, "files", 0)["roles"] = [
        "entrypoint",
        "worker",
    ]
    dual_recipe = RecipeDefinition.model_validate(dual_document)
    solo_revision_id = _uuid(5)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )
        session.execute(
            revisions.update()
            .where(CatalogDocumentRevision.id == dual_revision_id)
            .values(
                document=dual_document,
                content_digest=content_sha256(dual_recipe),
            )
        )
        solo_document = _recipe_document()
        _nested_object(solo_document, "identity")["slug"] = "synthetic-tiny-solo"
        _nested_object(solo_document, "metadata")["title"] = "Synthetic Tiny Solo"
        solo_recipe = RecipeDefinition.model_validate(solo_document)
        solo_root_id = _uuid(4)
        session.add(
            CatalogDocument(
                id=solo_root_id,
                kind="recipe",
                publisher=solo_recipe.identity.publisher,
                slug=solo_recipe.identity.slug,
                title=solo_recipe.metadata.title,
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            CatalogDocumentRevision(
                id=solo_revision_id,
                document_id=solo_root_id,
                kind="recipe",
                publisher=solo_recipe.identity.publisher,
                slug=solo_recipe.identity.slug,
                revision_number=1,
                state="active",
                schema_version=2,
                document=solo_document,
                content_digest=content_sha256(solo_recipe),
                execution_key="c" * 64,
                created_by="admin",
                created_at=NOW,
            )
        )
        session.add(
            ClusterMapping(
                id=_uuid(10),
                recipe_revision_id=dual_revision_id,
                topology_name="pair",
                generation=1,
                node_count=2,
                state="ready",
                parameters={},
                placement_digest="a" * 64,
                endpoint_owner_node_id=_node_id(1),
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add_all(
            [
                ClusterMappingNode(
                    id=_uuid(11), mapping_id=_uuid(10), node_id=_node_id(1), rank=0,
                    role="entrypoint", endpoint_owner=True, created_at=NOW,
                ),
                ClusterMappingNode(
                    id=_uuid(12), mapping_id=_uuid(10), node_id=_node_id(2), rank=1,
                    role="worker", endpoint_owner=False, created_at=NOW,
                ),
            ]
        )
        session.add(
            RecipeBuild(
                id=_uuid(13), recipe_revision_id=dual_revision_id,
                builder_node_id=_node_id(1), source_bundle_sha256="b" * 64,
                build_input_sha256="c" * 64, state="succeeded", policy_report={}, plan={},
                image_digest=f"sha256:{'d' * 64}", oci_layout_sha256="e" * 64,
                image_bytes=1024, created_at=NOW, updated_at=NOW,
            )
        )
        session.add(
            RecipeInstallation(
                id=_uuid(14), recipe_revision_id=dual_revision_id, mapping_id=_uuid(10),
                mapping_generation=1, recipe_build_id=_uuid(13),
                image_digest=f"sha256:{'d' * 64}", plan_digest="f" * 64,
                plan={}, state="installed", actor="admin", created_at=NOW, updated_at=NOW,
            )
        )
        session.add_all(
            [
                InstallationNode(
                    id=_uuid(15), installation_id=_uuid(14), node_id=_node_id(1), rank=0,
                    role="entrypoint", state="installed", required_bytes=1,
                    installed_bytes=1, updated_at=NOW,
                ),
                InstallationNode(
                    id=_uuid(16), installation_id=_uuid(14), node_id=_node_id(2), rank=1,
                    role="worker", state="installed", required_bytes=1,
                    installed_bytes=1, updated_at=NOW,
                ),
            ]
        )
        session.add(
            RecipeRun(
                id=_uuid(17), installation_id=_uuid(14), mapping_id=_uuid(10),
                mapping_generation=1, alias="dual-chat", plan_digest="1" * 64, plan={},
                state="running", route_state="published", route_generation=1,
                route_digest="2" * 64, actor="admin", created_at=NOW, updated_at=NOW,
            )
        )
        session.add_all(
            [
                RunNode(
                    id=_uuid(18), run_id=_uuid(17), node_id=_node_id(1), rank=0,
                    role="entrypoint", state="running", port=8000,
                    reserved_memory_bytes=1, updated_at=NOW,
                ),
                RunNode(
                    id=_uuid(19), run_id=_uuid(17), node_id=_node_id(2), rank=1,
                    role="worker", state="running", port=8001,
                    reserved_memory_bytes=1, updated_at=NOW,
                ),
            ]
        )

    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile_a = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Dual",
                "assignments": [{
                    "recipe_selector": "vonk-forge/synthetic-tiny-image",
                    "spark_ids": [_node_id(1), _node_id(2)],
                    "desired_state": "running",
                    "assignment_name": "dual-chat",
                }],
            }
        ),
        actor="admin",
    )
    profile_b = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Solo and idle",
                "assignments": [{
                    "recipe_selector": "vonk-forge/synthetic-tiny-solo",
                    "spark_ids": [_node_id(1)],
                    "desired_state": "running",
                    "assignment_name": "solo-chat",
                }],
            }
        ),
        actor="admin",
    )
    assert service.preview(profile_a.id).steps == []
    switch_to_b = service.preview(profile_b.id)
    assert switch_to_b.scope.node_ids == [_node_id(1), _node_id(2)]
    assert switch_to_b.scope.idle_node_ids == [_node_id(2)]
    assert [step.kind for step in switch_to_b.steps].count("stop") == 1
    assert switch_to_b.scope.idle_node_ids == [_node_id(2)]
    assert switch_to_b.summary.uninstalls == 0

    with sessions.begin() as session:
        run = session.get(RecipeRun, _uuid(17))
        assert run is not None
        run.state = "stopped"
        run.route_state = "withdrawn"
    back_to_a = service.preview(profile_a.id)
    assert [step.kind for step in back_to_a.steps] == ["start"]
    assert back_to_a.summary.installs == 0


@pytest.mark.parametrize("damage", ["numeric-variant", "missing-spark-ids", "extra", "invalid-root"])
def test_profile_round_trip_rejects_corrupt_stored_assignment(damage):
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = service.create(_input(revision_id), actor="admin")
    assert service.get(created.id) == created
    with sessions.begin() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        assignments = deepcopy(row.assignments)
        if damage == "invalid-root":
            assignments = {}
        elif damage == "numeric-variant":
            assignments[0]["model_variant"] = 123
        elif damage == "missing-spark-ids":
            del assignments[0]["spark_ids"]
        else:
            assignments[0]["undeclared"] = None
        # The stored document can be any JSON value, so persist the damaged
        # shape directly instead of through the typed ORM attribute.
        session.execute(
            update(FleetProfile)
            .where(FleetProfile.id == created.id)
            .values(assignments=assignments)
        )
    with pytest.raises(
        FleetProfileConflict, match="persisted Fleet profile choices are invalid"
    ):
        service.get(created.id)


def test_profile_preview_blocks_when_required_preparation_cannot_be_attested() -> None:
    """A plan that must prepare assets is blocked when they cannot be attested.

    ``AGENTS.md`` requires missing model or recipe-image assets to be actionable
    blockers, so a profile that still has to place its exact assets must not be
    admitted while the Controller cannot project that evidence.
    """

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)

    def _unavailable(_session, _assignment, _node_ids):
        raise ValueError(
            "The Run/Switch authority cannot attest exact model and OCI "
            "preparation evidence (run-switch.install-preparation-unavailable)."
        )

    service = FleetProfileService(
        sessions, clock=lambda: NOW, preparation_provider=_unavailable
    )
    profile = service.create(_input(revision_id), actor="admin")

    preview = service.preview(profile.id)

    assert preview.allowed is False
    assert preview.preparations == []
    reason = next(
        reason
        for reason in preview.reasons
        if reason.code == "profile.preparation_unavailable"
    )
    assert reason.severity == "error"
    assert "cannot attest" in reason.detail
    with pytest.raises(FleetProfileConflict, match="preview is blocked"):
        service.apply(
            profile.id,
            plan_digest=preview.plan_digest,
            request_key=_uuid(41),
            actor="admin",
        )


def test_profile_preview_projects_exact_preparation_from_run_switch_authority(
    tmp_path: Path,
) -> None:
    """Regression: the production composition must project real preparation.

    Production binds ``RunSwitchFleetProfileAdapter.preparation`` as the
    service's preparation provider.  When that binding is missing the preview
    reported ``allowed`` with an empty ``preparations`` list, which is the
    defect the Spark acceptance canary surfaced.
    """

    from vonk_control.run_switch_operations import RunSwitchOperationService

    from .test_recipe_operations import setup_services
    from .test_run_switch_operations import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    service = build_production_fleet_profile_service(
        sessions,
        clock=lifecycle._clock,
        run_switch_operations=run_switch,
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Preview preparation",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "preview-preparation",
                    }
                ],
            }
        ),
        actor="admin",
    )

    preview = service.preview(profile.id)

    assert preview.allowed is True
    assert len(preview.preparations) == 1
    preparation = preview.preparations[0].preparation
    assert tuple(preparation.target_node_ids) == tuple(sorted(nodes))
    assert preparation.model.artifact_set_sha256
    assert preparation.model.model_content_sha256
    assert preparation.runtime_image.image_digest.startswith("sha256:")
    assert preparation.runtime_image.oci_layout_sha256
    assert not any(
        reason.code == "profile.preparation_unavailable" for reason in preview.reasons
    )


def test_child_operation_state_distinguishes_absence_from_corruption() -> None:
    """A stored state that no longer validates must not become "running".

    The caller's default is only correct for a genuinely absent value; a
    present but malformed state used to be reported as the default, inventing
    an execution state.
    """

    assert _operation_state(None, default="running") == "running"
    assert _operation_state("succeeded", default="queued") == "succeeded"
    with pytest.raises(
        FleetProfileConflict, match="child operation state is invalid"
    ):
        _operation_state("not-a-state", default="running")
    with pytest.raises(
        FleetProfileConflict, match="child operation state is invalid"
    ):
        _operation_state(7, default="running")
