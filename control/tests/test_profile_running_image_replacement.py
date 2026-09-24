"""A fresh review must name the exact cache replacement of a running image."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_control.models import (
    AgentNode,
    FleetProfileApplication,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
)
from vonk_control.recipe_execution_contract import parse_stored_installation_plan
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImageReceipt,
)

from .test_profile_build_memory import _accepted_build_profile
from .test_profile_build_process_recovery import _worker_process


@pytest.mark.parametrize("distinct_build", [False, True])
def test_running_image_replacement_executes_the_reviewed_build_receipt(
    tmp_path, postgres_engine, distinct_build
):
    sessions, profiles, _planner, _node, application_id, selected = (
        _accepted_build_profile(tmp_path, postgres_engine, initially_installed=False)
    )
    assert application_id is not None
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime-images")
    receipt_path = next(storage.root.glob("*.receipt.json"))
    original = RuntimeImageReceipt.model_validate_json(receipt_path.read_text())
    original_bytes = storage.read_receipt(original.oci_archive_sha256)
    archive = Path(original_bytes.archive_path)
    (tmp_path / "expected-build.archive").write_bytes(archive.read_bytes())
    (tmp_path / "expected-build.receipt.json").write_text(original.model_dump_json())
    archive.unlink()
    receipt_path.unlink()
    _worker_process(
        {
            "database": postgres_engine.url.render_as_string(hide_password=False),
            "root": str(tmp_path),
            "now": profiles._clock().isoformat(),
            "application": application_id,
        },
        "resume",
    )
    original = storage.read_receipt(original.oci_archive_sha256)
    with sessions() as session:
        running = session.scalar(select(RecipeRun))
        application = session.get(FleetProfileApplication, application_id)
        assert running is not None and running.state == "running"
        assert running.route_state == "published"
        assert application is not None and application.profile_id is not None
        profile_id = application.profile_id
        old_run_id = running.id
        old_installation_id = running.installation_id
        installed = session.get(RecipeInstallation, old_installation_id)
        assert installed is not None
        old_plan = installed.plan
        now = running.updated_at + timedelta(seconds=1)
    profiles._clock = lambda: now

    # Both archives remain usable. A fresh verified build changes the selected
    # output without changing the already-running rank's immutable plan.
    replacement_build_id = str(uuid4()) if distinct_build else selected.build_id
    if distinct_build:
        # A completed build on another builder has the same executable inputs
        # but its own provenance. Keep the old successful record and running plan.
        with sessions.begin() as session:
            previous = session.get(RecipeBuild, selected.build_id)
            assert previous is not None
            builder = session.get(AgentNode, previous.builder_node_id)
            assert builder is not None
            second_builder_id = "spk_" + "d" * 32
            session.add(
                AgentNode(
                    node_id=second_builder_id,
                    state="active",
                    binary_digest=builder.binary_digest,
                    capabilities=list(builder.capabilities),
                    workload_intent_ordinal=1,
                )
            )
            session.flush()
            payload = deepcopy(previous.plan)
            payload["build_id"] = replacement_build_id
            session.add(
                RecipeBuild(
                    id=replacement_build_id,
                    recipe_revision_id=previous.recipe_revision_id,
                    builder_node_id=second_builder_id,
                    source_bundle_sha256=previous.source_bundle_sha256,
                    build_input_sha256=previous.build_input_sha256,
                    state="succeeded",
                    policy_report=deepcopy(previous.policy_report),
                    plan=payload,
                    image_digest=previous.image_digest,
                    oci_layout_sha256=previous.oci_layout_sha256,
                    image_bytes=previous.image_bytes,
                    created_at=now,
                    updated_at=now,
                )
            )
    replacement_bytes = b"a distinct verified rebuilt image archive"
    staged = storage.prepare_path()
    staged.write_bytes(replacement_bytes)
    changed = storage.commit(
        staged,
        receipt=original.model_copy(
            update={
                "build_id": replacement_build_id,
                "image_digest": "sha256:" + "b" * 64,
                "platform_manifest_digest": "sha256:" + "b" * 64,
                "oci_archive_sha256": hashlib.sha256(replacement_bytes).hexdigest(),
                "image_bytes": len(replacement_bytes),
                "archive_path": str(staged),
                "recorded_at": now.isoformat(),
            }
        ),
    )
    with sessions.begin() as session:
        build = session.get(RecipeBuild, replacement_build_id)
        assert build is not None and build.state == "succeeded"
        build.image_digest = changed.image_digest
        build.oci_layout_sha256 = changed.oci_archive_sha256
        build.image_bytes = changed.image_bytes
        build.updated_at = now
    review = profiles.preview(profile_id)
    assert review.allowed, review.model_dump(mode="json")
    assert (
        review.preparation_decisions[0].runtime_image.build_id == replacement_build_id
    )
    assert (
        review.preparation_decisions[0].runtime_image.image_digest
        == changed.image_digest
    )
    assert (
        review.preparation_decisions[0].runtime_image.oci_layout_sha256
        == changed.oci_archive_sha256
    )
    assert old_run_id in {run.run_id for run in review.admission_decisions[0].stops}
    with sessions() as session:
        installed = session.get(RecipeInstallation, old_installation_id)
        assert installed is not None and installed.plan == old_plan
    accepted = profiles.apply(
        profile_id,
        plan_digest=review.plan_digest,
        request_key=str(uuid4()),
        actor="admin",
    )
    assert accepted.id != application_id
    if distinct_build:
        # A later observation makes the old builder's completed receipt newest.
        # This cannot redirect the already accepted replacement to that image.
        with sessions.begin() as session:
            previous = session.get(RecipeBuild, selected.build_id)
            assert previous is not None
            previous.updated_at = now + timedelta(seconds=2)

    config = tmp_path / "replacement-worker.json"
    config.write_text(
        json.dumps(
            {
                "database": postgres_engine.url.render_as_string(hide_password=False),
                "root": str(tmp_path),
                "now": now.isoformat(),
                "application": accepted.id,
                "refresh_inventory": True,
            }
        )
    )
    config.chmod(0o600)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.test_profile_build_process_recovery",
            str(config),
            "resume",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["state"] == "succeeded", completed.stdout
    with sessions() as session:
        old = session.get(RecipeRun, old_run_id)
        assert (
            old is not None
            and old.state == "stopped"
            and old.route_state != "published"
        )
        replacement = session.scalar(
            select(RecipeRun).where(RecipeRun.id != old_run_id)
        )
        assert replacement is not None and replacement.state == "running"
        assert replacement.route_state == "published"
        assert replacement.installation_id != old_installation_id
        installed = session.get(RecipeInstallation, replacement.installation_id)
        assert installed is not None
        assert installed.recipe_build_id == replacement_build_id
        if distinct_build:
            previous = session.get(RecipeBuild, selected.build_id)
            assert (
                previous is not None and previous.image_digest == original.image_digest
            )
        for plan in parse_stored_installation_plan(
            installed.plan
        ).compiled_execution_plans.values():
            assert plan.runtime_image.image_digest == changed.image_digest
            assert plan.runtime_image.oci_layout_sha256 == changed.oci_archive_sha256
        stops = tuple(session.scalars(select(Job).where(Job.kind == "recipe.stop")))
        assert len(stops) == 1 and stops[0].payload["owner_id"] == old_run_id
    assert (
        storage.read_receipt(original.oci_archive_sha256).image_digest
        == original.image_digest
    )
    assert (
        storage.read_receipt(changed.oci_archive_sha256).image_digest
        == changed.image_digest
    )
