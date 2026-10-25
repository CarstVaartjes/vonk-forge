"""New versioned owner locks fail; existing owners have named follow-up sites."""

from __future__ import annotations

from pathlib import Path

import pytest

from control.tests.added_line_guards import check_source
from control.tests.optimistic_boundaries import scan_source as _locks
from control.tests.recipe_run_write_verdicts import (
    RECIPE_RUN_WRITE_VERDICTS,
    classified_write_lines,
    writer_name,
)

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


# Retained transactional admission/release boundaries, not new lock exemptions.
# Each verdict names why this lock remains; added lines still fail everywhere.
RECIPE_RUN_LOCK_VERDICTS: dict[str, dict[str, str]] = {
    "distributed_recovery/common.py": {
        "settle_observed_absent_runs_in_session": "retain: SKIP LOCKED claim for confirmed-absence release",
    },
    "agent_jobs/retirement.py": {
        "_release_retired_owner_in_session": "retain: confirmed reservation release",
    },
    "artifact_jobs/service.py": {
        "_submit_once": "retain: submission binds active run and capacity admission",
    },
    "fleet_profiles/queue_application.py": {
        "_queue_application": "retain: topology-atomic reviewed replacement admission",
    },
    "recipe_operations/action_plans.py": {
        "_stop_plan_in_session": "retain: Stop admission binds reviewed exact effects",
        "_uninstall_plan_in_session": "retain: uninstall admission excludes active runs",
    },
    "recipe_operations/job_run_stop.py": {
        "_stop_logical_job_run": "retain: exact logical Stop admission",
    },
    "recipe_operations/reconciliation_authority.py": {
        "_reconciliation_authority_in_session": "retain: authority binds inactive runs",
    },
    "recipe_operations/start.py": {
        "enqueue_one_shot_job_in_session": "retain: capacity and current-run admission",
    },
    "recipe_operations/stop.py": {
        "_check_service_stop": "retain: Stop acceptance revalidates the reviewed run",
    },
    "recipe_operations/stop_acceptance.py": {
        "_accept_service_stop": "retain: duplicate Stop replay under the run fence",
    },
}


@pytest.mark.parametrize(
    "statement",
    [
        "select(Job).with_for_update()",
        "select(Job.id).with_for_update(nowait=True)",
        "select(Job).with_for_update(of=Job)",
        "session.get(Job, identity, with_for_update=True)",
        "session.refresh(row, with_for_update=True)",
        "session.refresh(row, with_for_update={'nowait': True})",
        "session.query(Job, with_for_update=True)",
        "session.get(Job, identity, with_for_update={'nowait': True})",
        "AdmissionRowLock('owner', Job, select(Job))",
        "select(Owner).with_for_update(of=Owner)",
        "update(Job).values(state=value)",
        "session.query(Job).update(values)",
    ],
)
@pytest.mark.parametrize("owner", ["Job", "RecipeRun"])
def test_guard_rejects_versioned_owner_locks(statement: str, owner: str) -> None:
    statement = statement.replace("Job", owner)
    assert _locks(f"from vonk_control.models import {owner} as Owner\n" + statement)


@pytest.mark.parametrize(
    "statement",
    [
        "select(Job).with_for_update(skip_locked=True)",
        "session.get(Job, identity, with_for_update={'skip_locked': True})",
        "select(Job).with_for_update(of=AgentNode)",
        "session.get(Job, identity)",
        "session.refresh(row)",
        "session.refresh(row, with_for_update=False)",
        "select(AgentNode).with_for_update()",
    ],
)
@pytest.mark.parametrize("owner", ["Job", "RecipeRun"])
def test_guard_allows_claims_and_other_owners(statement: str, owner: str) -> None:
    assert not _locks(statement.replace("Job", owner))


def test_unconverted_job_owners_are_named_for_follow_up() -> None:
    unlisted = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "control/src").rglob("*.py"))
        if _locks(path.read_text(), frozenset({"Job"}))
        and str(path.relative_to(ROOT)) not in FOLLOW_UP_JOB_LOCKS
    ]
    assert not unlisted, "Unconverted Job owners: " + ", ".join(unlisted)


@pytest.mark.parametrize(
    "path", ["control/src/vonk_control/new_owner.py", *FOLLOW_UP_JOB_LOCKS]
)
@pytest.mark.parametrize("owner", ["Job", "RecipeRun"])
def test_new_locks_fail_even_in_follow_up_files(path: str, owner: str) -> None:
    source = f"select({owner})\nselect({owner}).with_for_update()\n"
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


def test_remaining_recipe_run_lock_sites_have_a_verdict() -> None:
    """Catches new files AND unreviewed locks added to already classified files."""
    import ast

    actual: dict[str, set[str]] = {}
    root = ROOT / "control/src/vonk_control"
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        lines = set(_locks(source, frozenset({"RecipeRun"}), include_claims=True))
        if not lines:
            continue
        functions = {
            node.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and lines.intersection(
                range(node.lineno, (node.end_lineno or node.lineno) + 1)
            )
        }
        actual[str(path.relative_to(root))] = functions
    assert actual == {
        path: set(verdicts) for path, verdicts in RECIPE_RUN_LOCK_VERDICTS.items()
    }


def test_added_refresh_lock_cannot_hide_behind_unchanged_receiver() -> None:
    source = "session.refresh(\n    row,\n    with_for_update=True,\n)\n"
    assert check_source(
        "control/src/vonk_control/owner.py", source, {3}, modes=("optimistic",)
    )


@pytest.mark.parametrize(
    "binding",
    ["def f(row: AgentNode):", "def f():\n    row = session.get(AgentNode, identity)"],
)
def test_refresh_of_explicit_other_model_is_allowed(binding: str) -> None:
    source = (
        "from vonk_control.models import AgentNode\n"
        + binding
        + "\n    session.refresh(row, with_for_update=True)\n"
    )
    assert not _locks(source)


def test_recipe_run_writes_outside_transition_have_a_verdict() -> None:
    """A new helper or result-consumer call cannot silently inherit a verdict."""
    import ast

    from control.tests.optimistic_boundaries import scan_unwrapped_recipe_run_writes

    actual: dict[str, set[str]] = {}
    root = ROOT / "control/src/vonk_control"
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        lines = scan_unwrapped_recipe_run_writes(source)
        if not lines:
            continue
        tree = ast.parse(source)
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for line in lines:
            enclosing = [
                node
                for node in functions
                if node.lineno <= line <= (node.end_lineno or node.lineno)
            ]
            owner = min(
                enclosing,
                key=lambda node: (node.end_lineno or node.lineno) - node.lineno,
            )
            actual.setdefault(str(path.relative_to(root)), set()).add(
                writer_name(owner, parents)
            )
    assert actual == {
        path: set(verdicts) for path, verdicts in RECIPE_RUN_WRITE_VERDICTS.items()
    }
    for path in actual:
        source = (root / path).read_text()
        assert set(scan_unwrapped_recipe_run_writes(source)) == classified_write_lines(
            "control/src/vonk_control/" + path, source
        ), f"Re-review the changed writer in {path}"


@pytest.mark.parametrize(
    "write",
    ["row.version += 1", "row.route_state = state", "row.version = row.version + 1"],
)
@pytest.mark.parametrize(
    "binding",
    [
        "row = session.get(Run, identity)",
        "row = session.scalar(select(Run))",
        "rows = tuple(session.scalars(select(Run))); row = rows[0]",
    ],
)
def test_added_run_write_outside_transition_is_rejected(write, binding) -> None:
    source = (
        "from vonk_control.models import RecipeRun as Run\n"
        "def f():\n    with sessions.begin() as session:\n"
        f"        {binding}\n        {write}\n"
    )
    # Classifications never exempt additions, even in a reviewed file.
    for path in ["new_owner.py", *RECIPE_RUN_WRITE_VERDICTS]:
        path = "control/src/vonk_control/" + path
        assert check_source(path, source, {5}, modes=("optimistic",))
        assert not check_source(path, source, {1}, modes=("optimistic",))


def test_transition_write_and_other_model_write_are_allowed() -> None:
    source = (
        "from vonk_control.models import RecipeRun, AgentNode\n"
        "def f():\n    def decide(session):\n"
        "        row = session.get(RecipeRun, identity)\n"
        "        row.version += 1\n"
        "    return transition(sessions, decide)\n"
        "def g(session):\n    row = session.get(AgentNode, identity)\n"
        "    row.state = state\n"
    )
    assert not check_source(
        "control/src/vonk_control/owner.py",
        source,
        set(range(1, 10)),
        modes=("optimistic",),
    )


def test_added_result_consumer_cannot_hide_behind_unchanged_call_line() -> None:
    source = "def claim(session):\n    self._result_consumer(\n        session, operation, attempt, message\n    )\n"
    assert check_source(
        "control/src/vonk_control/agent_jobs/claim_authority.py",
        source,
        {3},
        modes=("optimistic",),
    )


def test_new_write_in_a_reviewed_function_requires_another_verdict() -> None:
    path = "control/src/vonk_control/recipe_routes/candidate.py"
    source = (ROOT / path).read_text()
    bump = "            run.version += 1"
    line = source.splitlines().index(bump) + 1
    assert not check_source(path, source, {line}, modes=("optimistic",))
    changed = source.replace(bump, bump + "\n" + bump, 1)
    assert check_source(path, changed, {line + 1}, modes=("optimistic",))


def test_added_run_bump_continuation_line_is_guarded() -> None:
    source = (
        "from vonk_control.models import RecipeRun\n"
        "def write(session):\n    row = session.get(RecipeRun, identity)\n"
        "    (\n        row\n    ).version += 1\n"
    )
    assert check_source(
        "control/src/vonk_control/new_owner.py", source, {6}, modes=("optimistic",)
    )


@pytest.mark.parametrize(
    "statement",
    [
        "select(AgentNode).where(AgentNode.node_id == node_id).with_for_update()",
        "select(AgentNode.node_id).with_for_update(of=AgentNode, skip_locked=True)",
        "session.get(AgentNode, node_id, with_for_update=True)",
        "update(AgentNode).where(AgentNode.node_id == node_id).values(state=state)",
    ],
)
def test_new_agent_node_lock_or_write_fails_outside_admission_and_security(
    statement: str,
) -> None:
    # Wrong implementation: a contact writer locking or updating the node row
    # admission locks (production 2026-10-25: capacity_busy, DeadlockDetected).
    source = f"from vonk_control.models import AgentNode\n{statement}\n"
    path = "control/src/vonk_control/agent_jobs/identity.py"
    assert check_source(path, source, {2}, modes=("optimistic",))
    assert not check_source(path, source, {1}, modes=("optimistic",))


@pytest.mark.parametrize(
    ("path", "statement"),
    [
        (
            "control/src/vonk_control/run_admission.py",
            'AdmissionRowLock("nodes", AgentNode, select(AgentNode))',
        ),
        (
            "control/src/vonk_control/enrollment/service.py",
            "select(AgentNode).with_for_update(of=AgentNode)",
        ),
        (
            "control/src/vonk_control/agent_jobs/identity.py",
            "session.get(AgentNode, node_id, populate_existing=True)",
        ),
    ],
)
def test_admission_rows_security_edges_and_plain_reads_may_touch_agent_nodes(
    path: str, statement: str
) -> None:
    source = f"from vonk_control.models import AgentNode\n{statement}\n"
    assert not check_source(path, source, {2}, modes=("optimistic",))


@pytest.mark.parametrize(
    "statement",
    [
        "contact.last_seen_at = now",
        "node.contact.management_address = address",
        "session.execute(update(AgentNodeContact).values(last_seen_at=now))",
        "row.contact_repeated_evidence = evidence",
        "session.add(AgentNodeContact(node_id=node_id))",
        "session.execute(delete(AgentNodeContact))",
        "row = session.get(AgentNodeContact, node_id)\nrow.last_seen_at = now",
    ],
)
def test_contact_facts_have_one_upsert_writer(statement: str) -> None:
    source = f"{statement}\n"
    written = set(range(1, statement.count("\n") + 2))
    assert check_source(
        "control/src/vonk_control/presence.py", source, written, modes=("optimistic",)
    )
    assert not check_source(
        "control/src/vonk_control/agent_contacts.py",
        source,
        {1},
        modes=("optimistic",),
    )
    assert not check_source(
        "control/src/vonk_control/presence.py",
        "if contact.last_seen_at == now:\n    pass\n",
        {1},
        modes=("optimistic",),
    )
    assert not check_source(
        "control/src/vonk_control/models/fleet.py",
        "class AgentNodeContact(Base):\n    pass\n",
        {1},
        modes=("optimistic",),
    )
    # Same-named fields of unrelated objects are not contact facts.
    assert not check_source(
        "control/src/vonk_control/presence.py",
        "observation.management_address = address\n",
        {1},
        modes=("optimistic",),
    )
