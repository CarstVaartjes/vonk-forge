"""Build evidence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    ModelFileState,
    RunSwitchCode,
)

from ..catalog_revision_contract import (
    CatalogRevisionContractError,
    RecipeRevisionProjection,
    read_catalog_projection,
)
from ..models import (
    AgentNode,
    CatalogDocumentRevision,
    NodeArtifact,
    RecipeBuild,
    RecipeSourceBundle,
)
from ..preparation_contract import (
    RuntimeImageIdentity,
)
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_build_plan,
)
from ..run_switch_contract import (
    BuildCompatibilityEvidence,
    BuildSourceEvidence,
    RunSwitchBuildEvidence,
    RunSwitchReason,
    RuntimeImageStorageImpact,
    SparkGroup,
)
from .constants import _BUILD_EVIDENCE_STATE_ADAPTER
from .identity_helpers import _is_hex_digest, _normalise_architecture
from .planning_helpers import _as_reason, _digest


class BuildEvidenceMixin:
    @staticmethod
    def _build_evidence(
        session: Session,
        revision: CatalogDocumentRevision | None,
        build: RecipeBuild | None,
        candidate: RecipeBuild | None,
        group: SparkGroup,
        *,
        require_available: bool = True,
        defer_source_build: bool = False,
        expected_image: RuntimeImageIdentity | None = None,
    ) -> tuple[
        RunSwitchBuildEvidence,
        RuntimeImageStorageImpact,
        list[RunSwitchReason],
        list[RunSwitchReason],
    ]:
        blockers: list[RunSwitchReason] = []
        warnings: list[RunSwitchReason] = []
        # Source packages are catalog-owned; recipe context paths carry no digest.
        projected: RecipeRevisionProjection | None = None
        if revision is not None:
            try:
                projection = read_catalog_projection(revision)
                if isinstance(projection, RecipeRevisionProjection):
                    projected = projection
            except CatalogRevisionContractError:
                pass
        # Every recipe image is built for the DGX Spark platform.
        expected_architecture = "linux/arm64"
        source_digest = (
            candidate.source_bundle_sha256
            if candidate is not None
            else projected.source_bundle_sha256
            if projected is not None
            else None
        )
        source_row = (
            session.get(RecipeSourceBundle, source_digest)
            if isinstance(source_digest, str)
            else None
        )
        source_state = "available" if source_row is not None else "missing"
        source = BuildSourceEvidence(
            state=source_state,
            source_bundle_sha256=(
                source_digest if _is_hex_digest(source_digest) else None
            ),
            detail=(
                None
                if source_row is not None
                else "The verified source bundle is not present in Controller storage."
            ),
        )
        observed_architecture: str | None = None
        candidate_plan_valid = True
        if candidate is not None:
            try:
                parse_stored_build_plan(candidate.plan)
            except RecipeExecutionContractError:
                candidate_plan_valid = False
                blockers.append(
                    _as_reason(
                        RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID,
                        "The persisted source-build plan is invalid.",
                        scope="operation",
                    )
                )
            else:
                observed_architecture = "linux/arm64"
            if candidate_plan_valid and observed_architecture is None:
                builder = session.get(AgentNode, candidate.builder_node_id)
                if builder is not None and isinstance(builder.architecture, str):
                    observed_architecture = _normalise_architecture(
                        builder.architecture
                    )
        node_architectures = tuple(
            _normalise_architecture(node.architecture)
            for node in session.scalars(
                select(AgentNode).where(
                    AgentNode.node_id.in_([item.node_id for item in group.nodes])
                )
            )
            if isinstance(node.architecture, str)
        )
        compatibility_state = "unknown"
        if expected_architecture != "unknown" and observed_architecture is not None:
            compatibility_state = (
                "compatible"
                if (
                    _normalise_architecture(expected_architecture)
                    == _normalise_architecture(observed_architecture)
                    and len(node_architectures) == len(group.nodes)
                    and all(
                        architecture == _normalise_architecture(expected_architecture)
                        for architecture in node_architectures
                    )
                )
                else "incompatible"
            )
        compatibility = BuildCompatibilityEvidence(
            expected_architecture=expected_architecture,
            observed_architecture=observed_architecture,
            state=compatibility_state,
            evidence_digest=(
                _digest(
                    {
                        "expected": expected_architecture,
                        "observed": observed_architecture,
                        "nodes": node_architectures,
                    }
                )
                if observed_architecture is not None
                else None
            ),
            detail=(
                None
                if compatibility_state != "incompatible"
                else "The built image or selected Spark group does not match the recipe platform."
            ),
        )
        # The accepted image is an execution constraint, never proof of bytes.
        # Preserve a newly observed output so the caller can reject identity drift.
        source_identity = build if build is not None else candidate
        image_digest = (
            source_identity.image_digest
            if source_identity is not None and source_identity.image_digest is not None
            else expected_image.image_digest
            if build is None and expected_image is not None
            else build.image_digest
            if build is not None
            else None
        )
        image_bytes = (
            source_identity.image_bytes
            if source_identity is not None and source_identity.image_bytes is not None
            else expected_image.image_bytes
            if build is None and expected_image is not None
            else build.image_bytes
            if build is not None
            else None
        )
        oci_layout = (
            source_identity.oci_layout_sha256
            if source_identity is not None
            and source_identity.oci_layout_sha256 is not None
            else expected_image.oci_layout_sha256
            if build is None and expected_image is not None
            else build.oci_layout_sha256
            if build is not None
            else None
        )
        runtime_reused = 0
        runtime_missing = 0
        runtime_missing_by_node: dict[str, int] = {}
        runtime_reclaimable = 0
        runtime_reclaimable_digests: set[str] = set()
        runtime_raw_digest = (
            image_digest.removeprefix("sha256:")
            if isinstance(image_digest, str)
            else None
        )
        if image_bytes is not None and runtime_raw_digest is not None:
            for node in group.nodes:
                artifact = session.scalar(
                    select(NodeArtifact).where(
                        NodeArtifact.node_id == node.node_id,
                        NodeArtifact.digest == runtime_raw_digest,
                    )
                )
                if (
                    artifact is not None
                    and artifact.kind == "image"
                    and artifact.state == ModelFileState.VERIFIED
                    and artifact.size_bytes >= image_bytes
                ):
                    runtime_reused += image_bytes
                else:
                    runtime_missing += image_bytes
                    runtime_missing_by_node[node.node_id] = image_bytes
                runtime_missing_by_node.setdefault(node.node_id, 0)
                if (
                    artifact is not None
                    and artifact.state == ModelFileState.VERIFIED
                    and artifact.ref_count == 0
                ):
                    runtime_reclaimable += artifact.size_bytes
                    runtime_reclaimable_digests.add(artifact.digest)
        runtime_coverage = (
            "complete"
            if image_bytes is not None and runtime_missing == 0
            else "partial"
            if image_bytes is not None
            else "unknown"
        )
        runtime = RuntimeImageStorageImpact(
            build_id=(
                build.id
                if build is not None
                else candidate.id
                if candidate is not None
                else None
            ),
            preparation_required=False,
            image_digest=image_digest,
            oci_layout_sha256=oci_layout,
            image_bytes=image_bytes,
            required_bytes=(
                image_bytes * len(group.nodes) if image_bytes is not None else None
            ),
            reused_bytes=runtime_reused,
            copied_bytes=runtime_missing,
            missing_nas_bytes=image_bytes if build is None else None,
            missing_spark_bytes=(runtime_missing if image_bytes is not None else None),
            missing_image_distribution_bytes=(
                runtime_missing if image_bytes is not None else None
            ),
            missing_image_distribution_bytes_by_node=(
                runtime_missing_by_node if image_bytes is not None else None
            ),
            nas_coverage=(
                "partial"
                if build is None
                else "complete"
                if image_bytes is not None and oci_layout is not None
                else "unknown"
            ),
            spark_coverage=runtime_coverage,
            reclaimable_bytes=runtime_reclaimable,
            reclaimable_digests=sorted(runtime_reclaimable_digests),
        )
        state = "missing" if candidate is None else str(candidate.state)
        detail: str | None = None
        if build is not None:
            state = "available"
            if not isinstance(oci_layout, str) or not _is_hex_digest(oci_layout):
                state = "incompatible"
                detail = "The successful build has no verified OCI layout identity."
        elif candidate is not None:
            if candidate.state == LifecycleState.SUCCEEDED.value:
                state = "missing"
                detail = (
                    "The recorded build has no currently usable Controller archive."
                )
            else:
                detail = candidate.error
        if compatibility_state == "incompatible":
            state = "incompatible"
        if require_available:
            if build is None:
                pending = candidate is not None and candidate.state in {
                    "planned",
                    "building",
                }
                if (pending or defer_source_build) and source_state == "available":
                    warnings.append(
                        _as_reason(
                            RunSwitchCode.CONTAINER_BUILD_REQUIRED,
                            (
                                "The accepted runtime image needs cache repair before target transfer."
                                if defer_source_build and not pending
                                else "The exact OCI runtime image is pinned to a durable Controller build and will be prepared before target transfer."
                            ),
                            scope="operation",
                            severity="warning",
                            node_ids=[node.node_id for node in group.nodes],
                        )
                    )
                else:
                    blockers.append(
                        _as_reason(
                            RunSwitchCode.RECIPE_BUILD_UNAVAILABLE,
                            (
                                "No successful immutable runtime image build is available."
                                if candidate is None
                                else f"Runtime image preparation is {candidate.state}: {candidate.error or 'no completed image receipt is available.'}"
                            ),
                            scope="operation",
                            node_ids=[node.node_id for node in group.nodes],
                        )
                    )
            if compatibility_state == "incompatible":
                blockers.append(
                    _as_reason(
                        RunSwitchCode.RECIPE_BUILD_INCOMPATIBLE,
                        compatibility.detail
                        or "Runtime image architecture is incompatible with the selected group.",
                        scope="operation",
                        node_ids=[node.node_id for node in group.nodes],
                    )
                )
            elif compatibility_state == "unknown":
                warnings.append(
                    _as_reason(
                        RunSwitchCode.RECIPE_BUILD_COMPATIBILITY_UNKNOWN,
                        "The immutable build receipt does not include enough architecture evidence to prove compatibility.",
                        scope="operation",
                        severity="warning",
                        node_ids=[node.node_id for node in group.nodes],
                    )
                )
        return (
            RunSwitchBuildEvidence(
                state=_BUILD_EVIDENCE_STATE_ADAPTER.validate_python(state, strict=True),
                build_id=build.id
                if build is not None
                else candidate.id
                if candidate is not None
                else None,
                build_input_sha256=(
                    build.build_input_sha256
                    if build is not None
                    else candidate.build_input_sha256
                    if candidate is not None
                    else None
                ),
                builder_node_id=(
                    build.builder_node_id
                    if build is not None
                    else candidate.builder_node_id
                    if candidate is not None
                    else None
                ),
                image_digest=image_digest,
                image_bytes=image_bytes,
                oci_layout_sha256=oci_layout,
                source=source,
                compatibility=compatibility,
                runtime=runtime,
                detail=detail,
            ),
            runtime,
            blockers,
            warnings,
        )
