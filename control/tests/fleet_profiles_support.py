from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from importlib.resources import files
from typing import TypedDict

from sqlalchemy import Table, create_engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    LifecycleState,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_control.fleet_profile_contract import (
    FleetProfileChildOperation,
    FleetProfileChildProgress,
    FleetProfileInput,
    FleetProfileVerificationResult,
)
from vonk_control.fleet_profiles import (
    FleetProfileService,
)
from vonk_control.models import (
    AgentNode,
    AgentNodeContact,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    FleetProfileApplication,
    User,
)
from vonk_control.persisted_waits import wait_due
from vonk_control.platform_ports import HOST_ENDPOINT_PORT
from vonk_control.preparation_contract import (
    RolloutPreparation,
)
from vonk_control.recipe_execution_contract import installation_plan_document
from vonk_control.run_switch_contract import (
    RunSwitchAssessment,
    RunSwitchReason,
    SparkFit,
    SparkFitNode,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture
from .test_recipe_execution_contract import _installation_plan

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


def _installed_pair_plan(
    *, mapping_id: str, build_id: str, recipe_revision_id: str, recipe_digest: str
) -> dict[str, object]:
    """Use the real persisted contract for fixtures which promise image reuse."""
    plan = _installation_plan({})
    template = _nested_object(plan, "compiled_execution_plans", "spk_" + "0" * 32)
    node_template = _nested_object(plan, "nodes", 0)
    nodes: list[dict[str, object]] = []
    compiled: dict[str, object] = {}
    for rank, role in enumerate(("entrypoint", "worker")):
        node_id = _node_id(rank + 1)
        nodes.append({**node_template, "node_id": node_id, "rank": rank, "role": role})
        payload = deepcopy(template)
        # A two-Spark engine listens on the firewall-authorised host port.
        _nested_object(payload, "endpoint")["port"] = HOST_ENDPOINT_PORT
        _nested_object(payload, "identity")["recipe_revision_sha256"] = recipe_digest
        _nested_object(payload, "runtime", "placement").update(
            rank=rank, role=role, world_size=2
        )
        _nested_object(payload, "topology").update(name="pair", node_count=2)
        _nested_object(payload, "runtime_image").update(
            image_digest="sha256:" + "d" * 64,
            oci_layout_sha256="e" * 64,
            image_bytes=1024,
            build_id=build_id,
        )
        compiled[node_id] = payload
    plan.update(
        mapping_id=mapping_id,
        recipe_build_id=build_id,
        image_digest="sha256:" + "d" * 64,
        recipe_revision_id=recipe_revision_id,
        recipe_content_sha256=recipe_digest,
        plan_digest="f" * 64,
        nodes=nodes,
        compiled_execution_plans=compiled,
    )
    return installation_plan_document(plan)


def _assessment(preparation: RolloutPreparation) -> RunSwitchAssessment:
    blockers = [
        RunSwitchReason(**reason.model_dump(), scope="artifact")
        for reason in preparation.reasons
        if reason.severity == "blocker"
    ]
    return RunSwitchAssessment(
        alias="test",
        preparation=preparation,
        allowed=not blockers,
        blockers=blockers,
        warnings=[],
        stops=[],
        fit_current=SparkFit(
            allowed=not blockers,
            nodes=[
                SparkFitNode(
                    node_id=node,
                    rank=rank,
                    role="worker",
                    allowed=True,
                    ports_required=[],
                    disk_required_bytes=0,
                    memory_required_bytes=100,
                    memory_kind="unified",
                    memory_pool="shared",
                    memory_floor_bytes=0,
                )
                for rank, node in enumerate(preparation.target_node_ids)
            ],
        ),
        fit_after_stop=None,
    )


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
        self.cancellations: list[tuple[tuple[str, ...], int]] = []
        self.cancellation_requests: list[tuple[str, str, str]] = []

    def validate_resources_in_session(self, session, assignments, reviewed) -> None:
        # This deterministic adapter owns the isolated service-test boundary.
        # Production capacity is exercised through the real Run/Switch adapter.
        pass

    def request_superseded_workload_cancellation_in_session(
        self, session: Session, targets: tuple[str, ...], ordinal: int, now: datetime
    ) -> None:
        self.cancellations.append((targets, ordinal))

    def request_cancellation(
        self, application_id: str, *, request_key: str, actor: str
    ) -> None:
        self.cancellation_requests.append((application_id, request_key, actor))

    def recoverable_cache_loss(self, application_id: str, *, session: Session) -> bool:
        del application_id, session
        return False

    def recovery_refused(self, application_id: str, *, session: Session) -> bool:
        del application_id, session
        return False

    def failure_signature(self, application_id: str, *, session: Session) -> str | None:
        del application_id, session
        return None

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
            FleetProfileChildProgress(phase="model-download", bytes=0, total_bytes=100),
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
                id=operation_id, state=LifecycleState.RUNNING, progress=item
            )
            for item in progress
        ] + [
            FleetProfileChildOperation(
                id=operation_id,
                state=LifecycleState.SUCCEEDED,
                progress=progress[-1],
                result=FleetProfileVerificationResult(verified=True),
            )
        ]
        return self._operations[operation_id][0]

    def get(
        self, operation_id: str, *, session: object = None
    ) -> FleetProfileChildOperation:
        return self._operations[operation_id][self._states[operation_id]]

    def advance(
        self, operation_id: str, *, session: object = None
    ) -> FleetProfileChildOperation:
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
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )


def _seed(sessions: sessionmaker[Session]) -> tuple[str, str]:
    with sessions.begin() as session:
        session.add(User(subject="admin", role="administrator"))
    recipe_id = _uuid(1)
    revision_id = _uuid(2)
    document = _recipe_document()
    recipe = RecipeDefinition.model_validate(document)
    model_document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    model = ModelDefinition.model_validate(model_document)
    model_id = _uuid(3)
    model_revision_id = _uuid(4)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(1),
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    _node_id(1),
                    observed_at=NOW,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
                contact=AgentNodeContact(protocol_version=1, last_seen_at=NOW),
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
                    title="Synthetic Tiny build",
                    created_by="admin",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                CatalogDocument(
                    id=model_id,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    title="Synthetic Tiny",
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
                    content_digest=document_sha256(document),
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
                    document=model_document,
                    content_digest=document_sha256(model_document),
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
                    "recipe_selector": "vonk-forge/synthetic-tiny-build",
                    "spark_ids": [_node_id(1)],
                    "desired_state": "running",
                    "assignment_name": "studio-chat",
                }
            ],
        }
    )


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
            "node_count": 2,
            "roles": [
                {"name": "entrypoint", "count": 1, "endpoint_owner": True},
                {"name": "worker", "count": 1, "endpoint_owner": False},
            ],
            "parallelism": {
                "tensor": 2,
                "pipeline": 1,
                "data": 1,
                "backend": "nccl",
            },
            "start_order": ["worker", "entrypoint"],
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
                inventory_snapshot=inventory_items_fixture(
                    _node_id(2),
                    observed_at=NOW,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
                contact=AgentNodeContact(protocol_version=1, last_seen_at=NOW),
            )
        )
        session.execute(
            revisions.update()
            .where(CatalogDocumentRevision.id == dual_revision_id)
            .values(
                document=dual_document,
                content_digest=document_sha256(dual_document),
            )
        )
        _nested_object(solo_document, "identity")["slug"] = "synthetic-tiny-solo"
        solo = RecipeDefinition.model_validate(solo_document)
        solo_root_id = _uuid(4)
        session.add(
            CatalogDocument(
                id=solo_root_id,
                kind="recipe",
                publisher=solo.identity.publisher,
                slug=solo.identity.slug,
                title="Synthetic Tiny Solo",
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
                content_digest=document_sha256(solo_document),
                execution_key="c" * 64,
                created_by="admin",
                created_at=NOW,
            )
        )
    return dual_revision_id, solo_revision_id


def _follow_profile_retry(
    service: FleetProfileService,
    application_id: str,
    now: list[datetime],
    *,
    assert_no_work: bool = False,
) -> None:
    """Respect the accepted row's durable due time, without a guessed clock jump."""
    with service._sessions() as session:
        row = session.get(FleetProfileApplication, application_id)
        assert row is not None
        due = wait_due(row)
        child_id = row.current_operation_id
    if due is not None and due > now[0]:
        worked = service.tick()
        if assert_no_work:
            assert worked is False
        assert service.application(application_id).current_operation_id == child_id
        now[0] = due
