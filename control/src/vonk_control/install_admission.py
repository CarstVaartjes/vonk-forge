"""Role-aware disk admission for one mapping generation and exact OCI build."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .artifact_sizes import ArtifactSizeError, ArtifactSizeResolver
from .inventory_repository import InventoryRepository, InventorySnapshotView
from .legal_admission import operator_jurisdiction as validate_operator_jurisdiction
from .legal_admission import territorial_admission
from .models import (
    AgentNode,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    LocalRecipeRevision,
    NodeArtifact,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    ResourceReservation,
)
from .recipe_contract import recipe_topology
from .recipe_runtime_specs import RecipeRuntimeSpecError, resolve_recipe_entities
from .topology import Placement, TopologyError, validate_topology


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
    disk_floor_bytes: int
    free_after_bytes: int | None
    blockers: tuple[AdmissionReason, ...]
    warnings: tuple[AdmissionReason, ...]


@dataclass(frozen=True, slots=True)
class InstallPlan:
    mapping_id: str
    mapping_generation: int
    recipe_build_id: str
    image_digest: str
    recipe_revision_id: str
    recipe_content_sha256: str
    allowed: bool
    nodes: tuple[InstallNodePlan, ...]
    plan_digest: str


class InstallPlanConflict(RuntimeError):
    pass


class InstallAdmissionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        sizes: ArtifactSizeResolver,
        inventory_max_age: int = 300,
        disk_floor_bytes: int = 10_000_000_000,
        operator_jurisdiction: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._sizes = sizes
        self._inventory = InventoryRepository(sessions)
        self._inventory_max_age = inventory_max_age
        self._disk_floor = disk_floor_bytes
        self._operator_jurisdiction = validate_operator_jurisdiction(
            operator_jurisdiction
        )

    def plan_install(
        self,
        mapping_id: str,
        recipe_build_id: str,
        *,
        now: datetime,
        _session: Session | None = None,
    ) -> InstallPlan:
        with (
            nullcontext(_session) if _session is not None else self._sessions()
        ) as session:
            mapping = session.get(ClusterMapping, mapping_id)
            build = session.get(RecipeBuild, recipe_build_id)
            if mapping is None:
                raise KeyError(mapping_id)
            if build is None:
                raise KeyError(recipe_build_id)
            if mapping.state != "ready":
                raise ValueError("cluster mapping is not ready")
            if (
                build.state != "succeeded"
                or build.image_digest is None
                or build.image_bytes is None
                or build.recipe_revision_id != mapping.recipe_revision_id
            ):
                raise ValueError("successful recipe build does not match the mapping")
            revision = session.get(LocalRecipeRevision, mapping.recipe_revision_id)
            if (
                revision is None
                or revision.lifecycle != "resolved"
                or revision.content_sha256 is None
            ):
                raise ValueError("recipe revision is not resolved")
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
            document = revision.document
            try:
                resolved_entities = resolve_recipe_entities(session, document)
            except RecipeRuntimeSpecError as error:
                raise ValueError("exact recipe dependencies are unavailable") from error
            model_version = resolved_entities.get("model_version")
            model_document = getattr(model_version, "document", None)
            if not isinstance(model_document, Mapping):
                raise ValueError(  # noqa: TRY004
                    "exact model license authority is unavailable"
                )
            legal_admission = territorial_admission(
                model_document,
                self._operator_jurisdiction,
                operation="install",
            )
            topology_reason: AdmissionReason | None = None
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
                    {
                        node.node_id: tuple(
                            sorted(
                                {
                                    capability
                                    for capability in node.capabilities
                                    if not capability.startswith("fabric.")
                                }
                                | set(
                                    inventory_by_node[node.node_id].capabilities
                                    if inventory_by_node.get(node.node_id) is not None
                                    else ()
                                )
                            )
                        )
                        for node in nodes
                    },
                )
            except TopologyError as error:
                topology_reason = AdmissionReason(error.code, str(error))
            recipe_digest = revision.content_sha256
            mapping_generation = mapping.generation
            image_digest = build.image_digest
            image_bytes = build.image_bytes
        topology = recipe_topology(document)
        roles = topology.get("roles")
        if not isinstance(roles, list):
            raise TypeError("recipe topology roles are invalid")
        role_by_name = {
            str(role["name"]): role for role in roles if isinstance(role, dict)
        }
        try:
            artifacts = self._sizes.resolve(document)
        except ArtifactSizeError:
            artifacts = ()
        artifact_by_source = {item.source: item for item in artifacts}
        raw_artifacts = document.get("artifacts")
        if not isinstance(raw_artifacts, list):
            raise TypeError("recipe artifacts are invalid")
        artifact_source_by_id = {
            str(item["id"]): f"{item['repository']}@{item['revision']}"
            for item in raw_artifacts
            if isinstance(item, dict)
        }
        plans: list[InstallNodePlan] = []
        for mapping_node in mapping_nodes:
            blockers: list[AdmissionReason] = []
            warnings: list[AdmissionReason] = []
            if topology_reason is not None:
                blockers.append(topology_reason)
            if legal_admission.blocker is not None:
                blockers.append(AdmissionReason(*legal_admission.blocker))
            if legal_admission.warning is not None:
                warnings.append(AdmissionReason(*legal_admission.warning))
            role = role_by_name.get(mapping_node.role)
            if role is None or not isinstance(role.get("resources"), dict):
                raise TypeError("mapping role is absent from recipe topology")
            resources = role["resources"]
            disk = resources.get("disk")
            artifact_ids = role.get("artifacts")
            if not isinstance(disk, dict) or not isinstance(artifact_ids, list):
                raise TypeError("role disk resources are invalid")
            required_artifacts = [
                artifact_source_by_id[str(artifact_id)] for artifact_id in artifact_ids
            ]
            resolved_artifacts = [
                artifact_by_source.get(source) for source in required_artifacts
            ]
            if any(item is None for item in resolved_artifacts):
                blockers.append(
                    AdmissionReason(
                        "install.unknown_artifact_size",
                        "External artifact size metadata is incomplete.",
                    )
                )
            actual_artifact_bytes = sum(
                item.size_bytes for item in resolved_artifacts if item is not None
            )
            if image_bytes > int(disk["image_bytes"]):
                blockers.append(
                    AdmissionReason(
                        "install.image_size_underdeclared",
                        "Built image is larger than this role declares.",
                    )
                )
            if actual_artifact_bytes > int(disk["artifact_bytes"]):
                blockers.append(
                    AdmissionReason(
                        "install.artifact_size_underdeclared",
                        "External artifacts are larger than this role declares.",
                    )
                )
            snapshot = inventory_by_node.get(mapping_node.node_id)
            if snapshot is None:
                blockers.append(
                    AdmissionReason(
                        "install.inventory_missing",
                        "No authenticated inventory is available for this GPU node.",
                    )
                )
            if snapshot is not None and snapshot.stale:
                blockers.append(
                    AdmissionReason(
                        "install.stale_inventory",
                        "GPU node disk inventory is stale; refresh it before installing.",
                    )
                )
            if snapshot is not None and snapshot.artifact_store_read_only:
                blockers.append(
                    AdmissionReason(
                        "install.artifact_store_read_only",
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
                            NodeArtifact.state == "verified",
                        )
                    )
                )
                reserved = int(
                    session.scalar(
                        select(
                            func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)
                        ).where(
                            ResourceReservation.node_id == mapping_node.node_id,
                            ResourceReservation.kind == "disk",
                            ResourceReservation.state == "active",
                        )
                    )
                    or 0
                )
            raw_image_digest = image_digest.removeprefix("sha256:")
            reused_image = (
                image_bytes
                if any(
                    item.kind == "image"
                    and item.digest == raw_image_digest
                    and item.size_bytes == image_bytes
                    for item in present
                )
                else 0
            )
            if reused_image == 0:
                blockers.append(
                    AdmissionReason(
                        "install.image_not_distributed",
                        "The exact built image must be imported on this GPU node before installation.",
                    )
                )
            reused_artifacts = sum(
                item.size_bytes
                for item in artifacts
                if item.source in required_artifacts
                and any(
                    present_item.source == item.source
                    and present_item.size_bytes == item.size_bytes
                    for present_item in present
                )
            )
            reused = reused_image + reused_artifacts
            required_download = max(0, actual_artifact_bytes - reused_artifacts)
            required = (
                required_download
                + int(disk["staging_bytes"])
                + int(disk["cache_bytes"])
                + int(disk["rollback_bytes"])
            )
            floor = max(self._disk_floor, int(disk["safety_margin_bytes"]))
            free = snapshot.disk_free_bytes if snapshot else None
            free_after = None if free is None else free - reserved - required
            if free_after is not None and free_after < floor:
                blockers.append(
                    AdmissionReason(
                        "install.insufficient_disk",
                        f"Installation would leave {free_after} bytes, below the required {floor}-byte floor.",
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
                    floor,
                    free_after,
                    tuple(blockers),
                    tuple(warnings),
                )
            )
        identity = {
            "schema_version": 1,
            "mapping_id": mapping_id,
            "mapping_generation": mapping_generation,
            "recipe_build_id": recipe_build_id,
            "image_digest": image_digest,
            "recipe_revision_id": revision.id,
            "recipe_content_sha256": recipe_digest,
            "nodes": [_node_document(item) for item in plans],
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
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
        )

    def accept_install(self, plan: InstallPlan, *, actor: str, now: datetime) -> str:
        with self._sessions.begin() as session:
            return self.accept_install_in_session(session, plan, actor=actor, now=now)

    def accept_install_in_session(
        self,
        session: Session,
        plan: InstallPlan,
        *,
        actor: str,
        now: datetime,
    ) -> str:
        mapping = session.get(ClusterMapping, plan.mapping_id, with_for_update=True)
        build = session.get(RecipeBuild, plan.recipe_build_id, with_for_update=True)
        if (
            mapping is None
            or mapping.generation != plan.mapping_generation
            or mapping.state != "ready"
            or build is None
            or build.state != "succeeded"
            or build.image_digest != plan.image_digest
        ):
            raise InstallPlanConflict("mapping or build changed while reserving")
        revision = session.get(
            LocalRecipeRevision, plan.recipe_revision_id, with_for_update=True
        )
        mapping_nodes = tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == plan.mapping_id)
                .order_by(ClusterMappingNode.rank)
                .with_for_update()
            )
        )
        node_ids = tuple(node.node_id for node in mapping_nodes)
        session.scalars(
            select(AgentNode).where(AgentNode.node_id.in_(node_ids)).with_for_update()
        ).all()
        session.scalars(
            select(NodeArtifact)
            .where(NodeArtifact.node_id.in_(node_ids))
            .with_for_update()
        ).all()
        session.scalars(
            select(ResourceReservation)
            .where(ResourceReservation.node_id.in_(node_ids))
            .with_for_update()
        ).all()
        session.scalars(
            select(NodeInventorySnapshot)
            .where(NodeInventorySnapshot.node_id.in_(node_ids))
            .with_for_update()
        ).all()
        fresh = self.plan_install(
            plan.mapping_id, plan.recipe_build_id, now=now, _session=session
        )
        if (
            not fresh.allowed
            or fresh.plan_digest != plan.plan_digest
            or fresh.mapping_generation != plan.mapping_generation
        ):
            raise InstallPlanConflict("install.plan_stale_or_blocked")
        if (
            revision is None
            or revision.lifecycle != "resolved"
            or revision.content_sha256 != plan.recipe_content_sha256
            or tuple((node.node_id, node.rank, node.role) for node in mapping_nodes)
            != tuple((node.node_id, node.rank, node.role) for node in plan.nodes)
        ):
            raise InstallPlanConflict("install.plan_stale")
        try:
            resolve_recipe_entities(session, revision.document)
        except RecipeRuntimeSpecError as error:
            raise InstallPlanConflict("install.dependencies_stale") from error
        installation = RecipeInstallation(
            recipe_revision_id=plan.recipe_revision_id,
            model_version_sha256=_primary_model_sha256(revision.document),
            mapping_id=plan.mapping_id,
            mapping_generation=plan.mapping_generation,
            recipe_build_id=plan.recipe_build_id,
            image_digest=plan.image_digest,
            plan_digest=plan.plan_digest,
            plan={
                "schema_version": 1,
                "mapping_id": plan.mapping_id,
                "mapping_generation": plan.mapping_generation,
                "recipe_build_id": plan.recipe_build_id,
                "image_digest": plan.image_digest,
                "recipe_revision_id": plan.recipe_revision_id,
                "recipe_content_sha256": plan.recipe_content_sha256,
                "plan_digest": plan.plan_digest,
                "nodes": [_node_document(item) for item in plan.nodes],
            },
            state="planned",
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        for node in sorted(plan.nodes, key=lambda item: item.node_id):
            if (
                session.scalar(
                    select(AgentNode)
                    .where(AgentNode.node_id == node.node_id)
                    .with_for_update()
                )
                is None
            ):
                raise InstallPlanConflict("installation node disappeared")
            active = int(
                session.scalar(
                    select(
                        func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)
                    ).where(
                        ResourceReservation.node_id == node.node_id,
                        ResourceReservation.kind == "disk",
                        ResourceReservation.state == "active",
                    )
                )
                or 0
            )
            if (
                node.free_bytes is None
                or node.free_bytes - active - node.required_bytes
                < node.disk_floor_bytes
            ):
                raise InstallPlanConflict("disk capacity changed while reserving")
        session.add(installation)
        session.flush()
        for node in plan.nodes:
            session.add(
                InstallationNode(
                    installation_id=installation.id,
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    state="planned",
                    required_bytes=node.required_bytes,
                    installed_bytes=0,
                    updated_at=now,
                )
            )
            session.add(
                ResourceReservation(
                    node_id=node.node_id,
                    kind="disk",
                    resource_key=plan.plan_digest,
                    amount_bytes=node.required_bytes,
                    owner_kind="installation",
                    owner_id=installation.id,
                    state="active",
                    plan_digest=plan.plan_digest,
                    created_at=now,
                )
            )
        return installation.id


def _primary_model_sha256(document: Mapping[str, object]) -> str:
    model = document.get("model")
    digest = model.get("content_sha256") if isinstance(model, Mapping) else None
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise InstallPlanConflict("install.model_identity_unavailable")
    return digest


def _node_document(node: InstallNodePlan) -> dict[str, object]:
    return {
        **asdict(node),
        "inventory_observed_at": (
            node.inventory_observed_at.isoformat()
            if node.inventory_observed_at
            else None
        ),
    }
