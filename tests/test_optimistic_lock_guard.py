"""New versioned Job locks fail; existing owners have named follow-up sites."""

from __future__ import annotations

from pathlib import Path

import pytest

from control.tests.added_line_guards import check_source
from control.tests.optimistic_boundaries import scan_source as _locks

ROOT = Path(__file__).resolve().parents[1]

# Scoped follow-up: each owner needs its complete transaction/effect boundary
# converted together. This is a site map, not a quota or permission for new locks.
# The added-lines guard rejects additions even in these files.
FOLLOW_UP_JOB_LOCKS: dict[str, tuple[str, ...]] = {
    "control/src/vonk_control/agent_jobs/aggregation.py": ("_aggregate_parent_state",),
    "control/src/vonk_control/agent_jobs/claim_authority.py": ("_claim_has_authority",),
    "control/src/vonk_control/agent_jobs/identity.py": ("_active",),
    "control/src/vonk_control/agent_jobs/predicates.py": ("_anchor_start_budget",),
    "control/src/vonk_control/agent_jobs/reconcile.py": ("_sweep_gone_targets",),
    "control/src/vonk_control/agent_jobs/retry.py": (
        "abandon_idempotent_job_in_session",
    ),
    "control/src/vonk_control/agent_jobs/scope.py": ("_lock_target_scopes",),
    "control/src/vonk_control/agent_jobs/supersession.py": (
        "request_superseded_workload_cancellation_in_session",
    ),
    "control/src/vonk_control/agent_upgrades/acceptance.py": ("apply",),
    "control/src/vonk_control/agent_upgrades/recovery.py": (
        "consume_agent_result",
        "_supersede_older",
        "advance_node",
        "heal_rollouts",
    ),
    "control/src/vonk_control/agent_upgrades/resume.py": ("resume",),
    "control/src/vonk_control/prebuilt_images.py": ("_release", "_finish", "_renew"),
    "control/src/vonk_control/profile_capacity.py": ("profile_build_memory_claims",),
    "control/src/vonk_control/recipe_image_availability/cancellation.py": (
        "_cancel_update_child",
        "cancel",
        "_build_child_cancellation_pending",
    ),
    "control/src/vonk_control/recipe_image_availability/cancellation_reconcile.py": (
        "_reconcile_cancellation_pass",
        "_release_cancelled_claim",
        "_end_spent_cancellation",
    ),
    "control/src/vonk_control/recipe_image_availability/execution.py": (
        "_update_model_progress",
    ),
    "control/src/vonk_control/recipe_image_availability/removal_acceptance.py": (
        "observe_recipe_removal",
    ),
    "control/src/vonk_control/recipe_image_availability/removal_completion.py": (
        "_finish_recipe_removal",
    ),
    "control/src/vonk_control/recipe_image_availability/removal_execution.py": (
        "_checkpoint_recipe_image_removal",
        "_complete_recipe_image_removal",
        "_complete_recipe_model_child",
        "_record_recipe_removal_failure",
    ),
    "control/src/vonk_control/recipe_image_availability/requests.py": (
        "cancel_profile_preparation",
    ),
    "control/src/vonk_control/recipe_operations/action_plans.py": (
        "_uninstall_plan_in_session",
    ),
    "control/src/vonk_control/recipe_operations/build_cancellation.py": (
        "_cancel_prebuilt_build",
        "_cancel_current_build_locked",
    ),
    "control/src/vonk_control/recipe_operations/cancellation.py": ("cancel",),
    "control/src/vonk_control/recipe_operations/intent.py": (
        "_active_owned_workload_jobs",
    ),
    "control/src/vonk_control/recipe_operations/job_run_completion.py": (
        "_complete_jobrun_stop_in_session",
    ),
    "control/src/vonk_control/recipe_operations/job_run_stop.py": (
        "_stop_logical_job_run",
    ),
    "control/src/vonk_control/recipe_operations/offline_stop.py": (
        "reconcile_pending_service_stops",
        "reconcile_offline_stops",
    ),
    "control/src/vonk_control/recipe_operations/profile_job_run_stop.py": (
        "_one_shot_stop_prerequisite",
    ),
    "control/src/vonk_control/recipe_operations/projection.py": (
        "_project_node_result",
    ),
    "control/src/vonk_control/recipe_operations/reconciliation_authority.py": (
        "_lock_reconciliation_rows_in_session",
        "_reconciliation_authority_in_session",
    ),
    "control/src/vonk_control/recipe_operations/result_consumption.py": (
        "_apply_build_cleanup",
        "_record_node_result_once",
    ),
    "control/src/vonk_control/recipe_operations/retirement.py": (
        "reconcile_retired_operations",
    ),
    "control/src/vonk_control/recipe_operations/retry.py": ("_retry_once",),
    "control/src/vonk_control/recipe_operations/stop.py": (
        "claim_withdrawal",
        "_defer_accepted_service_stop",
    ),
    "control/src/vonk_control/recipe_operations/stop_acceptance.py": (
        "_service_profile_stop_owner",
    ),
    "control/src/vonk_control/recipe_operations/stop_dispatch.py": (
        "_dispatch_stop_after_withdrawal",
    ),
    "control/src/vonk_control/recipe_update_batches/cancellation.py": (
        "cancel",
        "_cancel_unreadable",
        "reconcile_cancellations",
    ),
    "control/src/vonk_control/recipe_update_batches/execution.py": ("_owned",),
    "control/src/vonk_control/run_switch_journal_repair/discovery.py": (
        "_lock_discovery",
    ),
    "control/src/vonk_control/run_switch_journal_repair/ending.py": (
        "_end_unproven_journal",
    ),
    "control/src/vonk_control/run_switch_journal_repair/pending.py": (
        "record_repair_cancellation",
    ),
    "control/src/vonk_control/run_switch_operations/advance.py": (
        "_advance",
        "checkpoint_job",
    ),
    "control/src/vonk_control/run_switch_operations/cancellation_retry.py": (
        "_cancel_once",
        "_retry_once",
    ),
    "control/src/vonk_control/run_switch_operations/endings.py": (
        "_expire_distribution_observation",
        "_fail",
    ),
    "control/src/vonk_control/run_switch_operations/evidence_waits.py": (
        "wake_evidence_waits",
    ),
    "control/src/vonk_control/run_switch_operations/observation.py": (
        "_record_wait",
        "_hold_after_advance_failure",
    ),
    "control/src/vonk_control/run_switch_operations/ownership.py": ("locked",),
    "control/src/vonk_control/run_switch_operations/reservation.py": (
        "_refresh_blocked_plan",
    ),
    "control/src/vonk_control/run_switch_operations/retry_holds.py": (
        "_hold_start_observation",
        "_hold_issued_observation",
        "_hold_capacity_writer",
        "_hold_for_preflight_refresh",
    ),
}


@pytest.mark.parametrize(
    "statement",
    [
        "select(Job).with_for_update()",
        "select(Job.id).with_for_update(nowait=True)",
        "select(Job).with_for_update(of=Job)",
        "session.get(Job, identity, with_for_update=True)",
        "session.get(Job, identity, with_for_update={'nowait': True})",
        "AdmissionRowLock('owner', Job, select(Job))",
        "select(Owner).with_for_update(of=Owner)",
        "update(Job).values(state=value)",
        "session.query(Job).update(values)",
    ],
)
def test_guard_rejects_versioned_owner_locks(statement: str) -> None:
    assert _locks(
        "from vonk_control.models import Job as Owner\n" + statement, frozenset({"Job"})
    )


@pytest.mark.parametrize(
    "statement",
    [
        "select(Job).with_for_update(skip_locked=True)",
        "session.get(Job, identity, with_for_update={'skip_locked': True})",
        "select(Job).with_for_update(of=AgentNode)",
        "session.get(Job, identity)",
        "select(AgentNode).with_for_update()",
    ],
)
def test_guard_allows_claims_and_other_owners(statement: str) -> None:
    assert not _locks(statement, frozenset({"Job"}))


def test_unconverted_job_owners_are_named_for_follow_up() -> None:
    unlisted = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "control/src").rglob("*.py"))
        if _locks(path.read_text())
        and str(path.relative_to(ROOT)) not in FOLLOW_UP_JOB_LOCKS
    ]
    assert not unlisted, "Unconverted Job owners: " + ", ".join(unlisted)


@pytest.mark.parametrize(
    "path", ["control/src/vonk_control/new_owner.py", *FOLLOW_UP_JOB_LOCKS]
)
def test_new_locks_fail_even_in_follow_up_files(path: str) -> None:
    source = "select(Job)\nselect(Job).with_for_update()\n"
    assert not check_source(path, source, {1}, modes=("optimistic",))
    assert check_source(path, source, {2}, modes=("optimistic",))


def test_new_chained_lock_or_target_cannot_hide_on_unchanged_expression() -> None:
    source = "query = (\n    select(Job)\n    .with_for_update()\n)\n"
    path = "control/src/vonk_control/owner.py"
    assert check_source(path, source, {2}, modes=("optimistic",))
    assert check_source(path, source, {3}, modes=("optimistic",))


def test_editing_an_existing_lock_predicate_is_not_a_new_lock() -> None:
    source = "query = (\n    select(Job)\n    .where(Job.id == identity)\n    .with_for_update()\n)\n"
    assert not check_source(
        "control/src/vonk_control/agent_jobs/scope.py",
        source,
        {3},
        modes=("optimistic",),
    )
