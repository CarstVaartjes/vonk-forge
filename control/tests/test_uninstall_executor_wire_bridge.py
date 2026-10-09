"""Controller claims drive real agent cleanup across damage, restart and expiry."""

from __future__ import annotations

import os
from datetime import UTC, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from vonk_agent_protocol import (
    TERMINAL_LIFECYCLE_STATES,
    AgentResultState,
    InstallationState,
    LifecycleState,
    OutcomeUnknown,
    RecipeUninstallPayload,
)
from vonk_agent_protocol.agent_state import AgentExecutorProbeMode
from vonk_control.agent_jobs import AgentJobService
from vonk_control.models import AgentOperation as StoredOperation
from vonk_control.models import Job, RecipeInstallation

from .agent_fences import fenced_operation
from .runtime_identity_support import claim_agent
from .test_agent_restart_recovery_wire_bridge import (
    _probe_request,
    _run_probe,
    controller,  # noqa: F401
    distribution_https,  # noqa: F401
    restart_probe,  # noqa: F401
)
from .test_recipe_operations import installed_recipe, setup_services


@pytest.mark.parametrize(
    ("fault_stage", "shared_object"),
    [
        ("spec-repair", False),
        ("spec-repair", True),
        ("identity-repair", False),
        ("reclamation", False),
        ("store-parent", False),
    ],
)
def test_uninstall_executor_repairs_discovery_without_bypassing_cleanup_authority(
    controller,  # noqa: F811 - imported pytest fixture
    distribution_https,  # noqa: F811 - imported pytest fixture
    restart_probe: Path,  # noqa: F811 - imported pytest fixture
    tmp_path: Path,
    shared_object: bool,
    fault_stage: str,
) -> None:
    """Catches fabricated Controller success and cleanup discovery lost on restart."""
    sessions, clock = controller
    server, certificates, _, _ = distribution_https
    sessions, recipes, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=sessions.kw["bind"]
    )
    installed = installed_recipe(
        recipes, mapping_id, build_id, nodes, request_id=str(uuid4())
    )
    jobs = AgentJobService(sessions, clock=clock)
    jobs.set_result_consumer(recipes.consume_agent_result)
    recipes._agent_jobs = jobs
    recipes._clock = clock
    preview = recipes.preview_uninstall(installed.owner_id)
    owner = recipes.uninstall(
        installed.owner_id,
        plan_digest=preview.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    first = claim_agent(jobs, nodes[0], "serial-0")
    assert first is not None and isinstance(first.payload, RecipeUninstallPayload)
    plan = first.payload.compiled_execution_plan
    assert plan is not None
    installation_id = installed.owner_id
    root = tmp_path / "agent-store"
    installation = root / "installations" / installation_id
    installation.mkdir(parents=True)
    protected = tmp_path / "user-document"
    protected.write_bytes(b"protected user data")
    # Damage is a stored observation, not new caller input. Replacing a symlink
    # in managed metadata must never follow it into this outside user's file.
    if fault_stage == "spec-repair":
        (installation / "spec.json").symlink_to(protected)
    else:
        (installation / "spec.json").write_text(plan.model_dump_json())
    identity_record = installation / "recipe-content.sha256"
    if fault_stage == "identity-repair":
        identity_record.symlink_to(protected)
    else:
        identity_record.write_text(plan.identity.recipe_revision_sha256)
    store = root / "distribution/models"
    store.mkdir(parents=True)
    targets = []
    for artifact in plan.artifacts:
        target = store / artifact.sha256
        target.write_bytes(b"verified cached model bytes")
        targets.append(target)
    shared = root / "installations" / str(uuid4())
    shared.mkdir()
    if shared_object:
        os.link(targets[0], shared / "shared-model")
    outside = tmp_path / "user-models"
    saved_store = root / "distribution/models-retained"
    if fault_stage == "store-parent":
        outside.mkdir()
        for target in targets:
            (outside / target.name).write_bytes(b"protected user model")
        store.rename(saved_store)
        store.symlink_to(outside, target_is_directory=True)
    # Persistent I/O damage prevents metadata repair or final reclamation.
    unavailable = (
        installation if fault_stage in {"spec-repair", "identity-repair"} else store
    )
    if fault_stage != "store-parent":
        unavailable.chmod(0o555)
    try:
        unknown = _run_probe(
            restart_probe,
            _probe_request(
                AgentExecutorProbeMode.UNINSTALL, first, root, server, certificates
            ),
        )
        assert unknown.state == AgentResultState.OBSERVING
        jobs.record_result(unknown)
        assert installation.exists() and all(target.exists() for target in targets)
        assert protected.read_bytes() == b"protected user data"
        with sessions() as session:
            stored = session.get(StoredOperation, fenced_operation(sessions, first).id)
            assert stored is not None and stored.recovery_deadline is not None
            deadline = stored.recovery_deadline.replace(tzinfo=UTC)
        # Restart both owners; the journal replays the same fence without effects.
        replay = _run_probe(
            restart_probe,
            _probe_request(
                AgentExecutorProbeMode.UNINSTALL, first, root, server, certificates
            ),
        )
        assert replay == unknown
        jobs = AgentJobService(sessions, clock=clock)
        jobs.set_result_consumer(recipes.consume_agent_result)
        recipes._agent_jobs = jobs
        clock.now = deadline + timedelta(seconds=1)
        jobs.reconcile_orders()
        with sessions() as session:
            ended = session.get(Job, owner.id)
            assert ended is not None and ended.state in TERMINAL_LIFECYCLE_STATES
            assert ended.state != LifecycleState.SUCCEEDED
        assert installation.exists() and protected.exists()
    finally:
        if fault_stage == "store-parent":
            store.unlink()
            saved_store.rename(store)
        else:
            unavailable.chmod(0o755)
    # Fault clearing changes only writability, never repairs spec.json by hand.
    # The fresh Controller claim supplies its accepted plan to the actual executor.
    fresh_preview = recipes.preview_uninstall(installed.owner_id)
    assert fresh_preview.allowed
    fresh = recipes.uninstall(
        installed.owner_id,
        plan_digest=fresh_preview.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    claim = claim_agent(jobs, nodes[0], "serial-0")
    assert claim is not None and claim.fence != first.fence
    repaired = _run_probe(
        restart_probe,
        _probe_request(
            AgentExecutorProbeMode.UNINSTALL, claim, root, server, certificates
        ),
    )
    # This HTTPS fixture serves distribution only, not helper grants. Restored
    # discovery must not fabricate privileged cleanup or bless bytes as removed.
    assert isinstance(repaired.result, OutcomeUnknown)
    assert repaired.state == AgentResultState.OBSERVING
    assert not (installation / "spec.json").is_symlink()
    assert plan.model_validate_json((installation / "spec.json").read_bytes()) == plan
    assert not identity_record.is_symlink()
    assert identity_record.read_text() == plan.identity.recipe_revision_sha256
    jobs.record_result(repaired)
    with sessions() as session:
        stored = session.get(Job, fresh.id)
        assert stored is not None and stored.state != LifecycleState.SUCCEEDED
        actual = session.get(RecipeInstallation, installed.owner_id)
        assert actual is not None and actual.state != InstallationState.UNINSTALLED
    assert installation.exists() and all(target.exists() for target in targets)
    assert protected.read_bytes() == b"protected user data"
    if fault_stage == "store-parent":
        assert all(
            (outside / target.name).read_bytes() == b"protected user model"
            for target in targets
        )
    if shared_object:
        assert targets[0].exists() and (shared / "shared-model").exists()
    # Pending authority is observable and cannot poison a later keyed request.
    next_preview = recipes.preview_uninstall(installed.owner_id)
    assert next_preview.allowed

    def request_cleanup():
        return recipes.uninstall(
            installed.owner_id,
            plan_digest=next_preview.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
        )

    next_owner = request_cleanup()
    assert next_owner.id != fresh.id
    # The automatic reconciler ends the old unknown executor within its bound.
    # Admission already succeeded; no operator repair or retirement is needed.
    with sessions() as session:
        prior = session.get(StoredOperation, fenced_operation(sessions, claim).id)
        assert prior is not None and prior.recovery_deadline is not None
        deadline = prior.recovery_deadline.replace(tzinfo=UTC)
    clock.now = deadline + timedelta(seconds=1)
    jobs.reconcile_orders()
    next_claim = claim_agent(jobs, nodes[0], "serial-0")
    assert next_claim is not None and next_claim.fence != claim.fence
    with sessions() as session:
        next_operation = session.get(
            StoredOperation, fenced_operation(sessions, next_claim).id
        )
        assert next_operation is not None
        assert next_operation.parent_job_id == next_owner.id
        assert isinstance(next_claim.payload, RecipeUninstallPayload)
