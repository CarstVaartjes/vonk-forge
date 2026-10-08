"""Stop dispatch for digest-bound recipe operations."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    RecipeStartPayload,
    RecipeStopPayload,
    RouteState,
    RunState,
    canonical_message,
)

from ..distributed_lifecycle import (
    DistributedLifecycleError,
    DistributedRecoveryInvalid,
)
from ..distributed_recovery import (
    recovery_start_plan,
    run_node_reports_absent,
    settle_absent_run_in_session,
)
from ..job_documents import (
    DistributedRecoveryMarker,
    ProfilePartialStop,
    RecipeStopParent,
)
from ..lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    retire_as_unknown,
)
from ..lifecycle.recipe_operation import RecipeOperationAdapter
from ..models import (
    Job,
    RecipeRun,
    RunNode,
)
from ..recipe_action_plans import (
    StopPlan,
)
from ..recipe_progress import (
    _role_phases as _role_phases,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _topology_order as _topology_order,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_routes import (
    route_publication_transaction,
)
from ..recipe_stop_payloads import (
    RecipeStopAuthorityError,
    durable_run_stop_payloads,
)
from ..stored_json import read_row_column
from ..strict_json import read_stored_model, serialize_json_value
from .contracts import RecipeOperationContext
from .errors import (
    RecipeOperationConflict,
    RecipeRequestInvalid,
    RecipeStopAuthorityRefused,
    _RouteNotWithdrawn,
)
from .interfaces import RecipeOperationView, new_recipe_job
from .observation_helpers import _active_recipe_revision
from .results import _validated_result

if TYPE_CHECKING:
    from .service import RecipeOperationService


class StopDispatchMixin:
    def _dispatch_stop_after_withdrawal(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None,
        profile_target_node_ids: Sequence[str] | None,
    ) -> RecipeOperationView:
        """Queue the Stop in one short transaction, if the route is still gone."""
        service = typing_cast("RecipeOperationService", self)

        now = service._clock()
        try:
            transaction = (
                service._route_publications.publication_transaction()
                if service._route_publications is not None
                else route_publication_transaction(service._sessions)
            )
            with transaction as session:
                existing = service._idempotent_job_in_session(
                    session,
                    request_id,
                    WireAgentOperation.RECIPE_STOP.value,
                    plan_digest,
                    owner_kind="run",
                    owner_id=run_id,
                )
                if existing is None:
                    raise RecipeStopAuthorityRefused("accepted Stop disappeared")
                document = service._service_stop_document(existing)
                review = document.service_stop_review
                if (
                    review is None
                    or review.stage == "dispatched"
                    or existing.state != LifecycleState.RUNNING.value
                ):
                    return service._view(existing, session=session)
                document, admitted = service._check_service_stop(session, existing)
                if service._route_publications is not None and (
                    not service._route_publications.withdrawal_complete_in_session(
                        session, frozenset({run_id})
                    )
                ):
                    # A competing publication listed the run again, or the
                    # withdrawal was superseded before it completed.
                    raise _RouteNotWithdrawn(run_id)
                run = session.get(RecipeRun, run_id)
                assert run is not None
                job = service._complete_absent_stop_in_session(
                    session,
                    run=run,
                    admitted=admitted,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    now=now,
                    accepted_parent=existing,
                )
                if job is not None:
                    return service._view(job, session=session)
                job = service._queue_stop_in_session(
                    session,
                    run=run,
                    admitted=admitted,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    now=now,
                    profile_target_node_ids=profile_target_node_ids,
                    accepted_parent=existing,
                )
                run.route_error = None
        except IntegrityError as error:
            raced = service._idempotent(
                request_id,
                WireAgentOperation.RECIPE_STOP.value,
                plan_digest,
                owner_kind="run",
                owner_id=run_id,
            )
            if raced is not None:
                return raced
            raise RecipeRequestInvalid(
                "request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            ) from error
        return service.get(job.id)

    @staticmethod
    def _absent_stop_nodes(
        session: Session,
        run: RecipeRun,
        admitted: StopPlan,
        now: datetime,
        *,
        lock: bool,
    ) -> tuple[RunNode, ...] | None:
        """The run's ranks when every one is already reported not running."""

        if admitted.missing_node_ids:
            return None
        statement = (
            select(RunNode)
            .where(RunNode.run_id == run.id)
            .order_by(RunNode.rank, RunNode.node_id)
        )
        if lock:
            statement = statement.with_for_update(of=RunNode)
        nodes = tuple(session.scalars(statement))
        if not nodes or not all(
            run_node_reports_absent(run, node, now) for node in nodes
        ):
            return None
        return nodes

    def _complete_absent_stop_in_session(
        self,
        session: Session,
        *,
        run: RecipeRun,
        admitted: StopPlan,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None,
        now: datetime,
        accepted_parent: Job | None = None,
    ) -> Job | None:
        """Finish a Stop whose ranks every Spark already reports as not running.

        There is nothing left to stop: the Sparks' own current report is the
        proof, so the run is recorded stopped, its ports and memory are released
        and the Stop succeeds without an agent round trip.
        """
        service = typing_cast("RecipeOperationService", self)

        nodes = service._absent_stop_nodes(session, run, admitted, now, lock=True)
        if nodes is None:
            return None
        ordinal = service._admit_workload_intent(
            session,
            kind=WireAgentOperation.RECIPE_STOP.value,
            targets=sorted(node.node_id for node in nodes),
            workload_intent_ordinal=workload_intent_ordinal,
            now=now,
        )
        settle_absent_run_in_session(session, run, nodes, now)
        retained_review = None
        if accepted_parent is not None:
            retained_review = service._service_stop_document(
                accepted_parent
            ).service_stop_review
            assert retained_review is not None
            retained_review = retained_review.model_copy(update={"stage": "dispatched"})
        payload = RecipeStopParent(
            schema_version=1,
            owner_kind="run",
            owner_id=run.id,
            plan_digest=admitted.plan_digest,
            service_stop_review=retained_review,
            workload_intent_ordinal=ordinal,
        )
        job = new_recipe_job(
            id=accepted_parent.id if accepted_parent is not None else str(uuid.uuid4()),
            request_id=request_id,
            kind=WireAgentOperation.RECIPE_STOP.value,
            state=LifecycleState.SUCCEEDED.value,
            actor=actor,
            authority_revision=admitted.authority_digest.removeprefix("sha256:"),
            targets=sorted(node.node_id for node in nodes),
            payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
            payload=serialize_json_value(payload),
            result=serialize_json_value(
                _validated_result(
                    WireAgentOperation.RECIPE_STOP.value, {RunState.STOPPED.value: True}
                )
            ),
            created_at=now,
            updated_at=now,
        )
        if accepted_parent is not None:
            RecipeOperationAdapter().finish(accepted_parent, now, failed=False)
            accepted_parent.status_reason = None
            accepted_parent.result = serialize_json_value(
                _validated_result(job.kind, read_row_column(job, "result"))
            )
            accepted_parent.payload = serialize_json_value(
                service._service_stop_document(job)
            )
            accepted_parent.payload_digest = job.payload_digest
            accepted_parent.updated_at = now
            job = accepted_parent
        else:
            session.add(job)
        session.flush()
        return job

    def queue_recovery_stop_in_session(
        self,
        session: Session,
        run_id: str,
        *,
        recovery_context: DistributedRecoveryMarker,
        workload_intent_ordinal: int,
        now: datetime,
    ) -> Job | Residue:
        """Queue an accepted-run recovery through the canonical Stop owner.

        The caller already owns the route-publication transaction and has
        withdrawn this run's route.  Keeping the Stop admission and queue write
        in that transaction makes duplicate recovery ticks and newer workload
        intent serialize against the same run and node facts.

        A recovery whose scope is no longer current, or whose stored continuation
        or start authority is damaged, queues nothing: the damage is retired as
        unknown (a :class:`Residue`).  A scope that is only not current yet
        (``EVIDENCE_UNAVAILABLE``) is retried on the next pass; damaged
        authority is settled by the recovery owner as unrecoverable.  Either way
        the owner goes on to the next run.
        """
        service = typing_cast("RecipeOperationService", self)

        run = session.get(RecipeRun, run_id, with_for_update=True)
        if (
            run is None
            or run.state != RunState.RUNNING
            or run.route_state != RouteState.WITHDRAWN
        ):
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "recovery Stop scope is no longer current",
            )
        if type(workload_intent_ordinal) is not int or workload_intent_ordinal < 1:
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                "recovery workload intent is invalid",
            )
        try:
            decoded = recovery_start_plan(
                recovery_context,
                now=now,
                require_unexpired=False,
            )
        except DistributedLifecycleError as error:
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"recovery continuation is invalid: {error}",
            )
        if decoded is None:
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.ROW_INCOMPLETE,
                "recovery continuation is missing",
            )
        start_phases, marker = decoded
        nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == run.id)
                .order_by(RunNode.rank, RunNode.node_id)
            )
        )
        if (
            len(nodes) != 1
            or len(start_phases) != 1
            or len(start_phases[0]) != 1
            or start_phases[0][0][0] != nodes[0].node_id
        ):
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "recovery Start rank set is invalid",
            )
        start_payload = start_phases[0][0][1]
        try:
            accepted_start = read_stored_model(
                RecipeStartPayload, canonical_message(start_payload), from_json=True
            )
        except (TypeError, ValueError) as error:
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"recovery Start payload is invalid: {error}",
            )
        if (
            str(accepted_start.run_id) != run.id
            or accepted_start.plan_digest != run.plan_digest
            or accepted_start.run_generation != run.run_generation
            or accepted_start.run_generation <= 1
            or accepted_start.compiled_execution_plan.runtime.placement.rank != 0
            or accepted_start.compiled_execution_plan.runtime.placement.role
            != "entrypoint"
        ):
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "recovery Start differs from accepted run",
            )
        request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "vonk:singleton-recovery-stop:"
                f"{run.id}:{accepted_start.run_generation}:{marker.deadline}",
            )
        )
        existing = service._idempotent_job_in_session(
            session,
            request_id,
            WireAgentOperation.RECIPE_STOP.value,
            None,
            owner_kind="run",
            owner_id=run.id,
        )
        if existing is not None:
            return existing
        admitted = service._stop_plan_in_session(session, run.id, lock=True)
        if (
            not admitted.allowed
            or admitted.authority_digest != run.plan_digest
            or {item.node_id for item in admitted.nodes} != {nodes[0].node_id}
        ):
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "recovery Stop admission is blocked",
            )
        try:
            return service._queue_stop_in_session(
                session,
                run=run,
                admitted=admitted,
                actor="system:singleton-recovery",
                request_id=request_id,
                workload_intent_ordinal=workload_intent_ordinal,
                now=now,
                job_context={"recovery": serialize_json_value(recovery_context)},
                stop_run_generation=accepted_start.run_generation - 1,
            )
        except RecipeOperationConflict as error:
            if str(error) == "workload intent was superseded":
                raise DistributedRecoveryInvalid(
                    "singleton recovery was superseded by a newer workload intent",
                    reason=InvalidRequestReason.SUPERSEDED,
                ) from error
            raise

    def _exact_stop_authority(
        self,
        session: Session,
        run: RecipeRun,
        admitted: StopPlan,
        *,
        stop_run_generation: int | None = None,
    ) -> tuple[tuple[str, ...] | None, Mapping[str, RecipeStopPayload], set[str]]:
        """Bind exact run identity before route withdrawal or destructive work.

        Current consent and durable run/rank ownership authorize Stop. Historical
        Start evidence is disposable. Unreadable topology stops exact targets
        together; mismatched target ownership remains a security refusal.
        """

        revision = _active_recipe_revision(session, admitted.recipe_revision_id)
        stop_order = (
            _topology_order(read_row_column(revision, "document"), "stop_order")
            if revision is not None
            else None
        )
        if stop_order is None:
            retire_as_unknown(
                "recipe.stop-order",
                run.id,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "recipe topology is unreadable; the targets are stopped together",
            )
        generation = (
            run.run_generation if stop_run_generation is None else stop_run_generation
        )
        try:
            exact_stop_payloads = durable_run_stop_payloads(
                session,
                run,
                admitted.nodes,
                run_generation=generation,
                cancel_pending_start=True,
                allow_missing_nodes=False,
            )
        except RecipeStopAuthorityError as error:
            raise RecipeStopAuthorityRefused(
                "recipe Stop exact target ownership differs"
            ) from error
        target_ids = set(admitted.target_node_ids)
        if not target_ids or not target_ids <= set(exact_stop_payloads):
            raise RecipeStopAuthorityRefused(
                "recipe Stop target lacks its exact durable Start authority"
            )
        return stop_order, exact_stop_payloads, target_ids

    def _queue_stop_in_session(
        self,
        session: Session,
        *,
        run: RecipeRun,
        admitted: StopPlan,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None,
        now: datetime,
        job_context: object | None = None,
        stop_run_generation: int | None = None,
        profile_target_node_ids: Sequence[str] | None = None,
        accepted_parent: Job | None = None,
    ) -> Job:
        service = typing_cast("RecipeOperationService", self)
        stop_order, exact_stop_payloads, target_ids = service._exact_stop_authority(
            session, run, admitted, stop_run_generation=stop_run_generation
        )
        context = (
            RecipeOperationContext.model_validate_json(canonical_message(job_context))
            if job_context is not None
            else RecipeOperationContext()
        )
        if accepted_parent is not None:
            parent_document = service._service_stop_document(accepted_parent)
            review = parent_document.service_stop_review
            assert review is not None and review.exact_payloads is not None
            exact_stop_payloads = review.exact_payloads
            stop_order = (
                tuple(review.stop_order) if review.stop_order is not None else None
            )
            context.service_stop_review = review.model_copy(
                update={"stage": "dispatched"}
            )

        if stop_order is not None and profile_target_node_ids is not None:
            reachable_roles = {
                node.role for node in admitted.nodes if node.node_id in target_ids
            }
            stop_order = tuple(role for role in stop_order if role in reachable_roles)

        if admitted.missing_node_ids:
            context.profile_partial_stop = ProfilePartialStop(
                target_node_ids=list(admitted.target_node_ids),
                missing_node_ids=list(admitted.missing_node_ids),
            )
        stop_payloads = tuple(
            (
                node.node_id,
                json.loads(canonical_message(exact_stop_payloads[node.node_id])),
            )
            for node in admitted.nodes
            if node.node_id in target_ids
        )
        stop_phases = (
            _role_phases(stop_order, stop_payloads)
            if stop_order is not None
            else (stop_payloads,)
        )
        if stop_phases is None:
            stop_phases = (stop_payloads,)
        run.state = RunState.STOPPING
        run.route_state = RouteState.WITHDRAWN
        run.updated_at = now
        job = service._queue_in_session(
            session,
            kind=WireAgentOperation.RECIPE_STOP.value,
            owner_kind="run",
            owner_id=run.id,
            plan_digest=admitted.plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=stop_payloads,
            phases=stop_phases,
            authority_digest=admitted.authority_digest,
            now=now,
            workload_intent_ordinal=workload_intent_ordinal,
            job_context=context,
            adopt_stop_parent=accepted_parent,
        )
        session.flush()
        return job
