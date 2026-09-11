"""Read-only projection of immutable identities and their actual evidence sources."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentUpgradeResult,
    RecipeStartPayload,
    RecipeStartResult,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from .deployment_provenance_contract import (
    AgentDeploymentEvidence,
    ControllerBuildMetadata,
    DeploymentModelIdentity,
    DeploymentObservations,
    DeploymentProvenance,
    EvidenceAge,
    PhysicalAcceptanceEvidence,
    PlatformBoundary,
    PlatformBoundaryName,
    PlatformObservation,
    RankProvenance,
    RecipeLibraryEvidence,
    WorkloadProvenance,
)
from .models import (
    AgentNode,
    AgentNodeProfile,
    AgentOperation,
    AgentOperationAttempt,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    RecipeBuild,
    RecipeInstallation,
    RecipeLibrarySyncRun,
    RecipeRun,
    RunNode,
)

CONTROLLER_BUILD_METADATA = Path("/usr/local/share/vonk-forge/controller-build.json")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def local_deployment_observations() -> DeploymentObservations:
    """No GitHub/network reads; malformed configured evidence fails visibly."""
    path = os.environ.get("VONK_DEPLOYMENT_OBSERVATIONS_FILE")
    observations = (
        DeploymentObservations.model_validate_json(Path(path).read_bytes())
        if path else DeploymentObservations()
    )
    if observations.controller is None and CONTROLLER_BUILD_METADATA.is_file():
        build = ControllerBuildMetadata.model_validate_json(
            CONTROLLER_BUILD_METADATA.read_bytes()
        )
        observations.controller = PlatformObservation(
            source="Running Controller image build metadata",
            observed_at=datetime.now(UTC),
            source_commit=build.source_commit,
        )
    return observations


class DeploymentProvenanceService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        observations: Callable[
            [], DeploymentObservations
        ] = local_deployment_observations,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        stale_after_seconds: int = 300,
    ) -> None:
        self._sessions = sessions
        self._observations = observations
        self._clock = clock
        self._stale_after_seconds = stale_after_seconds

    def snapshot(self) -> DeploymentProvenance:
        now = _utc(self._clock())
        observations = self._observations()
        from .deployment_observer import stored_observation
        with self._sessions() as session:
            if observations.repository is None:
                observations.repository = stored_observation(session, "repository")
            if observations.publication is None:
                observations.publication = stored_observation(session, "publication")

        def age(source: str, observed_at: datetime | None = None) -> EvidenceAge:
            seconds = (
                max(0, int((now - _utc(observed_at)).total_seconds()))
                if observed_at
                else None
            )
            return EvidenceAge(
                source=source,
                observed_at=_utc(observed_at) if observed_at else None,
                age_seconds=seconds,
                freshness="unknown"
                if seconds is None
                else "stale"
                if seconds > self._stale_after_seconds
                else "current",
            )

        platform: list[PlatformBoundary] = []
        boundary_observations: tuple[
            tuple[PlatformBoundaryName, PlatformObservation | None], ...
        ] = (
            ("repository", observations.repository),
            ("publication", observations.publication),
            ("controller_deployment", observations.controller),
        )
        for name, observation in boundary_observations:
            platform.append(
                PlatformBoundary(
                    boundary=name,
                    state="observed" if observation else "unknown",
                    evidence=age(observation.source, observation.observed_at)
                    if observation
                    else age("No Controller observation"),
                    source_commit=observation.source_commit if observation else None,
                    image_digest=observation.image_digest if observation else None,
                    manifest_sha256=observation.manifest_sha256
                    if observation
                    else None,
                )
            )
        repo, publication, controller = platform
        if (
            repo.source_commit
            and publication.source_commit
            and repo.source_commit != publication.source_commit
        ):
            repo.state = "repository_not_published"
        if (
            publication.source_commit
            and controller.source_commit
            and publication.source_commit != controller.source_commit
        ) or (
            publication.image_digest
            and controller.image_digest
            and publication.image_digest != controller.image_digest
        ):
            publication.state = "publication_not_deployed"

        with self._sessions() as session:
            package_receipts = {}
            start_receipts = {}
            for operation, attempt in session.execute(
                select(AgentOperation, AgentOperationAttempt)
                .join(
                    AgentOperationAttempt,
                    AgentOperationAttempt.operation_id == AgentOperation.id,
                )
                .where(
                    AgentOperation.kind.in_(("agent.upgrade.v1", "recipe.start")),
                    AgentOperationAttempt.state == "succeeded",
                )
                .order_by(
                    AgentOperation.updated_at,
                    AgentOperation.id,
                    AgentOperationAttempt.attempt,
                )
            ):
                if operation.kind == "agent.upgrade.v1":
                    package_receipts[operation.node_id] = (
                        AgentUpgradeResult.model_validate(attempt.result),
                        operation.updated_at,
                    )
                else:
                    payload = RecipeStartPayload.model_validate(operation.payload)
                    start_receipts[(payload.run_id, operation.node_id)] = (
                        RecipeStartResult.model_validate(attempt.result),
                        operation.updated_at,
                    )
            profiles = {
                row.node_id: row for row in session.scalars(select(AgentNodeProfile))
            }
            agents = []
            for node in session.scalars(select(AgentNode).order_by(AgentNode.node_id)):
                evidence = age("Authenticated agent contact", node.last_seen_at)
                profile = profiles.get(node.node_id)
                package = package_receipts.get(node.node_id)
                package_matches = bool(
                    package
                    and package[0].binary_digest == node.binary_digest
                    and package[0].build_digest == node.build_digest
                )
                agents.append(
                    AgentDeploymentEvidence(
                        node_id=node.node_id,
                        display_name=profile.display_name if profile else node.node_id,
                        state=node.state,
                        connectivity="unknown"
                        if evidence.freshness == "unknown"
                        else "offline"
                        if evidence.freshness == "stale"
                        else "recent",
                        semantic_version=node.semantic_version,
                        build_digest=node.build_digest,
                        binary_sha256=node.binary_digest,
                        evidence=evidence,
                        package_sha256=package[0].package_sha256
                        if package is not None and package_matches
                        else None,
                        package_evidence=age(
                            "Authenticated package upgrade receipt", package[1]
                        )
                        if package is not None and package_matches
                        else age("No package receipt bound to the observed binary"),
                    )
                )
            sync = session.scalar(
                select(RecipeLibrarySyncRun)
                .where(RecipeLibrarySyncRun.state == "succeeded")
                .order_by(
                    RecipeLibrarySyncRun.created_at.desc(), RecipeLibrarySyncRun.id
                )
                .limit(1)
            )
            library = RecipeLibraryEvidence(
                repository=sync.repository if sync else None,
                source_commit=sync.observed_commit if sync else None,
                state=sync.state if sync else "unknown",
                evidence=age(
                    "Controller recipe-library sync",
                    sync.completed_at or sync.started_at,
                )
                if sync
                else age("No recipe-library sync receipt"),
            )
            workloads = []
            installations = session.scalars(
                select(RecipeInstallation)
                .where(RecipeInstallation.state != "uninstalled")
                .order_by(RecipeInstallation.id)
            ).all()
            for installation in installations:
                revision = session.get(
                    CatalogDocumentRevision, installation.recipe_revision_id
                )
                if revision is None:
                    raise ValueError("Installation recipe revision missing")
                recipe = RecipeDefinition.model_validate(revision.document)
                if content_sha256(recipe) != revision.content_digest:
                    raise ValueError("Persisted recipe identity mismatch")
                models = []
                for selection in sorted(
                    recipe.models, key=lambda selection: selection.id
                ):
                    reference = selection.model
                    model_revision = session.scalar(
                        select(CatalogDocumentRevision).where(
                            CatalogDocumentRevision.kind == "model",
                            CatalogDocumentRevision.publisher == reference.publisher,
                            CatalogDocumentRevision.slug == reference.slug,
                            CatalogDocumentRevision.content_digest
                            == reference.content_sha256,
                        )
                    )
                    if model_revision is None:
                        raise ValueError("Referenced model revision missing")
                    model = ModelDefinition.model_validate(model_revision.document)
                    if content_sha256(model) != reference.content_sha256:
                        raise ValueError("Persisted model identity mismatch")
                    models.append(
                        DeploymentModelIdentity(
                            selection_id=selection.id,
                            publisher=model.identity.publisher,
                            slug=model.identity.slug,
                            content_sha256=reference.content_sha256,
                            repository=model.source.repository,
                            revision=model.source.revision,
                            artifact_key=model_revision.artifact_key,
                        )
                    )
                mapping = session.get(ClusterMapping, installation.mapping_id)
                build = (
                    session.get(RecipeBuild, installation.recipe_build_id)
                    if installation.recipe_build_id
                    else None
                )
                run = session.scalar(
                    select(RecipeRun)
                    .where(RecipeRun.installation_id == installation.id)
                    .order_by(RecipeRun.created_at.desc(), RecipeRun.id)
                    .limit(1)
                )
                run_generation = run.run_generation if run is not None else None
                installed = {
                    row.node_id: row
                    for row in session.scalars(
                        select(InstallationNode).where(
                            InstallationNode.installation_id == installation.id
                        )
                    )
                }
                running = (
                    {
                        row.node_id: row
                        for row in session.scalars(
                            select(RunNode).where(RunNode.run_id == run.id)
                        )
                    }
                    if run
                    else {}
                )
                placements = session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id == installation.mapping_id)
                    .order_by(ClusterMappingNode.rank)
                ).all()
                ranks = []
                for placement in placements:
                    installed_node, run_node = (
                        installed.get(placement.node_id),
                        running.get(placement.node_id),
                    )
                    start = (
                        start_receipts.get((run.id, placement.node_id)) if run else None
                    )
                    start_evidence = start[0].evidence if start else None
                    agreement = "unknown"
                    if run_node and run_node.observed_run_generation is not None:
                        if run_node.observed_run_generation != run_generation:
                            agreement = "mismatch"
                        elif run_node.observation_receipt_sha256:
                            agreement = "match"
                    if start_evidence:
                        if (
                            start_evidence.image_digest != installation.image_digest
                            or start_evidence.recipe_content_sha256
                            != revision.content_digest
                            or start_evidence.rank != placement.rank
                            or start_evidence.run_generation != run_generation
                        ):
                            agreement = "mismatch"
                        elif agreement != "mismatch":
                            agreement = "match"
                    ranks.append(
                        RankProvenance(
                            node_id=placement.node_id,
                            rank=placement.rank,
                            role=placement.role,
                            installation_state=installed_node.state
                            if installed_node
                            else "unknown",
                            installation_evidence_sha256=installed_node.evidence_digest
                            if installed_node
                            else None,
                            installation_evidence=age(
                                "Controller installation receipt",
                                installed_node.updated_at if installed_node else None,
                            ),
                            runtime_state=run_node.state if run_node else "unknown",
                            observed_run_generation=run_node.observed_run_generation
                            if run_node
                            else None,
                            observation_receipt_sha256=run_node.observation_receipt_sha256
                            if run_node
                            else None,
                            observed_recipe_sha256=start_evidence.recipe_content_sha256
                            if start_evidence
                            else None,
                            observed_image_digest=start_evidence.image_digest
                            if start_evidence
                            else None,
                            observed_artifact_set_sha256=start_evidence.artifact_set_digest
                            if start_evidence
                            else None,
                            identity_agreement=agreement,
                            runtime_evidence=age(
                                "Authenticated run observation",
                                run_node.updated_at
                                if run_node and run_node.observation_receipt_sha256
                                else start[1]
                                if start
                                else None,
                            ),
                        )
                    )
                artifact_sets = {
                    rank.observed_artifact_set_sha256
                    for rank in ranks
                    if rank.observed_artifact_set_sha256
                }
                if len(artifact_sets) > 1:
                    for rank in ranks:
                        if rank.observed_artifact_set_sha256:
                            rank.identity_agreement = "mismatch"
                acceptance = PhysicalAcceptanceEvidence(
                    state="not_qualified",
                    evidence=age("No physical Spark receipt for this execution"),
                )
                receipts = sorted(
                    (
                        r
                        for r in observations.physical_acceptance
                        if run and r.run_id == run.id
                    ),
                    key=lambda r: (r.observed_at, r.evidence_sha256),
                    reverse=True,
                )
                if receipts:
                    receipt = receipts[0]
                    matches = (
                        receipt.run_generation == run_generation
                        and receipt.installation_id == installation.id
                        and receipt.recipe_sha256 == revision.content_digest
                        and receipt.image_digest == installation.image_digest
                        and sorted(receipt.node_ids)
                        == sorted(p.node_id for p in placements)
                    )
                    acceptance = PhysicalAcceptanceEvidence(
                        state=("accepted" if receipt.passed else "failed")
                        if matches
                        else "identity_mismatch",
                        evidence=age(receipt.source, receipt.observed_at),
                        evidence_sha256=receipt.evidence_sha256,
                    )
                rank_states = {rank.identity_agreement for rank in ranks}
                workloads.append(
                    WorkloadProvenance(
                        installation_id=installation.id,
                        installation_state=installation.state,
                        recipe_revision_id=revision.id,
                        recipe_revision_number=revision.revision_number,
                        recipe_publisher=revision.publisher,
                        recipe_slug=revision.slug,
                        recipe_content_sha256=revision.content_digest,
                        source_bundle_sha256=build.source_bundle_sha256
                        if build
                        else None,
                        build_id=build.id if build else None,
                        build_input_sha256=build.build_input_sha256 if build else None,
                        image_digest=installation.image_digest,
                        models=models,
                        mapping_id=installation.mapping_id,
                        mapping_generation=installation.mapping_generation,
                        current_mapping_generation=mapping.generation
                        if mapping
                        else None,
                        mapping_agreement="unknown"
                        if not mapping
                        else "match"
                        if mapping.generation == installation.mapping_generation
                        else "mismatch",
                        run_id=run.id if run else None,
                        run_generation=run_generation,
                        run_state=run.state if run else None,
                        rank_agreement="mismatch"
                        if "mismatch" in rank_states
                        else "match"
                        if rank_states == {"match"}
                        and mapping
                        and len(ranks) == mapping.node_count
                        else "unknown",
                        ranks=ranks,
                        physical_acceptance=acceptance,
                    )
                )
            return DeploymentProvenance(
                generated_at=now,
                platform=platform,
                recipe_library=library,
                agents=agents,
                workloads=workloads,
            )
