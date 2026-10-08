"""Resolution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    LifecycleState,
    RunSwitchCode,
)
from vonk_forge_contracts import ModelDefinition

from ..cluster_mappings import (
    ClusterMappingError,
    effective_option_choices,
    mapping_option_choices,
)
from ..models import (
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    RecipeBuild,
    RecipeInstallation,
)
from ..preparation_contract import (
    RuntimeImageIdentity,
)
from ..recipe_execution_contract import (
    installation_serves_authorised_ports,
)
from ..recipe_runtime_specs import (
    OPTION_CHOICES_KEY,
    RecipeRuntimeSpecError,
    resolve_recipe_entities,
)
from ..run_switch_contract import (
    MappingSelection,
    RunSwitchReason,
    SparkGroup,
    SparkGroupNode,
)
from .planning_helpers import _as_reason

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class ResolutionMixin:
    def _resolve_documents(
        self,
        session: Session,
        revision: CatalogDocumentRevision | None,
        model_digest: str | None,
        *,
        requested_recipe_digest: str | None,
    ) -> tuple[
        ModelDefinition | None,
        Mapping[tuple[str, str, str], ModelDefinition],
        list[RunSwitchReason],
    ]:
        blockers: list[RunSwitchReason] = []
        model_document: ModelDefinition | None = None
        model_documents: dict[tuple[str, str, str], ModelDefinition] = {}
        if revision is None:
            return None, {}, blockers
        if (
            requested_recipe_digest is not None
            and revision.content_digest != requested_recipe_digest
        ):
            blockers.append(
                _as_reason(
                    RunSwitchCode.RECIPE_DIGEST_CHANGED,
                    "The selected recipe revision digest changed before planning.",
                    scope="recipe",
                    stale=True,
                )
            )
        try:
            resolved = resolve_recipe_entities(session, revision.document)
            resolved_model_items = resolved.model_revisions
            resolved_model = resolved_model_items[0] if resolved_model_items else None
            resolved_models = resolved.models
            for resolved_item in resolved_model_items:
                candidate_item = resolved_models.get(resolved_item.content_digest)
                if candidate_item is not None:
                    model_documents[
                        (
                            resolved_item.publisher,
                            resolved_item.slug,
                            resolved_item.content_digest,
                        )
                    ] = candidate_item
            model_document = (
                resolved_models.get(resolved_model.content_digest)
                if resolved_model is not None
                else None
            )
            if (
                model_digest is None
                or resolved_model is None
                or resolved_model.content_digest != model_digest
            ):
                blockers.append(
                    _as_reason(
                        RunSwitchCode.MODEL_REVISION_UNAVAILABLE,
                        "The exact model definition selected for this run is not resolved in local catalog authority.",
                        scope="model",
                    )
                )
        except (RecipeRuntimeSpecError, RuntimeError, TypeError, ValueError):
            blockers.append(
                _as_reason(
                    RunSwitchCode.RECIPE_DEPENDENCIES_UNAVAILABLE,
                    "Exact model and runtime dependencies could not be resolved from immutable catalog authority.",
                    scope="recipe",
                )
            )
        return model_document, model_documents, blockers

    def _resolve_mapping(
        self,
        session: Session,
        revision: CatalogDocumentRevision,
        group: SparkGroup,
        *,
        actor: str,
        option_choices: Mapping[str, str],
    ) -> tuple[
        ClusterMapping | None,
        MappingSelection | None,
        list[RunSwitchReason],
    ]:
        service = typing_cast("RunSwitchOperationService", self)
        try:
            wanted_choices = effective_option_choices(revision.document, option_choices)
        except ClusterMappingError as error:
            return (
                None,
                None,
                [
                    _as_reason(
                        RunSwitchCode.OPTION_INVALID,
                        str(error),
                        scope="mapping",
                    )
                ],
            )
        desired = tuple(
            (node.node_id, node.rank, node.role, node.endpoint_owner)
            for node in group.nodes
        )
        desired_ids = tuple(node.node_id for node in group.nodes)
        mappings = tuple(
            session.scalars(
                select(ClusterMapping)
                .where(
                    ClusterMapping.recipe_revision_id == revision.id,
                    ClusterMapping.state == "ready",
                )
                .order_by(ClusterMapping.generation.desc(), ClusterMapping.id)
            )
        )
        for mapping in mappings:
            nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id == mapping.id)
                    .order_by(ClusterMappingNode.rank)
                )
            )
            actual = tuple(
                (node.node_id, node.rank, node.role, node.endpoint_owner)
                for node in nodes
            )
            if actual != desired:
                continue
            # A different choice of recipe options is a different mapping (and
            # so a new installation and run) over the same cached model and image.
            if mapping_option_choices(mapping.parameters) != wanted_choices:
                continue
            return mapping, service._mapping_selection(mapping, nodes), []
        try:
            plan = service._mappings.preview(
                revision.id,
                desired_ids,
                {OPTION_CHOICES_KEY: wanted_choices} if wanted_choices else {},
                actor,
            )
        except (
            ClusterMappingError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            return (
                None,
                None,
                [
                    _as_reason(
                        RunSwitchCode.MAPPING_INVALID,
                        f"The selected Spark group cannot satisfy the exact recipe topology: {error}",
                        scope="mapping",
                        node_ids=desired_ids,
                    )
                ],
            )
        planned = tuple(
            (node.node_id, node.rank, node.role, node.endpoint_owner)
            for node in plan.nodes
        )
        if planned != desired:
            return (
                None,
                None,
                [
                    _as_reason(
                        RunSwitchCode.MAPPING_GROUP_MISMATCH,
                        "The selected Spark ranks and roles do not form the complete topology required by the recipe.",
                        scope="group",
                        node_ids=desired_ids,
                    )
                ],
            )
        return (
            None,
            MappingSelection(
                mapping_id=None,
                mapping_generation=plan.generation,
                topology_name=plan.topology_name,
                parameters=dict(plan.parameters),
                option_choices=mapping_option_choices(plan.parameters),
                placement_digest=plan.placement_digest,
                action="create",
                nodes=[
                    SparkGroupNode(
                        node_id=node.node_id,
                        rank=node.rank,
                        role=node.role,
                        endpoint_owner=node.endpoint_owner,
                    )
                    for node in plan.nodes
                ],
            ),
            [],
        )

    def _matching_installation(
        self,
        session: Session,
        revision_id: str,
        model_digest: str,
        mapping: ClusterMapping | None,
        group: SparkGroup,
    ) -> RecipeInstallation | None:
        if mapping is None:
            return None
        candidates = tuple(
            session.scalars(
                select(RecipeInstallation)
                .where(
                    RecipeInstallation.recipe_revision_id == revision_id,
                    RecipeInstallation.mapping_id == mapping.id,
                    RecipeInstallation.mapping_generation == mapping.generation,
                    RecipeInstallation.model_content_sha256 == model_digest,
                    RecipeInstallation.state.in_(
                        (
                            InstallationState.INSTALLED,
                            InstallationState.INSTALLING,
                            InstallationState.PARTIAL,
                        )
                    ),
                )
                .order_by(
                    RecipeInstallation.state.desc(),
                    RecipeInstallation.updated_at.desc(),
                )
            )
        )
        desired = {(node.node_id, node.rank, node.role) for node in group.nodes}
        for installation in candidates:
            if not installation_serves_authorised_ports(installation):
                continue
            installed = {
                (node.node_id, node.rank, node.role)
                for node in session.scalars(
                    select(InstallationNode).where(
                        InstallationNode.installation_id == installation.id
                    )
                )
                if node.state == InstallationNodeState.INSTALLED
            }
            if installed == desired:
                return installation
        return None

    def _build_is_available(self, candidate: RecipeBuild) -> bool:
        """Whether the recorded receipt still proves a present prepared image."""
        service = typing_cast("RunSwitchOperationService", self)

        if (
            candidate.state != LifecycleState.SUCCEEDED.value
            or candidate.image_digest is None
            or candidate.oci_layout_sha256 is None
            or type(candidate.image_bytes) is not int
        ):
            return False
        return (
            service._build_archive_available is None
            or service._build_archive_available(
                candidate.oci_layout_sha256, candidate.image_bytes
            )
        )

    def _matching_build(
        self,
        session: Session,
        revision_id: str,
        *,
        expected_image: RuntimeImageIdentity | None,
    ) -> RecipeBuild | None:
        service = typing_cast("RunSwitchOperationService", self)
        if expected_image is not None:
            # Accepted work remains bound to its approved receipt even when a
            # newer completed build becomes available while it is waiting. The
            # image is identified by its content: the receipt names the exact
            # build, and its digest is compared to the compiled image later.
            build = (
                session.get(RecipeBuild, expected_image.build_id)
                if expected_image.build_id is not None
                else None
            )
            if build is None or not service._build_is_available(build):
                return None
            return build

        # A fresh review selects the current completed image of this revision.
        # A successor with the same executable inputs finds its predecessor's
        # build by content in ``_select_build``.
        candidates = session.scalars(
            select(RecipeBuild)
            .where(
                RecipeBuild.recipe_revision_id == revision_id,
                RecipeBuild.state == LifecycleState.SUCCEEDED.value,
                RecipeBuild.image_digest.is_not(None),
                RecipeBuild.image_bytes.is_not(None),
            )
            .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
        )
        for candidate in candidates:
            if service._build_is_available(candidate):
                return candidate
        return None

    @staticmethod
    def _latest_build(session: Session, revision_id: str) -> RecipeBuild | None:
        return session.scalar(
            select(RecipeBuild)
            .where(RecipeBuild.recipe_revision_id == revision_id)
            .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id)
            .limit(1)
        )
