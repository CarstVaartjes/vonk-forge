"""Role-aware disk admission for one mapping generation and exact OCI build."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    InstallAdmissionCode,
    InstallationNodeState,
    InstallationState,
    InvalidRequestError,
    InvalidRequestReason,
    ModelFileState,
    ReservationState,
    RuntimePreflightCode,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)

from .admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    is_admission_contention,
    lock_admission_rows,
    node_admission_key,
)
from .categorized_errors import (
    BookkeepingUnknown,
    InvalidType,
    InvalidValue,
    MissingRecord,
)
from .cluster_mappings import validate_mapping_parameters
from .content_identity import same_image, same_model_object
from .disk_reservations import (
    describe_disk_charges,
    models_stored_on_node,
    outstanding_disk_charges,
    outstanding_disk_reservation_bytes,
)
from .inventory_repository import InventoryRepository, InventorySnapshotView
from .legal_admission import territorial_admission
from .models import (
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
from .profile_capacity import inherited_profile_disk
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    installation_plan_document,
)
from .recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    recipe_topology,
    resolve_recipe_entities,
)
from .resource_planning import installation_disk_requirement
from .runtime_preflight import (
    admission_blockers,
    latest_result,
    recipe_requirements,
    request_digest,
)
from .topology import Placement, TopologyError, validate_topology

# Sparks pull runtime images from the Controller's layered image store; an
# agent without this capability cannot install or start any recipe.
IMAGE_PULL_CAPABILITY = "recipe.image.pull.v1"
AGENT_UPGRADE_REQUIRED_DETAIL = (
    "Upgrade the Spark agent: this agent cannot pull runtime images from the "
    "Controller's layered image store."
)


@dataclass(frozen=True, slots=True)
class AdmissionReason:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class InstallNodePlan:
    node_id: str
    rank: int
    role: str
    allowed: bool
    inventory_observed_at: datetime | None
    free_bytes: int | None
    active_reserved_bytes: int
    reused_bytes: int
    required_download_bytes: int
    required_bytes: int
    #: The model payload the compiled plan materializes into the installation
    #: tree.  ``required_bytes`` is the disk reservation, so the presence health
    #: check compares the agent's measured tree against this payload instead.
    required_payload_bytes: int
    disk_floor_bytes: int
    free_after_bytes: int | None
    blockers: tuple[AdmissionReason, ...]
    warnings: tuple[AdmissionReason, ...]


@dataclass(frozen=True, slots=True)
class InstallPlan:
    mapping_id: str
    mapping_generation: int
    recipe_build_id: str | None
    image_digest: str
    recipe_revision_id: str
    recipe_content_sha256: str
    allowed: bool
    nodes: tuple[InstallNodePlan, ...]
    plan_digest: str
    # One strict Controller-issued launch document per mapped node.  It is
    # appended to preserve the positional shape used by older in-process
    # callers; production admission populates it before accepting an install.
    compiled_execution_plans: tuple[tuple[str, WireCompiledExecutionPlan], ...] = ()

    @property
    def compiled_plan_by_node(self) -> dict[str, WireCompiledExecutionPlan]:
        return dict(self.compiled_execution_plans)


class InstallPlanConflict(RuntimeError):
    code = InstallAdmissionCode.PLAN_INVALID


class InstallPlanStale(InvalidRequestError, InstallPlanConflict):
    """The plan no longer fits the request or the recipe: the caller re-plans."""


class InstallEvidenceChanged(UnknownOutcomeError, InstallPlanConflict):
    """Evidence the plan rested on moved or is unavailable: observe and retry."""


class StoredInstallIdentityDamaged(UnknownOutcomeError, ValueError, TypeError):
    """A stored installation identity that does not parse: unknown, to rebuild."""


class InstallAdmissionBusy(UnknownOutcomeError, InstallPlanConflict):
    """A capacity writer owns the row; retry only after releasing this transaction."""

    code = InstallAdmissionCode.CAPACITY_BUSY

    def __init__(
        self,
        *args: object,
        reason: WaitReason | None = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(*args, reason=reason)


class InstallPreflightExpired(InstallAdmissionBusy):
    """The exact plan is admissible except that its runtime evidence expired."""

    code = RuntimePreflightCode.STALE

    def __init__(
        self,
        code: str = RuntimePreflightCode.STALE,
        detail: str | None = None,
        *,
        reason: WaitReason | None = WaitReason.STALE_PLAN,
    ):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code, reason=reason)


#: Starts the detail of a ``compiled_plan_unavailable`` blocker whose cause is an
#: outcome that cannot be confirmed yet (as opposed to a plan that is invalid):
#: the install waits and is admitted again instead of ending as blocked.
UNSETTLED_PLAN_PREFIX = "Waiting for evidence: "

_RETRYABLE_INSTALL_BLOCKERS = {
    InstallAdmissionCode.INVENTORY_MISSING,
    InstallAdmissionCode.STALE_INVENTORY,
    InstallAdmissionCode.INSUFFICIENT_DISK,
    InstallAdmissionCode.ARTIFACT_STORE_READ_ONLY,
    InstallAdmissionCode.IMAGE_DISTRIBUTION_PENDING,
    RuntimePreflightCode.HOST_CHANGED,
    RuntimePreflightCode.REQUIREMENTS_CHANGED,
    RuntimePreflightCode.STALE,
}

_REFRESHABLE_PREFLIGHT_BLOCKERS = {
    RuntimePreflightCode.HOST_CHANGED,
    RuntimePreflightCode.REQUIREMENTS_CHANGED,
    RuntimePreflightCode.STALE,
}


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


class InstallAdmissionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        inventory_max_age: int = 300,
        disk_floor_bytes: int = 10_000_000_000,
        compiled_plan_provider: Callable[..., Mapping[str, WireCompiledExecutionPlan]]
        | None = None,
    ) -> None:
        self._sessions = sessions
        self._inventory = InventoryRepository(sessions)
        self._inventory_max_age = inventory_max_age
        self._disk_floor = disk_floor_bytes
        self._compiled_plan_provider = compiled_plan_provider

    def plan_install(
        self,
        mapping_id: str,
        recipe_build_id: str | None,
        *,
        now: datetime,
        _session: Session | None = None,
        compiled_execution_plans: Mapping[str, WireCompiledExecutionPlan] | None = None,
        profile_application_id: str | None = None,
    ) -> InstallPlan:
        with (
            nullcontext(_session) if _session is not None else self._sessions()
        ) as session:
            mapping = session.get(ClusterMapping, mapping_id)
            build = (
                session.get(RecipeBuild, recipe_build_id)
                if recipe_build_id is not None
                else None
            )
            if mapping is None:
                raise MissingRecord(mapping_id, reason=InvalidRequestReason.NOT_FOUND)
            if recipe_build_id is not None and build is None:
                raise MissingRecord(
                    recipe_build_id, reason=InvalidRequestReason.NOT_FOUND
                )
            if mapping.state != "ready":
                raise InvalidValue(
                    "cluster mapping is not ready",
                    reason=InvalidRequestReason.NOT_READY,
                )
            revision = _active_recipe_revision(session, mapping.recipe_revision_id)
            if (
                revision is None
                or revision.state != "active"
                or revision.content_digest is None
            ):
                raise InvalidValue(
                    "recipe revision is not resolved",
                    reason=InvalidRequestReason.NOT_READY,
                )
            if (
                build is None
                or build.state != "succeeded"
                or build.image_digest is None
                or build.image_bytes is None
                or build.oci_layout_sha256 is None
            ):
                raise InvalidValue(
                    "successful recipe build does not match the mapping",
                    reason=InvalidRequestReason.NOT_READY,
                )
            mapping_nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id == mapping.id)
                    .order_by(ClusterMappingNode.rank)
                )
            )
            nodes = tuple(
                session.scalars(
                    select(AgentNode).where(
                        AgentNode.node_id.in_(
                            [mapping_node.node_id for mapping_node in mapping_nodes]
                        )
                    )
                )
            )
            preflight_request = recipe_requirements(
                revision.document,
                source_build=False,
                minimum_free_bytes=self._disk_floor,
            )
            runtime_blockers_by_node = {
                node.node_id: admission_blockers(
                    preflight_request,
                    latest_result(
                        session,
                        node.node_id,
                        requirements_sha256=request_digest(preflight_request),
                    ),
                    current_fingerprint=node.preflight_fingerprint,
                    now=int(now.timestamp()),
                )
                for node in nodes
            }
            inventory_by_node: dict[str, InventorySnapshotView | None] = {}
            for mapping_node in mapping_nodes:
                try:
                    inventory_by_node[mapping_node.node_id] = self._inventory.latest(
                        mapping_node.node_id,
                        now=now,
                        maximum_age=self._inventory_max_age,
                        _session=session,
                    )
                except KeyError:
                    inventory_by_node[mapping_node.node_id] = None
            known_inventory = {
                node_id: inventory
                for node_id, inventory in inventory_by_node.items()
                if inventory is not None
            }
            document = revision.document
            try:
                resolved_entities = resolve_recipe_entities(session, document)
            except RecipeRuntimeSpecError as error:
                raise BookkeepingUnknown(
                    "exact recipe dependencies are unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            models = resolved_entities.model_revisions
            model_document = models[0].document if models else None
            if not isinstance(model_document, Mapping):
                raise BookkeepingUnknown(
                    "exact model license authority is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            compiled_plan_error: str | None = None
            compiled_plan_unsettled = False
            # The snapshot is complete. Production compilation consults managed
            # storage, so release the read transaction before invoking it.
            if _session is None:
                session.close()
            if (
                compiled_execution_plans is None
                and self._compiled_plan_provider is not None
            ):
                try:
                    compiled_execution_plans = self._compiled_plan_provider(
                        session=session,
                        revision=revision,
                        build=build,
                        mapping=mapping,
                        mapping_nodes=mapping_nodes,
                        parameters=validate_mapping_parameters(mapping.parameters),
                        resolved_entities=resolved_entities,
                    )
                except UnknownOutcomeError as error:
                    # Evidence that cannot be confirmed now (a receipt, storage
                    # or bookkeeping) is no verdict on the plan: the blockers it
                    # leaves are waiting ones, and the admitting owner retries.
                    compiled_plan_error = str(error)[:512]
                    compiled_plan_unsettled = True
                    compiled_execution_plans = {}
                except Exception as error:  # noqa: BLE001 - provider errors become typed admission evidence
                    compiled_plan_error = str(error)[:512]
                    compiled_execution_plans = {}
            compiled_plan_by_node = dict(compiled_execution_plans or {})
            # Build input identity belongs to build resolution; current-revision
            # authorization and present verified bytes belong to the compiler's
            # runtime-image resolver. The recipe that originally produced an
            # archive is provenance, not the recipe allowed to consume it now.
            # Bind that authorized receipt back to the exact selected result so
            # reuse cannot silently select a different build or archive.
            if build is not None and any(
                not _compiled_build_matches(value, build, revision.content_digest)
                for value in compiled_plan_by_node.values()
            ):
                compiled_plan_error = (
                    "compiled runtime image differs from the selected build"
                )
                compiled_plan_by_node = {}
            legal_admission = territorial_admission(
                model_document,
                operation="install",
            )
            topology_reason: AdmissionReason | None = None
            capabilities_by_node = {
                node.node_id: tuple(
                    sorted(
                        set(
                            known_inventory[node.node_id].capabilities
                            if node.node_id in known_inventory
                            else ()
                        )
                    )
                )
                for node in nodes
            }
            try:
                validate_topology(
                    document,
                    tuple(
                        Placement(
                            mapping_node.node_id,
                            mapping_node.rank,
                            mapping_node.role,
                            mapping_node.endpoint_owner,
                        )
                        for mapping_node in mapping_nodes
                    ),
                    capabilities_by_node,
                )
            except TopologyError as error:
                topology_reason = AdmissionReason(error.code, str(error))
            recipe_digest = revision.content_digest
            mapping_generation = mapping.generation
            image_digest = build.image_digest
            image_bytes = build.image_bytes
        role_by_name = {role.name: role for role in recipe_topology(document).roles}
        plans: list[InstallNodePlan] = []
        for mapping_node in mapping_nodes:
            blockers: list[AdmissionReason] = [
                AdmissionReason(reason.code, reason.detail)
                for reason in runtime_blockers_by_node.get(mapping_node.node_id, ())
            ]
            warnings: list[AdmissionReason] = []
            if topology_reason is not None:
                blockers.append(topology_reason)
            if (
                mapping_node.node_id in known_inventory
                and IMAGE_PULL_CAPABILITY
                not in known_inventory[mapping_node.node_id].capabilities
            ):
                blockers.append(
                    AdmissionReason(
                        InstallAdmissionCode.AGENT_UPGRADE_REQUIRED,
                        AGENT_UPGRADE_REQUIRED_DETAIL,
                    )
                )
            if legal_admission.warning is not None:
                warnings.append(AdmissionReason(*legal_admission.warning))
            if (
                self._compiled_plan_provider is not None
                or compiled_plan_error is not None
            ) and mapping_node.node_id not in compiled_plan_by_node:
                detail = "Controller-issued compiled execution plan is unavailable."
                if compiled_plan_error:
                    detail = f"{detail} {compiled_plan_error}"
                if compiled_plan_unsettled:
                    detail = f"{UNSETTLED_PLAN_PREFIX}{detail}"
                blockers.append(
                    AdmissionReason(
                        InstallAdmissionCode.COMPILED_PLAN_UNAVAILABLE, detail
                    )
                )
            role = role_by_name.get(mapping_node.role)
            if role is None:
                raise InvalidType(
                    "mapping role is absent from recipe topology",
                    reason=InvalidRequestReason.SUPERSEDED,
                )
            disk = role.resources.disk
            compiled_plan = compiled_plan_by_node.get(mapping_node.node_id)
            compiled_artifacts = (
                compiled_plan.artifacts if compiled_plan is not None else None
            )
            if compiled_artifacts is None:
                blockers.append(
                    AdmissionReason(
                        InstallAdmissionCode.COMPILED_PLAN_UNAVAILABLE,
                        (UNSETTLED_PLAN_PREFIX if compiled_plan_unsettled else "")
                        + "Controller-issued compiled model receipts are unavailable.",
                    )
                )
                compiled_artifacts = ()
            artifact_sizes: dict[str, int] = {}
            models_by_artifact: dict[str, object] = {}
            for artifact in compiled_artifacts:
                digest = artifact.sha256
                size = artifact.size_bytes
                models_by_artifact[digest] = artifact.model.content_sha256
                previous = artifact_sizes.setdefault(digest, size)
                if previous != size:
                    blockers.append(
                        AdmissionReason(
                            InstallAdmissionCode.COMPILED_PLAN_UNAVAILABLE,
                            "Compiled model receipts disagree about an object size.",
                        )
                    )
            actual_artifact_bytes = sum(artifact_sizes.values())
            if image_bytes is None:
                blockers.append(
                    AdmissionReason(
                        InstallAdmissionCode.COMPILED_PLAN_UNAVAILABLE,
                        (UNSETTLED_PLAN_PREFIX if compiled_plan_unsettled else "")
                        + "Controller-issued runtime image receipt is unavailable.",
                    )
                )
            elif image_bytes > disk.image_bytes:
                warnings.append(
                    AdmissionReason(
                        InstallAdmissionCode.IMAGE_SIZE_UNDERDECLARED,
                        "Image exceeds the recipe's estimate; disk admission uses its verified size.",
                    )
                )
            if actual_artifact_bytes > disk.artifact_bytes:
                warnings.append(
                    AdmissionReason(
                        InstallAdmissionCode.ARTIFACT_SIZE_UNDERDECLARED,
                        "Model files exceed the recipe's estimate; disk admission uses their verified sizes.",
                    )
                )
            snapshot = inventory_by_node.get(mapping_node.node_id)
            if snapshot is None:
                blockers.append(
                    AdmissionReason(
                        InstallAdmissionCode.INVENTORY_MISSING,
                        "No authenticated inventory is available for this GPU node.",
                    )
                )
            if snapshot is not None and snapshot.stale:
                blockers.append(
                    AdmissionReason(
                        InstallAdmissionCode.STALE_INVENTORY,
                        "GPU node disk inventory is stale; refresh it before installing.",
                    )
                )
            if snapshot is not None and snapshot.artifact_store_read_only:
                blockers.append(
                    AdmissionReason(
                        InstallAdmissionCode.ARTIFACT_STORE_READ_ONLY,
                        "The GPU node artifact store is read-only.",
                    )
                )
            with (
                nullcontext(_session) if _session is not None else self._sessions()
            ) as session:
                present = tuple(
                    session.scalars(
                        select(NodeArtifact).where(
                            NodeArtifact.node_id == mapping_node.node_id,
                            NodeArtifact.state == ModelFileState.VERIFIED,
                        )
                    )
                )
                stored_models = models_stored_on_node(session, mapping_node.node_id)
                charges = outstanding_disk_charges(
                    session,
                    mapping_node.node_id,
                    inventory_observed_at=snapshot.observed_at if snapshot else None,
                    excluded_profile_application_ids=(profile_application_id,)
                    if profile_application_id is not None
                    else (),
                )
                reserved = sum(charge.amount_bytes for charge in charges)
                holders = describe_disk_charges(session, charges)
            raw_image_digest = image_digest.removeprefix("sha256:")
            reused_image = (
                image_bytes
                if image_bytes is not None
                and any(
                    item.kind == "image"
                    and item.digest == raw_image_digest
                    and item.size_bytes == image_bytes
                    for item in present
                )
                else 0
            )
            if reused_image == 0:
                # Run/Switch may deliberately compile and persist the exact
                # schema-2 launch plan before its ordered target-copy phase.
                # The high-level operation owns the missing-image transfer;
                # admission records the gap as evidence instead of rejecting
                # a valid cold install before the Controller can distribute it.
                warnings.append(
                    AdmissionReason(
                        InstallAdmissionCode.IMAGE_DISTRIBUTION_PENDING,
                        "The exact built image will be imported by the ordered Run/Switch target-copy phase.",
                    )
                )
            reused_artifacts = sum(
                size
                for digest, size in artifact_sizes.items()
                # The Spark's shared store already holds the model of an
                # installation it has: a new installation links those files.
                if models_by_artifact.get(digest) in stored_models
                or any(
                    present_item.kind == "model"
                    and same_model_object(
                        present_item, {"sha256": digest, "size_bytes": size}
                    )
                    for present_item in present
                )
            )
            reused = reused_image + reused_artifacts
            # ``image_bytes`` is the stored image's layers, what a pull moves
            # (less when the Spark already holds shared layers). Docker keeps
            # them unpacked, a larger footprint the next inventory observes
            # in the Spark's free disk.
            required_download = max(0, actual_artifact_bytes - reused_artifacts) + max(
                0, (image_bytes or 0) - reused_image
            )
            disk_need = installation_disk_requirement(
                disk,
                required_download_bytes=required_download,
                minimum_floor_bytes=self._disk_floor,
            )
            required = disk_need.required_bytes
            floor = disk_need.floor_bytes
            free = snapshot.disk_free_bytes if snapshot else None
            free_after = None if free is None else free - reserved - required
            if free_after is not None and free_after < floor:
                blockers.append(
                    AdmissionReason(
                        InstallAdmissionCode.INSUFFICIENT_DISK,
                        (
                            f"Installation would leave {free_after} bytes, below the required {floor}-byte floor."
                            + (f" Disk is {holders}." if holders else "")
                        )[:512],
                    )
                )
            plans.append(
                InstallNodePlan(
                    mapping_node.node_id,
                    mapping_node.rank,
                    mapping_node.role,
                    not blockers,
                    snapshot.observed_at if snapshot else None,
                    free,
                    reserved,
                    reused,
                    required_download,
                    required,
                    actual_artifact_bytes,
                    floor,
                    free_after,
                    tuple(blockers),
                    tuple(warnings),
                )
            )
        identity = _installation_plan_identity(
            mapping_id=mapping_id,
            mapping_generation=mapping_generation,
            recipe_build_id=recipe_build_id,
            image_digest=image_digest,
            recipe_revision_id=revision.id,
            recipe_content_sha256=recipe_digest,
            compiled_execution_plans={
                node_id: compiled_plan_by_node[node_id].model_dump(mode="json")
                for node_id in sorted(compiled_plan_by_node)
            },
            nodes=plans,
        )
        digest = _installation_plan_digest(identity)
        return InstallPlan(
            mapping_id,
            mapping_generation,
            recipe_build_id,
            image_digest,
            revision.id,
            recipe_digest,
            all(item.allowed for item in plans),
            tuple(plans),
            digest,
            tuple(
                (node_id, compiled_plan_by_node[node_id])
                for node_id in sorted(compiled_plan_by_node)
            ),
        )

    def accept_install(self, plan: InstallPlan, *, actor: str, now: datetime) -> str:
        self.refresh_install_receipts(plan, now=now)
        with self._sessions.begin() as session:
            return self.accept_install_in_session(session, plan, actor=actor, now=now)

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
            plan = self.plan_install(
                plan.mapping_id,
                plan.recipe_build_id,
                now=now,
                _session=session,
                compiled_execution_plans=plan.compiled_plan_by_node,
                profile_application_id=profile_application_id,
            )
            require_admissible(plan)
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
                {
                    "schema_version": 1,
                    "mapping_id": plan.mapping_id,
                    "mapping_generation": plan.mapping_generation,
                    "recipe_build_id": plan.recipe_build_id,
                    "image_digest": plan.image_digest,
                    "recipe_revision_id": plan.recipe_revision_id,
                    "recipe_content_sha256": plan.recipe_content_sha256,
                    "allowed": plan.allowed,
                    "plan_digest": plan.plan_digest,
                    "compiled_execution_plans": {
                        node_id: compiled.model_dump(mode="json")
                        for node_id, compiled in plan.compiled_plan_by_node.items()
                    },
                    "nodes": [_node_document(item) for item in plan.nodes],
                }
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


def _primary_model_sha256(document: Mapping[str, object]) -> str:
    selections = document.get("models")
    model_selection = (
        selections[0]
        if isinstance(selections, Sequence)
        and not isinstance(selections, (str, bytes))
        and selections
        else None
    )
    model = (
        model_selection.get("model") if isinstance(model_selection, Mapping) else None
    )
    digest = model.get("content_sha256") if isinstance(model, Mapping) else None
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise InstallEvidenceChanged(
            InstallAdmissionCode.MODEL_IDENTITY_UNAVAILABLE,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    return digest


def _compiled_build_matches(
    payload: WireCompiledExecutionPlan, build: RecipeBuild, recipe_digest: str
) -> bool:
    return (
        payload.identity.recipe_revision_sha256 == recipe_digest
        and build.image_bytes is not None
        and same_image(payload.runtime_image, build)
    )


def _node_document(node: InstallNodePlan) -> dict[str, object]:
    return {
        **asdict(node),
        "inventory_observed_at": (
            node.inventory_observed_at.isoformat()
            if node.inventory_observed_at
            else None
        ),
    }


_INSTALL_NODE_DIGEST_FIELDS = (
    "node_id",
    "rank",
    "role",
    "reused_bytes",
    "required_download_bytes",
    "required_bytes",
    "disk_floor_bytes",
)


def _node_digest_document(
    node: InstallNodePlan | Mapping[str, object],
) -> dict[str, object]:
    """Bind install work and safety envelopes, not transient observations."""

    if isinstance(node, Mapping):
        if any(key not in node for key in _INSTALL_NODE_DIGEST_FIELDS):
            raise StoredInstallIdentityDamaged(
                "stored installation node identity is incomplete",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return {key: node[key] for key in _INSTALL_NODE_DIGEST_FIELDS}
    return {key: getattr(node, key) for key in _INSTALL_NODE_DIGEST_FIELDS}


def _installation_plan_identity(
    *,
    mapping_id: str,
    mapping_generation: int,
    recipe_build_id: str | None,
    image_digest: str,
    recipe_revision_id: str,
    recipe_content_sha256: str,
    compiled_execution_plans: Mapping[str, object],
    nodes: Sequence[InstallNodePlan | Mapping[str, object]],
) -> dict[str, object]:
    """Return the sole canonical identity document used for install hashing."""

    return {
        "schema_version": 1,
        "mapping_id": mapping_id,
        "mapping_generation": mapping_generation,
        "recipe_build_id": recipe_build_id,
        "image_digest": image_digest,
        "recipe_revision_id": recipe_revision_id,
        "recipe_content_sha256": recipe_content_sha256,
        "compiled_execution_plans": dict(compiled_execution_plans),
        "nodes": [_node_digest_document(item) for item in nodes],
    }


def _installation_plan_digest(identity: Mapping[str, object]) -> str:
    """Preserve the exact install-admission JSON digest spelling."""

    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def installation_plan_digest_from_stored_document(value: object) -> str:
    """Recompute an installation's immutable identity without parsing launch data.

    Reconciliation needs to prove that the persisted opaque document still
    matches the original accepted digest.  The compiled plans are generic JSON
    values here; this path deliberately does not construct or validate a
    ``CompiledExecutionPlan``.
    """

    if not isinstance(value, Mapping):
        raise StoredInstallIdentityDamaged(
            "stored installation identity is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    nodes = value.get("nodes")
    compiled = value.get("compiled_execution_plans")
    if (
        value.get("schema_version") != 1
        or type(value.get("mapping_generation")) is not int
        or not isinstance(nodes, list)
        or not nodes
        or not isinstance(compiled, Mapping)
        or any(not isinstance(item, Mapping) for item in nodes)
        or any(not isinstance(item, Mapping) for item in compiled.values())
    ):
        raise StoredInstallIdentityDamaged(
            "stored installation identity is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    node_documents: list[dict[str, object]] = []
    for item in nodes:
        assert isinstance(item, Mapping)
        if (
            not isinstance(item.get("node_id"), str)
            or type(item.get("rank")) is not int
            or not isinstance(item.get("role"), str)
            or any(
                not _is_nonnegative_json_int(item.get(key))
                for key in (
                    "reused_bytes",
                    "required_download_bytes",
                    "required_bytes",
                    "disk_floor_bytes",
                )
            )
        ):
            raise StoredInstallIdentityDamaged(
                "stored installation node identity is invalid",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        node_documents.append(_node_digest_document(item))
    if len({item["node_id"] for item in node_documents}) != len(node_documents):
        raise StoredInstallIdentityDamaged(
            "stored installation node identities are duplicated",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    if set(compiled) != {item["node_id"] for item in node_documents}:
        raise StoredInstallIdentityDamaged(
            "stored compiled plans do not match installation nodes",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    identity = _installation_plan_identity(
        mapping_id=value["mapping_id"],
        mapping_generation=value["mapping_generation"],
        recipe_build_id=value.get("recipe_build_id"),
        image_digest=value["image_digest"],
        recipe_revision_id=value["recipe_revision_id"],
        recipe_content_sha256=value["recipe_content_sha256"],
        compiled_execution_plans=compiled,
        nodes=nodes,
    )
    return _installation_plan_digest(identity)


def _is_nonnegative_json_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
