from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, Table, select, update
from vonk_agent_protocol import (
    DesiredAssignmentState,
    LifecycleState,
    ProfileReasonCode,
    SupersedeCode,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_control.fleet_profile_contract import (
    FleetProfileAssignment,
    FleetProfileInput,
    FleetProfileNode,
    FleetProfileRunEffect,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildResult,
    FleetProfileSwitchQueueItem,
    FleetProfileView,
)
from vonk_control.fleet_profiles import (
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    _operation_state,
    build_production_fleet_profile_service,
)
from vonk_control.models import (
    AgentNode,
    AgentNodeContact,
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
from vonk_control.run_switch_contract import (
    RunSwitchAssessment,
)
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .fleet_profiles_support import (
    NOW,
    _assessment,
    _database,
    _exact_preparation,
    _follow_profile_retry,
    _input,
    _installed_pair_plan,
    _nested_object,
    _node_id,
    _recipe_document,
    _seed,
    _SwitchAdapter,
    _uuid,
)
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture


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
        solo_document = _recipe_document()
        _nested_object(solo_document, "identity")["slug"] = "synthetic-tiny-solo"
        solo_recipe = RecipeDefinition.model_validate(solo_document)
        solo_root_id = _uuid(4)
        session.add(
            CatalogDocument(
                id=solo_root_id,
                kind="recipe",
                publisher=solo_recipe.identity.publisher,
                slug=solo_recipe.identity.slug,
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
    assert service.get(created.id).definition == created.definition
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
    from vonk_control.fleet_profile_contract import UnavailableFleetProfileView

    # Keep the exact saved identity observable. Dropping a malformed choice
    # would let an ordinary edit silently delete accepted authoring intent.
    observed = service.read_number(created.number)
    if damage == "extra":
        # The column owner adopts retired fields on reads. Known intent stays
        # visible, and observing it never rewrites the stored document.
        assert isinstance(observed, FleetProfileView)
        assert observed.definition == created.definition
        with sessions() as session:
            row = session.get(FleetProfile, created.id)
            assert row is not None and row.assignments == assignments
        return
    assert isinstance(observed, UnavailableFleetProfileView)
    assert observed.id == created.id and observed.revision == created.revision
    assert observed.definition is None
    with sessions() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None and row.assignments == assignments


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
    application = service.apply(
        profile.id,
        request_key=_uuid(41),
        actor="admin",
    )
    assert application.state == "queued"


def test_profile_load_with_missing_preparation_enqueues_it_and_waits() -> None:
    """A load asks for the model and image it lacks instead of stopping there."""

    from vonk_control.operations.blockers import make_blocker

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

    application = service.apply(profile.id, request_key=_uuid(42), actor="admin")

    assert requested == [revision_id]
    assert application.state == "queued"  # waiting for the preparation, not blocked
    assert application.next_attempt_at is not None


def test_load_waiting_on_a_working_preparation_outlives_its_wait_window() -> None:
    """Catches the 30-minute acceptance budget ending a load whose model and
    image are still being prepared; it ends only when its preparation stalls,
    with that evidence."""

    from datetime import timedelta

    from vonk_agent_protocol import RecipeImageCode
    from vonk_control.fleet_profile_contract import FLEET_PROFILE_ENDED_STATES
    from vonk_control.operations.blockers import make_blocker

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = [NOW]
    working = [True]

    def _missing(_session, _assignment, _node_ids, **_kwargs):
        raise ValueError("no successful immutable runtime image build is available")

    def _starter(
        recipe_revision_id: str, *, actor: str, application_id: str | None = None
    ):
        if not working[0]:
            # The preparation's own stall window ended it.
            return [
                make_blocker(
                    RecipeImageCode.PREPARATION_EXHAUSTED,
                    "Preparation made no progress for its stall window.",
                    severity="error",
                )
            ]
        return [
            make_blocker(
                "recipe_image.preparing",
                "Preparing the model and runtime image (download).",
                severity="info",
            )
        ]

    def _progressed(recipe_revision_id: str, *, application_id: str | None = None):
        return now[0] if working[0] else None

    service = FleetProfileService(
        sessions, clock=lambda: now[0], assessment_provider=_missing
    )
    service.bind_preparation_starter(_starter, progress=_progressed)
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(profile.id, request_key=_uuid(43), actor="admin")
    window = timedelta(minutes=30)
    for _ in range(10):  # three and a third old windows of a slow download
        now[0] += window / 3
        service._observe_pending_admissions(now[0])
    assert service.application(application.id).state not in FLEET_PROFILE_ENDED_STATES
    working[0] = False  # the preparation stalled
    for _ in range(4):
        now[0] += window / 3
        service._observe_pending_admissions(now[0])
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        assert row.state in FLEET_PROFILE_ENDED_STATES
        assert "no progress" in (row.status_reason or "")


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

    from .recipe_operations_support import setup_services
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

    from .recipe_operations_support import installed_recipe, setup_services
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
        exact, _ = adapter._plan_queue(
            session,
            (),
            tuple(nodes),
            application_id=_uuid(1235),
            installation_policy="exact",
            reviewed_effects=reviewed.effects,
            expected_images={},
        )
        retained, _ = adapter._plan_queue(
            session,
            (),
            tuple(nodes),
            application_id=_uuid(1236),
            installation_policy="keep-cached",
            reviewed_effects=reviewed.effects,
            expected_images={},
        )

    assert FleetProfileSwitchQueueItem(kind="cleanup", id=installed.owner_id) in exact
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

    from .recipe_operations_support import setup_services
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

    sessions, lifecycle, _adapter, run_switch, service, profile, planned, _nodes = (
        _exact_planned_cleanup_profile(tmp_path)
    )

    now = [lifecycle._clock()]
    lifecycle._clock = lambda: now[0]
    service._clock = lifecycle._clock
    run_switch._clock = lifecycle._clock

    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]

    application = service.apply(
        profile.id,
        request_key=_uuid(910),
        actor="admin",
    )
    for _ in range(12):
        _follow_profile_retry(service, application.id, now)
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

    from .recipe_operations_support import (
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
    now = [lifecycle._clock()]
    lifecycle._clock = lambda: now[0]
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
        _follow_profile_retry(service, application.id, now)
        run_switch.tick()
        service.tick()
        current = service.application(application.id)
        if current.state == "succeeded":
            break
        state = current.progress.switch_adapter
        assert state is not None
        for pending_child in state.pending_children:
            assert pending_child.kind == state.queue[pending_child.queue_index].kind
            child = run_switch.get(pending_child.operation_id)
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

    state = FleetProfileSwitchAdapterState.model_validate_json(
        json.dumps(
            {
                "child_id": _uuid(900),
                "scope_node_ids": [_node_id(1)],
                "assignment_ids": [],
                "assignments": [],
                "queue": [],
                "actor": "admin",
                "request_id": _uuid(901),
                "state": "running",
                "child_progress": {
                    "phase": "start",
                    "node_ids": [_node_id(1)],
                    "startup_budget_seconds": 1800,
                    "start_deadline": "2026-09-17T08:48:21.262460Z",
                },
            }
        )
    )

    view = RunSwitchFleetProfileAdapter._view_from_state(
        FleetProfileApplication(id=_uuid(900), wait_evidence=None, next_action_at=None),
        state,
    )

    progress = view.progress
    assert progress is not None
    assert progress.phase == "start"
    start_deadline = progress.start_deadline
    assert start_deadline is not None
    assert start_deadline.year == 2026
    assert start_deadline.microsecond == 262460


def test_a_replaced_workload_keeps_serving_until_its_successor_starts() -> None:
    """A replacement must prepare before stopping the exact old workload."""
    from vonk_control.fleet_profiles import _switch_queue

    identities = {
        name: _uuid(19890 + index)
        for index, name in enumerate(
            ("old-glm", "spread", "new-glm", "solo-three", "solo-four", "leftover")
        )
    }

    def stop(name: str, *nodes: int) -> FleetProfileRunEffect:
        return FleetProfileRunEffect(
            run_id=identities[name],
            installation_id=_uuid(19899),
            alias=name,
            node_ids=[_node_id(node) for node in nodes],
            action="stop",
        )

    def assignment(name: str, *nodes: int) -> FleetProfileAssignment:
        return FleetProfileAssignment(
            id=identities[name],
            recipe_revision_id=_uuid(19898),
            recipe_id=_uuid(19897),
            recipe_title=name,
            topology_name="full-reviewed-group",
            desired_state=DesiredAssignmentState.RUNNING,
            alias=name,
            nodes=[
                FleetProfileNode(
                    node_id=_node_id(node),
                    rank=index,
                    role=f"rank-{index}",
                    endpoint_owner=index == 0,
                )
                for index, node in enumerate(nodes)
            ],
        )

    queue = _switch_queue(
        [stop("old-glm", 1, 2), stop("spread", 3, 4)],
        [
            assignment("new-glm", 1, 2),
            assignment("solo-three", 3),
            assignment("solo-four", 4),
        ],
        [identities["leftover"]],
    )
    assert [(item.kind, item.id) for item in queue] == [
        # Neither single-node successor alone replaces the old two-node gang.
        ("stop", identities["spread"]),
        ("run", identities["new-glm"]),
        ("run", identities["solo-three"]),
        ("run", identities["solo-four"]),
        # This successor keeps the complete old group serving while preparing.
        ("stop", identities["old-glm"]),
        ("cleanup", identities["leftover"]),
    ]


def test_a_stop_step_for_an_already_replaced_workload_is_skipped(
    tmp_path: Path,
) -> None:
    from vonk_control.run_switch_operations import RunSwitchOperationService

    from .recipe_operations_support import (
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
    assert state is not None and not state.pending_children
    assert current.state == "succeeded", current.status_reason


def test_every_application_state_is_active_or_named_ended() -> None:
    """A new terminal state must join the one ended-state set every consumer uses."""

    from typing import get_args

    from vonk_control.fleet_profile_contract import (
        FLEET_PROFILE_ENDED_STATES,
        FleetProfileOperationState,
    )

    active = {"queued", "running", "backoff"}
    assert {
        str(state) for state in get_args(FleetProfileOperationState)
    } == active | set(FLEET_PROFILE_ENDED_STATES)


def test_a_superseded_application_names_its_reason_and_never_a_failure() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    view = service.apply(profile.id, request_key=_uuid(981), actor="admin")
    ended = view.model_copy(update={"state": "superseded"})
    with pytest.raises(ValueError):
        type(view).model_validate(ended.model_dump())
    named = ended.model_copy(
        update={
            "reason_code": SupersedeCode.SUPERSEDED_BY_RETRY,
            "superseded_by": _uuid(982),
        }
    )
    type(view).model_validate(named.model_dump())
    # A retry supersession must name its successor; others may not name a failure.
    with pytest.raises(ValueError):
        type(view).model_validate(
            named.model_copy(update={"superseded_by": None}).model_dump()
        )
    with pytest.raises(ValueError):
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
    from vonk_control.operations.blockers import make_blocker

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

        # Automatic recovery scans backing-off loads, rather than pending admission.
        with sessions.begin() as session:
            row = session.get(FleetProfileApplication, ended.id)
            assert row is not None
            row.state = LifecycleState.BACKOFF

        for method in (
            "_reconcile_selected_roster",
            "_observe_pending_admissions",
            "_observe_pending_cancellation",
        ):
            monkeypatch.setattr(service, method, lambda now, application_id=None: False)
        monkeypatch.setattr(
            service,
            "_automatic_profile_recovery",
            lambda now, application_id=None: (ended.id, "admin"),
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
    assert scopes == [ended.id] * (1 if end_path == "admission" else 2) + [fresh.id]
    assert fresh.id != ended.id


@pytest.mark.parametrize("initially_waiting", [False, True])
def test_planner_validation_defect_ends_load_with_evidence_after_restart(
    initially_waiting: bool,
) -> None:
    """Catch internal contract errors being requeued as missing cache evidence."""
    from types import SimpleNamespace

    from .non_blocking import assert_ended_without_blocking

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    inject_defect = not initially_waiting
    cache_missing = initially_waiting
    calls: list[str] = []
    clock = [NOW]

    def assess(_session, _assignment, _nodes, **_kwargs):
        calls.append(_assignment.id)
        valid = _assessment(_exact_preparation((_node_id(1),)))
        if inject_defect:
            return RunSwitchAssessment.model_validate(
                {**valid.model_dump(), "allowed": False}
            )
        if cache_missing:
            raise ValueError("runtime image is still preparing")
        return valid

    def service():
        return FleetProfileService(
            sessions,
            clock=lambda: clock[0],
            assessment_provider=assess,
            switch_adapter=_SwitchAdapter(),
        )

    owner = service()
    profile = owner.create(_input(revision_id), actor="admin")
    application = owner.apply(profile.id, request_key=_uuid(930), actor="admin")
    if initially_waiting:
        assert application.state == LifecycleState.QUEUED
        inject_defect = True
        clock[0] += timedelta(minutes=1)
        owner = service()
        assert owner.tick()
        application = owner.application(application.id)
    assert len(calls) == (2 if initially_waiting else 1)
    assert application.state == LifecycleState.FAILED
    reason = next(
        item
        for item in application.progress.blockers
        if item.code == ProfileReasonCode.ADMISSION_DEFECT
    )
    assert "RunSwitchAssessment" in reason.detail
    assert "admission verdict" in reason.detail
    assert application.next_attempt_at is None
    observed_calls = len(calls)
    restarted = service()
    for _ in range(3):
        restarted.tick()
    assert len(calls) == observed_calls
    assert restarted.application(application.id).state == LifecycleState.FAILED
    inject_defect = cache_missing = False

    def assert_defect(view) -> None:
        assert any(
            item.code == ProfileReasonCode.ADMISSION_DEFECT
            for item in view.progress.blockers
        )

    ended, fresh = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        application,
        end=lambda view: view,
        assert_reason=assert_defect,
        fresh=lambda _: restarted.apply(
            profile.id, request_key=_uuid(931), actor="admin"
        ),
    )
    assert fresh.id != ended.id
    assert not fresh.progress.admission_pending
