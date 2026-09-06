"""Role-aware disk admission for one mapping generation and exact OCI build."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .artifact_sizes import ArtifactSizeResolver
from .compiled_execution_plan import (
    CompiledExecutionPlanError,
    validate_compiled_launch_payload,
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
    # One strict Controller-issued launch document per mapped node.  It is
    # appended to preserve the positional shape used by older in-process
    # callers; production admission populates it before accepting an install.
    compiled_execution_plans: tuple[tuple[str, dict[str, object]], ...] = ()

    @property
    def compiled_plan_by_node(self) -> dict[str, dict[str, object]]:
        return {node_id: dict(value) for node_id, value in self.compiled_execution_plans}


class InstallPlanConflict(RuntimeError):
    pass


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
        statement = statement.with_for_update(of=CatalogDocumentRevision)
    return session.scalar(statement)


class InstallAdmissionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        sizes: ArtifactSizeResolver,
        inventory_max_age: int = 300,
        disk_floor_bytes: int = 10_000_000_000,
        operator_jurisdiction: str | None = None,
        compiled_plan_provider: Callable[..., Mapping[str, Mapping[str, object]]] | None = None,
    ) -> None:
        self._sessions = sessions
        # Canonical model bytes come from the receipt-bound compiled plan.
        # Keep the constructor slot while callers converge on that single
        # authority; this service never reads recipe-level artifact metadata.
        del sizes
        self._inventory = InventoryRepository(sessions)
        self._inventory_max_age = inventory_max_age
        self._disk_floor = disk_floor_bytes
        # Territorial declarations remain informational model metadata.  The
        # install plan deliberately does not derive an operator location or
        # enforce a territory denial.  Keep accepting the constructor keyword
        # while callers converge on the geography-free admission contract.
        del operator_jurisdiction
        self._compiled_plan_provider = compiled_plan_provider

    def plan_install(
        self,
        mapping_id: str,
        recipe_build_id: str,
        *,
        now: datetime,
        _session: Session | None = None,
        compiled_execution_plans: Mapping[str, Mapping[str, object]] | None = None,
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
            revision = _active_recipe_revision(session, mapping.recipe_revision_id)
            if (
                revision is None
                or revision.state != "active"
                or revision.content_digest is None
            ):
                raise ValueError("recipe revision is not resolved")
            canonical_build_revision = session.get(
                CatalogDocumentRevision, build.recipe_revision_id
            )
            if (
                build.state != "succeeded"
                or build.image_digest is None
                or build.image_bytes is None
                or canonical_build_revision is None
                or canonical_build_revision.kind != "recipe"
                or canonical_build_revision.state != "active"
                or canonical_build_revision.content_digest != revision.content_digest
            ):
                raise ValueError("successful recipe build does not match the mapping")
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
            models = resolved_entities.get("models")
            model_document = (
                getattr(models[0], "document", None)
                if isinstance(models, Sequence) and models
                else None
            )
            if not isinstance(model_document, Mapping):
                raise ValueError(  # noqa: TRY004
                    "exact model license authority is unavailable"
                )
            compiled_plan_error: str | None = None
            if compiled_execution_plans is None and self._compiled_plan_provider is not None:
                try:
                    compiled_execution_plans = self._compiled_plan_provider(
                        session=session,
                        revision=revision,
                        build=build,
                        mapping=mapping,
                        mapping_nodes=mapping_nodes,
                        parameters=mapping.parameters,
                        resolved_entities=resolved_entities,
                    )
                except Exception as error:  # provider errors become typed admission evidence
                    compiled_plan_error = str(error)[:512]
                    compiled_execution_plans = {}
            if compiled_execution_plans is not None:
                try:
                    if not isinstance(compiled_execution_plans, Mapping):
                        raise TypeError("compiled execution plan mapping is invalid")
                    compiled_execution_plans = {
                        str(node_id): validate_compiled_launch_payload(value)
                        for node_id, value in compiled_execution_plans.items()
                    }
                except (CompiledExecutionPlanError, TypeError, ValueError) as error:
                    compiled_plan_error = str(error)[:512]
                    compiled_execution_plans = {}
            compiled_plan_by_node = {
                str(node_id): dict(value)
                for node_id, value in (compiled_execution_plans or {}).items()
                if isinstance(node_id, str) and isinstance(value, Mapping)
            }
            legal_admission = territorial_admission(
                model_document,
                None,
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
            recipe_digest = revision.content_digest
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
            if (
                (self._compiled_plan_provider is not None or compiled_plan_error is not None)
                and mapping_node.node_id not in compiled_plan_by_node
            ):
                detail = "Controller-issued compiled execution plan is unavailable."
                if compiled_plan_error:
                    detail = f"{detail} {compiled_plan_error}"
                blockers.append(AdmissionReason("install.compiled_plan_unavailable", detail))
            role = role_by_name.get(mapping_node.role)
            if role is None or not isinstance(role.get("resources"), dict):
                raise TypeError("mapping role is absent from recipe topology")
            resources = role["resources"]
            disk = resources.get("disk")
            if not isinstance(disk, dict):
                raise TypeError("role disk resources are invalid")
            compiled_plan = compiled_plan_by_node.get(mapping_node.node_id)
            compiled_artifacts = (
                compiled_plan.get("artifacts")
                if isinstance(compiled_plan, Mapping)
                else None
            )
            if not isinstance(compiled_artifacts, Sequence) or isinstance(
                compiled_artifacts, (str, bytes)
            ):
                blockers.append(
                    AdmissionReason(
                        "install.compiled_plan_unavailable",
                        "Controller-issued compiled model receipts are unavailable.",
                    )
                )
                compiled_artifacts = ()
            artifact_sizes: dict[str, int] = {}
            for artifact in compiled_artifacts:
                if not isinstance(artifact, Mapping):
                    continue
                digest = artifact.get("sha256")
                size = artifact.get("size_bytes")
                if (
                    isinstance(digest, str)
                    and type(size) is int
                    and size >= 0
                ):
                    previous = artifact_sizes.setdefault(digest, size)
                    if previous != size:
                        blockers.append(
                            AdmissionReason(
                                "install.compiled_plan_unavailable",
                                "Compiled model receipts disagree about an object size.",
                            )
                        )
            actual_artifact_bytes = sum(artifact_sizes.values())
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
                # Run/Switch may deliberately compile and persist the exact
                # schema-2 launch plan before its ordered target-copy phase.
                # The high-level operation owns the missing-image transfer;
                # admission records the gap as evidence instead of rejecting
                # a valid cold install before the Controller can distribute it.
                warnings.append(
                    AdmissionReason(
                        "install.image_distribution_pending",
                        "The exact built image will be imported by the ordered Run/Switch target-copy phase.",
                    )
                )
            reused_artifacts = sum(
                size
                for digest, size in artifact_sizes.items()
                if any(
                    present_item.kind == "model"
                    and present_item.digest == digest
                    and present_item.size_bytes == size
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
            "compiled_execution_plans": {
                node_id: compiled_plan_by_node[node_id]
                for node_id in sorted(compiled_plan_by_node)
            },
            "nodes": [_node_digest_document(item) for item in plans],
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
            tuple(
                (node_id, compiled_plan_by_node[node_id])
                for node_id in sorted(compiled_plan_by_node)
            ),
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
        revision = _active_recipe_revision(
            session, plan.recipe_revision_id, for_update=True
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
            plan.mapping_id,
            plan.recipe_build_id,
            now=now,
            _session=session,
            compiled_execution_plans=plan.compiled_plan_by_node,
        )
        if (
            not fresh.allowed
            or fresh.plan_digest != plan.plan_digest
            or fresh.mapping_generation != plan.mapping_generation
        ):
            raise InstallPlanConflict("install.plan_stale_or_blocked")
        if (
            revision is None
            or revision.state != "active"
            or revision.content_digest != plan.recipe_content_sha256
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
                "compiled_execution_plans": plan.compiled_plan_by_node,
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
    selections = document.get("models")
    model_selection = (
        selections[0]
        if isinstance(selections, Sequence)
        and not isinstance(selections, (str, bytes))
        and selections
        else None
    )
    model = (
        model_selection.get("model")
        if isinstance(model_selection, Mapping)
        else None
    )
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


def _node_digest_document(node: InstallNodePlan) -> dict[str, object]:
    """Bind install work and safety envelopes, not transient observations."""

    return {
        "node_id": node.node_id,
        "rank": node.rank,
        "role": node.role,
        "reused_bytes": node.reused_bytes,
        "required_download_bytes": node.required_download_bytes,
        "required_bytes": node.required_bytes,
        "disk_floor_bytes": node.disk_floor_bytes,
    }
