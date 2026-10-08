"""Executor verification."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    RouteState,
    RunState,
    RunSwitchCode,
    WaitReason,
)
from vonk_agent_protocol.compiled_execution_plan import CompiledExecutionPlan

from ..models import (
    STOPPABLE_RUN_STATES,
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
)
from ..run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchPlan,
    RunSwitchRuntimePlanResult,
)
from .errors import RunSwitchRetryLater
from .interfaces import PhaseExecution
from .result_helpers import _phase_result

if TYPE_CHECKING:
    from .executor import RecipeLifecyclePhaseExecutor


class ExecutorVerificationMixin:
    @staticmethod
    def _prepared_installation_result(
        installation_id: str,
        mapping_id: str,
        install_plan_digest: str,
        compiled: Mapping[str, CompiledExecutionPlan],
    ) -> PhaseExecution:
        first_compiled = next(iter(compiled.values()), None)
        identity = first_compiled.identity if first_compiled is not None else None
        return PhaseExecution(
            result=_phase_result(
                {
                    "phase": "prepare",
                    "subphase": "runtime-plan",
                    "installation_id": installation_id,
                    "mapping_id": mapping_id,
                    "install_plan_digest": install_plan_digest,
                    "model_artifact_set_sha256": (
                        identity.model_artifact_set_sha256
                        if identity is not None
                        else None
                    ),
                    "model_artifact_set_bytes": (
                        sum(
                            artifact.size_bytes for artifact in first_compiled.artifacts
                        )
                        if first_compiled is not None
                        else None
                    ),
                    "compiled_plan_persisted": True,
                }
            )
        )

    @staticmethod
    def _bound_installation(
        session: Session,
        plan: RunSwitchPlan,
        installation_id: str,
        mapping_id: str,
        install_plan_digest: str | None,
    ) -> tuple[RecipeInstallation, tuple[InstallationNode, ...]]:
        installation = session.get(RecipeInstallation, installation_id)
        if (
            installation is None
            or plan.mapping is None
            or installation.recipe_revision_id != plan.recipe_revision_id
            or installation.model_content_sha256 != plan.model_content_sha256
            or installation.mapping_id != mapping_id
            or installation.mapping_generation != plan.mapping.mapping_generation
            or installation.image_digest != plan.image_digest
            or (
                install_plan_digest is not None
                and installation.plan_digest != install_plan_digest
            )
        ):
            raise RunSwitchRetryLater(
                RunSwitchCode.INSTALLATION_IDENTITY_CHANGED,
                reason=WaitReason.SCOPE_CHANGED,
            )
        members = tuple(
            session.scalars(
                select(InstallationNode)
                .where(InstallationNode.installation_id == installation_id)
                .order_by(InstallationNode.rank, InstallationNode.node_id)
            )
        )
        expected = {
            (node.node_id, node.rank, node.role) for node in plan.spark_group.nodes
        }
        if {(node.node_id, node.rank, node.role) for node in members} != expected:
            raise RunSwitchRetryLater(
                RunSwitchCode.INSTALLATION_MEMBERSHIP_CHANGED,
                reason=WaitReason.SCOPE_CHANGED,
            )
        return installation, members

    def _verify_installation(
        self, plan: RunSwitchPlan, progress: RunSwitchOperationResult
    ) -> PhaseExecution:
        """Verify the bound installation, not merely its completed child job."""
        service = typing_cast("RecipeLifecyclePhaseExecutor", self)

        installation_id = plan.installation_id
        mapping_id = plan.mapping.mapping_id if plan.mapping is not None else None
        install_plan_digest = None
        phase_results = progress.phase_results
        if phase_results:
            for result in phase_results:
                if isinstance(result, RunSwitchRuntimePlanResult):
                    installation_id = result.installation_id
                    mapping_id = result.mapping_id
                    install_plan_digest = result.install_plan_digest
        if installation_id is None or mapping_id is None or plan.mapping is None:
            raise RunSwitchRetryLater(
                RunSwitchCode.INSTALLATION_VERIFICATION_UNAVAILABLE,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        with service._sessions() as session:
            installation, members = service._bound_installation(
                session, plan, installation_id, mapping_id, install_plan_digest
            )
            runs = tuple(
                session.scalars(
                    select(RecipeRun).where(
                        RecipeRun.installation_id == installation_id
                    )
                )
            )
            active_runs = sum(run.state in STOPPABLE_RUN_STATES for run in runs)
            unwithdrawn_routes = sum(
                run.route_state != RouteState.WITHDRAWN for run in runs
            )
            verified = (
                installation.state == InstallationState.INSTALLED
                and all(
                    node.state == InstallationNodeState.INSTALLED for node in members
                )
                and not active_runs
                and not unwithdrawn_routes
            )
            evidence = {
                "final_verified": verified,
                "installation_id": installation_id,
                "installation_state": installation.state,
                "active_runs": active_runs,
                "unwithdrawn_routes": unwithdrawn_routes,
                "ranks": [
                    {
                        "node_id": node.node_id,
                        "rank": node.rank,
                        "role": node.role,
                        "state": node.state,
                    }
                    for node in members
                ],
            }
            if verified:
                return PhaseExecution(
                    result=_phase_result(
                        {"phase": "final_verify", "subphase": None, **evidence}
                    )
                )
            if installation.state in {
                InstallationState.PLANNED,
                InstallationState.INSTALLING,
            }:
                return PhaseExecution(
                    result=_phase_result(
                        {"phase": "final_verify", "subphase": None, **evidence}
                    ),
                    waiting=True,
                )
        raise RunSwitchRetryLater(RunSwitchCode.INSTALLATION_VERIFICATION_FAILED)

    def _verify_cleanup(
        self, plan: RunSwitchPlan, *, request_key: str
    ) -> PhaseExecution:
        """Observe whether the scoped removal has actually taken effect.

        The installation row and its active runs are the authority, so the
        result is derived from durable state rather than from the removal
        child's own report.
        """
        service = typing_cast("RecipeLifecyclePhaseExecutor", self)

        installation_id = plan.installation_id
        if installation_id is None:
            raise RunSwitchRetryLater(
                RunSwitchCode.UNINSTALL_TARGET_UNAVAILABLE,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        reconciliation_complete = True
        reconcile_request_id: str | None = (
            str(uuid.uuid5(uuid.UUID(request_key), "reconcile"))
            if plan.cleanup_mode == "reconcile"
            else None
        )
        if plan.cleanup_mode == "reconcile" and plan.cleanup_disposition != "abandon":
            if service._lifecycle is None or plan.reconciliation_authority is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.RECONCILIATION_AUTHORITY_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            assert reconcile_request_id is not None
            reconciliation_complete = service._lifecycle.reconciliation_complete(
                reconcile_request_id,
                expected_authority=plan.reconciliation_authority,
            )
        with service._sessions() as session:
            installation = session.get(RecipeInstallation, installation_id)
            members = tuple(
                session.scalars(
                    select(InstallationNode)
                    .where(InstallationNode.installation_id == installation_id)
                    .order_by(InstallationNode.rank, InstallationNode.node_id)
                )
            )
            runs = tuple(
                session.scalars(
                    select(RecipeRun).where(
                        RecipeRun.installation_id == installation_id
                    )
                )
            )
            active_runs = sum(
                run.state != RunState.STOPPED or run.route_state != RouteState.WITHDRAWN
                for run in runs
            )
        if plan.cleanup_mode == "reconcile":
            expected_members = {
                (node.node_id, node.rank, node.role) for node in plan.spark_group.nodes
            }
            exact_members = {
                (node.node_id, node.rank, node.role) for node in members
            } == expected_members
            removed = (
                installation is not None
                and installation.state == InstallationState.UNINSTALLED
                and exact_members
                and all(
                    node.state == InstallationNodeState.UNINSTALLED for node in members
                )
                and reconciliation_complete
            )
            if not reconciliation_complete:
                raise RunSwitchRetryLater(
                    RunSwitchCode.RECONCILIATION_VERIFICATION_FAILED
                )
            if (
                installation is None
                or installation.state != InstallationState.UNINSTALLED
                or not exact_members
                or any(
                    node.state != InstallationNodeState.UNINSTALLED for node in members
                )
            ):
                raise RunSwitchRetryLater(
                    RunSwitchCode.RECONCILIATION_STATE_VERIFICATION_FAILED
                )
        else:
            removed = (
                installation is None
                or installation.state == InstallationState.UNINSTALLED
            )
        evidence = {
            "installation_id": installation_id,
            "installation_state": (
                installation.state if installation is not None else None
            ),
            "removed": removed,
            "active_runs": active_runs,
            "cleanup_mode": plan.cleanup_mode,
            **(
                {"reconciliation_request_id": reconcile_request_id}
                if plan.cleanup_mode == "reconcile"
                else {}
            ),
        }
        if removed and not active_runs:
            return PhaseExecution(
                result=_phase_result(
                    {
                        "phase": "final_verify",
                        "subphase": None,
                        "final_verified": True,
                        **evidence,
                    }
                )
            )
        return PhaseExecution(
            result=_phase_result(
                {
                    "phase": "final_verify",
                    "subphase": None,
                    "final_verified": False,
                    **evidence,
                }
            ),
            waiting=True,
        )
