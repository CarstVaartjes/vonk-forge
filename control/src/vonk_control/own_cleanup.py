"""Cleanup of a record's own effect is authorised by identity, never by drift.

A cleanup removes what its own record put on a Spark. Which effect that is
follows from the stored order alone: its run, the container it targets, its
installation and its generation, on the node the order was sent to. What
changed since (the run's plan or generation, the revision, the mapping, the
node's workload intent, a cancellation and how long ago it was recorded, the
digests of the Controller's own rows) is bookkeeping drift and never blocks it.

It is refused only at a real edge: the caller's node, certificate and live
attempt fence (checked by the grant), and a container that is not the order's
own. A newer owner of the same container is fenced on the Spark itself: the
helper stops only a container whose labels carry exactly this identity, and
its generation fence keeps a newer generation's Start.
"""

from __future__ import annotations

from dataclasses import dataclass

from vonk_agent_protocol import ContainerRuntimeAction
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import RecipeStartPayload

from .lifecycle.agent_operation import OWNER_KIND as ARTIFACT_JOB_OWNER
from .models import Job
from .owner_liveness import OwnerKind

#: Container-runtime actions that remove the order's own effect: granted by
#: the order's identity, whatever drifted.
CLEANUP_ACTIONS = frozenset({ContainerRuntimeAction.STOP})
#: Every other container-runtime action, granted by current authority.
#: Installation cleanup stays here: its grant carries the node's current
#: workload intent, which the helper's installation fence trusts, so it waits
#: until cleanup joins its owning intent.
EFFECT_ACTIONS = frozenset(
    {
        ContainerRuntimeAction.RUNTIME_PREFLIGHT,
        ContainerRuntimeAction.IMAGE_PULL,
        ContainerRuntimeAction.IMAGE_INSPECT,
        ContainerRuntimeAction.RUN_INSPECT,
        ContainerRuntimeAction.START,
        ContainerRuntimeAction.INSTALLATION_CLEANUP,
    }
)


@dataclass(frozen=True, slots=True)
class OwnTarget:
    """The one container an order's own cleanup may remove."""

    run_id: str
    target_id: str
    installation_id: str
    run_generation: int


def own_runtime_target(
    parent: Job, order: RecipeStartPayload | RecipeJobRunRequest
) -> OwnTarget | None:
    """The container a lifecycle order started, from the stored order alone;
    ``None`` when the order is not its parent's own."""
    if isinstance(order, RecipeStartPayload):
        owner, target = (OwnerKind.RUN.value, order.run_id), order.run_id
    else:
        owner, target = (ARTIFACT_JOB_OWNER, order.job_id), order.job_id
    if (parent.payload.get("owner_kind"), parent.payload.get("owner_id")) != owner:
        return None
    return OwnTarget(
        run_id=order.run_id,
        target_id=target,
        installation_id=order.installation_id,
        run_generation=order.run_generation,
    )
