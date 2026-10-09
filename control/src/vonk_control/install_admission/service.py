"""Install admission: service."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallAdmissionCode,
    InstallationNodeState,
    InstallationState,
    InvalidRequestReason,
    ReservationState,
    UnknownOutcomeError,
    WaitReason,
)

from ..admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    admission_attempts,
    is_admission_contention,
    lock_admission_rows,
    node_admission_key,
)
from ..disk_reservations import (
    outstanding_disk_reservation_bytes,
)
from ..models import (
    AgentNode,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    NodeArtifact,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    ResourceReservation,
)
from ..profile_capacity import inherited_profile_disk
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    installation_plan_document,
)
from ..recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    resolve_recipe_entities,
)
from .contracts import (
    InstallAdmissionBusy,
    InstallEvidenceChanged,
    InstallPlan,
    InstallPlanStale,
)
from .identity import _primary_model_sha256
from .planning import InstallPlanningService
from .validation import (
    _active_recipe_revision,
    require_admissible,
    require_same_execution,
)


class InstallAdmissionService(InstallPlanningService):
    def accept_install(self, plan: InstallPlan, *, actor: str, now: datetime) -> str:
        refused: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                self.refresh_install_receipts(plan, now=now)
                with self._sessions.begin() as session:
                    return self.accept_install_in_session(
                        session, plan, actor=actor, now=now
                    )
            except UnknownOutcomeError as error:
                refused = error
        assert refused is not None
        raise refused

    def refresh_install_receipts(
        self,
        plan: InstallPlan,
        *,
        now: datetime,
        profile_application_id: str | None = None,
    ) -> None:
        """Recheck managed bytes before entering the acceptance transaction."""
        if self._compiled_plan_provider is None:
            return
        fresh = self.plan_install(
            plan.mapping_id,
            plan.recipe_build_id,
            now=now,
            profile_application_id=profile_application_id,
        )
        require_admissible(fresh)

    def accept_install_in_session(
        self,
        session: Session,
        plan: InstallPlan,
        *,
        actor: str,
        now: datetime,
        profile_application_id: str | None = None,
        workload_intent_ordinal: int | None = None,
    ) -> str:
        try:
            reviewed = plan
            plan = self.plan_install(
                plan.mapping_id,
                plan.recipe_build_id,
                now=now,
                _session=session,
                compiled_execution_plans=plan.compiled_plan_by_node,
                profile_application_id=profile_application_id,
            )
            require_admissible(plan)
            require_same_execution(reviewed, plan)
            acquire_admission_keys(
                session,
                tuple(node_admission_key(node.node_id) for node in plan.nodes),
                holder="install-admission",
            )
            return self._accept_install_in_session(
                session,
                plan,
                actor=actor,
                now=now,
                profile_application_id=profile_application_id,
                workload_intent_ordinal=workload_intent_ordinal,
            )
        except AdmissionLockBusy as error:
            raise InstallAdmissionBusy(
                InstallAdmissionCode.CAPACITY_BUSY,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error
        except OperationalError as error:
            if is_admission_contention(error):
                raise InstallAdmissionBusy(
                    InstallAdmissionCode.CAPACITY_BUSY,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            raise

    def _accept_install_in_session(
        self,
        session: Session,
        plan: InstallPlan,
        *,
        actor: str,
        now: datetime,
        profile_application_id: str | None = None,
        workload_intent_ordinal: int | None = None,
    ) -> str:
        node_ids = tuple(node.node_id for node in plan.nodes)
        requests = [
            AdmissionRowLock(
                "target-agent-nodes",
                AgentNode,
                select(AgentNode).where(AgentNode.node_id.in_(node_ids)),
            ),
            AdmissionRowLock(
                "reviewed-catalog-revision",
                CatalogDocumentRevision,
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.id == plan.recipe_revision_id
                ),
            ),
            AdmissionRowLock(
                "reviewed-mapping",
                ClusterMapping,
                select(ClusterMapping).where(ClusterMapping.id == plan.mapping_id),
            ),
            AdmissionRowLock(
                "reviewed-mapping-nodes",
                ClusterMappingNode,
                select(ClusterMappingNode).where(
                    ClusterMappingNode.mapping_id == plan.mapping_id
                ),
            ),
        ]
        if plan.recipe_build_id is not None:
            requests.append(
                AdmissionRowLock(
                    "reviewed-recipe-build",
                    RecipeBuild,
                    select(RecipeBuild).where(RecipeBuild.id == plan.recipe_build_id),
                )
            )
        requests.extend(
            (
                AdmissionRowLock(
                    "node-artifacts",
                    NodeArtifact,
                    select(NodeArtifact).where(NodeArtifact.node_id.in_(node_ids)),
                ),
                AdmissionRowLock(
                    "node-inventory-snapshots",
                    NodeInventorySnapshot,
                    select(NodeInventorySnapshot).where(
                        NodeInventorySnapshot.node_id.in_(node_ids)
                    ),
                ),
                AdmissionRowLock(
                    "node-resource-reservations",
                    ResourceReservation,
                    select(ResourceReservation).where(
                        ResourceReservation.node_id.in_(node_ids)
                    ),
                ),
            )
        )
        lock_admission_rows(session, requests)
        mapping = session.get(ClusterMapping, plan.mapping_id)
        build = (
            session.get(RecipeBuild, plan.recipe_build_id)
            if plan.recipe_build_id is not None
            else None
        )
        revision = _active_recipe_revision(session, plan.recipe_revision_id)
        if (
            mapping is None
            or mapping.state != "ready"
            or revision is None
            or build is None
            or build.state != "succeeded"
            or build.image_digest != plan.image_digest
        ):
            raise InstallPlanStale(
                "mapping or build changed while reserving",
                reason=InvalidRequestReason.SUPERSEDED,
            )
        mapping_nodes = tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == plan.mapping_id)
                .order_by(ClusterMappingNode.rank)
            )
        )
        claims = (
            inherited_profile_disk(
                session,
                profile_application_id,
                plan.recipe_revision_id,
                node_ids,
                workload_intent_ordinal=workload_intent_ordinal,
                now=now,
            )
            if profile_application_id is not None
            else {}
        )
        fresh = self.plan_install(
            plan.mapping_id,
            plan.recipe_build_id,
            now=now,
            _session=session,
            compiled_execution_plans=plan.compiled_plan_by_node,
            profile_application_id=profile_application_id,
        )
        require_admissible(fresh)
        require_same_execution(plan, fresh)
        if {node.node_id for node in fresh.nodes} != set(node_ids):
            raise InstallAdmissionBusy(
                "install target membership changed during admission",
                reason=WaitReason.SCOPE_CHANGED,
            )
        plan = fresh
        if (
            revision is None
            or revision.state != "active"
            or revision.content_digest != plan.recipe_content_sha256
            or tuple((node.node_id, node.rank, node.role) for node in mapping_nodes)
            != tuple((node.node_id, node.rank, node.role) for node in plan.nodes)
        ):
            raise InstallPlanStale(
                InstallAdmissionCode.PLAN_STALE, reason=InvalidRequestReason.SUPERSEDED
            )
        try:
            resolve_recipe_entities(session, revision.document)
        except RecipeRuntimeSpecError as error:
            raise InstallPlanStale(
                InstallAdmissionCode.DEPENDENCIES_STALE,
                reason=InvalidRequestReason.SUPERSEDED,
            ) from error
        except (TypeError, ValueError) as error:
            raise InstallPlanStale(
                InstallAdmissionCode.DEPENDENCIES_STALE,
                reason=InvalidRequestReason.SUPERSEDED,
            ) from error
        try:
            persisted_plan = installation_plan_document(
                plan.stored_plan().model_dump(mode="json")
            )
        except RecipeExecutionContractError as error:
            raise InstallPlanStale(
                InstallAdmissionCode.PLAN_INVALID, reason=InvalidRequestReason.MALFORMED
            ) from error
        installation = RecipeInstallation(
            recipe_revision_id=plan.recipe_revision_id,
            model_content_sha256=_primary_model_sha256(revision.document),
            mapping_id=plan.mapping_id,
            mapping_generation=plan.mapping_generation,
            recipe_build_id=plan.recipe_build_id,
            image_digest=plan.image_digest,
            plan_digest=plan.plan_digest,
            plan=persisted_plan,
            state=InstallationState.PLANNED,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        for node in sorted(fresh.nodes, key=lambda item: item.node_id):
            if session.get(AgentNode, node.node_id) is None:
                raise InstallEvidenceChanged(
                    "installation node disappeared", reason=WaitReason.SCOPE_CHANGED
                )
            active = outstanding_disk_reservation_bytes(
                session,
                node.node_id,
                inventory_observed_at=node.inventory_observed_at,
                excluded_profile_application_ids=(profile_application_id,)
                if profile_application_id is not None
                else (),
            )
            if (
                node.free_bytes is None
                or node.free_bytes - active - node.required_bytes
                < node.disk_floor_bytes
            ):
                raise InstallEvidenceChanged(
                    "disk capacity changed while reserving",
                    reason=WaitReason.SCOPE_CHANGED,
                )
        session.add(installation)
        session.flush()
        for node in plan.nodes:
            inherited = claims.get(node.node_id)
            if inherited is not None and node.required_bytes > inherited.amount_bytes:
                raise InstallPlanStale(
                    "installation exceeds its reviewed disk claim",
                    reason=InvalidRequestReason.CONFLICT,
                )
            session.add(
                InstallationNode(
                    installation_id=installation.id,
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    state=InstallationNodeState.PLANNED,
                    required_bytes=node.required_bytes,
                    installed_bytes=0,
                    updated_at=now,
                )
            )
            if inherited is not None:
                claim = inherited
                claim.owner_kind = "installation"
                claim.owner_id = installation.id
                claim.resource_key = plan.plan_digest
                claim.plan_digest = plan.plan_digest
                claim.amount_bytes = node.required_bytes
            else:
                session.add(
                    ResourceReservation(
                        node_id=node.node_id,
                        kind="disk",
                        resource_key=plan.plan_digest,
                        amount_bytes=node.required_bytes,
                        owner_kind="installation",
                        owner_id=installation.id,
                        state=ReservationState.ACTIVE,
                        plan_digest=plan.plan_digest,
                        created_at=now,
                    )
                )
        return installation.id
