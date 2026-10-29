"""The swap a Spark can lend the next load phase.

A load may rise above its steady demand (the reservation) while it loads;
swap carries that rise. Ranks that are still planned or starting hold their
admitted rise (``StoredRunNodePlan.load_memory_bytes`` above
``required_memory_bytes``) until the rank runs. A one-shot job run's ranks
run from activation and each job loads again, so they hold it while the run
is active.
"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import RunState
from vonk_agent_protocol.inventory import HostSettings
from vonk_agent_protocol.swap import GIB

from .models import RecipeRun, RunNode
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_plan,
)
from .resource_planning import SwapCapacity, SwapHolder
from .swap import selected_swap

_LOADING = (RunState.PLANNED, RunState.STARTING)
_HOLDING = (*_LOADING, RunState.RUNNING)


def swap_capacity(
    session: Session,
    node_id: str,
    host_settings: object,
    *,
    excluded_run_ids: Collection[str] = (),
) -> SwapCapacity:
    """Reported swap (none when the agent reports none) and what loads hold."""

    observation = (
        host_settings.swap if isinstance(host_settings, HostSettings) else None
    )
    holders: list[SwapHolder] = []
    for run, rank_state in session.execute(
        select(RecipeRun, RunNode.state)
        .join(RunNode, RunNode.run_id == RecipeRun.id)
        .where(
            RunNode.node_id == node_id,
            RunNode.state.in_(_HOLDING),
            RecipeRun.id.not_in(excluded_run_ids),
        )
    ):
        try:
            plan = parse_stored_run_plan(run.plan)
        except RecipeExecutionContractError:
            # Its steady claim still counts; a damaged plan names no rise.
            continue
        rank = next((node for node in plan.nodes if node.node_id == node_id), None)
        if (
            rank is not None
            and plan.plan_digest == run.plan_digest
            and rank.load_memory_bytes is not None
            # A running service has loaded; a job run loads per job.
            and (rank_state in _LOADING or plan.execution_mode is not None)
        ):
            holders.append(
                SwapHolder(
                    run.id,
                    max(0, rank.load_memory_bytes - rank.required_memory_bytes),
                    job_run=plan.execution_mode is not None,
                )
            )
    return SwapCapacity(
        total_bytes=observation.total_bytes if observation is not None else 0,
        used_bytes=max(0, observation.total_bytes - observation.free_bytes)
        if observation is not None
        else 0,
        holders=tuple(holders),
        desired_bytes=selected_swap(session, node_id).size_gib * GIB,
    )
