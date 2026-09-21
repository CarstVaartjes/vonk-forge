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
from vonk_control.fleet_profile_contract import FleetProfilePreview
from vonk_control.fleet_profiles import (
    RunSwitchFleetProfileAdapter,
    build_production_fleet_profile_service,
)
from vonk_control.models import AgentNode, FleetProfileApplication, Job, RecipeBuild
from vonk_control.recipe_operations import _record_build_evidence
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .test_fleet_profile_cache_recovery import _typed_cache_failure
from .test_fleet_profile_recovery_current import _failed_profile
from .test_fleet_profiles import NOW, _uuid


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
        service.load(profile.number, request_key=_uuid(911), actor="admin")

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
            assert child.payload["plan"]["image_digest"] == image_digest
