"""Uninstall for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    InvalidRequestReason,
    RecipeReconcilePayload,
    ReconcileCode,
    WaitReason,
    canonical_message,
)

from ..admission_locking import (
    AdmissionLockBusy,
    acquire_admission_keys,
    admission_attempts,
    admission_wait_exhausted,
    job_request_key,
    node_admission_key,
)
from ..install_admission import (
    InstallAdmissionBusy,
)
from ..models import (
    InstallationNode,
    RecipeInstallation,
)
from ..run_switch_contract import (
    RunSwitchReconciliationAuthority,
)
from ..strict_json import serialize_json_value
from .contracts import RecipeInstallationAbandonResult, RecipeInstallationDisposal
from .errors import RecipeRequestInvalid
from .interfaces import RecipeOperationView

if TYPE_CHECKING:
    from .service import RecipeOperationService


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
            run_switch_plan_digest,
            owner_kind="installation",
            owner_id=installation_id,
        )
        if existing is not None:
            return existing
        now = service._clock()
        try:
            with service._sessions.begin() as session:
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
                        (
                            job_request_key(request_id),
                            *(node_admission_key(node_id) for node_id in target_nodes),
                        ),
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
                    run_switch_plan_digest,
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
                        RecipeReconcilePayload(
                            installation_id=authority.installation_id,
                            plan_digest=authority.original_plan_digest,
                        ).model_dump(mode="json"),
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
                        "reconciliation_authority": serialize_json_value(authority),
                    },
                )
        except AdmissionLockBusy as error:
            raise InstallAdmissionBusy(
                ReconcileCode.CAPACITY_BUSY, reason=WaitReason.OBSERVATION_UNAVAILABLE
            ) from error
        except IntegrityError as error:
            raced = service._idempotent(
                request_id,
                WireAgentOperation.RECIPE_RECONCILE.value,
                run_switch_plan_digest,
                owner_kind="installation",
                owner_id=installation_id,
            )
            if raced is not None:
                return raced
            raise RecipeRequestInvalid(
                "request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            ) from error
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
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
        unattended_guard: Callable[[Session], None] | None = None,
        also_removing: Collection[str] = (),
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
            with service._sessions.begin() as session:
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
                    session, installation_id, lock=True, also_removing=also_removing
                )
                if not plan.allowed:
                    raise RecipeRequestInvalid(
                        "uninstall plan is stale or blocked: "
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
                            {
                                "installation_id": installation_id,
                                "recipe_content_sha256": (
                                    plan.installation_authority_digest
                                ),
                                "plan_digest": plan.original_plan_digest,
                                "cleanup_model_content_sha256": (
                                    plan.model_impact.model_content_sha256
                                    if node.node_id
                                    in plan.model_impact.cleanup_node_ids
                                    else None
                                ),
                            },
                        )
                        for node in plan.nodes
                        if node.state != InstallationNodeState.UNINSTALLED
                    ),
                    authority_digest=plan.installation_authority_digest,
                    now=now,
                    workload_intent_ordinal=workload_intent_ordinal,
                    unattended_guard=unattended_guard,
                )
        except IntegrityError as error:
            raced = service._idempotent(
                request_id,
                WireAgentOperation.RECIPE_UNINSTALL.value,
                plan_digest,
                owner_kind="installation",
                owner_id=installation_id,
            )
            if raced is not None:
                return raced
            raise RecipeRequestInvalid(
                "request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            ) from error
        service._agent_jobs.notify_available()
        return service.get(job.id)

    def abandon_never_installed(
        self, installation_id: str
    ) -> RecipeInstallationAbandonResult:
        """Resolve a persisted plan that never reached a node.

        The cleanup phase uses this instead of ``uninstall`` when the
        installation's own assessment reports the never-installed
        disposition.  Nothing is queued to an agent: there are no installed
        bytes, only the admission record and its disk reservation.  The
        assessment is re-derived under the installation row lock, so a
        concurrent install turns this into a refusal rather than a silent
        state flip.  The installation row and its immutable plan are retained,
        so the history stays auditable.
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
            installation.updated_at = now
            service._release(session, "installation", installation_id, now)
        return RecipeInstallationAbandonResult(
            installation_id=installation_id,
            disposition=RecipeInstallationDisposal.ABANDONED,
        )
