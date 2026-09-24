"""Profile recovery retains the artifacts accepted before cache loss."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select
from vonk_control.fleet_profile_contract import FleetProfileInput, FleetProfilePreview
from vonk_control.fleet_profiles import (
    FleetProfileConflict,
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
from vonk_control.recipe_operations import _record_build_evidence
from vonk_control.run_switch_contract import RunSwitchPlan
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

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


def _complete_rebuild(sessions, storage, *, archive: bytes, image_digest: str) -> None:
    """Deliver a completed build through the persisted build-evidence consumer."""

    archive_digest = hashlib.sha256(archive).hexdigest()
    (storage.root / archive_digest).write_bytes(archive)
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
        _record_build_evidence(
            session,
            build,
            {
                "build_input_sha256": build.build_input_sha256,
                "image_bytes": len(archive),
                "image_digest": image_digest,
                "oci_layout_sha256": archive_digest,
                "policy": {
                    "dockerfile": "Dockerfile",
                    "findings": [],
                    "passed": True,
                },
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
    (storage.root / original_image.oci_layout_sha256).unlink()
    _complete_rebuild(
        sessions,
        storage,
        archive=b"a different rebuilt OCI archive",
        image_digest="sha256:" + "2" * 64,
    )

    restarted = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=adapter._run_switch
    )
    restarted.tick()

    with sessions() as session:
        applications = list(session.scalars(select(FleetProfileApplication)))
        assert len(applications) == (2 if after_retry_admission else 1)
        blocked = next(
            (row for row in applications if row.id != first.id), applications[0]
        )
        assert blocked.state == "failed"
        if after_retry_admission:
            assert "profile.runtime-image-changed" in (blocked.status_reason or "")
            assert "review and load" in (blocked.status_reason or "")
        else:
            assert "profile.recovery_artifact_changed" in (blocked.status_reason or "")
            assert "explicit new load" in (blocked.status_reason or "")
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
        path = storage.root / build.oci_layout_sha256
        archive = path.read_bytes()
        path.unlink()
    adapter = cast(RunSwitchFleetProfileAdapter, service._switch_adapter)
    adapter._run_switch._build_archive_available = storage.build_archive_available
    _complete_rebuild(sessions, storage, archive=archive, image_digest=image_digest)
    if supersede:
        service.load(
            profile.number,
            request_key=_uuid(911),
            actor="admin",
            expected_plan_digest=service.preview(profile.id).plan_digest,
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
        plan_digest=preview.plan_digest,
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
        with pytest.raises(FleetProfileConflict, match="recovery_identity_unavailable"):
            service.retry(first.id, request_key=_uuid(922), actor="admin")
    else:
        retried = service.retry(first.id, request_key=_uuid(922), actor="admin")
        assert retried.retry_of_application_id == first.id
        assert retried.state == "queued"
