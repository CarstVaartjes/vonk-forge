"""Profile recovery retains the artifacts accepted before cache loss."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState
from vonk_control.fleet_profile_contract import FleetProfileInput, FleetProfilePreview
from vonk_control.fleet_profiles import (
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    build_production_fleet_profile_service,
)
from vonk_control.models import (
    AgentNode,
    CatalogDocumentRevision,
    FleetProfileApplication,
    Job,
    RecipeBuild,
    RecipeInstallation,
)
from vonk_control.recipe_operations import record_build_evidence
from vonk_control.run_switch_contract import RunSwitchPlan
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .runtime_image_fixtures import place_test_image, remove_test_image
from .test_fleet_profile_cache_recovery import _typed_cache_failure
from .test_fleet_profile_recovery_current import _failed_profile
from .test_fleet_profiles import (
    NOW,
    _assessment,
    _exact_preparation,
    _SwitchAdapter,
    _uuid,
)
from .test_recipe_operations import installed_recipe, setup_services


def _complete_rebuild(
    sessions, storage, *, archive_digest: str, image_bytes: int, image_digest: str
) -> None:
    """Deliver a completed build through the persisted build-evidence consumer."""

    place_test_image(storage, archive_digest, image_bytes)
    with sessions.begin() as session:
        build = session.scalar(select(RecipeBuild))
        assert build is not None
        build.state = "building"
        build.image_digest = None
        build.oci_layout_sha256 = None
        build.image_bytes = None
        build_id = build.id
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        record_build_evidence(
            session,
            build,
            {
                "image_bytes": image_bytes,
                "image_digest": image_digest,
                "oci_layout_sha256": archive_digest,
            },
            now=NOW,
        )


@pytest.mark.parametrize("after_retry_admission", [False, True])
def test_rebuilt_image_cannot_replace_persisted_profile_identity(
    tmp_path: Path, after_retry_admission: bool
) -> None:
    """Neither retry admission nor its later child may bind a different image."""

    sessions, lifecycle, service, _profile, _desired, first, child_id, nodes = (
        _failed_profile(tmp_path)
    )
    _typed_cache_failure(sessions, first.id, child_id, "runtime_image.cache_missing")
    with sessions() as session:
        original = session.get(FleetProfileApplication, first.id)
        assert original is not None
        accepted_json = deepcopy(original.plan)
        accepted = FleetProfilePreview.model_validate_json(
            json.dumps(accepted_json), strict=True
        )
        original_image = accepted.preparations[0].preparation.runtime_image
        original_ordinals = {
            node.node_id: node.workload_intent_ordinal
            for node in session.scalars(select(AgentNode))
        }
    adapter = cast(RunSwitchFleetProfileAdapter, service._switch_adapter)
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime-images")
    adapter._run_switch._build_archive_available = storage.build_archive_available
    if after_retry_admission:
        assert service.tick()
    remove_test_image(storage, original_image.oci_layout_sha256)
    _complete_rebuild(
        sessions,
        storage,
        archive_digest=hashlib.sha256(b"a different rebuilt OCI archive").hexdigest(),
        image_bytes=len(b"a different rebuilt OCI archive"),
        image_digest="sha256:" + "2" * 64,
    )

    restarted = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=adapter._run_switch
    )
    restarted.tick()
    # A stale content observation does not certify changed accepted bytes.
    # Let the existing child observer consume its immutable budget, across a
    # reconstructed parent service, before asserting its ending.
    from vonk_control.run_switch_operations.constants import (
        _FINAL_VERIFICATION_MAX_SECONDS,
    )

    end_time = NOW + timedelta(seconds=_FINAL_VERIFICATION_MAX_SECONDS + 1)
    adapter._run_switch._clock = lambda: end_time
    restarted._clock = lambda: end_time
    for _ in range(3):
        adapter._run_switch.tick()
        restarted.tick()

    with sessions() as session:
        applications = list(session.scalars(select(FleetProfileApplication)))
        assert len(applications) == (2 if after_retry_admission else 1)
        blocked = next(
            (row for row in applications if row.id != first.id), applications[0]
        )
        assert blocked.state == LifecycleState.FAILED
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
        original = session.get(FleetProfileApplication, first.id)
        assert original is not None and original.plan == accepted_json
        for node_id in nodes:
            node = session.get(AgentNode, node_id)
            assert node is not None
            assert node.workload_intent_ordinal == original_ordinals[node_id]
    reviewed = restarted.preview(_profile.id)
    assert reviewed.allowed
    from .non_blocking import assert_ended_without_blocking

    def cause(receipt):
        assert receipt.result is not None

    _, fresh = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        restarted.application(blocked.id),
        end=lambda operation: restarted.application(operation.id),
        fresh=lambda _world: restarted.apply(
            _profile.id, request_key=_uuid(931), actor="admin"
        ),
        assert_reason=cause,
    )
    assert fresh.id != blocked.id


@pytest.mark.parametrize("supersede", [False, True])
def test_restored_exact_bytes_recover_only_current_profile_intent(
    tmp_path: Path, supersede: bool
) -> None:
    """A fifth loss still resumes exact bytes; refreshed evidence grants no new intent."""

    sessions, lifecycle, service, profile, _desired, first, child_id, _nodes = (
        _failed_profile(tmp_path)
    )
    _typed_cache_failure(sessions, first.id, child_id, "runtime_image.cache_missing")
    with sessions.begin() as session:
        application = session.get(FleetProfileApplication, first.id)
        assert application is not None
        application.progress = {**application.progress, "attempt": 5}
    with sessions() as session:
        build = session.scalar(select(RecipeBuild))
        assert build is not None and build.oci_layout_sha256 is not None
        image_digest = build.image_digest
        assert image_digest is not None
        storage = FilesystemRuntimeImageStorage(tmp_path / "runtime-images")
        archive_digest = build.oci_layout_sha256
        image_bytes = build.image_bytes
        assert image_bytes is not None
        remove_test_image(storage, archive_digest)
    adapter = cast(RunSwitchFleetProfileAdapter, service._switch_adapter)
    adapter._run_switch._build_archive_available = storage.build_archive_available
    _complete_rebuild(
        sessions,
        storage,
        archive_digest=archive_digest,
        image_bytes=image_bytes,
        image_digest=image_digest,
    )
    if supersede:
        service.load(
            profile.number,
            request_key=_uuid(911),
            actor="admin",
        )

    def recovered_clock():
        return lifecycle._clock() + timedelta(seconds=20)

    adapter._run_switch._clock = recovered_clock
    restarted = build_production_fleet_profile_service(
        sessions, clock=recovered_clock, run_switch_operations=adapter._run_switch
    )
    restarted.tick()
    with sessions() as session:
        applications = list(session.scalars(select(FleetProfileApplication)))
        assert len(applications) == 2
        successor = next(row for row in applications if row.id != first.id)
        assert successor.progress.get("retry_of_application_id") == (
            None if supersede else first.id
        )
    if not supersede:
        assert restarted.tick()
        with sessions() as session:
            children = list(
                session.scalars(select(Job).where(Job.kind == "recipe.run-switch.v2"))
            )
            assert len(children) == 2
            child = next(row for row in children if row.id != child_id)
            plan = RunSwitchPlan.model_validate_json(json.dumps(child.payload["plan"]))
            assert plan.image_digest == image_digest


@pytest.mark.parametrize("kept_needs_work", [False, True])
def test_retry_requires_preparation_only_when_an_assignment_needs_work(
    tmp_path: Path, kept_needs_work: bool
) -> None:
    """An unchanged installed assignment cannot block a different assignment's retry."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=_uuid(920)
    )
    other_node = "spk_" + "2" * 32
    with sessions.begin() as session:
        session.add(AgentNode(node_id=other_node, state="active", last_seen_at=NOW))
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert revision is not None
        selector = f"{revision.publisher}/{revision.slug}"

    attest_kept = False

    def preparation(_session, _assignment, node_ids, **_kwargs):
        if node_ids == nodes and not attest_kept:
            raise ValueError("Kept installation has no current cache observation")
        return _assessment(_exact_preparation(node_ids))

    service = FleetProfileService(
        sessions,
        clock=lifecycle._clock,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=preparation,
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Keep installed and load another Spark",
                "assignments": [
                    {
                        "recipe_selector": selector,
                        "spark_ids": list(nodes),
                        "desired_state": "installed",
                    },
                    {
                        "recipe_selector": selector,
                        "spark_ids": [other_node],
                        "desired_state": "running",
                        "assignment_name": "other-chat",
                    },
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed and len(preview.preparations) == 1
    kept = next(item for item in preview.assignments if tuple(item.node_ids) == nodes)
    assert kept.actions == ["keep"]
    first = service.apply(
        profile.id,
        request_key=_uuid(921),
        actor="admin",
    )
    with sessions.begin() as session:
        application = session.get(FleetProfileApplication, first.id)
        assert application is not None
        application.state = "failed"
        application.status_reason = "Worker interrupted before issuing the switch"
        if kept_needs_work:
            installation = session.get(RecipeInstallation, installed.owner_id)
            assert installation is not None
            installation.state = "failed"
    if kept_needs_work:
        attest_kept = True
        current = service.preview(profile.id)
        assert current.allowed
        assert next(
            item
            for item in current.assignments
            if item.assignment_id == kept.assignment_id
        ).actions == ["switch"]
        # The accepted plan never bound an identity for the kept assignment, so
        # there is none to replace: the verified preparation observed now is bound
        # (and Run/Switch verifies its digest again when the child starts).
        retried = service.retry(first.id, request_key=_uuid(922), actor="admin")
        assert retried.retry_of_application_id == first.id
        assert retried.state == "queued"
    else:
        # The service clock may move backward across reconstruction/retry.
        # Receipt chronology remains monotonic, while updated_at reflects the
        # retry's actual injected wallclock.
        retry_clock = first.created_at - timedelta(seconds=1)
        service._clock = lambda: retry_clock
        retried = service.retry(first.id, request_key=_uuid(922), actor="admin")
        assert retried.retry_of_application_id == first.id
        assert retried.state == "queued"
        assert retried.created_at == first.created_at + timedelta(microseconds=1)
        assert retried.updated_at == retry_clock
        with sessions() as session:
            latest = service.endpoint_intent(session, profile.number)
        assert latest.application_id == retried.id
