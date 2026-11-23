"""The one edge that ends an unfinished run: an exact Stop, else lost.

A run whose Start ended unfinished holds claims and may hold launched effects.
The edge first withdraws the route and records the run as ``stopping``
(``withdraw_for_cleanup``, also written alone by a result transaction that
leaves the Stop to be queued after its commit). It then queues the exact
relational Stop under the given workload intent; with no exact Stop authority
nothing is invented and the run is ended as ``lost`` (or ``failed`` once its Sparks are
proven to have it gone), claims held until their next report settles it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from vonk_agent_protocol.agent_operations import AgentOperation as WireAgentOperation
from vonk_agent_protocol.recipe_operations import RecipeStopPayload
from vonk_agent_protocol.state_machines import RouteState, RunState
from vonk_agent_protocol.wire_model import canonical_message

from ..distributed_recovery.common import ended_run_state
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..models.execution import RecipeInstallation, RecipeRun, RunNode
from ..models.jobs import Job
from ..stored.json_values import read_row_column
from .observation_helpers import _active_recipe_revision
from .progress import _role_phases, _topology_order
from .stop_payloads import RecipeStopAuthorityError, durable_run_stop_payloads

if TYPE_CHECKING:
    from .service import RecipeOperationService


def withdraw_for_cleanup(run: RecipeRun, now: datetime, *, note: str) -> None:
    """Withdraw the route and record the run as ``stopping`` with ``note``.

    The head of ``end_unfinished_run``. A result transaction may write it alone
    and leave the Stop to be queued after its commit; it names no queued Stop,
    which only ``end_unfinished_run`` does once it finds or queues one.
    """

    run.route_state = RouteState.WITHDRAWN
    run.state = RunState.STOPPING
    run.route_error = note
    run.updated_at = now


def end_unfinished_run(
    service: RecipeOperationService,
    session: Session,
    run: RecipeRun,
    now: datetime,
    *,
    actor: str,
    request_id: str,
    ordinal: int | None,
    note: str,
) -> bool:
    """End an unfinished run: withdraw its route, then stop it or mark it lost.

    The run becomes ``stopping`` with its exact Stop queued under ``ordinal``
    (already queued under ``request_id``: left as it is). With no ordinal (no
    single intent owns its Sparks) the run is ``lost``, stoppable, so the owner
    of the Sparks still finds it; it is never left ``failed``. ``note`` leads
    the route error. Without exact Start
    authority nothing is invented: the run is ended as unknown (``lost``, or
    ``failed`` once its Sparks are proven to have it gone). True only when this
    call queued the Stop.
    """

    withdraw_for_cleanup(run, now, note=note)
    if ordinal is None:
        run.state = RunState.LOST
        run.route_error = f"{note}; cleanup awaits the owning intent"
        return False
    if session.scalar(select(Job.id).where(Job.request_id == request_id)):
        run.route_error = f"{note}; cleanup queued"
        return False

    stop_nodes = tuple(
        session.scalars(
            select(RunNode).where(RunNode.run_id == run.id).order_by(RunNode.rank)
        )
    )
    installation = session.get(RecipeInstallation, run.installation_id)
    revision = (
        _active_recipe_revision(session, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    # An unreadable document (a Residue) has no role order: scheduling only.
    document = read_row_column(revision, "document") if revision is not None else None
    stop_order = (
        _topology_order(document, "stop_order") if revision is not None else None
    )
    try:
        exact_stop_payloads: Mapping[str, RecipeStopPayload] | None = (
            durable_run_stop_payloads(
                session,
                run,
                stop_nodes,
                run_generation=run.run_generation,
                cancel_pending_start=True,
                allow_missing_nodes=False,
            )
        )
    except RecipeStopAuthorityError:
        exact_stop_payloads = None
    if exact_stop_payloads is None:
        # No exact Start authority to stop from: nothing is invented.  What
        # the Start launched is unknown, so the run is lost (claims held)
        # unless its Sparks already report it gone; their next report settles it.
        retire_as_unknown(
            "recipe.start-cleanup",
            run.id,
            BookkeepingReason.ROW_INCOMPLETE,
            "failed Start lacks exact cleanup authority",
        )
        run.state = ended_run_state(run, stop_nodes, now)
        run.route_error = (
            "failed recipe Start lacks exact cleanup authority; "
            "claims held until its Sparks report it gone"
        )
        return False
    stop_node_payloads = tuple(
        (
            run_node.node_id,
            json.loads(canonical_message(exact_stop_payloads[run_node.node_id])),
        )
        for run_node in stop_nodes
    )
    service._queue_in_session(
        session,
        kind=WireAgentOperation.RECIPE_STOP.value,
        owner_kind="run",
        owner_id=run.id,
        plan_digest=run.plan_digest,
        actor=actor,
        request_id=request_id,
        node_payloads=stop_node_payloads,
        # Exact relational Stop authority above already binds every target.
        # An unreadable role order changes scheduling, never target membership.
        phases=(
            _role_phases(stop_order, stop_node_payloads)
            if stop_order is not None
            else None
        )
        or (stop_node_payloads,),
        authority_digest=run.plan_digest.removeprefix("sha256:"),
        now=now,
        workload_intent_ordinal=ordinal,
    )
    run.route_error = f"{note}; cleanup queued"
    return True
