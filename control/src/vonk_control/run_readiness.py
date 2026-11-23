"""When a profile member's run serves: one rule for the load and its views.

A serving run serves once its route is published. A job run (``one-shot-jobs``)
never has an endpoint: it serves by holding its reservation, so its route stays
withdrawn by design, and nothing runs between submissions, so the freshness of
its ranks' runtime reports is no evidence either way. The profile load's final
verify, the profile assessment and the fleet presence all read a member through
this rule, so none of them calls a ready job member degraded and self-healing
never "repairs" one.

Whether a job run's Sparks can take a submission right now (agent active, fresh
inventory) is the load's own final-verify wait, not part of this rule: a late
inventory report must not make self-healing stop and re-activate a held run.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from pydantic import GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema
from vonk_agent_protocol.reason_codes.evidence import RunDegradedReason
from vonk_agent_protocol.state_machines import RouteState, RunState
from vonk_agent_protocol.wire_model import WireEnum

from .run_states import HOLDING_RUN_STATES, STARTING_RUN_STATES


def run_not_ready_for_profile(
    *,
    one_shot: bool,
    state: str,
    route_state: str,
    rank_states: Iterable[str],
    ranks_fresh: bool | None = None,
) -> RunDegradedReason | None:
    """Why the run does not serve its profile member; ``None`` when it does.

    The caller has already matched the run's ranks to the member's exact
    membership. ``ranks_fresh`` is ``None`` where a reader does not assess
    runtime freshness. A job run whose route is anything but withdrawn is not
    in its serving route state, reported as ``ROUTE_NOT_PUBLISHED``.
    """

    if state != RunState.RUNNING:
        return RunDegradedReason.RUN_NOT_RUNNING
    if any(rank_state != RunState.RUNNING for rank_state in rank_states):
        return RunDegradedReason.RANK_NOT_RUNNING
    if one_shot:
        return (
            None
            if route_state == RouteState.WITHDRAWN
            else RunDegradedReason.ROUTE_NOT_PUBLISHED
        )
    if ranks_fresh is False:
        return RunDegradedReason.RANK_STALE
    if route_state != RouteState.PUBLISHED:
        return RunDegradedReason.ROUTE_NOT_PUBLISHED
    return None


class GroupHealth(WireEnum):
    """What the fleet says about a loaded recipe group; one transition function."""

    HEALTHY = "healthy"
    #: Within its start: its owning Start operation fails the run past the
    #: start deadline, which is degraded; a group still starting is progress.
    STARTING = "starting"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """A plain string enum inline: the published shape stays a string field."""
        return {"type": "string", "enum": [member.value for member in cls]}


class GroupVerdict(NamedTuple):
    """A group's state and the tri-state ``healthy`` fact derived from it."""

    state: GroupHealth
    healthy: bool | None


def group_health(
    *,
    issue: bool,
    reason: RunDegradedReason | None,
    run_state: str,
    rank_states: Iterable[str],
) -> GroupVerdict:
    """The one verdict: unreadable evidence, then ready, then still starting.

    A group is starting while its run is planned or starting and no rank is
    failed, lost or stopping; a lost rank or a missing rank is degraded.
    ``healthy`` is true only for a ready group, false only for a degraded one.
    """
    if issue:
        return GroupVerdict(GroupHealth.UNAVAILABLE, None)
    if reason is None:
        return GroupVerdict(GroupHealth.HEALTHY, True)
    if (
        reason == RunDegradedReason.RUN_NOT_RUNNING
        and run_state in STARTING_RUN_STATES
        and all(state in HOLDING_RUN_STATES for state in rank_states)
    ):
        return GroupVerdict(GroupHealth.STARTING, None)
    return GroupVerdict(GroupHealth.DEGRADED, False)
