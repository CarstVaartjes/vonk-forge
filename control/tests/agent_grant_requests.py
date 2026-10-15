"""The agent's host-runtime grant requests, for tests whose claims are live.

Tests send these instead of building the agent's request themselves: what a
test hand-builds is the Controller's own idea of the agent, which hid #1579.
Each builder is pinned byte for byte to a shared vector that the agent's Rust
suite produces from its production builders
(``job_run_grant_requests_are_the_shared_vector_the_controller_grants``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from vonk_agent_protocol import ContainerRuntimeAction, canonical_message
from vonk_agent_protocol.host_helper import HostRuntimeRequest
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_control.agent_api import HostRuntimeGrantRequest
from vonk_control.recipe_stop_payloads import stop_payload_from_job_run

VECTORS = Path(__file__).parents[2] / "agent_protocol/src/vonk_agent_protocol/vectors"
JOB_RUN_GRANT_REQUESTS = "job-run-host-runtime-grant-requests-v1.json"
# The agent's grant lifetime (``HOST_RUNTIME_GRANT_TTL_SECONDS``).
GRANT_SECONDS = 10


def vector(name: str) -> dict:
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))


def _sha256(document: object) -> str:
    return hashlib.sha256(canonical_message(document)).hexdigest()


def job_run_grant_request(
    plan: RecipeJobRunRequest,
    *,
    fence: str,
    action: ContainerRuntimeAction,
    arguments: Sequence[str] = ("job-run",),
) -> HostRuntimeGrantRequest:
    """The agent's grant request for its JobRun Start, or its one Stop.

    ``arguments`` are the Start's; the Controller binds only their digest.

    The agent sends the same Stop, fencing any pending Start of the job, for
    cleanup after a failed run and for cancellation alike.
    """
    start_plan_sha256 = stop_plan_sha256 = None
    if action is ContainerRuntimeAction.START:
        request = HostRuntimeRequest(
            action="start",
            fence=fence,
            arguments=list(arguments),
            job_plan=plan,
            run_generation=plan.run_generation,
        )
        start_plan_sha256 = _sha256(plan)
    elif action is ContainerRuntimeAction.STOP:
        stop = stop_payload_from_job_run(plan, cancel_pending_start=True)
        request = HostRuntimeRequest(
            action="stop",
            fence=fence,
            arguments=[],
            stop_plan=stop,
            run_generation=stop.run_generation,
        )
        stop_plan_sha256 = _sha256(stop)
    else:
        raise ValueError(f"a JobRun asks no {action.value} grant")
    # Start and Stop alike target the job's own container.
    return HostRuntimeGrantRequest(
        fence=fence,
        action=action.value,
        request_sha256=_sha256(request),
        expires_in_seconds=GRANT_SECONDS,
        start_plan_sha256=start_plan_sha256,
        stop_plan_sha256=stop_plan_sha256,
        run_generation=plan.run_generation,
        runtime_run_id=plan.run_id,
        runtime_target_id=plan.job_id,
        runtime_installation_id=plan.installation_id,
    )
