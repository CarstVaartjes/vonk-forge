"""Continuing effects for Fleet profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import LifecycleState
from vonk_agent_protocol.agent_words import (
    ProfileChildJobKind,
    ProfileChildPhase,
    ProfileProjectionKind,
)

from .. import job_states
from ..fleet_profile_contract import (
    FleetProfileAdoptedApplicationEffect,
    FleetProfileAdoptedStopEffect,
    FleetProfileAssignment,
    FleetProfilePendingEffect,
    FleetProfileSwitchChildState,
    profile_switch_child_request_key,
)
from ..job_documents import RunSwitchJobPayload
from ..lifecycle.evidence import Residue
from ..models import AgentNode, FleetProfileApplication, FleetProfileSelection, Job
from ..run_switch_contract import RunSwitchOperationResult
from ..stored_json import read_row_column
from .contracts import (
    FleetProfileConflict,
)
from .persistence import (
    _persisted_profile_plan,
    _persisted_profile_progress,
    _persisted_profile_scope,
    _workload_intent_ordinal,
)
from .projection_support import (
    _digest,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    @staticmethod
    def _stop_binding_is_exact(
        session: Session,
        application: FleetProfileApplication,
        stop: FleetProfileAdoptedStopEffect,
    ) -> bool:
        progress = _persisted_profile_progress(application)
        document = progress.switch_adapter
        if document is None or stop.queue_index >= len(document.queue):
            return False
        item = document.queue[stop.queue_index]
        if (
            item.kind != ProfileChildPhase.STOP.value
            or item.id != stop.effect.run_id
            or item.profile_stop_scope != stop.effect.profile_stop_scope
        ):
            return False
        request_key = profile_switch_child_request_key(
            application.id, stop.queue_index, item.kind, item.id
        )
        if request_key != stop.request_key:
            return False
        record = next(
            (
                child
                for child in (*document.pending_children, *document.children)
                if child.queue_index == stop.queue_index
                and child.kind == ProfileChildPhase.STOP.value
                and (
                    child.operation_id == stop.operation_id
                    or child.original_operation_id == stop.operation_id
                )
            ),
            None,
        )
        if record is None:
            return False
        job = session.get(Job, record.operation_id)
        if (
            job is None
            or job.kind != ProfileChildJobKind.STOP.value
            or job.request_id != request_key
            or not isinstance(job.payload, dict)
            or job.payload_digest != _digest(job.payload)
        ):
            return False
        payload = read_row_column(job, "payload")
        child_result = read_row_column(job, "result")
        if not isinstance(payload, RunSwitchJobPayload) or not isinstance(
            child_result, RunSwitchOperationResult
        ):
            return False
        child_plan = payload.plan
        return (
            child_plan.action == ProfileChildPhase.STOP.value
            and child_plan.run_id == stop.effect.run_id
            and child_plan.installation_id == stop.effect.installation_id
            and child_plan.profile_stop_scope == stop.effect.profile_stop_scope
            and sorted(node.node_id for node in child_plan.spark_group.nodes)
            == stop.effect.node_ids
            and sorted(job.targets) == stop.effect.node_ids
            and child_result.profile_application_id == application.id
            and child_result.workload_intent_ordinal == progress.workload_intent_ordinal
        )

    @staticmethod
    def _adopted_application_scope(
        session: Session, application: FleetProfileApplication
    ) -> tuple[str, ...] | None:
        """Read continuing authority exclusively from the selected bound plan.

        Keeping the original executor is an effect of a newer accepted decision,
        never a second selected profile. Cancellation, altered identity and a
        missing member invalidate that effect instead of guessing ownership.
        """
        from .service import FleetProfileService

        selection = session.get(FleetProfileSelection, 1)
        if selection is None or selection.application_id == application.id:
            return None
        selected = session.get(FleetProfileApplication, selection.application_id)
        if selected is None or selected.selection_generation != selection.generation:
            return None
        selected_plan = _persisted_profile_plan(selected)
        original_plan = _persisted_profile_plan(application)
        original_progress = _persisted_profile_progress(application)
        selected_progress = _persisted_profile_progress(selected)
        if (
            isinstance(selected_plan, Residue)
            or isinstance(original_plan, Residue)
            or original_progress.cancellation is not None
            or selected_progress.cancellation is not None
            or selected_progress.intended_profile is None
            or original_progress.intended_profile is None
            or selected_progress.intended_profile.installation_policy
            != original_progress.intended_profile.installation_policy
        ):
            return None
        links = [
            item
            for item in selected_plan.effects.adopted
            if item.application_id == application.id
        ]
        if len(links) != 1:
            return None
        link = links[0]
        if (
            link.plan_digest != application.plan_digest
            or link.workload_intent_ordinal != original_progress.workload_intent_ordinal
        ):
            return None
        old = {item.id: item for item in original_progress.intended_profile.assignments}
        desired = {
            item.id: item for item in selected_progress.intended_profile.assignments
        }
        if any(
            identifier not in old or old[identifier] != desired.get(identifier)
            for identifier in link.assignment_ids
        ):
            return None
        scope = {
            node.node_id
            for identifier in link.assignment_ids
            for node in old[identifier].nodes
        }
        original_state = original_progress.switch_adapter
        desired_nodes = {
            node.node_id
            for item in selected_progress.intended_profile.assignments
            for node in item.nodes
        }
        for stop in link.stops:
            if not FleetProfileService._stop_binding_is_exact(
                session, application, stop
            ):
                return None
            if original_state is None or stop.queue_index >= len(original_state.queue):
                return None
            item = original_state.queue[stop.queue_index]
            if (
                item.kind != ProfileChildPhase.STOP.value
                or item.id != stop.effect.run_id
                or item.profile_stop_scope != stop.effect.profile_stop_scope
            ):
                return None
            if stop.request_key != profile_switch_child_request_key(
                application.id, stop.queue_index, item.kind, item.id
            ):
                return None
            if (
                stop.effect not in original_plan.effects.runs
                or set(stop.effect.node_ids) & desired_nodes
            ):
                return None
            records = (*original_state.pending_children, *original_state.children)
            if not any(
                child.queue_index == stop.queue_index
                and (
                    child.operation_id == stop.operation_id
                    or child.original_operation_id == stop.operation_id
                )
                and child.kind == ProfileChildPhase.STOP.value
                for child in records
            ):
                return None
            scope.update(stop.effect.node_ids)
        if scope != set(link.node_ids):
            return None
        original_preparations = {
            item.assignment_id: item for item in original_plan.preparation_decisions
        }
        selected_preparations = {
            item.assignment_id: item for item in selected_plan.preparation_decisions
        }
        if any(
            original_preparations.get(identifier)
            != selected_preparations.get(identifier)
            for identifier in link.assignment_ids
        ):
            return None
        nodes = tuple(
            session.execute(
                select(AgentNode.node_id, AgentNode.workload_intent_ordinal)
                .where(AgentNode.node_id.in_(scope), AgentNode.revoked_at.is_(None))
                .order_by(AgentNode.node_id)
            )
        )
        if nodes != tuple(
            (node_id, link.workload_intent_ordinal) for node_id in link.node_ids
        ):
            return None
        return tuple(link.node_ids)

    @classmethod
    def _continuing_effects(
        cls,
        session: Session,
        assignments: tuple[FleetProfileAssignment, ...],
        target_nodes: set[str],
        installation_policy: str,
        *,
        excluded_application_id: str | None = None,
    ) -> list[FleetProfileAdoptedApplicationEffect]:
        """Bind complete equivalent assignment scopes to their original owners."""
        cls = _typing_cast("type[_FleetProfileService]", cls)  # noqa: PLW0642 -- assembled mixin interface
        desired = {item.id: item for item in assignments}
        effects: list[FleetProfileAdoptedApplicationEffect] = []
        owned_nodes: set[str] = set()
        for application in session.scalars(
            select(FleetProfileApplication)
            .where(
                FleetProfileApplication.state.in_(
                    job_states.words(
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.NEEDS_OPERATOR,
                    )
                )
            )
            .order_by(FleetProfileApplication.created_at, FleetProfileApplication.id)
        ):
            if application.id == excluded_application_id:
                continue
            try:
                plan = _persisted_profile_plan(application)
                progress = _persisted_profile_progress(application)
            except (FleetProfileConflict, ValidationError, TypeError, ValueError):
                continue
            if (
                isinstance(plan, Residue)
                or progress.admission_pending
                or progress.cancellation is not None
                or progress.intended_profile is None
                or progress.workload_intent_ordinal is None
            ):
                continue
            if not cls._application_is_current_selection(
                session, application, progress
            ):
                continue
            if progress.intended_profile.installation_policy != installation_policy:
                continue
            if (
                not progress.switch_adapter
                and {item.id: item for item in progress.intended_profile.assignments}
                == desired
            ):
                # An explicit repeat of the entire same profile remains a new
                # request. Continuity only preserves independent unchanged work
                # while the newer decision changes another assignment.
                continue
            effect_nodes = {node_id for step in plan.steps for node_id in step.node_ids}
            existing_adopted_scope = cls._adopted_application_scope(
                session, application
            )
            if existing_adopted_scope:
                effect_nodes &= set(existing_adopted_scope)
            repeating_assignments = {
                item.id: item for item in progress.intended_profile.assignments
            } == desired
            equivalent = [
                item
                for item in progress.intended_profile.assignments
                if not repeating_assignments
                and item == desired.get(item.id)
                and {node.node_id for node in item.nodes} <= target_nodes
                and {node.node_id for node in item.nodes} <= effect_nodes
            ]
            scope = {node.node_id for item in equivalent for node in item.nodes}
            # Stop/removal effects remain topology-atomic too. A cross-boundary
            # effect cannot be borrowed merely because one rank is unchanged.
            for effect in (*plan.effects.runs, *plan.effects.installations):
                members = set(effect.node_ids)
                if members & scope and not members <= scope:
                    scope -= members
            equivalent = [
                item
                for item in equivalent
                if {node.node_id for node in item.nodes} <= scope
            ]
            scope = {node.node_id for item in equivalent for node in item.nodes}
            stops: list[FleetProfileAdoptedStopEffect] = []
            document = progress.switch_adapter
            desired_nodes = {
                node.node_id for item in assignments for node in item.nodes
            }
            if document is not None:
                for child in (*document.pending_children, *document.children):
                    if child.kind != ProfileChildPhase.STOP.value:
                        continue
                    item = document.queue[child.queue_index]
                    effect = next(
                        (
                            effect
                            for effect in plan.effects.runs
                            if effect.action == ProfileChildPhase.STOP.value
                            and effect.run_id == item.id
                        ),
                        None,
                    )
                    if effect is None or effect.profile_stop_scope is not None:
                        # A removed-rank gang still binds its original complete
                        # topology; membership reconciliation owns that cleanup.
                        continue
                    members = set(effect.node_ids)
                    if (
                        members & desired_nodes
                        or not members <= target_nodes
                        or not members <= effect_nodes
                    ):
                        continue
                    if (
                        isinstance(child, FleetProfileSwitchChildState)
                        and child.state != LifecycleState.SUCCEEDED.value
                    ):
                        continue
                    binding = FleetProfileAdoptedStopEffect(
                        effect=effect,
                        queue_index=child.queue_index,
                        operation_id=child.original_operation_id or child.operation_id,
                        request_key=profile_switch_child_request_key(
                            application.id, child.queue_index, item.kind, item.id
                        ),
                    )
                    if not cls._stop_binding_is_exact(session, application, binding):
                        continue
                    stops.append(binding)
                    scope.update(members)
            if not scope or scope & owned_nodes:
                continue
            nodes = tuple(
                session.execute(
                    select(AgentNode.node_id, AgentNode.workload_intent_ordinal)
                    .where(AgentNode.node_id.in_(scope), AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
                )
            )
            if nodes != tuple(
                (node_id, progress.workload_intent_ordinal) for node_id in sorted(scope)
            ):
                continue
            effects.append(
                FleetProfileAdoptedApplicationEffect(
                    application_id=application.id,
                    plan_digest=application.plan_digest,
                    workload_intent_ordinal=progress.workload_intent_ordinal,
                    node_ids=sorted(scope),
                    assignment_ids=sorted(item.id for item in equivalent),
                    stops=sorted(stops, key=lambda stop: stop.queue_index),
                )
            )
            owned_nodes.update(scope)
        return sorted(effects, key=lambda item: item.application_id)

    @staticmethod
    def _pending_effects(
        session: Session,
        changed_nodes: set[str],
        *,
        excluded_application_id: str | None = None,
    ) -> list[FleetProfilePendingEffect]:
        """Identify older orders whose effects the new decision supersedes."""
        effects: list[FleetProfilePendingEffect] = []
        if not changed_nodes:
            return effects
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
            if members & changed_nodes:
                effects.append(
                    FleetProfilePendingEffect(
                        kind=ProfileProjectionKind.JOB.value,
                        id=pending.id,
                        node_ids=sorted(members),
                    )
                )
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
            # A parked receipt is not accepted selection authority, but a
            # fresh reviewed load still needs to show that accepting it
            # will retire the older overlapping receipt.
            plan = _persisted_profile_plan(pending)
            if isinstance(plan, Residue):
                # A damaged order retains only its readable frozen authority.
                # Preview's existing blocker handles an unreadable scope.
                members = set(_persisted_profile_scope(pending) or ())
            else:
                members = {node_id for step in plan.steps for node_id in step.node_ids}
            if members & changed_nodes:
                effects.append(
                    FleetProfilePendingEffect(
                        kind=ProfileProjectionKind.APPLICATION.value,
                        id=pending.id,
                        node_ids=sorted(members),
                    )
                )
        return sorted(effects, key=lambda effect: (effect.kind, effect.id))
