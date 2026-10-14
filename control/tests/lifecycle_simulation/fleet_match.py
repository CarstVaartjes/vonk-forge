"""Whether the fleet runs what a succeeded profile load says it runs.

A load that ends ``succeeded`` while it is the selected profile claims the
fleet *is* that profile. That is judged from what the Controller shows: the
load's accepted profile (its view), its reviewed plan, the run rows and their
ranks, and the run-switch receipts that started each run.

- On every Spark the profile covers, the live runs are exactly its running
  assignments there (same recipe revision and alias), each serving by the
  load's own readiness rule (``run_readiness``: a service run published, a job
  run held with its route withdrawn); an idle profile leaves nothing running.
- Every such run belongs to the load's intent: the load (or a load it
  retries) started it, its plan keeps it as it is, or its plan adopted the
  load that started it as the continuing executor on that Spark.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_control.fleet_profile_contract import FleetProfilePreview
from vonk_control.models import (
    ACTIVE_RUN_STATES,
    FleetProfileApplication,
    FleetProfileSelection,
    Job,
    RecipeRun,
    RunNode,
)
from vonk_control.recipe_operations.rank_authority import _run_is_one_shot
from vonk_control.run_readiness import run_not_ready_for_profile
from vonk_control.run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchStartResult,
)
from vonk_control.stored_json import read_row_column

from .world import World

#: A run is its recipe revision under its endpoint alias.
type _Placement = tuple[str, str | None]


def fleet_mismatch(world: World, application_id: str) -> str | None:
    """What the fleet runs that the succeeded load does not say, or ``None``.

    Only the selected profile (or a retry of it) claims the fleet: a load that
    succeeded as the continuing executor of a newer one claims its own part.
    """

    view = world.controller.profiles.application(application_id)
    intended = view.progress.intended_profile
    if intended is None:
        return "it names no accepted profile"
    covered = set(intended.scope.node_ids)
    wanted: dict[str, Counter[_Placement]] = {node: Counter() for node in covered}
    for assignment in intended.assignments:
        if assignment.desired_state != "running":
            continue
        for node in assignment.nodes:
            wanted[node.node_id][(assignment.recipe_revision_id, assignment.alias)] += 1
    with world.sessions() as session:
        lineage = _lineage(session, application_id)
        selection = session.get(FleetProfileSelection, 1)
        if selection is None or selection.application_id not in lineage:
            return None
        starters, kept = _intent(session, lineage, covered)
        live = session.execute(
            select(RecipeRun, RunNode.node_id)
            .join(RunNode, RunNode.run_id == RecipeRun.id)
            .where(
                RecipeRun.state.in_(tuple(ACTIVE_RUN_STATES)),
                RunNode.node_id.in_(covered),
            )
            .order_by(RecipeRun.id, RunNode.node_id)
        ).all()
        started = _start_receipts(session)
        unready = {run.id: _not_serving(session, run) for run, _node_id in live}
    found: dict[str, Counter[_Placement]] = {node: Counter() for node in covered}
    problems: list[str] = []
    for run, node_id in live:
        found[node_id][(str(run.plan.get("recipe_revision_id")), run.alias)] += 1
        if unready[run.id] is not None:
            problems.append(
                f"run {run.id} on {node_id} does not serve: {unready[run.id]}"
            )
        starter = started.get(run.id)
        if run.id not in kept and starter not in starters[node_id]:
            problems.append(
                f"run {run.id} on {node_id} was started by load {starter}, outside"
                " the load's intent"
            )
    for node_id in sorted(covered):
        if found[node_id] != wanted[node_id]:
            problems.append(
                f"{node_id} runs {sorted(found[node_id].elements())}, the profile"
                f" {sorted(wanted[node_id].elements())}"
            )
    return "; ".join(problems) or None


def _not_serving(session: Session, run: RecipeRun) -> str | None:
    """Why the run does not serve its member, by the load's own readiness rule."""

    ranks = session.scalars(select(RunNode.state).where(RunNode.run_id == run.id))
    reason = run_not_ready_for_profile(
        one_shot=_run_is_one_shot(session, run),
        state=run.state,
        route_state=run.route_state,
        rank_states=tuple(ranks),
    )
    return None if reason is None else str(reason)


def _lineage(session: Session, application_id: str) -> set[str]:
    """The load and the loads it retries."""

    lineage: set[str] = set()
    current = session.get(FleetProfileApplication, application_id)
    while current is not None and current.id not in lineage:
        lineage.add(current.id)
        progress = current.progress if isinstance(current.progress, dict) else {}
        parent = progress.get("retry_of_application_id")
        current = session.get(FleetProfileApplication, parent) if parent else None
    return lineage


def _intent(
    session: Session, lineage: set[str], covered: set[str]
) -> tuple[dict[str, set[str]], set[str]]:
    """Per covered Spark, the loads whose starts are the load's intent there;
    and the runs its plans keep as they are."""

    starters = {node_id: set(lineage) for node_id in covered}
    kept: set[str] = set()
    for load_id in sorted(lineage):
        plan = _plan(session, load_id)
        if plan is None:
            continue
        kept |= _kept(plan, covered)
        for effect in plan.effects.adopted:
            owner = _plan(session, effect.application_id)
            if owner is not None:
                kept |= _kept(owner, set(effect.node_ids))
            for node_id in effect.node_ids:
                starters.setdefault(node_id, set()).add(effect.application_id)
    return starters, kept


def _plan(session: Session, application_id: str) -> FleetProfilePreview | None:
    row = session.get(FleetProfileApplication, application_id)
    plan = None if row is None else read_row_column(row, "plan")
    return plan if isinstance(plan, FleetProfilePreview) else None


def _kept(plan: FleetProfilePreview, nodes: set[str]) -> set[str]:
    return {
        effect.run_id
        for effect in plan.effects.runs
        if effect.action == "keep" and set(effect.node_ids) <= nodes
    }


def _start_receipts(session: Session) -> dict[str, str | None]:
    """Each started run's load, from the run-switch receipt that started it."""

    receipts: dict[str, str | None] = {}
    for job in session.scalars(select(Job).order_by(Job.created_at, Job.id)):
        result = read_row_column(job, "result")
        if not isinstance(result, RunSwitchOperationResult):
            continue
        for phase in result.phase_results:
            if isinstance(phase, RunSwitchStartResult):
                receipts[phase.run_id] = result.profile_application_id
    return receipts
