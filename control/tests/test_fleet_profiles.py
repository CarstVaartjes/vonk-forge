from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.cluster_mappings import ClusterMappingPlan
from vonk_control.fleet_profile_contract import (
    FleetProfileChildOperation,
    FleetProfileChildProgress,
    FleetProfileInput,
    FleetProfileScope,
)
from vonk_control.fleet_profiles import (
    FleetProfileConflict,
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
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
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.preparation_contract import RolloutPreparation
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


def _uuid(value: int) -> str:
    return f"00000000-0000-4000-8000-{value:012x}"


def _node_id(value: int) -> str:
    return "spk_" + f"{value:032x}"


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
                "model_version_sha256": "b" * 64,
                "recipe_revision_sha256": "c" * 64,
                "artifact_count": 1,
                "artifact_set_bytes": 100,
                "dependency_model_version_sha256": [],
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


class _SwitchAdapter:
    def __init__(self) -> None:
        self.starts: list[dict[str, object]] = []
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
                result={"verified": True},
            )
        ]
        return self._operations[operation_id][0]

    def get(self, operation_id: str) -> FleetProfileChildOperation:
        states = self._operations[operation_id]
        index = min(self._states[operation_id] + 1, len(states) - 1)
        self._states[operation_id] = index
        return states[index]


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
        session.add(
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
            "scope": {"node_ids": [_node_id(1)]},
            "assignments": [
                {
                    "recipe_revision_id": revision_id,
                    "topology_name": "solo",
                    "desired_state": "running",
                    "alias": "studio-chat",
                    "nodes": [
                        {
                            "node_id": _node_id(1),
                            "rank": 0,
                            "role": "entrypoint",
                            "endpoint_owner": True,
                        }
                    ],
                }
            ],
        }
    )


class _ProfileLifecycleSimulator:
    """Small operation boundary that materializes each accepted lifecycle effect."""

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

    def get(self, operation_id: str) -> SimpleNamespace:
        return self.operations[operation_id]

    def preview_mapping(self, revision_id, node_ids, *, parameters, actor):
        with self.sessions() as session:
            revision = session.get(CatalogDocumentRevision, revision_id)
            topology = revision.document["topology"]
        return ClusterMappingPlan(
            recipe_revision_id=revision_id,
            recipe_content_sha256="a" * 64,
            topology_name=topology["name"],
            generation=1,
            parameters=dict(parameters),
            nodes=tuple(
                SimpleNamespace(
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
    dual_document = _recipe_document()
    dual_topology = dual_document["topology"]
    role_template = dict(dual_topology["roles"][0])
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
    dual_document["topology"]["roles"] = [
        {**role_template, "name": "entrypoint", "endpoint_owner": True},
        {**role_template, "name": "worker", "endpoint_owner": False},
    ]
    dual_document["models"][0]["files"][0]["roles"] = ["entrypoint", "worker"]
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
            CatalogDocumentRevision.__table__.update()
            .where(CatalogDocumentRevision.id == dual_revision_id)
            .values(
                document=dual_document,
                content_digest=content_sha256(RecipeDefinition.model_validate(dual_document)),
            )
        )
        solo_document["identity"]["slug"] = "synthetic-tiny-solo"
        solo_document["metadata"]["title"] = "Synthetic Tiny Solo"
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
) -> FleetProfileApplication:
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
    scope = {"node_ids": [_node_id(1), _node_id(2)]}
    profile_a = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Dual applied",
                "scope": scope,
                "assignments": [
                    {
                        "recipe_revision_id": dual_revision_id,
                        "topology_name": "pair",
                        "desired_state": "running",
                        "alias": "dual-chat",
                        "nodes": [
                            {
                                "node_id": _node_id(1),
                                "rank": 0,
                                "role": "entrypoint",
                                "endpoint_owner": True,
                            },
                            {
                                "node_id": _node_id(2),
                                "rank": 1,
                                "role": "worker",
                                "endpoint_owner": False,
                            },
                        ],
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
                "scope": scope,
                "assignments": [
                    {
                        "recipe_revision_id": solo_revision_id,
                        "topology_name": "solo",
                        "desired_state": "running",
                        "alias": "solo-chat",
                        "nodes": [
                            {
                                "node_id": _node_id(1),
                                "rank": 0,
                                "role": "entrypoint",
                                "endpoint_owner": True,
                            }
                        ],
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
        assert (
            session.get(RecipeInstallation, dual_installation_id).state == "installed"
        )
        assert session.get(RecipeBuild, dual_build_id).state == "succeeded"
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
        assert (
            session.get(RecipeInstallation, dual_installation_id).state == "installed"
        )
        assert session.get(RecipeBuild, dual_build_id).state == "succeeded"
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

    preparation_preview = service.prepare_preview(profile.id)
    prepared = service.prepare(
        profile.id,
        plan_digest=preparation_preview.plan_digest,
        request_key=_uuid(41),
        actor="admin",
    )
    with sessions() as session:
        row = session.get(FleetProfileApplication, prepared.id)
        assert row is not None
        prepared_item = service._operation_item(row)
    assert prepared_item["kind"] == "fleet-profile.prepare"


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
    assert progress["child_progress"]["phase"] == "model-download"
    assert adapter.starts[0]["scope_node_ids"] == (_node_id(1), _node_id(2))

    observed_phases = [progress["child_progress"]["phase"]]
    for _ in range(8):
        assert service.tick() is True
        current = service.application(application.id)
        child_progress = current.progress.get("child_progress")
        if isinstance(child_progress, dict):
            observed_phases.append(child_progress["phase"])
        if current.state == "succeeded":
            break

    assert service.application(application.id).state == "succeeded"
    assert observed_phases[:5] == [
        "model-download",
        "container-download",
        "target-copy",
        "start",
        "final-verify",
    ]
    assert service.application(application.id).progress["step_results"]["0"][
        "result"
    ] == {"verified": True}


def test_profile_switch_adapter_plans_disjoint_assignments_once_and_resumes() -> None:
    sessions = _database()
    dual_revision_id, solo_revision_id = _seed_dual_solo_without_runtime_state(sessions)
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
                "scope": {
                    "node_ids": [_node_id(1), _node_id(2), _node_id(3)]
                },
                "assignments": [
                    {
                        "recipe_revision_id": dual_revision_id,
                        "topology_name": "pair",
                        "desired_state": "running",
                        "alias": "dual-chat",
                        "nodes": [
                            {
                                "node_id": _node_id(1),
                                "rank": 0,
                                "role": "entrypoint",
                                "endpoint_owner": True,
                            },
                            {
                                "node_id": _node_id(2),
                                "rank": 1,
                                "role": "worker",
                                "endpoint_owner": False,
                            },
                        ],
                    },
                    {
                        "recipe_revision_id": solo_revision_id,
                        "topology_name": "solo",
                        "desired_state": "running",
                        "alias": "solo-chat",
                        "nodes": [
                            {
                                "node_id": _node_id(3),
                                "rank": 0,
                                "role": "entrypoint",
                                "endpoint_owner": True,
                            }
                        ],
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
    dual_revision_id, solo_revision_id = _seed_dual_solo_without_runtime_state(sessions)
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
                "scope": {"node_ids": [_node_id(1), _node_id(2)]},
                "assignments": [
                    {
                        "recipe_revision_id": solo_revision_id,
                        "topology_name": "solo",
                        "desired_state": "running",
                        "alias": "solo-one",
                        "nodes": [
                            {
                                "node_id": _node_id(1),
                                "rank": 0,
                                "role": "entrypoint",
                                "endpoint_owner": True,
                            }
                        ],
                    },
                    {
                        "recipe_revision_id": solo_revision_id,
                        "topology_name": "solo",
                        "desired_state": "running",
                        "alias": "solo-two",
                        "nodes": [
                            {
                                "node_id": _node_id(2),
                                "rank": 0,
                                "role": "entrypoint",
                                "endpoint_owner": True,
                            }
                        ],
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
            scope=FleetProfileScope(node_ids=[_node_id(1), _node_id(2)]),
            assignments=[],
        ),
        actor="admin",
    )

    preview = service.preview(profile.id)

    assert preview.allowed is True
    assert preview.scope.idle_node_ids == [_node_id(1), _node_id(2)]
    assert preview.steps == []
    assert preview.preparations == []


def test_production_profile_adapter_binds_one_real_run_switch_child(
    tmp_path: Path,
) -> None:
    """The profile child delegates exact preparation and run admission to RunSwitch."""

    try:
        from .test_recipe_operations import setup_services
    except ImportError as error:
        pytest.skip(f"run-operation fixture awaits canonical catalog migration: {error}")
    from vonk_control.run_switch_operations import RunSwitchOperationService

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
                "scope": {"node_ids": list(nodes)},
                "assignments": [
                    {
                        "recipe_revision_id": revision.id,
                        "topology_name": "nodes_2",
                        "desired_state": "running",
                        "alias": "production-child",
                        "nodes": [
                            {
                                "node_id": nodes[0],
                                "rank": 0,
                                "role": "entrypoint",
                                "endpoint_owner": True,
                            },
                            {
                                "node_id": nodes[1],
                                "rank": 1,
                                "role": "worker",
                                "endpoint_owner": False,
                            },
                        ],
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
    adapter_progress = current.progress["switch_adapter"]
    child_id = adapter_progress["active_operation_id"]
    assert isinstance(child_id, str)
    child = run_switch.get(child_id)
    assert child.kind == "recipe.run-switch.v2"
    assert child.node_ids == list(nodes)
    assert child.action == "switch"
    assert current.progress["child_progress"]["node_ids"] == list(nodes)
    assert current.progress["child_progress"]["phase"] == "runtime-install"

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
    assert resumed.progress["switch_adapter"]["active_operation_id"] == child_id


def test_production_profile_adapter_routes_all_idle_to_one_complete_stop_child(
    tmp_path: Path,
) -> None:
    """An empty desired set still stops a complete in-scope run through RunSwitch."""

    try:
        from .test_recipe_operations import (
            installed_recipe,
            setup_services,
            started_recipe,
        )
    except ImportError as error:
        pytest.skip(f"run-operation fixture awaits canonical catalog migration: {error}")
    from vonk_control.run_switch_operations import RunSwitchOperationService

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
            scope=FleetProfileScope(node_ids=list(nodes)),
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
    state = current.progress["switch_adapter"]
    child_id = state["active_operation_id"]
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
        {
            "recipe_revision_id": revision_id,
            "topology_name": "solo",
            "desired_state": "installed",
            "alias": None,
            "nodes": [
                {
                    "node_id": _node_id(2),
                    "rank": 0,
                    "role": "entrypoint",
                    "endpoint_owner": True,
                }
            ],
        },
        {
            "recipe_revision_id": revision_id,
            "topology_name": "solo",
            "desired_state": "installed",
            "alias": None,
            "nodes": [
                {
                    "node_id": _node_id(1),
                    "rank": 0,
                    "role": "entrypoint",
                    "endpoint_owner": True,
                }
            ],
        },
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
            scope=FleetProfileScope(node_ids=[_node_id(1), _node_id(2)]),
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
    value["assignments"][0]["alias"] = None
    with pytest.raises(ValidationError, match="require an endpoint alias"):
        FleetProfileInput.model_validate(value)

    value = _input(_uuid(2)).model_dump(mode="json")
    value["assignments"][0]["nodes"][0]["endpoint_owner"] = False
    with pytest.raises(ValidationError, match="exactly one endpoint owner"):
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
    assert created.assignments[0].recipe_title == "Synthetic Tiny image"
    assert created.assignments[0].nodes[0].node_id == _node_id(1)
    assert len(created.profile_digest) == 64
    assert created.profile_digest == loaded.profile_digest


def test_direct_placement_profile_is_deterministic_and_hidden_from_saved_profiles() -> (
    None
):
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile_id = _uuid(30)
    assignment = _input(revision_id).assignments[0]

    first = service.ensure_internal_placement(profile_id, assignment, actor="admin")
    replay = service.ensure_internal_placement(profile_id, assignment, actor="admin")

    assert first == replay
    assert first.id == profile_id
    assert service.list().profiles == []


def test_profile_delete_removes_terminal_application_history() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(_input(revision_id), actor="admin")
    application_id = _uuid(20)
    with sessions.begin() as session:
        session.add(
            FleetProfileApplication(
                id=application_id,
                request_key=_uuid(21),
                profile_id=profile.id,
                profile_digest=profile.profile_digest,
                plan_digest="e" * 64,
                state="waiting-for-operator",
                plan={"steps": []},
                current_step=0,
                current_operation_id=None,
                progress={},
                result=None,
                status_reason="Operator review required",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    service.delete(profile.id)

    with sessions() as session:
        assert session.get(FleetProfile, profile.id) is None
        assert session.get(FleetProfileApplication, application_id) is None


def test_profile_validation_rejects_unknown_sparks_and_recipe_topology_drift() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    value = _input(revision_id).model_dump(mode="json")
    value["assignments"][0]["nodes"][0]["node_id"] = _node_id(9)
    value["scope"] = {"node_ids": [_node_id(9)]}

    with pytest.raises(FleetProfileConflict, match="active enrolled Fleet member"):
        service.create(FleetProfileInput.model_validate(value), actor="admin")

    value = _input(revision_id).model_dump(mode="json")
    value["assignments"][0]["topology_name"] = "pair"
    with pytest.raises(FleetProfileConflict, match="topology"):
        service.create(FleetProfileInput.model_validate(value), actor="admin")


def test_profile_validation_rejects_rank_order_that_mapping_would_rewrite() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    document = _recipe_document()
    topology = document["topology"]
    leader = deepcopy(topology["roles"][0])
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
    document["models"][0]["files"][0]["roles"] = ["leader", "worker"]
    parsed_document = RecipeDefinition.model_validate(document)
    with sessions.begin() as session:
        session.execute(
            CatalogDocumentRevision.__table__.update()
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
    value["assignments"][0].update(
        {
            "topology_name": "pair",
            "nodes": [
                {
                    "node_id": _node_id(2),
                    "rank": 0,
                    "role": "leader",
                    "endpoint_owner": True,
                },
                {
                    "node_id": _node_id(1),
                    "rank": 1,
                    "role": "worker",
                    "endpoint_owner": False,
                },
            ],
        }
    )
    value["scope"] = {"node_ids": [_node_id(1), _node_id(2)]}

    with pytest.raises(
        FleetProfileConflict, match="deterministic Spark identity order"
    ):
        FleetProfileService(sessions, clock=lambda: NOW).create(
            FleetProfileInput.model_validate(value), actor="admin"
        )


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
        stored_plan = session.get(FleetProfileApplication, application.id).plan
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

    class Operations:
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
    dual_document = _recipe_document()
    dual_topology = dual_document["topology"]
    role_template = dict(dual_topology["roles"][0])
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
    dual_document["topology"]["roles"] = [
        {**role_template, "name": "entrypoint", "endpoint_owner": True},
        {**role_template, "name": "worker", "endpoint_owner": False},
    ]
    dual_document["models"][0]["files"][0]["roles"] = ["entrypoint", "worker"]
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
            CatalogDocumentRevision.__table__.update()
            .where(CatalogDocumentRevision.id == dual_revision_id)
            .values(
                document=dual_document,
                content_digest=content_sha256(dual_recipe),
            )
        )
        solo_document = _recipe_document()
        solo_document["identity"]["slug"] = "synthetic-tiny-solo"
        solo_document["metadata"]["title"] = "Synthetic Tiny Solo"
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
    scope = {"node_ids": [_node_id(1), _node_id(2)]}
    profile_a = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Dual",
                "scope": scope,
                "assignments": [{
                    "recipe_revision_id": dual_revision_id, "topology_name": "pair",
                    "desired_state": "running", "alias": "dual-chat",
                    "nodes": [
                        {"node_id": _node_id(1), "rank": 0, "role": "entrypoint", "endpoint_owner": True},
                        {"node_id": _node_id(2), "rank": 1, "role": "worker", "endpoint_owner": False},
                    ],
                }],
            }
        ),
        actor="admin",
    )
    profile_b = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Solo and idle",
                "scope": scope,
                "assignments": [{
                    "recipe_revision_id": solo_revision_id, "topology_name": "solo",
                    "desired_state": "running", "alias": "solo-chat",
                    "nodes": [{"node_id": _node_id(1), "rank": 0, "role": "entrypoint", "endpoint_owner": True}],
                }],
            }
        ),
        actor="admin",
    )
    assert service.preview(profile_a.id).steps == []
    cross_scope = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Narrow scope",
                "scope": {"node_ids": [_node_id(1)]},
                "assignments": [],
            }
        ),
        actor="admin",
    )
    cross_preview = service.preview(cross_scope.id)
    assert cross_preview.allowed is False
    assert any(
        reason.code == "profile.distributed_cross_scope"
        for reason in cross_preview.reasons
    )
    switch_to_b = service.preview(profile_b.id)
    assert [step.kind for step in switch_to_b.steps].count("stop") == 1
    assert switch_to_b.scope.idle_node_ids == [_node_id(2)]
    assert switch_to_b.summary.uninstalls == 0

    with sessions.begin() as session:
        session.get(RecipeRun, _uuid(17)).state = "stopped"
        session.get(RecipeRun, _uuid(17)).route_state = "withdrawn"
    back_to_a = service.preview(profile_a.id)
    assert [step.kind for step in back_to_a.steps] == ["start"]
    assert back_to_a.summary.installs == 0
