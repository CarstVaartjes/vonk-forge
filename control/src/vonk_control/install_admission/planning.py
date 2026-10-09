"""Install admission: planning."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    InstallAdmissionCode,
    InvalidRequestReason,
    ModelFileState,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)

from ..categorized_errors import (
    BookkeepingUnknown,
    InvalidType,
    InvalidValue,
    MissingRecord,
)
from ..cluster_mappings import validate_mapping_parameters
from ..content_identity import same_model_object
from ..disk_reservations import (
    describe_disk_charges,
    models_stored_on_node,
    outstanding_disk_charges,
)
from ..inventory_repository import InventoryRepository, InventorySnapshotView
from ..legal_admission import territorial_admission
from ..models import (
    AgentNode,
    ClusterMapping,
    ClusterMappingNode,
    NodeArtifact,
    RecipeBuild,
)
from ..recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    recipe_topology,
    resolve_recipe_entities,
)
from ..resource_planning import installation_disk_requirement
from ..runtime_preflight import (
    admission_blockers,
    latest_result,
    recipe_requirements,
    request_digest,
)
from ..topology import Placement, TopologyError, validate_topology
from .contracts import (
    AGENT_UPGRADE_REQUIRED_DETAIL,
    IMAGE_PULL_CAPABILITY,
    UNSETTLED_PLAN_PREFIX,
    AdmissionReason,
    InstallNodePlan,
    InstallPlan,
)
from .identity import (
    _compiled_build_matches,
    _installation_plan_digest,
    _installation_plan_identity,
)
from .validation import _active_recipe_revision


class InstallPlanningService:
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
                raise BookkeepingUnknown(
                    "recipe build evidence is unavailable",
                    reason=WaitReason.RECEIPT_MISSING,
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
                raise BookkeepingUnknown(
                    "recipe build evidence is unavailable",
                    reason=WaitReason.RECEIPT_MISSING,
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
                    compiled_plan_unsettled = True
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
