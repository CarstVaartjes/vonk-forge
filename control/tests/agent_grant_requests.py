"""The agent's host-runtime grant requests, for tests whose claims are live.

Tests send these instead of building the agent's request themselves: what a
test hand-builds is the Controller's own idea of the agent, which hid #1579.
"""

from __future__ import annotations

import hashlib

from vonk_agent_protocol import ContainerRuntimeAction, canonical_message
from vonk_agent_protocol.host_helper import HostRuntimeRequest
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import (
    RecipeStartPayload,
    RecipeStopPayload,
)
from vonk_control.agent_api import HostRuntimeGrantRequest
from vonk_control.recipe_stop_payloads import (
    stop_payload_from_job_run,
    stop_payload_from_start,
)

# The agent's grant lifetime (``HOST_RUNTIME_GRANT_TTL_SECONDS``).
GRANT_SECONDS = 10


def _sha256(document: object) -> str:
    return hashlib.sha256(canonical_message(document)).hexdigest()


def own_stop_grant_request(
    order: RecipeStartPayload | RecipeJobRunRequest,
    *,
    fence: str,
    cancel_pending_start: bool,
) -> HostRuntimeGrantRequest:
    """The agent's grant request for the Stop of its own order's container.

    The agent derives the Stop from its claim (``exact_stop_plan_from_claim``)
    and chooses the flag by the path it takes: a Start order sends ``False`` to
    clean up after readiness fails or to heal a retained run and ``True`` when
    it cancels; a JobRun always sends ``True``.
    """
    if isinstance(order, RecipeStartPayload):
        stop = stop_payload_from_start(order, cancel_pending_start=cancel_pending_start)
    else:
        stop = stop_payload_from_job_run(
            order, cancel_pending_start=cancel_pending_start
        )
    return stop_grant_request(stop, fence=fence)


def stop_grant_request(
    stop: RecipeStopPayload, *, fence: str
) -> HostRuntimeGrantRequest:
    """The agent's grant request for a Stop document: a Stop order's claim as
    it was sent, or the Stop the agent derived for its own order."""
    request = HostRuntimeRequest(
        action="stop",
        fence=fence,
        arguments=[],
        stop_plan=stop,
        run_generation=stop.run_generation,
    )
    return HostRuntimeGrantRequest(
        fence=fence,
        action=ContainerRuntimeAction.STOP.value,
        request_sha256=_sha256(request),
        expires_in_seconds=GRANT_SECONDS,
        stop_plan_sha256=_sha256(stop),
        run_generation=stop.run_generation,
        runtime_run_id=stop.run_id,
        runtime_target_id=stop.target_runtime_id,
        runtime_installation_id=stop.installation_id,
    )
