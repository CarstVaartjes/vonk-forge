"""Persistence for digest-bound recipe operations."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, object_session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InstallAdmissionCode,
    InvalidRequestReason,
    LifecycleState,
    RecipeInstallPayload,
    RecipeStartPayload,
    ReservationState,
    WaitReason,
    canonical_message,
)

from .. import agent_operation_states, job_states
from ..admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    is_admission_contention,
    job_request_key,
    lock_admission_rows,
    node_admission_key,
)
from ..agent_jobs import (
    AgentJobService,
    release_owned_reservations_in_session,
)
from ..bounded_json import require_mapping
from ..compiled_execution_plan import (
    MAX_COMPILED_EXECUTION_PLAN_BYTES,
    CompiledExecutionPlanError,
    validate_compiled_launch_payload,
)
from ..install_admission import (
    InstallAdmissionBusy,
)
from ..job_documents import controller_recipe_document
from ..lifecycle.agent_operation import retry_scheduled
from ..lifecycle.evidence import (
    BookkeepingReason,
    retire_as_unknown,
)
from ..models import (
    AgentNode,
    AgentOperation,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from ..recipe_lifecycle_contract import (
    validate_recipe_lifecycle_terminal,
)
from ..recipe_progress import (
    _parent_identity as _parent_identity,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _project_recipe_operation_progress as _project_recipe_operation_progress,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import _recorded_parent
from ..run_admission import (
    RunAdmissionBusy,
)
from ..stored_json import read_row_column
from ..strict_json import read_stored_model
from .constants import _WORKLOAD_INTENT_KINDS
from .contracts import RecipeOperationContext
from .errors import RecipeRequestInvalid
from .interfaces import RecipeOperationView, new_recipe_job
from .observation_helpers import _RECIPE_PARENT_READERS
from .rank_authority import _RECIPE_WIRE_PAYLOAD_MODELS
from .results import _recorded_result

if TYPE_CHECKING:
    from .service import RecipeOperationService


class PersistenceMixin:
    def _idempotent(
        self,
        request_id: str,
        kind: str,
        plan_digest: str | None,
        *,
        owner_kind: str | None = None,
        owner_id: str | None = None,
        installation_id: str | None = None,
        run_alias: str | None = None,
        install_identity: tuple[str, str | None] | None = None,
    ) -> RecipeOperationView | None:
        service = typing_cast("RecipeOperationService", self)
        with service._sessions() as session:
            return service._idempotent_in_session(
                session,
                request_id,
                kind,
                plan_digest,
                owner_kind=owner_kind,
                owner_id=owner_id,
                installation_id=installation_id,
                run_alias=run_alias,
                install_identity=install_identity,
            )

    def _idempotent_in_session(
        self,
        session: Session,
        request_id: str,
        kind: str,
        plan_digest: str | None,
        *,
        owner_kind: str | None = None,
        owner_id: str | None = None,
        installation_id: str | None = None,
        run_alias: str | None = None,
        install_identity: tuple[str, str | None] | None = None,
    ) -> RecipeOperationView | None:
        service = typing_cast("RecipeOperationService", self)
        existing = service._idempotent_job_in_session(
            session,
            request_id,
            kind,
            plan_digest,
            owner_kind=owner_kind,
            owner_id=owner_id,
            installation_id=installation_id,
            run_alias=run_alias,
            install_identity=install_identity,
        )
        return service._view(existing) if existing is not None else None

    def _idempotent_job_in_session(
        self,
        session: Session,
        request_id: str,
        kind: str,
        plan_digest: str | None,
        *,
        owner_kind: str | None = None,
        owner_id: str | None = None,
        installation_id: str | None = None,
        run_alias: str | None = None,
        install_identity: tuple[str, str | None] | None = None,
    ) -> Job | None:
        existing = session.scalar(select(Job).where(Job.request_id == request_id))
        if existing is None:
            return None
        existing_digest = _parent_identity(existing, "plan_digest")
        if (
            existing.kind != kind
            or (plan_digest is not None and existing_digest != plan_digest)
            or (
                owner_kind is not None
                and _parent_identity(existing, "owner_kind") != owner_kind
            )
            or (
                owner_id is not None
                and _parent_identity(existing, "owner_id") != owner_id
            )
        ):
            raise RecipeRequestInvalid(
                "request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            )
        if installation_id is not None:
            if (
                existing.kind
                not in {WireAgentOperation.RECIPE_START.value, "recipe.job.activate.v1"}
                or _parent_identity(existing, "owner_kind") != "run"
            ):
                raise RecipeRequestInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
            run = session.get(RecipeRun, _parent_identity(existing, "owner_id"))
            if (
                run is None
                or run.installation_id != installation_id
                or (run_alias is not None and run.alias != run_alias)
            ):
                raise RecipeRequestInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
        if install_identity is not None:
            # An install replay returns the old job only for the same mapping
            # and build; a reused key for another mapping is a conflict.
            installation = (
                session.get(RecipeInstallation, _parent_identity(existing, "owner_id"))
                if _parent_identity(existing, "owner_kind") == "installation"
                else None
            )
            if (
                installation is None
                or (installation.mapping_id, installation.recipe_build_id)
                != install_identity
            ):
                raise RecipeRequestInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
        return existing

    @staticmethod
    def _admit_workload_intent(
        session: Session,
        *,
        kind: str,
        targets: Sequence[str],
        workload_intent_ordinal: int | None,
        now: datetime,
        supersede: bool = True,
    ) -> int:
        """Bind a request to the workload intent that owns its target Sparks.

        A standalone request takes the next ordinal and fences older orders; a
        child must carry its parent's exact, still-current ordinal. An
        unattended request (``supersede=False``) takes no new intent: it joins
        the one every target Spark already shares, so it cancels nothing and
        leaves recovery of a workload on those Sparks untouched, and any later
        load supersedes it.
        """

        try:
            target_nodes = tuple(
                lock_admission_rows(
                    session,
                    (
                        AdmissionRowLock(
                            "workload-target-nodes",
                            AgentNode,
                            select(AgentNode).where(AgentNode.node_id.in_(targets)),
                        ),
                    ),
                ).get("workload-target-nodes", ())
            )
        except AdmissionLockBusy as error:
            if kind == WireAgentOperation.RECIPE_INSTALL.value:
                raise InstallAdmissionBusy(
                    InstallAdmissionCode.CAPACITY_BUSY,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            raise RunAdmissionBusy("run capacity writer is busy") from error
        if tuple(node.node_id for node in target_nodes) != tuple(targets):
            raise RecipeRequestInvalid("workload intent target disappeared")
        if workload_intent_ordinal is None and not supersede:
            shared = {node.workload_intent_ordinal for node in target_nodes}
            if len(shared) != 1 or min(shared) < 1:
                raise RunAdmissionBusy(
                    "workload intent differs across the target Sparks",
                )
            return shared.pop()
        if workload_intent_ordinal is None:
            next_ordinal = (
                max(node.workload_intent_ordinal for node in target_nodes) + 1
            )
            for node in target_nodes:
                node.workload_intent_ordinal = next_ordinal
            try:
                AgentJobService.request_superseded_workload_cancellation_in_session(
                    session, targets, next_ordinal, now
                )
            except AdmissionLockBusy as error:
                if kind == WireAgentOperation.RECIPE_INSTALL.value:
                    raise InstallAdmissionBusy(
                        InstallAdmissionCode.CAPACITY_BUSY,
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    ) from error
                raise RunAdmissionBusy("run capacity writer is busy") from error
            return next_ordinal
        if (
            type(workload_intent_ordinal) is not int
            or workload_intent_ordinal < 1
            or any(
                node.workload_intent_ordinal != workload_intent_ordinal
                for node in target_nodes
            )
        ):
            raise RecipeRequestInvalid(
                "workload intent was superseded", reason=InvalidRequestReason.SUPERSEDED
            )
        return workload_intent_ordinal

    def _queue_in_session(
        self,
        session: Session,
        *,
        kind: str,
        owner_kind: str,
        owner_id: str,
        plan_digest: str,
        actor: str,
        request_id: str,
        node_payloads: Sequence[tuple[str, object]],
        authority_digest: str,
        now: datetime,
        phases: Sequence[Sequence[tuple[str, object]]] | None = None,
        job_context: object | None = None,
        workload_intent_ordinal: int | None = None,
        unattended_guard: Callable[[Session], None] | None = None,
        adopt_stop_parent: Job | None = None,
    ) -> Job:
        service = typing_cast("RecipeOperationService", self)
        if not node_payloads:
            raise RecipeRequestInvalid("operation group has no target nodes")
        try:
            payload_model = _RECIPE_WIRE_PAYLOAD_MODELS[kind]
        except KeyError:
            payload_model = None
        if payload_model is not None:
            try:
                for _node_id, payload in node_payloads:
                    payload_model.model_validate_json(canonical_message(payload))
            except Exception as error:
                raise RecipeRequestInvalid(
                    f"{kind} payload does not satisfy its wire schema"
                ) from error
        job_id = str(uuid.uuid4())
        requested_phase_groups = (
            tuple(tuple(group) for group in phases)
            if phases is not None
            else (tuple(node_payloads),)
        )
        if not requested_phase_groups or any(
            not group for group in requested_phase_groups
        ):
            raise RecipeRequestInvalid("operation phases are invalid")
        flattened = tuple(item for group in requested_phase_groups for item in group)
        if {node_id for node_id, _payload in flattened} != {
            node_id for node_id, _payload in node_payloads
        } or len({node_id for node_id, _payload in node_payloads}) != len(
            node_payloads
        ):
            raise RecipeRequestInvalid("operation phases do not match target nodes")
        phase_groups = tuple(
            tuple((str(uuid.uuid4()), node_id, payload) for node_id, payload in group)
            for group in requested_phase_groups
        )
        targets = sorted(
            {node_id for _operation_id, node_id, _payload in sum(phase_groups, ())}
        )
        try:
            acquire_admission_keys(
                session,
                (
                    job_request_key(request_id),
                    *(node_admission_key(node_id) for node_id in targets),
                ),
                holder="recipe-operation",
            )
        except AdmissionLockBusy as error:
            if kind == WireAgentOperation.RECIPE_INSTALL.value:
                raise InstallAdmissionBusy(
                    InstallAdmissionCode.CAPACITY_BUSY,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            raise RunAdmissionBusy("run capacity writer is busy") from error
        existing = service._idempotent_job_in_session(
            session,
            request_id,
            kind,
            plan_digest,
            owner_kind=owner_kind,
            owner_id=owner_id,
        )
        if existing is not None:
            if adopt_stop_parent is None:
                return existing
            if (
                kind != WireAgentOperation.RECIPE_STOP.value
                or existing.id != adopt_stop_parent.id
                or existing.state != LifecycleState.RUNNING.value
                or (
                    getattr(
                        read_row_column(existing, "payload"), "execution_mode", None
                    )
                    != "one-shot-jobs"
                    and service._service_stop_document(existing).service_stop_review
                    is None
                )
                or getattr(_recorded_parent(existing), "phases", None) is not None
                or existing.targets != targets
                or getattr(_recorded_parent(existing), "workload_intent_ordinal", None)
                != workload_intent_ordinal
                or session.scalar(
                    select(AgentOperation.id).where(
                        AgentOperation.parent_job_id == existing.id
                    )
                )
                is not None
            ):
                raise RecipeRequestInvalid("accepted logical Stop identity changed")
            job_id = existing.id
        if kind in _WORKLOAD_INTENT_KINDS:
            # Only a standalone request admits a new intent. A child carries
            # its parent's exact ordinal and may not capture newer authority.
            workload_intent_ordinal = service._admit_workload_intent(
                session,
                kind=kind,
                targets=targets,
                workload_intent_ordinal=workload_intent_ordinal,
                now=now,
                supersede=unattended_guard is None,
            )
            if unattended_guard is not None:
                # The target Spark rows are locked now, so a load admitted
                # before this point is visible to the guard and one admitted
                # after it waits for this transaction.
                unattended_guard(session)
        job_document = {
            "schema_version": 1,
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            "plan_digest": plan_digest,
        }
        if workload_intent_ordinal is not None:
            if type(workload_intent_ordinal) is not int or workload_intent_ordinal < 1:
                raise RecipeRequestInvalid("workload intent ordinal is invalid")
            job_document["workload_intent_ordinal"] = workload_intent_ordinal
        if phases is not None:
            job_document["phases"] = [
                [
                    {
                        "operation_id": operation_id,
                        "node_id": node_id,
                        "payload": json.loads(canonical_message(payload)),
                    }
                    for operation_id, node_id, payload in group
                ]
                for group in phase_groups
            ]
        if job_context is not None:
            context = RecipeOperationContext.model_validate_json(
                canonical_message(job_context)
            )
            job_document.update(
                (
                    (name, value)
                    for name, value in require_mapping(
                        controller_recipe_document(context), "recipe context"
                    ).items()
                    if value is not None
                )
            )
        parent_model = _RECIPE_PARENT_READERS[kind]
        parent = parent_model.validate_json(canonical_message(job_document))
        job_document = dict(
            require_mapping(controller_recipe_document(parent), "recipe parent")
        )
        # A formatted timestamp binds its spelling as well as its instant.
        if job_context is not None and context.start_deadline is not None:
            job_document["start_deadline"] = context.start_deadline
        if kind in {
            WireAgentOperation.RECIPE_INSTALL.value,
            WireAgentOperation.RECIPE_START.value,
        }:
            try:
                for _node_id, payload in flattened:
                    # A missing or non-mapping plan is refused by the validator
                    # itself (``CompiledExecutionPlanError``).
                    launch = read_stored_model(
                        RecipeInstallPayload
                        if kind == WireAgentOperation.RECIPE_INSTALL.value
                        else RecipeStartPayload,
                        canonical_message(payload),
                        from_json=True,
                    )
                    validate_compiled_launch_payload(launch.compiled_execution_plan)
            except (CompiledExecutionPlanError, TypeError, ValueError) as error:
                raise RecipeRequestInvalid(
                    f"compiled execution plan is invalid: {error}"
                ) from None
            if len(canonical_message(job_document)) > MAX_COMPILED_EXECUTION_PLAN_BYTES:
                raise RecipeRequestInvalid("recipe operation job payload is too large")
        job = new_recipe_job(
            id=job_id,
            request_id=request_id,
            kind=kind,
            state=LifecycleState.RUNNING.value,
            actor=actor,
            authority_revision=authority_digest.removeprefix("sha256:"),
            targets=targets,
            payload_digest=hashlib.sha256(canonical_message(parent)).hexdigest(),
            payload=job_document,
            created_at=now,
            updated_at=now,
        )
        if existing is not None:
            existing.payload = job_document
            existing.payload_digest = job.payload_digest
            existing.authority_revision = job.authority_revision
            existing.status_reason = None
            existing.updated_at = now
            job = existing
        else:
            session.add(job)
        try:
            session.flush()
            for operation_id, node_id, payload in phase_groups[0]:
                service._agent_jobs.enqueue_in_session(
                    session,
                    job_id,
                    node_id,
                    kind,
                    authority_digest.removeprefix("sha256:"),
                    json.loads(
                        canonical_message(
                            _RECIPE_WIRE_PAYLOAD_MODELS[kind].model_validate_json(
                                canonical_message(payload)
                            )
                        )
                    ),
                    operation_id=operation_id,
                )
        except AdmissionLockBusy as error:
            if kind == WireAgentOperation.RECIPE_INSTALL.value:
                raise InstallAdmissionBusy(
                    InstallAdmissionCode.CAPACITY_BUSY,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            raise RunAdmissionBusy("run capacity writer is busy") from error
        except OperationalError as error:
            if not is_admission_contention(error):
                raise
            if kind == WireAgentOperation.RECIPE_INSTALL.value:
                raise InstallAdmissionBusy(
                    InstallAdmissionCode.CAPACITY_BUSY,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            raise RunAdmissionBusy("run capacity writer is busy") from error
        return job

    def _view(self, job: Job, *, session: Session | None = None) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        session = session or object_session(job)
        try:
            validate_recipe_lifecycle_terminal(
                job.kind, job.state, read_row_column(job, "result")
            )
        except (TypeError, ValueError) as error:
            # A terminal operation whose receipt does not hold is shown without
            # it: the receipt is evidence, never a reason to hide the operation.
            retire_as_unknown(
                "recipe.operation-result",
                job.id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"{type(error).__name__}: {error}",
            )
        waiting_children = (
            tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(
                        AgentOperation.parent_job_id == job.id,
                        AgentOperation.state.in_(agent_operation_states.PARKED),
                    )
                    .order_by(AgentOperation.node_id, AgentOperation.id)
                )
            )
            if session is not None
            and job.state
            in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.NEEDS_OPERATOR,
            )
            else ()
        )
        retry_due_at = min(
            (
                due
                for due in (retry_scheduled(child) for child in waiting_children)
                if due is not None
            ),
            default=None,
        )
        child_reason = next(
            (child.status_reason for child in waiting_children if child.status_reason),
            None,
        )
        return RecipeOperationView(
            id=job.id,
            kind=job.kind,
            owner_id=_parent_identity(job, "owner_id") or "",
            state=job.state,
            plan_digest=_parent_identity(job, "plan_digest") or "",
            nodes=tuple(job.targets),
            lifecycle_result=_recorded_result(
                job.kind, read_row_column(job, "result"), subject=job.id
            ),
            retry_due_at=retry_due_at,
            status_reason=child_reason or job.status_reason,
            progress=(
                _project_recipe_operation_progress(session, job, now=service._clock())
                if session is not None
                else None
            ),
        )

    @staticmethod
    def _release(
        session: Session, owner_kind: str, owner_id: str, now: datetime
    ) -> None:
        release_owned_reservations_in_session(session, owner_kind, owner_id, now)

    @staticmethod
    def _release_node_reservations(
        session: Session,
        run_id: str,
        node_ids: Sequence[str],
        now: datetime,
    ) -> None:
        """Release only the ranks whose Stop receipts are proven."""

        if not node_ids:
            return
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.node_id.in_(node_ids),
                ResourceReservation.state == ReservationState.ACTIVE,
            )
        ):
            reservation.state = ReservationState.RELEASED
            reservation.released_at = now
