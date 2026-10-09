"""Install admission: validation."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallAdmissionCode,
    InvalidRequestReason,
    WaitReason,
)

from ..models import (
    CatalogDocumentRevision,
)
from .contracts import (
    _REFRESHABLE_PREFLIGHT_BLOCKERS,
    _RETRYABLE_INSTALL_BLOCKERS,
    UNSETTLED_PLAN_PREFIX,
    InstallAdmissionBusy,
    InstallPlan,
    InstallPlanStale,
    InstallPreflightExpired,
)


def require_admissible(plan: InstallPlan) -> None:
    if plan.allowed:
        return
    codes = {reason.code for node in plan.nodes for reason in node.blockers}
    if codes and codes <= _REFRESHABLE_PREFLIGHT_BLOCKERS:
        blocker = next(
            reason
            for node in plan.nodes
            for reason in node.blockers
            if reason.code in _REFRESHABLE_PREFLIGHT_BLOCKERS
        )
        raise InstallPreflightExpired(
            blocker.code, blocker.detail, reason=WaitReason.STALE_PLAN
        )
    if codes and all(
        reason.code in _RETRYABLE_INSTALL_BLOCKERS
        or (
            reason.code == InstallAdmissionCode.COMPILED_PLAN_UNAVAILABLE
            and reason.detail.startswith(UNSETTLED_PLAN_PREFIX)
        )
        for node in plan.nodes
        for reason in node.blockers
    ):
        causes = "; ".join(
            f"{node.node_id} {reason.code}: {reason.detail}"[:160]
            for node in plan.nodes
            for reason in node.blockers
        )[:600]
        raise InstallAdmissionBusy(
            f"install is waiting for inventory or capacity ({causes})",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    raise InstallPlanStale(
        f"{InstallAdmissionCode.PLAN_INVALID}: install plan is blocked by current admission evidence",
        reason=InvalidRequestReason.CONFLICT,
    )


def _active_recipe_revision(
    session: Session,
    revision_id: str | None,
    *,
    for_update: bool = False,
) -> CatalogDocumentRevision | None:
    """Load only an active canonical Recipe revision for admission."""

    if not isinstance(revision_id, str) or not revision_id:
        return None
    statement = select(CatalogDocumentRevision).where(
        CatalogDocumentRevision.id == revision_id,
        CatalogDocumentRevision.kind == "recipe",
        CatalogDocumentRevision.state == "active",
    )
    if for_update:
        statement = statement.with_for_update(of=CatalogDocumentRevision, nowait=True)
    return session.scalar(statement)


def require_same_execution(reviewed: InstallPlan, current: InstallPlan) -> None:
    """Re-observe storage while keeping the reviewed artifacts and target effects."""

    def identity(plan: InstallPlan) -> tuple[object, ...]:
        return (
            plan.mapping_id,
            plan.mapping_generation,
            plan.recipe_build_id,
            plan.image_digest,
            plan.recipe_revision_id,
            plan.recipe_content_sha256,
            plan.compiled_execution_plans,
            tuple(
                (
                    node.node_id,
                    node.rank,
                    node.role,
                    node.required_bytes,
                    node.required_payload_bytes,
                    node.disk_floor_bytes,
                )
                for node in plan.nodes
            ),
        )

    if identity(reviewed) != identity(current):
        raise InstallPlanStale(
            InstallAdmissionCode.PLAN_STALE, reason=InvalidRequestReason.SUPERSEDED
        )
