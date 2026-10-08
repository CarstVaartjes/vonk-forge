"""Installation preparation for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InstallAdmissionCode,
    InstallationNodeState,
    InstallationState,
    LifecycleState,
    UnknownOutcomeError,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)

from .. import job_states
from ..admission_locking import admission_attempts
from ..install_admission import (
    InstallAdmissionBusy,
    InstallPlan,
    InstallPlanConflict,
)
from ..install_admission import (
    require_admissible as require_install_admissible,
)
from ..lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from ..models import (
    InstallationNode,
    Job,
    RecipeInstallation,
)
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_installation_plan,
)
from ..recipe_progress import (
    _parent_intent as _parent_intent,  # noqa: PLC0414 -- shared helper export
)
from ..stored_json import read_row_column
from ..strict_json import serialize_json_value
from .constants import _bounded_blocker_reason
from .errors import RecipeRequestInvalid, RecipeRetryLater
from .interfaces import RecipeOperationView
from .observation_helpers import _active_recipe_revision

if TYPE_CHECKING:
    from .service import RecipeOperationService


class InstallationPreparationMixin:
    def preview_install(
        self,
        mapping_id: str,
        recipe_build_id: str | None,
        *,
        profile_application_id: str | None = None,
    ) -> InstallPlan:
        service = typing_cast("RecipeOperationService", self)
        return service._install_admission.plan_install(
            mapping_id,
            recipe_build_id,
            now=service._clock(),
            profile_application_id=profile_application_id,
        )

    def prepare_installation(
        self,
        plan: InstallPlan,
        *,
        actor: str,
        profile_application_id: str | None = None,
        workload_intent_ordinal: int | None = None,
    ) -> str:
        service = typing_cast("RecipeOperationService", self)
        last_error: UnknownOutcomeError = RecipeRetryLater(
            "installation preparation observation exhausted"
        )
        for _attempt in admission_attempts():
            try:
                return service._prepare_installation_once(
                    plan,
                    actor=actor,
                    profile_application_id=profile_application_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                )
            except UnknownOutcomeError as error:
                if service._install_preparation is not None:
                    service._install_preparation(
                        plan,
                        actor,
                        f"{plan.plan_digest}:{profile_application_id}:{workload_intent_ordinal}",
                    )
                last_error = error
        raise last_error

    def _prepare_installation_once(
        self,
        plan: InstallPlan,
        *,
        actor: str,
        profile_application_id: str | None = None,
        workload_intent_ordinal: int | None = None,
    ) -> str:
        """Persist an admitted installation without starting Spark work.

        Run/Switch has to compile and persist the exact launch document before
        it copies model/image bytes to a target.  The regular ``install``
        method intentionally queues the agent child immediately, so this
        small lifecycle primitive stops at the durable Controller boundary.
        Reusing a matching installation makes the phase safe to replay after a
        process crash between the database commit and high-level progress
        checkpoint.
        """
        service = typing_cast("RecipeOperationService", self)

        if not plan.allowed and {
            reason.code for node in plan.nodes for reason in node.blockers
        } <= {InstallAdmissionCode.INSUFFICIENT_DISK}:
            # The exact plan an earlier attempt already persisted holds its own
            # disk claim; counting that claim against itself must not stop
            # this attempt adopting it.
            with service._sessions() as session:
                adopted = service._prepared_installation_id(
                    session, plan, planned_only=True
                )
            if adopted is not None:
                return adopted
        if not plan.allowed:
            service._request_install_storage(plan)
            try:
                require_install_admissible(plan)
            except InstallAdmissionBusy:
                raise
            except InstallPlanConflict as error:
                reasons = list(
                    dict.fromkeys(
                        _bounded_blocker_reason(reason.code, reason.detail)
                        for node in plan.nodes
                        for reason in node.blockers
                    )
                )
                raise RecipeRequestInvalid(
                    "install plan is blocked: " + "; ".join(reasons[:3])
                ) from error
        now = service._clock()
        with service._sessions() as session:
            existing_id = service._prepared_installation_id(session, plan)
        if existing_id is not None:
            return existing_id
        try:
            service._install_admission.refresh_install_receipts(
                plan, now=now, profile_application_id=profile_application_id
            )
        except InstallAdmissionBusy:
            raise
        except UnknownOutcomeError:
            raise
        except (RuntimeError, ValueError) as error:
            raise RecipeRetryLater("installation evidence is unavailable") from error
        with service._sessions.begin() as session:
            existing_id = service._prepared_installation_id(session, plan)
            if existing_id is not None:
                return existing_id
            try:
                installation_id = service._install_admission.accept_install_in_session(
                    session,
                    plan,
                    actor=actor,
                    now=now,
                    profile_application_id=profile_application_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                )
            except InstallAdmissionBusy:
                raise
            except UnknownOutcomeError:
                raise
            except (RuntimeError, ValueError) as error:
                raise RecipeRetryLater(
                    "installation evidence is unavailable"
                ) from error
            installation = session.get(RecipeInstallation, installation_id)
            if installation is None:
                raise RecipeRetryLater("installation evidence is unavailable")
            # The row was written by this very transaction: its plan must carry
            # the compiled documents before anything can commit it.
            try:
                stored_plan = parse_stored_installation_plan(
                    read_row_column(installation, "plan")
                )
            except RecipeExecutionContractError as error:
                raise RecipeRetryLater(
                    "compiled execution plan was not persisted"
                ) from error
            if not stored_plan.compiled_execution_plans:
                raise RecipeRetryLater("compiled execution plan was not persisted")
            return installation_id

    @staticmethod
    def _prepared_installation_id(
        session: Session, plan: InstallPlan, *, planned_only: bool = False
    ) -> str | None:
        """The newest installation already prepared for this exact plan.

        An installation whose stored plan cannot be read is no preparation: it is
        retired as unknown and skipped, so the next acceptance prepares a fresh,
        readable one (the damaged row is left to space-driven cleanup).
        """

        candidates = session.scalars(
            select(RecipeInstallation)
            .where(
                RecipeInstallation.mapping_id == plan.mapping_id,
                RecipeInstallation.mapping_generation == plan.mapping_generation,
                RecipeInstallation.recipe_build_id == plan.recipe_build_id,
                RecipeInstallation.plan_digest == plan.plan_digest,
                RecipeInstallation.state.in_(
                    (InstallationState.PLANNED,)
                    if planned_only
                    else (
                        InstallationState.PLANNED,
                        InstallationState.INSTALLING,
                        InstallationState.PARTIAL,
                        InstallationState.INSTALLED,
                    )
                ),
            )
            .order_by(RecipeInstallation.created_at.desc())
        )
        for existing in candidates:

            def read(candidate: RecipeInstallation = existing) -> bool | Damaged:
                stored = parse_stored_installation_plan(
                    read_row_column(candidate, "plan")
                )
                if not stored.compiled_execution_plans:
                    return Damaged("stored installation has no compiled plan")
                return True

            if (
                read_or_rebuild(
                    kind="recipe.installation-plan", subject=existing.id, read=read
                )
                is True
            ):
                return existing.id
        return None

    def _stored_compiled_plans(
        self,
        session: Session,
        installation: RecipeInstallation,
        node_ids: Collection[str],
        *,
        now: datetime,
    ) -> dict[str, WireCompiledExecutionPlan] | Residue:
        """The compiled launch documents an installation was accepted with.

        A stored plan that does not parse (or does not cover the ranks) is
        re-planned from its mapping and build; the result is evidence only when
        it carries the installation's own plan digest.  Otherwise the damage is
        retired as unknown and the caller refuses the request, which the next
        prepare answers with a fresh installation.
        """
        service = typing_cast("RecipeOperationService", self)

        wanted = set(node_ids)

        def read() -> dict[str, WireCompiledExecutionPlan] | Damaged:
            plans = parse_stored_installation_plan(
                read_row_column(installation, "plan")
            ).compiled_execution_plans
            if not plans or not wanted <= set(plans):
                return Damaged("stored installation plan lacks compiled documents")
            return dict(plans)

        def rebuild() -> dict[str, WireCompiledExecutionPlan] | None:
            try:
                fresh = service._install_admission.plan_install(
                    installation.mapping_id,
                    installation.recipe_build_id,
                    now=now,
                    _session=session,
                )
            except (RuntimeError, ValueError, KeyError):
                return None
            plans = fresh.compiled_plan_by_node
            if fresh.plan_digest != installation.plan_digest or not wanted <= set(
                plans
            ):
                return None
            return plans

        return read_or_rebuild(
            kind="recipe.installation-plan",
            subject=installation.id,
            read=read,
            rebuild=rebuild,
        )

    def start_installation(
        self,
        installation_id: str,
        *,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        """Queue the already prepared installation after target verification."""
        service = typing_cast("RecipeOperationService", self)

        now = service._clock()
        with service._sessions.begin() as session:
            installation = session.get(
                RecipeInstallation, installation_id, with_for_update=True
            )
            if installation is None:
                raise RecipeRequestInvalid("recipe installation is unavailable")
            existing = service._idempotent_in_session(
                session,
                request_id,
                WireAgentOperation.RECIPE_INSTALL.value,
                installation.plan_digest,
                owner_kind="installation",
                owner_id=installation_id,
            )
            if existing is not None:
                return existing
            reconciliation = session.scalar(
                select(Job.id)
                .where(
                    Job.kind == WireAgentOperation.RECIPE_RECONCILE.value,
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == installation_id,
                )
                .limit(1)
            )
            if reconciliation is not None:
                raise RecipeRetryLater("recipe installation is being reconciled")
            receipt_missing = False
            if installation.state == InstallationState.INSTALLED:
                completed = session.scalar(
                    select(Job)
                    .where(
                        Job.kind == WireAgentOperation.RECIPE_INSTALL.value,
                        Job.state == LifecycleState.SUCCEEDED.value,
                        Job.payload["owner_id"].as_string() == installation_id,
                        Job.payload["plan_digest"].as_string()
                        == installation.plan_digest,
                    )
                    .order_by(Job.updated_at.desc())
                    .limit(1)
                )
                if completed is not None:
                    return service._view(completed)
                # Installed, but its receipt is gone: the effect is unknown, so
                # the install is run again (it verifies bytes already present)
                # instead of refusing the request.
                retire_as_unknown(
                    "recipe.install-receipt",
                    installation_id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "installed installation has no successful install receipt",
                )
                receipt_missing = True
            active = session.scalar(
                select(Job)
                .where(
                    Job.kind == WireAgentOperation.RECIPE_INSTALL.value,
                    Job.state.in_(
                        (LifecycleState.QUEUED.value, LifecycleState.RUNNING.value)
                    ),
                    Job.payload["owner_id"].as_string() == installation_id,
                    Job.payload["plan_digest"].as_string() == installation.plan_digest,
                )
                .order_by(Job.updated_at.desc(), Job.id)
                .limit(1)
            )
            if active is not None:
                if (
                    workload_intent_ordinal is not None
                    and _parent_intent(active) != workload_intent_ordinal
                ):
                    raise RecipeRetryLater(
                        "prior installation intent requires exact observation before replacement"
                    )
                return service._view(active)
            if not receipt_missing and installation.state not in {
                InstallationState.PLANNED,
                InstallationState.PARTIAL,
                InstallationState.FAILED,
                InstallationState.INSTALLING,
            }:
                raise RecipeRequestInvalid("recipe installation is not launchable")
            nodes = tuple(
                session.scalars(
                    select(InstallationNode)
                    .where(InstallationNode.installation_id == installation_id)
                    .order_by(InstallationNode.rank, InstallationNode.node_id)
                )
            )
            raw_plans = service._stored_compiled_plans(
                session, installation, [node.node_id for node in nodes], now=now
            )
            if isinstance(raw_plans, Residue) or not nodes:
                raise RecipeRetryLater(
                    "stored installation plan is unreadable; it is recorded and the "
                    "next preparation installs afresh"
                )
            revision = _active_recipe_revision(session, installation.recipe_revision_id)
            if revision is None or revision.content_digest is None:
                raise RecipeRequestInvalid("recipe revision is unavailable")
            installation.state = InstallationState.INSTALLING
            installation.updated_at = now
            for node in nodes:
                node.state = InstallationNodeState.PLANNED
                node.updated_at = now
            job = service._queue_in_session(
                session,
                kind=WireAgentOperation.RECIPE_INSTALL.value,
                owner_kind="installation",
                owner_id=installation_id,
                plan_digest=installation.plan_digest,
                actor=actor,
                request_id=request_id,
                node_payloads=tuple(
                    (
                        node.node_id,
                        {
                            "installation_id": installation_id,
                            "plan_digest": installation.plan_digest,
                            "expected_bytes": node.required_bytes,
                            "compiled_execution_plan": serialize_json_value(
                                raw_plans[node.node_id]
                            ),
                        },
                    )
                    for node in nodes
                ),
                authority_digest=revision.content_digest,
                now=now,
                workload_intent_ordinal=workload_intent_ordinal,
            )
        service._agent_jobs.notify_available()
        return service.get(job.id)
