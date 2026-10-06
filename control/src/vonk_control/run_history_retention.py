"""Evidence required before discarding a terminal run's recovery history."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import RecipeRun, RunNode
from .recipe_execution_contract import StoredRunPlan
from .stored_json import read_row_column


def run_absence_reconciled(session: Session, run: RecipeRun) -> bool:
    """Missing, damaged, or stale observations never prove processes absent."""
    plan = read_row_column(run, "plan")
    nodes = session.execute(
        select(
            RunNode.node_id,
            RunNode.rank,
            RunNode.observation_process_running,
            RunNode.observed_run_generation,
            RunNode.observation_observed_at,
        ).where(RunNode.run_id == run.id)
    ).all()
    return (
        isinstance(plan, StoredRunPlan)
        and bool(plan.nodes)
        and plan.run_generation == run.run_generation
        and plan.plan_digest == run.plan_digest
        and plan.installation_id == run.installation_id
        and plan.mapping_id == run.mapping_id
        and plan.mapping_generation == run.mapping_generation
        and {(node.node_id, node.rank) for node in plan.nodes}
        == {(node.node_id, node.rank) for node in nodes}
        and all(
            node.observation_process_running is False
            and node.observed_run_generation == run.run_generation
            and node.observation_observed_at is not None
            for node in nodes
        )
    )
