"""Install for digest-bound recipe operations."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast as typing_cast

from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InstallAdmissionCode,
    InstallationState,
    InvalidRequestError,
    InvalidRequestReason,
    SecurityRefusalError,
    UnknownOutcomeError,
    WaitReason,
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
    InstallPlan,
)
from ..install_admission import (
    require_admissible as require_install_admissible,
)
from ..install_admission import (
    require_same_execution as require_same_install_execution,
)
from ..models import (
    RecipeInstallation,
)
from ..strict_json import serialize_json_value
from .errors import RecipeRequestInvalid, RecipeRetryLater
from .interfaces import RecipeOperationView

if TYPE_CHECKING:
    from .service import RecipeOperationService


class InstallMixin:
    def _install_once(
        self,
        plan: InstallPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        if plan_digest != plan.plan_digest:
            raise RecipeRequestInvalid(
                "reviewed plan digest does not match the submitted plan",
                reason=InvalidRequestReason.CONFLICT,
            )
        reviewed = plan
        now = service._clock()
        install_identity = (plan.mapping_id, plan.recipe_build_id)
        existing = service._idempotent(
            request_id,
            WireAgentOperation.RECIPE_INSTALL.value,
            None,
            install_identity=install_identity,
        )
        if existing is not None:
            return existing
        plan = service._install_admission.plan_install(
            plan.mapping_id,
            plan.recipe_build_id,
            now=now,
        )
        if not plan.allowed:
            service._request_install_storage(plan)
        require_install_admissible(plan)
        require_same_install_execution(reviewed, plan)
        try:
            service._install_admission.refresh_install_receipts(plan, now=now)
        except (SecurityRefusalError, InvalidRequestError, UnknownOutcomeError):
            raise
        except (RuntimeError, ValueError, TypeError, OSError) as error:
            raise RecipeRetryLater(str(error)) from error
        with service._sessions.begin() as session:
            try:
                acquire_admission_keys(
                    session,
                    (
                        job_request_key(request_id),
                        *(node_admission_key(node.node_id) for node in plan.nodes),
                    ),
                    holder="recipe-operation",
                )
            except AdmissionLockBusy as error:
                raise InstallAdmissionBusy(
                    InstallAdmissionCode.CAPACITY_BUSY,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            replay = service._idempotent_in_session(
                session,
                request_id,
                WireAgentOperation.RECIPE_INSTALL.value,
                None,
                install_identity=install_identity,
            )
            if replay is not None:
                return replay
            try:
                installation_id = service._install_admission.accept_install_in_session(
                    session, plan, actor=actor, now=now
                )
            except (SecurityRefusalError, InvalidRequestError, UnknownOutcomeError):
                raise
            except (RuntimeError, ValueError, TypeError, OSError) as error:
                raise RecipeRetryLater(str(error)) from error
            installation = session.get(RecipeInstallation, installation_id)
            assert installation is not None
            installation.state = InstallationState.INSTALLING
            installation.updated_at = now
            compiled_plans = plan.compiled_plan_by_node
            if set(compiled_plans) != {node.node_id for node in plan.nodes}:
                raise RecipeRetryLater(
                    "compiled execution plan is missing for one or more mapped nodes"
                )
            job = service._queue_in_session(
                session,
                kind=WireAgentOperation.RECIPE_INSTALL.value,
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
                            "plan_digest": plan.plan_digest,
                            "expected_bytes": node.required_bytes,
                            "compiled_execution_plan": serialize_json_value(
                                compiled_plans[node.node_id]
                            ),
                        },
                    )
                    for node in plan.nodes
                ),
                authority_digest=plan.recipe_content_sha256,
                now=now,
                workload_intent_ordinal=workload_intent_ordinal,
            )
        service._agent_jobs.notify_available()
        return service.get(job.id)

    def install(
        self,
        plan: InstallPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        refused: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                return service._install_once(
                    plan,
                    plan_digest=plan_digest,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                )
            except UnknownOutcomeError as error:
                refused = error
                if admission_wait_exhausted(error):
                    break
        assert refused is not None
        raise refused
