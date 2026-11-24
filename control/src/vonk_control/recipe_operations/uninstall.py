"""Uninstall for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from vonk_agent_protocol.agent_operations import AgentOperation as WireAgentOperation
from vonk_agent_protocol.lifecycle_vocabulary import InvalidRequestReason, WaitReason
from vonk_agent_protocol.reason_codes.recipes import ReconcileCode
from vonk_agent_protocol.recipe_operations import (
    RecipeReconcilePayload,
    RecipeUninstallPayload,
)
from vonk_agent_protocol.state_machines import InstallationNodeState, InstallationState
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.admission_locking import (
    AdmissionLockBusy,
    ConstraintReplay,
    acquire_admission_keys,
    admission_attempts,
    admission_wait_exhausted,
    admitting,
    is_admission_contention,
    node_admission_key,
)
from vonk_control.agent_operation_facts import aware
from vonk_control.install_admission.contracts import InstallAdmissionBusy
from vonk_control.jobs.documents import controller_recipe_document
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models.execution import InstallationNode, RecipeInstallation
from vonk_control.recipe_operations.contracts import (
    RecipeInstallationAbandonResult,
    RecipeInstallationDisposal,
)
from vonk_control.recipe_operations.errors import RecipeRequestInvalid
from vonk_control.recipe_operations.interfaces import RecipeOperationView
from vonk_control.run_switch_contract.requests import RunSwitchReconciliationAuthority
from vonk_control.runs.admission import RunAdmissionBusy
from vonk_control.strict_json import serialize_json_value
from vonk_control.waits.persisted import schedule_wait

if TYPE_CHECKING:
    from vonk_control.recipe_operations.service import RecipeOperationService


class UninstallMixin:
    def _reconcile_installation_once(
        self,
        installation_id: str,
        *,
        expected_authority: RunSwitchReconciliationAuthority,
        run_switch_plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        existing = service._idempotent(
            request_id,
            WireAgentOperation.RECIPE_RECONCILE.value,
            None,
            owner_kind="installation",
            owner_id=installation_id,
        )
        if existing is not None:
            return existing
        now = service._clock()
        try:
            with admitting(), service._sessions.begin() as session:
                target_nodes = tuple(
                    session.scalars(
                        select(InstallationNode.node_id)
                        .where(InstallationNode.installation_id == installation_id)
                        .order_by(InstallationNode.node_id)
                    )
                )
                try:
                    acquire_admission_keys(
                        session,
                        tuple(node_admission_key(node_id) for node_id in target_nodes),
                        holder="recipe-operation",
                    )
                except AdmissionLockBusy as error:
                    raise InstallAdmissionBusy(
                        ReconcileCode.CAPACITY_BUSY,
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    ) from error
                authority = service._reconciliation_authority_in_session(
                    session, installation_id, lock=True
                )
                if canonical_message(authority) != canonical_message(
                    expected_authority
                ):
                    raise RecipeRequestInvalid(
                        "reconciliation authority changed after preview",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                existing = service._idempotent_in_session(
                    session,
                    request_id,
                    WireAgentOperation.RECIPE_RECONCILE.value,
                    None,
                    owner_kind="installation",
                    owner_id=installation_id,
                )
                if existing is not None:
                    return existing
                pending_targets = tuple(
                    target for target in authority.targets if target.state == "pending"
                )
                if not pending_targets:
                    raise RecipeRequestInvalid(
                        "reconciliation has no pending targets but is not complete"
                    )
                payloads = tuple(
                    (
                        target.node_id,
                        serialize_json_value(
                            RecipeReconcilePayload(
                                installation_id=authority.installation_id,
                                plan_digest=authority.original_plan_digest,
                            )
                        ),
                    )
                    for target in pending_targets
                )
                job = service._queue_in_session(
                    session,
                    kind=WireAgentOperation.RECIPE_RECONCILE.value,
                    owner_kind="installation",
                    owner_id=installation_id,
                    plan_digest=run_switch_plan_digest,
                    actor=actor,
                    request_id=request_id,
                    node_payloads=payloads,
                    authority_digest=authority.recipe_content_sha256,
                    now=now,
                    workload_intent_ordinal=workload_intent_ordinal,
                    job_context={
                        "reconciliation_authority": controller_recipe_document(
                            authority
                        ),
                    },
                )
        except AdmissionLockBusy as error:
            raise InstallAdmissionBusy(
                ReconcileCode.CAPACITY_BUSY, reason=WaitReason.OBSERVATION_UNAVAILABLE
            ) from error
        except ConstraintReplay as duplicate:
            return duplicate.replay(
                lambda: service._idempotent(
                    request_id,
                    WireAgentOperation.RECIPE_RECONCILE.value,
                    None,
                    owner_kind="installation",
                    owner_id=installation_id,
                )
            )
        service._agent_jobs.notify_available()
        return service.get(job.id)

    def reconcile_installation(
        self,
        installation_id: str,
        *,
        expected_authority: RunSwitchReconciliationAuthority,
        run_switch_plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        """Queue exact managed cleanup under a current Run/Switch review."""
        service = typing_cast("RecipeOperationService", self)
        refused: InstallAdmissionBusy | None = None
        for _attempt in admission_attempts():
            try:
                return service._reconcile_installation_once(
                    installation_id,
                    expected_authority=expected_authority,
                    run_switch_plan_digest=run_switch_plan_digest,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                )
            except InstallAdmissionBusy as error:
                refused = error
                if admission_wait_exhausted(error):
                    break
        assert refused is not None
        raise refused

    def uninstall(
        self,
        installation_id: str,
        *,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
        unattended_guard: Callable[[Session], None] | None = None,
        also_removing: Collection[str] = (),
        preserve_model_files: bool = False,
    ) -> RecipeOperationView:
        """Queue the removal of one installation from its Sparks.

        ``also_removing`` names installations removed in the same sweep: they do
        not keep the shared model files, so the last of them to go frees them.

        ``unattended_guard`` marks a removal nobody asked for (the storage
        sweep). It takes no new workload intent, so it supersedes no order and
        costs no running workload its recovery, and it is called with the
        target Sparks locked so the caller can re-check that the installation is
        still unused; raising refuses the removal before anything is queued.
        """
        service = typing_cast("RecipeOperationService", self)

        existing = service._idempotent(
            request_id,
            WireAgentOperation.RECIPE_UNINSTALL.value,
            None,
            owner_kind="installation",
            owner_id=installation_id,
        )
        if existing is not None:
            return existing
        now = service._clock()
        try:
            with admitting(), service._sessions.begin() as session:
                installation_fence = session.scalar(
                    select(RecipeInstallation)
                    .where(RecipeInstallation.id == installation_id)
                    .with_for_update(of=RecipeInstallation)
                )
                if installation_fence is None:
                    raise RecipeRequestInvalid("recipe installation does not exist")
                existing = service._idempotent_in_session(
                    session,
                    request_id,
                    WireAgentOperation.RECIPE_UNINSTALL.value,
                    None,
                    owner_kind="installation",
                    owner_id=installation_id,
                )
                if existing is not None:
                    return existing
                plan = service._uninstall_plan_in_session(
                    session,
                    installation_id,
                    lock=True,
                    also_removing=also_removing,
                    preserve_model_files=preserve_model_files,
                )
                # Uninstall is declarative: it removes the installation as it
                # is now. A plan that moved on since the caller's preview is
                # re-planned, never refused; only a blocked one is.
                if not plan.allowed:
                    raise RecipeRequestInvalid(
                        "uninstall plan is blocked: "
                        + "; ".join(reason.code for reason in plan.blockers),
                        reason=InvalidRequestReason.SUPERSEDED,
                    )
                if plan.disposition != "uninstall":
                    # A plan that never reached a node is abandoned by the
                    # cleanup phase; it must never queue agent removal work.
                    raise RecipeRequestInvalid(
                        "installation was never installed; abandon it instead "
                        "of uninstalling it"
                    )
                installation = session.get(RecipeInstallation, installation_id)
                assert installation is not None
                compiled = service._stored_compiled_plans(
                    session,
                    installation,
                    tuple(node.node_id for node in plan.nodes),
                    now=now,
                )
                compiled_plans = {} if isinstance(compiled, Residue) else compiled
                job = service._queue_in_session(
                    session,
                    kind=WireAgentOperation.RECIPE_UNINSTALL.value,
                    owner_kind="installation",
                    owner_id=installation_id,
                    plan_digest=plan.plan_digest,
                    actor=actor,
                    request_id=request_id,
                    node_payloads=tuple(
                        (
                            node.node_id,
                            serialize_json_value(
                                RecipeUninstallPayload(
                                    installation_id=installation_id,
                                    recipe_content_sha256=plan.installation_authority_digest,
                                    plan_digest=plan.original_plan_digest,
                                    cleanup_model_content_sha256=(
                                        plan.model_impact.model_content_sha256
                                        if node.node_id
                                        in plan.model_impact.cleanup_node_ids
                                        else None
                                    ),
                                    compiled_execution_plan=compiled_plans.get(
                                        node.node_id
                                    ),
                                )
                            ),
                        )
                        for node in plan.nodes
                        if node.state != InstallationNodeState.UNINSTALLED
                    ),
                    authority_digest=plan.installation_authority_digest,
                    now=now,
                    workload_intent_ordinal=workload_intent_ordinal,
                    unattended_guard=unattended_guard,
                )
        except ConstraintReplay as duplicate:
            return duplicate.replay(
                lambda: service._idempotent(
                    request_id,
                    WireAgentOperation.RECIPE_UNINSTALL.value,
                    None,
                    owner_kind="installation",
                    owner_id=installation_id,
                )
            )
        except OperationalError as error:
            if not is_admission_contention(error):
                raise
            raise RunAdmissionBusy("uninstall admission is busy") from error
        service._agent_jobs.notify_available()
        return service.get(job.id)

    def abandon_never_installed(
        self, installation_id: str, *, untouched_since: datetime | None = None
    ) -> RecipeInstallationAbandonResult:
        """Resolve a persisted plan that never reached a node.

        The cleanup phase uses this instead of ``uninstall`` when the
        installation's own assessment reports the never-installed
        disposition.  Nothing is queued to an agent: there are no installed
        bytes, only the admission record and its disk reservation.  The
        assessment is re-derived under the installation row lock, so a
        concurrent install turns this into a refusal rather than a silent
        state flip.  With ``untouched_since`` a row touched after it (an adoption
        stamps ``updated_at`` under this lock) is refused the same way.  The
        installation row and its immutable plan are retained, so the history
        stays auditable.
        """
        service = typing_cast("RecipeOperationService", self)

        now = service._clock()
        with service._sessions.begin() as session:
            installation = session.scalar(
                select(RecipeInstallation)
                .where(RecipeInstallation.id == installation_id)
                .with_for_update(of=RecipeInstallation)
            )
            if installation is None:
                raise RecipeRequestInvalid("recipe installation does not exist")
            if installation.state == InstallationState.UNINSTALLED:
                # A restarted cleanup phase replays the disposal.  The row is
                # already resolved, so the replay succeeds instead of failing
                # the operation after the effect has landed.
                return RecipeInstallationAbandonResult(
                    installation_id=installation_id,
                    disposition=RecipeInstallationDisposal.ABANDONED,
                )
            if untouched_since is not None and aware(installation.updated_at) > aware(
                untouched_since
            ):
                raise RecipeRequestInvalid(
                    "installation was adopted meanwhile; it cannot be abandoned",
                    reason=InvalidRequestReason.UNSUPPORTED,
                )
            plan = service._uninstall_plan_in_session(
                session, installation_id, lock=True
            )
            if not plan.allowed or plan.disposition != "abandon":
                raise RecipeRequestInvalid(
                    "installation is not a never-installed plan; it cannot be abandoned",
                    reason=InvalidRequestReason.UNSUPPORTED,
                )
            nodes = tuple(
                session.scalars(
                    select(InstallationNode)
                    .where(InstallationNode.installation_id == installation_id)
                    .with_for_update(of=InstallationNode)
                )
            )
            for node in nodes:
                node.state = InstallationNodeState.UNINSTALLED
                node.updated_at = now
            installation.state = InstallationState.UNINSTALLED
            schedule_wait(installation, None)
            installation.updated_at = now
            service._release(session, "installation", installation_id, now)
        return RecipeInstallationAbandonResult(
            installation_id=installation_id,
            disposition=RecipeInstallationDisposal.ABANDONED,
        )
