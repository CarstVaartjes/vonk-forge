"""Automatic profile recovery for typed Controller cache loss."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.agent_words import ProfileChildPhase
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.fleet_profile_contract import (
    FleetProfileInput,
    FleetProfileSwitchAdapterState,
)
from vonk_control.fleet_profiles import (
    RunSwitchFleetProfileAdapter,
    build_production_fleet_profile_service,
)
from vonk_control.job_documents import RunSwitchJobPayload
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models import (
    AgentNode,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    FleetProfileSelection,
    Job,
    NodeArtifact,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeSourceBundle,
    User,
)
from vonk_control.recipe_builds import RecipeBuildPlan
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.run_switch_contract import RunSwitchApplyRequest
from vonk_control.run_switch_operations import PhaseExecution, RunSwitchOperationService
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationError,
)
from vonk_control.stored_json import read_row_column

from .runtime_image_fixtures import remove_test_image
from .test_fleet_profile_recovery_current import _failed_profile
from .test_fleet_profiles import NOW, _node_id
from .test_recipe_operations import setup_services
from .test_run_switch_operations import (
    ColdStartPhaseExecutor,
    CompleteArtifactInspector,
    _request,
)


def _typed_cache_failure(
    sessions,
    application_id: str,
    child_id: str,
    code: str,
    *,
    make_due: bool = True,
) -> None:
    with sessions.begin() as session:
        child = session.get(Job, child_id)
        application = session.get(FleetProfileApplication, application_id)
        assert child is not None and child.result is not None
        assert application is not None
        child.result = {
            **child.result,
            "phase": "prepare",
            "subphase": "runtime-image",
            "child_operation_id": None,
            "failed_phase": "prepare",
            "failure_code": code,
            "retryable": False,
        }
        if make_due:
            application.updated_at = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("remove_author", [False, True])
def test_typed_cache_loss_queues_one_scope_bound_profile_retry(
    tmp_path: Path, remove_author: bool
) -> None:
    sessions, lifecycle, service, _profile, _desired, first, child_id, nodes = (
        _failed_profile(tmp_path)
    )
    if remove_author:
        with sessions.begin() as session:
            author = session.scalar(select(User).where(User.subject == "admin"))
            assert author is not None
            session.delete(author)
    _typed_cache_failure(
        sessions,
        first.id,
        child_id,
        "runtime_image.cache_missing",
        make_due=False,
    )
    with sessions() as session:
        original_child = session.get(Job, child_id)
        assert original_child is not None
        original_plan = deepcopy(original_child.payload["plan"])
        original_ordinal = original_child.payload["workload_intent_ordinal"]
        assert type(original_ordinal) is int

    assert service.tick() is False
    with sessions.begin() as session:
        application = session.get(FleetProfileApplication, first.id)
        assert application is not None
        application.updated_at = datetime(2020, 1, 1, tzinfo=UTC)
    assert service.tick() is True
    with sessions() as session:
        applications = tuple(
            session.scalars(
                select(FleetProfileApplication).order_by(
                    FleetProfileApplication.created_at
                )
            )
        )
        children = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        )
        assert len(applications) == 2
        retry = next(row for row in applications if row.id != first.id)
        assert retry.progress["retry_of_application_id"] == first.id
        assert retry.progress["attempt"] == 2
        assert retry.progress["workload_intent_ordinal"] == original_ordinal
        retry_scope = require_mapping(retry.plan["scope"], "retry scope")
        assert tuple(require_sequence(retry_scope["node_ids"], "retry nodes")) == tuple(
            nodes
        )
        original_child = session.get(Job, child_id)
        assert original_child is not None
        assert original_child.payload["plan"] == original_plan
        assert original_child.result is not None
        assert original_child.result["failure_code"] == "runtime_image.cache_missing"
        assert len(children) == 1

    adapter = cast(RunSwitchFleetProfileAdapter, service._switch_adapter)
    restarted = build_production_fleet_profile_service(
        sessions,
        clock=lifecycle._clock,
        run_switch_operations=adapter._run_switch,
    )
    assert restarted.tick() is True
    assert restarted.tick() in {False, True}
    with sessions() as session:
        assert len(tuple(session.scalars(select(FleetProfileApplication)))) == 2
        retried_children = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        )
        assert len(retried_children) == 2
        retry_child = next(child for child in retried_children if child.id != child_id)
        retry_parent = read_row_column(retry_child, "payload")
        assert isinstance(retry_parent, RunSwitchJobPayload)
        assert all(
            phase.subphase != ProfileChildPhase.MODEL_DOWNLOAD
            for phase in retry_parent.plan.phases
        )


@pytest.mark.parametrize("authority_change", ["removed", "disabled", "demoted"])
def test_selected_profile_reconciles_a_newly_enrolled_spark(
    tmp_path: Path, authority_change: str
) -> None:
    sessions, _lifecycle, service, _profile, _desired, first, child_id, _nodes = (
        _failed_profile(tmp_path)
    )
    _typed_cache_failure(sessions, first.id, child_id, "runtime_image.cache_missing")
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(99),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
    with sessions.begin() as session:
        author = session.scalar(select(User).where(User.subject == "admin"))
        assert author is not None
        if authority_change == "removed":
            session.delete(author)
        elif authority_change == "disabled":
            author.disabled_at = NOW
        else:
            author.role = "viewer"
    assert service.tick() is True
    with sessions() as session:
        applications = tuple(session.scalars(select(FleetProfileApplication)))
        assert len(applications) == 2
        selection = session.get(FleetProfileSelection, 1)
        assert selection is not None
        assert selection.application_id != first.id
        selected = session.get(FleetProfileApplication, selection.application_id)
        assert selected is not None
        intended = service._intended_profile(selected, session=session)
        assert not isinstance(intended, Residue)
        assert _node_id(99) in intended.scope.node_ids


def test_cache_recovery_redownloads_bytes_that_failed_their_digest(
    tmp_path: Path,
) -> None:
    """A digest mismatch discards the bytes; recovery downloads them again."""

    sessions, _lifecycle, service, _profile, _desired, first, child_id, _nodes = (
        _failed_profile(tmp_path)
    )
    _typed_cache_failure(sessions, first.id, child_id, "runtime_image.archive_mismatch")
    assert service.tick() is True
    with sessions() as session:
        assert len(tuple(session.scalars(select(FleetProfileApplication)))) == 2


def test_cache_recovery_refuses_access_and_integrity_failures(tmp_path: Path) -> None:
    for code in (
        "runtime_image.archive_unavailable",
        "runtime_image.receipt_invalid",
    ):
        case = tmp_path / code.rsplit(".", 1)[-1]
        case.mkdir()
        sessions, _lifecycle, service, _profile, _desired, first, child_id, _nodes = (
            _failed_profile(case)
        )
        _typed_cache_failure(sessions, first.id, child_id, code)
        assert service.tick() is False
        with sessions() as session:
            assert len(tuple(session.scalars(select(FleetProfileApplication)))) == 1


@pytest.mark.parametrize(
    "recovery_blocker", [None, "contract", "capacity", "model-drift"]
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_cache_recovery_replans_an_actually_missing_build_archive(
    tmp_path: Path,
    recovery_blocker: str | None,
) -> None:
    sessions, _lifecycle, service, profile, _desired, first, child_id, nodes = (
        _failed_profile(tmp_path)
    )
    adapter = cast(RunSwitchFleetProfileAdapter, service._switch_adapter)
    run_switch = adapter._run_switch
    lifecycle = cast(RecipeOperationService, run_switch._lifecycle)
    with sessions.begin() as session:
        build = session.scalar(select(RecipeBuild))
        assert build is not None
        revision = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert revision is not None
        builder = session.get(AgentNode, build.builder_node_id)
        assert builder is not None
        builder.binary_digest = "a" * 64
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == build.builder_node_id
            )
        )
        assert snapshot is not None
        snapshot.capabilities = [*snapshot.capabilities, "recipe.build.v1"]
        if session.get(RecipeSourceBundle, build.source_bundle_sha256) is None:
            session.add(
                RecipeSourceBundle(
                    sha256=build.source_bundle_sha256,
                    media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                    archive_bytes=1,
                    total_bytes=1,
                    file_count=1,
                    storage_key="restored-source-bundle",
                    manifest={"schema_version": 1},
                    verified_at=NOW,
                )
            )
        build_plan = RecipeBuildPlan(
            build_id=build.id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            builder_node_id=build.builder_node_id,
            source_bundle_sha256=build.source_bundle_sha256,
            build_input_sha256=build.build_input_sha256,
            agent_payload=dict(build.plan),
            policy_report=dict(build.policy_report),
        )
        archive_digest = build.oci_layout_sha256
        archive_bytes = build.image_bytes
    assert archive_digest is not None and archive_bytes is not None
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime-images")
    remove_test_image(storage, archive_digest)
    assert storage.build_archive_available(archive_digest, archive_bytes) is False
    run_switch._build_archive_available = storage.build_archive_available

    def replan_build(recipe_revision_id: str, builder_node_id: str) -> RecipeBuildPlan:
        with sessions.begin() as session:
            build = session.get(RecipeBuild, build_plan.build_id)
            assert build is not None
            build.state = "planned"
            build.image_digest = None
            build.oci_layout_sha256 = None
            build.image_bytes = None
        return build_plan

    lifecycle.preview_build = replan_build
    # Ordinary review can report cache loss, but only recovery of the accepted
    # request may create a replacement build plan. Exercise the real profile
    # and Run/Switch planners with a builder seam that persists its SQL effect.
    with sessions() as session:
        before_build = session.get(RecipeBuild, build_plan.build_id)
        assert before_build is not None
        before = (
            before_build.state,
            before_build.image_digest,
            before_build.oci_layout_sha256,
            before_build.image_bytes,
        )
    review = service.preview(profile.id)
    assert not review.allowed
    with sessions() as session:
        after_build = session.get(RecipeBuild, build_plan.build_id)
        assert after_build is not None
        assert (
            after_build.state,
            after_build.image_digest,
            after_build.oci_layout_sha256,
            after_build.image_bytes,
        ) == before
    _typed_cache_failure(sessions, first.id, child_id, "runtime_image.cache_missing")

    if recovery_blocker == "model-drift":
        original_inspector = run_switch._artifacts

        class ChangedArtifactSet:
            def inspect(self, *args, **kwargs):
                return replace(
                    original_inspector.inspect(*args, **kwargs),
                    artifact_set_sha256="9" * 64,
                )

        run_switch._artifacts = ChangedArtifactSet()
        # Image repair must not copy the old model projection over current
        # evidence. A changed model set still requires a new operator review.
        assert service.tick() is True
        with sessions() as session:
            applications = list(session.scalars(select(FleetProfileApplication)))
            assert len(applications) == 1
            assert "profile.recovery_artifact_changed" in (
                applications[0].status_reason or ""
            )
            assert (
                len(
                    list(
                        session.scalars(
                            select(Job).where(Job.kind == "recipe.run-switch.v2")
                        )
                    )
                )
                == 1
            )
        return

    if recovery_blocker is not None:
        with sessions.begin() as session:
            if recovery_blocker == "contract":
                build = session.get(RecipeBuild, build_plan.build_id)
                assert build is not None
                build.plan = {}
            else:
                inventory = session.scalar(select(NodeInventorySnapshot))
                assert inventory is not None
                inventory.host_memory_free_bytes = 0
                inventory.gpu_memory_free_bytes = 0
        recovery_review = service.preview(profile.id, allow_pending_cache_rebuild=True)
        assert not recovery_review.allowed
        assert recovery_review.assessments[0].assessment.blockers
        # A blocked recovery admits no replacement intent and changes no build;
        # the accepted intent keeps retrying with a visible next attempt.
        service.tick()
        with sessions() as session:
            applications = tuple(session.scalars(select(FleetProfileApplication)))
            assert len(applications) == 1
            assert "next attempt" in (applications[0].status_reason or "")
            build = session.get(RecipeBuild, build_plan.build_id)
            assert build is not None and build.state == before[0]
        return

    assert service.tick() is True
    recovery_review = service.preview(profile.id, allow_pending_cache_rebuild=True)
    assert recovery_review.allowed and recovery_review.assessments
    assert service.tick() is True
    with sessions() as session:
        retry = session.scalar(
            select(FleetProfileApplication)
            .where(FleetProfileApplication.id != first.id)
            .order_by(FleetProfileApplication.created_at.desc())
        )
        assert retry is not None
        switch_state = FleetProfileSwitchAdapterState.model_validate_json(
            canonical_message(retry.progress["switch_adapter"])
        )
        assert len(switch_state.pending_children) == 1
        pending_child = switch_state.pending_children[0]
        assert pending_child.kind == switch_state.queue[pending_child.queue_index].kind
        retry_child = session.get(Job, pending_child.operation_id)
        assert retry_child is not None
        child_plan = require_mapping(retry_child.payload["plan"], "child plan")
        child_build = require_mapping(child_plan["build"], "child build")
        child_phases = require_sequence(child_plan["phases"], "child phases")
        first_phase = require_mapping(child_phases[0], "first child phase")
        assert child_build["state"] == "planned"
        assert first_phase["subphase"] == "container-build"
        retry_scope = require_mapping(retry.plan["scope"], "retry scope")
        assert tuple(require_sequence(retry_scope["node_ids"], "retry nodes")) == tuple(
            nodes
        )


@pytest.mark.usefixtures("damaged_json_rows")
def test_malformed_failed_profile_does_not_block_unrelated_queued_work(
    tmp_path: Path,
) -> None:
    sessions, _lifecycle, service, profile, desired, first, child_id, _nodes = (
        _failed_profile(tmp_path)
    )
    _typed_cache_failure(sessions, first.id, child_id, "runtime_image.cache_missing")
    other = service.create(
        FleetProfileInput(
            name="Independent queued profile",
            assignments=desired.assignments,
        ),
        actor="admin",
    )
    queued = service.apply(
        other.id,
        request_key="00000000-0000-4000-8000-000000009002",
        actor="admin",
    )
    with sessions.begin() as session:
        malformed = session.get(FleetProfile, profile.id)
        assert malformed is not None
        malformed.assignments = [{"recipe_selector": 7}]

    assert service.tick() is True
    assert service.application(queued.id).state == "running"


class _MissingRuntimeImage(ColdStartPhaseExecutor):
    def execute(self, plan, phase, **kwargs) -> PhaseExecution:
        if phase.subphase == "runtime-image":
            raise RuntimeImagePreparationError(
                "runtime_image.cache_missing",
                "OCI archive is not present in Controller storage",
            )
        return super().execute(plan, phase, **kwargs)


class _ColdInspector(CompleteArtifactInspector):
    def inspect(self, *args, **kwargs):
        return replace(
            super().inspect(*args, **kwargs),
            missing_nas_bytes=1024,
            nas_coverage="partial",
        )


def test_run_switch_persists_typed_cache_failure(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        session.query(NodeArtifact).delete()
    executor = _MissingRuntimeImage()
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=_ColdInspector(missing_spark_bytes=1024),
        phase_executor=executor,
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key="00000000-0000-4000-8000-000000009001",
        ),
        actor="admin",
    )
    for _ in range(12):
        service._advance(operation.operation_id)
        failed = service.get(operation.operation_id)
        if failed.state == "failed":
            break
    else:
        raise AssertionError("Run/Switch did not reach the runtime-image failure")
    assert failed.result is not None
    assert failed.result.failure_code == "runtime_image.cache_missing"
    assert failed.result.retryable is False
