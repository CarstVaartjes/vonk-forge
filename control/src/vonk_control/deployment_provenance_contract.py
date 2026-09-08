"""Controller-owned evidence boundaries; absent evidence never implies deployment."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints

from .strict_json import StrictJSONModel

Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Commit = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]
ImageDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]


class ProvenanceModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ControllerBuildMetadata(ProvenanceModel):
    source_commit: Commit


class EvidenceAge(ProvenanceModel):
    source: str
    observed_at: datetime | None = None
    age_seconds: int | None = Field(default=None, ge=0)
    freshness: Literal["current", "stale", "unknown"] = "unknown"


class PlatformObservation(ProvenanceModel):
    source: str
    observed_at: datetime
    source_commit: Commit | None = None
    image_digest: ImageDigest | None = None
    manifest_sha256: Sha256 | None = None


class PhysicalAcceptanceReceipt(ProvenanceModel):
    """An explicit physical-lane receipt, bound to a particular execution."""

    source: str
    observed_at: datetime
    evidence_sha256: Sha256
    run_id: str
    run_generation: int = Field(ge=1)
    installation_id: str
    recipe_sha256: Sha256
    image_digest: ImageDigest
    node_ids: list[str] = Field(min_length=1)
    passed: bool
    lane: Literal["physical-spark"]


class DeploymentObservations(ProvenanceModel):
    """Locally supplied observations, collected outside request hot paths."""

    schema_version: Literal[2] = 2
    repository: PlatformObservation | None = None
    publication: PlatformObservation | None = None
    controller: PlatformObservation | None = None
    physical_acceptance: list[PhysicalAcceptanceReceipt] = Field(default_factory=list)


class PlatformBoundary(ProvenanceModel):
    boundary: Literal["repository", "publication", "controller_deployment"]
    state: Literal[
        "observed", "unknown", "repository_not_published", "publication_not_deployed"
    ]
    evidence: EvidenceAge
    source_commit: Commit | None = None
    image_digest: ImageDigest | None = None
    manifest_sha256: Sha256 | None = None


class RecipeLibraryEvidence(ProvenanceModel):
    repository: str | None = None
    source_commit: Commit | None = None
    state: str
    evidence: EvidenceAge


class DeploymentModelIdentity(ProvenanceModel):
    selection_id: str
    publisher: str
    slug: str
    content_sha256: Sha256
    repository: str
    revision: str
    artifact_key: Sha256 | None = None


class RankProvenance(ProvenanceModel):
    node_id: str
    rank: int = Field(ge=0)
    role: str
    installation_state: str
    installation_evidence_sha256: Sha256 | None = None
    installation_evidence: EvidenceAge
    runtime_state: str
    observed_run_generation: int | None = None
    observation_receipt_sha256: Sha256 | None = None
    observed_recipe_sha256: Sha256 | None = None
    observed_image_digest: ImageDigest | None = None
    observed_artifact_set_sha256: Sha256 | None = None
    identity_agreement: Literal["match", "mismatch", "unknown"]
    runtime_evidence: EvidenceAge


class PhysicalAcceptanceEvidence(ProvenanceModel):
    boundary: Literal["physical_acceptance"] = "physical_acceptance"
    state: Literal["accepted", "failed", "not_qualified", "identity_mismatch"]
    evidence: EvidenceAge
    evidence_sha256: Sha256 | None = None


class WorkloadProvenance(ProvenanceModel):
    installation_id: str
    installation_state: str
    recipe_revision_id: str
    recipe_revision_number: int
    recipe_publisher: str
    recipe_slug: str
    recipe_content_sha256: Sha256
    source_bundle_sha256: Sha256 | None = None
    build_id: str | None = None
    build_input_sha256: Sha256 | None = None
    image_digest: ImageDigest
    models: list[DeploymentModelIdentity]
    mapping_id: str
    mapping_generation: int
    current_mapping_generation: int | None = None
    mapping_agreement: Literal["match", "mismatch", "unknown"]
    run_id: str | None = None
    run_generation: int | None = None
    run_state: str | None = None
    rank_agreement: Literal["match", "mismatch", "unknown"]
    ranks: list[RankProvenance]
    physical_acceptance: PhysicalAcceptanceEvidence


class AgentDeploymentEvidence(ProvenanceModel):
    boundary: Literal["agent_deployment"] = "agent_deployment"
    node_id: str
    display_name: str
    state: str
    connectivity: Literal["recent", "offline", "unknown"]
    semantic_version: str | None = None
    build_digest: str | None = None
    binary_sha256: Sha256 | None = None
    package_sha256: Sha256 | None = None
    package_evidence: EvidenceAge
    evidence: EvidenceAge


class DeploymentProvenance(ProvenanceModel):
    schema_version: Literal[2] = 2
    generated_at: datetime
    platform: list[PlatformBoundary]
    recipe_library: RecipeLibraryEvidence
    agents: list[AgentDeploymentEvidence]
    workloads: list[WorkloadProvenance]
