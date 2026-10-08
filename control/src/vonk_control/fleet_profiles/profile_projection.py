"""Profile projection for Fleet profiles."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ModelCacheBlockerCode
from vonk_agent_protocol.agent_words import ProfileDocumentState
from vonk_forge_contracts import RecipeDefinition

from ..catalog_revision_contract import read_catalog_document
from ..fleet_profile_contract import (
    FleetAssignmentModelView,
    FleetAssignmentRecipeView,
    FleetCacheSummary,
    FleetNodeView,
    FleetProfileAssignment,
    FleetProfileAssignmentInput,
    FleetProfileAssignmentView,
    FleetProfileView,
)
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..model_cache_contract import CachedResourceEstimate
from ..models import (
    AgentNode,
    AgentNodeProfile,
    CatalogDocument,
    CatalogDocumentRevision,
    FleetProfile,
)
from ..recipe_runtime_specs import recipe_topology, resolve_recipe_entities
from ..recipe_update_notice import RecipeUpdateNotice, recipe_update_notice
from .dependencies import (
    _INSTALLATION_POLICY_ADAPTER,
    _LOGGER,
    _OBSERVED_ASSIGNMENT_LABELS,
)
from .persistence import _stored_recipe
from .projection_support import (
    _aware,
    _digest,
    _effective_option_choices,
    _profile_document,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def _view(self, session: Session, row: FleetProfile) -> FleetProfileView:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        choices = self._choices(row)
        assignments: list[FleetProfileAssignmentView] = []
        cache_cached = 0
        cache_missing = 0
        cache_unknown = 0
        warnings: list[str] = []
        selection = self._selected_profile_snapshot(session)
        if isinstance(selection, Residue):
            # Preserve draft reads even if the selected receipt is damaged.
            # The application worker owns failing that receipt explicitly.
            selection = None
            warnings.append("The selected profile application is unreadable")
        loaded_assignments = (
            selection.intended.assignments
            if selection is not None and selection.profile_id == row.id
            else None
        )
        assigned_nodes = (
            {
                node.node_id
                for assignment in loaded_assignments
                for node in assignment.nodes
            }
            if loaded_assignments is not None
            else {node_id for choice in choices for node_id in choice.spark_ids}
        )
        for choice in choices:
            resolved_head = self._recipe_document(session, choice.recipe_selector)
            head_error: str | None = None
            if isinstance(resolved_head, Residue):
                head_error = resolved_head.note
            else:
                try:
                    read_catalog_document(resolved_head[1])
                except ValueError as error:
                    head_error = str(error)
            if head_error is not None:
                # No readable (or no active) revision exists for this recipe (the
                # identity lookup already prefers the newest readable one). Show the
                # choice as needing attention instead of failing the profile.
                _LOGGER.warning(
                    "profile %s choice %s needs attention: %s",
                    row.id,
                    choice.recipe_selector,
                    head_error,
                )
                warnings.append(
                    f"Recipe {choice.recipe_selector} needs attention: "
                    "no readable revision is available; the recipe catalog "
                    "sync will replace it"
                )
                cache_unknown += 1
                assignments.append(
                    self._attention_assignment(
                        session, choice, loaded_assignments=loaded_assignments
                    )
                )
                continue
            resolved_choice = self._resolve_choice(session, choice)
            if isinstance(resolved_choice, Residue):
                # (Unreachable in practice: the head resolved just above.)
                cache_unknown += 1
                assignments.append(
                    self._attention_assignment(
                        session, choice, loaded_assignments=loaded_assignments
                    )
                )
                continue
            recipe, revision, cache = resolved_choice
            required = recipe_topology(_stored_recipe(revision)).node_count
            if self._cache_resolver is None:
                warnings.append(
                    "Cache resolution is unavailable until the cache service is configured"
                )
            recipe_state = (
                "Cached"
                if cache is not None and cache.recipe.cached
                else "Recipe not cached"
            )
            if cache is not None and (
                ModelCacheBlockerCode.RECIPE_NOT_CACHED in cache.blockers
            ):
                # The selected exact revision is bound into the profile, so the
                # load prepares this cache entry rather than accept a
                # silently different older revision.
                warnings.append(
                    f"Recipe {recipe.publisher}/{recipe.slug} revision "
                    f"{revision.revision_number} is not in the local cache; "
                    "the Controller prepares the exact cache entry when the profile loads"
                )
            model_state = (
                "Cached"
                if cache is not None and cache.model.cached
                else "Model not cached"
            )
            if cache is None:
                cache_unknown += 1
            elif recipe_state == "Cached" and model_state == "Cached":
                cache_cached += 1
            else:
                cache_missing += 1
            resources = (
                cache.resources if cache is not None else CachedResourceEstimate()
            )
            try:
                model_document = resolve_recipe_entities(
                    session,
                    _stored_recipe(revision).model_dump(mode="json"),
                ).model_revisions
            except (RuntimeError, TypeError, ValueError) as error:
                unavailable = retire_as_unknown(
                    "profile-model",
                    revision.id,
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    str(error),
                )
                warnings.append(unavailable.note)
                assignments.append(
                    self._attention_assignment(
                        session, choice, loaded_assignments=loaded_assignments
                    )
                )
                continue
            candidate_model = next(iter(model_document), None)
            model = (
                candidate_model
                if isinstance(candidate_model, CatalogDocumentRevision)
                else None
            )
            model_selector = None
            model_name = None
            if model is not None:
                model_selector = f"{model.publisher}/{model.slug}"
                model_name = self._model_title(session, _stored_recipe(revision))
            assignment_selector = self._assignment_selector(choice)
            effective_choices, choice_notes = _effective_option_choices(
                _stored_recipe(revision),
                choice.option_choices,
            )
            warnings.extend(
                f"{recipe.publisher}/{recipe.slug}: {note}" for note in choice_notes
            )
            recipe_update = self._loaded_recipe_update(
                session, loaded_assignments, recipe, revision, choice.spark_ids
            )
            assignments.append(
                FleetProfileAssignmentView(
                    selector=assignment_selector,
                    display_name=recipe.title,
                    recipe_selector=f"{recipe.publisher}/{recipe.slug}",
                    recipe_id=recipe.id,
                    spark_ids=list(choice.spark_ids),
                    required_sparks=required,
                    assigned_sparks=len(choice.spark_ids),
                    model=FleetAssignmentModelView(
                        selector=model_selector,
                        name=model_name,
                        variant=choice.model_variant,
                        state=model_state,
                        content_sha256=(
                            cache.model.content_sha256 if cache is not None else None
                        ),
                    ),
                    recipe=FleetAssignmentRecipeView(
                        selector=f"{recipe.publisher}/{recipe.slug}",
                        name=recipe.title,
                        state=recipe_state,
                        revision_id=revision.id,
                    ),
                    resources=resources,
                    option_choices=effective_choices,
                    observed_state=self._observed_assignment_state(
                        session,
                        loaded_assignments,
                        recipe_id=recipe.id,
                        spark_ids=choice.spark_ids,
                    ),
                    recipe_update=recipe_update,
                )
            )
        roster = tuple(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.revoked_at.is_(None))
                .order_by(AgentNode.node_id)
            )
        )
        display_names = {
            item.node_id: item.display_name
            for item in session.scalars(
                select(AgentNodeProfile).where(
                    AgentNodeProfile.node_id.in_([node.node_id for node in roster])
                )
            )
        }
        fleet = [
            FleetNodeView(
                selector=node.node_id,
                display_name=display_names.get(node.node_id, node.node_id),
                state="Assigned" if node.node_id in assigned_nodes else "Idle",
            )
            for node in roster
        ]
        document = _profile_document(row)
        loaded_revision = (
            selection.profile_revision
            if selection is not None and selection.profile_id == row.id
            else None
        )
        return FleetProfileView(
            id=row.id,
            number=row.number,
            revision=row.revision,
            name=row.name,
            description=row.description,
            installation_policy=_INSTALLATION_POLICY_ADAPTER.validate_python(
                row.installation_policy, strict=True
            ),
            labels=dict(row.labels),
            favorite=row.favorite,
            definition=self._definition(row),
            assignments=assignments,
            fleet=fleet,
            status=ProfileDocumentState.LOADED.value
            if loaded_revision is not None
            else ProfileDocumentState.DRAFT.value,
            loaded_revision=loaded_revision,
            cache_summary=FleetCacheSummary(
                cached=cache_cached, missing=cache_missing, unknown=cache_unknown
            ),
            warnings=sorted(set(warnings)),
            next_actions=[f"vonkctl --profile {row.number} profile load"],
            profile_digest=_digest(document),
            created_by=row.created_by,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _loaded_recipe_update(
        session: Session,
        loaded_assignments: Sequence[FleetProfileAssignment] | None,
        recipe: CatalogDocument,
        newest: CatalogDocumentRevision,
        spark_ids: Sequence[str],
    ) -> RecipeUpdateNotice | None:
        """Say when the loaded workload runs an older revision than `newest`.

        Load and run always resolve the newest revision, so this only ever
        describes what is already running; it never restarts anything.
        """

        if loaded_assignments is None:
            return None
        nodes = set(spark_ids)
        loaded = next(
            (
                assignment
                for assignment in loaded_assignments
                if assignment.recipe_id == recipe.id
                and {node.node_id for node in assignment.nodes} == nodes
            ),
            None,
        )
        if loaded is None or loaded.recipe_revision_id == newest.id:
            return None
        running = session.get(CatalogDocumentRevision, loaded.recipe_revision_id)
        if running is None:
            return None
        return recipe_update_notice(recipe.title, running, newest)

    @classmethod
    def _observed_assignment_state(
        cls,
        session: Session,
        loaded_assignments: Sequence[FleetProfileAssignment] | None,
        *,
        recipe_id: str,
        spark_ids: Sequence[str],
    ) -> str:
        """Project one saved assignment onto the live loaded application.

        Only the currently selected application's exact assignment says what
        is loaded; a saved edit that application does not contain is honestly
        "Not loaded".  The label comes from the same assignment-state predicate
        that planning and endpoint publication use, never a second opinion.
        """
        cls = _typing_cast("type[_FleetProfileService]", cls)  # noqa: PLW0642 -- assembled mixin interface

        if loaded_assignments is None:
            return "Not loaded"
        nodes = set(spark_ids)
        loaded = next(
            (
                assignment
                for assignment in loaded_assignments
                if assignment.recipe_id == recipe_id
                and {node.node_id for node in assignment.nodes} == nodes
            ),
            None,
        )
        if loaded is None:
            return "Not loaded"
        return _OBSERVED_ASSIGNMENT_LABELS[
            cls._assignment_state(session, loaded).current_state
        ]

    def _attention_assignment(
        self,
        session: Session,
        choice: FleetProfileAssignmentInput,
        *,
        loaded_assignments: Sequence[FleetProfileAssignment] | None,
    ) -> FleetProfileAssignmentView:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        document_id = session.scalar(
            select(CatalogDocument.id).where(
                CatalogDocument.kind == "recipe",
                CatalogDocument.publisher == choice.recipe_selector.split("/")[0],
                CatalogDocument.slug == choice.recipe_selector.split("/")[-1],
            )
        )
        return FleetProfileAssignmentView(
            selector=self._assignment_selector(choice),
            display_name=choice.recipe_selector,
            recipe_selector=choice.recipe_selector,
            recipe_id=document_id,
            spark_ids=list(choice.spark_ids),
            assigned_sparks=len(choice.spark_ids),
            model=FleetAssignmentModelView(
                variant=choice.model_variant, state="Needs attention"
            ),
            recipe=FleetAssignmentRecipeView(
                selector=choice.recipe_selector, state="Needs attention"
            ),
            observed_state=(
                self._observed_assignment_state(
                    session,
                    loaded_assignments,
                    recipe_id=document_id,
                    spark_ids=choice.spark_ids,
                )
                if document_id is not None
                else "Not loaded"
            ),
        )

    @staticmethod
    def _model_title(session: Session, document: RecipeDefinition) -> str | None:
        # Both call paths validate the same persisted recipe document through
        # ``recipe_topology`` before this projection runs, so a corrupt
        # document already raises there. The only resolution failure reachable
        # here is a referenced model revision that is not currently active,
        # for which a missing display title is deliberate.
        try:
            models = resolve_recipe_entities(
                session, document.model_dump(mode="json")
            ).model_revisions
        except (KeyError, RuntimeError, TypeError, ValueError):
            return None
        if not models:
            return None
        model = models[0]
        root = session.get(CatalogDocument, model.document_id)
        return root.title if root is not None else f"{model.publisher}/{model.slug}"
