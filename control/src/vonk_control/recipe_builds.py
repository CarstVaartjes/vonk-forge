"""Durable source-build planning and exact OCI result recording."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .inventory_repository import InventoryRepository
from .models import (
    AgentNode,
    ClusterMapping,
    ClusterMappingNode,
    LocalRecipeRevision,
    NodeArtifact,
    RecipeBuild,
    RecipeSourceBundle,
    ResourceReservation,
)
from .source_bundles import SourceBundleError, SourceBundleStore
from .source_policy import (
    SourcePolicyError,
    SourcePolicyReport,
    enforce_build_source_policy,
    inspect_build_source_policy,
)

_OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RecipeBuildError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class RecipeBuildPlan:
    build_id: str
    recipe_revision_id: str
    recipe_content_sha256: str
    builder_node_id: str
    source_bundle_sha256: str
    build_input_sha256: str
    agent_payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class CompletedRecipeBuild:
    build_id: str
    image_digest: str
    oci_layout_sha256: str
    image_bytes: int


@dataclass(frozen=True, slots=True)
class ImageDistributionPlan:
    build_id: str
    mapping_id: str
    mapping_generation: int
    image_digest: str
    targets: tuple[tuple[str, dict[str, object]], ...]


class RecipeBuildService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        bundles: SourceBundleStore,
        inventory_max_age: int = 300,
    ) -> None:
        self._sessions = sessions
        self._bundles = bundles
        self._inventory = InventoryRepository(sessions)
        self._inventory_max_age = inventory_max_age

    def check_source(self, recipe_revision_id: str) -> SourcePolicyReport:
        with self._sessions() as session:
            revision = session.get(LocalRecipeRevision, recipe_revision_id)
            if revision is None:
                raise KeyError(recipe_revision_id)
            if revision.lifecycle != "resolved":
                raise RecipeBuildError(
                    "build.recipe_unresolved", "only a resolved recipe can be checked"
                )
            document = copy.deepcopy(revision.document)
            build = document.get("build")
            context = build.get("context") if isinstance(build, dict) else None
            source_sha256 = context.get("sha256") if isinstance(context, dict) else None
            _ensure_supported_build_network(build)
            if (
                not isinstance(source_sha256, str)
                or session.get(RecipeSourceBundle, source_sha256) is None
            ):
                raise RecipeBuildError(
                    "build.source_unavailable", "verified source bundle is unavailable"
                )
        try:
            bundle = self._bundles.get(source_sha256)
        except SourceBundleError as error:
            raise RecipeBuildError(error.code, str(error)) from error
        return inspect_build_source_policy(document, bundle)

    def plan(
        self, recipe_revision_id: str, builder_node_id: str, *, now: datetime
    ) -> RecipeBuildPlan:
        with self._sessions() as session:
            revision = session.get(LocalRecipeRevision, recipe_revision_id)
            if revision is None:
                raise KeyError(recipe_revision_id)
            if revision.lifecycle != "resolved" or revision.content_sha256 is None:
                raise RecipeBuildError(
                    "build.recipe_unresolved", "only a resolved recipe can be built"
                )
            node = session.get(AgentNode, builder_node_id)
            if node is None:
                raise RecipeBuildError("build.node_unknown", "builder GPU node is unknown")
            _validate_builder(node)
            document = copy.deepcopy(revision.document)
            build = document.get("build")
            context = build.get("context") if isinstance(build, dict) else None
            source_sha256 = context.get("sha256") if isinstance(context, dict) else None
            _ensure_supported_build_network(build)
            if not isinstance(source_sha256, str):
                raise RecipeBuildError(
                    "build.source_invalid", "recipe source bundle identity is invalid"
                )
            stored = session.get(RecipeSourceBundle, source_sha256)
            if stored is None:
                raise RecipeBuildError(
                    "build.source_unavailable", "verified source bundle is unavailable"
                )
        try:
            bundle = self._bundles.get(source_sha256)
            policy = enforce_build_source_policy(document, bundle)
        except SourceBundleError as error:
            raise RecipeBuildError(error.code, str(error)) from error
        except SourcePolicyError as error:
            finding = error.report.findings[0]
            raise RecipeBuildError(finding.code, finding.detail) from error
        try:
            snapshot = self._inventory.latest(
                builder_node_id, now=now, maximum_age=self._inventory_max_age
            )
        except KeyError as error:
            raise RecipeBuildError(
                "build.inventory_missing", "fresh builder inventory is unavailable"
            ) from error
        if snapshot.stale:
            raise RecipeBuildError(
                "build.inventory_stale", "builder inventory is stale"
            )
        if "recipe.build.v1" not in snapshot.capabilities:
            raise RecipeBuildError(
                "build.capability_missing",
                "builder does not support typed recipe builds",
            )
        assert isinstance(build, dict)
        resources = build.get("resources")
        if not isinstance(resources, dict):
            raise RecipeBuildError(
                "build.resources_invalid", "build resource envelope is invalid"
            )
        temporary_bytes = int(resources["temporary_bytes"])
        memory_bytes = int(resources["memory_bytes"])
        with self._sessions() as session:
            disk_reserved = _reserved(session, builder_node_id, "disk")
            memory_reserved = _reserved(session, builder_node_id, "host-memory")
        # The rootless builder retains its source/staging data while exporting
        # the OCI layout.  Reserve the concurrent peak, not just the inputs.
        # The OCI export is the build output. Bind it to the largest declared
        # per-node image envelope across the selected recipe's profiles;
        # CUDA/vLLM images routinely exceed 64 MiB.
        output_bytes = _declared_image_bytes(document)
        required_disk = temporary_bytes + len(bundle.archive) + output_bytes
        if snapshot.disk_free_bytes - disk_reserved < required_disk:
            raise RecipeBuildError(
                "build.insufficient_disk", "builder lacks temporary disk capacity"
            )
        if snapshot.host_memory_free_bytes - memory_reserved < memory_bytes:
            raise RecipeBuildError(
                "build.insufficient_memory", "builder lacks build memory capacity"
            )
        build_identity = {
            "schema_version": 1,
            "recipe_revision_id": revision.id,
            "recipe_content_sha256": revision.content_sha256,
            "source_bundle_sha256": source_sha256,
            "build": build,
        }
        build_input_sha256 = _digest(build_identity)
        proposed_build_id = str(uuid.uuid4())
        limits = {
            "cpu_cores": 8,
            "memory_bytes": memory_bytes,
            "temporary_bytes": temporary_bytes,
            "processes": 4096,
            "timeout_seconds": int(resources["timeout_seconds"]),
            "output_bytes": output_bytes,
            "gpu": 0,
            "privileged": False,
            "host_mounts": False,
            "container_socket": False,
        }
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "recipe.build.v1",
            "build_id": proposed_build_id,
            "recipe_revision_id": revision.id,
            "recipe_content_sha256": revision.content_sha256,
            "source_bundle_sha256": source_sha256,
            "source_bundle_bytes": len(bundle.archive),
            "build_input_sha256": build_input_sha256,
            "dockerfile": build["dockerfile"],
            "platform": build["platform"],
            "arguments": copy.deepcopy(build["arguments"]),
            "network": copy.deepcopy(build["network"]),
            "limits": limits,
        }
        policy_document = {
            "passed": policy.passed,
            "source_bundle_sha256": policy.source_bundle_sha256,
            "dockerfile": policy.dockerfile,
            "findings": [asdict(item) for item in policy.findings],
        }
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(RecipeBuild).where(
                    RecipeBuild.recipe_revision_id == revision.id,
                    RecipeBuild.builder_node_id == builder_node_id,
                    RecipeBuild.build_input_sha256 == build_input_sha256,
                )
            )
            if existing is None:
                existing = RecipeBuild(
                    id=proposed_build_id,
                    recipe_revision_id=revision.id,
                    builder_node_id=builder_node_id,
                    source_bundle_sha256=source_sha256,
                    build_input_sha256=build_input_sha256,
                    state="planned",
                    policy_report=policy_document,
                    plan=copy.deepcopy(payload),
                    created_at=now,
                    updated_at=now,
                )
                session.add(existing)
                session.flush()
            else:
                payload = copy.deepcopy(existing.plan)
            build_id = existing.id
        return RecipeBuildPlan(
            build_id=build_id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_sha256,
            builder_node_id=builder_node_id,
            source_bundle_sha256=source_sha256,
            build_input_sha256=build_input_sha256,
            agent_payload=payload,
        )

    def record_success(
        self,
        build_id: str,
        *,
        build_input_sha256: str,
        image_digest: str,
        oci_layout_sha256: str,
        image_bytes: int,
        now: datetime,
    ) -> CompletedRecipeBuild:
        if (
            _SHA256.fullmatch(build_input_sha256) is None
            or _OCI_DIGEST.fullmatch(image_digest) is None
            or _SHA256.fullmatch(oci_layout_sha256) is None
            or not isinstance(image_bytes, int)
            or isinstance(image_bytes, bool)
            or image_bytes < 1
        ):
            raise RecipeBuildError(
                "build.evidence_invalid", "build result evidence is invalid"
            )
        with self._sessions.begin() as session:
            build = session.get(RecipeBuild, build_id, with_for_update=True)
            if build is None:
                raise KeyError(build_id)
            if build.build_input_sha256 != build_input_sha256:
                raise RecipeBuildError(
                    "build.input_mismatch", "build result does not match its inputs"
                )
            if build.state == "succeeded":
                if (
                    build.image_digest != image_digest
                    or build.oci_layout_sha256 != oci_layout_sha256
                    or build.image_bytes != image_bytes
                ):
                    raise RecipeBuildError(
                        "build.result_conflict", "build already has different evidence"
                    )
            elif build.state not in {"planned", "building"}:
                raise RecipeBuildError(
                    "build.state", "failed build cannot accept success evidence"
                )
            else:
                build.state = "succeeded"
                build.image_digest = image_digest
                build.oci_layout_sha256 = oci_layout_sha256
                build.image_bytes = image_bytes
                build.error = None
                build.updated_at = now
        return CompletedRecipeBuild(
            build_id, image_digest, oci_layout_sha256, image_bytes
        )

    def reserve_in_session(
        self, session: Session, plan: RecipeBuildPlan, *, now: datetime
    ) -> None:
        try:
            snapshot = self._inventory.latest(
                plan.builder_node_id, now=now, maximum_age=self._inventory_max_age
            )
        except KeyError as error:
            raise RecipeBuildError(
                "build.inventory_missing", "fresh builder inventory is unavailable"
            ) from error
        node = session.get(AgentNode, plan.builder_node_id, with_for_update=True)
        if node is None:
            raise RecipeBuildError("build.node_unknown", "builder GPU node is unknown")
        _validate_builder(node)
        if snapshot.stale:
            raise RecipeBuildError(
                "build.inventory_stale", "builder inventory is stale"
            )
        limits = plan.agent_payload.get("limits")
        source_bytes = plan.agent_payload.get("source_bundle_bytes")
        if not isinstance(limits, dict) or not isinstance(source_bytes, int):
            raise RecipeBuildError("build.plan_invalid", "build plan is invalid")
        temporary_bytes = limits.get("temporary_bytes")
        memory_bytes = limits.get("memory_bytes")
        output_bytes = limits.get("output_bytes")
        if (
            not isinstance(temporary_bytes, int)
            or not isinstance(memory_bytes, int)
            or not isinstance(output_bytes, int)
        ):
            raise RecipeBuildError("build.plan_invalid", "build plan is invalid")
        # Staging and the OCI export coexist until the build is committed.
        disk_bytes = temporary_bytes + source_bytes + output_bytes
        if (
            snapshot.disk_free_bytes - _reserved(session, plan.builder_node_id, "disk")
            < disk_bytes
        ):
            raise RecipeBuildError(
                "build.insufficient_disk", "builder disk capacity changed"
            )
        if (
            snapshot.host_memory_free_bytes
            - _reserved(session, plan.builder_node_id, "host-memory")
            < memory_bytes
        ):
            raise RecipeBuildError(
                "build.insufficient_memory", "builder memory capacity changed"
            )
        session.add_all(
            (
                ResourceReservation(
                    node_id=plan.builder_node_id,
                    kind="disk",
                    resource_key=plan.build_input_sha256,
                    amount_bytes=disk_bytes,
                    owner_kind="recipe-build",
                    owner_id=plan.build_id,
                    state="active",
                    plan_digest=plan.build_input_sha256,
                    created_at=now,
                ),
                ResourceReservation(
                    node_id=plan.builder_node_id,
                    kind="host-memory",
                    resource_key=plan.build_input_sha256,
                    amount_bytes=memory_bytes,
                    owner_kind="recipe-build",
                    owner_id=plan.build_id,
                    state="active",
                    plan_digest=plan.build_input_sha256,
                    created_at=now,
                ),
            )
        )

    def plan_distribution(
        self, build_id: str, mapping_id: str, *, generation: int
    ) -> ImageDistributionPlan:
        with self._sessions() as session:
            build = session.get(RecipeBuild, build_id)
            mapping = session.get(ClusterMapping, mapping_id)
            if build is None:
                raise KeyError(build_id)
            if mapping is None:
                raise KeyError(mapping_id)
            if (
                build.state != "succeeded"
                or build.image_digest is None
                or build.oci_layout_sha256 is None
                or build.image_bytes is None
            ):
                raise RecipeBuildError(
                    "build.result_unavailable", "successful OCI build is unavailable"
                )
            if (
                mapping.state != "ready"
                or mapping.generation != generation
                or mapping.recipe_revision_id != build.recipe_revision_id
            ):
                raise RecipeBuildError(
                    "build.mapping_mismatch",
                    "mapping generation does not match the build",
                )
            nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id == mapping_id)
                    .order_by(ClusterMappingNode.rank)
                )
            )
            raw_digest = build.image_digest.removeprefix("sha256:")
            present = set(
                session.scalars(
                    select(NodeArtifact.node_id).where(
                        NodeArtifact.node_id.in_([item.node_id for item in nodes]),
                        NodeArtifact.kind == "image",
                        NodeArtifact.digest == raw_digest,
                        NodeArtifact.size_bytes == build.image_bytes,
                        NodeArtifact.state == "verified",
                    )
                )
            )
            targets: list[tuple[str, dict[str, object]]] = []
            for item in nodes:
                if item.node_id in present:
                    continue
                node = session.get(AgentNode, item.node_id)
                if (
                    node is None
                    or node.state != "active"
                    or "recipe.image.import.v1" not in node.capabilities
                ):
                    raise RecipeBuildError(
                        "build.import_capability_missing",
                        "a mapped GPU node cannot import the exact OCI result",
                    )
                targets.append(
                    (
                        item.node_id,
                        {
                            "schema_version": 1,
                            "kind": "recipe.image.import.v1",
                            "build_id": build.id,
                            "mapping_id": mapping.id,
                            "mapping_generation": mapping.generation,
                            "source_node_id": build.builder_node_id,
                            "image_digest": build.image_digest,
                            "oci_layout_sha256": build.oci_layout_sha256,
                            "image_bytes": build.image_bytes,
                        },
                    )
                )
        return ImageDistributionPlan(
            build.id,
            mapping.id,
            mapping.generation,
            build.image_digest,
            tuple(targets),
        )


def _validate_builder(node: AgentNode) -> None:
    if (
        node.state != "active"
        or node.revoked_at is not None
        or node.architecture != "linux-arm64"
        or node.agent_implementation != "rust"
        or node.migration_state != "complete"
        or "recipe.build.v1" not in node.capabilities
    ):
        raise RecipeBuildError(
            "build.node_incompatible", "builder GPU node is inactive or incompatible"
        )


def _ensure_supported_build_network(build: object) -> None:
    if not isinstance(build, dict):
        return
    network = build.get("network")
    if isinstance(network, dict) and network.get("mode") == "public":
        raise RecipeBuildError(
            "build.network_unsupported",
            "public build networking is unavailable until a hostname-aware egress boundary is installed",
        )


def _declared_image_bytes(document: dict[str, object]) -> int:
    profiles = document.get("deployment_profiles")
    values: list[int] = []
    if isinstance(profiles, list):
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            roles = profile.get("roles")
            if not isinstance(roles, list):
                continue
            for role in roles:
                if not isinstance(role, dict):
                    continue
                resources = role.get("resources")
                disk = resources.get("disk") if isinstance(resources, dict) else None
                image_bytes = disk.get("image_bytes") if isinstance(disk, dict) else None
                if isinstance(image_bytes, int) and not isinstance(image_bytes, bool):
                    values.append(image_bytes)
    if not values or min(values) < 1 or max(values) > 16 * 1024**4:
        raise RecipeBuildError(
            "build.image_size_invalid",
            "recipe profiles must declare a positive per-node image size",
        )
    return max(values)


def _reserved(session: Session, node_id: str, kind: str) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)).where(
                ResourceReservation.node_id == node_id,
                ResourceReservation.kind == kind,
                ResourceReservation.state == "active",
            )
        )
        or 0
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
