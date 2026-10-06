from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, Table, create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    DesiredAssignmentState,
    LifecycleState,
    SupersedeCode,
)
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
    FleetProfileAssignment,
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
    FleetProfileAdmissionBusy,
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    _operation_state,
    build_production_fleet_profile_service,
)
from vonk_control.lifecycle.evidence import Residue
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
    User,
)
from vonk_control.operation_api import OperationQuery
from vonk_control.operation_item_contract import operation_item
from vonk_control.platform_ports import HOST_ENDPOINT_PORT
from vonk_control.preparation_contract import (
    ControllerAssetState,
    ModelArtifactPreparation,
    PreparationReason,
    RolloutPreparation,
    RuntimeImagePreparation,
    TargetAssetState,
)
from vonk_control.recipe_execution_contract import installation_plan_document
from vonk_control.run_switch_contract import (
    RunSwitchAssessment,
    RunSwitchReason,
    SparkFit,
    SparkFitNode,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

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


def test_profile_progress_and_results_are_closed_nested_contracts() -> None:
    operation_id = _uuid(700)
    progress = FleetProfileApplicationProgress.model_validate(
        {
            "operation_kind": "fleet-profile.apply",
            "completed_steps": 1,
            "total_steps": 1,
            "step_results": {
                "0": {
                    "kind": "switch",
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


def test_profile_switch_state_rebuilds_malformed_persisted_progress() -> None:
    # Damaged progress is rebuilt from the row's own receipt: it names no child
    # document, so the child is found again through its deterministic key.
    assert (
        RunSwitchFleetProfileAdapter._state(FleetProfileApplication(progress="invalid"))
        is None
    )
    assert (
        RunSwitchFleetProfileAdapter._state(
            FleetProfileApplication(progress={"switch_adapter": "invalid"})
        )
        is None
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_profile_application_read_degrades_for_malformed_persisted_plan_and_result() -> (
    None
):
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(
        profile.id,
        request_key=_uuid(701),
        actor="admin",
    )

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        admitted_plan = deepcopy(row.plan)
        row.plan = {"steps": []}
    # A plan that cannot be read degrades the view (the step count the progress
    # recorded, no cancellation projection); the read is never refused.
    degraded = service.application(application.id)
    assert degraded.id == application.id
    assert degraded.cancellation is None

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.plan = admitted_plan
        # A JSON array is never a valid stored result document; write it as the
        # driver would have persisted it rather than through the typed attribute.
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == application.id)
            .values(result=["malformed"])
        )
    # A damaged result of a load that has not succeeded is retired (no result).
    assert service.application(application.id).result is None


@pytest.mark.usefixtures("damaged_json_rows")
def test_profile_worker_retires_malformed_persisted_plan_as_unknown() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(
        profile.id,
        request_key=_uuid(702),
        actor="admin",
    )

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.plan = {"steps": []}

    assert service.tick() is True
    with sessions() as session:
        retired = session.get(FleetProfileApplication, application.id)
        assert retired is not None
        # Retired, not failed for a person: its effect is unknown, and the worker
        # went on to the next item.
        assert retired.state == "cancelled"
        assert "effect is unknown" in (retired.status_reason or "")


def test_profile_worker_retires_malformed_persisted_progress_as_unknown() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)

    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(
        profile.id,
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
        retired = session.get(FleetProfileApplication, application.id)
        assert retired is not None
        assert retired.state == "cancelled"
        assert "effect is unknown" in (retired.status_reason or "")


def test_profile_application_read_rebuilds_result_for_succeeded_state() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(
        profile.id,
        request_key=_uuid(703),
        actor="admin",
    )

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.state = "succeeded"
        row.current_step = application.total_steps
        row.result = None
    # The result is rebuilt from the step the receipt reached.
    rebuilt = service.application(application.id).result
    assert rebuilt is not None
    assert rebuilt.completed_steps == application.total_steps


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
                protocol_version=1,
                architecture="linux-arm64",
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
                protocol_version=1,
                architecture="linux-arm64",
                last_seen_at=NOW,
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
                content_digest=document_sha256(solo_document),
                execution_key="c" * 64,
                created_by="admin",
                created_at=NOW,
            )
        )
    return dual_revision_id, solo_revision_id


def test_profile_admission_proceeds_when_the_reviewed_build_record_is_missing() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    preparation = _exact_preparation((_node_id(1),))
    preparation = preparation.model_copy(
        update={
            "runtime_image": preparation.runtime_image.model_copy(
                update={"build_id": _uuid(999)}
            )
        }
    )
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda *_args, **_kwargs: _assessment(preparation),
    )
    profile = service.create(_input(revision_id), actor="admin")
    preview = service.preview(profile.id)
    assert preview.allowed
    # The build record the review named is gone: there is no running build to
    # protect, so the load is admitted (the exact image identity is the plan's own
    # and is verified again when its child starts), not refused.
    application = service.apply(profile.id, request_key=_uuid(40), actor="admin")
    assert application.state == "queued"
    with sessions() as session:
        assert session.scalar(select(FleetProfileApplication)) is not None


def test_profile_operation_projection_uses_bound_scope_and_canonical_phase() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids))
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(
        profile.id,
        request_key=_uuid(40),
        actor="admin",
    )

    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        item = service._operation_item(row).model_dump(mode="json")

    assert item["id"] == application.id
    assert item["parent_id"] is None
    assert item["node_ids"] == [_node_id(1)]
    assert item["kind"] == "fleet-profile.apply"
    assert item["progress"]["phase"] == "prepare"


def test_profile_view_projects_the_loaded_assignment_state(monkeypatch) -> None:
    # Live regression: a loaded profile whose run was serving still listed
    # its assignment as "Not loaded" because the view never consulted the
    # loaded application.
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids))
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")
    assert service.get(profile.id).assignments[0].observed_state == "Not loaded"

    service.apply(
        profile.id,
        request_key=_uuid(441),
        actor="admin",
    )
    assert service.get(profile.id).assignments[0].observed_state == "Not placed"

    real_state = FleetProfileService._assignment_state.__func__

    def running(cls, session, assignment, **kwargs):
        state = real_state(cls, session, assignment, **kwargs)
        state.current_state = "running"
        return state

    monkeypatch.setattr(FleetProfileService, "_assignment_state", classmethod(running))
    view = service.get(profile.id)
    assert view.status == "loaded"
    assert view.assignments[0].observed_state == "Running"

    # A saved edit the loaded application does not contain is not loaded.
    with sessions.begin() as session:
        saved = session.get(FleetProfile, profile.id)
        assert saved is not None
        saved.assignments = [{**saved.assignments[0], "spark_ids": [_node_id(2)]}]
    assert service.get(profile.id).assignments[0].observed_state == "Not loaded"


@pytest.mark.usefixtures("damaged_json_rows")
def test_profile_endpoint_intent_uses_loaded_application_after_saved_edits() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids))
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")
    with sessions() as session:
        saved_only = service.endpoint_intent(session, profile.number)
    assert saved_only.assignments == ()

    application = service.apply(
        profile.id,
        request_key=_uuid(440),
        actor="admin",
    )

    with sessions.begin() as session:
        saved = session.get(FleetProfile, profile.id)
        assert saved is not None
        saved.assignments = []

    with sessions() as session:
        intent = service.endpoint_intent(session, profile.number)

    assert intent.application_id == application.id
    assert intent.application_state == "queued"
    assert intent.assignments is not None
    assert len(intent.assignments) == 1
    assert intent.assignments[0].alias == "studio-chat"
    assert intent.assignments[0].state == "not-published-yet"

    # A completed application cannot make a missing run look merely pending.
    # The saved definition is still empty; the immutable assignment remains
    # visible with truthful withdrawal and no usable run identity.
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.state = "succeeded"
    with sessions() as session:
        withdrawn = service.endpoint_intent(session, profile.number)
    assert withdrawn.application_id == application.id
    assert withdrawn.assignments is not None
    assert len(withdrawn.assignments) == 1
    assert withdrawn.assignments[0].state == "withdrawn"
    assert withdrawn.assignments[0].expected_run_id is None

    # A required field missing from immutable persisted progress should make
    # endpoint membership explicitly unreadable, while execution keeps using
    # the strict progress validator.
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        valid_progress = deepcopy(row.progress)
    assert isinstance(valid_progress, dict)

    for missing_field in ("reviewed_plan_digest", "reviewed_application_id"):
        corrupted_progress = deepcopy(valid_progress)
        intended_profile = corrupted_progress.get("intended_profile")
        assert isinstance(intended_profile, dict)
        intended_profile.pop(missing_field)
        with sessions.begin() as session:
            row = session.get(FleetProfileApplication, application.id)
            assert row is not None
            row.progress = corrupted_progress

        with sessions() as session:
            unreadable = service.endpoint_intent(session, profile.number)
            row = session.get(FleetProfileApplication, application.id)
            assert row is not None
            # The strict reader names the damaged field and retires the intent
            # as unknown (a Residue); it never raises into a read.
            retired = service._intended_profile(row, session=session)
            assert isinstance(retired, Residue)

        assert unreadable.application_id == application.id
        assert unreadable.assignments is None
        assert unreadable.projection_issue is not None
        assert unreadable.projection_issue.detail == (
            f"stored document is invalid at intended_profile.{missing_field} (missing)"
        )

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.progress = valid_progress

    # Endpoint discovery is a read-only projection. If the immutable reviewed
    # plan is corrupt, expose that membership is unknown without changing the
    # strict validator used by execution and recovery.
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.plan = {}

    with sessions() as session:
        unavailable = service.endpoint_intent(session, profile.number)
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        assert isinstance(service._intended_profile(row, session=session), Residue)

    assert unavailable.application_id == application.id
    assert unavailable.application_state == "succeeded"
    assert unavailable.assignments is None
    assert unavailable.projection_issue is not None
    assert unavailable.projection_issue.code == "profile.application_intent.invalid"
    assert unavailable.projection_issue.detail == (
        "stored document is invalid at profile_id (missing)"
    )


def test_profile_switch_delegates_non_idle_assignment_and_surfaces_child_progress() -> (
    None
):
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
    adapter = _SwitchAdapter()
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
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
        request_key=_uuid(42),
        actor="admin",
    )
    assert service.tick() is True
    progress = service.application(application.id).progress
    assert progress.child_progress is not None
    assert progress.child_progress.phase == "model-download"
    assert adapter.starts[0]["scope_node_ids"] == (_node_id(1),)
    with sessions() as session:
        # The accepted selection covers the whole fleet, including idle Sparks,
        # so a later profile load can fence an older effect on either node.
        assert [
            node.workload_intent_ordinal
            for node in session.scalars(select(AgentNode).order_by(AgentNode.node_id))
        ] == [1, 1]

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


def test_new_profile_load_supersedes_older_queued_scope_by_acceptance_order() -> None:
    """A pending whole-fleet load cannot veto a later authorized profile."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = [NOW]
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=_SwitchAdapter()
    )
    first_profile = service.create(_input(revision_id), actor="admin")
    second_profile = service.create(
        _input(revision_id).model_copy(update={"name": "Newer choice"}),
        actor="admin",
    )
    first = service.apply(
        first_profile.id,
        request_key=_uuid(971),
        actor="admin",
    )
    second = service.apply(
        second_profile.id,
        request_key=_uuid(972),
        actor="admin",
    )
    assert first.state == second.state == "queued"
    assert first.created_at < second.created_at
    assert first.progress.workload_intent_ordinal == 1
    assert second.progress.workload_intent_ordinal == 2
    # Admission cancels the older logical order even if the worker selects
    # the newer row first under a tied clock.
    replaced = service.application(first.id)
    assert replaced.state == "superseded"
    assert replaced.reason_code == "superseded-by-intent"
    assert service.application(second.id).state == "queued"


def test_parked_profile_load_fences_workload_before_admission_retry(
    monkeypatch,
) -> None:
    """A parked replacement cancels the older intent before full admission resumes."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    adapter = _SwitchAdapter()
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
    profile = service.create(_input(revision_id), actor="admin")
    original_queue = FleetProfileService._queue_application

    def stay_busy(self, reviewed, **kwargs):
        raise FleetProfileAdmissionBusy("test admission owner")

    monkeypatch.setattr(FleetProfileService, "_queue_application", stay_busy)
    parked = service.apply(
        profile.id,
        request_key=_uuid(973),
        actor="admin",
    )
    # Admission retries itself; the accepted intent stays queued with a due time.
    assert parked.state == "queued"
    assert parked.progress.admission_retry_at is not None
    assert parked.progress.workload_intent_ordinal is None

    monkeypatch.setattr(FleetProfileService, "_queue_application", original_queue)
    assert service.tick() is True
    resumed = service.application(parked.id)
    assert resumed.progress.workload_intent_ordinal == 1
    assert adapter.cancellations == [((_node_id(1),), 1)]


def test_parked_profile_load_rechecks_fencing_after_ordinal_is_bound(
    monkeypatch,
) -> None:
    """A parked admission repeats supersession reconciliation after a retry."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    adapter = _SwitchAdapter()
    now = [NOW]
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=adapter
    )
    profile = service.create(_input(revision_id), actor="admin")

    def stay_busy(self, reviewed, **kwargs):
        raise FleetProfileAdmissionBusy("test admission owner")

    monkeypatch.setattr(FleetProfileService, "_queue_application", stay_busy)
    parked = service.apply(
        profile.id,
        request_key=_uuid(974),
        actor="admin",
    )
    # Admission retries itself; the accepted intent stays queued with a due time.
    assert parked.state == "queued"
    assert parked.progress.admission_retry_at is not None
    assert parked.progress.workload_intent_ordinal is None

    assert service.tick() is True
    first = service.application(parked.id)
    assert first.progress.workload_intent_ordinal == 1
    assert adapter.cancellations == [((_node_id(1),), 1)]

    # The second attempt is due on the lifecycle core's clock (3 to 5 seconds).
    now[0] += timedelta(seconds=6)
    assert service.tick() is True
    assert adapter.cancellations == [((_node_id(1),), 1), ((_node_id(1),), 1)]


def test_newer_parked_profile_load_retires_older_parked_intent(
    monkeypatch,
) -> None:
    """A newer parked intent must stop an older retry loop from starving it."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = [NOW]
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    preview = service.preview(profile.id)

    def stay_busy(self, reviewed, **kwargs):
        raise FleetProfileAdmissionBusy("test admission owner")

    monkeypatch.setattr(FleetProfileService, "_queue_application", stay_busy)
    first = service.apply(
        profile.id,
        request_key=_uuid(976),
        actor="admin",
    )
    assert first.state == "queued"
    assert service.tick() is True
    first = service.application(first.id)
    assert first.progress.workload_intent_ordinal == 1

    refreshed = service.preview(profile.id)
    assert refreshed.allowed is True
    assert refreshed.plan_digest != preview.plan_digest
    assert any(
        effect.kind == "profile-application" and effect.id == first.id
        for effect in refreshed.effects.superseded
    )
    second = service.apply(
        profile.id,
        request_key=_uuid(977),
        actor="admin",
    )
    assert second.state == "queued"
    assert service.tick() is True
    first = service.application(first.id)
    second = service.application(second.id)
    assert first.state == "superseded"
    assert "later scoped intent" in (first.status_reason or "")
    assert second.progress.workload_intent_ordinal == 2


@pytest.mark.parametrize("old_state", ["failed", "queued", "running"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_new_load_is_independent_of_invalid_historical_progress(old_state: str) -> None:
    """A history parser failure must not veto a fresh authorized workload."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = NOW
    service = FleetProfileService(
        sessions, clock=lambda: now, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    first = service.load(
        profile.number,
        request_key=_uuid(975),
        actor="admin",
    )
    now += timedelta(seconds=1)
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        row.state = old_state
        row.status_reason = "stored attempt cannot continue"
        damaged_progress = {**row.progress, "attempt": "invalid"}
        row.progress = damaged_progress

    second = service.load(
        profile.number,
        request_key=_uuid(976),
        actor="admin",
    )
    assert second.state == "queued"
    assert second.progress.workload_intent_ordinal == 2
    assert second.retry_of_application_id is None
    assert second.progress.intended_profile is not None
    assert (
        service.load(
            profile.number,
            request_key=_uuid(976),
            actor="admin",
        )
        == second
    )
    assert service.progress_number(profile.number).id == second.id
    with sessions() as session:
        old = session.get(FleetProfileApplication, first.id)
        assert old is not None
        assert old.progress == damaged_progress
        assert old.state == old_state
    if old_state != "failed":
        # The damaged order is retired as superseded with its effect unknown by its
        # own worker pass; the fresh load is not held back by it.
        assert service.tick() is True
        with sessions() as session:
            old = session.get(FleetProfileApplication, first.id)
            assert old is not None
            assert old.state == "cancelled"
            assert old.progress == damaged_progress
    # Invalid history is never executed or silently repaired: replaying its key
    # answers with the stored receipt, rebuilt from its own columns.
    replay = service.load(
        profile.number,
        request_key=_uuid(975),
        actor="admin",
    )
    assert replay.id == first.id


@pytest.mark.usefixtures("damaged_json_rows")
def test_preview_isolates_an_unreadable_pending_plan() -> None:
    """A damaged pending step list must not veto a fresh authorized preview."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    assigned = service.create(_input(revision_id), actor="admin")
    pending = service.load(
        assigned.number,
        request_key=_uuid(981),
        actor="admin",
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, pending.id)
        assert row is not None
        # The step list is unreadable, but the declared scope written at
        # admission is still the durable boundary.
        row.plan = {**row.plan, "steps": [{"kind": "not-a-step"}]}
    idle = service.create(
        FleetProfileInput(name="All idle", assignments=[]), actor="admin"
    )

    preview = service.preview(idle.id)

    assert preview.allowed is True
    assert [step.node_ids for step in preview.steps] == [[_node_id(1)]]
    assert all(
        reason.code != "profile.pending_record_unreadable" for reason in preview.reasons
    )
    # The damaged order is preserved for its own worker to quarantine.
    with sessions() as session:
        damaged = session.get(FleetProfileApplication, pending.id)
        assert damaged is not None
        assert damaged.state == "queued"
        assert damaged.plan["steps"] == [{"kind": "not-a-step"}]


@pytest.mark.parametrize("desired_state", ["installed", "running"])
def test_named_assignment_keeps_authoring_name_without_inventing_an_endpoint(
    desired_state: str,
) -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    document = _input(revision_id).model_dump(mode="json")
    document["assignments"][0]["desired_state"] = desired_state
    saved = service.create(
        FleetProfileInput.model_validate_json(json.dumps(document)), actor="admin"
    )

    # Re-read persisted authoring data before the real preview conversion.
    stored = service.get(saved.id)
    assert stored.definition.assignments[0].assignment_name == "studio-chat"
    preview = service.preview(saved.id)
    assignment = preview.resolved_assignments[0]
    assert assignment.desired_state == desired_state
    assert assignment.alias == ("studio-chat" if desired_state == "running" else None)


@pytest.mark.usefixtures("damaged_json_rows")
def test_preview_skips_an_unreadable_pending_plan_without_a_scope() -> None:
    """A record that cannot be read at all never blocks new profile work."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    assigned = service.create(_input(revision_id), actor="admin")
    pending = service.load(
        assigned.number,
        request_key=_uuid(982),
        actor="admin",
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, pending.id)
        assert row is not None
        row.plan = {"steps": [{"kind": "not-a-step"}]}
    idle = service.create(
        FleetProfileInput(name="All idle", assignments=[]), actor="admin"
    )

    preview = service.preview(idle.id)

    assert all(
        reason.code != "profile.pending_record_unreadable" for reason in preview.reasons
    )


def test_preview_treats_a_preparation_blocker_as_not_allowed() -> None:
    """A preparation blocker blocks admission instead of only being listed.

    Preparation reasons carry the run-switch severity vocabulary, whose blocking
    value is ``blocker`` rather than this contract's ``error``.  Counting only
    ``error`` reported the profile as allowed while its own preparation said the
    persisted source-build plan is invalid -- and the apply then refused with
    ``profile child plan blocked: run-switch.container-build-plan-invalid`` a
    moment later, which is the silent admission the profile cache contract
    forbids.
    """

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    node_id = _node_id(1)
    digest = "a" * 64
    asset_bytes = 1024

    def blocking_preparation(
        session: Session,
        assignment: FleetProfileAssignment,
        expected_nodes: tuple[str, ...],
    ) -> RolloutPreparation:
        del session, assignment, expected_nodes
        # Shaped like the live GLM receipt: the Controller holds verified bytes,
        # no Spark has the asset yet, and the stored build plan cannot be
        # revalidated under the current executable identity.
        return RolloutPreparation(
            model=ModelArtifactPreparation(
                artifact_set_sha256=digest,
                model_content_sha256="b" * 64,
                recipe_revision_sha256="c" * 64,
                artifact_count=1,
                artifact_set_bytes=asset_bytes,
                completeness="complete",
                controller=ControllerAssetState(
                    state="ready",
                    expected_bytes=asset_bytes,
                    verified_bytes=asset_bytes,
                    missing_bytes=0,
                    verified_sha256=digest,
                    verified_at=NOW,
                    source="nas-cache",
                ),
                targets=[
                    TargetAssetState(
                        node_id=node_id,
                        state="unknown",
                        expected_bytes=asset_bytes,
                        present_bytes=0,
                        missing_bytes=asset_bytes,
                    )
                ],
            ),
            runtime_image=RuntimeImagePreparation(
                image_digest="sha256:" + "d" * 64,
                oci_layout_sha256="e" * 64,
                image_bytes=asset_bytes,
                architecture="linux-arm64",
                runtime_interface="openai",
                build_id=_uuid(1234),
                controller=ControllerAssetState(
                    state="ready",
                    expected_bytes=asset_bytes,
                    verified_bytes=asset_bytes,
                    missing_bytes=0,
                    verified_sha256="e" * 64,
                    verified_at=NOW,
                    source="controller-build",
                ),
                targets=[
                    TargetAssetState(
                        node_id=node_id,
                        state="unknown",
                        expected_bytes=asset_bytes,
                        present_bytes=0,
                        missing_bytes=asset_bytes,
                    )
                ],
            ),
            target_node_ids=[node_id],
            controller_ready=True,
            targets_ready=False,
            ready=False,
            reasons=[
                PreparationReason(
                    code="run-switch.container-build-plan-invalid",
                    detail="The persisted source-build plan is invalid.",
                    severity="blocker",
                )
            ],
        )

    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda *args, **_kwargs: _assessment(
            blocking_preparation(*args)
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")

    preview = service.preview(profile.id)

    # The blocker is carried by the preparation, not by the profile's own
    # reasons, and it must still decide the admission verdict.
    assert [(reason.code, reason.detail) for reason in preview.reasons] == []
    assert preview.summary.blockers == 1
    assert preview.allowed is False


@pytest.mark.usefixtures("damaged_json_rows")
def test_activity_projection_keeps_valid_records_when_one_is_unreadable() -> None:
    """One damaged profile application must not fail the whole Activity page."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    valid = service.load(
        profile.number,
        request_key=_uuid(983),
        actor="admin",
    )
    with sessions() as session:
        valid_row = session.get(FleetProfileApplication, valid.id)
        assert valid_row is not None
        valid_progress = deepcopy(valid_row.progress)
    damaged_id = _uuid(984)
    with sessions.begin() as session:
        session.add(
            FleetProfileApplication(
                id=damaged_id,
                request_key=_uuid(985),
                profile_id=profile.id,
                profile_digest="a" * 64,
                plan_digest="b" * 64,
                state="failed",
                plan={"steps": []},
                current_step=0,
                current_operation_id=None,
                progress=valid_progress,
                result=None,
                status_reason="stored attempt cannot continue",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    page = service.operation_provider().list_operations(
        OperationQuery(after=None, limit=10, state=None, node_id=None)
    )

    items = {
        operation_item(item).id: operation_item(item).model_dump(
            mode="json", exclude_none=False
        )
        for item in page.items
    }
    assert set(items) == {valid.id, damaged_id}
    assert items[valid.id]["failure"] is None
    assert items[valid.id]["node_ids"] == [_node_id(1)]
    unreadable = items[damaged_id]
    assert unreadable.get("failure") is None
    assert unreadable["status_reason"] == (
        "Stored profile application record is unreadable."
    )
    assert unreadable["supported_actions"] == []
    assert unreadable["result"] is None


@pytest.mark.usefixtures("damaged_json_rows")
def test_activity_lists_a_record_with_retired_fields_and_new_work_proceeds() -> None:
    """Fields a newer Controller retired never make stored history unreadable."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    old = service.load(profile.number, request_key=_uuid(991), actor="admin")
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, old.id)
        assert row is not None
        row.progress = {**row.progress, "retired_field": {"nested": 1}}
        row.plan = {**row.plan, "retired_plan_field": True}

    items = {
        operation_item(item).id: operation_item(item).model_dump(mode="json")
        for item in service.operation_provider()
        .list_operations(OperationQuery(after=None, limit=10, state=None, node_id=None))
        .items
    }

    assert items[old.id]["kind"] == "fleet-profile.apply"
    assert items[old.id]["node_ids"] == [_node_id(1)]
    assert items[old.id].get("status_reason") is None
    idle = service.create(
        FleetProfileInput(name="All idle", assignments=[]), actor="admin"
    )
    assert service.preview(idle.id).allowed is True


@pytest.mark.usefixtures("damaged_json_rows")
def test_retry_eligibility_survives_a_damaged_sibling_receipt() -> None:
    """Damaged history must not deny a valid receipt its retry authority."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = [NOW]
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    first = service.load(
        profile.number,
        request_key=_uuid(986),
        actor="admin",
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        row.state = "failed"
        row.status_reason = "stored attempt cannot continue"
        valid_progress = deepcopy(row.progress)
        session.add(
            FleetProfileApplication(
                id=_uuid(987),
                request_key=_uuid(988),
                profile_id=profile.id,
                profile_digest=row.profile_digest,
                plan_digest="c" * 64,
                state="failed",
                plan=deepcopy(row.plan),
                current_step=0,
                current_operation_id=None,
                progress={**valid_progress, "unexpected": {}},
                result=None,
                status_reason="stored attempt cannot continue",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    assert service.retry_eligible(first.id) is True

    # The authoritative per-node intent still fences the receipt once a later
    # load supersedes it, even though a damaged sibling is present.
    service.load(
        profile.number,
        request_key=_uuid(989),
        actor="admin",
    )
    assert service.retry_eligible(first.id) is False


def test_new_load_replaces_same_profile_while_same_key_replays() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = [NOW]
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    first = service.load(
        profile.number,
        request_key=_uuid(977),
        actor="admin",
    )
    second = service.load(
        profile.number,
        request_key=_uuid(978),
        actor="admin",
    )
    assert second.id != first.id
    assert second.progress.workload_intent_ordinal == 2
    assert service.application(first.id).state == "superseded"
    assert second.progress.intended_profile is not None
    assert (
        service.load(
            profile.number,
            request_key=_uuid(978),
            actor="admin",
        )
        == second
    )


def test_profile_switch_adapter_plans_disjoint_assignments_once_and_resumes() -> None:
    sessions = _database()
    _dual_revision_id, _solo_revision_id = _seed_dual_solo_without_runtime_state(
        sessions
    )
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(3),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
    adapter = _SwitchAdapter()
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Dual plus solo",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
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

    resumed = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
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
    dual_revision_id, _solo_revision_id = _seed_dual_solo_without_runtime_state(
        sessions
    )
    with sessions.begin() as session:
        dual_recipe_digest = session.scalar(
            select(CatalogDocumentRevision.content_digest).where(
                CatalogDocumentRevision.id == dual_revision_id
            )
        )
        assert dual_recipe_digest is not None
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
                plan=_installed_pair_plan(
                    mapping_id=_uuid(600),
                    build_id=_uuid(603),
                    recipe_revision_id=dual_revision_id,
                    recipe_digest=dual_recipe_digest,
                ),
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
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
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
    assert preview.summary.stops == 1
    assert set(preview.steps[0].node_ids) == {_node_id(1), _node_id(2)}
    application = service.apply(
        profile.id,
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
                last_seen_at=NOW,
            )
        )
    adapter = _SwitchAdapter()
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
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
        request_key=_uuid(801),
        actor="admin",
    )
    assert application.state == "succeeded"
    assert adapter.cancellations == []
    with sessions() as session:
        assert [
            node.workload_intent_ordinal
            for node in session.scalars(select(AgentNode).order_by(AgentNode.node_id))
        ] == [0, 0]
    assert application.result is not None
    assert application.result.changed is False
    assert application.result.completed_steps == 0
    readback = service.application(application.id)
    assert readback.result is not None
    assert readback.result.model_dump(mode="json") == {
        "changed": False,
        "completed_steps": 0,
    }


@pytest.mark.usefixtures("damaged_json_rows")
def test_all_idle_profile_supersedes_a_queued_load_without_a_run() -> None:
    sessions = _database()
    _seed(sessions)
    with sessions.begin() as session:
        node = session.get(AgentNode, _node_id(1))
        assert node is not None
        node.workload_intent_ordinal = 1
        session.add(
            Job(
                id=_uuid(820),
                request_id=_uuid(821),
                kind="recipe.run-switch.v2",
                state="queued",
                actor="admin",
                authority_revision="a" * 64,
                targets=[_node_id(1)],
                payload_digest="b" * 64,
                payload={"workload_intent_ordinal": 1},
                result={},
                created_at=NOW,
                updated_at=NOW,
            )
        )
    adapter = _SwitchAdapter()
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
    profile = service.create(
        FleetProfileInput(name="Idle", assignments=[]), actor="admin"
    )

    preview = service.preview(profile.id)

    assert [step.node_ids for step in preview.steps] == [[_node_id(1)]]
    application = service.apply(
        profile.id,
        request_key=_uuid(822),
        actor="admin",
    )
    assert application.state == "queued"
    assert adapter.cancellations == [((_node_id(1),), 2)]
    with sessions() as session:
        node = session.get(AgentNode, _node_id(1))
        assert node is not None
        assert node.workload_intent_ordinal == 2


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
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
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
    assert not isinstance(replay, Residue)
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
        assessment_provider=restarted_adapter.assess,
    )
    assert restarted_service.tick() is False
    resumed = restarted_service.application(application.id)
    assert resumed.current_operation_id == application.id
    assert resumed.progress.switch_adapter is not None
    assert resumed.progress.switch_adapter.active_operation_id == child_id


def _transfer_result(nodes: tuple[str, ...]) -> dict[str, object]:
    """Return one real Run/Switch result document carrying a transfer phase."""

    import datetime as _datetime

    from vonk_agent_protocol import DistributionObject
    from vonk_control.distribution_assignment import NodeDistributionAssignment

    node_id = nodes[0]
    archive_sha256 = "1" * 64
    assignment = NodeDistributionAssignment(
        assignment_id=_uuid(648),
        plan_digest="c" * 64,
        generation=1,
        node_id=node_id,
        expires_at=_datetime.datetime(2026, 9, 12, tzinfo=UTC),
        model_artifact_set_sha256="d" * 64,
        objects=(
            DistributionObject(name="model", sha256="e" * 64, bytes=10, kind="model"),
        ),
        oci_image_digest="sha256:" + "f" * 64,
        oci_image_config_digest="sha256:" + "9" * 64,
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
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
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
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    persisted = restarted.application(application.id)
    assert persisted.progress.step_results == completed.progress.step_results
    assert any(
        isinstance(value.result, FleetProfileSwitchChildResult)
        for value in persisted.progress.step_results.values()
    )


def test_waiting_switch_child_keeps_the_profile_running(tmp_path: Path) -> None:
    """A Run/Switch child that is waiting on its own observation is in progress.

    Background runtime-image preparation and overdue start observations park
    the child in ``waiting`` and resume it automatically. The profile used to
    fail the whole load with "Run/Switch child returned waiting".
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
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Waiting switch child",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "waiting-switch-child",
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
        request_key=_uuid(648),
        actor="admin",
    )
    assert service.tick() is True
    started = service.application(application.id)
    assert started.progress.switch_adapter is not None
    child_id = started.progress.switch_adapter.active_operation_id
    assert isinstance(child_id, str)

    with sessions.begin() as session:
        job = session.get(Job, child_id)
        assert job is not None
        job.state = "waiting"
        job.status_reason = "Runtime image preparation is running in the background"
        job.updated_at = lifecycle._clock()

    service.tick()
    waiting = service.application(application.id)
    assert waiting.state in {"queued", "running"}, waiting.status_reason
    assert waiting.progress.switch_adapter is not None
    assert waiting.progress.switch_adapter.active_operation_id == child_id

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
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
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
            sessions,
            clock=lifecycle._clock,
            switch_adapter=adapter,
            assessment_provider=adapter.assess,
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
            request_key=_uuid(646),
            actor="admin",
        )

        assert service.tick() is True
        started = service.application(application.id)
        assert started.current_operation_id == application.id
        assert started.progress.switch_adapter is not None
        child_id = started.progress.switch_adapter.active_operation_id
        assert isinstance(child_id, str)
        # The next pass reads the child under the same row lock; unchanged
        # progress is not written again.
        assert service.tick() is False
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
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
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


def test_all_idle_profile_stops_a_lost_run_that_still_has_residue(
    tmp_path: Path,
) -> None:
    """A lost run owns capacity and residue: an idle profile must still stop it.

    Wrong implementation this catches: stop selection read the active-run set,
    which excludes ``lost``, so the profile saw nothing to stop and the residue
    stayed on the Spark forever.
    """

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
        request_id=_uuid(1642),
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installation.owner_id,
        nodes,
        request_id=_uuid(1643),
    )
    with sessions.begin() as session:
        lost = session.get(RecipeRun, run.owner_id)
        assert lost is not None
        lost.state = "lost"
        assert session.scalar(select(RunNode.id).where(RunNode.run_id == lost.id))
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
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    profile = service.create(
        FleetProfileInput(name="All idle over a lost run", assignments=[]),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]

    application = service.apply(
        profile.id,
        request_key=_uuid(1644),
        actor="admin",
    )
    assert service.tick() is True
    state = service.application(application.id).progress.switch_adapter
    assert state is not None
    child_id = state.active_operation_id
    assert isinstance(child_id, str)
    child = run_switch.get(child_id)
    assert child.kind == "recipe.stop.v2"
    assert child.node_ids == list(nodes)


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
            recipe_selector="vonk-forge/synthetic-tiny-build",
            spark_ids=[_node_id(2)],
            desired_state=DesiredAssignmentState.INSTALLED,
        ),
        FleetProfileAssignmentInput(
            recipe_selector="vonk-forge/synthetic-tiny-build",
            spark_ids=[_node_id(1)],
            desired_state=DesiredAssignmentState.INSTALLED,
        ),
    ]
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids))
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
        assessment_provider=lambda *args, **_kwargs: _assessment(
            changing_observation_provider(*args)
        ),
    )
    assert (
        digest_service.preview(profile.id).plan_digest
        == digest_service.preview(profile.id).plan_digest
    )

    assert [item.assignment_id for item in preview.preparations] == sorted(
        item.assignment_id for item in preview.preparations
    )
    assert len(preview.preparations) == 2
    assert {
        item.preparation.model.artifact_set_sha256 for item in preview.preparations
    } == {preview.preparations[0].preparation.model.artifact_set_sha256}
    assert {
        item.preparation.runtime_image.image_digest for item in preview.preparations
    } == {"sha256:" + "d" * 64}
    assert {
        item.preparation.model.artifact_set_bytes for item in preview.preparations
    } == {100}


def test_profile_rejects_preparation_evidence_for_another_scope() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids)).model_copy(
                update={"preparation": _exact_preparation((_node_id(2),))}
            )
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
    assert created.assignments[0].recipe.name == "Synthetic Tiny build"
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
    assert any(
        reason.code == "profile.topology_incomplete" for reason in preview.reasons
    )


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
            "node_count": 2,
            "roles": [leader, worker],
            "parallelism": {
                "tensor": 2,
                "pipeline": 1,
                "data": 1,
                "backend": "nccl",
            },
            "start_order": ["worker", "leader"],
        }
    )
    _nested_object(document, "models", 0, "files", 0)["roles"] = ["leader", "worker"]
    RecipeDefinition.model_validate(document)
    with sessions.begin() as session:
        session.execute(
            revisions.update()
            .where(CatalogDocumentRevision.id == revision_id)
            .values(
                document=document,
                content_digest=document_sha256(document),
            )
        )
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
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
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")

    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert preview.assignments[0].current_state == "not-placed"
    assert preview.assignments[0].actions == ["switch"]
    assert [step.kind for step in preview.steps] == ["switch"]
    assert len(preview.plan_digest) == 64

    application = service.apply(
        profile.id,
        request_key=_uuid(4),
        actor="admin",
    )
    replay = service.apply(
        profile.id,
        request_key=_uuid(4),
        actor="admin",
    )
    assert application == replay
    assert application.state == "queued"
    assert application.total_steps == 1
    assert application.profile_digest == preview.profile_digest
    with sessions() as session:
        stored = session.get(FleetProfileApplication, application.id)
        assert stored is not None
        stored_plan = stored.plan
        assert stored_plan["scope"] == {
            "node_ids": [_node_id(1)],
            "idle_node_ids": [],
        }


def test_profile_apply_uses_latest_saved_profile_when_digest_is_stale() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")

    updated = service.update(
        profile.id,
        _input(revision_id, name="Studio exact").model_copy(
            update={"expected_revision": profile.revision}
        ),
        actor="admin",
    )
    assert updated.profile_digest != profile.profile_digest
    application = service.apply(profile.id, request_key=_uuid(5), actor="admin")
    assert application.profile_digest == updated.profile_digest
    assert application.state == "queued"


@pytest.mark.parametrize("compiled_port", ["authorised", "recipe-declared"])
def test_profile_scope_reconciles_idle_member_and_retains_reusable_installation(
    compiled_port: str,
) -> None:
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
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                last_seen_at=NOW,
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
                content_digest=document_sha256(solo_document),
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
                    id=_uuid(11),
                    mapping_id=_uuid(10),
                    node_id=_node_id(1),
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    created_at=NOW,
                ),
                ClusterMappingNode(
                    id=_uuid(12),
                    mapping_id=_uuid(10),
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
                id=_uuid(13),
                recipe_revision_id=dual_revision_id,
                builder_node_id=_node_id(1),
                source_bundle_sha256="b" * 64,
                build_input_sha256="c" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=f"sha256:{'d' * 64}",
                oci_layout_sha256="e" * 64,
                image_bytes=1024,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            RecipeInstallation(
                id=_uuid(14),
                recipe_revision_id=dual_revision_id,
                mapping_id=_uuid(10),
                mapping_generation=1,
                recipe_build_id=_uuid(13),
                image_digest=f"sha256:{'d' * 64}",
                plan_digest="f" * 64,
                plan=_installed_pair_plan(
                    mapping_id=_uuid(10),
                    build_id=_uuid(13),
                    recipe_revision_id=dual_revision_id,
                    recipe_digest=document_sha256(dual_document),
                ),
                state="installed",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add_all(
            [
                InstallationNode(
                    id=_uuid(15),
                    installation_id=_uuid(14),
                    node_id=_node_id(1),
                    rank=0,
                    role="entrypoint",
                    state="installed",
                    required_bytes=1,
                    installed_bytes=1,
                    updated_at=NOW,
                ),
                InstallationNode(
                    id=_uuid(16),
                    installation_id=_uuid(14),
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
                id=_uuid(17),
                installation_id=_uuid(14),
                mapping_id=_uuid(10),
                mapping_generation=1,
                alias="dual-chat",
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
                    id=_uuid(18),
                    run_id=_uuid(17),
                    node_id=_node_id(1),
                    rank=0,
                    role="entrypoint",
                    state="running",
                    port=8000,
                    reserved_memory_bytes=1,
                    updated_at=NOW,
                ),
                RunNode(
                    id=_uuid(19),
                    run_id=_uuid(17),
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

    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile_a = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Dual",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
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
                "name": "Solo and idle",
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
    if compiled_port == "recipe-declared":
        # Wrong implementation this catches: the load reused an installation
        # compiled for a port the firewall refuses, so the start failed again
        # and the recipe could never be loaded without removing it by hand.
        with sessions.begin() as session:
            stale = session.get(RecipeInstallation, _uuid(14))
            assert stale is not None
            plan = deepcopy(stale.plan)
            for compiled in _nested_object(plan, "compiled_execution_plans").values():
                _nested_object(compiled, "endpoint")["port"] = 8000
            stale.plan = plan
        reload = service.preview(profile_a.id)
        assert reload.summary.installs == 1
        return
    no_op = service.preview(profile_a.id)
    assert no_op.steps == []
    retained = service.apply(
        profile_a.id,
        request_key=_uuid(802),
        actor="admin",
    )
    assert retained.state == "succeeded"
    with sessions() as session:
        assert [
            node.workload_intent_ordinal
            for node in session.scalars(select(AgentNode).order_by(AgentNode.node_id))
        ] == [0, 0]
    switch_to_b = service.preview(profile_b.id)
    assert switch_to_b.scope.node_ids == [_node_id(1), _node_id(2)]
    assert switch_to_b.scope.idle_node_ids == [_node_id(2)]
    assert [step.kind for step in switch_to_b.steps] == ["switch"]
    assert switch_to_b.scope.idle_node_ids == [_node_id(2)]
    assert switch_to_b.summary.uninstalls == 0

    with sessions.begin() as session:
        run = session.get(RecipeRun, _uuid(17))
        assert run is not None
        run.state = "stopped"
        run.route_state = "withdrawn"
    back_to_a = service.preview(profile_a.id)
    assert [step.kind for step in back_to_a.steps] == ["switch"]
    assert back_to_a.summary.installs == 0


@pytest.mark.parametrize(
    "damage", ["numeric-variant", "missing-spark-ids", "extra", "invalid-root"]
)
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
    # The damaged choice is retired (skipped), never a refusal to read the profile.
    assert service.get(created.id).assignments == []


def test_profile_preview_blocks_when_required_preparation_cannot_be_attested() -> None:
    """A plan that must prepare assets is blocked when they cannot be attested.

    ``AGENTS.md`` requires missing model or recipe-image assets to be actionable
    blockers, so a profile that still has to place its exact assets must not be
    admitted while the Controller cannot project that evidence.
    """

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)

    def _unavailable(_session, _assignment, _node_ids, **_kwargs):
        raise ValueError(
            "The Run/Switch authority cannot attest exact model and OCI "
            "preparation evidence (run-switch.install-preparation-unavailable)."
        )

    service = FleetProfileService(
        sessions, clock=lambda: NOW, assessment_provider=_unavailable
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
    application = service.apply(
        profile.id,
        request_key=_uuid(41),
        actor="admin",
    )
    assert application.state == "queued"
    assert "Next attempt" in (application.status_reason or "")


def test_profile_load_with_missing_preparation_enqueues_it_and_waits() -> None:
    """A load asks for the model and image it lacks instead of stopping there."""

    from vonk_control.operation_blockers import make_blocker

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)

    def _missing(_session, _assignment, _node_ids, **_kwargs):
        raise ValueError("no successful immutable runtime image build is available")

    requested: list[str] = []

    def _starter(
        recipe_revision_id: str, *, actor: str, application_id: str | None = None
    ):
        requested.append(recipe_revision_id)
        return [
            make_blocker(
                "recipe_image.preparing",
                "Preparing the model and runtime image (prepare).",
                severity="info",
            )
        ]

    service = FleetProfileService(
        sessions, clock=lambda: NOW, assessment_provider=_missing
    )
    service.bind_preparation_starter(_starter)
    profile = service.create(_input(revision_id), actor="admin")

    # The review lists what the Controller will prepare.
    preview = service.preview(profile.id)
    assert preview.allowed is False
    assert [step.kind for step in preview.preparation_steps] == ["prepare", "prepare"]
    assert "Download the model" in preview.preparation_steps[0].label
    assert "Build the runtime image" in preview.preparation_steps[1].label

    application = service.apply(profile.id, request_key=_uuid(42), actor="admin")

    assert requested == [revision_id]
    assert application.state == "queued"  # waiting for the preparation, not blocked
    codes = {item.code for item in application.blockers}
    assert "recipe_image.preparing" in codes
    assert application.next_attempt_at is not None


def test_profile_preview_projects_exact_preparation_from_run_switch_authority(
    tmp_path: Path,
) -> None:
    """Regression: the production composition must project real preparation.

    Production binds ``RunSwitchFleetProfileAdapter.assess`` as the
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


def test_child_operation_state_unknown_is_observed_never_refused() -> None:
    """A stored state outside the contract is unknown, and unknown is observed.

    The caller's default is the observing state (``running``): a malformed value
    never refuses the read and never reads as a terminal state it did not reach.
    """

    assert _operation_state(None, default=LifecycleState.RUNNING) == "running"
    assert _operation_state("succeeded", default=LifecycleState.QUEUED) == "succeeded"
    assert _operation_state("not-a-state", default=LifecycleState.RUNNING) == "running"
    assert _operation_state(7, default=LifecycleState.RUNNING) == "running"


def _exact_cleanup_profile(tmp_path: Path, *, engine=None):
    """A production-wired profile that removes an unlisted installation."""

    from vonk_control.run_switch_operations import RunSwitchOperationService

    from .test_recipe_operations import installed_recipe, setup_services
    from .test_run_switch_operations import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=engine
    )
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
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
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Exact cleanup",
                "installation_policy": "exact",
                "assignments": [],
            }
        ),
        actor="admin",
    )
    return sessions, lifecycle, adapter, service, profile, installed, nodes


def test_switch_queue_removes_an_installation_the_profile_no_longer_references(
    tmp_path: Path,
) -> None:
    """The orchestrator, not the profile layer, performs the removal."""

    sessions, _lifecycle, adapter, service, profile, installed, nodes = (
        _exact_cleanup_profile(tmp_path)
    )
    reviewed = service.preview(profile.id)
    with sessions() as session:
        exact = adapter._plan_queue(
            session,
            (),
            tuple(nodes),
            application_id=_uuid(1235),
            installation_policy="exact",
            reviewed_effects=reviewed.effects,
            expected_images={},
        )
        retained = adapter._plan_queue(
            session,
            (),
            tuple(nodes),
            application_id=_uuid(1236),
            installation_policy="keep-cached",
            reviewed_effects=reviewed.effects,
            expected_images={},
        )

    assert {"kind": "cleanup", "id": installed.owner_id} in exact
    # Retention decides whether it is removed at all: keep-cached retains it.
    assert retained == []


def test_profile_preview_delegates_removal_to_the_orchestrator(
    tmp_path: Path,
) -> None:
    """The profile states the intent and keeps it visible; Run/Switch removes it."""

    _sessions, _lifecycle, _adapter, service, profile, installed, _nodes = (
        _exact_cleanup_profile(tmp_path)
    )

    preview = service.preview(profile.id)

    assert [step.kind for step in preview.steps] == ["switch"]
    delegated = [
        reason
        for reason in preview.reasons
        if reason.code == "profile.cleanup_delegated"
    ]
    assert delegated, [reason.code for reason in preview.reasons]
    assert installed.owner_id in delegated[0].detail
    assert preview.summary.starts == 0
    assert preview.summary.uninstalls == 1


def _exact_planned_cleanup_profile(tmp_path: Path):
    """A production-wired exact profile whose only leftover is a persisted plan."""

    from vonk_control.run_switch_operations import RunSwitchOperationService

    from .test_recipe_operations import setup_services
    from .test_run_switch_operations import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    admission = lifecycle._install_admission
    plan = admission.plan_install(mapping_id, build_id, now=lifecycle._clock())
    assert plan.allowed, [
        (node.node_id, reason.code) for node in plan.nodes for reason in node.blockers
    ]
    planned = admission.accept_install(plan, actor="admin", now=lifecycle._clock())
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
            name="Exact planned cleanup",
            installation_policy="exact",
            assignments=[],
        ),
        actor="admin",
    )
    return sessions, lifecycle, adapter, run_switch, service, profile, planned, nodes


def test_exact_profile_switch_abandons_a_never_installed_leftover(
    tmp_path: Path,
) -> None:
    """A switch must not be blocked by a plan that was never installed.

    The cleanup candidate set still hands the superseded installation to
    Run/Switch, but the installation's own assessment resolves it as an
    abandonment, so the switch is admitted and the leftover is disposed of
    without node work.  Before the disposition existed this exact load failed
    with ``run-switch.plan_blocked: run-switch.uninstall-blocked``.
    """

    sessions, _lifecycle, _adapter, run_switch, service, profile, planned, _nodes = (
        _exact_planned_cleanup_profile(tmp_path)
    )

    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]

    application = service.apply(
        profile.id,
        request_key=_uuid(910),
        actor="admin",
    )
    for _ in range(12):
        run_switch.tick()
        service.tick()
        if service.application(application.id).state in {"succeeded", "failed"}:
            break

    completed = service.application(application.id)
    assert completed.state == "succeeded", completed.status_reason
    # The profile application keeps the Run/Switch receipt, so the operator
    # sees the abandonment and its reason rather than a silent disappearance.
    from vonk_control.run_switch_contract import RunSwitchUninstallResult

    receipts = [
        item.result.run_switch
        for item in completed.progress.step_results.values()
        if isinstance(item.result, FleetProfileSwitchChildResult)
    ]
    dispositions = [
        phase
        for receipt in receipts
        for phase in receipt.phase_results
        if isinstance(phase, RunSwitchUninstallResult)
    ]
    assert [phase.disposition for phase in dispositions] == ["abandoned"]
    assert [phase.reason for phase in dispositions] == ["installation-not-installed"]
    with sessions() as session:
        installation = session.get(RecipeInstallation, planned)
        assert installation is not None and installation.state == "uninstalled"
        assert all(
            node.state == "uninstalled"
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == planned
                )
            )
        )


def test_acceptance_cleanup_consumer_reads_the_complete_run_switch_result(
    tmp_path: Path,
    postgres_engine: Engine,
) -> None:
    """The packaged acceptance consumer follows delegated cleanup evidence."""

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

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=1, engine=postgres_engine
    )
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=_uuid(900)
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installation.owner_id,
        nodes,
        request_id=_uuid(901),
        alias="acceptance-cleanup",
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
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    profile = service.create(
        FleetProfileInput(
            name="Acceptance exact cleanup",
            installation_policy="exact",
            assignments=[],
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    application = service.apply(
        profile.id,
        request_key=_uuid(902),
        actor="admin",
    )
    completed_agent_operations: set[str] = set()
    for _ in range(30):
        run_switch.tick()
        service.tick()
        current = service.application(application.id)
        if current.state == "succeeded":
            break
        state = current.progress.switch_adapter
        assert state is not None
        active_id = state.active_operation_id
        if active_id is None:
            continue
        child = run_switch.get(active_id)
        operation_id = child.result.child_operation_id if child.result else None
        if operation_id is None or operation_id in completed_agent_operations:
            continue
        with sessions() as session:
            operation = session.get(Job, operation_id)
            assert operation is not None
            kind = operation.kind
        assert kind in {"recipe.stop", "recipe.uninstall"}
        lifecycle.record_node_result(
            operation_id, nodes[0], succeeded=True, evidence={}
        )
        completed_agent_operations.add(operation_id)
    else:
        pytest.fail(
            "profile cleanup did not converge: "
            + json.dumps(current.model_dump(mode="json"), default=str)
        )

    completed = service.application(application.id)
    payload = {
        "preview": preview.model_dump(mode="json"),
        "application": completed.model_dump(mode="json"),
        "installation_id": installation.owner_id,
        "run_id": run.owner_id,
        "node_id": nodes[0],
    }
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import copy
import json
import runpy
import sys

m = runpy.run_path("tests/acceptance/test_spark_lifecycle.py")
value = json.load(sys.stdin)
m["_validate_canary_cleanup_preview"](
    value["preview"], node_id=value["node_id"]
)
m["_validate_canary_cleanup_application"](
    value["application"],
    installation_ids=[value["installation_id"]],
    run_id=value["run_id"],
)

def rejected(changed):
    try:
        m["_validate_canary_cleanup_application"](
            changed,
            installation_ids=[value["installation_id"]],
            run_id=value["run_id"],
        )
    except m["LifecycleError"]:
        return
    raise AssertionError("malformed cleanup evidence was accepted")

missing = copy.deepcopy(value["application"])
missing["progress"]["switch_adapter"]["result"]["children"].pop()
rejected(missing)
wrong_run = copy.deepcopy(value["application"])
children = wrong_run["progress"]["switch_adapter"]["result"]["children"]
stop = next(child for child in children if child["kind"] == "stop")
for receipt in stop["result"]["run_switch"]["phase_results"]:
    if receipt.get("phase") == "stop":
        receipt["run_id"] = "00000000-0000-4000-8000-000000000998"
rejected(wrong_run)
wrong = copy.deepcopy(value["application"])
children = wrong["progress"]["switch_adapter"]["result"]["children"]
cleanup = next(child for child in children if child["kind"] == "cleanup")
for receipt in cleanup["result"]["run_switch"]["phase_results"]:
    if "installation_id" in receipt:
        receipt["installation_id"] = "00000000-0000-4000-8000-000000000999"
rejected(wrong)
unverified = copy.deepcopy(value["application"])
children = unverified["progress"]["switch_adapter"]["result"]["children"]
cleanup = next(child for child in children if child["kind"] == "cleanup")
final = next(
    receipt for receipt in cleanup["result"]["run_switch"]["phase_results"]
    if receipt["phase"] == "final_verify"
)
final["final_verified"] = False
rejected(unverified)
""",
        ],
        cwd=Path(__file__).parents[2],
        input=json.dumps(payload),
        text=True,
        check=True,
    )


def test_persisted_child_progress_is_read_with_json_semantics() -> None:
    """Persisted JSON must be validated as JSON, not as Python objects.

    The database driver hands back decoded JSON, and the profile's own progress
    document stores ``start_deadline`` as the ISO string it serialized.  Reading
    that document in Python mode rejected it, and on the live fleet that failed
    a whole profile application after the distribution and install had already
    succeeded: "FleetProfileChildProgress -> start_deadline -> Input should be a
    valid datetime".
    """

    state = {
        "state": "running",
        "child_progress": {
            "phase": "start",
            "node_ids": [_node_id(1)],
            "startup_budget_seconds": 1800,
            "start_deadline": "2026-09-17T08:48:21.262460Z",
        },
    }

    view = RunSwitchFleetProfileAdapter._view_from_state(
        cast("FleetProfileApplication", SimpleNamespace(id=_uuid(900))), state
    )

    progress = view.progress
    assert progress is not None
    assert progress.phase == "start"
    start_deadline = progress.start_deadline
    assert start_deadline is not None
    assert start_deadline.year == 2026
    assert start_deadline.microsecond == 262460


def test_a_replaced_workload_keeps_serving_until_its_successor_starts() -> None:
    """Wrong implementation: every stop ran first, so loading a new revision of
    a running workload took it down for the successor's whole preparation."""

    from types import SimpleNamespace

    from vonk_control.fleet_profiles import _switch_queue

    def stop(run_id: str, *nodes: int):
        return SimpleNamespace(
            run_id=run_id,
            node_ids=[_node_id(n) for n in nodes],
            profile_stop_scope=None,
        )

    def assignment(assignment_id: str, *nodes: int, state: str = "running"):
        return SimpleNamespace(
            id=assignment_id,
            nodes=[SimpleNamespace(node_id=_node_id(n)) for n in nodes],
            desired_state=state,
        )

    # The ordering reads only identities and Spark sets, so plain records
    # stand in for the full effect and assignment models.
    queue = _switch_queue(
        cast(Any, [stop("old-glm", 1, 2), stop("spread", 3, 4)]),
        cast(
            Any,
            [
                assignment("new-glm", 1, 2),
                assignment("solo-three", 3),
                assignment("solo-four", 4),
            ],
        ),
        ["leftover"],
    )

    assert [(item["kind"], item["id"]) for item in queue] == [
        # Two new runs take the spread workload's Sparks separately; neither
        # one alone can stop it, so it stops first.
        ("stop", "spread"),
        ("run", "new-glm"),
        ("run", "solo-three"),
        ("run", "solo-four"),
        # The new GLM's own Run/Switch stops the old one right before start.
        ("stop", "old-glm"),
        ("cleanup", "leftover"),
    ]


def test_a_stop_step_for_an_already_replaced_workload_is_skipped(
    tmp_path: Path,
) -> None:
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
        lifecycle, mapping_id, build_id, nodes, request_id=_uuid(652)
    )
    run = started_recipe(
        sessions, lifecycle, installation.owner_id, nodes, request_id=_uuid(653)
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
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    profile = service.create(
        FleetProfileInput(name="All idle", assignments=[]), actor="admin"
    )
    service.preview(profile.id)
    application = service.apply(profile.id, request_key=_uuid(654), actor="admin")
    # Its replacement already stopped it before this step was reached.
    with sessions.begin() as session:
        stored = session.get(RecipeRun, run.owner_id)
        assert stored is not None
        stored.state = "stopped"

    service.tick()
    service.tick()

    current = service.application(application.id)
    state = current.progress.switch_adapter
    assert state is not None and state.active_operation_id is None
    assert current.state == "succeeded", current.status_reason


def test_every_application_state_is_active_or_named_ended() -> None:
    """A new terminal state must join the one ended-state set every consumer uses."""

    from typing import get_args

    from vonk_control.fleet_profile_contract import (
        FLEET_PROFILE_ENDED_STATES,
        FleetProfileOperationState,
    )

    active = {"queued", "running", "needs-operator"}
    literal = next(
        arg for arg in get_args(FleetProfileOperationState) if hasattr(arg, "__args__")
    )
    assert {str(state) for state in get_args(literal)} == active | set(
        FLEET_PROFILE_ENDED_STATES
    )


def test_a_superseded_application_names_its_reason_and_never_a_failure() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    view = service.apply(profile.id, request_key=_uuid(981), actor="admin")
    ended = view.model_copy(update={"state": "superseded"})
    with pytest.raises(ValueError, match="reason code"):
        type(view).model_validate(ended.model_dump())
    named = ended.model_copy(
        update={
            "reason_code": SupersedeCode.SUPERSEDED_BY_RETRY,
            "superseded_by": _uuid(982),
        }
    )
    type(view).model_validate(named.model_dump())
    # A retry supersession must name its successor; others may not name a failure.
    with pytest.raises(ValueError, match="successor"):
        type(view).model_validate(
            named.model_copy(update={"superseded_by": None}).model_dump()
        )
    with pytest.raises(ValueError, match="only a superseded"):
        type(view).model_validate(
            view.model_copy(update={"superseded_by": _uuid(982)}).model_dump()
        )


@pytest.mark.parametrize(
    "end_path", ["admission", "cache-recovery", "conflict-recovery"]
)
def test_exhausted_preparation_ends_and_a_fresh_load_is_admitted(
    monkeypatch, end_path: str
) -> None:
    """An exhausted chain must release its application scope, not poison new loads."""
    from vonk_agent_protocol import RecipeImageCode, ReservationState
    from vonk_control.models import FleetProfileApplication
    from vonk_control.operation_blockers import make_blocker

    from .non_blocking import assert_ended_without_blocking

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    scopes: list[str | None] = []

    def missing(_session, _assignment, _node_ids, **_kwargs):
        raise ValueError("no successful immutable runtime image build is available")

    def prepare(
        recipe_revision_id: str, *, actor: str, application_id: str | None = None
    ):
        scopes.append(application_id)
        code = (
            RecipeImageCode.PREPARATION_EXHAUSTED
            if len(scopes) == (1 if end_path == "admission" else 2)
            else RecipeImageCode.PREPARING
        )
        # A bounded UI projection must retain the ending reason even when many
        # missing assets precede it; otherwise a restart would retry this end.
        noise = [
            make_blocker(
                RecipeImageCode.PREPARING,
                "Another asset is preparing",
                severity="error",
                node_ids=[_uuid(1000 + index)],
            )
            for index in range(32)
        ]
        return [
            *noise,
            make_blocker(code, "Bounded preparation result", severity="error"),
        ]

    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        assessment_provider=missing,
        switch_adapter=_SwitchAdapter(),
    )
    service.bind_preparation_starter(prepare)
    profile = service.create(_input(revision_id), actor="admin")
    ended = service.apply(profile.id, request_key=_uuid(920), actor="admin")
    if end_path != "admission":
        from vonk_control import fleet_profiles as fp

        for method in (
            "_reconcile_selected_roster",
            "_observe_pending_admissions",
            "_heal_legacy_applications",
            "_observe_pending_cancellation",
        ):
            monkeypatch.setattr(service, method, lambda now: False)
        monkeypatch.setattr(
            service, "_automatic_profile_recovery", lambda now: (ended.id, "admin")
        )
        monkeypatch.setattr(service, "_retry_eligible", lambda session, row: True)
        error = (
            fp._FleetProfileRecoveryBindingConflict
            if end_path == "cache-recovery"
            else fp.FleetProfileAdmissionEffectBusy
        )

        def interrupted(*args, **kwargs):
            raise error("missing assets during automatic recovery")

        monkeypatch.setattr(service, "retry", interrupted)
        assert service.tick()
        ended = service.application(ended.id)

    def released() -> None:
        with sessions() as session:
            row = session.get(FleetProfileApplication, ended.id)
            assert row is not None
            assert not row.progress["admission_pending"]
            assert row.progress.get("admission_retry_at") is None
            assert row.progress.get("retry_due_at") is None
            assert row.current_operation_id is None
            from sqlalchemy import select
            from vonk_control.models import ResourceReservation

            assert (
                list(
                    session.scalars(
                        select(ResourceReservation).where(
                            ResourceReservation.owner_kind == "fleet-profile",
                            ResourceReservation.owner_id == ended.id,
                            ResourceReservation.state.in_(
                                (ReservationState.ACTIVE, ReservationState.PROMISED)
                            ),
                        )
                    )
                )
                == []
            )
        assert ended.next_attempt_at is None

    ended, fresh = assert_ended_without_blocking(
        None,
        ended,
        end=lambda view: view,
        assert_released=released,
        fresh=lambda _: service.apply(
            profile.id, request_key=_uuid(921), actor="admin"
        ),
    )
    assert RecipeImageCode.PREPARATION_EXHAUSTED in {b.code for b in ended.blockers}
    assert scopes == [ended.id] * (1 if end_path == "admission" else 2) + [fresh.id]
    assert fresh.id != ended.id
