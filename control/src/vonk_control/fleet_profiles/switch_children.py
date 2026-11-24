"""How a profile reads its Run/Switch children: assignment intent, queue nodes, endings."""

from __future__ import annotations

from vonk_agent_protocol.agent_words import (
    Disposition,
    ProfileAction,
    ProfileChildPhase,
    ProfileSwitchChildKind,
)
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.reason_codes.runtime import RuntimeImageCode
from vonk_agent_protocol.state_machines import DesiredAssignmentState
from vonk_control.failures.classification import is_redownload, is_security_failure
from vonk_control.fleet_profile_contract.definitions import FleetProfileAssignment
from vonk_control.fleet_profile_contract.switch_state import FleetProfileSwitchQueueItem
from vonk_control.fleet_profiles.persistence import (
    _persisted_profile_plan,
    _persisted_profile_progress,
)
from vonk_control.jobs import states as job_states
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models.fleet import FleetProfileApplication
from vonk_control.run_switch_contract.operations import RunSwitchOperation
from vonk_control.run_switch_contract.vocabulary import RunSwitchPlacementAction


def _cache_loss(child: RunSwitchOperation) -> bool:
    """A typed, pre-effect Controller cache loss: the replay prepares it again."""

    result = child.result
    return (
        child.state == LifecycleState.FAILED.value
        and result is not None
        and result.phase == ProfileChildPhase.PREPARE.value
        and result.subphase == "runtime-image"
        and result.child_operation_id is None
        and result.failure_code == RuntimeImageCode.CACHE_MISSING
    )


def _ending_disposition(child: RunSwitchOperation) -> Disposition:
    """What a Run/Switch child's ending lets profile recovery do.

    The Run/Switch owner ends a definite failure (a ``FAIL`` or ``SECURITY``
    :func:`~vonk_control.failures.classification.disposition`) not retryable,
    under its typed code; only an observation window that expired ends
    retryable.  A replay can change bytes it fetches again (a cache loss, a
    digest mismatch), and an ending its owner never typed is unknown: those are
    observed again within the repeat budget.  A child that has not failed has
    no ending to judge.
    """

    result = child.result
    code = None if result is None else result.failure_code
    if is_security_failure(code):
        return Disposition.SECURITY
    if _cache_loss(child):
        return Disposition.WAIT
    if (
        child.state not in job_states.words(LifecycleState.FAILED)
        or result is None
        or code is None
        or result.retryable
        or is_redownload(code)
    ):
        return Disposition.OBSERVE
    return Disposition.FAIL


def _assignment_intent(
    assignment: FleetProfileAssignment,
) -> tuple[RunSwitchPlacementAction, str]:
    return (
        ProfileSwitchChildKind.INSTALL.value
        if assignment.desired_state == DesiredAssignmentState.INSTALLED
        else ProfileAction.SWITCH.value,
        assignment.alias or assignment.id,
    )


def _queue_item_nodes(
    application: FleetProfileApplication, item: FleetProfileSwitchQueueItem
) -> tuple[str, ...]:
    reviewed = _persisted_profile_plan(application)
    progress = _persisted_profile_progress(application)
    if isinstance(reviewed, Residue) or progress.intended_profile is None:
        return ()
    if item.kind in {
        ProfileSwitchChildKind.RUN.value,
        ProfileSwitchChildKind.INSTALL.value,
    }:
        assignment = next(
            (
                value
                for value in progress.intended_profile.assignments
                if value.id == item.id
            ),
            None,
        )
        return (
            tuple(sorted(node.node_id for node in assignment.nodes))
            if assignment
            else ()
        )
    if item.kind == ProfileChildPhase.STOP.value:
        effect = next(
            (
                value
                for value in reviewed.effects.runs
                if value.run_id == item.id
                and value.action == ProfileChildPhase.STOP.value
            ),
            None,
        )
        if effect is None:
            return ()
        return tuple(
            effect.profile_stop_scope.target_node_ids
            if effect.profile_stop_scope
            else effect.node_ids
        )
    effect = next(
        (
            value
            for value in reviewed.effects.installations
            if value.installation_id == item.id and value.action == "remove"
        ),
        None,
    )
    return tuple(effect.node_ids) if effect else ()
