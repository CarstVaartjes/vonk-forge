"""Recipe builds: planning concerns."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING

from vonk_agent_protocol import (
    RecipeBuildCode,
    SecurityRefusalError,
    UnknownOutcomeError,
    WaitReason,
)

from ..catalog_revision_contract import PrebuiltImage, RecipeRevisionProjection
from ..categorized_errors import MissingRecord
from ..disk_reservations import outstanding_disk_reservation_bytes
from ..models import AgentNode, CatalogDocumentRevision
from ..prebuilt_images import (
    PREBUILT_KEY_MISMATCH,
    PREBUILT_NOT_PINNED,
    PREBUILT_RECENT_PULL_FAILURE,
    PREBUILT_USED,
    PrebuiltDecision,
    executable_build_key,
    prebuilt_failed,
)
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    build_plan_document,
    build_policy_document,
)
from ..runtime_adapters import RuntimeAdapter
from ..source_policy import (
    SourcePolicyError,
    dockerfile_base_images,
    enforce_build_source_policy,
)
from ..storage_demands import spark_scope
from .common import (
    _BUILD_OBSERVATION_DELAYS,
    _LOGGER,
    BUILD_ARTIFACT_FORMAT,
    RecipeBuildError,
    RecipeBuildInvalid,
    RecipeBuildPlan,
    RecipeBuildRefused,
    RecipeBuildResolution,
    RecipeBuildUnknown,
    RecipeSourcePolicyError,
    _available_build_memory,
    _build_disk_envelope,
    _build_disk_reserve,
    _canonical_build,
    _canonical_build_resources,
    _canonical_recipe_document,
    _declared_image_bytes,
    _digest,
    _public_build_network,
    _read_recipe_projection,
    _resolved_adapter,
    _source_bundle_handle,
    _source_policy_document,
    _validate_builder,
    derive_build_input_identity,
)

if TYPE_CHECKING:
    from .service import RecipeBuildService


def _admit_spark_build(
    self: RecipeBuildService,
    builder_node_id: str,
    *,
    now: datetime,
    public_network: bool,
    memory_bytes: int,
    disk_envelope: int,
) -> None:
    """Admit a Spark build against fresh inventory, disk and memory."""
    try:
        snapshot = self._inventory.latest(
            builder_node_id, now=now, maximum_age=self._inventory_max_age
        )
    except KeyError as error:
        raise RecipeBuildUnknown(
            RecipeBuildCode.INVENTORY_MISSING,
            "fresh builder inventory is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error
    if snapshot.stale:
        raise RecipeBuildUnknown(
            RecipeBuildCode.INVENTORY_STALE,
            "builder inventory is stale",
            reason=WaitReason.STALE_PLAN,
        )
    if "recipe.build.v1" not in snapshot.capabilities:
        raise RecipeBuildUnknown(
            RecipeBuildCode.CAPABILITY_MISSING,
            "builder does not support typed recipe builds",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    if public_network and "recipe.build.egress-proxy.v1" not in snapshot.capabilities:
        raise RecipeBuildUnknown(
            RecipeBuildCode.NETWORK_CAPABILITY_MISSING,
            "fresh builder inventory does not prove the hostname-aware build egress boundary",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    with self._sessions() as session:
        disk_reserved = outstanding_disk_reservation_bytes(
            session, builder_node_id, inventory_observed_at=snapshot.observed_at
        )
        memory_available = _available_build_memory(session, snapshot)
    # Preserve a separate host reserve so an admitted build cannot crowd
    # out the Spark itself.
    disk_needed = disk_envelope + _build_disk_reserve(snapshot.disk_total_bytes)
    if snapshot.disk_free_bytes - disk_reserved < disk_needed:
        if self._storage_demands is not None:
            self._storage_demands.request(
                spark_scope(builder_node_id),
                disk_reserved + disk_needed,
                source="build",
                subject=builder_node_id,
                reason=RecipeBuildCode.INSUFFICIENT_DISK,
            )
        raise RecipeBuildUnknown(
            RecipeBuildCode.INSUFFICIENT_DISK,
            "builder lacks temporary disk capacity",
        )
    if memory_available < memory_bytes:
        raise RecipeBuildUnknown(
            RecipeBuildCode.INSUFFICIENT_MEMORY,
            "builder lacks build memory capacity",
        )


def _usable_prebuilt(
    self: RecipeBuildService,
    recipe_revision_id: str,
    projected: RecipeRevisionProjection,
    *,
    build: Mapping[str, object],
    source_sha256: str,
    base_images: Sequence[Mapping[str, object]],
    adapter: RuntimeAdapter,
    now: datetime,
) -> tuple[PrebuiltImage | None, PrebuiltDecision]:
    """The catalog's prebuilt image when it was built from these exact inputs.

    Otherwise ``None``. Either way the decision names why: the image is
    used, the catalog pins none, it was built from other inputs (for
    example under a different platform adapter), or a pull failed
    recently. The decision is stored with the build and logged, so a
    Spark build is never planned without saying why.
    """
    image = projected.prebuilt_image
    if image is None:
        decision = PrebuiltDecision(
            PREBUILT_NOT_PINNED,
            "the signed catalog pins no prebuilt image for this revision",
        )
        _LOGGER.info("recipe revision %s: %s", recipe_revision_id, decision)
        return None, decision
    key = executable_build_key(
        derive_build_input_identity(
            build,
            source_bundle_sha256=source_sha256,
            builder_binary_digest=None,
            base_images=base_images,
            runtime_adapter=adapter.document(),
        )
    )
    if key != image.build_key:
        decision = PrebuiltDecision(
            PREBUILT_KEY_MISMATCH,
            f"prebuilt image {image.reference} was built from other inputs "
            f"(catalog key {image.build_key}, Controller key {key})",
        )
        _LOGGER.warning(
            "recipe revision %s: %s; building on a Spark instead",
            recipe_revision_id,
            decision,
        )
        return None, decision
    with self._sessions() as session:
        failed = prebuilt_failed(session, recipe_revision_id, image, now=now)
    if failed is not None:
        decision = PrebuiltDecision(PREBUILT_RECENT_PULL_FAILURE, failed)
        _LOGGER.warning(
            "recipe revision %s: %s; building on a Spark instead",
            recipe_revision_id,
            decision,
        )
        return None, decision
    decision = PrebuiltDecision(
        PREBUILT_USED, f"pulling the catalog's prebuilt image {image.reference}"
    )
    _LOGGER.info("recipe revision %s: %s", recipe_revision_id, decision)
    return image, decision


def prepare_plan(
    self: RecipeBuildService,
    recipe_revision_id: str,
    builder_node_id: str,
    *,
    now: datetime,
    resolution: RecipeBuildResolution | None = None,
) -> RecipeBuildPlan:
    """Re-observe with the bounded, resource-free policy in ``check_source``."""
    last_error: UnknownOutcomeError | None = None
    for delay in _BUILD_OBSERVATION_DELAYS:
        if delay:
            self._sleep(delay)
        try:
            return self._prepare_plan_once(
                recipe_revision_id, builder_node_id, now=now, resolution=resolution
            )
        except UnknownOutcomeError as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _prepare_plan_once(
    self: RecipeBuildService,
    recipe_revision_id: str,
    builder_node_id: str,
    *,
    now: datetime,
    resolution: RecipeBuildResolution | None = None,
) -> RecipeBuildPlan:
    with self._sessions() as session:
        revision = session.get(CatalogDocumentRevision, recipe_revision_id)
        if revision is None:
            raise MissingRecord(recipe_revision_id)
        if revision.kind != "recipe" or revision.state != "active":
            raise RecipeBuildInvalid(
                RecipeBuildCode.RECIPE_UNRESOLVED,
                "only a resolved recipe can be built",
            )
        projected = _read_recipe_projection(revision)
        node = session.get(AgentNode, builder_node_id)
        if node is None:
            raise RecipeBuildUnknown(
                RecipeBuildCode.NODE_UNKNOWN,
                "builder GPU node is unknown",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        _validate_builder(node)
        document = _canonical_recipe_document(revision.document)
        build = _canonical_build(document, projected)
        adapter = _resolved_adapter(projected)
        source_sha256 = _source_bundle_handle(projected)
        public_network = _public_build_network(build)
        # Claim capabilities describe operations; the probed egress boundary
        # is checked against fresh host inventory below.
        assert node.binary_digest is not None
        builder_binary_digest = node.binary_digest
    bundle = self._verified_bundle(projected, build, source_sha256)
    try:
        policy = enforce_build_source_policy(
            _source_policy_document(document, build, source_sha256), bundle
        )
    except SourcePolicyError as error:
        raise RecipeSourcePolicyError(error.report) from error
    dockerfile_path = build.get("dockerfile") if isinstance(build, dict) else None
    dockerfile_payload = (
        bundle.files.get(dockerfile_path) if isinstance(dockerfile_path, str) else None
    )
    if dockerfile_payload is None:
        raise RecipeBuildInvalid(
            RecipeBuildCode.SOURCE_INVALID,
            "recipe Dockerfile authority is unavailable",
        )
    base_images = list(dockerfile_base_images(dockerfile_payload))
    prebuilt, prebuilt_decision = self._usable_prebuilt(
        revision.id,
        projected,
        build=build,
        source_sha256=source_sha256,
        base_images=base_images,
        adapter=adapter,
        now=now,
    )
    if prebuilt is not None:
        # The Controller pulls this image; the Spark builds nothing, so
        # no Spark inventory, disk or memory is admitted for it. The
        # builder stays the nominal owner of the row, and the pinned
        # manifest digest stands in for the builder binary identity.
        builder_binary_digest = prebuilt.digest.removeprefix("sha256:")
    resources, security = _canonical_build_resources(projected)
    temporary_bytes = resources.temporary_bytes
    memory_bytes = resources.memory_bytes
    cpu_cores = resources.cpu_cores
    processes = resources.processes
    capabilities = list(security.capabilities)
    output_bytes = _declared_image_bytes(document)
    base_image_storage_bytes = resources.download_bytes if base_images else 0
    if prebuilt is None:
        try:
            self._admit_spark_build(
                builder_node_id,
                now=now,
                public_network=public_network,
                memory_bytes=memory_bytes,
                # The rootless builder retains inputs while exporting the
                # image. Treat recipe storage as a generous peak envelope,
                # not an exact quota over Podman's implementation-specific
                # graph.
                disk_envelope=_build_disk_envelope(
                    base_image_bytes=base_image_storage_bytes,
                    temporary_bytes=temporary_bytes,
                    source_bytes=len(bundle.archive),
                    output_bytes=output_bytes,
                ),
            )
        except RecipeBuildError as error:
            error.prebuilt_unused = (
                None if prebuilt_decision.used else prebuilt_decision
            )
            raise
    model_inputs = projected.build_model_artifacts
    build_identity = derive_build_input_identity(
        build,
        source_bundle_sha256=source_sha256,
        builder_binary_digest=builder_binary_digest,
        artifact_format=BUILD_ARTIFACT_FORMAT,
        base_images=base_images,
        effective_settings=document["settings"],
        model_artifacts=(
            model_inputs
            if isinstance(model_inputs, Sequence)
            and not isinstance(model_inputs, (str, bytes))
            else None
        ),
        runtime_adapter=adapter.document(),
    )
    build_input_sha256 = _digest(build_identity)
    if resolution is not None:
        intent = copy.deepcopy(build_identity)
        intent.pop("builder_binary_digest", None)
        if (
            resolution.recipe_revision_id != revision.id
            or resolution.recipe_content_sha256 != revision.content_digest
            or resolution.source_bundle_sha256 != source_sha256
            or resolution.input_intent_sha256 != _digest(intent)
        ):
            raise RecipeBuildRefused(
                RecipeBuildCode.RESOLUTION_STALE,
                "immutable build resolution no longer matches the recipe",
            )
    proposed_build_id = str(uuid.uuid4())
    limits = {
        "cpu_cores": cpu_cores,
        "memory_bytes": memory_bytes,
        "temporary_bytes": temporary_bytes,
        "processes": processes,
        "timeout_seconds": resources.timeout_seconds,
        "output_bytes": output_bytes,
    }
    payload: dict[str, object] = {
        "adapter": adapter.to_wire().model_dump(mode="json"),
        "build_id": proposed_build_id,
        "recipe_revision_id": revision.id,
        "recipe_content_sha256": revision.content_digest,
        "source_bundle_sha256": source_sha256,
        "source_bundle_bytes": len(bundle.archive),
        "build_input_sha256": build_input_sha256,
        "base_images": copy.deepcopy(base_images),
        "base_image_storage_bytes": base_image_storage_bytes,
        "capabilities": capabilities,
        "dockerfile": build["dockerfile"],
        "network": copy.deepcopy(build["network"]),
        "options": (
            projected.build_options.model_dump(mode="json")
            if projected.build_options is not None
            else {}
        ),
        "limits": limits,
    }
    policy_document = {
        "passed": policy.passed,
        "source_bundle_sha256": policy.source_bundle_sha256,
        "dockerfile": policy.dockerfile,
        "findings": [asdict(item) for item in policy.findings],
        "builder_binary_digest": builder_binary_digest,
        "artifact_format": BUILD_ARTIFACT_FORMAT,
    }
    policy_document["prebuilt_decision"] = {
        "code": prebuilt_decision.code,
        "detail": prebuilt_decision.detail,
    }
    if prebuilt is not None:
        policy_document["prebuilt_image"] = prebuilt.reference
    try:
        # Persist the canonical JSON-mode representation.  This is also
        # the representation handed to the agent build queue.
        payload = build_plan_document(payload)
        policy_document = build_policy_document(policy_document)
    except RecipeExecutionContractError as error:
        raise RecipeBuildUnknown(
            RecipeBuildCode.CONTRACT_INVALID,
            "source build envelope is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error
    return RecipeBuildPlan(
        build_id=proposed_build_id,
        recipe_revision_id=revision.id,
        recipe_content_sha256=revision.content_digest,
        builder_node_id=builder_node_id,
        source_bundle_sha256=source_sha256,
        build_input_sha256=build_input_sha256,
        agent_payload=payload,
        policy_report=policy_document,
    )


def plan(
    self: RecipeBuildService,
    recipe_revision_id: str,
    builder_node_id: str,
    *,
    now: datetime,
    resolution: RecipeBuildResolution | None = None,
) -> RecipeBuildPlan:
    """Prepare a build outside locks, then persist it in one short transaction."""
    prepared = self.prepare_plan(
        recipe_revision_id,
        builder_node_id,
        now=now,
        resolution=resolution,
    )
    with self._sessions.begin() as session:
        return self.persist_plan_in_session(session, prepared, now=now)


def reusable_build_id(self: RecipeBuildService, recipe_revision_id: str) -> str | None:
    """The succeeded build whose inputs this revision reproduces, whoever built it.

    The executable inputs identify an image, not the revision that first
    produced it, nor the builder binary that happened to build it: the
    same resolution that lets a download reuse a build. It needs no
    capacity and creates or changes nothing.
    """

    try:
        return self.resolve(recipe_revision_id).build_id
    except (UnknownOutcomeError, SecurityRefusalError):
        raise
    except (RecipeBuildError, KeyError, TypeError, ValueError):
        return None
