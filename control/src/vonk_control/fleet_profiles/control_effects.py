"""Control effects for Fleet profiles."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    DesiredAssignmentState,
    LifecycleState,
    ObservedAssignmentState,
    ProfileReasonCode,
)
from vonk_agent_protocol.agent_words import (
    ProfileAction,
    ProfileChildPhase,
    ProfileInstallationPolicy,
    ProfileReasonSeverity,
)

from .. import job_states
from ..fleet_profile_contract import (
    FleetProfileAssignment,
    FleetProfileEffects,
    FleetProfileInstallationEffect,
    FleetProfileReason,
    FleetProfileRunEffect,
)
from ..lifecycle.evidence import Residue
from ..models import (
    STOPPABLE_RUN_STATES,
    AgentNode,
    ClusterMappingNode,
    FleetProfileApplication,
    Job,
    RecipeInstallation,
    RecipeRun,
)
from ..preparation_contract import RuntimeImageIdentity
from ..run_switch_contract import RunSwitchProfileStopScope, SparkGroup, SparkGroupNode
from ..strict_json import warn_unreadable_once
from .contracts import (
    _ProfileControlEffects,
)
from .dependencies import _ACTIVE_INSTALL_STATES
from .persistence import (
    _persisted_profile_plan,
    _persisted_profile_progress,
    _persisted_profile_scope,
    _workload_intent_ordinal,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    @classmethod
    def _control_effects(
        cls,
        session: Session,
        resolved_assignments: tuple[FleetProfileAssignment, ...],
        target_nodes: set[str],
        installation_policy: str,
        *,
        expected_images: Mapping[str, RuntimeImageIdentity] | None = None,
        excluded_application_id: str | None = None,
    ) -> _ProfileControlEffects:
        """Reconcile SQL-owned effects without cache, planner or external work."""
        cls = _typing_cast("type[_FleetProfileService]", cls)  # noqa: PLW0642 -- assembled mixin interface
        adopted = cls._continuing_effects(
            session,
            resolved_assignments,
            target_nodes,
            installation_policy,
            excluded_application_id=excluded_application_id,
        )
        adopted_nodes = {node_id for effect in adopted for node_id in effect.node_ids}
        adopted_assignments = {
            identifier for effect in adopted for identifier in effect.assignment_ids
        }
        states = {
            assignment.id: cls._assignment_state(
                session,
                assignment,
                expected_image=(expected_images or {}).get(assignment.id),
            )
            for assignment in resolved_assignments
        }
        desired_installation_ids: set[str] = set()
        desired_run_ids: set[str] = set()
        run_effects: list[FleetProfileRunEffect] = []
        installation_effects: list[FleetProfileInstallationEffect] = []
        reasons: list[FleetProfileReason] = []
        changed_nodes: set[str] = set()
        unavailable_assignment_ids: set[str] = set()
        adapter_switch_needed = False
        for assignment in resolved_assignments:
            state = states[assignment.id]
            unavailable_assignment_ids.update(
                (assignment.id,)
                if {node.node_id for node in assignment.nodes} - target_nodes
                else ()
            )
            if state.installation is not None:
                desired_installation_ids.add(state.installation.id)
                installation_effects.append(
                    FleetProfileInstallationEffect(
                        installation_id=state.installation.id,
                        node_ids=list(
                            cls._installation_node_ids(session, state.installation.id)
                        ),
                        action=ProfileAction.KEEP.value,
                    )
                )
            if (
                state.run is not None
                and assignment.desired_state == DesiredAssignmentState.RUNNING
                and state.current_state == ObservedAssignmentState.RUNNING
            ):
                desired_run_ids.add(state.run.id)
            if (
                state.current_state != assignment.desired_state
                and assignment.id not in adopted_assignments
            ):
                assignment_targets = {
                    node.node_id for node in assignment.nodes
                } & target_nodes
                if assignment_targets:
                    adapter_switch_needed = True
                    changed_nodes.update(assignment_targets)

        # Every active run intersecting scope is reconciled to the desired
        # running set, independent of installation retention policy.  A
        # distributed run that crosses the boundary is a hard blocker: the
        # controller must never stop only the in-scope ranks.
        active_runs = tuple(
            session.scalars(
                select(RecipeRun)
                .where(RecipeRun.state.in_(STOPPABLE_RUN_STATES))
                .order_by(RecipeRun.created_at, RecipeRun.id)
            )
        )
        run_nodes = cls._run_nodes(session, [run.id for run in active_runs])
        if not resolved_assignments:
            # An empty assignment set is an explicit all-idle outcome.  If
            # the scope currently contains a run, route reconciliation
            # through the composite child so Run/Switch can stop the
            # complete distributed group exactly once.
            for run in active_runs:
                members = set(run_nodes.get(run.id, ()))
                members.update(cls._installation_node_ids(session, run.installation_id))
                if members & target_nodes:
                    adapter_switch_needed = True
                    changed_nodes.update(members & target_nodes)
            # A queued workload may not have created a Run yet.  An
            # explicit all-idle profile still has cancellation work in
            # that case; the adapter waits for issued cancellation receipts
            # before publishing its final no-workload receipt.
            for pending in session.scalars(
                select(Job).where(
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    )
                )
            ):
                if _workload_intent_ordinal(pending) is None:
                    continue
                members = set(pending.targets)
                if not members & target_nodes:
                    continue
                if not members <= target_nodes:
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.PENDING_CROSS_SCOPE,
                            detail="A pending workload crosses the selected idle scope.",
                            severity=ProfileReasonSeverity.ERROR.value,
                        )
                    )
                    continue
                adapter_switch_needed = True
                changed_nodes.update(members)
            for pending in session.scalars(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                )
            ):
                if pending.id == excluded_application_id:
                    continue
                if _persisted_profile_progress(pending).admission_pending:
                    continue
                _pending_plan = _persisted_profile_plan(pending)
                if isinstance(_pending_plan, Residue):
                    # The step list is unreadable, so the declared frozen
                    # scope is the only durable authority left for which
                    # nodes this order can still affect.  Never infer a
                    # narrower cleanup scope from a damaged document.
                    scope = _persisted_profile_scope(pending)
                    if scope is None:
                        # An unreadable record never blocks new work.
                        warn_unreadable_once("profile application", pending.id)
                        continue
                    members = set(scope)
                else:
                    members = {
                        node_id
                        for step in _pending_plan.steps
                        for node_id in step.node_ids
                    }
                if not members & target_nodes:
                    # An unrelated damaged record must not veto a fresh
                    # authorized profile; it stays queued for its own
                    # worker to quarantine.
                    continue
                if not members <= target_nodes:
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.PENDING_CROSS_SCOPE,
                            detail="A pending profile change crosses the selected idle scope.",
                            severity=ProfileReasonSeverity.ERROR.value,
                        )
                    )
                    continue
                adapter_switch_needed = True
                changed_nodes.update(members)
        for run in active_runs:
            members = set(run_nodes.get(run.id, ()))
            # Installation membership is the authoritative complete
            # placement even when a partial observation omitted a rank.
            members.update(cls._installation_node_ids(session, run.installation_id))
            intersection = members & target_nodes
            if not intersection:
                continue
            if members <= adopted_nodes:
                # The immutable borrowed plan owns transitional runs, including
                # its exact stops. This decision neither repeats nor cancels them.
                continue
            profile_stop_scope = None
            missing_members = members - target_nodes
            if missing_members:
                mapping_members = tuple(
                    session.scalars(
                        select(ClusterMappingNode)
                        .where(ClusterMappingNode.mapping_id == run.mapping_id)
                        .order_by(ClusterMappingNode.rank, ClusterMappingNode.node_id)
                    )
                )
                active_missing = tuple(
                    session.scalars(
                        select(AgentNode).where(
                            AgentNode.node_id.in_(missing_members),
                            AgentNode.revoked_at.is_(None),
                        )
                    )
                )
                if (
                    not intersection
                    or len(members) < 2
                    or {node.node_id for node in mapping_members} != members
                    or active_missing
                ):
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.DISTRIBUTED_CROSS_SCOPE,
                            detail=(
                                f"Running workload {run.alias} uses Sparks outside "
                                "the profile scope; review the complete distributed group."
                            ),
                            severity=ProfileReasonSeverity.ERROR.value,
                        )
                    )
                    continue
                original_group = SparkGroup(
                    nodes=[
                        SparkGroupNode(
                            node_id=node.node_id,
                            rank=node.rank,
                            role=node.role,
                            endpoint_owner=node.endpoint_owner,
                        )
                        for node in mapping_members
                    ]
                )
                profile_stop_scope = RunSwitchProfileStopScope(
                    original_group=original_group,
                    target_node_ids=sorted(intersection),
                    missing_node_ids=sorted(missing_members),
                )
            run_effects.append(
                FleetProfileRunEffect(
                    run_id=run.id,
                    installation_id=run.installation_id,
                    alias=run.alias,
                    node_ids=sorted(members),
                    action=ProfileAction.KEEP.value
                    if run.id in desired_run_ids
                    else ProfileChildPhase.STOP.value,
                    profile_stop_scope=profile_stop_scope
                    if run.id not in desired_run_ids
                    else None,
                )
            )
            if run_effects[-1].action == ProfileChildPhase.STOP.value:
                adapter_switch_needed = True
                changed_nodes.update(intersection)

        if (
            installation_policy == ProfileInstallationPolicy.EXACT.value
            and target_nodes
        ):
            installations = tuple(
                session.scalars(
                    select(RecipeInstallation)
                    .where(RecipeInstallation.state.in_(_ACTIVE_INSTALL_STATES))
                    .order_by(RecipeInstallation.created_at, RecipeInstallation.id)
                )
            )
            installation_nodes = cls._installation_nodes(
                session, [item.id for item in installations]
            )
            for installation in installations:
                nodes = installation_nodes.get(installation.id, ())
                node_ids = {node.node_id for node in nodes}
                if (
                    not node_ids.intersection(target_nodes)
                    or node_ids <= adopted_nodes
                    or installation.id in desired_installation_ids
                ):
                    continue
                if not node_ids <= target_nodes:
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.SHARED_INSTALLATION_SCOPE,
                            detail="Exact installation policy would affect a multi-Spark installation outside the profile scope.",
                            severity=ProfileReasonSeverity.ERROR.value,
                        )
                    )
                    continue
                # An all-idle exact profile still owns removal of scoped
                # stopped residue. Without a switch step the adapter never
                # receives this desired retention decision.
                adapter_switch_needed = True
                changed_nodes.update(node_ids)
                installation_effects.append(
                    FleetProfileInstallationEffect(
                        installation_id=installation.id,
                        node_ids=sorted(node_ids),
                        action="remove",
                    )
                )
                reasons.append(
                    FleetProfileReason(
                        code=ProfileReasonCode.CLEANUP_DELEGATED,
                        detail=(
                            "Run/Switch removes installation "
                            f"{installation.id} under this profile's "
                            "retention policy."
                        )[:512],
                        severity=ProfileReasonSeverity.INFO.value,
                    )
                )

        if adapter_switch_needed and active_runs:
            reasons.append(
                FleetProfileReason(
                    code=ProfileReasonCode.INTERRUPTION_EXPECTED,
                    detail=(
                        "The reviewed plan includes required runtime stops; "
                        "affected workloads may be unavailable until final starts complete."
                    ),
                    severity=ProfileReasonSeverity.WARNING.value,
                )
            )

        return _ProfileControlEffects(
            states=states,
            effects=FleetProfileEffects(
                runs=sorted(run_effects, key=lambda effect: effect.run_id),
                installations=sorted(
                    installation_effects, key=lambda effect: effect.installation_id
                ),
                superseded=cls._pending_effects(
                    session,
                    changed_nodes,
                    excluded_application_id=excluded_application_id,
                ),
                adopted=adopted,
            ),
            changed_nodes=changed_nodes,
            unavailable_assignment_ids=unavailable_assignment_ids,
            switch_needed=adapter_switch_needed,
            reasons=reasons,
        )
